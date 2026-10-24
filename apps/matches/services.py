from datetime import timedelta

from django.db import transaction

from apps.accounts.errors import ApiError, ConflictError
from apps.players.models import Player
from apps.scoring import engine
from apps.scoring.services import initialise_match_state
from apps.teams.models import Team, TeamPlayer

from .models import Match, MatchConfiguration, MatchParticipant, MatchStatus

CONFIG_FIELDS = {
    "bestOf": "best_of", "goldenPoint": "golden_point", "tiebreakEnabled": "tiebreak_enabled",
    "tiebreakAt": "tiebreak_at", "tiebreakPoints": "tiebreak_points", "superTiebreak": "super_tiebreak",
    "superTiebreakPoints": "super_tiebreak_points", "gamesPerSet": "games_per_set", "firstServer": "first_server",
}
CONFLICT_WINDOW = timedelta(minutes=90)


def resolve_team(side, actor):
    """Existing team by id, or a new team from two existing/new players."""
    if side.get("id"):
        try:
            team = Team.objects.prefetch_related("memberships__player").get(pk=side["id"])
        except Team.DoesNotExist:
            raise ApiError("team_not_found", "Team does not exist")
        if team.memberships.count() != 2:
            raise ApiError("team_incomplete", f"Team '{team.name}' must have two players")
        return team
    players = []
    for p in side.get("players") or []:
        if p.get("id"):
            try:
                players.append(Player.objects.get(pk=p["id"]))
            except Player.DoesNotExist:
                raise ApiError("player_not_found", "Player does not exist")
        else:
            first, last = (p.get("first_name") or "").strip(), (p.get("last_name") or "").strip()
            if not first:
                raise ApiError("player_name_required", "New players need a first name")
            players.append(Player.objects.create(first_name=first, last_name=last, created_by=actor))
    if len(players) != 2 or players[0].pk == players[1].pk:
        raise ApiError("two_players_required", "Each team needs two different players")
    name = (side.get("name") or "").strip() or " / ".join((p.last_name or p.first_name) for p in players)
    team = Team.objects.create(name=name, created_by=actor)
    for pos, p in enumerate(players, start=1):
        TeamPlayer.objects.create(team=team, player=p, position=pos)
    return team


def set_participants(match, team_index, team):
    match.participants.filter(team_index=team_index).delete()
    for tp in team.memberships.select_related("player").order_by("position"):
        MatchParticipant.objects.create(match=match, team_index=team_index, position=tp.position, player=tp.player)


def check_scheduling(match, court, umpire, scheduled_at):
    if not scheduled_at:
        return
    lo, hi = scheduled_at - CONFLICT_WINDOW, scheduled_at + CONFLICT_WINDOW
    others = Match.objects.filter(scheduled_at__gt=lo, scheduled_at__lt=hi).exclude(
        status__in=[MatchStatus.CANCELLED, MatchStatus.COMPLETED]).exclude(pk=match.pk if match else None)
    if court and others.filter(court=court).exists():
        raise ConflictError("court_conflict", f"{court.name} is already booked around that time")
    if umpire and others.filter(umpire=umpire).exists():
        raise ConflictError("umpire_conflict", f"{umpire.display_name} is already assigned around that time")


def apply_config(match, config):
    conf = match.configuration
    merged = {**conf.engine_config(), **(config or {})}
    merged = engine.normalize_config(merged)
    for key, field in CONFIG_FIELDS.items():
        setattr(conf, field, merged[key])
    conf.save()


@transaction.atomic
def create_match(data, actor):
    tournament = data.get("tournament")
    team_a = resolve_team(data["team_a"], actor) if data.get("team_a") else None
    team_b = resolve_team(data["team_b"], actor) if data.get("team_b") else None
    if bool(team_a) != bool(team_b):
        raise ApiError("both_teams_required", "Provide both teams or neither")
    if team_a and team_a.pk == team_b.pk:
        raise ApiError("same_team", "A team cannot play itself")
    if team_a:
        ids_a = set(team_a.memberships.values_list("player_id", flat=True))
        ids_b = set(team_b.memberships.values_list("player_id", flat=True))
        if ids_a & ids_b:
            raise ApiError("shared_player", "A player cannot be in both teams")
    court, umpire, when = data.get("court"), data.get("umpire"), data.get("scheduled_at")
    match = Match(
        name=data["name"], tournament=tournament, team_a=team_a, team_b=team_b, court=court, umpire=umpire,
        scheduled_at=when, is_public=data.get("is_public", True), created_by=actor,
        status=MatchStatus.SCHEDULED.value if team_a else MatchStatus.DRAFT.value,
    )
    check_scheduling(None, court, umpire, when)
    match.save()
    MatchConfiguration.objects.create(match=match)
    apply_config(match, data.get("config"))
    match.refresh_from_db()
    initialise_match_state(match)
    if team_a:
        set_participants(match, 0, team_a)
        set_participants(match, 1, team_b)
    return match


@transaction.atomic
def update_match(match, data, actor):
    started = match.status not in ("draft", "scheduled", "ready") or match.state.revision > 0
    if started and ("config" in data or "team_a" in data or "team_b" in data):
        raise ConflictError("match_started", "Teams and rules cannot be changed after the match has started")
    if match.tournament_id and ("team_a" in data or "team_b" in data):
        raise ConflictError("fixture_teams_locked", "Teams of tournament matches are set by the fixtures")
    for f in ("name", "is_public"):
        if f in data:
            setattr(match, f, data[f])
    court = data["court"] if "court" in data else match.court
    umpire = data["umpire"] if "umpire" in data else match.umpire
    when = data["scheduled_at"] if "scheduled_at" in data else match.scheduled_at
    if any(k in data for k in ("court", "umpire", "scheduled_at")):
        check_scheduling(match, court if "court" in data else None, umpire if "umpire" in data else None, when)
    match.court, match.umpire, match.scheduled_at = court, umpire, when
    for key, idx in (("team_a", 0), ("team_b", 1)):
        if key in data and data[key]:
            team = resolve_team(data[key], actor)
            setattr(match, key, team)
            set_participants(match, idx, team)
    if match.team_a_id and match.team_b_id:
        if match.team_a_id == match.team_b_id:
            raise ApiError("same_team", "A team cannot play itself")
        if match.status == MatchStatus.DRAFT:
            match.status = MatchStatus.SCHEDULED.value
    match.save()
    if "config" in data:
        apply_config(match, data["config"])
        initialise_match_state(match)
    return match
