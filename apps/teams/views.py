from django.db.models import ProtectedError
from rest_framework import viewsets

from apps.accounts.errors import ConflictError
from apps.accounts.permissions import IsAdminRole
from apps.audit.services import log as audit_log

from .models import Team
from .serializers import TeamSerializer


class TeamViewSet(viewsets.ModelViewSet):
    serializer_class = TeamSerializer
    permission_classes = [IsAdminRole]
    search_fields = ["name", "players__first_name", "players__last_name"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        return Team.objects.prefetch_related("memberships__player").distinct()

    def perform_create(self, serializer):
        t = serializer.save(created_by=self.request.user)
        audit_log(self.request.user, "team.create", "team", t.pk, request=self.request)

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError:
            raise ConflictError("team_in_use", "This team is registered in a tournament or has matches and cannot be deleted.")
        audit_log(self.request.user, "team.delete", "team", instance.pk, request=self.request)
