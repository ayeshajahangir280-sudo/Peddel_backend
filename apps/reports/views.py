import re

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.errors import ApiError, ConflictError
from apps.accounts.permissions import IsAdminRole, can_manage_match, can_manage_tournament
from apps.audit.services import log as audit_log
from apps.matches.models import Match
from apps.tournaments.models import Tournament, TournamentStatus

from . import pdf

SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _pdf_response(data, filename):
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{SAFE.sub("-", filename)}"'
    resp["Cache-Control"] = "private, no-store"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


class MatchPdfView(APIView):
    """Admin-only. Umpires are NOT given report access merely because they can score a match."""

    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(responses={(200, "application/pdf"): OpenApiTypes.BINARY}, tags=["admin-reports"])
    def get(self, request, pk):
        try:
            match = Match.objects.select_related(
                "tournament", "court", "umpire", "team_a", "team_b", "winner", "state", "configuration"
            ).prefetch_related("participants__player").get(pk=pk)
        except Match.DoesNotExist:
            raise ApiError("not_found", "Match not found", 404)
        if not can_manage_match(request.user, match):
            raise PermissionDenied("You cannot export this match.")
        try:
            data = pdf.build_match_pdf(match)
        except ValueError:
            raise ConflictError("result_not_final", "A result PDF is only available for completed matches")
        audit_log(request.user, "report.match_pdf", "match", match.pk, request=request)
        return _pdf_response(data, f"match-result-{match.reference}.pdf")


SECTION_SETS = {
    "full": ("info", "teams", "standings", "fixtures", "knockout", "champion"),
    "standings": ("info", "standings"),
    "fixtures": ("info", "fixtures", "knockout"),
}


class TournamentPdfView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    @extend_schema(responses={(200, "application/pdf"): OpenApiTypes.BINARY}, tags=["admin-reports"])
    def get(self, request, pk, kind="full"):
        try:
            t = Tournament.objects.select_related("champion").get(pk=pk)
        except Tournament.DoesNotExist:
            raise ApiError("not_found", "Tournament not found", 404)
        if not can_manage_tournament(request.user, t):
            raise PermissionDenied("You cannot export this tournament.")
        data = pdf.build_tournament_pdf(t, SECTION_SETS[kind])
        final = t.status == TournamentStatus.COMPLETED and t.champion_id
        prefix = {"full": "tournament-results" if final else "tournament-progress",
                  "standings": "tournament-standings", "fixtures": "tournament-fixtures"}[kind]
        audit_log(request.user, f"report.tournament_pdf.{kind}", "tournament", t.pk, request=request)
        return _pdf_response(data, f"{prefix}-{t.slug}.pdf")
