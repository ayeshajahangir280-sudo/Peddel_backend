"""True multi-threaded concurrency tests. They need row-level locking, so they run on PostgreSQL only
(set DATABASE_URL to a PostgreSQL database; they are skipped on SQLite)."""
import threading

import pytest
from django.conf import settings
from django.db import connection

from apps.matches.models import Match
from apps.scoring.models import ScoreEvent
from tests.conftest import client_for, new_action_id

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif("postgresql" not in settings.DATABASES["default"]["ENGINE"], reason="needs PostgreSQL row locks"),
]


def run_parallel(n, fn):
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as e:  # pragma: no cover - surfaced by assertions
            results[i] = e
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def post_point(user, match_id, team, cid, expected=None):
    body = {"action": "point", "team": team, "clientActionId": cid}
    if expected is not None:
        body["expectedRevision"] = expected
    return client_for(user).post(f"/api/admin/matches/{match_id}/score/", body, format="json")


def test_identical_concurrent_requests_award_one_point(make_match, t_admin):
    m = make_match()
    cid = new_action_id()
    res = run_parallel(12, lambda i: post_point(t_admin, m["id"], 0, cid))
    assert all(not isinstance(r, Exception) and r.status_code == 200 for r in res), res
    assert {r.json()["revision"] for r in res} == {1}
    assert ScoreEvent.objects.filter(match_id=m["id"]).count() == 1
    assert Match.objects.get(pk=m["id"]).state.data["points"] == [1, 0]


def test_distinct_concurrent_points_are_all_applied_without_lost_updates(make_match, t_admin):
    m = make_match()
    res = run_parallel(10, lambda i: post_point(t_admin, m["id"], 0, new_action_id()))
    assert all(r.status_code == 200 for r in res), [getattr(r, "content", r) for r in res]
    revisions = sorted(r.json()["revision"] for r in res)
    assert revisions == list(range(1, 11))  # strictly monotonic, no duplicates, none lost
    state = Match.objects.get(pk=m["id"]).state.data
    # 10 points for team 0 = 2 games and 2 points (4 per game)
    assert state["games"] == [2, 0] and state["points"] == [2, 0]
    assert ScoreEvent.objects.filter(match_id=m["id"]).count() == 10


def test_concurrent_writers_with_same_expected_revision_only_one_wins(make_match, t_admin):
    m = make_match()
    res = run_parallel(8, lambda i: post_point(t_admin, m["id"], i % 2, new_action_id(), expected=0))
    codes = sorted(r.status_code for r in res)
    assert codes == [200] + [409] * 7, codes
    assert all(r.json()["error"]["code"] == "stale_revision" for r in res if r.status_code == 409)
    assert ScoreEvent.objects.filter(match_id=m["id"]).count() == 1
    assert Match.objects.get(pk=m["id"]).state.revision == 1


def test_concurrent_undo_with_same_action_id_reverses_once(make_match, t_admin):
    m = make_match()
    for _ in range(3):
        post_point(t_admin, m["id"], 0, new_action_id())
    cid = new_action_id()
    res = run_parallel(6, lambda i: client_for(t_admin).post(
        f"/api/admin/matches/{m['id']}/score/", {"action": "undo", "clientActionId": cid}, format="json"))
    assert all(r.status_code == 200 for r in res)
    assert Match.objects.get(pk=m["id"]).state.data["points"] == [2, 0]
    assert ScoreEvent.objects.filter(match_id=m["id"], event_type="undo").count() == 1
