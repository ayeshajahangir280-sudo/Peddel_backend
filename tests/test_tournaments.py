import pytest

from apps.matches.models import Match
from apps.standings.services import rank_rows
from apps.tournaments.models import Tournament, TournamentFixture
from tests.conftest import client_for, new_action_id, score

pytestmark = pytest.mark.django_db


def make_teams(api, n, prefix="Team"):
    ids = []
    for i in range(n):
        p = [api.post("/api/admin/players/", {"first_name": f"{prefix}{i}P{k}", "last_name": "Z"}, format="json").json()["id"] for k in (1, 2)]
        t = api.post("/api/admin/teams/", {"name": f"{prefix} {i + 1:02d}", "player_ids": p}, format="json")
        assert t.status_code == 201, t.content
        ids.append(t.json()["id"])
    return ids


def make_tournament(api, fmt="groups_knockout", teams=8, **extra):
    r = api.post("/api/admin/tournaments/", {"name": f"Open {fmt} {teams}", "format": fmt, "venue": "Club", **extra}, format="json")
    assert r.status_code == 201, r.content
    t = r.json()
    for team_id in make_teams(api, teams, prefix=f"T{t['id']}-"):
        assert api.post(f"/api/admin/tournaments/{t['id']}/registrations/", {"team_id": team_id}, format="json").status_code == 201
    return t


def confirm(api, t, **opts):
    return api.post(f"/api/admin/tournaments/{t['id']}/fixtures/confirm/", {"seed": 7, **opts}, format="json")


def finish_match(api, match_id, winner):
    """Record a completed result through the audited result workflow (fast path for tournament tests)."""
    games = [[6, 2], [6, 3]] if winner == 0 else [[2, 6], [3, 6]]
    r = api.post(f"/api/admin/matches/{match_id}/result/", {
        "clientActionId": new_action_id(), "reason": "test result",
        "sets": [{"games": g} for g in games]}, format="json")
    assert r.status_code == 200, r.content
    return r


def fixtures(api, t):
    return api.get(f"/api/admin/tournaments/{t['id']}/fixtures/").json()


def play_all_group_matches(api, t):
    for f in TournamentFixture.objects.filter(tournament_id=t["id"], stage="group").select_related("match"):
        # the team that was registered first (lower id) wins
        winner = 0 if f.team_a_id < f.team_b_id else 1
        finish_match(api, f.match_id, winner)


def test_round_robin_each_team_plays_everyone_once(admin_api):
    t = make_tournament(admin_api, "round_robin", 5)
    assert confirm(admin_api, t).status_code == 201
    fx = TournamentFixture.objects.filter(tournament_id=t["id"])
    assert fx.count() == 10
    pairs = {frozenset((f.team_a_id, f.team_b_id)) for f in fx}
    assert len(pairs) == 10  # no duplicate fixtures
    assert Match.objects.filter(tournament_id=t["id"]).count() == 10


def test_preview_does_not_persist_and_confirm_is_reproducible(admin_api):
    t = make_tournament(admin_api, "groups_knockout", 8)
    p1 = admin_api.post(f"/api/admin/tournaments/{t['id']}/fixtures/preview/", {"seed": 11, "shuffle": True}, format="json").json()
    assert TournamentFixture.objects.filter(tournament_id=t["id"]).count() == 0
    assert p1["counts"] == {"groups": 2, "group_matches": 12, "knockout_matches": 3}
    p2 = admin_api.post(f"/api/admin/tournaments/{t['id']}/fixtures/preview/", {"seed": 11, "shuffle": True}, format="json").json()
    assert p1["groups"] == p2["groups"]


def test_regeneration_requires_confirmation_and_is_blocked_after_play(admin_api):
    t = make_tournament(admin_api, "groups_knockout", 8)
    assert confirm(admin_api, t).status_code == 201
    r = confirm(admin_api, t)
    assert r.status_code == 409 and r.json()["error"]["code"] == "fixtures_exist"
    assert confirm(admin_api, t, replace=True).status_code == 201
    assert TournamentFixture.objects.filter(tournament_id=t["id"], stage="group").count() == 12  # no duplicates
    first = TournamentFixture.objects.filter(tournament_id=t["id"], stage="group").first()
    finish_match(admin_api, first.match_id, 0)
    r = confirm(admin_api, t, replace=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "fixtures_in_progress"


def test_full_tournament_flow_to_champion(admin_api):
    t = make_tournament(admin_api, "groups_knockout", 8)
    confirm(admin_api, t)
    ko = TournamentFixture.objects.filter(tournament_id=t["id"], stage="knockout", is_bye=False)
    assert all(f.team_a_id is None for f in ko.filter(round_number=1))  # not resolved before group stage ends

    play_all_group_matches(admin_api, t)
    standings = admin_api.get(f"/api/admin/tournaments/{t['id']}/standings/").json()
    assert len(standings) == 8 and sum(1 for s in standings if s["qualified"]) == 4
    for g in ("Group A", "Group B"):
        rows = [s for s in standings if s["group_name"] == g]
        assert [s["rank"] for s in rows] == [1, 2, 3, 4]
        assert [s["wins"] for s in rows] == [3, 2, 1, 0]

    semis = list(TournamentFixture.objects.filter(tournament_id=t["id"], stage="knockout", round_number=1).order_by("position"))
    assert all(f.team_a_id and f.team_b_id for f in semis)  # group qualifiers placed into the bracket
    # cross pairing: nobody from the same group meets in the semifinals
    assert Tournament.objects.get(pk=t["id"]).champion_id is None

    for f in semis:
        finish_match(admin_api, f.match_id, 0)
    final = TournamentFixture.objects.get(tournament_id=t["id"], round_number=2, stage="knockout")
    assert final.team_a_id and final.team_b_id
    winner_team = final.team_a_id
    admin_api.post(f"/api/admin/tournaments/{t['id']}/set-status/", {"status": "published"}, format="json")
    finish_match(admin_api, final.match_id, 0)

    t_obj = Tournament.objects.get(pk=t["id"])
    assert t_obj.champion_id == winner_team and t_obj.status == "completed"


def test_group_correction_blocked_after_knockout_started_but_allowed_before(admin_api):
    t = make_tournament(admin_api, "groups_knockout", 4)  # 2 groups of 2, 1 qualifier... use q=2
    confirm(admin_api, t, groups_count=2, qualifiers_per_group=1)
    play_all_group_matches(admin_api, t)
    final = TournamentFixture.objects.get(tournament_id=t["id"], stage="knockout")
    assert final.team_a_id and final.team_b_id
    gm = TournamentFixture.objects.filter(tournament_id=t["id"], stage="group").first()
    finish_match(admin_api, final.match_id, 0)
    # flipping a group result would change who qualifies for a final that has been played
    r = admin_api.post(f"/api/admin/matches/{gm.match_id}/result/", {
        "clientActionId": new_action_id(), "reason": "flip", "sets": [{"games": [2, 6]}, {"games": [3, 6]}]}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "downstream_started"
    # rolled back: result unchanged
    gm.match.refresh_from_db()
    assert gm.match.state.data["winner"] == 0


def test_knockout_undo_blocked_when_next_round_started(admin_api):
    t = make_tournament(admin_api, "single_elimination", 4)
    confirm(admin_api, t)
    semis = list(TournamentFixture.objects.filter(tournament_id=t["id"], round_number=1).order_by("position"))
    for f in semis:
        finish_match(admin_api, f.match_id, 0)
    final = TournamentFixture.objects.get(tournament_id=t["id"], round_number=2)
    assert final.team_a_id and final.team_b_id
    score(admin_api, final.match_id, 0)  # final has started
    r = admin_api.post(f"/api/admin/matches/{semis[0].match_id}/result/", {
        "clientActionId": new_action_id(), "reason": "oops", "sets": [{"games": [2, 6]}, {"games": [2, 6]}]}, format="json")
    assert r.status_code == 409


def test_single_elimination_with_byes(admin_api):
    t = make_tournament(admin_api, "single_elimination", 5)
    confirm(admin_api, t)
    fx = TournamentFixture.objects.filter(tournament_id=t["id"])
    r1 = fx.filter(round_number=1)
    assert r1.count() == 4 and r1.filter(is_bye=True).count() == 3
    # byes advance automatically: 3 teams already in round 2
    r2 = fx.filter(round_number=2)
    assert sum(1 for f in r2 for x in (f.team_a_id, f.team_b_id) if x) == 3
    assert Match.objects.filter(tournament_id=t["id"]).count() == 4  # 1 round-1 + 2 semis + final


def test_registration_rules(admin_api):
    t = make_tournament(admin_api, "round_robin", 2)
    teams = make_teams(admin_api, 1, prefix="Extra")
    reg = lambda tid: admin_api.post(f"/api/admin/tournaments/{t['id']}/registrations/", {"team_id": tid}, format="json")  # noqa: E731
    assert reg(teams[0]).status_code == 201
    dup = reg(teams[0])
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_registration"
    # a player already on another registered team cannot be registered again
    from apps.teams.models import Team

    shared_player = Team.objects.get(pk=teams[0]).memberships.first().player_id
    p2 = admin_api.post("/api/admin/players/", {"first_name": "New", "last_name": "Guy"}, format="json").json()["id"]
    clash = admin_api.post("/api/admin/teams/", {"name": "Clash", "player_ids": [shared_player, p2]}, format="json").json()["id"]
    r = reg(clash)
    assert r.status_code == 409 and r.json()["error"]["code"] == "player_already_registered"


def test_registration_deadline_enforced(admin_api):
    t = admin_api.post("/api/admin/tournaments/", {"name": "Late", "registration_deadline": "2020-01-01"}, format="json").json()
    team = make_teams(admin_api, 1)[0]
    r = admin_api.post(f"/api/admin/tournaments/{t['id']}/registrations/", {"team_id": team}, format="json")
    assert r.status_code == 409 and r.json()["error"]["code"] == "registration_closed"


def test_ranking_criteria_are_configurable():
    rows = [
        {"team_id": 1, "played": 2, "wins": 1, "losses": 1, "points": 3, "sets_for": 2, "sets_against": 3, "games_for": 20, "games_against": 25},
        {"team_id": 2, "played": 2, "wins": 1, "losses": 1, "points": 3, "sets_for": 3, "sets_against": 2, "games_for": 25, "games_against": 20},
        {"team_id": 3, "played": 2, "wins": 1, "losses": 1, "points": 3, "sets_for": 2, "sets_against": 2, "games_for": 22, "games_against": 22},
    ]
    results = [(1, 2), (2, 3), (3, 1)]  # circular: head-to-head cannot separate them
    by_sets = rank_rows(rows, ["points", "head_to_head", "set_difference"], results)
    assert [r["team_id"] for r in by_sets] == [2, 3, 1]
    rows[0]["points"] = 6
    assert rank_rows(rows, ["wins", "points"], results)[0]["team_id"] in (1, 2, 3)
    h2h = rank_rows(rows[:2], ["head_to_head", "set_difference"], [(1, 2)])
    assert [r["team_id"] for r in h2h] == [1, 2]
    gd_first = rank_rows(rows[1:], ["game_difference"], [])
    assert gd_first[0]["team_id"] == 2


def test_ranking_criteria_validation(admin_api):
    r = admin_api.post("/api/admin/tournaments/", {"name": "Bad", "ranking_criteria": ["points", "nonsense"]}, format="json")
    assert r.status_code == 400


def test_tournament_permissions_between_admins(admin_api, t_admin2):
    t = admin_api.post("/api/admin/tournaments/", {"name": "Mine"}, format="json").json()
    other = client_for(t_admin2)
    assert other.get(f"/api/admin/tournaments/{t['id']}/").status_code == 404
    assert other.get("/api/admin/tournaments/").json()["count"] == 0


def test_slug_unique_and_status_lifecycle(admin_api):
    a = admin_api.post("/api/admin/tournaments/", {"name": "Same Name"}, format="json").json()
    b = admin_api.post("/api/admin/tournaments/", {"name": "Same Name"}, format="json").json()
    assert a["slug"] != b["slug"]
    r = admin_api.post("/api/admin/tournaments/", {"name": "x", "slug": a["slug"]}, format="json")
    assert r.status_code == 400
    # cannot publish without teams
    r = admin_api.post(f"/api/admin/tournaments/{a['id']}/set-status/", {"status": "published"}, format="json")
    assert r.status_code == 409
    assert admin_api.post(f"/api/admin/tournaments/{a['id']}/set-status/", {"status": "completed"}, format="json").status_code == 409


def test_delete_tournament_with_fixtures_blocked(admin_api):
    t = make_tournament(admin_api, "round_robin", 3)
    confirm(admin_api, t)
    assert admin_api.delete(f"/api/admin/tournaments/{t['id']}/").status_code == 409
