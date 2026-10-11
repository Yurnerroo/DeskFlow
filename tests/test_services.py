from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from bookings.locks import RedisLock, resource_lock_key
from bookings.models import Booking, BookingStatus, Resource
from bookings.services import (
    BookingCoordinator,
    HoldExpired,
    InvalidTransition,
    SlotUnavailable,
)
from tests.factories import BookingFactory, UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def coordinator() -> BookingCoordinator:
    return BookingCoordinator()


@pytest.fixture
def slot() -> tuple[datetime, datetime]:
    start = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
    return start, start + timedelta(hours=1)


@pytest.fixture
def held_lock(resource: Resource) -> Iterator[RedisLock]:
    lock = RedisLock(resource_lock_key(resource.pk))
    assert lock.acquire()
    yield lock
    lock.release()


def _hold(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
) -> Booking:
    return coordinator.hold(resource, user, *slot)


def test_hold_creates_pending_booking_with_deadline(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
) -> None:
    before = timezone.now()
    booking = _hold(coordinator, resource, user, slot)

    assert booking.status == BookingStatus.PENDING
    assert booking.expires_at is not None
    assert booking.expires_at >= before + coordinator.hold_ttl


def test_overlapping_hold_is_rejected(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
) -> None:
    _hold(coordinator, resource, user, slot)
    start, end = slot

    with pytest.raises(SlotUnavailable):
        coordinator.hold(resource, UserFactory.create(), start + timedelta(minutes=30), end)

    assert Booking.objects.filter(resource=resource).count() == 1


def test_adjacent_hold_is_allowed(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
) -> None:
    _, end = slot
    _hold(coordinator, resource, user, slot)

    booking = coordinator.hold(resource, user, end, end + timedelta(hours=1))

    assert booking.status == BookingStatus.PENDING


def test_hold_fails_fast_when_resource_lock_is_taken(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
    held_lock: RedisLock,
) -> None:
    with pytest.raises(SlotUnavailable):
        _hold(coordinator, resource, user, slot)

    assert not Booking.objects.exists()


def test_hold_releases_lock_after_success(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
) -> None:
    _hold(coordinator, resource, user, slot)

    lock = RedisLock(resource_lock_key(resource.pk))
    assert lock.acquire()
    lock.release()


@pytest.mark.parametrize(
    ("start_offset", "duration"),
    [(timedelta(hours=1), timedelta(0)), (timedelta(hours=-1), timedelta(hours=1))],
    ids=["empty-range", "in-the-past"],
)
def test_hold_validates_time_range(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    start_offset: timedelta,
    duration: timedelta,
) -> None:
    start = timezone.now() + start_offset

    with pytest.raises(ValueError):
        coordinator.hold(resource, user, start, start + duration)


def test_confirm_moves_pending_to_confirmed(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
) -> None:
    booking = _hold(coordinator, resource, user, slot)

    confirmed = coordinator.confirm(booking.pk)

    assert confirmed.status == BookingStatus.CONFIRMED
    assert confirmed.expires_at is None


def test_confirm_after_deadline_expires_booking(coordinator: BookingCoordinator) -> None:
    deadline = timezone.now() - timedelta(seconds=1)
    booking = BookingFactory.create(expires_at=deadline)

    with pytest.raises(HoldExpired) as exc_info:
        coordinator.confirm(booking.pk)

    assert exc_info.value.deadline == deadline
    booking.refresh_from_db()
    assert booking.status == BookingStatus.EXPIRED


def test_fail_payment_releases_slot(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
) -> None:
    booking = _hold(coordinator, resource, user, slot)

    failed = coordinator.fail_payment(booking.pk)

    assert failed.status == BookingStatus.PAYMENT_FAILED
    assert _hold(coordinator, resource, user, slot).status == BookingStatus.PENDING


def test_cancel_confirmed_booking(coordinator: BookingCoordinator) -> None:
    booking = BookingFactory.create(status=BookingStatus.CONFIRMED)

    assert coordinator.cancel(booking.pk).status == BookingStatus.CANCELLED


@pytest.mark.parametrize(
    "status",
    [BookingStatus.CONFIRMED, BookingStatus.EXPIRED, BookingStatus.CANCELLED],
)
def test_confirm_rejects_non_pending(
    coordinator: BookingCoordinator, status: BookingStatus
) -> None:
    booking = BookingFactory.create(status=status)

    with pytest.raises(InvalidTransition):
        coordinator.confirm(booking.pk)

    booking.refresh_from_db()
    assert booking.status == status


def test_cancel_rejects_terminal_status(coordinator: BookingCoordinator) -> None:
    booking = BookingFactory.create(status=BookingStatus.EXPIRED)

    with pytest.raises(InvalidTransition):
        coordinator.cancel(booking.pk)


def test_expire_stale_only_touches_overdue_pending(coordinator: BookingCoordinator) -> None:
    now = timezone.now()
    overdue = BookingFactory.create(expires_at=now - timedelta(minutes=1))
    fresh = BookingFactory.create(expires_at=now + timedelta(minutes=5))
    confirmed = BookingFactory.create(status=BookingStatus.CONFIRMED)

    assert coordinator.expire_stale(now) == 1

    overdue.refresh_from_db()
    fresh.refresh_from_db()
    confirmed.refresh_from_db()
    assert overdue.status == BookingStatus.EXPIRED
    assert overdue.expires_at is None
    assert fresh.status == BookingStatus.PENDING
    assert confirmed.status == BookingStatus.CONFIRMED


def test_expire_stale_frees_slot_for_new_hold(
    coordinator: BookingCoordinator,
    resource: Resource,
    user: User,
    slot: tuple[datetime, datetime],
) -> None:
    _hold(coordinator, resource, user, slot)
    with pytest.raises(SlotUnavailable):
        _hold(coordinator, resource, UserFactory.create(), slot)

    coordinator.expire_stale(timezone.now() + coordinator.hold_ttl + timedelta(seconds=1))

    assert _hold(coordinator, resource, UserFactory.create(), slot).status == BookingStatus.PENDING


@pytest.mark.parametrize("status", [BookingStatus.EXPIRED, BookingStatus.CANCELLED])
def test_mark_refunded_late_from_released_booking(
    coordinator: BookingCoordinator, status: BookingStatus
) -> None:
    booking = BookingFactory.create(status=status, expires_at=None)

    refunded = coordinator.mark_refunded_late(booking.pk)

    assert refunded.status == BookingStatus.REFUNDED_LATE_WEBHOOK


@pytest.mark.parametrize("status", [BookingStatus.PENDING, BookingStatus.CONFIRMED])
def test_mark_refunded_late_rejects_live_booking(
    coordinator: BookingCoordinator, status: BookingStatus
) -> None:
    booking = BookingFactory.create(status=status)

    with pytest.raises(InvalidTransition):
        coordinator.mark_refunded_late(booking.pk)

    booking.refresh_from_db()
    assert booking.status == status
