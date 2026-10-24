"""Generate tests/fixtures/scoring_contract.json from the authoritative Python engine.

Run:  python tests/generate_scoring_fixtures.py
The TypeScript engine (admin/src/features/scoring/engine.ts) is verified against the same file.
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.scoring import engine  # noqa: E402

CONFIGS = {
    "default": {},
    "golden": {"goldenPoint": True},
    "super": {"superTiebreak": True},
    "super_golden": {"superTiebreak": True, "goldenPoint": True},
    "bo1": {"bestOf": 1},
    "no_tiebreak": {"tiebreakEnabled": False, "bestOf": 1},
    "server1": {"firstServer": 1},
    "short_sets": {"gamesPerSet": 4, "tiebreakAt": 4, "tiebreakPoints": 5},
}


def play(cfg, script):
    cfgn = engine.normalize_config(cfg)
    state = engine.initial_state(cfgn)
    steps = []
    for team in script:
        if state["completed"]:
            break
        state = engine.apply_point(cfgn, state, team)
        d = engine.describe(cfgn, state)
        steps.append({"team": team, "state": state, "display": d})
    return cfgn, steps


def scenarios():
    out = []
    # hand-written
    out.append(("one_game", "default", [0, 0, 0, 0]))
    out.append(("deuce_loop", "default", [0, 0, 0, 1, 1, 1, 0, 1, 0, 1, 0, 0]))
    out.append(("golden_point", "golden", [0, 0, 0, 1, 1, 1, 1]))
    out.append(("advantage_back_to_deuce", "default", [0, 1, 0, 1, 0, 1, 0, 1, 1, 1]))
    # tie-break set: alternate games to 6-6 then tie-break
    g = []
    for _ in range(5):
        g += [0] * 4 + [1] * 4
    g += [0] * 4 + [1] * 4  # 6-6
    g += [0] * 7  # tiebreak 7-0
    out.append(("tiebreak_set", "default", g))
    # random full matches
    rng = random.Random(20260101)
    for i in range(24):
        name = list(CONFIGS)[i % len(CONFIGS)]
        bias = rng.choice([0.5, 0.5, 0.6, 0.75])
        out.append((f"random_{i}_{name}", name, [0 if rng.random() < bias else 1 for _ in range(900)]))
    return out


def main():
    data = {"version": 1, "scenarios": []}
    for name, cfg_name, script in scenarios():
        cfg = CONFIGS[cfg_name]
        cfgn, steps = play(cfg, script)
        data["scenarios"].append({"name": name, "config": cfg, "normalizedConfig": cfgn, "steps": steps})
    out = ROOT / "tests" / "fixtures" / "scoring_contract.json"
    out.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(data['scenarios'])} scenarios)")


if __name__ == "__main__":
    main()
