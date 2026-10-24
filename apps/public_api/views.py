"""Public, read-only API. Every response is built from an explicit allow-list of fields.

There are intentionally NO report/PDF routes or fields anywhere in this module.
"""
from html import escape

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from django.db import connection
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.matches.models import Match, MatchStatus
from apps.scoring import engine
from apps.scoring.models import EventType, ScoreEvent
from apps.scoring.snapshot import build_snapshot, load_match
from apps.scoring.visibility import is_match_public
from apps.standings.models import TournamentStanding
from apps.tournaments.models import Tournament, TournamentFixture, TournamentStatus, Visibility
from apps.tournaments.services import final_rankings


class PublicView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


def public_tournaments():
    return Tournament.objects.filter(
        visibility=Visibility.PUBLIC, status__in=[TournamentStatus.PUBLISHED, TournamentStatus.COMPLETED]
    )


def _img(field):
    return f"/media/{field.name}" if field else None


def phase_of(t, live_ids):
    if t.status == TournamentStatus.COMPLETED:
        return "completed"
    if t.pk in live_ids:
        return "live"
    return "upcoming"


def _card(t, live_ids):
    return {
        "slug": t.slug, "name": t.name, "logo": _img(t.logo), "cover": _img(t.cover_image), "venue": t.venue,
        "startDate": t.start_date, "endDate": t.end_date, "phase": phase_of(t, live_ids),
        "teamsCount": getattr(t, "teams_n", 0), "organizer": t.organizer,
        "description": (t.description or "")[:240],
    }


class TournamentListView(PublicView):
    @extend_schema(responses=OpenApiTypes.OBJECT, tags=["public"])
    def get(self, request):
        qs = public_tournaments().annotate(
            teams_n=Count("registrations", filter=Q(registrations__withdrawn=False), distinct=True)
        )
        q = request.query_params.get("search")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(venue__icontains=q))
        live_ids = set(Match.objects.filter(status__in=[MatchStatus.LIVE, MatchStatus.PAUSED], tournament__in=qs)
                       .values_list("tournament_id", flat=True))
        phase = request.query_params.get("phase")
        items = [_card(t, live_ids) for t in qs.order_by("-start_date", "-id")[:200]]
        if phase in ("upcoming", "live", "completed"):
            items = [i for i in items if i["phase"] == phase]
        return Response({"results": items, "count": len(items)})


def _match_summary(m):
    data = m.state.data
    return {
        "publicId": m.public_id, "status": m.status,
        "scheduledAt": m.scheduled_at, "court": m.court.name if m.court else None,
        "sets": data.get("sets", []), "games": data.get("games"), "winnerIndex": data.get("winner"),
        "inProgress": m.status in (MatchStatus.LIVE, MatchStatus.PAUSED),
    }


class TournamentDetailView(PublicView):
    @extend_schema(responses=OpenApiTypes.OBJECT, tags=["public"])
    def get(self, request, slug):
        try:
            t = public_tournaments().select_related("champion").get(slug=slug)
        except Tournament.DoesNotExist:
            raise Http404
        regs = t.registrations.filter(withdrawn=False).select_related("team", "group").prefetch_related("team__memberships__player")
        teams = []
        for r in regs:
            players = []
            for m in r.team.memberships.all():
                p = m.player
                players.append({"name": p.full_name, "photo": _img(p.photo) if p.show_photo_publicly else None})
            teams.append({"name": r.team.name, "group": r.group.name if r.group else None, "players": players})
        fixtures = (TournamentFixture.objects.filter(tournament=t)
                    .select_related("group", "team_a", "team_b", "match__state", "match__court", "next_fixture")
                    .order_by("stage", "round_number", "position"))
        group_fx, ko_fx = [], []
        for f in fixtures:
            if f.is_bye:
                continue
            visible = f.match and f.match.status != MatchStatus.DRAFT
            entry = {
                "round": f.round_name, "roundNumber": f.round_number, "position": f.position,
                "group": f.group.name if f.group else None,
                "teamA": f.team_a.name if f.team_a else f.slot_a_source.get("label", "TBD"),
                "teamB": f.team_b.name if f.team_b else f.slot_b_source.get("label", "TBD"),
                "match": _match_summary(f.match) if visible else None,
            }
            (group_fx if f.stage == "group" else ko_fx).append(entry)
        live_ids = {t.pk} if any(f["match"] and f["match"]["inProgress"] for f in group_fx + ko_fx) else set()
        stand = TournamentStanding.objects.filter(tournament=t).select_related("team", "group").order_by("group__order", "rank")
        groups = {}
        for s in stand:
            groups.setdefault(s.group.name, []).append({
                "rank": s.rank, "team": s.team.name, "played": s.played, "wins": s.wins, "losses": s.losses,
                "points": s.points, "setDifference": s.set_difference, "gameDifference": s.game_difference,
                "qualified": s.qualified,
            })
        complete = t.status == TournamentStatus.COMPLETED
        card = _card(t, live_ids)
        card["teamsCount"] = len(teams)
        card.update({
            "description": t.description, "rules": t.rules,
            "format": t.format, "rankingCriteria": t.ranking_criteria, "pointsPerWin": t.points_win,
            "teams": teams,
            "standings": [{"group": g, "rows": rows, "isFinal": complete} for g, rows in groups.items()],
            "groupFixtures": group_fx, "knockout": ko_fx,
            "champion": t.champion.name if (t.champion and complete) else None,
            "rankings": [{"place": p, "team": team.name} for p, team in final_rankings(t)] if complete else [],
        })
        return Response(card)


class MatchDetailView(PublicView):
    @extend_schema(responses=OpenApiTypes.OBJECT, tags=["public"])
    def get(self, request, public_id):
        try:
            match = load_match(public_id=public_id.upper())
        except Match.DoesNotExist:
            raise Http404
        if not is_match_public(match):
            raise Http404
        snap = build_snapshot(match)
        cfg = match.configuration.engine_config()
        events = (ScoreEvent.objects.filter(match=match, event_type__in=[EventType.POINT, EventType.UNDO])
                  .order_by("-sequence")[:30])
        snap["timeline"] = [
            {"sequence": e.sequence, "type": e.event_type, "team": e.team_index, "at": e.created_at,
             "sets": e.state_after.get("sets", []), "games": e.state_after.get("games"),
             "points": engine.point_labels(cfg, e.state_after)[0]}
            for e in events
        ]
        return Response(snap)


@extend_schema(responses=OpenApiTypes.OBJECT, tags=["health"])
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([AnonRateThrottle])
def health(request):
    db_ok = True
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
    except Exception:
        db_ok = False
    return Response({"status": "ok" if db_ok else "degraded", "database": db_ok}, status=200 if db_ok else 503)


def _share_page(title, desc, image, target):
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title><meta name='description' content='{escape(desc)}'>"
        f"<meta property='og:title' content='{escape(title)}'><meta property='og:description' content='{escape(desc)}'>"
        "<meta property='og:type' content='website'>"
        + (f"<meta property='og:image' content='{escape(image)}'>" if image else "")
        + "<meta name='twitter:card' content='summary_large_image'>"
        f"<meta http-equiv='refresh' content='0;url={escape(target)}'></head>"
        f"<body><a href='{escape(target)}'>{escape(title)}</a></body></html>"
    )
    return HttpResponse(html, content_type="text/html")


def share_tournament(request, slug):
    try:
        t = public_tournaments().get(slug=slug)
    except Tournament.DoesNotExist:
        raise Http404
    img = request.build_absolute_uri(f"/media/{(t.cover_image or t.logo).name}") if (t.cover_image or t.logo) else ""
    return _share_page(t.name, t.description[:200] or f"{t.name} - padel tournament", img,
                       f"{settings.PUBLIC_SITE_URL}/public-tournaments/{t.slug}")


def share_match(request, public_id):
    try:
        m = load_match(public_id=public_id.upper())
    except Match.DoesNotExist:
        raise Http404
    if not is_match_public(m):
        raise Http404
    a = m.team_a.name if m.team_a else "TBD"
    b = m.team_b.name if m.team_b else "TBD"
    return _share_page(f"{a} vs {b}", f"Live padel score - {m.name}", "", f"{settings.PUBLIC_SITE_URL}/matches/{m.public_id}")
