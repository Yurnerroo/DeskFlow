from datetime import timedelta
from typing import Any

import pytest
from django.conf import settings
from django.db import OperationalError
from django.utils import timezone

from bookings.models import BookingStatus
from bookings.tasks import expire_stale_holds
from config import celery_app
from payments import tasks as payment_tasks
from payments.tasks import reconcile_pending_payments
from tests.factories import BookingFactory

pytestmark = pytest.mark.django_db


def test_expire_stale_holds_expires_only_overdue_pending() -> None:
    overdue = BookingFactory.create(
        status=BookingStatus.PENDING, expires_at=timezone.now() - timedelta(minutes=1)
    )
    fresh = BookingFactory.create(
        status=BookingStatus.PENDING, expires_at=timezone.now() + timedelta(minutes=10)
    )

    assert expire_stale_holds.apply().get() == 1

    overdue.refresh_from_db()
    fresh.refresh_from_db()
    assert overdue.status == BookingStatus.EXPIRED
    assert fresh.status == BookingStatus.PENDING


def test_expire_stale_holds_is_idempotent() -> None:
    BookingFactory.create(
        status=BookingStatus.PENDING, expires_at=timezone.now() - timedelta(minutes=1)
    )

    assert expire_stale_holds() == 1
    assert expire_stale_holds() == 0


def test_reconcile_task_delegates_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def fake_reconcile() -> int:
        calls.append(True)
        return 7

    monkeypatch.setattr(payment_tasks, "reconcile_pending", fake_reconcile)

    assert reconcile_pending_payments() == 7
    assert calls == [True]


@pytest.mark.parametrize("task", [expire_stale_holds, reconcile_pending_payments])
def test_tasks_retry_on_transient_db_errors(task: Any) -> None:
    assert task.acks_late is True
    assert OperationalError in task.autoretry_for
    assert task.max_retries == 3
    assert task.retry_backoff is True
    assert task.retry_jitter is True


def test_beat_schedule_points_at_registered_tasks() -> None:
    schedule = settings.CELERY_BEAT_SCHEDULE

    for entry in schedule.values():
        assert entry["task"] in celery_app.tasks
        # A missed tick must not pile up behind the next one.
        assert entry["options"]["expires"] < entry["schedule"]

    assert schedule["expire-stale-holds"]["schedule"] == 60.0
    assert schedule["reconcile-pending-payments"]["schedule"] == 300.0
