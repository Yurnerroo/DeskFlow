import uuid

from django.db import models

from bookings.models import Booking


class Provider(models.TextChoices):
    STRIPE = "STRIPE", "Stripe"
    PAYPAL = "PAYPAL", "PayPal"
    WISE = "WISE", "Wise"


class PaymentStatus(models.TextChoices):
    CREATED = "CREATED", "Created locally"
    PENDING = "PENDING", "Pending at provider"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    REFUNDED = "REFUNDED", "Refunded"
    # Provider returned a status we don't recognise. Never guessed into success or failure;
    # reconciliation resolves it later.
    UNKNOWN = "UNKNOWN", "Unknown"


class RefundStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class Payment(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=16, choices=Provider)
    # Money is stored as integer minor units (cents) to avoid float rounding.
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=16, choices=PaymentStatus, default=PaymentStatus.CREATED, db_index=True
    )
    # Provider-side id (Stripe PaymentIntent id, PayPal order id). Empty until the provider
    # call succeeds, so uniqueness is only enforced once it is set.
    provider_ref = models.CharField(max_length=255, blank=True, default="")
    # Sent to the provider so retries of the same logical charge never double-charge.
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_ref"],
                condition=~models.Q(provider_ref=""),
                name="payment_unique_provider_ref",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0), name="payment_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.amount_minor} {self.currency} [{self.status}]"


class Refund(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount_minor = models.PositiveBigIntegerField()
    status = models.CharField(
        max_length=16, choices=RefundStatus, default=RefundStatus.PENDING, db_index=True
    )
    reason = models.CharField(max_length=64, blank=True, default="")
    provider_ref = models.CharField(max_length=255, blank=True, default="")
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0), name="refund_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"Refund {self.amount_minor} of payment {self.payment_id} [{self.status}]"
