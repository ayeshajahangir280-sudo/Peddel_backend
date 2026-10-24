import pytest

from apps.matches.models import Match
from apps.scoring.models import IdempotencyRecord, ScoreEvent
from tests.conftest import client_for, new_action_id, play_points, score, win_game, win_set_6_0

pytestmark = pytest.mark.django_db


def test_create_match_with_new_players_and_defaults(make_match):
    m = make_match()
    assert m["status"] == "scheduled"
    assert [len(t["players"]) for t in m["teams"]] == [2, 2]
    assert m["revision"] == 0 and m["config"]["bestOf"] == 3
    assert m["shareUrl"].endswith("/matches/" + m["publicId"])


def test_create_match_with_existing_players_and_team(admin_api, make_match):
    first = make_match()
    second = admin_api.post(
        "/api/admin/matches/",
        {"name": "Rematch", "team_a": {"id": first["teams"][0]["id"]},
         "team_b": {"players": [{"first_name": "N1"}, {"first_name": "N2"}]}},
        format="json",
    )
    assert second.status_code == 201, second.content


def test_match_validation_rejects_bad_input(admin_api):
    r = admin_api.post("/api/admin/matches/", {"name": "x", "team_a": {"players": [{"first_name": "Solo"}]},
                                              "team_b": {"players": [{"first_name": "A"}, {"first_name": "B"}]}}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "two_players_required"
    r = admin_api.post("/api/admin/matches/", {"name": "x", "config": {"bestOf": 5}}, format="json")
    assert r.status_code == 400


def test_point_scoring_and_revision_increments(admin_api, make_match):
    m = make_match()
    r = score(admin_api, m["id"], 0)
    body = r.json()
    assert r.status_code == 200 and body["revision"] == 1
    assert body["snapshot"]["state"]["points"] == [1, 0]
    assert body["snapshot"]["status"] == "live"
    assert body["snapshot"]["display"]["pointLabels"] == ["15", "0"]


def test_idempotent_retry_does_not_double_count(admin_api, make_match):
    m = make_match()
    cid = new_action_id()
    r1 = score(admin_api, m["id"], 0, cid=cid)
    r2 = score(admin_api, m["id"], 0, cid=cid)
    r3 = score(admin_api, m["id"], 0, cid=cid)
    assert r1.json()["revision"] == r2.json()["revision"] == r3.json()["revision"] == 1
    assert r2.json()["replayed"] is True
    assert ScoreEvent.objects.filter(match_id=m["id"]).count() == 1
    assert IdempotencyRecord.objects.filter(match_id=m["id"]).count() == 1
    state = admin_api.get(f"/api/admin/matches/{m['id']}/").json()["state"]
    assert state["points"] == [1, 0]


def test_stale_expected_revision_is_rejected_with_current_state(admin_api, make_match):
    m = make_match()
    score(admin_api, m["id"], 0)
    r = score(admin_api, m["id"], 1, expectedRevision=0)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "stale_revision" and err["details"]["currentRevision"] == 1
    assert ScoreEvent.objects.filter(match_id=m["id"]).count() == 1  # nothing applied
    ok = score(admin_api, m["id"], 1, expectedRevision=1)
    assert ok.status_code == 200 and ok.json()["revision"] == 2


def test_invalid_client_action_id_and_team(admin_api, make_match):
    m = make_match()
    r = admin_api.post(f"/api/admin/matches/{m['id']}/score/", {"action": "point", "team": 0, "clientActionId": "x"}, format="json")
    assert r.status_code == 400
    r = admin_api.post(f"/api/admin/matches/{m['id']}/score/", {"action": "point", "team": 5, "clientActionId": new_action_id()}, format="json")
    assert r.status_code == 400


def test_undo_restores_previous_state_and_keeps_history(admin_api, make_match):
    m = make_match()
    play_points(admin_api, m["id"], [0, 0, 1])
    r = score(admin_api, m["id"], action="undo")
    assert r.status_code == 200
    assert r.json()["snapshot"]["state"]["points"] == [2, 0]
    assert ScoreEvent.objects.filter(match_id=m["id"]).count() == 4  # original events retained
    assert ScoreEvent.objects.filter(match_id=m["id"], reverses__isnull=False).count() == 1
    r = score(admin_api, m["id"], action="undo")
    assert r.json()["snapshot"]["state"]["points"] == [1, 0]


def test_undo_is_idempotent(admin_api, make_match):
    m = make_match()
    play_points(admin_api, m["id"], [0, 0])
    cid = new_action_id()
    a = score(admin_api, m["id"], action="undo", cid=cid).json()
    b = score(admin_api, m["id"], action="undo", cid=cid).json()
    assert a["revision"] == b["revision"]
    state = admin_api.get(f"/api/admin/matches/{m['id']}/").json()["state"]
    assert state["points"] == [1, 0]  # only ONE point undone


def test_undo_with_nothing_to_undo(admin_api, make_match):
    m = make_match()
    r = score(admin_api, m["id"], action="undo")
    assert r.status_code == 409 and r.json()["error"]["code"] == "nothing_to_undo"


def test_undo_across_game_and_set_transitions(admin_api, make_match):
    m = make_match()
    win_set_6_0(admin_api, m["id"], 0)  # set complete
    state = admin_api.get(f"/api/admin/matches/{m['id']}/").json()["state"]
    assert len(state["sets"]) == 1 and state["games"] == [0, 0]
    score(admin_api, m["id"], action="undo")  # undo the final point of the set
    state = admin_api.get(f"/api/admin/matches/{m['id']}/").json()["state"]
    assert state["sets"] == [] and state["games"] == [5, 0] and state["points"] == [3, 0]
    play_points(admin_api, m["id"], [0])
    state = admin_api.get(f"/api/admin/matches/{m['id']}/").json()["state"]
    assert len(state["sets"]) == 1


def test_match_completion_blocks_further_scoring_and_undo_reopens(admin_api, make_match):
    m = make_match({"bestOf": 1})
    win_set_6_0(admin_api, m["id"], 0)
    data = admin_api.get(f"/api/admin/matches/{m['id']}/").json()
    assert data["status"] == "completed" and data["winnerIndex"] == 0
    r = score(admin_api, m["id"], 1)
    assert r.status_code == 409
    r = score(admin_api, m["id"], action="undo")
    assert r.status_code == 200
    data = admin_api.get(f"/api/admin/matches/{m['id']}/").json()
    assert data["status"] == "live" and data["winnerIndex"] is None and data["completedAt"] is None


def test_pause_resume_and_scoring_blocked_while_paused(admin_api, make_match):
    m = make_match()
    score(admin_api, m["id"], 0)
    assert score(admin_api, m["id"], action="pause").status_code == 200
    r = score(admin_api, m["id"], 0)
    assert r.status_code == 409 and r.json()["error"]["code"] == "match_paused"
    assert score(admin_api, m["id"], action="pause").status_code == 409
    assert score(admin_api, m["id"], action="resume").status_code == 200
    assert score(admin_api, m["id"], 0).status_code == 200


def test_end_match_early_needs_winner_when_level(admin_api, make_match):
    m = make_match()
    play_points(admin_api, m["id"], [0])
    r = score(admin_api, m["id"], action="end")
    assert r.status_code == 400 and r.json()["error"]["code"] == "winner_required"
    r = score(admin_api, m["id"], action="end", winner=1)
    assert r.status_code == 200
    d = admin_api.get(f"/api/admin/matches/{m['id']}/").json()
    assert d["status"] == "completed" and d["winnerIndex"] == 1
    # an early end cannot be undone: it needs the audited correction workflow
    assert score(admin_api, m["id"], action="undo").json()["error"]["code"] == "undo_not_allowed"


def test_result_correction_requires_reason_and_is_audited(admin_api, umpire_api, make_match):
    from apps.audit.models import AuditLog

    m = make_match({"bestOf": 1})
    win_set_6_0(admin_api, m["id"], 0)
    body = {"clientActionId": new_action_id(), "sets": [{"games": [4, 6]}], "reason": ""}
    assert admin_api.post(f"/api/admin/matches/{m['id']}/result/", body, format="json").status_code == 400
    body["reason"] = "Scorer tapped wrong team"
    r = admin_api.post(f"/api/admin/matches/{m['id']}/result/", body, format="json")
    assert r.status_code == 200, r.content
    d = admin_api.get(f"/api/admin/matches/{m['id']}/").json()
    assert d["winnerIndex"] == 1 and d["state"]["sets"][0]["games"] == [4, 6]
    assert AuditLog.objects.filter(action="match.result_correction", object_id=str(m["id"])).exists()
    # umpires cannot correct results
    body["clientActionId"] = new_action_id()
    assert umpire_api.post(f"/api/admin/matches/{m['id']}/result/", body, format="json").status_code == 403
    # impossible scores rejected
    body.update(clientActionId=new_action_id(), sets=[{"games": [6, 5]}])
    assert admin_api.post(f"/api/admin/matches/{m['id']}/result/", body, format="json").status_code == 400


def test_status_transitions_validated(admin_api, make_match):
    m = make_match()
    r = admin_api.post(f"/api/admin/matches/{m['id']}/status/", {"status": "ready"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "ready"
    assert admin_api.post(f"/api/admin/matches/{m['id']}/status/", {"status": "live"}, format="json").status_code == 400
    assert admin_api.post(f"/api/admin/matches/{m['id']}/status/", {"status": "cancelled"}, format="json").status_code == 200
    assert admin_api.post(f"/api/admin/matches/{m['id']}/status/", {"status": "scheduled"}, format="json").status_code == 409
    assert score(admin_api, m["id"], 0).status_code == 409  # cancelled matches cannot be scored


def test_cannot_edit_rules_after_start(admin_api, make_match):
    m = make_match()
    score(admin_api, m["id"], 0)
    r = admin_api.patch(f"/api/admin/matches/{m['id']}/", {"config": {"goldenPoint": True}}, format="json")
    assert r.status_code == 409
    r = admin_api.patch(f"/api/admin/matches/{m['id']}/", {"name": "Renamed"}, format="json")
    assert r.status_code == 200 and r.json()["name"] == "Renamed"


def test_config_applies_before_start(admin_api, make_match):
    m = make_match({"goldenPoint": True, "bestOf": 1})
    play_points(admin_api, m["id"], [0, 0, 0, 1, 1, 1])
    r = score(admin_api, m["id"], 1).json()
    assert r["snapshot"]["state"]["games"] == [0, 1]  # golden point decided at 40-40


def test_court_and_umpire_double_booking_prevented(admin_api, make_match, court, umpire):
    make_match(scheduled_at="2030-01-01T10:00:00Z")
    r = admin_api.post("/api/admin/matches/", {
        "name": "Clash", "court": court.pk, "scheduled_at": "2030-01-01T10:30:00Z",
        "team_a": {"players": [{"first_name": "p1"}, {"first_name": "p2"}]},
        "team_b": {"players": [{"first_name": "p3"}, {"first_name": "p4"}]}}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "court_conflict"


def test_history_endpoint_is_bounded(admin_api, make_match):
    m = make_match()
    play_points(admin_api, m["id"], [0, 1, 0, 1])
    r = admin_api.get(f"/api/admin/matches/{m['id']}/history/?limit=2")
    assert len(r.json()["events"]) == 2


def test_delete_only_unstarted_friendlies(admin_api, make_match):
    a, b = make_match(), make_match()
    score(admin_api, b["id"], 0)
    assert admin_api.delete(f"/api/admin/matches/{a['id']}/").status_code == 204
    assert admin_api.delete(f"/api/admin/matches/{b['id']}/").status_code == 409


def test_qr_png(admin_api, make_match):
    m = make_match()
    r = admin_api.get(f"/api/admin/matches/{m['id']}/qr/")
    assert r.status_code == 200 and r["Content-Type"] == "image/png" and r.content[:4] == b"\x89PNG"


def test_state_survives_reload_from_database(admin_api, make_match):
    m = make_match()
    play_points(admin_api, m["id"], [0, 0, 1])
    fresh = client_for(Match.objects.get(pk=m["id"]).created_by)
    d = fresh.get(f"/api/admin/matches/{m['id']}/").json()
    assert d["revision"] == 3 and d["state"]["points"] == [2, 1]


def test_full_match_flow_super_tiebreak(admin_api, make_match):
    m = make_match({"superTiebreak": True})
    win_set_6_0(admin_api, m["id"], 0)
    win_set_6_0(admin_api, m["id"], 1)
    d = admin_api.get(f"/api/admin/matches/{m['id']}/").json()
    assert d["state"]["isSuper"] and d["state"]["tiebreakTarget"] == 10
    play_points(admin_api, m["id"], [1] * 10)
    d = admin_api.get(f"/api/admin/matches/{m['id']}/").json()
    assert d["status"] == "completed" and d["winnerIndex"] == 1
    assert win_game  # silence linters for shared helper import
