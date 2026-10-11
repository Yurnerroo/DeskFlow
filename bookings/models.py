from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models


class TsTzRange(models.Func):
    """Build a half-open [start, end) timestamptz range so back-to-back bookings don't overlap."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class KindChoices(models.TextChoices):
    DESK = "Desk", "Desk"
    ROOM = "Room", "Room"


class Workspace(models.Model):
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=100)


class Resource(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    kind = models.CharField(choices=KindChoices)
    capacity = models.IntegerField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(capacity__gte=1), name="capacity_gte_1"),
        ]


class BookingStatus(models.TextChoices):
    PENDING = "PENDING", "Pending payment"
    CONFIRMED = "CONFIRMED", "Confirmed"
    EXPIRED = "EXPIRED", "Expired"
    PAYMENT_FAILED = "PAYMENT_FAILED", "Payment failed"
    REFUNDED_LATE_WEBHOOK = "REFUNDED_LATE_WEBHOOK", "Refunded (late webhook)"
    CANCELLED = "CANCELLED", "Cancelled"


# Statuses that occupy the slot. Everything else is terminal and frees it.
ACTIVE_STATUSES = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


class Booking(models.Model):
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    status = models.CharField(
        max_length=32, choices=BookingStatus, default=BookingStatus.PENDING, db_index=True
    )
    # Hold deadline for PENDING bookings; null once the booking leaves PENDING.
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="booking_end_after_start",
            ),
            # The database is the final arbiter of double booking: no two active
            # bookings on the same resource may have overlapping time ranges.
            ExclusionConstraint(
                name="booking_no_overlap_active",
                expressions=[
                    ("resource", RangeOperators.EQUAL),
                    (TsTzRange("start_time", "end_time"), RangeOperators.OVERLAPS),
                ],
                condition=models.Q(status__in=ACTIVE_STATUSES),
            ),
        ]
