"""Group standings: a recomputable projection of completed group-stage matches."""
from itertools import groupby

from django.db import transaction

from apps.matches.models import MatchStatus

from .models import TournamentStanding


def _row(team_id):
    return {"team_id": team_id, "played": 0, "wins": 0, "losses": 0, "points": 0,
            "sets_for": 0, "sets_against": 0, "games_for": 0, "games_against": 0}


def _metric(row, key):
    if key == "set_difference":
        return row["sets_for"] - row["sets_against"]
    if key == "game_difference":
        return row["games_for"] - row["games_against"]
    if key == "games_won":
        return row["games_for"]
    if key == "sets_won":
        return row["sets_for"]
    return row[key]


def rank_rows(rows, criteria, results):
    """Order rows by the configured criteria.

    `results` is a list of (winner_team_id, loser_team_id) for head-to-head. Ties are broken criterion
    by criterion; head_to_head only considers matches among the teams still tied at that point.
    """
    if len(rows) <= 1:
        return list(rows)
    if not criteria:
        return sorted(rows, key=lambda r: r["team_id"])
    crit, rest = criteria[0], criteria[1:]
    if crit == "head_to_head":
        ids = {r["team_id"] for r in rows}
        wins = {i: 0 for i in ids}
        for w, l in results:
            if w in ids and l in ids:
                wins[w] += 1
        keyf = lambda r: wins[r["team_id"]]  # noqa: E731
    else:
        keyf = lambda r: _metric(r, crit)  # noqa: E731
    ordered = sorted(rows, key=keyf, reverse=True)
    out = []
    for _, grp in groupby(ordered, key=keyf):
        out.extend(rank_rows(list(grp), rest, results))
    return out


def set_and_game_totals(state):
    sets = [0, 0]
    games = [0, 0]
    for s in state.get("sets", []):
        g = s["games"]
        games[0] += g[0]
        games[1] += g[1]
        if g[0] > g[1]:
            sets[0] += 1
        elif g[1] > g[0]:
            sets[1] += 1
    return sets, games


@transaction.atomic
def recompute(tournament):
    from apps.tournaments.models import TournamentFixture

    TournamentStanding.objects.filter(tournament=tournament).delete()
    groups = list(tournament.groups.all())
    fixtures = list(
        TournamentFixture.objects.filter(tournament=tournament, stage=TournamentFixture.Stage.GROUP)
        .select_related("match__state")
    )
    by_group = {g.pk: [] for g in groups}
    for fx in fixtures:
        by_group.setdefault(fx.group_id, []).append(fx)
    regs = list(tournament.registrations.filter(withdrawn=False, group__isnull=False))
    created = []
    for group in groups:
        teams = [r.team_id for r in regs if r.group_id == group.pk]
        rows = {t: _row(t) for t in teams}
        results = []
        gfix = by_group.get(group.pk, [])
        for fx in gfix:
            m = fx.match
            if not m or m.status != MatchStatus.COMPLETED or not m.team_a_id or not m.team_b_id:
                continue
            if m.team_a_id not in rows or m.team_b_id not in rows:
                continue
            state = m.state.data
            sets, games = set_and_game_totals(state)
            win_idx = state.get("winner")
            ids = [m.team_a_id, m.team_b_id]
            for i in (0, 1):
                r = rows[ids[i]]
                r["played"] += 1
                r["sets_for"] += sets[i]
                r["sets_against"] += sets[1 - i]
                r["games_for"] += games[i]
                r["games_against"] += games[1 - i]
                if win_idx == i:
                    r["wins"] += 1
                    r["points"] += tournament.points_win
                else:
                    r["losses"] += 1
                    r["points"] += tournament.points_loss
            results.append((ids[win_idx], ids[1 - win_idx]))
        group_done = bool(gfix) and all(f.match and f.match.status == MatchStatus.COMPLETED for f in gfix)
        ordered = rank_rows(list(rows.values()), tournament.ranking_criteria, results)
        for pos, r in enumerate(ordered, start=1):
            created.append(TournamentStanding(
                tournament=tournament, group=group, team_id=r["team_id"], rank=pos,
                played=r["played"], wins=r["wins"], losses=r["losses"], points=r["points"],
                sets_for=r["sets_for"], sets_against=r["sets_against"],
                games_for=r["games_for"], games_against=r["games_against"],
                qualified=group_done and pos <= tournament.qualifiers_per_group,
            ))
    TournamentStanding.objects.bulk_create(created)
    return created


def group_complete(tournament, group_id):
    from apps.tournaments.models import TournamentFixture

    fx = TournamentFixture.objects.filter(tournament=tournament, group_id=group_id, stage="group").select_related("match")
    fx = list(fx)
    return bool(fx) and all(f.match_id and f.match.status == MatchStatus.COMPLETED for f in fx)
