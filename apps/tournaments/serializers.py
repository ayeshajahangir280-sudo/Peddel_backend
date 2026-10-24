from rest_framework import serializers

from apps.accounts.validators import validate_image
from apps.standings.models import TournamentStanding
from apps.teams.serializers import TeamSerializer

from .models import (
    RANKING_CHOICES, Tournament, TournamentFixture, TournamentGroup, TournamentRegistration,
)


class TournamentSerializer(serializers.ModelSerializer):
    logo = serializers.ImageField(required=False, allow_null=True, validators=[validate_image], write_only=True)
    cover_image = serializers.ImageField(required=False, allow_null=True, validators=[validate_image], write_only=True)
    logo_url = serializers.SerializerMethodField()
    cover_url = serializers.SerializerMethodField()
    teams_count = serializers.SerializerMethodField()
    champion_name = serializers.CharField(source="champion.name", read_only=True, default=None)
    matches_total = serializers.SerializerMethodField()
    matches_completed = serializers.SerializerMethodField()

    class Meta:
        model = Tournament
        fields = [
            "id", "name", "slug", "logo", "cover_image", "logo_url", "cover_url", "description", "venue",
            "start_date", "end_date", "registration_deadline", "organizer", "rules", "format", "status",
            "visibility", "match_format", "ranking_criteria", "points_win", "points_loss", "groups_count",
            "qualifiers_per_group", "champion", "champion_name", "teams_count", "matches_total",
            "matches_completed", "fixtures_confirmed_at", "created_at",
        ]
        read_only_fields = ["id", "status", "champion", "fixtures_confirmed_at", "created_at"]
        extra_kwargs = {"slug": {"required": False}}

    def get_logo_url(self, o):
        return f"/media/{o.logo.name}" if o.logo else None

    def get_cover_url(self, o):
        return f"/media/{o.cover_image.name}" if o.cover_image else None

    def get_teams_count(self, o):
        return getattr(o, "teams_n", None) if hasattr(o, "teams_n") else o.registrations.filter(withdrawn=False).count()

    def get_matches_total(self, o):
        return getattr(o, "matches_n", None) if hasattr(o, "matches_n") else o.matches.count()

    def get_matches_completed(self, o):
        return getattr(o, "matches_done", None) if hasattr(o, "matches_done") else o.matches.filter(status="completed").count()

    def validate_ranking_criteria(self, v):
        if not v or any(c not in RANKING_CHOICES for c in v) or len(set(v)) != len(v):
            raise serializers.ValidationError(f"Use unique values from: {', '.join(sorted(RANKING_CHOICES))}")
        return v

    def validate_match_format(self, v):
        from apps.scoring.engine import EngineError, normalize_config

        try:
            return normalize_config({**v, "firstServer": 0}) | {}
        except EngineError as e:
            raise serializers.ValidationError(str(e))

    def validate_slug(self, v):
        qs = Tournament.objects.filter(slug=v)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This slug is already used.")
        return v

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "End date cannot be before the start date."})
        for k in ("logo", "cover_image"):
            if k in attrs and attrs[k] is None:
                attrs.pop(k)
        return attrs


class RegistrationSerializer(serializers.ModelSerializer):
    team = TeamSerializer(read_only=True)
    team_id = serializers.IntegerField(write_only=True)
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)

    class Meta:
        model = TournamentRegistration
        fields = ["id", "team", "team_id", "group_name", "seed", "withdrawn", "registered_at"]
        read_only_fields = ["id", "registered_at"]


class GroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = TournamentGroup
        fields = ["id", "name", "order"]


class StandingSerializer(serializers.ModelSerializer):
    team_name = serializers.CharField(source="team.name", read_only=True)
    group_name = serializers.CharField(source="group.name", read_only=True)
    set_difference = serializers.IntegerField(read_only=True)
    game_difference = serializers.IntegerField(read_only=True)

    class Meta:
        model = TournamentStanding
        fields = ["group_name", "rank", "team_id", "team_name", "played", "wins", "losses", "points", "sets_for",
                  "sets_against", "games_for", "games_against", "set_difference", "game_difference", "qualified"]


class FixtureSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)
    match = serializers.SerializerMethodField()
    team_a_name = serializers.SerializerMethodField()
    team_b_name = serializers.SerializerMethodField()

    class Meta:
        model = TournamentFixture
        fields = ["id", "stage", "group_name", "round_number", "round_name", "position", "is_bye", "team_a_name",
                  "team_b_name", "match"]

    def get_team_a_name(self, f):
        return f.team_a.name if f.team_a else f.slot_a_source.get("label", "TBD")

    def get_team_b_name(self, f):
        return f.team_b.name if f.team_b else f.slot_b_source.get("label", "TBD")

    def get_match(self, f):
        m = f.match
        if not m:
            return None
        return {"id": m.pk, "status": m.status, "scheduledAt": m.scheduled_at,
                "court": m.court.name if m.court else None,
                "umpire": m.umpire.display_name if m.umpire else None,
                "sets": m.state.data.get("sets", []), "winnerIndex": m.state.data.get("winner")}
