from __future__ import annotations

import datetime as dt
import logging
from typing import cast

import pytest
from django.db.models import Model

from bookings.models import Workspace
from payments.models import (
    Payee,
    Payment,
    PaymentStatus,
    Payout,
    PayoutStatus,
    Refund,
    RefundStatus,
)
from payments.settlement import Mismatch, settle_day
from tests.factories import (
    BookingFactory,
    PayeeFactory,
    PaymentFactory,
    PayoutFactory,
    ResourceFactory,
    WorkspaceFactory,
)

pytestmark = pytest.mark.django_db

DAY = dt.date(2025, 3, 10)
NOON = dt.datetime.combine(DAY, dt.time(12), tzinfo=dt.UTC)


def _ws() -> Workspace:
    return cast(Workspace, WorkspaceFactory())


def _at(obj: Model, when: dt.datetime = NOON) -> None:
    # created_at is auto_now_add, so backdate with a queryset update.
    type(obj)._default_manager.filter(pk=obj.pk).update(created_at=when)


def _payment(
    workspace: Workspace,
    amount: int = 2500,
    *,
    currency: str = "EUR",
    status: str = PaymentStatus.SUCCEEDED,
    when: dt.datetime = NOON,
) -> Payment:
    # A fresh resource per payment keeps bookings from overlapping on one desk.
    booking = BookingFactory(resource=ResourceFactory(workspace=workspace))
    payment = cast(
        Payment,
        PaymentFactory(booking=booking, amount_minor=amount, currency=currency, status=status),
    )
    _at(payment, when)
    return payment


def _payout(
    workspace: Workspace,
    amount: int = 2500,
    *,
    currency: str = "EUR",
    status: str = PayoutStatus.PAID,
    when: dt.datetime = NOON,
) -> Payout:
    payee = Payee.objects.filter(workspace=workspace).first() or PayeeFactory(workspace=workspace)
    payout = cast(
        Payout,
        PayoutFactory(payee=payee, amount_minor=amount, currency=currency, status=status),
    )
    _at(payout, when)
    return payout


def test_matching_totals_report_nothing() -> None:
    ws = _ws()
    _payment(ws, 2500)
    _payment(ws, 1500)
    _payout(ws, 4000)

    assert settle_day(DAY) == []


def test_missing_payout_is_reported() -> None:
    ws = _ws()
    PayeeFactory(workspace=ws)
    _payment(ws, 2500)

    assert settle_day(DAY) == [Mismatch(ws.pk, "EUR", 2500, 0)]


def test_overpayment_is_reported_with_negative_difference() -> None:
    ws = _ws()
    _payment(ws, 2500)
    _payout(ws, 3000)

    [mismatch] = settle_day(DAY)
    assert mismatch.difference_minor == -500


def test_successful_refunds_are_subtracted() -> None:
    ws = _ws()
    payment = _payment(ws, 2500, status=PaymentStatus.REFUNDED)
    refund = Refund.objects.create(
        payment=payment, amount_minor=1000, status=RefundStatus.SUCCEEDED
    )
    _at(refund)
    pending = Refund.objects.create(payment=payment, amount_minor=500)
    _at(pending)
    _payout(ws, 1500)

    assert settle_day(DAY) == []


def test_currencies_are_settled_separately() -> None:
    ws = _ws()
    _payment(ws, 2500, currency="EUR")
    _payment(ws, 3000, currency="USD")
    _payout(ws, 2500, currency="EUR")

    assert settle_day(DAY) == [Mismatch(ws.pk, "USD", 3000, 0)]


def test_workspace_without_payee_is_skipped() -> None:
    _payment(_ws(), 2500)
    PayoutFactory(payee=PayeeFactory())  # payee with no workspace

    assert settle_day(DAY) == []


def test_failed_payouts_do_not_count() -> None:
    ws = _ws()
    _payment(ws, 2500)
    _payout(ws, 2500, status=PayoutStatus.FAILED)

    assert settle_day(DAY) == [Mismatch(ws.pk, "EUR", 2500, 0)]


@pytest.mark.parametrize(
    "status", [PayoutStatus.CREATED, PayoutStatus.PROCESSING, PayoutStatus.UNKNOWN]
)
def test_in_flight_payouts_count_as_paid(status: str) -> None:
    ws = _ws()
    _payment(ws, 2500)
    _payout(ws, 2500, status=status)

    assert settle_day(DAY) == []


def test_uncaptured_payments_are_ignored() -> None:
    ws = _ws()
    PayeeFactory(workspace=ws)
    _payment(ws, 2500, status=PaymentStatus.PENDING)
    _payment(ws, 2500, status=PaymentStatus.FAILED)

    assert settle_day(DAY) == []


def test_two_payees_do_not_double_count_payments() -> None:
    ws = _ws()
    first = PayeeFactory(workspace=ws)
    PayeeFactory(workspace=ws)
    _payment(ws, 2500)
    _at(cast(Payout, PayoutFactory(payee=first, amount_minor=2500)))

    assert settle_day(DAY) == []


def test_records_outside_the_day_are_excluded() -> None:
    ws = _ws()
    PayeeFactory(workspace=ws)
    start = dt.datetime.combine(DAY, dt.time.min, tzinfo=dt.UTC)
    _payment(ws, 2500, when=start - dt.timedelta(microseconds=1))
    _payment(ws, 2500, when=start + dt.timedelta(days=1))
    _payout(ws, 9999, when=start + dt.timedelta(days=1))

    assert settle_day(DAY) == []


def test_window_includes_midnight_start() -> None:
    ws = _ws()
    PayeeFactory(workspace=ws)
    _payment(ws, 2500, when=dt.datetime.combine(DAY, dt.time.min, tzinfo=dt.UTC))

    assert settle_day(DAY) == [Mismatch(ws.pk, "EUR", 2500, 0)]


def test_mismatches_are_sorted_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    second, first = _ws(), _ws()
    for ws in (second, first):
        PayeeFactory(workspace=ws)
        _payment(ws, 2500)

    with caplog.at_level(logging.WARNING, logger="payments.settlement"):
        mismatches = settle_day(DAY)

    assert [m.workspace_id for m in mismatches] == sorted([first.pk, second.pk])
    records = [r for r in caplog.records if r.getMessage() == "settlement mismatch"]
    assert len(records) == 2
    assert records[0].__dict__["day"] == DAY.isoformat()
    assert records[0].__dict__["expected_minor"] == 2500
