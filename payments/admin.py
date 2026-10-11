from django.contrib import admin

from payments.models import Payment, ProcessedWebhookEvent, Refund


class RefundInline(admin.TabularInline):
    model = Refund
    extra = 0
    readonly_fields = ("idempotency_key", "provider_ref", "created_at")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "provider", "amount_minor", "currency", "status", "created_at")
    list_filter = ("provider", "status", "currency")
    search_fields = ("provider_ref", "idempotency_key")
    readonly_fields = ("idempotency_key", "provider_ref", "created_at", "updated_at")
    inlines = (RefundInline,)


@admin.register(ProcessedWebhookEvent)
class ProcessedWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "provider", "event_type", "received_at", "processed_at")
    list_filter = ("provider", "event_type")
    search_fields = ("event_id",)
    readonly_fields = (
        "provider",
        "event_id",
        "event_type",
        "payload",
        "received_at",
        "processed_at",
    )
