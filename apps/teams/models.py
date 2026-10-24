from django.conf import settings
from django.db import models


class Team(models.Model):
    name = models.CharField(max_length=100)
    players = models.ManyToManyField("players.Player", through="TeamPlayer", related_name="teams")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    def ordered_players(self):
        return [tp.player for tp in self.memberships.all()]  # prefetch 'memberships__player' for efficiency


class TeamPlayer(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    player = models.ForeignKey("players.Player", on_delete=models.CASCADE, related_name="memberships")
    position = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["team", "player"], name="uniq_team_player"),
            models.UniqueConstraint(fields=["team", "position"], name="uniq_team_position"),
            models.CheckConstraint(condition=models.Q(position__in=[1, 2]), name="team_position_1_or_2"),
        ]
