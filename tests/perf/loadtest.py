"""Light-load benchmark against a RUNNING server (stdlib + websockets only).

    python tests/perf/loadtest.py --base http://127.0.0.1:8100 --host-header api.example.com \
        --user admin --password '...'

Measures (client side, so it includes network + serialisation):
  1. single scorer: sequential scoring latency (p50/p95/p99)
  2. concurrent scorers on separate matches
  3. spectator fan-out: N websocket spectators, time from "score committed" (POST returned) to every spectator seeing it
  4. public REST read throughput
Numbers depend entirely on the machine/DB they run on - record them per environment (see docs/TESTING.md).
"""
import argparse
import asyncio
import http.client
import json
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import websockets


class Api:
    def __init__(self, base, host_header=None):
        u = urlparse(base)
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)
        self.host_header = host_header
        self.token = None
        self.local = threading.local()

    def conn(self):
        c = getattr(self.local, "c", None)
        if c is None:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            c = self.local.c = cls(self.host, self.port, timeout=30)
        return c

    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.host_header:
            headers["Host"] = self.host_header
        for attempt in (0, 1):
            try:
                c = self.conn()
                c.request(method, path, json.dumps(body) if body is not None else None, headers)
                r = c.getresponse()
                data = r.read()
                try:
                    return r.status, (json.loads(data) if data else None)
                except ValueError:
                    return r.status, {"raw": data[:200].decode("utf-8", "replace")}
            except (http.client.HTTPException, OSError):
                self.local.c = None
                if attempt:
                    raise


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def summarize(name, ms):
    print(f"{name:<44} n={len(ms):<5} p50={pct(ms, 50):7.1f}ms  p95={pct(ms, 95):7.1f}ms  p99={pct(ms, 99):7.1f}ms  max={max(ms):7.1f}ms")
    return {"name": name, "n": len(ms), "p50": pct(ms, 50), "p95": pct(ms, 95), "p99": pct(ms, 99), "max": max(ms)}


def make_match(api, i):
    st, m = api.call("POST", "/api/admin/matches/", {
        "name": f"load {uuid.uuid4().hex[:6]} {i}",
        "team_a": {"players": [{"first_name": f"LA{i}a"}, {"first_name": f"LA{i}b"}]},
        "team_b": {"players": [{"first_name": f"LB{i}a"}, {"first_name": f"LB{i}b"}]},
    })
    assert st == 201, (st, m)
    return m


def score(api, mid, team, expected=None):
    body = {"action": "point", "team": team, "clientActionId": uuid.uuid4().hex}
    if expected is not None:
        body["expectedRevision"] = expected
    t0 = time.perf_counter()
    st, r = api.call("POST", f"/api/admin/matches/{mid}/score/", body)
    return (time.perf_counter() - t0) * 1000, st, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--host-header")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--points", type=int, default=300)
    ap.add_argument("--scorers", type=int, default=8)
    ap.add_argument("--spectators", type=int, default=50)
    ap.add_argument("--origin", default="http://localhost:5173", help="allowed WebSocket Origin")
    ap.add_argument("--out")
    a = ap.parse_args()

    api = Api(a.base, a.host_header)
    st, r = api.call("POST", "/api/auth/login/", {"username": a.user, "password": a.password})
    assert st == 200, (st, r)
    api.token = r["access"]
    results = []

    # 1. single scorer
    m = make_match(api, 0)
    lat, rev = [], 0
    for i in range(a.points):
        ms, st, r = score(api, m["id"], int(i % 3 == 0), expected=rev)
        if st == 409 and r["error"]["code"] in ("match_completed", "not_scorable"):
            break
        assert st == 200, (st, r)
        rev = r["revision"]
        lat.append(ms)
    results.append(summarize("1. single scorer, sequential (with expectedRevision)", lat))

    # 2. concurrent scorers, separate matches
    matches = [make_match(api, i + 1) for i in range(a.scorers)]
    lat2, errors = [], []

    def scorer(mid):
        local_api = Api(a.base, a.host_header)
        local_api.token = api.token
        out = []
        for i in range(80):
            ms, st, r = score(local_api, mid, i % 2)
            if st != 200:
                errors.append(st)
            out.append(ms)
        return out

    t0 = time.perf_counter()
    with ThreadPoolExecutor(a.scorers) as ex:
        for part in ex.map(scorer, [x["id"] for x in matches]):
            lat2 += part
    wall = time.perf_counter() - t0
    results.append(summarize(f"2. {a.scorers} concurrent scorers (separate matches)", lat2))
    print(f"   throughput: {len(lat2) / wall:.0f} points/s, non-200 responses: {len(errors)}")

    # 3. spectator fan-out
    target = make_match(api, 99)
    ws_base = a.base.replace("http", "ws", 1)

    async def fanout():
        got = {}
        ready = asyncio.Event()
        connected = 0
        extra = {"additional_headers": {"Host": a.host_header}} if a.host_header else {}

        async def spectator(i):
            nonlocal connected
            async with websockets.connect(f"{ws_base}/ws/matches/{target['publicId']}/", origin=a.origin, **extra) as ws:
                await ws.recv()  # initial snapshot
                connected += 1
                if connected == a.spectators:
                    ready.set()
                while True:
                    msg = json.loads(await ws.recv())
                    rev = msg["snapshot"]["revision"]
                    got.setdefault(rev, []).append(time.perf_counter())

        tasks = [asyncio.create_task(spectator(i)) for i in range(a.spectators)]
        await asyncio.wait_for(ready.wait(), 30)
        loop = asyncio.get_running_loop()
        delays = []
        for i in range(30):
            sent = time.perf_counter()
            ms, st, r = await loop.run_in_executor(None, score, api, target["id"], i % 2)
            assert st == 200, (st, r)
            rev = r["revision"]
            for _ in range(200):
                if len(got.get(rev, [])) >= a.spectators:
                    break
                await asyncio.sleep(0.005)
            seen = got.get(rev, [])
            if len(seen) >= a.spectators:
                delays.append((max(seen) - sent) * 1000)
            await asyncio.sleep(0.05)
        for t in tasks:
            t.cancel()
        return delays

    delays = asyncio.run(fanout())
    if delays:
        results.append(summarize(f"3. score -> all {a.spectators} spectators received", delays))
    else:
        print("3. spectator fan-out: no complete deliveries measured")

    # 4. public reads
    st, lst = api.call("GET", "/api/public/matches/" + target["publicId"] + "/")
    lat4 = []

    def reader(_):
        local = Api(a.base, a.host_header)
        out = []
        for _ in range(60):
            t = time.perf_counter()
            s, _b = local.call("GET", "/api/public/matches/" + target["publicId"] + "/")
            out.append((time.perf_counter() - t) * 1000)
            assert s == 200
        return out

    with ThreadPoolExecutor(20) as ex:
        for part in ex.map(reader, range(20)):
            lat4 += part
    results.append(summarize("4. public match read, 20 concurrent clients", lat4))

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
