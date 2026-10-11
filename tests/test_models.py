from datetime import timedelta

import pytest
from django.db import IntegrityError

from bookings.models import BookingStatus, Resource
from tests.factories import BookingFactory, ResourceFactory

pytestmark = pytest.mark.django_db


def test_resource_capacity_must_be_positive() -> None:
    with pytest.raises(IntegrityError):
        ResourceFactory.create(capacity=0)


def test_booking_end_must_be_after_start(resource: Resource) -> None:
    booking = BookingFactory.build(resource=resource)
    booking.end_time = booking.start_time - timedelta(minutes=1)
    booking.user.save()
    with pytest.raises(IntegrityError):
        booking.save()


def test_valid_booking_is_persisted(resource: Resource) -> None:
    booking = BookingFactory.create(resource=resource)
    assert booking.pk is not None


def test_new_booking_defaults_to_pending(resource: Resource) -> None:
    booking = BookingFactory.create(resource=resource)
    booking.refresh_from_db()
    assert booking.status == BookingStatus.PENDING
    assert booking.expires_at is None


def test_overlapping_active_bookings_are_rejected(resource: Resource) -> None:
    first = BookingFactory.create(resource=resource)
    with pytest.raises(IntegrityError):
        BookingFactory.create(
            resource=resource,
            start_time=first.start_time + timedelta(minutes=30),
            end_time=first.end_time + timedelta(minutes=30),
        )


def test_expired_booking_does_not_block_slot(resource: Resource) -> None:
    first = BookingFactory.create(resource=resource, status=BookingStatus.EXPIRED)
    second = BookingFactory.create(
        resource=resource, start_time=first.start_time, end_time=first.end_time
    )
    assert second.pk is not None


def test_adjacent_bookings_are_allowed(resource: Resource) -> None:
    first = BookingFactory.create(resource=resource)
    second = BookingFactory.create(
        resource=resource,
        start_time=first.end_time,
        end_time=first.end_time + timedelta(hours=1),
    )
    assert second.pk is not None


def test_same_slot_on_other_resource_is_allowed(resource: Resource) -> None:
    first = BookingFactory.create(resource=resource)
    other = BookingFactory.create(start_time=first.start_time, end_time=first.end_time)
    assert other.resource_id != resource.pk
