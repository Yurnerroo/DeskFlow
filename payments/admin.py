from django.contrib import admin

from payments.models import Payment, Refund


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
