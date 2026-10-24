from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers, viewsets

from apps.accounts.permissions import IsSuperAdmin

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ["id", "actor_name", "action", "object_type", "object_id", "details", "created_at"]

    @extend_schema_field(str)
    def get_actor_name(self, obj):
        return obj.actor.display_name if obj.actor else "system"


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.select_related("actor")
    serializer_class = AuditLogSerializer
    permission_classes = [IsSuperAdmin]
    search_fields = ["action", "object_type", "object_id"]
