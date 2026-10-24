from django.conf import settings
from rest_framework import serializers

from apps.accounts.models import User
from apps.scoring import engine
from apps.tournaments.models import Tournament

from .models import Court, Match


class CourtSerializer(serializers.ModelSerializer):
    class Meta:
        model = Court
        fields = ["id", "name", "venue", "is_active"]


class PlayerInputSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False)
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=60)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=60)


class SideSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False)
    name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    players = PlayerInputSerializer(many=True, required=False)


class ConfigSerializer(serializers.Serializer):
    bestOf = serializers.ChoiceField(choices=[1, 3], required=False)
    goldenPoint = serializers.BooleanField(required=False)
    tiebreakEnabled = serializers.BooleanField(required=False)
    tiebreakAt = serializers.IntegerField(min_value=1, max_value=12, required=False)
    tiebreakPoints = serializers.IntegerField(min_value=1, max_value=30, required=False)
    superTiebreak = serializers.BooleanField(required=False)
    superTiebreakPoints = serializers.IntegerField(min_value=1, max_value=30, required=False)
    gamesPerSet = serializers.IntegerField(min_value=1, max_value=12, required=False)
    firstServer = serializers.ChoiceField(choices=[0, 1], required=False)

    def validate(self, attrs):
        try:
            engine.normalize_config(attrs)
        except engine.EngineError as e:
            raise serializers.ValidationError(str(e))
        return attrs


class MatchWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=140)
    tournament = serializers.PrimaryKeyRelatedField(queryset=Tournament.objects.all(), required=False, allow_null=True)
    team_a = SideSerializer(required=False, allow_null=True)
    team_b = SideSerializer(required=False, allow_null=True)
    court = serializers.PrimaryKeyRelatedField(queryset=Court.objects.filter(is_active=True), required=False, allow_null=True)
    umpire = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_active=True), required=False, allow_null=True)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)
    is_public = serializers.BooleanField(required=False)
    config = ConfigSerializer(required=False)


def _team(match, idx):
    team = match.team_a if idx == 0 else match.team_b
    players = [{"id": p.player_id, "name": p.player.full_name} for p in match.participants.all() if p.team_index == idx]
    return {"id": team.pk if team else None, "name": team.name if team else "TBD", "players": players}


class MatchReadSerializer(serializers.Serializer):
    """Administrative representation (includes umpire and internal ids).

    The declared fields document the OpenAPI schema; `to_representation` builds the actual payload.
    """

    id = serializers.IntegerField(read_only=True)
    publicId = serializers.CharField(read_only=True)
    reference = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    isPublic = serializers.BooleanField(read_only=True)
    tournament = serializers.JSONField(read_only=True, allow_null=True)
    teams = serializers.JSONField(read_only=True)
    court = serializers.JSONField(read_only=True, allow_null=True)
    umpire = serializers.JSONField(read_only=True, allow_null=True)
    scheduledAt = serializers.DateTimeField(read_only=True, allow_null=True)
    config = serializers.JSONField(read_only=True)
    revision = serializers.IntegerField(read_only=True)
    state = serializers.JSONField(read_only=True)
    display = serializers.JSONField(read_only=True)
    durationSeconds = serializers.IntegerField(read_only=True)
    winnerIndex = serializers.IntegerField(read_only=True, allow_null=True)
    resultVerified = serializers.BooleanField(read_only=True)
    startedAt = serializers.DateTimeField(read_only=True, allow_null=True)
    completedAt = serializers.DateTimeField(read_only=True, allow_null=True)
    shareUrl = serializers.CharField(read_only=True)
    createdAt = serializers.DateTimeField(read_only=True)

    def to_representation(self, m):
        state = m.state.data
        cfg = m.configuration.engine_config()
        return {
            "id": m.pk,
            "publicId": m.public_id,
            "reference": m.reference,
            "name": m.name,
            "status": m.status,
            "isPublic": m.is_public,
            "tournament": {"id": m.tournament_id, "name": m.tournament.name, "slug": m.tournament.slug} if m.tournament_id else None,
            "teams": [_team(m, 0), _team(m, 1)],
            "court": {"id": m.court_id, "name": m.court.name} if m.court_id else None,
            "umpire": {"id": m.umpire_id, "name": m.umpire.display_name} if m.umpire_id else None,
            "scheduledAt": m.scheduled_at,
            "config": cfg,
            "revision": m.state.revision,
            "state": state,
            "display": engine.describe(cfg, state),
            "durationSeconds": m.duration_seconds(),
            "winnerIndex": state.get("winner"),
            "resultVerified": m.result_verified,
            "startedAt": m.started_at,
            "completedAt": m.completed_at,
            "shareUrl": f"{settings.PUBLIC_SITE_URL}/matches/{m.public_id}",
            "createdAt": m.created_at,
        }


class ScoreActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["point", "undo", "pause", "resume", "end"])
    clientActionId = serializers.CharField(max_length=64)
    team = serializers.ChoiceField(choices=[0, 1], required=False)
    expectedRevision = serializers.IntegerField(min_value=0, required=False)
    winner = serializers.ChoiceField(choices=[0, 1], required=False)
    playerId = serializers.IntegerField(required=False)


class SetEntrySerializer(serializers.Serializer):
    games = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=30), min_length=2, max_length=2)
    tiebreak = serializers.ListField(child=serializers.IntegerField(min_value=0, max_value=99), min_length=2, max_length=2,
                                     required=False, allow_null=True)


class ResultSerializer(serializers.Serializer):
    clientActionId = serializers.CharField(max_length=64)
    sets = SetEntrySerializer(many=True)
    reason = serializers.CharField(max_length=300)
    expectedRevision = serializers.IntegerField(min_value=0, required=False)
