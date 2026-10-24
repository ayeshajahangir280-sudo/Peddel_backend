"""Black-box rules QA against a deployed API (creates clearly named friendly matches).

    python tests/perf/rules_qa.py --base https://api.example.com --user admin --password '...'
Every scenario follows the project guide (Love/15/30/40, deuce loops, 6-4/7-5 sets, 6-6 tie-break first to 7 by 2, undo in
tie-break, best of 1/3, ended match disables scoring, refresh keeps state).
"""
import argparse
import json
import time
import urllib.error
import urllib.request
import uuid

lat = []
fails = []
passed = 0


class C:
    def __init__(self, base):
        self.base, self.token = base.rstrip("/"), None

    def call(self, method, path, body=None):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers=h)
        t = time.perf_counter()
        try:
            r = urllib.request.urlopen(req, timeout=30)
            code, data = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, data = e.code, e.read()
        lat.append((time.perf_counter() - t) * 1000)
        return code, (json.loads(data) if data else None)


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        fails.append(name)
        print(f"  FAIL  {name}  {detail}")


def new_match(c, tag, **cfg):
    n = uuid.uuid4().hex[:5]
    code, m = c.call("POST", "/api/admin/matches/", {
        "name": f"QA {tag} {n}",
        "team_a": {"name": f"QA A {n}", "players": [{"first_name": f"QA{n}a1"}, {"first_name": f"QA{n}a2"}]},
        "team_b": {"name": f"QA B {n}", "players": [{"first_name": f"QA{n}b1"}, {"first_name": f"QA{n}b2"}]},
        "config": cfg,
    })
    assert code == 201, (code, m)
    return m


class Play:
    def __init__(self, c, m):
        self.c, self.id = c, m["id"]
        self.snap = None

    def act(self, action, team=None, **kw):
        body = {"action": action, "clientActionId": "qa-" + uuid.uuid4().hex[:20], **kw}
        if team is not None:
            body["team"] = team
        code, r = self.c.call("POST", f"/api/admin/matches/{self.id}/score/", body)
        if code == 200:
            self.snap = r["snapshot"]
        return code, r

    def pts(self, seq):
        for t in seq:
            code, r = self.act("point", 0 if t in "A0" else 1)
            assert code == 200, (code, r)
        return self.snap

    def game(self, t):
        return self.pts(t * 4)

    def games(self, a, b):
        for _ in range(a):
            self.game("A")
        for _ in range(b):
            self.game("B")

    @property
    def st(self):
        return self.snap["state"]

    @property
    def labels(self):
        return self.snap["display"]["pointLabels"]


def alternate_to_6_6(p):  # 5-5 then each wins one -> 6-6
    for _ in range(5):
        p.game("A")
        p.game("B")
    p.game("A")
    p.game("B")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    a = ap.parse_args()
    c = C(a.base)
    code, r = c.call("POST", "/api/auth/login/", {"username": a.user, "password": a.password})
    assert code == 200, r
    c.token = r["access"]

    print("\n[1] Point values: 0/15/30/40 and game win")
    p = Play(c, new_match(c, "points"))
    p.act("point", 0)
    check("first point shows 15-0", p.labels == ["15", "0"], p.labels)
    p.pts("AA")
    check("40-0 after three points", p.labels == ["40", "0"], p.labels)
    p.pts("A")
    check("fourth point wins the game, points reset", p.st["games"] == [1, 0] and p.st["points"] == [0, 0], p.st)
    check("match is live", p.snap["status"] == "live")

    print("\n[2] Deuce, advantage, repeated deuce (guide section 3)")
    p = Play(c, new_match(c, "deuce"))
    p.pts("AAABBB")
    check("40-40 is Deuce", p.snap["display"]["pointKind"] == "deuce" and p.labels == ["40", "40"])
    for i in range(5):
        p.pts("AB")
        check(f"deuce loop #{i + 1} returns to deuce", p.snap["display"]["pointKind"] == "deuce")
    p.pts("A")
    check("advantage A", p.labels == ["AD", "40"] and p.snap["display"]["pointKind"] == "advantage", p.labels)
    p.pts("B")
    check("advantage lost -> back to deuce", p.snap["display"]["pointKind"] == "deuce")
    p.pts("B")
    check("advantage B", p.labels == ["40", "AD"])
    p.pts("B")
    check("advantage + point = game B", p.st["games"] == [0, 1] and p.st["points"] == [0, 0])

    print("\n[3] Sets: 6-4 wins, 6-5 continues, 7-5 wins (section 4)")
    p = Play(c, new_match(c, "set64", bestOf=1))
    for _ in range(4):
        p.game("A"); p.game("B")
    p.games(2, 0)
    check("6-4 wins the set and (best of 1) the match", p.st["completed"] and p.st["sets"][0]["games"] == [6, 4] and p.st["winner"] == 0, p.st)
    p = Play(c, new_match(c, "set75", bestOf=1))
    for _ in range(5):
        p.game("A"); p.game("B")
    p.game("A")
    check("6-5 -> keep playing", not p.st["completed"] and p.st["games"] == [6, 5], p.st["games"])
    p.game("A")
    check("7-5 -> set won", p.st["completed"] and p.st["sets"][0]["games"] == [7, 5])

    print("\n[4] Tie-break at 6-6: first to 7, win by 2, undo inside it (sections 4, 13)")
    p = Play(c, new_match(c, "tiebreak", bestOf=1))
    alternate_to_6_6(p)
    check("6-6 starts a tie-break with numeric points", p.st["inTiebreak"] and p.st["games"] == [6, 6] and p.labels == ["0", "0"], p.st)
    p.pts("AAAAAA")
    check("6-0 in tie-break shows numbers", p.labels == ["6", "0"], p.labels)
    p.pts("BBBBBB")
    check("6-6 in tie-break: not won at 7 without 2 clear", p.labels == ["6", "6"] and not p.st["completed"])
    p.pts("A")
    check("7-6 does NOT win (needs 2 clear)", p.labels == ["7", "6"] and not p.st["completed"])
    p.act("undo")
    check("undo inside tie-break -> 6-6", p.labels == ["6", "6"] and p.st["inTiebreak"], p.labels)
    p.pts("AB")
    check("7-7 continues", p.labels == ["7", "7"] and not p.st["completed"])
    p.pts("AA")
    check("9-7 wins tie-break: set 7-6, tie-break score recorded", p.st["completed"] and p.st["sets"][0]["games"] == [7, 6] and p.st["sets"][0]["tiebreak"] == [9, 7], p.st["sets"])

    print("\n[5] Best of 3, undo across game/set boundaries, ended match disables buttons")
    p = Play(c, new_match(c, "bo3"))
    p.games(6, 0)
    check("set 1 to A (6-0), games reset", len(p.st["sets"]) == 1 and p.st["games"] == [0, 0])
    p.act("undo")
    check("undo of the set-winning point restores 5-0 40-0", p.st["sets"] == [] and p.st["games"] == [5, 0] and p.labels == ["40", "0"], (p.st["games"], p.labels))
    p.pts("A")
    p.games(0, 6)
    check("set 2 to B -> sets 1-1, match continues", len(p.st["sets"]) == 2 and not p.st["completed"])
    p.games(6, 0)
    check("A wins set 3 -> match won 2-1", p.st["completed"] and p.st["winner"] == 0 and len(p.st["sets"]) == 3)
    code, r = p.act("point", 1)
    check("scoring after match end is rejected (buttons disabled server-side)", code == 409, (code, r))
    code, r = p.act("undo")
    check("undo re-opens the completed match", code == 200 and not p.st["completed"] and p.snap["status"] == "live")
    p.pts("A")
    check("and it completes again", p.st["completed"])

    print("\n[6] Tie-break disabled (configurable) and golden point")
    p = Play(c, new_match(c, "notb", bestOf=1, tiebreakEnabled=False))
    alternate_to_6_6(p)
    check("6-6 without tie-break continues as normal games", not p.st["inTiebreak"] and not p.st["completed"])
    p.game("A"); p.game("A")
    check("8-6 wins the set", p.st["completed"] and p.st["sets"][0]["games"] == [8, 6])
    p = Play(c, new_match(c, "golden", bestOf=1, goldenPoint=True))
    p.pts("AAABBB")
    check("golden point at 40-40", p.snap["display"]["pointKind"] == "golden")
    p.pts("B")
    check("next point wins the game", p.st["games"] == [0, 1])

    print("\n[7] Super tie-break decider")
    p = Play(c, new_match(c, "super", superTiebreak=True))
    p.games(6, 0); p.games(0, 6)
    check("1-1 in sets starts a super tie-break to 10", p.st["inTiebreak"] and p.st["isSuper"] and p.st["tiebreakTarget"] == 10)
    p.pts("A" * 10)
    check("10-0 wins the match", p.st["completed"] and p.st["winner"] == 0)

    print("\n[8] Serve indicator, pause, end match")
    m = new_match(c, "serve")
    p = Play(c, m)
    s0 = m["state"]["server"]
    p.game("A")
    check("server changes every game", p.st["server"] != s0)
    code, _ = p.act("pause")
    check("pause works", code == 200 and p.snap["status"] == "paused")
    code, r = p.act("point", 0)
    check("no scoring while paused", code == 409)
    code, _ = p.act("resume")
    check("resume works", code == 200 and p.snap["status"] == "live")
    code, r = p.act("end")
    check("ending a level match asks for a winner", code == 400, (code, r))
    code, r = p.act("end", winner=1)
    check("end match with chosen winner", code == 200 and p.snap["status"] == "completed" and p.snap["winnerIndex"] == 1)

    print("\n[9] Refresh keeps state; duplicates and stale writes are rejected")
    p = Play(c, new_match(c, "persist"))
    p.pts("AABAB")
    code, fresh = c.call("GET", f"/api/admin/matches/{p.id}/")
    check("reloaded match equals last confirmed state", fresh["state"] == p.st and fresh["revision"] == p.snap["revision"])
    cid = "qa-dup-" + uuid.uuid4().hex[:12]
    body = {"action": "point", "team": 0, "clientActionId": cid}
    r1 = c.call("POST", f"/api/admin/matches/{p.id}/score/", body)[1]
    r2 = c.call("POST", f"/api/admin/matches/{p.id}/score/", body)[1]
    r3 = c.call("POST", f"/api/admin/matches/{p.id}/score/", body)[1]
    check("same clientActionId x3 counts once", r1["revision"] == r2["revision"] == r3["revision"] and r2["replayed"])
    code, r = c.call("POST", f"/api/admin/matches/{p.id}/score/", {**body, "clientActionId": "qa-x-" + uuid.uuid4().hex[:12], "expectedRevision": 1})
    check("stale revision rejected with current state", code == 409 and r["error"]["code"] == "stale_revision")
    code, pub = c.call("GET", f"/api/public/matches/{p.snap['publicId']}/")
    check("public page data matches (no private fields)", code == 200 and pub["revision"] == r1["revision"] and "umpire" not in json.dumps(pub).lower())

    print(f"\nrequests: {len(lat)}  p50={sorted(lat)[len(lat) // 2]:.0f}ms  p95={sorted(lat)[int(len(lat) * .95)]:.0f}ms  max={max(lat):.0f}ms (internet round trips)")
    print(f"RESULT: {passed} passed, {len(fails)} failed")
    for f in fails:
        print("  FAILED:", f)
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
