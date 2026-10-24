"""Authoritative padel scoring engine.

Pure functions over plain dict/list state (JSON serialisable). A TypeScript twin lives in
admin/src/features/scoring/engine.ts; both are verified against tests/fixtures/scoring_contract.json.

State shape (camelCase on purpose, shared with the TS engine):
    sets:          [{"games": [a, b], "tiebreak": [a, b] | None, "super": bool}]  completed sets
    games:         [a, b]    games in the current set
    points:        [a, b]    raw points in the current game / tie-break
    inTiebreak:    bool
    isSuper:       bool      current tie-break is a deciding-set super tie-break
    tiebreakTarget:int
    tbFirstServer: 0|1|None
    server:        0|1       team currently serving
    winner:        0|1|None
    completed:     bool
"""
from copy import deepcopy

DEFAULT_CONFIG = {
    "bestOf": 3,
    "goldenPoint": False,
    "tiebreakEnabled": True,
    "tiebreakAt": 6,
    "tiebreakPoints": 7,
    "superTiebreak": False,
    "superTiebreakPoints": 10,
    "gamesPerSet": 6,
    "firstServer": 0,
}

LABELS = ["0", "15", "30", "40"]


class EngineError(ValueError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def normalize_config(cfg=None):
    c = {**DEFAULT_CONFIG, **(cfg or {})}
    if c["bestOf"] not in (1, 3):
        raise EngineError("invalid_config", "bestOf must be 1 or 3")
    if c["firstServer"] not in (0, 1):
        raise EngineError("invalid_config", "firstServer must be 0 or 1")
    for key in ("tiebreakAt", "tiebreakPoints", "superTiebreakPoints", "gamesPerSet"):
        if not isinstance(c[key], int) or c[key] < 1:
            raise EngineError("invalid_config", f"{key} must be a positive integer")
    if c["tiebreakAt"] > c["gamesPerSet"]:
        raise EngineError("invalid_config", "tiebreakAt cannot exceed gamesPerSet")
    c["goldenPoint"] = bool(c["goldenPoint"])
    c["tiebreakEnabled"] = bool(c["tiebreakEnabled"])
    c["superTiebreak"] = bool(c["superTiebreak"]) and c["bestOf"] == 3
    return c


def initial_state(cfg):
    return {
        "sets": [],
        "games": [0, 0],
        "points": [0, 0],
        "inTiebreak": False,
        "isSuper": False,
        "tiebreakTarget": 0,
        "tbFirstServer": None,
        "server": cfg["firstServer"],
        "winner": None,
        "completed": False,
    }


def sets_won(state):
    w = [0, 0]
    for s in state["sets"]:
        g = s["games"]
        if g[0] > g[1]:
            w[0] += 1
        elif g[1] > g[0]:
            w[1] += 1
    return w


def sets_to_win(cfg):
    return cfg["bestOf"] // 2 + 1


def _game_won(cfg, points, team):
    o = 1 - team
    if points[team] < 4:
        return False
    return cfg["goldenPoint"] or points[team] - points[o] >= 2


def _finish_set(cfg, s, team, tiebreak=None, add_game=False):
    games = list(s["games"])
    if add_game:
        games[team] += 1
    if s["isSuper"]:
        games = [0, 0]
        games[team] = 1
    s["sets"].append({"games": games, "tiebreak": tiebreak, "super": bool(s["isSuper"])})
    was_tb = s["inTiebreak"]
    tb_first = s["tbFirstServer"]
    s["games"] = [0, 0]
    s["points"] = [0, 0]
    s["inTiebreak"] = False
    s["isSuper"] = False
    s["tiebreakTarget"] = 0
    s["tbFirstServer"] = None
    won = sets_won(s)
    if won[team] >= sets_to_win(cfg):
        s["winner"] = team
        s["completed"] = True
        return
    if was_tb:
        s["server"] = 1 - tb_first
    if cfg["superTiebreak"] and won[0] == won[1] == sets_to_win(cfg) - 1:
        s["inTiebreak"] = True
        s["isSuper"] = True
        s["tiebreakTarget"] = cfg["superTiebreakPoints"]
        s["tbFirstServer"] = s["server"]


def apply_point(cfg, state, team):
    """Return the new state after `team` wins a point. Never mutates `state`."""
    if team not in (0, 1):
        raise EngineError("invalid_team", "team must be 0 or 1")
    if state["completed"]:
        raise EngineError("match_completed", "The match is already completed")
    s = deepcopy(state)
    o = 1 - team
    s["points"][team] += 1

    if s["inTiebreak"]:
        total = s["points"][0] + s["points"][1]
        first = s["tbFirstServer"]
        s["server"] = first if ((total + 1) // 2) % 2 == 0 else 1 - first
        if s["points"][team] >= s["tiebreakTarget"] and s["points"][team] - s["points"][o] >= 2:
            _finish_set(cfg, s, team, tiebreak=list(s["points"]), add_game=True)
        return s

    if not _game_won(cfg, s["points"], team):
        return s

    s["games"][team] += 1
    s["points"] = [0, 0]
    s["server"] = 1 - s["server"]
    g = s["games"]
    if g[team] >= cfg["gamesPerSet"] and g[team] - g[o] >= 2:
        _finish_set(cfg, s, team)
    elif cfg["tiebreakEnabled"] and g[0] == cfg["tiebreakAt"] and g[1] == cfg["tiebreakAt"]:
        s["inTiebreak"] = True
        s["tiebreakTarget"] = cfg["tiebreakPoints"]
        s["tbFirstServer"] = s["server"]
    return s


def point_labels(cfg, state):
    """Return (labels, kind) where kind is normal|deuce|advantage|golden|tiebreak|super."""
    a, b = state["points"]
    if state["inTiebreak"]:
        return [str(a), str(b)], ("super" if state["isSuper"] else "tiebreak")
    if a < 3 or b < 3:
        return [LABELS[min(a, 3)], LABELS[min(b, 3)]], "normal"
    if cfg["goldenPoint"]:
        return ["40", "40"], "golden"
    if a == b:
        return ["40", "40"], "deuce"
    return (["AD", "40"], "advantage") if a > b else (["40", "AD"], "advantage")


def match_point_teams(cfg, state):
    """Teams for which the next point wins the match."""
    if state["completed"]:
        return []
    return [t for t in (0, 1) if apply_point(cfg, state, t)["completed"]]


def set_point_teams(cfg, state):
    if state["completed"]:
        return []
    n = len(state["sets"])
    return [t for t in (0, 1) if len(apply_point(cfg, state, t)["sets"]) > n]


def describe(cfg, state):
    labels, kind = point_labels(cfg, state)
    return {
        "pointLabels": labels,
        "pointKind": kind,
        "matchPointFor": match_point_teams(cfg, state),
        "setPointFor": set_point_teams(cfg, state),
        "setsWon": sets_won(state),
    }


# --- manual result entry / correction -------------------------------------------------


def _valid_set(cfg, entry, is_deciding_super):
    g = entry["games"]
    tb = entry.get("tiebreak")
    if is_deciding_super:
        if g not in ([1, 0], [0, 1]):
            raise EngineError("invalid_result", "A super tie-break set must be recorded as 1-0 or 0-1")
        if not tb or max(tb) < cfg["superTiebreakPoints"] or abs(tb[0] - tb[1]) < 2:
            raise EngineError("invalid_result", "Invalid super tie-break score")
        if (g[0] > g[1]) != (tb[0] > tb[1]):
            raise EngineError("invalid_result", "Super tie-break winner mismatch")
        return {"games": g, "tiebreak": tb, "super": True}
    hi, lo = max(g), min(g)
    gps = cfg["gamesPerSet"]
    if hi >= gps and hi - lo >= 2 and tb is None:
        if hi > gps and (hi - lo != 2 or (cfg["tiebreakEnabled"] and hi != gps + 1)):
            raise EngineError("invalid_result", f"Impossible set score {g[0]}-{g[1]}")
        return {"games": g, "tiebreak": None, "super": False}
    if cfg["tiebreakEnabled"] and hi == cfg["tiebreakAt"] + 1 and lo == cfg["tiebreakAt"]:
        if not tb or max(tb) < cfg["tiebreakPoints"] or abs(tb[0] - tb[1]) < 2 or (g[0] > g[1]) != (tb[0] > tb[1]):
            raise EngineError("invalid_result", "Invalid tie-break score")
        return {"games": g, "tiebreak": tb, "super": False}
    raise EngineError("invalid_result", f"Impossible set score {g[0]}-{g[1]}")


def build_completed_state(cfg, sets, first_server=None):
    """Validate a list of {"games","tiebreak"} entries and return a completed match state."""
    if not sets:
        raise EngineError("invalid_result", "At least one set is required")
    state = initial_state(cfg)
    won = [0, 0]
    need = sets_to_win(cfg)
    recorded = []
    for i, entry in enumerate(sets):
        if max(won) >= need:
            raise EngineError("invalid_result", "Sets recorded after the match was decided")
        entry = {"games": [int(entry["games"][0]), int(entry["games"][1])], "tiebreak": entry.get("tiebreak")}
        if entry["tiebreak"] is not None:
            entry["tiebreak"] = [int(entry["tiebreak"][0]), int(entry["tiebreak"][1])]
        deciding = cfg["superTiebreak"] and won[0] == won[1] == need - 1 and cfg["bestOf"] == 3
        clean = _valid_set(cfg, entry, deciding)
        recorded.append(clean)
        won[0 if clean["games"][0] > clean["games"][1] else 1] += 1
    if max(won) < need:
        raise EngineError("invalid_result", "The result does not decide the match")
    state["sets"] = recorded
    state["winner"] = 0 if won[0] > won[1] else 1
    state["completed"] = True
    return state


def state_from_early_end(state, winner):
    s = deepcopy(state)
    s["winner"] = winner
    s["completed"] = True
    s["points"] = list(s["points"])
    return s
