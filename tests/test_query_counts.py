"""Guards against N+1 queries on the hot / large endpoints: query count must not grow with data size."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from tests.conftest import new_action_id, score
from tests.test_tournaments import confirm, make_tournament

pytestmark = pytest.mark.django_db


def count(fn):
    with CaptureQueriesContext(connection) as ctx:
        r = fn()
    return len(ctx), r


def test_scoring_action_uses_a_small_constant_number_of_queries(admin_api, make_match):
    m = make_match()
    score(admin_api, m["id"], 0)  # warm-up (first point flips status etc.)
    n, r = count(lambda: score(admin_api, m["id"], 1))
    assert r.status_code == 200
    assert n <= 30, f"scoring action used {n} queries"


def test_public_tournament_detail_query_count_is_independent_of_size(admin_api):
    small = make_tournament(admin_api, "round_robin", 4)
    confirm(admin_api, small)
    big = make_tournament(admin_api, "round_robin", 8)
    confirm(admin_api, big)
    for t in (small, big):
        admin_api.post(f"/api/admin/tournaments/{t['id']}/set-status/", {"status": "published"}, format="json")
    anon = APIClient()
    n_small, r1 = count(lambda: anon.get(f"/api/public/tournaments/{small['slug']}/"))
    n_big, r2 = count(lambda: anon.get(f"/api/public/tournaments/{big['slug']}/"))
    assert r1.status_code == r2.status_code == 200
    assert len(r1.json()["groupFixtures"]) == 6 and len(r2.json()["groupFixtures"]) == 28
    assert n_big <= n_small + 2, f"{n_small} queries (4 teams) vs {n_big} queries (8 teams)"


def test_admin_match_list_query_count_is_flat(admin_api, make_match):
    for _ in range(2):
        make_match()
    n_few, _ = count(lambda: admin_api.get("/api/admin/matches/"))
    for _ in range(8):
        make_match()
    n_many, r = count(lambda: admin_api.get("/api/admin/matches/"))
    assert r.json()["count"] == 10
    assert n_many <= n_few + 1, f"{n_few} -> {n_many}"


def test_admin_fixture_list_and_standings_flat(admin_api):
    a = make_tournament(admin_api, "round_robin", 4)
    confirm(admin_api, a)
    b = make_tournament(admin_api, "round_robin", 8)
    confirm(admin_api, b)
    na, _ = count(lambda: admin_api.get(f"/api/admin/tournaments/{a['id']}/fixtures/"))
    nb, _ = count(lambda: admin_api.get(f"/api/admin/tournaments/{b['id']}/fixtures/"))
    assert nb <= na + 1, f"{na} vs {nb}"
    na, _ = count(lambda: admin_api.get(f"/api/admin/tournaments/{a['id']}/standings/"))
    nb, _ = count(lambda: admin_api.get(f"/api/admin/tournaments/{b['id']}/standings/"))
    assert nb <= na + 1


def test_team_and_player_lists_do_not_n_plus_one(admin_api):
    from tests.test_tournaments import make_teams

    make_teams(admin_api, 2, "A")
    n_few, _ = count(lambda: admin_api.get("/api/admin/teams/"))
    make_teams(admin_api, 10, "B")
    n_many, _ = count(lambda: admin_api.get("/api/admin/teams/"))
    assert n_many <= n_few + 1
    assert new_action_id()
