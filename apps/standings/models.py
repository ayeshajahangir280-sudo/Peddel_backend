from django.db import models


class TournamentStanding(models.Model):
    """Recomputable projection of confirmed group-stage results."""

    tournament = models.ForeignKey("tournaments.Tournament", on_delete=models.CASCADE, related_name="standings")
    group = models.ForeignKey("tournaments.TournamentGroup", on_delete=models.CASCADE, related_name="standings")
    team = models.ForeignKey("teams.Team", on_delete=models.CASCADE, related_name="+")
    rank = models.PositiveSmallIntegerField(default=0)
    played = models.PositiveSmallIntegerField(default=0)
    wins = models.PositiveSmallIntegerField(default=0)
    losses = models.PositiveSmallIntegerField(default=0)
    points = models.IntegerField(default=0)
    sets_for = models.PositiveSmallIntegerField(default=0)
    sets_against = models.PositiveSmallIntegerField(default=0)
    games_for = models.PositiveSmallIntegerField(default=0)
    games_against = models.PositiveSmallIntegerField(default=0)
    qualified = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["group__order", "rank"]
        constraints = [models.UniqueConstraint(fields=["tournament", "group", "team"], name="uniq_standing")]
        indexes = [models.Index(fields=["tournament", "group", "rank"])]

    @property
    def set_difference(self):
        return self.sets_for - self.sets_against

    @property
    def game_difference(self):
        return self.games_for - self.games_against
