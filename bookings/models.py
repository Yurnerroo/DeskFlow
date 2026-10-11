from django.conf import settings
from django.db import models


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


class Booking(models.Model):
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="booking_end_after_start",
            ),
        ]
