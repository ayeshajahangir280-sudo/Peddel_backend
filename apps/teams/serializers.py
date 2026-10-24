from django.db import transaction
from rest_framework import serializers

from apps.players.models import Player
from apps.players.serializers import PlayerBriefSerializer

from .models import Team, TeamPlayer


class TeamSerializer(serializers.ModelSerializer):
    players = serializers.SerializerMethodField()
    player_ids = serializers.ListField(child=serializers.IntegerField(), write_only=True, min_length=2, max_length=2)

    class Meta:
        model = Team
        fields = ["id", "name", "players", "player_ids", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_players(self, obj):
        return PlayerBriefSerializer([m.player for m in obj.memberships.all()], many=True).data

    def validate_player_ids(self, ids):
        if len(set(ids)) != 2:
            raise serializers.ValidationError("A team needs two different players.")
        found = Player.objects.filter(pk__in=ids).count()
        if found != 2:
            raise serializers.ValidationError("One or more players do not exist.")
        return ids

    @transaction.atomic
    def create(self, validated):
        ids = validated.pop("player_ids")
        team = Team.objects.create(**validated)
        for pos, pid in enumerate(ids, start=1):
            TeamPlayer.objects.create(team=team, player_id=pid, position=pos)
        return team

    @transaction.atomic
    def update(self, instance, validated):
        ids = validated.pop("player_ids", None)
        for k, v in validated.items():
            setattr(instance, k, v)
        instance.save()
        if ids is not None:
            instance.memberships.all().delete()
            for pos, pid in enumerate(ids, start=1):
                TeamPlayer.objects.create(team=instance, player_id=pid, position=pos)
        return instance
