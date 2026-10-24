"""Public-safe match snapshot: the single shape sent to REST clients and WebSocket rooms."""
from apps.matches.models import Match

from . import engine


def load_match(match_id=None, public_id=None):
    qs = Match.objects.select_related("state", "configuration", "court", "tournament", "team_a", "team_b")
    qs = qs.prefetch_related("participants__player")
    return qs.get(pk=match_id) if match_id is not None else qs.get(public_id=public_id)


def _team_block(match, index):
    team = match.team_a if index == 0 else match.team_b
    players = [p.player.full_name for p in match.participants.all() if p.team_index == index]
    return {"name": team.name if team else "TBD", "players": players}


def build_snapshot(match):
    """Contains NO private data (no umpire, no emails, no internal ids)."""
    state = match.state.data
    cfg = match.configuration.engine_config()
    snap = {
        "publicId": match.public_id,
        "name": match.name,
        "status": match.status,
        "revision": match.state.revision,
        "scheduledAt": match.scheduled_at.isoformat() if match.scheduled_at else None,
        "court": match.court.name if match.court else None,
        "tournament": {"name": match.tournament.name, "slug": match.tournament.slug} if match.tournament else None,
        "teams": [_team_block(match, 0), _team_block(match, 1)],
        "state": state,
        "config": cfg,
        "durationSeconds": match.duration_seconds(),
        "winnerIndex": state.get("winner"),
        "startedAt": match.started_at.isoformat() if match.started_at else None,
        "completedAt": match.completed_at.isoformat() if match.completed_at else None,
    }
    snap["display"] = engine.describe(cfg, state) if state else None
    return snap
