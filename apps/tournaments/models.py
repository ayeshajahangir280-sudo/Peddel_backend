import uuid

from django.conf import settings
from django.db import models
from django.utils.text import slugify


def tournament_image_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    return f"tournaments/{uuid.uuid4().hex}.{ext}"


class TournamentFormat(models.TextChoices):
    ROUND_ROBIN = "round_robin", "Round robin"
    GROUPS_KNOCKOUT = "groups_knockout", "Groups + knockout"
    SINGLE_ELIMINATION = "single_elimination", "Single elimination"


class TournamentStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    COMPLETED = "completed", "Completed"
    ARCHIVED = "archived", "Archived"
    CANCELLED = "cancelled", "Cancelled"


class Visibility(models.TextChoices):
    PUBLIC = "public", "Public"
    PRIVATE = "private", "Private"


DEFAULT_RANKING = ["points", "wins", "head_to_head", "set_difference", "game_difference"]
RANKING_CHOICES = {"points", "wins", "head_to_head", "set_difference", "game_difference", "games_won", "sets_won"}
DEFAULT_MATCH_FORMAT = {
    "bestOf": 3, "goldenPoint": False, "tiebreakEnabled": True, "tiebreakAt": 6,
    "tiebreakPoints": 7, "superTiebreak": True, "superTiebreakPoints": 10, "gamesPerSet": 6,
}


def default_ranking():
    return list(DEFAULT_RANKING)


def default_match_format():
    return dict(DEFAULT_MATCH_FORMAT)


class Tournament(models.Model):
    name = models.CharField(max_length=140)
    slug = models.SlugField(max_length=160, unique=True, allow_unicode=False)
    logo = models.ImageField(upload_to=tournament_image_path, blank=True)
    cover_image = models.ImageField(upload_to=tournament_image_path, blank=True)
    description = models.TextField(blank=True)
    venue = models.CharField(max_length=160, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    registration_deadline = models.DateField(null=True, blank=True)
    organizer = models.CharField(max_length=140, blank=True)
    rules = models.TextField(blank=True)
    format = models.CharField(max_length=24, choices=TournamentFormat.choices, default=TournamentFormat.GROUPS_KNOCKOUT)
    status = models.CharField(max_length=12, choices=TournamentStatus.choices, default=TournamentStatus.DRAFT, db_index=True)
    visibility = models.CharField(max_length=8, choices=Visibility.choices, default=Visibility.PUBLIC)

    match_format = models.JSONField(default=default_match_format)
    ranking_criteria = models.JSONField(default=default_ranking)
    points_win = models.PositiveSmallIntegerField(default=3)
    points_loss = models.PositiveSmallIntegerField(default=0)
    groups_count = models.PositiveSmallIntegerField(default=2)
    qualifiers_per_group = models.PositiveSmallIntegerField(default=2)

    champion = models.ForeignKey("teams.Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_tournaments")
    managers = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="managed_tournaments")
    fixtures_confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-id"]
        indexes = [models.Index(fields=["visibility", "status"])]

    def __str__(self):
        return self.name

    @property
    def is_public(self):
        return self.visibility == Visibility.PUBLIC and self.status in (TournamentStatus.PUBLISHED, TournamentStatus.COMPLETED)

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "tournament"
            slug, n = base, 2
            while Tournament.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, n = f"{base}-{n}", n + 1
            self.slug = slug
        super().save(*args, **kwargs)


class TournamentGroup(models.Model):
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="groups")
    name = models.CharField(max_length=40)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]
        constraints = [models.UniqueConstraint(fields=["tournament", "name"], name="uniq_group_name")]

    def __str__(self):
        return f"{self.tournament.name} / {self.name}"


class TournamentRegistration(models.Model):
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="registrations")
    team = models.ForeignKey("teams.Team", on_delete=models.PROTECT, related_name="registrations")
    group = models.ForeignKey(TournamentGroup, null=True, blank=True, on_delete=models.SET_NULL, related_name="registrations")
    seed = models.PositiveSmallIntegerField(default=0)
    withdrawn = models.BooleanField(default=False)
    registered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["seed", "id"]
        constraints = [models.UniqueConstraint(fields=["tournament", "team"], name="uniq_registration")]


class TournamentFixture(models.Model):
    class Stage(models.TextChoices):
        GROUP = "group", "Group stage"
        KNOCKOUT = "knockout", "Knockout"

    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="fixtures")
    match = models.OneToOneField("matches.Match", null=True, blank=True, on_delete=models.SET_NULL, related_name="fixture")
    stage = models.CharField(max_length=10, choices=Stage.choices)
    group = models.ForeignKey(TournamentGroup, null=True, blank=True, on_delete=models.CASCADE, related_name="fixtures")
    round_number = models.PositiveSmallIntegerField(default=1)
    round_name = models.CharField(max_length=40, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    team_a = models.ForeignKey("teams.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    team_b = models.ForeignKey("teams.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # Where each slot comes from: {"type": "team"|"group_rank"|"winner"|"bye", ...}
    slot_a_source = models.JSONField(default=dict, blank=True)
    slot_b_source = models.JSONField(default=dict, blank=True)
    is_bye = models.BooleanField(default=False)
    next_fixture = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="feeders")
    next_slot = models.PositiveSmallIntegerField(null=True, blank=True)  # 0 -> team_a, 1 -> team_b

    class Meta:
        ordering = ["stage", "round_number", "position", "id"]
        indexes = [models.Index(fields=["tournament", "stage", "round_number"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(next_slot__isnull=True) | models.Q(next_slot__in=[0, 1]), name="fixture_next_slot"
            )
        ]
