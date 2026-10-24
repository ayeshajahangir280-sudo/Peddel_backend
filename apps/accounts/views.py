from drf_spectacular.utils import extend_schema
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.audit.services import log as audit_log

from .models import Role, User
from .permissions import IsAdminRole
from .serializers import UserSerializer


class LoginView(TokenObtainPairView):
    permission_classes = [AllowAny]
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            user = User.objects.filter(username=request.data.get("username")).first()
            if user:
                audit_log(user, "auth.login", "user", user.id, request=request)
        return response


class RefreshView(TokenRefreshView):
    permission_classes = [AllowAny]
    throttle_scope = "login"


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=UserSerializer)
    def get(self, request):
        return Response(UserSerializer(request.user, context={"request": request}).data)


class UserViewSet(viewsets.ModelViewSet):
    """User management. Super Admin manages everyone; Tournament Admins may manage umpires only."""

    queryset = User.objects.none()  # real queryset is built per-user in get_queryset
    serializer_class = UserSerializer
    permission_classes = [IsAdminRole]
    search_fields = ["username", "email", "first_name", "last_name"]
    ordering_fields = ["username", "date_joined"]
    ordering = ["username"]

    def get_queryset(self):
        qs = User.objects.all()
        if not self.request.user.is_super_admin:
            qs = qs.filter(role=Role.UMPIRE)
        role = self.request.query_params.get("role")
        if role:
            qs = qs.filter(role=role)
        return qs

    def perform_create(self, serializer):
        user = serializer.save()
        audit_log(self.request.user, "user.create", "user", user.id, request=self.request)

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            from .errors import ApiError

            raise ApiError("cannot_delete_self", "You cannot delete your own account.")
        audit_log(self.request.user, "user.delete", "user", instance.id, request=self.request)
        instance.is_active = False  # soft-delete: preserves history of events and audit logs
        instance.save(update_fields=["is_active"])
