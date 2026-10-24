import json

import pytest
from django.urls import get_resolver
from rest_framework.test import APIClient

from apps.tournaments.models import TournamentFixture
from tests.conftest import client_for, new_action_id, play_points, score, win_set_6_0
from tests.test_tournaments import confirm, finish_match, make_tournament, play_all_group_matches

pytestmark = pytest.mark.django_db


def publish(api, t):
    r = api.post(f"/api/admin/tournaments/{t['id']}/set-status/", {"status": "published"}, format="json")
    assert r.status_code == 200, r.content


@pytest.fixture
def anon():
    return APIClient()


def collect_urls(patterns, prefix=""):
    for p in patterns:
        if hasattr(p, "url_patterns"):
            yield from collect_urls(p.url_patterns, prefix + str(p.pattern))
        else:
            yield prefix + str(p.pattern)


# ------------------------------------------------------------------ visibility
def test_draft_and_private_tournaments_never_leak(admin_api, anon):
    draft = make_tournament(admin_api, "round_robin", 3)
    confirm(admin_api, draft)
    private = make_tournament(admin_api, "round_robin", 3, visibility="private")
    confirm(admin_api, private)
    publish(admin_api, private)

    assert anon.get("/api/public/tournaments/").json()["count"] == 0
    for t in (draft, private):
        assert anon.get(f"/api/public/tournaments/{t['slug']}/").status_code == 404
        from apps.matches.models import Match

        for m in Match.objects.filter(tournament_id=t["id"]):
            assert anon.get(f"/api/public/matches/{m.public_id}/").status_code == 404


def test_published_tournament_public_payload_is_safe_and_read_only(admin_api, anon):
    t = make_tournament(admin_api, "groups_knockout", 8)
    confirm(admin_api, t)
    publish(admin_api, t)
    lst = anon.get("/api/public/tournaments/").json()
    assert lst["count"] == 1 and lst["results"][0]["slug"] == t["slug"] and lst["results"][0]["phase"] == "upcoming"
    detail = anon.get(f"/api/public/tournaments/{t['slug']}/")
    assert detail.status_code == 200
    raw = detail.content.decode().lower()
    for forbidden in ("pdf", "email", "phone", "umpire", "created_by", "owner"):
        assert forbidden not in raw
    assert len(detail.json()["teams"]) == 8
    # writes are rejected
    assert anon.post("/api/public/tournaments/", {}, format="json").status_code in (401, 403, 405)
    assert anon.delete(f"/api/public/tournaments/{t['slug']}/").status_code in (401, 403, 405)


def test_public_search_and_phase_filter(admin_api, anon):
    a = make_tournament(admin_api, "round_robin", 3)
    publish(admin_api, a)
    assert anon.get("/api/public/tournaments/?search=zzzz").json()["count"] == 0
    assert anon.get("/api/public/tournaments/?phase=completed").json()["count"] == 0
    assert anon.get("/api/public/tournaments/?phase=upcoming").json()["count"] == 1


def test_public_match_live_score_and_timeline(admin_api, anon, make_match):
    m = make_match()
    assert anon.get(f"/api/public/matches/{m['publicId']}/").status_code == 200  # shareable friendly
    play_points(admin_api, m["id"], [0, 0, 1])
    d = anon.get(f"/api/public/matches/{m['publicId']}/").json()
    assert d["revision"] == 3 and d["state"]["points"] == [2, 1] and len(d["timeline"]) == 3
    raw = json.dumps(d).lower()
    assert "umpire" not in raw and "pdf" not in raw and "email" not in raw
    assert anon.get("/api/public/matches/NOSUCHID01/").status_code == 404


def test_private_friendly_and_draft_matches_hidden(admin_api, anon, make_match):
    m = make_match(is_public=False)
    assert anon.get(f"/api/public/matches/{m['publicId']}/").status_code == 404
    d = admin_api.post("/api/admin/matches/", {"name": "No teams yet"}, format="json").json()
    assert d["status"] == "draft"
    assert anon.get(f"/api/public/matches/{d['publicId']}/").status_code == 404


def test_unpublishing_hides_tournament_matches(admin_api, anon):
    t = make_tournament(admin_api, "round_robin", 3)
    confirm(admin_api, t)
    publish(admin_api, t)
    from apps.matches.models import Match

    mid = Match.objects.filter(tournament_id=t["id"]).first().public_id
    assert anon.get(f"/api/public/matches/{mid}/").status_code == 200
    admin_api.post(f"/api/admin/tournaments/{t['id']}/set-status/", {"status": "draft"}, format="json")
    assert anon.get(f"/api/public/matches/{mid}/").status_code == 404


def test_images_of_private_tournaments_not_served(admin_api, anon, settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.MEDIA_ROOT = tmp_path
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(buf, "PNG")
    t = admin_api.post("/api/admin/tournaments/", {"name": "Img", "logo": SimpleUploadedFile("l.png", buf.getvalue(), "image/png")},
                       format="multipart")
    assert t.status_code == 201, t.content
    url = t.json()["logo_url"]
    assert anon.get(url).status_code == 404  # draft
    from apps.tournaments.models import Tournament

    Tournament.objects.filter(pk=t.json()["id"]).update(status="published")
    assert anon.get(url).status_code == 200
    Tournament.objects.filter(pk=t.json()["id"]).update(visibility="private")
    assert anon.get(url).status_code == 404
    bad = admin_api.post("/api/admin/tournaments/", {"name": "Bad", "logo": SimpleUploadedFile("l.txt", b"nope", "text/plain")}, format="multipart")
    assert bad.status_code == 400


# ------------------------------------------------------------------ PDFs
def test_no_pdf_route_exists_outside_admin_reports():
    urls = [u for u in collect_urls(get_resolver().url_patterns) if "pdf" in u.lower()]
    assert urls, "expected admin PDF routes"
    assert all(u.startswith("api/admin/reports/") for u in urls), urls


@pytest.mark.parametrize("path", [
    "/api/public/reports/matches/1/pdf/", "/api/public/matches/ABC/pdf/", "/api/public/tournaments/x/pdf/",
    "/api/public/tournaments/x/results.pdf", "/api/admin/reports/matches/1/pdf/", "/api/admin/reports/tournaments/1/pdf/",
    "/api/admin/reports/tournaments/1/standings/pdf/", "/api/admin/reports/tournaments/1/fixtures/pdf/",
])
def test_anonymous_cannot_download_any_pdf(anon, path):
    r = anon.get(path)
    assert r.status_code in (401, 404)
    assert r.get("Content-Type", "") != "application/pdf"


def test_match_pdf_flow_and_permissions(admin_api, umpire_api, root_api, t_admin2, anon, make_match):
    m = make_match({"bestOf": 1})
    url = f"/api/admin/reports/matches/{m['id']}/pdf/"
    # not completed -> refused
    r = admin_api.get(url)
    assert r.status_code == 409 and r.json()["error"]["code"] == "result_not_final"
    win_set_6_0(admin_api, m["id"], 0)
    r = admin_api.get(url)
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf" and r.content[:5] == b"%PDF-"
    assert f"match-result-MATCH{m['id']}.pdf" in r["Content-Disposition"]
    assert "no-store" in r["Cache-Control"]
    assert root_api.get(url).status_code == 200
    # the assigned umpire scored the match but must NOT receive report access
    assert umpire_api.get(url).status_code == 403
    assert client_for(t_admin2).get(url).status_code == 403  # admin of a different scope
    assert anon.get(url).status_code == 401


def test_match_pdf_content(admin_api, make_match):
    from io import BytesIO

    from reportlab.pdfgen import canvas  # noqa: F401 - ensures reportlab present

    from apps.matches.models import Match
    from apps.reports.pdf import build_match_pdf

    m = make_match({"bestOf": 1})
    win_set_6_0(admin_api, m["id"], 0)
    data = build_match_pdf(Match.objects.get(pk=m["id"]))
    assert data.startswith(b"%PDF") and len(data) > 1500
    assert BytesIO(data).read(5) == b"%PDF-"


def test_tournament_pdf_progress_vs_final_and_permissions(admin_api, umpire_api, t_admin2, anon):
    t = make_tournament(admin_api, "groups_knockout", 8)
    confirm(admin_api, t)
    url = f"/api/admin/reports/tournaments/{t['id']}/pdf/"
    r = admin_api.get(url)
    assert r.status_code == 200 and f"tournament-progress-{t['slug']}.pdf" in r["Content-Disposition"]
    play_all_group_matches(admin_api, t)
    for f in TournamentFixture.objects.filter(tournament_id=t["id"], round_number=1, stage="knockout"):
        finish_match(admin_api, f.match_id, 0)
    final = TournamentFixture.objects.get(tournament_id=t["id"], stage="knockout", round_number=2)
    finish_match(admin_api, final.match_id, 0)
    admin_api.post(f"/api/admin/tournaments/{t['id']}/set-status/", {"status": "published"}, format="json")  # no-op on completed
    r = admin_api.get(url)
    assert f"tournament-results-{t['slug']}.pdf" in r["Content-Disposition"]
    for kind in ("standings", "fixtures"):
        assert admin_api.get(f"/api/admin/reports/tournaments/{t['id']}/{kind}/pdf/").status_code == 200
    assert umpire_api.get(url).status_code == 403
    assert client_for(t_admin2).get(url).status_code == 403
    assert anon.get(url).status_code == 401


def test_pdf_pages_labelled_not_final_when_in_progress(admin_api, monkeypatch):
    from reportlab import rl_config

    from apps.reports.pdf import build_tournament_pdf
    from apps.tournaments.models import Tournament

    monkeypatch.setattr(rl_config, "pageCompression", 0)  # make text streams greppable
    t = make_tournament(admin_api, "round_robin", 4)
    confirm(admin_api, t)
    data = build_tournament_pdf(Tournament.objects.get(pk=t["id"]))
    assert b"PROGRESS REPORT" in data and b"Progress Report" in data
    assert b"Final Results" not in data and b"Champion:" not in data
    assert b"provisional" in data


def test_umpire_scope_limits(umpire_api, umpire2, admin_api, make_match):
    m = make_match()
    other = make_match(assign_umpire=False)
    assert score(umpire_api, m["id"], 0).status_code == 200
    assert score(umpire_api, other["id"], 0).status_code == 404  # not visible at all
    ids = [x["id"] for x in umpire_api.get("/api/admin/matches/").json()["results"]]
    assert ids == [m["id"]]
    assert umpire_api.post("/api/admin/matches/", {"name": "x"}, format="json").status_code == 403
    assert umpire_api.get("/api/admin/players/").status_code == 403
    assert umpire_api.get("/api/admin/dashboard/").status_code == 403
    assert client_for(umpire2).get(f"/api/admin/matches/{m['id']}/").status_code == 404


def test_unauthenticated_admin_endpoints_rejected(anon):
    for path in ("/api/admin/matches/", "/api/admin/tournaments/", "/api/admin/players/", "/api/admin/dashboard/",
                 "/api/admin/users/", "/api/admin/audit/"):
        assert anon.get(path).status_code == 401


def test_login_and_me(super_admin, anon):
    r = anon.post("/api/auth/login/", {"username": "root", "password": "Passw0rd!x"}, format="json")
    assert r.status_code == 200 and "access" in r.json()
    me = anon.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {r.json()['access']}")
    assert me.status_code == 200 and me.json()["role"] == "super_admin" and "password" not in me.json()
    assert anon.post("/api/auth/login/", {"username": "root", "password": "wrong"}, format="json").status_code == 401


def test_dashboard_counts(admin_api, make_match):
    make_match()
    live = make_match()
    score(admin_api, live["id"], 0)
    d = admin_api.get("/api/admin/dashboard/").json()
    assert d["totals"]["upcomingMatches"] == 1 and d["totals"]["liveMatches"] == 1 and d["totals"]["players"] == 8


def test_health_and_share_pages(anon, admin_api, make_match):
    assert anon.get("/api/health/").json()["status"] == "ok"
    m = make_match()
    r = anon.get(f"/share/m/{m['publicId']}/")
    assert r.status_code == 200 and b"og:title" in r.content
    assert new_action_id()


def test_openapi_schema_and_docs_are_served(anon):
    r = anon.get("/api/schema/")
    assert r.status_code == 200, r.content[:300]
    body = r.content.decode()
    assert "/api/admin/matches/" in body and "/api/public/tournaments/" in body
    assert anon.get("/api/docs/").status_code == 200
    # the public part of the API documents no PDF operation at all
    import re

    public_paths = re.findall(r"^  (/api/public/[^:]*):", body, flags=re.M)
    assert public_paths and not any("pdf" in p for p in public_paths)
