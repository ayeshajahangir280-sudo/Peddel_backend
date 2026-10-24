from django.db.models import ProtectedError
from rest_framework import viewsets

from apps.accounts.errors import ConflictError
from apps.accounts.permissions import IsAdminRole
from apps.audit.services import log as audit_log

from .models import Player
from .serializers import PlayerSerializer


class PlayerViewSet(viewsets.ModelViewSet):
    queryset = Player.objects.all()
    serializer_class = PlayerSerializer
    permission_classes = [IsAdminRole]
    search_fields = ["first_name", "last_name", "email"]
    ordering_fields = ["first_name", "last_name", "created_at"]

    def perform_create(self, serializer):
        p = serializer.save(created_by=self.request.user)
        audit_log(self.request.user, "player.create", "player", p.pk, request=self.request)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError:
            raise ConflictError("player_in_use", "This player has played matches and cannot be deleted.")
        audit_log(self.request.user, "player.delete", "player", instance.pk, request=self.request)
