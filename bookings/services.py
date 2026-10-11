"""Booking coordinator: the only place that moves a booking through its lifecycle.

Layering of the double-booking defence:

1. Redis lock per resource - cheap fast-fail under contention, keeps the hot path
   from hammering Postgres with doomed inserts.
2. Postgres exclusion constraint - the source of truth. Even if the lock is lost
   (TTL expiry, Redis failover) two active overlapping bookings cannot be committed.

The lock is acquired *outside* the transaction and released only after commit, so
the next holder always sees the previous holder's row.
"""

from collections.abc import Collection
from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone
from redis import Redis

from bookings.locks import LockNotAcquired, redis_lock, resource_lock_key
from bookings.models import Booking, BookingStatus, Resource

DEFAULT_HOLD_TTL = timedelta(minutes=10)


class BookingError(Exception):
    pass


class SlotUnavailable(BookingError):
    """The requested slot is taken or is being taken right now."""


class InvalidTransition(BookingError):
    def __init__(self, booking: Booking, target: BookingStatus) -> None:
        super().__init__(f"Booking {booking.pk}: cannot go from {booking.status} to {target}")
        self.booking = booking
        self.target = target


class HoldExpired(BookingError):
    """Payment arrived after the hold deadline; the booking has been marked EXPIRED."""

    def __init__(self, booking: Booking, deadline: datetime) -> None:
        super().__init__(f"Booking {booking.pk}: hold expired at {deadline.isoformat()}")
        self.booking = booking
        self.deadline = deadline


class BookingCoordinator:
    def __init__(
        self,
        hold_ttl: timedelta = DEFAULT_HOLD_TTL,
        lock_ttl_ms: int = 10_000,
        redis_client: Redis | None = None,
    ) -> None:
        self.hold_ttl = hold_ttl
        self.lock_ttl_ms = lock_ttl_ms
        self.redis_client = redis_client

    def hold(
        self,
        resource: Resource,
        user: User,
        start_time: datetime,
        end_time: datetime,
    ) -> Booking:
        """Reserve a slot as PENDING until payment confirms it or the hold expires."""
        if end_time <= start_time:
            raise ValueError("end_time must be after start_time")
        now = timezone.now()
        if start_time <= now:
            raise ValueError("start_time must be in the future")

        try:
            with redis_lock(
                resource_lock_key(resource.pk), ttl_ms=self.lock_ttl_ms, client=self.redis_client
            ):
                try:
                    with transaction.atomic():
                        return Booking.objects.create(
                            resource=resource,
                            user=user,
                            start_time=start_time,
                            end_time=end_time,
                            status=BookingStatus.PENDING,
                            expires_at=now + self.hold_ttl,
                        )
                except IntegrityError as exc:
                    raise SlotUnavailable("Slot overlaps an active booking") from exc
        except LockNotAcquired as exc:
            raise SlotUnavailable("Slot is being booked by someone else") from exc

    def confirm(self, booking_id: int) -> Booking:
        """PENDING -> CONFIRMED, or PENDING -> EXPIRED if the hold deadline has passed."""
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=booking_id)
            self._ensure_from(booking, (BookingStatus.PENDING,), BookingStatus.CONFIRMED)
            deadline = booking.expires_at
            expired = deadline is not None and deadline <= timezone.now()
            self._set_status(booking, BookingStatus.EXPIRED if expired else BookingStatus.CONFIRMED)
        # Raised after commit so the EXPIRED status is persisted for the caller to act on.
        if expired and deadline is not None:
            raise HoldExpired(booking, deadline)
        return booking

    def fail_payment(self, booking_id: int) -> Booking:
        return self._transition(booking_id, (BookingStatus.PENDING,), BookingStatus.PAYMENT_FAILED)

    def cancel(self, booking_id: int) -> Booking:
        return self._transition(
            booking_id, (BookingStatus.PENDING, BookingStatus.CONFIRMED), BookingStatus.CANCELLED
        )

    def expire_stale(self, now: datetime | None = None) -> int:
        """Release every PENDING hold past its deadline. Safe to run concurrently.

        A single conditional UPDATE: if confirm() holds the row lock, Postgres re-checks
        the WHERE clause after it commits and skips the now-CONFIRMED row.
        """
        now = now or timezone.now()
        return Booking.objects.filter(status=BookingStatus.PENDING, expires_at__lte=now).update(
            status=BookingStatus.EXPIRED, expires_at=None, updated_at=now
        )

    def _transition(
        self, booking_id: int, allowed_from: Collection[str], target: BookingStatus
    ) -> Booking:
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=booking_id)
            self._ensure_from(booking, allowed_from, target)
            self._set_status(booking, target)
        return booking

    @staticmethod
    def _ensure_from(
        booking: Booking, allowed_from: Collection[str], target: BookingStatus
    ) -> None:
        if booking.status not in allowed_from:
            raise InvalidTransition(booking, target)

    @staticmethod
    def _set_status(booking: Booking, status: BookingStatus) -> None:
        booking.status = status
        booking.expires_at = None
        booking.save(update_fields=["status", "expires_at", "updated_at"])
