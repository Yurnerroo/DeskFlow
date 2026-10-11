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
