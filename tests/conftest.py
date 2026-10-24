import itertools
import uuid

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.matches.models import Court


@pytest.fixture
def api():
    return APIClient()


def make_user(username, role):
    return User.objects.create_user(username=username, email=f"{username}@example.com", password="Passw0rd!x", role=role)


@pytest.fixture
def super_admin(db):
    return make_user("root", Role.SUPER_ADMIN)


@pytest.fixture
def t_admin(db):
    return make_user("tadmin", Role.TOURNAMENT_ADMIN)


@pytest.fixture
def t_admin2(db):
    return make_user("tadmin2", Role.TOURNAMENT_ADMIN)


@pytest.fixture
def umpire(db):
    return make_user("ump1", Role.UMPIRE)


@pytest.fixture
def umpire2(db):
    return make_user("ump2", Role.UMPIRE)


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def admin_api(t_admin):
    return client_for(t_admin)


@pytest.fixture
def root_api(super_admin):
    return client_for(super_admin)


@pytest.fixture
def umpire_api(umpire):
    return client_for(umpire)


@pytest.fixture
def court(db):
    return Court.objects.create(name="Court 1")


_counter = itertools.count(1)


def new_action_id():
    return f"act-{uuid.uuid4().hex[:16]}"


def side(name, a, b):
    return {"name": name, "players": [{"first_name": a, "last_name": "X"}, {"first_name": b, "last_name": "Y"}]}


@pytest.fixture
def make_match(admin_api, umpire, court):
    def _make(config=None, assign_umpire=True, **extra):
        n = next(_counter)
        payload = {
            "name": f"Friendly {n}",
            "team_a": side(f"Alpha{n}", f"A1_{n}", f"A2_{n}"),
            "team_b": side(f"Bravo{n}", f"B1_{n}", f"B2_{n}"),
            "court": court.pk,
            "config": config or {},
            **extra,
        }
        if assign_umpire:
            payload["umpire"] = umpire.pk
        r = admin_api.post("/api/admin/matches/", payload, format="json")
        assert r.status_code == 201, r.content
        return r.json()

    return _make


def score(api, match_id, team=None, action="point", **kw):
    body = {"action": action, "clientActionId": kw.pop("cid", new_action_id()), **kw}
    if team is not None:
        body["team"] = team
    return api.post(f"/api/admin/matches/{match_id}/score/", body, format="json")


def play_points(api, match_id, teams):
    last = None
    for t in teams:
        last = score(api, match_id, t)
        assert last.status_code == 200, last.content
    return last.json() if last else None


def win_game(api, match_id, team):
    return play_points(api, match_id, [team] * 4)


def win_set_6_0(api, match_id, team):
    for _ in range(6):
        win_game(api, match_id, team)
