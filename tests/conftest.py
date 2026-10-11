import pytest
from django.contrib.auth.models import AbstractUser

from bookings.models import Resource
from tests.factories import ResourceFactory, UserFactory


@pytest.fixture
def user() -> AbstractUser:
    return UserFactory.create()


@pytest.fixture
def resource() -> Resource:
    return ResourceFactory.create()
