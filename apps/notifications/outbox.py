import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from .models import OutboxEvent

log = logging.getLogger(__name__)


def match_group(public_id):
    return f"match_{public_id}"


def enqueue(public_id, revision, payload):
    """Write an outbox row inside the current transaction and publish once it commits."""
    event = OutboxEvent.objects.create(group=match_group(public_id), revision=revision, payload=payload)
    transaction.on_commit(lambda: publish(event.pk))
    return event


def publish(event_id):
    """Best-effort delivery. On failure the row stays pending for `dispatch_outbox`."""
    try:
        event = OutboxEvent.objects.get(pk=event_id, dispatched_at__isnull=True)
    except OutboxEvent.DoesNotExist:
        return False
    try:
        layer = get_channel_layer()
        async_to_sync(layer.group_send)(event.group, {"type": "match.update", "payload": event.payload})
    except Exception as exc:  # Redis down etc. REST keeps working; clients resync on reconnect.
        OutboxEvent.objects.filter(pk=event_id).update(attempts=event.attempts + 1)
        log.warning("outbox publish failed id=%s: %s", event_id, exc)
        return False
    OutboxEvent.objects.filter(pk=event_id).update(dispatched_at=timezone.now())
    return True


def dispatch_pending(limit=200):
    sent = 0
    for event_id in OutboxEvent.objects.filter(dispatched_at__isnull=True).values_list("pk", flat=True)[:limit]:
        sent += 1 if publish(event_id) else 0
    return sent
