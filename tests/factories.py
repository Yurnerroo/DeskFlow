from datetime import timedelta

import factory
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.utils import timezone

from bookings.models import Booking, KindChoices, Resource, Workspace


class UserFactory(factory.django.DjangoModelFactory[User]):
    class Meta:
        model = get_user_model()

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")


class WorkspaceFactory(factory.django.DjangoModelFactory[Workspace]):
    class Meta:
        model = Workspace

    name = factory.Sequence(lambda n: f"Workspace {n}")
    address = "1 Main St"


class ResourceFactory(factory.django.DjangoModelFactory[Resource]):
    class Meta:
        model = Resource

    workspace = factory.SubFactory(WorkspaceFactory)
    name = factory.Sequence(lambda n: f"Desk {n}")
    kind = KindChoices.DESK
    capacity = 1


class BookingFactory(factory.django.DjangoModelFactory[Booking]):
    class Meta:
        model = Booking

    resource = factory.SubFactory(ResourceFactory)
    user = factory.SubFactory(UserFactory)
    start_time = factory.LazyFunction(lambda: timezone.now() + timedelta(days=1))
    end_time = factory.LazyAttribute(lambda o: o.start_time + timedelta(hours=1))
