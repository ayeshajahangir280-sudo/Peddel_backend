"""Full tournament QA against a deployed API. Every match is played point by point through the scoring endpoint and the
resulting tables / bracket / champion are compared with an independent calculation made here.

    python tests/perf/tournament_qa.py --base https://api.example.com --user admin --password '...'
"""
import argparse
import itertools
import json
import time
import urllib.error
import urllib.request
import uuid

fails, passed = [], 0
lat = []


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        fails.append(name)
        print(f"  FAIL  {name}  {detail}")


class C:
    def __init__(self, base):
        self.base, self.t = base.rstrip("/"), None

    def call(self, m, p, b=None, raw=False):
        h = {"Content-Type": "application/json"}
        if self.t:
            h["Authorization"] = "Bearer " + self.t
        r = urllib.request.Request(self.base + p, method=m, data=json.dumps(b).encode() if b is not None else None, headers=h)
        t0 = time.perf_counter()
        try:
            x = urllib.request.urlopen(r, timeout=60)
            code, data, hdr = x.status, x.read(), dict(x.headers)
        except urllib.error.HTTPError as e:
            code, data, hdr = e.code, e.read(), dict(e.headers)
        lat.append((time.perf_counter() - t0) * 1000)
        if raw:
            return code, data, hdr
        return code, (json.loads(data) if data else None)


def play_match(c, match_id, winner, loser_games):
    """Winner takes the (best-of-1) set 6-loser_games. Returns final snapshot."""
    seq = []
    for _ in range(loser_games):
        seq += [1 - winner] * 4
    seq += [winner] * 4 * 6
    snap = None
    rev = None
    for t in seq:
        code, r = c.call("POST", f"/api/admin/matches/{match_id}/score/", {"action": "point", "team": t, "clientActionId": "tq-" + uuid.uuid4().hex[:18], **({"expectedRevision": rev} if rev is not None else {})})
        assert code == 200, (code, r)
        snap, rev = r["snapshot"], r["revision"]
    return snap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--public", default="")
    a = ap.parse_args()
    c = C(a.base)
    code, r = c.call("POST", "/api/auth/login/", {"username": a.user, "password": a.password})
    assert code == 200, r
    c.t = r["access"]
    run = uuid.uuid4().hex[:5]

    print("\n[1] Setup: umpire, courts, 8 teams, tournament (groups + knockout, best of 1)")
    code, ump = c.call("POST", "/api/admin/users/", {"username": f"qaump{run}", "email": f"qaump{run}@example.com", "password": "QaUmpire#2026", "role": "umpire", "first_name": "QA", "last_name": "Umpire"})
    check("umpire created", code == 201, (code, ump))
    courts = []
    for i in (1, 2):
        code, ct = c.call("POST", "/api/admin/courts/", {"name": f"QA Court {run}-{i}", "venue": "QA"})
        courts.append(ct["id"])
    teams = []
    for i in range(1, 9):
        pids = [c.call("POST", "/api/admin/players/", {"first_name": f"QT{run}P{i}{k}", "last_name": "Qa"})[1]["id"] for k in "ab"]
        code, t = c.call("POST", "/api/admin/teams/", {"name": f"QA {run} Team {i}", "player_ids": pids})
        teams.append(t["id"])
    check("8 teams created", len(teams) == 8)
    code, T = c.call("POST", "/api/admin/tournaments/", {
        "name": f"QA Tournament {run}", "format": "groups_knockout", "venue": "QA Club", "organizer": "QA", "groups_count": 2, "qualifiers_per_group": 2,
        "start_date": "2026-11-01", "end_date": "2026-11-03", "description": "Automated QA tournament.",
        "match_format": {"bestOf": 1, "goldenPoint": False, "tiebreakEnabled": True, "superTiebreak": False},
        "ranking_criteria": ["points", "wins", "head_to_head", "set_difference", "game_difference"]})
    check("tournament created with unique slug", code == 201 and T["slug"].startswith("qa-tournament"), (code, T))
    tid, base = T["id"], f"/api/admin/tournaments/{T['id']}"
    check("not public while draft", c.call("GET", f"/api/public/tournaments/{T['slug']}/")[0] == 404)
    for tm in teams:
        code, _ = c.call("POST", f"{base}/registrations/", {"team_id": tm})
        assert code == 201
    check("duplicate registration refused", c.call("POST", f"{base}/registrations/", {"team_id": teams[0]})[0] == 409)

    print("\n[2] Fixtures: preview, confirm, guards")
    code, pv = c.call("POST", f"{base}/fixtures/preview/", {"groups_count": 2, "qualifiers_per_group": 2, "seed": 5})
    check("preview: 2 groups, 12 group matches, 3 knockout matches", pv["counts"] == {"groups": 2, "group_matches": 12, "knockout_matches": 3}, pv.get("counts"))
    check("preview persisted nothing", c.call("GET", f"{base}/fixtures/")[1] == [])
    code, _ = c.call("POST", f"{base}/fixtures/confirm/", {"groups_count": 2, "qualifiers_per_group": 2, "seed": 5})
    check("fixtures confirmed", code == 201)
    check("second confirm without replace refused", c.call("POST", f"{base}/fixtures/confirm/", {"seed": 5})[0] == 409)
    fx = c.call("GET", f"{base}/fixtures/")[1]
    groups = [f for f in fx if f["stage"] == "group"]
    ko = [f for f in fx if f["stage"] == "knockout"]
    pairs = {(f["team_a_name"], f["team_b_name"]) for f in groups}
    check("12 unique group fixtures (every pair once per group)", len(groups) == 12 and len({frozenset(p) for p in pairs}) == 12)
    check("knockout placeholders (2 semis + final) unresolved before groups end", len(ko) == 3 and all("Group" in f["team_a_name"] or "Winner" in f["team_a_name"] for f in ko))
    code, _ = c.call("POST", f"{base}/set-status/", {"status": "published"})
    check("published", code == 200)
    check("public tournament page now visible", c.call("GET", f"/api/public/tournaments/{T['slug']}/")[0] == 200)

    print("\n[3] Schedule: courts, umpire, double booking")
    g0 = groups[0]["match"]["id"]
    code, _ = c.call("PATCH", f"/api/admin/matches/{g0}/", {"court": courts[0], "umpire": ump["id"], "scheduled_at": "2026-11-01T10:00:00Z"})
    check("court + umpire + time assigned", code == 200)
    g1 = groups[1]["match"]["id"]
    code, r = c.call("PATCH", f"/api/admin/matches/{g1}/", {"court": courts[0], "scheduled_at": "2026-11-01T10:30:00Z"})
    check("same court within 90 min refused", code == 409 and r["error"]["code"] == "court_conflict", (code, r))
    code, _ = c.call("PATCH", f"/api/admin/matches/{g1}/", {"court": courts[1], "scheduled_at": "2026-11-01T10:30:00Z"})
    check("different court accepted", code == 200)

    print("\n[4] Play all 12 group matches point by point (lower-numbered team wins 6-k)")
    num = {f"QA {run} Team {i}": i for i in range(1, 9)}
    exp = {}  # team -> dict
    for t in num:
        exp[t] = dict(p=0, w=0, l=0, sf=0, sa=0, gf=0, ga=0)
    started = time.perf_counter()
    for idx, f in enumerate(groups):
        a_, b_ = f["team_a_name"], f["team_b_name"]
        win_idx = 0 if num[a_] < num[b_] else 1
        k = idx % 5  # loser games 0..4
        snap = play_match(c, f["match"]["id"], win_idx, k)
        assert snap["state"]["completed"] and snap["state"]["sets"][0]["games"][win_idx] == 6 and snap["state"]["sets"][0]["games"][1 - win_idx] == k, snap["state"]
        w, l = (a_, b_) if win_idx == 0 else (b_, a_)
        exp[w].update(p=exp[w]["p"] + 3, w=exp[w]["w"] + 1, sf=exp[w]["sf"] + 1, gf=exp[w]["gf"] + 6, ga=exp[w]["ga"] + k)
        exp[l].update(l=exp[l]["l"] + 1, sa=exp[l]["sa"] + 1, gf=exp[l]["gf"] + k, ga=exp[l]["ga"] + 6)
    check(f"12 group matches played ({time.perf_counter() - started:.0f}s)", True)
    st = c.call("GET", f"{base}/standings/")[1]
    check("24 standing rows (2 groups x 4)", len(st) == 8, len(st))
    ok = True
    for s in st:
        e = exp[s["team_name"]]
        ok &= (s["points"], s["wins"], s["losses"], s["sets_for"], s["sets_against"], s["games_for"], s["games_against"]) == (e["p"], e["w"], e["l"], e["sf"], e["sa"], e["gf"], e["ga"])
    check("every team's P/W/L/points/sets/games equals the independent calculation", ok)
    for g in ("Group A", "Group B"):
        rows = [s for s in st if s["group_name"] == g]
        order = [num[s["team_name"]] for s in rows]
        check(f"{g} ranked by the criteria (strongest team first)", order == sorted(order), order)
        check(f"{g}: top two flagged qualified", [s["qualified"] for s in rows] == [True, True, False, False])
    pub = c.call("GET", f"/api/public/tournaments/{T['slug']}/")[1]
    check("public standings match admin standings", [r["team"] for g in pub["standings"] for r in g["rows"]] == [s["team_name"] for s in st])

    print("\n[5] Knockout: qualifiers placed automatically, cross pairing, play semis and final")
    fx = c.call("GET", f"{base}/fixtures/")[1]
    semis = sorted([f for f in fx if f["stage"] == "knockout" and f["round_number"] == 1], key=lambda f: f["position"])
    check("both semifinals have real teams", all(f["team_a_name"].startswith("QA ") and f["team_b_name"].startswith("QA ") for f in semis), [(f["team_a_name"], f["team_b_name"]) for f in semis])
    gname = lambda team: "A" if team in [s["team_name"] for s in st if s["group_name"] == "Group A"] else "B"
    check("no semifinal between teams of the same group", all(gname(f["team_a_name"]) != gname(f["team_b_name"]) for f in semis))
    code, r = c.call("POST", f"{base}/fixtures/confirm/", {"replace": True, "seed": 5})
    check("regenerating fixtures after play is refused", code == 409 and r["error"]["code"] == "fixtures_in_progress", (code, r))
    finalists = []
    for f in semis:
        a_, b_ = f["team_a_name"], f["team_b_name"]
        win_idx = 0 if num[a_] < num[b_] else 1
        play_match(c, f["match"]["id"], win_idx, 2)
        finalists.append(a_ if win_idx == 0 else b_)
    fx = c.call("GET", f"{base}/fixtures/")[1]
    final = [f for f in fx if f["stage"] == "knockout" and f["round_number"] == 2][0]
    check("final populated with the two semifinal winners", {final["team_a_name"], final["team_b_name"]} == set(finalists), (final["team_a_name"], final["team_b_name"], finalists))
    play_match(c, final["match"]["id"], 0 if num[final["team_a_name"]] < num[final["team_b_name"]] else 1, 3)
    champion = min(finalists, key=lambda t: num[t])
    T2 = c.call("GET", f"{base}/")[1]
    check("champion determined", T2["champion_name"] == champion, (T2["champion_name"], champion))
    check("tournament status = completed automatically", T2["status"] == "completed", T2["status"])
    pub = c.call("GET", f"/api/public/tournaments/{T['slug']}/")[1]
    check("public page shows champion and final ranking", pub["champion"] == champion and pub["rankings"][0] == {"place": 1, "team": champion} and len(pub["rankings"]) >= 3, pub["rankings"])
    check("public phase = completed", pub["phase"] == "completed")

    print("\n[6] Corrections and guards after the tournament finished")
    # 6a: a correction that does NOT change who qualifies/positions is allowed (rank-1 team beats last-placed team: flip it)
    rows_a = [s for s in st if s["group_name"] == "Group A"]
    top, last = rows_a[0]["team_name"], rows_a[-1]["team_name"]
    f_safe = next(f for f in groups if {f["team_a_name"], f["team_b_name"]} == {top, last})
    flip = lambda f, loser_first: [{"games": [0, 6]}] if loser_first else [{"games": [6, 0]}]
    top_is_a = f_safe["team_a_name"] == top
    code, r = c.call("POST", f"/api/admin/matches/{f_safe['match']['id']}/result/", {"clientActionId": "tq-" + uuid.uuid4().hex[:14], "reason": "QA: harmless flip", "sets": flip(f_safe, top_is_a)})
    check("correction that keeps the same qualifiers is allowed (audited, standings recomputed)", code == 200, (code, str(r)[:150]))
    st_after = c.call("GET", f"{base}/standings/")[1]
    check("standings recomputed after the correction (top team lost 3 points)", next(s for s in st_after if s["team_name"] == top)["points"] == next(s for s in st if s["team_name"] == top)["points"] - 3)
    c.call("POST", f"/api/admin/matches/{f_safe['match']['id']}/result/", {"clientActionId": "tq-" + uuid.uuid4().hex[:14], "reason": "QA: restore original result", "sets": flip(f_safe, not top_is_a)})
    check("restoring the original result restores the standings exactly", c.call("GET", f"{base}/standings/")[1] == st)
    # 6b: a flip between rank 1 and rank 2 changes the semifinal pairings, which were already played -> refused + rolled back
    second = rows_a[1]["team_name"]
    f_bad = next(f for f in groups if {f["team_a_name"], f["team_b_name"]} == {top, second})
    top_is_a = f_bad["team_a_name"] == top
    code, r = c.call("POST", f"/api/admin/matches/{f_bad['match']['id']}/result/", {"clientActionId": "tq-" + uuid.uuid4().hex[:14], "reason": "QA: flip that would change the bracket", "sets": flip(f_bad, top_is_a)})
    check("correction that would change a played knockout is refused", code == 409 and r["error"]["code"] == "downstream_started", (code, str(r)[:200]))
    check("and rolled back: standings unchanged", c.call("GET", f"{base}/standings/")[1] == st)
    ms = c.call("GET", f"/api/admin/matches/?tournament={tid}&page_size=100")[1]
    check("all 15 tournament matches completed", ms["count"] == 15 and all(m["status"] == "completed" for m in ms["results"]), ms["count"])

    print("\n[7] Admin PDFs (final) and public restrictions")
    code, data, hdr = c.call("GET", f"/api/admin/reports/tournaments/{tid}/pdf/", raw=True)
    cd = hdr.get("content-disposition") or hdr.get("Content-Disposition", "")
    check("final tournament PDF is labelled 'results' and is a PDF", code == 200 and data[:5] == b"%PDF-" and f"tournament-results-{T['slug']}.pdf" in cd, (code, cd))
    for kind in ("standings", "fixtures"):
        code, data, _ = c.call("GET", f"/api/admin/reports/tournaments/{tid}/{kind}/pdf/", raw=True)
        check(f"{kind} PDF downloads", code == 200 and data[:5] == b"%PDF-")
    code, data, hdr = c.call("GET", f"/api/admin/reports/matches/{final['match']['id']}/pdf/", raw=True)
    check("final match PDF downloads", code == 200 and data[:5] == b"%PDF-")
    anon = C(a.base)
    for path in (f"/api/admin/reports/tournaments/{tid}/pdf/", f"/api/public/tournaments/{T['slug']}/pdf/", f"/api/public/reports/tournaments/{T['slug']}/pdf/"):
        check(f"anonymous cannot get {path.split('/api/')[1]}", anon.call("GET", path, raw=True)[0] in (401, 404))
    check("public payload has no PDF/private fields", not any(x in json.dumps(pub).lower() for x in ("pdf", "email", "umpire", "phone")))

    print("\n[8] Lifecycle: unpublish hides it, republish shows it, archive")
    code, r = c.call("POST", f"{base}/set-status/", {"status": "draft"})
    check("a completed tournament cannot be reverted to draft", code == 409, (code, r))
    code, _ = c.call("POST", f"{base}/set-status/", {"status": "archived"})
    check("archive hides it from the public site", code == 200 and c.call("GET", f"/api/public/tournaments/{T['slug']}/")[0] == 404)
    mid = ms["results"][0]["publicId"]
    check("and its matches disappear too", anon.call("GET", f"/api/public/matches/{mid}/")[0] == 404)
    code, _ = c.call("POST", f"{base}/set-status/", {"status": "published"})
    pub2 = c.call("GET", f"/api/public/tournaments/{T['slug']}/")[1]
    check("restoring brings it back as completed with its champion", code == 200 and pub2["champion"] == champion and pub2["phase"] == "completed")

    s = sorted(lat)
    print(f"\nrequests: {len(lat)}  p50={s[len(s) // 2]:.0f}ms  p95={s[int(len(s) * .95)]:.0f}ms")
    print(f"RESULT: {passed} passed, {len(fails)} failed")
    print(f"tournament left published for you to browse: {a.public or '<public site>'}/public-tournaments/{T['slug']}")
    for f in fails:
        print("  FAILED:", f)
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
