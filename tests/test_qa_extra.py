"""QA regression tests: PDF content, tournament edge cases, permissions, persistence."""
from io import BytesIO

import pytest
from pypdf import PdfReader
from rest_framework.test import APIClient

from apps.matches.models import Match
from apps.tournaments.models import TournamentFixture
from tests.conftest import client_for, new_action_id, score
from tests.test_tournaments import confirm, finish_match, make_teams, make_tournament, play_all_group_matches

pytestmark = pytest.mark.django_db


def pdf_text(data):
    r = PdfReader(BytesIO(data))
    return len(r.pages), "\n".join(p.extract_text() for p in r.pages)


def result(api, match_id, sets, reason="qa"):
    r = api.post(f"/api/admin/matches/{match_id}/result/", {"clientActionId": new_action_id(), "reason": reason, "sets": sets}, format="json")
    return r


# ----------------------------------------------------------------------------- PDF content
def test_match_pdf_contains_teams_players_winner_scores_and_tiebreak(admin_api, make_match):
    m = make_match({"bestOf": 3})
    assert result(admin_api, m["id"], [{"games": [7, 6], "tiebreak": [9, 7]}, {"games": [3, 6]}, {"games": [6, 2]}]).status_code == 200
    r = admin_api.get(f"/api/admin/reports/matches/{m['id']}/pdf/")
    pages, text = pdf_text(r.content)
    assert pages == 1
    for needle in (m["teams"][0]["name"], m["teams"][1]["name"], f"MATCH{m['id']}", "Winner:"):
        assert needle in text, needle
    for team in m["teams"]:
        for p in team["players"]:
            assert p["name"] in text, p["name"]  # all four player names
    assert f"Winner: {m['teams'][0]['name']}" in text
    assert "7-6" in text and "9-7" in text  # set score + tie-break score
    assert "3-6" in text and "6-2" in text
    assert "Completed - awaiting verification" in text


def test_match_pdf_reflects_a_correction_and_verification(admin_api, make_match):
    m = make_match({"bestOf": 1})
    result(admin_api, m["id"], [{"games": [6, 1]}])
    _, before = pdf_text(admin_api.get(f"/api/admin/reports/matches/{m['id']}/pdf/").content)
    assert f"Winner: {m['teams'][0]['name']}" in before
    assert result(admin_api, m["id"], [{"games": [4, 6]}], "scorer error").status_code == 200
    admin_api.post(f"/api/admin/matches/{m['id']}/verify/")
    _, after = pdf_text(admin_api.get(f"/api/admin/reports/matches/{m['id']}/pdf/").content)
    assert f"Winner: {m['teams'][1]['name']}" in after and "4-6" in after
    assert "Official (verified)" in after


def test_super_tiebreak_set_is_shown_in_pdf(admin_api, make_match):
    m = make_match({"bestOf": 3, "superTiebreak": True})
    result(admin_api, m["id"], [{"games": [6, 4]}, {"games": [3, 6]}, {"games": [1, 0], "tiebreak": [10, 8]}])
    _, text = pdf_text(admin_api.get(f"/api/admin/reports/matches/{m['id']}/pdf/").content)
    assert "Super tie-break" in text and "10-8" in text


def test_tournament_pdf_progress_then_final_content_and_pagination(admin_api):
    t = make_tournament(admin_api, "groups_knockout", 16)
    confirm(admin_api, t, groups_count=2, qualifiers_per_group=2)
    url = f"/api/admin/reports/tournaments/{t['id']}/pdf/"
    pages, text = pdf_text(admin_api.get(url).content)
    assert "Progress Report" in text and "PROGRESS REPORT" in text and "Final Results" not in text
    assert pages >= 3, f"a 16-team report should paginate, got {pages} page(s)"
    assert "Group A" in text and "Group B" in text and "Participating teams" in text
    play_all_group_matches(admin_api, t)
    for f in TournamentFixture.objects.filter(tournament_id=t["id"], stage="knockout", is_bye=False).order_by("round_number", "position"):
        f.refresh_from_db()
        finish_match(admin_api, f.match_id, 0)
    admin_api.post(f"/api/admin/tournaments/{t['id']}/set-status/", {"status": "published"}, format="json")
    r = admin_api.get(url)
    pages2, text2 = pdf_text(r.content)
    assert "Final Results" in text2 and "PROGRESS REPORT" not in text2
    champion = admin_api.get(f"/api/admin/tournaments/{t['id']}/").json()["champion_name"]
    assert f"Champion: {champion}" in text2
    assert "Champion and final ranking" in text2 and "Place" in text2
    # the long fixtures table spans several pages and repeats its header row on each
    assert text2.count("Status\nResult") >= 2, "fixtures table header should repeat on continuation pages"


def test_long_names_do_not_break_pdf(admin_api):
    long = "X" * 55
    p = [admin_api.post("/api/admin/players/", {"first_name": long, "last_name": long[:50] + str(i)}, format="json").json()["id"] for i in range(4)]
    ta = admin_api.post("/api/admin/teams/", {"name": "Á" * 95, "player_ids": p[:2]}, format="json").json()["id"]
    tb = admin_api.post("/api/admin/teams/", {"name": "Ö & <b>Co</b> " + "Z" * 80, "player_ids": p[2:]}, format="json").json()["id"]
    m = admin_api.post("/api/admin/matches/", {"name": "long names", "team_a": {"id": ta}, "team_b": {"id": tb}, "config": {"bestOf": 1}}, format="json").json()
    result(admin_api, m["id"], [{"games": [6, 0]}])
    r = admin_api.get(f"/api/admin/reports/matches/{m['id']}/pdf/")
    assert r.status_code == 200
    _, text = pdf_text(r.content)
    assert "Á" in text and "&" in text  # unicode and markup characters are escaped, not interpreted


# ----------------------------------------------------------------------------- tournament edge cases
def test_insufficient_teams_are_rejected(admin_api):
    one = make_tournament(admin_api, "round_robin", 1)
    r = admin_api.post(f"/api/admin/tournaments/{one['id']}/fixtures/confirm/", {}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_enough_teams"
    three = make_tournament(admin_api, "groups_knockout", 3)
    r = admin_api.post(f"/api/admin/tournaments/{three['id']}/fixtures/preview/", {"groups_count": 2}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_enough_teams"
    assert TournamentFixture.objects.filter(tournament_id=three["id"]).count() == 0
    se = make_tournament(admin_api, "single_elimination", 1)
    assert admin_api.post(f"/api/admin/tournaments/{se['id']}/fixtures/confirm/", {}, format="json").status_code == 400


def test_invalid_qualifier_count_rejected(admin_api):
    t = make_tournament(admin_api, "groups_knockout", 4)
    r = admin_api.post(f"/api/admin/tournaments/{t['id']}/fixtures/preview/", {"groups_count": 2, "qualifiers_per_group": 3}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_qualifiers"


def test_odd_team_counts_in_groups_and_knockout(admin_api):
    t = make_tournament(admin_api, "groups_knockout", 7)
    plan = admin_api.post(f"/api/admin/tournaments/{t['id']}/fixtures/preview/", {"groups_count": 2, "qualifiers_per_group": 2}, format="json").json()
    sizes = sorted(len(g["teams"]) for g in plan["groups"])
    assert sizes == [3, 4]
    assert plan["counts"]["group_matches"] == 3 + 6  # 3-team group: 3 matches, 4-team group: 6
    confirm(admin_api, t, groups_count=2, qualifiers_per_group=2)
    pairs = {frozenset((f.team_a_id, f.team_b_id)) for f in TournamentFixture.objects.filter(tournament_id=t["id"], stage="group")}
    assert len(pairs) == 9  # no duplicates
    play_all_group_matches(admin_api, t)
    standings = admin_api.get(f"/api/admin/tournaments/{t['id']}/standings/").json()
    assert len(standings) == 7 and sum(s["qualified"] for s in standings) == 4
    semis = TournamentFixture.objects.filter(tournament_id=t["id"], stage="knockout", round_number=1)
    assert all(f.team_a_id and f.team_b_id for f in semis)


def test_five_team_round_robin_everyone_plays_equally(admin_api):
    t = make_tournament(admin_api, "round_robin", 5)
    confirm(admin_api, t)
    from collections import Counter

    c = Counter()
    for f in TournamentFixture.objects.filter(tournament_id=t["id"]):
        c[f.team_a_id] += 1
        c[f.team_b_id] += 1
    assert set(c.values()) == {4} and len(c) == 5  # odd count handled with byes: each team plays the other 4


def test_knockout_waits_until_every_group_match_is_complete(admin_api):
    t = make_tournament(admin_api, "groups_knockout", 8)
    confirm(admin_api, t, groups_count=2, qualifiers_per_group=2)
    fixtures = list(TournamentFixture.objects.filter(tournament_id=t["id"], stage="group").select_related("match"))
    for f in fixtures[:-1]:  # leave exactly one group match unplayed
        finish_match(admin_api, f.match_id, 0 if f.team_a_id < f.team_b_id else 1)
    semis = TournamentFixture.objects.filter(tournament_id=t["id"], stage="knockout", round_number=1)
    unresolved_group = fixtures[-1].group
    # slots fed by the unfinished group stay empty; no one is qualified from it yet
    standings = admin_api.get(f"/api/admin/tournaments/{t['id']}/standings/").json()
    assert not any(s["qualified"] for s in standings if s["group_name"] == unresolved_group.name)
    assert any(f.team_a_id is None or f.team_b_id is None for f in semis)
    last = fixtures[-1]
    finish_match(admin_api, last.match_id, 0 if last.team_a_id < last.team_b_id else 1)
    assert all(f.team_a_id and f.team_b_id for f in TournamentFixture.objects.filter(tournament_id=t["id"], stage="knockout", round_number=1))


def test_tied_standings_are_resolved_by_configured_criteria(admin_api):
    """Three teams beat each other in a circle (1>2, 2>3, 3>1) -> equal points & wins & head-to-head; set/game difference decides."""
    t = make_tournament(admin_api, "round_robin", 3, match_format={"bestOf": 1})
    confirm(admin_api, t)
    fx = {frozenset((f.team_a_id, f.team_b_id)): f for f in TournamentFixture.objects.filter(tournament_id=t["id"]).select_related("match")}
    ids = sorted({x for k in fx for x in k})
    a, b, c = ids

    def play(winner, loser, k):
        f = fx[frozenset((winner, loser))]
        w_idx = 0 if f.team_a_id == winner else 1
        games = [6, k] if w_idx == 0 else [k, 6]
        assert result(admin_api, f.match_id, [{"games": games}]).status_code == 200

    play(a, b, 0)  # a beats b 6-0
    play(b, c, 4)  # b beats c 6-4
    play(c, a, 3)  # c beats a 6-3
    rows = admin_api.get(f"/api/admin/tournaments/{t['id']}/standings/").json()
    assert {r["points"] for r in rows} == {3} and {r["wins"] for r in rows} == {1}
    by_gd = sorted(rows, key=lambda r: r["game_difference"], reverse=True)
    assert [r["team_id"] for r in rows] == [r["team_id"] for r in by_gd]  # ranked by game difference after the tie
    assert [r["rank"] for r in rows] == [1, 2, 3]
    # changing the criteria changes the table (criteria are per tournament)
    admin_api.patch(f"/api/admin/tournaments/{t['id']}/", {"ranking_criteria": ["points", "games_won"]}, format="json")
    admin_api.post(f"/api/admin/tournaments/{t['id']}/recalculate/")
    rows2 = admin_api.get(f"/api/admin/tournaments/{t['id']}/standings/").json()
    assert [r["team_id"] for r in rows2] == [r["team_id"] for r in sorted(rows2, key=lambda r: r["games_for"], reverse=True)]


def test_completed_matches_survive_regeneration_attempts(admin_api):
    t = make_tournament(admin_api, "round_robin", 4)
    confirm(admin_api, t)
    f = TournamentFixture.objects.filter(tournament_id=t["id"]).first()
    finish_match(admin_api, f.match_id, 0)
    for body in ({"replace": True}, {"replace": True, "seed": 3}, {}):
        r = admin_api.post(f"/api/admin/tournaments/{t['id']}/fixtures/confirm/", body, format="json")
        assert r.status_code == 409
    m = Match.objects.get(pk=f.match_id)
    assert m.status == "completed" and m.state.data["completed"]
    assert TournamentFixture.objects.filter(tournament_id=t["id"]).count() == 6


# ----------------------------------------------------------------------------- permissions (direct API)
def test_public_visitors_cannot_write_anything(admin_api, make_match, umpire_api):
    anon = APIClient()
    m = make_match()
    t = make_tournament(admin_api, "round_robin", 2)
    writes = [
        ("post", "/api/admin/matches/"), ("post", f"/api/admin/matches/{m['id']}/score/"), ("post", f"/api/admin/matches/{m['id']}/result/"),
        ("post", "/api/admin/tournaments/"), ("post", f"/api/admin/tournaments/{t['id']}/registrations/"),
        ("post", f"/api/admin/tournaments/{t['id']}/fixtures/confirm/"), ("patch", f"/api/admin/tournaments/{t['id']}/"),
        ("delete", f"/api/admin/tournaments/{t['id']}/"), ("post", "/api/admin/players/"), ("post", "/api/admin/users/"),
        ("get", "/api/admin/players/"), ("get", "/api/admin/audit/"),
    ]
    for method, path in writes:
        assert getattr(anon, method)(path, {}, format="json").status_code == 401, (method, path)
    for method in ("post", "put", "patch", "delete"):
        for path in ("/api/public/tournaments/", f"/api/public/tournaments/{t['slug']}/", f"/api/public/matches/{m['publicId']}/"):
            assert getattr(anon, method)(path, {}, format="json").status_code in (401, 403, 405), (method, path)


def test_umpire_cannot_manage_tournaments_players_or_other_matches(admin_api, umpire_api, make_match):
    t = make_tournament(admin_api, "round_robin", 2)
    other = make_match(assign_umpire=False)
    checks = [
        umpire_api.post("/api/admin/tournaments/", {"name": "hack"}, format="json"),
        umpire_api.get(f"/api/admin/tournaments/{t['id']}/"),
        umpire_api.post(f"/api/admin/tournaments/{t['id']}/fixtures/confirm/", {}, format="json"),
        umpire_api.post("/api/admin/teams/", {"name": "x", "player_ids": [1, 2]}, format="json"),
        umpire_api.get("/api/admin/users/"),
        umpire_api.post(f"/api/admin/matches/{other['id']}/score/", {"action": "point", "team": 0, "clientActionId": new_action_id()}, format="json"),
    ]
    assert [r.status_code for r in checks] == [403, 403, 403, 403, 403, 404]


def test_private_player_data_never_in_public_payloads(admin_api, anon_client=None):
    p = admin_api.post("/api/admin/players/", {"first_name": "Priv", "last_name": "Ate", "email": "secret@example.com", "phone": "+123456"}, format="json").json()
    ids = [p["id"], admin_api.post("/api/admin/players/", {"first_name": "B", "last_name": "B"}, format="json").json()["id"]]
    team = admin_api.post("/api/admin/teams/", {"name": "Priv Team", "player_ids": ids}, format="json").json()["id"]
    t = admin_api.post("/api/admin/tournaments/", {"name": "Privacy Cup", "format": "round_robin"}, format="json").json()
    admin_api.post(f"/api/admin/tournaments/{t['id']}/registrations/", {"team_id": team}, format="json")
    other = make_teams(admin_api, 1, "Z")[0]
    admin_api.post(f"/api/admin/tournaments/{t['id']}/registrations/", {"team_id": other}, format="json")
    admin_api.post(f"/api/admin/tournaments/{t['id']}/set-status/", {"status": "published"}, format="json")
    body = APIClient().get(f"/api/public/tournaments/{t['slug']}/").content.decode()
    assert "Priv Ate" in body  # names are public
    for secret in ("secret@example.com", "+123456", "email", "phone"):
        assert secret not in body


# ----------------------------------------------------------------------------- persistence of history
def test_every_action_is_persisted_in_event_history(admin_api, make_match):
    m = make_match()
    for team in (0, 1, 0):
        score(admin_api, m["id"], team)
    score(admin_api, m["id"], action="undo")
    score(admin_api, m["id"], action="pause")
    score(admin_api, m["id"], action="resume")
    from apps.scoring.models import ScoreEvent

    events = list(ScoreEvent.objects.filter(match_id=m["id"]).order_by("sequence"))
    assert [e.event_type for e in events] == ["point", "point", "point", "undo", "pause", "resume"]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6]
    assert events[3].reverses_id == events[2].id and events[2].reversed_by.id == events[3].id  # original kept, reversal linked
    assert events[3].state_after == events[2].state_before
    ms = Match.objects.get(pk=m["id"]).state
    assert ms.revision == 6 and ms.data == events[-1].state_after


def test_audit_log_records_sensitive_actions(admin_api, make_match, root_api):
    m = make_match({"bestOf": 1})
    result(admin_api, m["id"], [{"games": [6, 0]}], "audit me")
    admin_api.post(f"/api/admin/matches/{m['id']}/verify/")
    admin_api.get(f"/api/admin/reports/matches/{m['id']}/pdf/")
    actions = {a["action"] for a in root_api.get("/api/admin/audit/?page_size=100").json()["results"]}
    assert {"match.create", "match.result_entry", "match.verify", "report.match_pdf"} <= actions


def test_pdf_logo_works_with_storage_without_filesystem_path():
    """Cloud storages (Cloudinary) have no .path; the logo must still be embedded."""
    import io

    from PIL import Image as PILImage

    from apps.reports.pdf import _logo

    buf = io.BytesIO()
    PILImage.new("RGB", (40, 20), "teal").save(buf, "PNG")

    class CloudField:
        name = "tournaments/x.png"

        def __bool__(self):
            return True

        @property
        def path(self):
            raise NotImplementedError("This backend doesn't support absolute paths.")

        def open(self, mode="rb"):
            return io.BytesIO(buf.getvalue())

    img = _logo(CloudField())
    assert img is not None and img.drawWidth > 0

    class Broken(CloudField):
        def open(self, mode="rb"):
            raise OSError("network down")

    assert _logo(Broken()) is None  # report still generated without the logo
    assert _logo(None) is None


def test_tournament_pdf_embeds_uploaded_logo(admin_api, settings, tmp_path):
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image as PILImage

    settings.MEDIA_ROOT = tmp_path
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 30), "navy").save(buf, "PNG")
    t = admin_api.post("/api/admin/tournaments/", {"name": "Logo Cup", "logo": SimpleUploadedFile("l.png", buf.getvalue(), "image/png")}, format="multipart").json()
    r = admin_api.get(f"/api/admin/reports/tournaments/{t['id']}/pdf/")
    assert r.status_code == 200
    assert len(PdfReader(BytesIO(r.content)).pages[0].images) == 1  # the logo is really in the PDF
