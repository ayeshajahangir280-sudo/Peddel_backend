from django.conf import settings
from django.db import models


class EventType(models.TextChoices):
    POINT = "point", "Point"
    UNDO = "undo", "Undo"
    PAUSE = "pause", "Pause"
    RESUME = "resume", "Resume"
    END = "end", "End match"
    RESULT = "result", "Result entry / correction"


class ScoreEvent(models.Model):
    """Append-only history. Undo never deletes events; it adds a reversal event."""

    match = models.ForeignKey("matches.Match", on_delete=models.CASCADE, related_name="events")
    sequence = models.PositiveIntegerField()  # == match revision after this event
    client_action_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=10, choices=EventType.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    team_index = models.PositiveSmallIntegerField(null=True, blank=True)
    player = models.ForeignKey("players.Player", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    state_before = models.JSONField(default=dict)
    state_after = models.JSONField(default=dict)
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by")
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["match", "sequence"], name="uniq_event_sequence"),
            models.UniqueConstraint(fields=["match", "client_action_id"], name="uniq_event_client_action"),
        ]
        indexes = [models.Index(fields=["match", "event_type", "sequence"])]


class IdempotencyRecord(models.Model):
    """Stores the response for a processed client action so retries return the same result."""

    match = models.ForeignKey("matches.Match", on_delete=models.CASCADE, related_name="+")
    client_action_id = models.CharField(max_length=64)
    response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["match", "client_action_id"], name="uniq_idempotency")]
