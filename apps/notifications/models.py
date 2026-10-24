from django.db import models


class OutboxEvent(models.Model):
    """Transactional outbox: written in the same DB transaction as the score change."""

    group = models.CharField(max_length=80, db_index=True)
    revision = models.PositiveIntegerField(default=0)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["id"]
