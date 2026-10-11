import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from payments.models import ProcessedWebhookEvent
from payments.providers import ProviderError, WebhookVerificationError, get_provider
from payments.services import apply_webhook_event

logger = logging.getLogger(__name__)


class ProviderWebhookView(APIView):
    """Inbound webhook endpoint shared by all payment providers.

    Authenticity comes from the provider signature, not a session, so DRF auth is off.
    Deduplication relies on the (provider, event_id) unique constraint of the inbox table:
    the insert and the domain update commit together, so a redelivered event is either a
    no-op duplicate or a retry of a previously rolled-back attempt.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request: Request, provider: str) -> Response:
        try:
            adapter = get_provider(provider)
        except (LookupError, ValueError):
            return Response({"detail": "Unknown provider."}, status=status.HTTP_404_NOT_FOUND)

        try:
            event = adapter.parse_webhook(request.body, request.headers)
        except WebhookVerificationError:
            logger.warning("Rejected %s webhook with invalid signature", adapter.provider)
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                try:
                    with transaction.atomic():
                        inbox = ProcessedWebhookEvent.objects.create(
                            provider=adapter.provider,
                            event_id=event.event_id,
                            event_type=event.event_type,
                            payload=event.payload,
                        )
                except IntegrityError:
                    return Response({"status": "duplicate"}, status=status.HTTP_200_OK)

                apply_webhook_event(event, adapter)
                inbox.processed_at = timezone.now()
                inbox.save(update_fields=["processed_at"])
        except ProviderError as exc:
            # Rolled back, including the inbox row: a non-2xx makes the provider redeliver.
            logger.warning("Webhook %s failed, awaiting redelivery: %s", event.event_id, exc)
            return Response(
                {"detail": "Temporarily unable to process event."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({"status": "processed"}, status=status.HTTP_200_OK)
