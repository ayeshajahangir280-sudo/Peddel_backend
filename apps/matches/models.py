import secrets
import string

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.accounts.errors import ConflictError


class MatchStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SCHEDULED = "scheduled", "Scheduled"
    READY = "ready", "Ready"
    LIVE = "live", "Live"
    PAUSED = "paused", "Paused"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


ALLOWED_TRANSITIONS = {
    MatchStatus.DRAFT: {MatchStatus.SCHEDULED, MatchStatus.CANCELLED},
    MatchStatus.SCHEDULED: {MatchStatus.DRAFT, MatchStatus.READY, MatchStatus.LIVE, MatchStatus.CANCELLED},
    MatchStatus.READY: {MatchStatus.SCHEDULED, MatchStatus.LIVE, MatchStatus.CANCELLED},
    MatchStatus.LIVE: {MatchStatus.PAUSED, MatchStatus.COMPLETED, MatchStatus.CANCELLED},
    MatchStatus.PAUSED: {MatchStatus.LIVE, MatchStatus.COMPLETED, MatchStatus.CANCELLED},
    MatchStatus.COMPLETED: set(),
    MatchStatus.CANCELLED: set(),
}
PRE_START = {MatchStatus.DRAFT, MatchStatus.SCHEDULED, MatchStatus.READY}


def new_public_id():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


class Court(models.Model):
    name = models.CharField(max_length=60, unique=True)
    venue = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Match(models.Model):
    public_id = models.CharField(max_length=16, unique=True, default=new_public_id, editable=False)
    name = models.CharField(max_length=140)
    tournament = models.ForeignKey("tournaments.Tournament", null=True, blank=True, on_delete=models.CASCADE, related_name="matches")
    team_a = models.ForeignKey("teams.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    team_b = models.ForeignKey("teams.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    winner = models.ForeignKey("teams.Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    court = models.ForeignKey(Court, null=True, blank=True, on_delete=models.SET_NULL, related_name="matches")
    umpire = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="umpired_matches")
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=12, choices=MatchStatus.choices, default=MatchStatus.DRAFT, db_index=True)
    # Friendly (non-tournament) matches are shareable by link unless this is switched off.
    is_public = models.BooleanField(default=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    paused_seconds = models.PositiveIntegerField(default=0)

    result_verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-scheduled_at", "-id"]
        verbose_name_plural = "matches"
        indexes = [
            models.Index(fields=["tournament", "status"]),
            models.Index(fields=["status", "scheduled_at"]),
        ]

    def __str__(self):
        return self.name

    @property
    def reference(self):
        return f"MATCH{self.pk}"

    def transition(self, new_status):
        new_status = MatchStatus(new_status)
        if new_status == self.status:
            return
        if new_status not in ALLOWED_TRANSITIONS[MatchStatus(self.status)]:
            raise ConflictError("invalid_transition", f"Cannot change status from {self.status} to {new_status.value}")
        self.status = new_status.value

    def duration_seconds(self, now=None):
        if not self.started_at:
            return 0
        end = self.completed_at or now or timezone.now()
        paused = self.paused_seconds
        if self.paused_at and not self.completed_at:
            paused += int((end - self.paused_at).total_seconds())
        return max(0, int((end - self.started_at).total_seconds()) - paused)


class MatchConfiguration(models.Model):
    match = models.OneToOneField(Match, on_delete=models.CASCADE, related_name="configuration")
    best_of = models.PositiveSmallIntegerField(default=3)
    golden_point = models.BooleanField(default=False)
    tiebreak_enabled = models.BooleanField(default=True)
    tiebreak_at = models.PositiveSmallIntegerField(default=6)
    tiebreak_points = models.PositiveSmallIntegerField(default=7)
    super_tiebreak = models.BooleanField(default=False)
    super_tiebreak_points = models.PositiveSmallIntegerField(default=10)
    games_per_set = models.PositiveSmallIntegerField(default=6)
    first_server = models.PositiveSmallIntegerField(default=0)

    def engine_config(self):
        from apps.scoring.engine import normalize_config

        return normalize_config(
            {
                "bestOf": self.best_of,
                "goldenPoint": self.golden_point,
                "tiebreakEnabled": self.tiebreak_enabled,
                "tiebreakAt": self.tiebreak_at,
                "tiebreakPoints": self.tiebreak_points,
                "superTiebreak": self.super_tiebreak,
                "superTiebreakPoints": self.super_tiebreak_points,
                "gamesPerSet": self.games_per_set,
                "firstServer": self.first_server,
            }
        )


class MatchState(models.Model):
    """Authoritative live score. `revision` increases by one on every committed action."""

    match = models.OneToOneField(Match, on_delete=models.CASCADE, related_name="state")
    revision = models.PositiveIntegerField(default=0)
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class MatchParticipant(models.Model):
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="participants")
    team_index = models.PositiveSmallIntegerField()  # 0 = team A, 1 = team B
    position = models.PositiveSmallIntegerField()  # 1 or 2
    player = models.ForeignKey("players.Player", on_delete=models.PROTECT, related_name="match_participations")

    class Meta:
        ordering = ["team_index", "position"]
        constraints = [
            models.UniqueConstraint(fields=["match", "team_index", "position"], name="uniq_match_slot"),
            models.UniqueConstraint(fields=["match", "player"], name="uniq_match_player"),
            models.CheckConstraint(condition=models.Q(team_index__in=[0, 1]), name="participant_team_index"),
        ]
