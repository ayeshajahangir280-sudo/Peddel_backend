import json
from pathlib import Path

import pytest

from apps.scoring import engine as E

CFG = E.normalize_config()


def play(cfg, teams, state=None):
    state = state or E.initial_state(cfg)
    for t in teams:
        state = E.apply_point(cfg, state, t)
    return state


def labels(cfg, st):
    return E.point_labels(cfg, st)


def test_love_15_30_40_progression():
    s = E.initial_state(CFG)
    seen = []
    for _ in range(3):
        s = E.apply_point(CFG, s, 0)
        seen.append(labels(CFG, s)[0][0])
    assert seen == ["15", "30", "40"]


def test_game_win_without_deuce():
    s = play(CFG, [0, 0, 0, 0])
    assert s["games"] == [1, 0] and s["points"] == [0, 0]


def test_deuce_and_advantage_and_back_to_deuce():
    s = play(CFG, [0, 0, 0, 1, 1, 1])
    assert labels(CFG, s) == (["40", "40"], "deuce")
    s = E.apply_point(CFG, s, 0)
    assert labels(CFG, s) == (["AD", "40"], "advantage")
    s = E.apply_point(CFG, s, 1)
    assert labels(CFG, s) == (["40", "40"], "deuce")
    s = E.apply_point(CFG, s, 1)
    assert labels(CFG, s) == (["40", "AD"], "advantage")
    s = E.apply_point(CFG, s, 1)
    assert s["games"] == [0, 1]


def test_repeated_deuce_many_times():
    s = play(CFG, [0, 0, 0, 1, 1, 1])
    for _ in range(10):
        s = play(CFG, [0, 1], s)
        assert labels(CFG, s)[1] == "deuce"
    assert s["games"] == [0, 0]


def test_golden_point_decides_at_deuce():
    g = E.normalize_config({"goldenPoint": True})
    s = play(g, [0, 0, 0, 1, 1, 1])
    assert labels(g, s)[1] == "golden"
    s = E.apply_point(g, s, 1)
    assert s["games"] == [0, 1]


def test_set_won_6_4_and_7_5():
    s = play(CFG, ([0] * 4 + [1] * 4) * 4 + [0] * 8)  # 4-4 then 6-4
    assert s["sets"][0]["games"] == [6, 4]
    s = play(CFG, ([0] * 4 + [1] * 4) * 5 + [0] * 8)  # 5-5, then 6-5, 7-5
    assert s["sets"][0]["games"] == [7, 5] and s["sets"][0]["tiebreak"] is None


def test_tiebreak_triggers_at_6_6_and_needs_two_clear_points():
    s = play(CFG, ([0] * 4 + [1] * 4) * 6)
    assert s["games"] == [6, 6] and s["inTiebreak"] and s["tiebreakTarget"] == 7
    s = play(CFG, [0] * 6 + [1] * 6, s)
    assert s["points"] == [6, 6] and s["inTiebreak"]
    s = play(CFG, [0, 1, 0, 0], s)
    assert not s["inTiebreak"] and s["sets"][0]["games"] == [7, 6] and s["sets"][0]["tiebreak"] == [9, 7]


def test_tiebreak_server_rotation_every_two_points():
    s = play(CFG, ([0] * 4 + [1] * 4) * 6)
    first = s["tbFirstServer"]
    servers = [s["server"]]
    for _ in range(6):
        s = E.apply_point(CFG, s, 0)
        servers.append(s["server"])
    # server for points 0..6: first, other, other, first, first, other, other
    assert servers == [first, 1 - first, 1 - first, first, first, 1 - first, 1 - first]


def test_best_of_three_match_completion_and_no_more_points():
    s = play(CFG, [0] * 24 + [0] * 24)
    assert s["completed"] and s["winner"] == 0 and len(s["sets"]) == 2
    with pytest.raises(E.EngineError) as ei:
        E.apply_point(CFG, s, 1)
    assert ei.value.code == "match_completed"


def test_best_of_one():
    c = E.normalize_config({"bestOf": 1})
    s = play(c, [1] * 24)
    assert s["completed"] and s["winner"] == 1 and len(s["sets"]) == 1


def test_super_tiebreak_replaces_third_set():
    c = E.normalize_config({"superTiebreak": True})
    s = play(c, [0] * 24 + [1] * 24)  # 1-1 in sets
    assert s["inTiebreak"] and s["isSuper"] and s["tiebreakTarget"] == 10
    s = play(c, [0] * 10, s)
    assert s["completed"] and s["winner"] == 0
    assert s["sets"][-1]["super"] and s["sets"][-1]["tiebreak"] == [10, 0]


def test_super_tiebreak_needs_two_clear():
    c = E.normalize_config({"superTiebreak": True})
    s = play(c, [0] * 24 + [1] * 24)
    s = play(c, [0] * 9 + [1] * 9, s)
    s = play(c, [0], s)
    assert not s["completed"]
    s = play(c, [0], s)
    assert s["completed"] and s["sets"][-1]["tiebreak"] == [11, 9]


def test_apply_point_does_not_mutate_input():
    s = E.initial_state(CFG)
    before = json.dumps(s, sort_keys=True)
    E.apply_point(CFG, s, 0)
    assert json.dumps(s, sort_keys=True) == before


def test_invalid_team_and_config():
    with pytest.raises(E.EngineError):
        E.apply_point(CFG, E.initial_state(CFG), 2)
    with pytest.raises(E.EngineError):
        E.normalize_config({"bestOf": 5})


def test_match_point_and_set_point_flags():
    s = play(CFG, [0] * 20)  # 5-0 in set 1
    d = E.describe(CFG, s)
    assert d["setPointFor"] == []
    s = play(CFG, [0] * 3, s)  # 40-0 at 5-0 => set point
    assert E.describe(CFG, s)["setPointFor"] == [0]
    c1 = E.normalize_config({"bestOf": 1})
    s = play(c1, [0] * 23)
    assert E.describe(c1, s)["matchPointFor"] == [0]


def test_build_completed_state_validates_scores():
    st = E.build_completed_state(CFG, [{"games": [6, 4]}, {"games": [7, 6], "tiebreak": [7, 5]}])
    assert st["completed"] and st["winner"] == 0
    st = E.build_completed_state(CFG, [{"games": [7, 5]}, {"games": [4, 6]}, {"games": [6, 3]}])
    assert st["winner"] == 0
    for bad in ([{"games": [6, 5]}], [{"games": [8, 6]}, {"games": [6, 0]}], [{"games": [7, 6]}, {"games": [6, 0]}],
                [{"games": [6, 0]}, {"games": [6, 0]}, {"games": [6, 0]}], [{"games": [6, 4]}]):
        with pytest.raises(E.EngineError):
            E.build_completed_state(CFG, bad)


def test_build_completed_state_super_tiebreak():
    c = E.normalize_config({"superTiebreak": True})
    st = E.build_completed_state(c, [{"games": [6, 4]}, {"games": [3, 6]}, {"games": [1, 0], "tiebreak": [10, 7]}])
    assert st["winner"] == 0 and st["sets"][-1]["super"]


# ---- cross-language contract: the TypeScript engine is verified against the same file
FIXTURE = Path(__file__).parent / "fixtures" / "scoring_contract.json"


def test_contract_fixture_replays_identically():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    kinds = set()
    for sc in data["scenarios"]:
        cfg = E.normalize_config(sc["config"])
        assert cfg == sc["normalizedConfig"]
        state = E.initial_state(cfg)
        for step in sc["steps"]:
            state = E.apply_point(cfg, state, step["team"])
            assert state == step["state"], sc["name"]
            assert E.describe(cfg, state) == step["display"], sc["name"]
            kinds.add(step["display"]["pointKind"])
    assert {"normal", "deuce", "advantage", "golden", "tiebreak", "super"} <= kinds
