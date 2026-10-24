from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.accounts.errors import ApiError, ConflictError
from apps.accounts.permissions import IsAdminRole, can_manage_tournament
from apps.audit.services import log as audit_log
from apps.standings.models import TournamentStanding
from apps.teams.models import Team

from . import services
from .models import Tournament, TournamentFixture, TournamentRegistration, TournamentStatus
from .serializers import (
    FixtureSerializer, RegistrationSerializer, StandingSerializer, TournamentSerializer,
)

STATUS_TRANSITIONS = {
    TournamentStatus.DRAFT: {TournamentStatus.PUBLISHED, TournamentStatus.CANCELLED},
    TournamentStatus.PUBLISHED: {TournamentStatus.DRAFT, TournamentStatus.ARCHIVED, TournamentStatus.CANCELLED},
    TournamentStatus.COMPLETED: {TournamentStatus.ARCHIVED},
    TournamentStatus.ARCHIVED: {TournamentStatus.PUBLISHED},
    TournamentStatus.CANCELLED: {TournamentStatus.DRAFT},
}


class TournamentViewSet(viewsets.ModelViewSet):
    queryset = Tournament.objects.none()  # real queryset is built per-user in get_queryset
    serializer_class = TournamentSerializer
    permission_classes = [IsAdminRole]
    search_fields = ["name", "venue", "organizer"]
    ordering_fields = ["start_date", "name", "created_at"]

    def get_queryset(self):
        u = self.request.user
        qs = Tournament.objects.select_related("champion").annotate(
            teams_n=Count("registrations", filter=Q(registrations__withdrawn=False), distinct=True),
            matches_n=Count("matches", distinct=True),
            matches_done=Count("matches", filter=Q(matches__status="completed"), distinct=True),
        )
        if not u.is_super_admin:
            qs = qs.filter(Q(owner=u) | Q(managers=u)).distinct()
        st = self.request.query_params.get("status")
        if st:
            qs = qs.filter(status__in=st.split(","))
        return qs

    def perform_create(self, serializer):
        t = serializer.save(owner=self.request.user)
        audit_log(self.request.user, "tournament.create", "tournament", t.pk, {"name": t.name}, self.request)

    def perform_update(self, serializer):
        t = serializer.save()
        audit_log(self.request.user, "tournament.update", "tournament", t.pk, request=self.request)

    def perform_destroy(self, instance):
        if instance.fixtures.exists() or instance.status not in (TournamentStatus.DRAFT, TournamentStatus.CANCELLED):
            raise ConflictError("tournament_in_use", "Only draft or cancelled tournaments without fixtures can be deleted. Archive it instead.")
        audit_log(self.request.user, "tournament.delete", "tournament", instance.pk, {"name": instance.name}, self.request)
        instance.delete()

    def _t(self):
        t = self.get_object()
        if not can_manage_tournament(self.request.user, t):
            raise PermissionDenied()
        return t

    # ---- lifecycle
    @action(detail=True, methods=["post"], url_path="set-status")
    def set_status(self, request, pk=None):
        t = self._t()
        target = request.data.get("status")
        if target not in [s.value for s in TournamentStatus]:
            raise ApiError("invalid_status", "Unknown status")
        if target not in STATUS_TRANSITIONS.get(t.status, set()):
            raise ConflictError("invalid_transition", f"Cannot change a {t.status} tournament to {target}")
        if target == TournamentStatus.PUBLISHED and t.registrations.filter(withdrawn=False).count() < 2:
            raise ConflictError("not_ready", "Register at least two teams before publishing")
        if target == TournamentStatus.PUBLISHED and t.champion_id:
            target = TournamentStatus.COMPLETED.value
        t.status = target
        t.save(update_fields=["status", "updated_at"])
        audit_log(request.user, f"tournament.{target}", "tournament", t.pk, request=request)
        return Response(self.get_serializer(self.get_queryset().get(pk=t.pk)).data)

    # ---- registrations
    @action(detail=True, methods=["get", "post"])
    def registrations(self, request, pk=None):
        t = self._t()
        if request.method == "GET":
            regs = t.registrations.select_related("team", "group").prefetch_related("team__memberships__player")
            return Response(RegistrationSerializer(regs, many=True).data)
        if t.status in (TournamentStatus.COMPLETED, TournamentStatus.CANCELLED, TournamentStatus.ARCHIVED):
            raise ConflictError("tournament_closed", f"Cannot register teams in a {t.status} tournament")
        if t.fixtures.exists():
            raise ConflictError("fixtures_exist", "Fixtures are already generated. Withdraw the team or regenerate fixtures first.")
        if t.registration_deadline and timezone.localdate() > t.registration_deadline:
            raise ConflictError("registration_closed", "The registration deadline has passed")
        team_id = request.data.get("team_id")
        try:
            team = Team.objects.prefetch_related("memberships").get(pk=team_id)
        except (Team.DoesNotExist, ValueError, TypeError):
            raise ApiError("team_not_found", "Team does not exist")
        if t.registrations.filter(team=team).exists():
            raise ConflictError("duplicate_registration", "This team is already registered")
        pids = {m.player_id for m in team.memberships.all()}
        clash = TournamentRegistration.objects.filter(tournament=t, withdrawn=False, team__memberships__player_id__in=pids).exists()
        if clash:
            raise ConflictError("player_already_registered", "One of this team's players is already registered with another team")
        seed = (t.registrations.order_by("-seed").values_list("seed", flat=True).first() or 0) + 1
        reg = TournamentRegistration.objects.create(tournament=t, team=team, seed=int(request.data.get("seed") or seed))
        audit_log(request.user, "tournament.register", "tournament", t.pk, {"team": team.name}, request)
        return Response(RegistrationSerializer(reg).data, status=201)

    @action(detail=True, methods=["delete"], url_path=r"registrations/(?P<reg_id>\d+)")
    def unregister(self, request, pk=None, reg_id=None):
        t = self._t()
        if t.fixtures.exists():
            raise ConflictError("fixtures_exist", "Fixtures are generated; regenerate them after removing a team.")
        deleted, _ = t.registrations.filter(pk=reg_id).delete()
        if not deleted:
            raise ApiError("not_found", "Registration not found", 404)
        audit_log(request.user, "tournament.unregister", "tournament", t.pk, {"registration": reg_id}, request)
        return Response(status=204)

    # ---- fixtures
    @action(detail=True, methods=["get"])
    def fixtures(self, request, pk=None):
        t = self._t()
        qs = (TournamentFixture.objects.filter(tournament=t)
              .select_related("group", "team_a", "team_b", "match__state", "match__court", "match__umpire")
              .order_by("stage", "round_number", "position"))
        return Response(FixtureSerializer(qs, many=True).data)

    @action(detail=True, methods=["post"], url_path="fixtures/preview")
    def fixtures_preview(self, request, pk=None):
        t = self._t()
        return Response(services.preview(t, request.data or {}))

    @action(detail=True, methods=["post"], url_path="fixtures/confirm")
    def fixtures_confirm(self, request, pk=None):
        t = self._t()
        with transaction.atomic():
            plan = services.confirm_fixtures(t, request.data or {}, request.user)
        audit_log(request.user, "tournament.fixtures", "tournament", t.pk,
                  {"matches": len(plan["group_fixtures"]) + len(plan["knockout"])}, request)
        return Response(services.preview(t, {**(request.data or {}), "seed": plan["seed"]}) | {"confirmed": True}, status=201)

    # ---- standings / bracket
    @action(detail=True, methods=["get"])
    def standings(self, request, pk=None):
        t = self._t()
        rows = TournamentStanding.objects.filter(tournament=t).select_related("team", "group").order_by("group__order", "rank")
        return Response(StandingSerializer(rows, many=True).data)

    @action(detail=True, methods=["post"])
    def recalculate(self, request, pk=None):
        t = self._t()
        services.reconcile(t)
        return Response({"ok": True})
