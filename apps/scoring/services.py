"""Transactional scoring service: the only code path that mutates a live score."""
import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.errors import ApiError, ConflictError
from apps.audit.services import log as audit_log
from apps.matches.models import PRE_START, Match, MatchState, MatchStatus
from apps.notifications import outbox

from . import engine
from .models import EventType, IdempotencyRecord, ScoreEvent
from .snapshot import build_snapshot, load_match

ACTIONS = {"point", "undo", "pause", "resume", "end", "result"}
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,64}$")


def perform_action(match_id, actor, action, client_action_id, **kwargs):
    """Apply one scoring action idempotently. Returns the response dict."""
    if action not in ACTIONS:
        raise ApiError("invalid_action", f"Unknown action '{action}'")
    if not client_action_id or not CLIENT_ID_RE.match(str(client_action_id)):
        raise ApiError("invalid_client_action_id", "clientActionId must be 8-64 URL-safe characters")
    try:
        with transaction.atomic():
            return _perform(match_id, actor, action, str(client_action_id), **kwargs)
    except IntegrityError:
        # Lost a race against an identical concurrent request: return its stored result.
        rec = IdempotencyRecord.objects.filter(match_id=match_id, client_action_id=client_action_id).first()
        if rec:
            return {**rec.response, "replayed": True}
        raise


def _complete(match, after, now):
    match.status = MatchStatus.COMPLETED.value
    match.completed_at = now
    if not match.started_at:
        match.started_at = now
    if match.paused_at:
        match.paused_seconds += int((now - match.paused_at).total_seconds())
        match.paused_at = None
    match.winner = match.team_a if after["winner"] == 0 else match.team_b
    match.result_verified = False
    match.verified_by = None
    match.verified_at = None


def _require_teams(match):
    if not (match.team_a_id and match.team_b_id):
        raise ConflictError("teams_missing", "Both teams must be assigned before the match can be scored")
    counts = [0, 0]
    for p in match.participants.all():
        counts[p.team_index] += 1
    if counts != [2, 2]:
        raise ConflictError("players_missing", "Each team needs two players before the match can be scored")


def _perform(match_id, actor, action, cid, team=None, expected_revision=None, winner=None,
             player_id=None, sets=None, reason=""):
    # Row lock serialises all writers for this match (no-op on SQLite, row lock on PostgreSQL).
    ms = MatchState.objects.select_for_update().get(match_id=match_id)

    rec = IdempotencyRecord.objects.filter(match_id=match_id, client_action_id=cid).first()
    if rec:
        return {**rec.response, "replayed": True}

    match = load_match(match_id)
    if expected_revision is not None and expected_revision != ms.revision:
        raise ConflictError(
            "stale_revision", "Another action changed this match. Reload the latest score.",
            details={"currentRevision": ms.revision, "snapshot": build_snapshot(match)},
        )

    cfg = match.configuration.engine_config()
    before = ms.data
    now = timezone.now()
    status = match.status
    target = None

    if action == "point":
        if team not in (0, 1):
            raise ApiError("invalid_team", "team must be 0 or 1")
        if status == MatchStatus.PAUSED:
            raise ConflictError("match_paused", "The match is paused. Resume before scoring.")
        if status not in (*PRE_START, MatchStatus.LIVE) or status == MatchStatus.DRAFT:
            raise ConflictError("not_scorable", f"A {status} match cannot be scored")
        _require_teams(match)
        try:
            after = engine.apply_point(cfg, before, team)
        except engine.EngineError as e:
            raise ConflictError(e.code, str(e))
        if status in PRE_START:
            match.transition(MatchStatus.LIVE)
            match.started_at = now
        if after["completed"]:
            _complete(match, after, now)

    elif action == "undo":
        if status == MatchStatus.CANCELLED:
            raise ConflictError("not_scorable", "A cancelled match cannot be changed")
        last = (
            ScoreEvent.objects.filter(match=match, reversed_by__isnull=True,
                                      event_type__in=[EventType.POINT, EventType.END, EventType.RESULT])
            .order_by("-sequence").first()
        )
        if last is None:
            raise ConflictError("nothing_to_undo", "There is nothing to undo")
        if last.event_type != EventType.POINT:
            raise ConflictError("undo_not_allowed", "The last action cannot be undone. Use the result correction workflow.")
        target = last
        after = target.state_before
        if status == MatchStatus.COMPLETED:
            from apps.tournaments import services as tservices

            match.status = MatchStatus.LIVE.value
            match.completed_at = None
            match.winner = None
            match.result_verified = False
            match.save()
            ms.data = after
            tservices.on_match_reopened(match)  # raises ConflictError if downstream play has begun
        team = target.team_index

    elif action == "pause":
        if status != MatchStatus.LIVE:
            raise ConflictError("invalid_transition", "Only a live match can be paused")
        match.transition(MatchStatus.PAUSED)
        match.paused_at = now
        after = before

    elif action == "resume":
        if status != MatchStatus.PAUSED:
            raise ConflictError("invalid_transition", "Only a paused match can be resumed")
        match.transition(MatchStatus.LIVE)
        match.paused_seconds += int((now - match.paused_at).total_seconds()) if match.paused_at else 0
        match.paused_at = None
        after = before

    elif action == "end":
        if status not in (MatchStatus.LIVE, MatchStatus.PAUSED):
            raise ConflictError("invalid_transition", "Only a live or paused match can be ended")
        lead = engine.sets_won(before)
        if winner not in (0, 1):
            if lead[0] == lead[1]:
                raise ApiError("winner_required", "Choose the winning team to end the match early")
            winner = 0 if lead[0] > lead[1] else 1
        after = engine.state_from_early_end(before, winner)
        _complete(match, after, now)
        team = winner

    else:  # result: manual entry or audited correction
        if status == MatchStatus.CANCELLED:
            raise ConflictError("not_scorable", "A cancelled match cannot be changed")
        _require_teams(match)
        if not (reason or "").strip():
            raise ApiError("reason_required", "A reason is required for result entry and corrections")
        try:
            after = engine.build_completed_state(cfg, sets or [])
        except engine.EngineError as e:
            raise ApiError(e.code, str(e))
        was_completed = status == MatchStatus.COMPLETED
        if was_completed:
            match.status = MatchStatus.LIVE.value
        elif status in PRE_START:
            match.status = MatchStatus.LIVE.value
        _complete(match, after, now)
        team = after["winner"]
        audit_log(actor, "match.result_correction" if was_completed else "match.result_entry", "match", match.pk,
                  {"before": before.get("sets"), "after": after["sets"], "reason": reason})

    ms.revision += 1
    ms.data = after
    ms.save(update_fields=["revision", "data", "updated_at"])
    match.save()

    event = ScoreEvent.objects.create(
        match=match, sequence=ms.revision, client_action_id=cid, event_type=action, actor=actor,
        team_index=team if action in ("point", "undo", "end", "result") else None,
        player_id=player_id if action == "point" else None,
        state_before=before, state_after=after, reverses=target, reason=(reason or "")[:300],
    )

    if action == "end":
        audit_log(actor, "match.end", "match", match.pk, {"winner": winner})
    if action == "undo":
        audit_log(actor, "match.undo", "match", match.pk, {"reversed_sequence": target.sequence})

    if after.get("completed") and action in ("point", "end", "result"):
        from apps.tournaments import services as tservices

        tservices.on_match_completed(match)

    match = load_match(match_id)
    snapshot = build_snapshot(match)
    response = {"revision": ms.revision, "eventId": event.pk, "snapshot": snapshot, "replayed": False}
    IdempotencyRecord.objects.create(match=match, client_action_id=cid, response=response)
    outbox.enqueue(match.public_id, ms.revision, snapshot)
    return response


def initialise_match_state(match):
    cfg = match.configuration.engine_config()
    MatchState.objects.update_or_create(match=match, defaults={"revision": 0, "data": engine.initial_state(cfg)})


def history(match, limit=50):
    """Bounded event history for the scoring screen."""
    return list(
        ScoreEvent.objects.filter(match=match).order_by("-sequence")
        .values("sequence", "event_type", "team_index", "created_at", "reverses_id", "reason")[:limit]
    )


__all__ = ["perform_action", "initialise_match_state", "history", "Match"]
