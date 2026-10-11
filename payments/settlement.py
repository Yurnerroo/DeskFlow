"""Daily settlement: compare money taken in against money paid out, per workspace.

Each workspace operator should be paid what their bookings earned. Payments, refunds
and payouts are written by different flows (checkout, webhooks, refund jobs, Wise
polling), so a bug or a stuck job in any of them shows up here as a mismatch. The job
only reports; it never moves money on its own, because an automatic "fix" built on a
wrong total would make the problem worse.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Sum

from payments.models import (
    Payee,
    Payment,
    PaymentStatus,
    Payout,
    PayoutStatus,
    Refund,
    RefundStatus,
)

logger = logging.getLogger(__name__)

# REFUNDED payments were still captured; the refund itself is subtracted separately.
_CAPTURED = (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED)
# In-flight payouts count as paid: the money is committed and polling will finish them.
# Counting only PAID would report a false mismatch every time a transfer is slow.
_COMMITTED = (
    PayoutStatus.CREATED,
    PayoutStatus.PROCESSING,
    PayoutStatus.UNKNOWN,
    PayoutStatus.PAID,
)

Key = tuple[int, str]


@dataclass(frozen=True)
class Mismatch:
    workspace_id: int
    currency: str
    expected_minor: int
    actual_minor: int

    @property
    def difference_minor(self) -> int:
        return self.expected_minor - self.actual_minor


def _day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
    return start, start + dt.timedelta(days=1)


def settle_day(day: dt.date) -> list[Mismatch]:
    """Return one mismatch per (workspace, currency) whose totals for ``day`` disagree.

    Only workspaces with at least one payee are checked; without a payee there is
    nobody to pay, which is a setup problem rather than a settlement one.
    """
    start, end = _day_bounds(day)
    expected: defaultdict[Key, int] = defaultdict(int)
    actual: defaultdict[Key, int] = defaultdict(int)
    # A subquery, not a join through payees: a workspace with two payees would
    # otherwise have every payment summed twice.
    paid_workspaces = Payee.objects.filter(workspace__isnull=False).values("workspace")

    payments = (
        Payment.objects.filter(
            status__in=_CAPTURED,
            created_at__gte=start,
            created_at__lt=end,
            booking__resource__workspace__in=paid_workspaces,
        )
        .values("booking__resource__workspace", "currency")
        .annotate(total=Sum("amount_minor"))
    )
    for pay_row in payments:
        key = (pay_row["booking__resource__workspace"], pay_row["currency"])
        expected[key] += pay_row["total"]

    # Refunds have no currency of their own; they are always in the payment's currency.
    refunds = (
        Refund.objects.filter(
            status=RefundStatus.SUCCEEDED,
            created_at__gte=start,
            created_at__lt=end,
            payment__booking__resource__workspace__in=paid_workspaces,
        )
        .values("payment__booking__resource__workspace", "payment__currency")
        .annotate(total=Sum("amount_minor"))
    )
    for refund_row in refunds:
        key = (refund_row["payment__booking__resource__workspace"], refund_row["payment__currency"])
        expected[key] -= refund_row["total"]

    payouts = (
        Payout.objects.filter(
            status__in=_COMMITTED,
            created_at__gte=start,
            created_at__lt=end,
            payee__workspace__isnull=False,
        )
        .values("payee__workspace", "currency")
        .annotate(total=Sum("amount_minor"))
    )
    for payout_row in payouts:
        actual[(payout_row["payee__workspace"], payout_row["currency"])] += payout_row["total"]

    mismatches = [
        Mismatch(key[0], key[1], expected[key], actual[key])
        for key in sorted(expected.keys() | actual.keys())
        if expected[key] != actual[key]
    ]
    for m in mismatches:
        logger.warning(
            "settlement mismatch",
            extra={
                "day": day.isoformat(),
                "workspace_id": m.workspace_id,
                "currency": m.currency,
                "expected_minor": m.expected_minor,
                "actual_minor": m.actual_minor,
            },
        )
    return mismatches
