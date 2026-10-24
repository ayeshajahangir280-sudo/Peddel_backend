import io

import qrcode
from django.conf import settings
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.errors import ApiError, ConflictError
from apps.accounts.permissions import IsAdminRole, can_manage_match, can_score_match
from apps.audit.models import AuditLog
from apps.audit.services import log as audit_log
from apps.players.models import Player
from apps.scoring import services as scoring
from apps.teams.models import Team
from apps.tournaments.models import Tournament

from .models import PRE_START, Court, Match, MatchStatus
from .serializers import (
    CourtSerializer, MatchReadSerializer, MatchWriteSerializer, ResultSerializer, ScoreActionSerializer,
)
from .services import create_match, update_match


class CourtViewSet(viewsets.ModelViewSet):
    queryset = Court.objects.all()
    serializer_class = CourtSerializer
    permission_classes = [IsAdminRole]
    search_fields = ["name", "venue"]

    def perform_destroy(self, instance):
        if instance.matches.exists():
            instance.is_active = False  # keep history; hide from new assignments
            instance.save(update_fields=["is_active"])
        else:
            instance.delete()


def visible_matches(user):
    qs = Match.objects.select_related(
        "tournament", "court", "umpire", "team_a", "team_b", "winner", "state", "configuration"
    ).prefetch_related("participants__player")
    if user.is_super_admin:
        return qs
    if user.is_tournament_admin:
        return qs.filter(Q(created_by=user) | Q(tournament__owner=user) | Q(tournament__managers=user)).distinct()
    return qs.filter(umpire=user)


class MatchViewSet(viewsets.ModelViewSet):
    queryset = Match.objects.none()  # real queryset is built per-user in get_queryset
    permission_classes = [IsAuthenticated]
    search_fields = ["name", "team_a__name", "team_b__name", "public_id"]
    ordering_fields = ["scheduled_at", "created_at", "status"]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = visible_matches(self.request.user)
        p = self.request.query_params
        if p.get("status"):
            qs = qs.filter(status__in=p["status"].split(","))
        if p.get("tournament"):
            qs = qs.filter(tournament_id=p["tournament"])
        if p.get("friendly") in ("1", "true"):
            qs = qs.filter(tournament__isnull=True)
        if p.get("court"):
            qs = qs.filter(court_id=p["court"])
        if p.get("umpire"):
            qs = qs.filter(umpire_id=p["umpire"])
        if p.get("from"):
            qs = qs.filter(scheduled_at__gte=p["from"])
        if p.get("to"):
            qs = qs.filter(scheduled_at__lte=p["to"])
        return qs

    def get_serializer_class(self):
        return MatchWriteSerializer if self.action in ("create", "partial_update") else MatchReadSerializer

    def _require_admin(self):
        if not self.request.user.is_admin_role:
            raise PermissionDenied("Administrator role required.")

    def _match_for_manage(self, pk):
        match = self.get_object()
        if not can_manage_match(self.request.user, match):
            raise PermissionDenied("You cannot manage this match.")
        return match

    def create(self, request, *args, **kwargs):
        self._require_admin()
        ser = MatchWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if data.get("tournament") and not request.user.is_super_admin:
            from apps.accounts.permissions import can_manage_tournament

            if not can_manage_tournament(request.user, data["tournament"]):
                raise PermissionDenied("You cannot manage this tournament.")
        match = create_match(data, request.user)
        audit_log(request.user, "match.create", "match", match.pk, {"name": match.name}, request)
        match = visible_matches(request.user).get(pk=match.pk)
        return Response(MatchReadSerializer(match).data, status=201)

    def partial_update(self, request, *args, **kwargs):
        match = self._match_for_manage(kwargs["pk"])
        ser = MatchWriteSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        update_match(match, ser.validated_data, request.user)
        audit_log(request.user, "match.update", "match", match.pk, request=request)
        return Response(MatchReadSerializer(visible_matches(request.user).get(pk=match.pk)).data)

    def destroy(self, request, *args, **kwargs):
        match = self._match_for_manage(kwargs["pk"])
        if match.tournament_id:
            raise ConflictError("fixture_match", "Tournament matches are managed through fixtures.")
        if match.status not in PRE_START or match.state.revision > 0:
            raise ConflictError("match_started", "Started matches cannot be deleted; cancel them instead.")
        audit_log(request.user, "match.delete", "match", match.pk, {"name": match.name}, request)
        match.delete()
        return Response(status=204)

    @extend_schema(request=OpenApiTypes.OBJECT, responses=MatchReadSerializer)
    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        match = self._match_for_manage(pk)
        target = request.data.get("status")
        if target not in [s.value for s in MatchStatus]:
            raise ApiError("invalid_status", "Unknown status")
        if target in (MatchStatus.LIVE, MatchStatus.PAUSED, MatchStatus.COMPLETED):
            raise ApiError("use_scoring", "Live, paused and completed are set through scoring actions")
        if target in (MatchStatus.SCHEDULED, MatchStatus.READY) and not (match.team_a_id and match.team_b_id):
            raise ConflictError("teams_missing", "Assign both teams first")
        match.transition(target)
        match.save()
        audit_log(request.user, "match.status", "match", match.pk, {"status": target}, request)
        return Response(MatchReadSerializer(visible_matches(request.user).get(pk=match.pk)).data)

    @extend_schema(request=ScoreActionSerializer, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=["post"])
    def score(self, request, pk=None):
        match = self.get_object()
        if not can_score_match(request.user, match):
            raise PermissionDenied("You are not allowed to score this match.")
        ser = ScoreActionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        result = scoring.perform_action(
            match.pk, request.user, d["action"], d["clientActionId"], team=d.get("team"),
            expected_revision=d.get("expectedRevision"), winner=d.get("winner"), player_id=d.get("playerId"),
        )
        return Response(result)

    @extend_schema(request=ResultSerializer, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=["post"])
    def result(self, request, pk=None):
        """Manual result entry / audited correction (administrators only; umpires cannot)."""
        match = self._match_for_manage(pk)
        ser = ResultSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        sets = [{"games": s["games"], "tiebreak": s.get("tiebreak")} for s in d["sets"]]
        result = scoring.perform_action(
            match.pk, request.user, "result", d["clientActionId"], sets=sets, reason=d["reason"],
            expected_revision=d.get("expectedRevision"),
        )
        return Response(result)

    @extend_schema(request=None, responses=MatchReadSerializer)
    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        match = self._match_for_manage(pk)
        if match.status != MatchStatus.COMPLETED:
            raise ConflictError("not_completed", "Only completed matches can be verified")
        match.result_verified = True
        match.verified_by = request.user
        match.verified_at = timezone.now()
        match.save(update_fields=["result_verified", "verified_by", "verified_at"])
        audit_log(request.user, "match.verify", "match", match.pk, request=request)
        return Response(MatchReadSerializer(visible_matches(request.user).get(pk=match.pk)).data)

    @extend_schema(responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        match = self.get_object()
        if not can_score_match(request.user, match):
            raise PermissionDenied()
        return Response({"events": scoring.history(match, limit=min(int(request.query_params.get("limit", 40)), 200))})

    @extend_schema(responses={(200, "image/png"): OpenApiTypes.BINARY})
    @action(detail=True, methods=["get"])
    def qr(self, request, pk=None):
        match = self._match_for_manage(pk)
        img = qrcode.make(f"{settings.PUBLIC_SITE_URL}/matches/{match.public_id}", box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        resp = HttpResponse(buf.getvalue(), content_type="image/png")
        resp["Cache-Control"] = "private, max-age=300"
        return resp


class DashboardView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        user = request.user
        matches = visible_matches(user)
        now = timezone.now()
        counts = matches.aggregate(
            live=Count("id", filter=Q(status__in=[MatchStatus.LIVE, MatchStatus.PAUSED])),
            completed=Count("id", filter=Q(status=MatchStatus.COMPLETED)),
            upcoming=Count("id", filter=Q(status__in=[MatchStatus.SCHEDULED, MatchStatus.READY])),
        )
        tournaments = Tournament.objects.all()
        if not user.is_super_admin:
            tournaments = tournaments.filter(Q(owner=user) | Q(managers=user)).distinct()
        reader = MatchReadSerializer
        upcoming = matches.filter(status__in=[MatchStatus.SCHEDULED, MatchStatus.READY]).order_by("scheduled_at")[:5]
        live = matches.filter(status__in=[MatchStatus.LIVE, MatchStatus.PAUSED]).order_by("-started_at")[:5]
        activity = AuditLog.objects.select_related("actor")[:12] if user.is_super_admin else \
            AuditLog.objects.select_related("actor").filter(actor=user)[:12]
        return Response({
            "totals": {
                "tournaments": tournaments.count(), "upcomingMatches": counts["upcoming"],
                "liveMatches": counts["live"], "completedMatches": counts["completed"],
                "players": Player.objects.count(), "teams": Team.objects.count(),
            },
            "upcoming": reader(upcoming, many=True).data,
            "live": reader(live, many=True).data,
            "recentActivity": [
                {"id": a.id, "actor": a.actor.display_name if a.actor else "system", "action": a.action,
                 "objectType": a.object_type, "objectId": a.object_id, "at": a.created_at}
                for a in activity
            ],
            "serverTime": now,
        })
