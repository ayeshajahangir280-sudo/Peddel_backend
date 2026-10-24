"""Tournament workflows: fixture preview/confirm, bracket reconciliation, champion detection."""
import math
import random
import string

from django.db import transaction
from django.utils import timezone

from apps.accounts.errors import ApiError, ConflictError
from apps.matches.models import PRE_START, Match, MatchConfiguration, MatchParticipant, MatchState, MatchStatus
from apps.scoring.engine import initial_state, normalize_config
from apps.standings import services as standings

from .models import (
    Tournament, TournamentFixture, TournamentFormat, TournamentGroup, TournamentStatus,
)

ROUND_NAMES = {2: "Final", 4: "Semifinal", 8: "Quarterfinal", 16: "Round of 16", 32: "Round of 32", 64: "Round of 64"}


# ---------------------------------------------------------------- planning (pure)
def round_robin_rounds(team_ids):
    """Circle method. Returns list of rounds, each a list of (a, b) pairs."""
    teams = list(team_ids)
    if len(teams) % 2:
        teams.append(None)
    n = len(teams)
    rounds = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = teams[i], teams[n - 1 - i]
            if a is not None and b is not None:
                pairs.append((a, b) if r % 2 == 0 else (b, a))
        rounds.append(pairs)
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
    return rounds


def bracket_order(size):
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


def _active_regs(tournament):
    return list(tournament.registrations.filter(withdrawn=False).select_related("team").order_by("seed", "id"))


def plan_fixtures(tournament, options):
    """Pure plan from registrations + options. Raises ApiError on invalid input."""
    regs = _active_regs(tournament)
    seed = options.get("seed")
    if seed is None:
        seed = random.SystemRandom().randint(1, 10**9)
    if options.get("shuffle"):
        random.Random(seed).shuffle(regs)
    fmt = tournament.format
    team_ids = [r.team_id for r in regs]
    names = {r.team_id: r.team.name for r in regs}
    plan = {"seed": seed, "format": fmt, "groups": [], "group_fixtures": [], "knockout": [], "warnings": []}

    if fmt == TournamentFormat.SINGLE_ELIMINATION:
        if len(team_ids) < 2:
            raise ApiError("not_enough_teams", "At least 2 registered teams are required")
        plan["knockout"] = _plan_knockout(
            [{"type": "team", "team_id": t, "label": names[t]} for t in team_ids]
        )
        return plan, names

    n_groups = 1 if fmt == TournamentFormat.ROUND_ROBIN else int(options.get("groups_count") or tournament.groups_count)
    if n_groups < 1 or len(team_ids) < 2 * n_groups:
        raise ApiError("not_enough_teams", f"At least {2 * n_groups} registered teams are required for {n_groups} group(s)")
    letters = string.ascii_uppercase
    group_names = ["League"] if fmt == TournamentFormat.ROUND_ROBIN else [f"Group {letters[i]}" for i in range(n_groups)]
    buckets = [[] for _ in range(n_groups)]
    for i, t in enumerate(team_ids):  # snake distribution keeps groups balanced by seed
        row, col = divmod(i, n_groups)
        buckets[col if row % 2 == 0 else n_groups - 1 - col].append(t)
    for gi, (gname, teams) in enumerate(zip(group_names, buckets)):
        plan["groups"].append({"name": gname, "order": gi, "team_ids": teams, "teams": [names[t] for t in teams]})
        for rnd, pairs in enumerate(round_robin_rounds(teams), start=1):
            for pos, (a, b) in enumerate(pairs):
                plan["group_fixtures"].append({
                    "group": gname, "round": rnd, "position": pos, "team_a": a, "team_b": b,
                    "team_a_name": names[a], "team_b_name": names[b],
                })
    if fmt == TournamentFormat.GROUPS_KNOCKOUT:
        q = int(options.get("qualifiers_per_group") or tournament.qualifiers_per_group)
        if q < 1 or any(q > len(g["team_ids"]) for g in plan["groups"]):
            raise ApiError("invalid_qualifiers", "Qualifiers per group exceed the size of a group")
        if n_groups * q < 2:
            raise ApiError("invalid_qualifiers", "At least 2 teams must qualify for the knockout stage")
        seeds = []
        for rank in range(1, q + 1):
            for g in plan["groups"]:
                label = {1: "Winner", 2: "Runner-up"}.get(rank, f"{rank}th place") + f" {g['name']}"
                seeds.append({"type": "group_rank", "group": g["name"], "rank": rank, "label": label})
        plan["knockout"] = _plan_knockout(seeds)
        plan["qualifiers_per_group"] = q
    plan["groups_count"] = n_groups
    return plan, names


def _plan_knockout(seeds):
    n = len(seeds)
    size = 1 << max(1, math.ceil(math.log2(n)))
    order = bracket_order(size)
    rounds = int(math.log2(size))
    fixtures = []
    for i in range(size // 2):
        sa, sb = order[2 * i], order[2 * i + 1]
        a = seeds[sa - 1] if sa <= n else {"type": "bye", "label": "BYE"}
        b = seeds[sb - 1] if sb <= n else {"type": "bye", "label": "BYE"}
        fixtures.append({"round": 1, "position": i, "round_name": ROUND_NAMES.get(size, f"Round of {size}"),
                         "slot_a": a, "slot_b": b, "is_bye": a["type"] == "bye" or b["type"] == "bye"})
    for r in range(2, rounds + 1):
        count = size // (2 ** r)
        name = ROUND_NAMES.get(count * 2, f"Round of {count * 2}")
        for i in range(count):
            fixtures.append({
                "round": r, "position": i, "round_name": name, "is_bye": False,
                "slot_a": {"type": "winner", "label": f"Winner of {_fx_label(r - 1, 2 * i, size)}"},
                "slot_b": {"type": "winner", "label": f"Winner of {_fx_label(r - 1, 2 * i + 1, size)}"},
            })
    return fixtures


def _fx_label(rnd, pos, size):
    count = size // (2 ** rnd)
    name = ROUND_NAMES.get(count * 2, f"Round of {count * 2}")
    return f"{name} {pos + 1}" if count > 1 else name


def preview(tournament, options):
    plan, _ = plan_fixtures(tournament, options)
    existing = TournamentFixture.objects.filter(tournament=tournament).count()
    started = _started_fixture_count(tournament)
    plan["existing_fixtures"] = existing
    plan["has_started_matches"] = started > 0
    plan["counts"] = {"groups": len(plan["groups"]), "group_matches": len(plan["group_fixtures"]),
                      "knockout_matches": len([f for f in plan["knockout"] if not f["is_bye"]])}
    if existing and not started:
        plan["warnings"].append(f"{existing} existing fixture(s) will be replaced.")
    if started:
        plan["warnings"].append("Fixtures cannot be regenerated: some matches have already started or finished.")
    return plan


def _started_fixture_count(tournament):
    return Match.objects.filter(tournament=tournament).exclude(status__in=list(PRE_START)).count() + \
        MatchState.objects.filter(match__tournament=tournament, revision__gt=0).count()


# ---------------------------------------------------------------- persistence
def _match_name(fx, names=None):
    a = fx.team_a.name if fx.team_a else (fx.slot_a_source.get("label") or "TBD")
    b = fx.team_b.name if fx.team_b else (fx.slot_b_source.get("label") or "TBD")
    prefix = fx.round_name or (fx.group.name if fx.group else "Match")
    return f"{prefix}: {a} vs {b}"[:140]


def _config_for(tournament):
    cfg = normalize_config({**tournament.match_format, "firstServer": 0})
    return cfg


def _ensure_match(tournament, fx):
    """Create the Match for a fixture if it has none (also used when both slots are filled)."""
    if fx.match_id:
        return fx.match
    cfg = _config_for(tournament)
    match = Match.objects.create(tournament=tournament, name=_match_name(fx), status=MatchStatus.DRAFT,
                                 created_by=tournament.owner)
    MatchConfiguration.objects.create(
        match=match, best_of=cfg["bestOf"], golden_point=cfg["goldenPoint"], tiebreak_enabled=cfg["tiebreakEnabled"],
        tiebreak_at=cfg["tiebreakAt"], tiebreak_points=cfg["tiebreakPoints"], super_tiebreak=cfg["superTiebreak"],
        super_tiebreak_points=cfg["superTiebreakPoints"], games_per_set=cfg["gamesPerSet"], first_server=0,
    )
    MatchState.objects.create(match=match, revision=0, data=initial_state(cfg))
    fx.match = match
    fx.save(update_fields=["match"])
    return match


def _sync_match(fx):
    """Mirror the fixture's resolved teams into its match (only while the match has not started)."""
    if fx.is_bye:
        return
    match = _ensure_match(fx.tournament, fx)
    if match.status not in PRE_START or match.state.revision > 0:
        return
    team_changed = (match.team_a_id, match.team_b_id) != (fx.team_a_id, fx.team_b_id)
    match.team_a, match.team_b = fx.team_a, fx.team_b
    match.name = _match_name(fx)
    both = bool(fx.team_a_id and fx.team_b_id)
    if both and match.status == MatchStatus.DRAFT:
        match.status = MatchStatus.SCHEDULED.value
    elif not both and match.status in (MatchStatus.SCHEDULED, MatchStatus.READY):
        match.status = MatchStatus.DRAFT.value
    match.save()
    if team_changed:
        match.participants.all().delete()
        for idx, team in enumerate((fx.team_a, fx.team_b)):
            if not team:
                continue
            for tp in team.memberships.select_related("player").order_by("position"):
                MatchParticipant.objects.create(match=match, team_index=idx, position=tp.position, player=tp.player)


@transaction.atomic
def confirm_fixtures(tournament, options, actor=None):
    if tournament.status in (TournamentStatus.COMPLETED, TournamentStatus.CANCELLED, TournamentStatus.ARCHIVED):
        raise ConflictError("tournament_closed", f"Fixtures cannot be generated for a {tournament.status} tournament")
    existing = TournamentFixture.objects.filter(tournament=tournament).count()
    if existing:
        if _started_fixture_count(tournament):
            raise ConflictError("fixtures_in_progress",
                                "Fixtures cannot be regenerated because matches have already started or finished.")
        if not options.get("replace"):
            raise ConflictError("fixtures_exist", f"{existing} fixture(s) already exist. Confirm replacement to regenerate.",
                                details={"existing": existing})
        Match.objects.filter(tournament=tournament).delete()  # cascades fixtures' match links; none have events
        TournamentFixture.objects.filter(tournament=tournament).delete()
        tournament.groups.all().delete()
        tournament.registrations.update(group=None)
        tournament.standings.all().delete()

    plan, names = plan_fixtures(tournament, options)
    group_objs = {}
    for g in plan["groups"]:
        obj = TournamentGroup.objects.create(tournament=tournament, name=g["name"], order=g["order"])
        group_objs[g["name"]] = obj
        tournament.registrations.filter(team_id__in=g["team_ids"]).update(group=obj)

    from apps.teams.models import Team

    teams = {t.pk: t for t in Team.objects.filter(pk__in=names.keys()).prefetch_related("memberships__player")}
    for f in plan["group_fixtures"]:
        fx = TournamentFixture.objects.create(
            tournament=tournament, stage=TournamentFixture.Stage.GROUP, group=group_objs[f["group"]],
            round_number=f["round"], round_name=f"{f['group']} - Round {f['round']}", position=f["position"],
            team_a=teams[f["team_a"]], team_b=teams[f["team_b"]],
            slot_a_source={"type": "team", "team_id": f["team_a"]}, slot_b_source={"type": "team", "team_id": f["team_b"]},
        )
        _sync_match(fx)

    created = {}
    for f in plan["knockout"]:
        src_a, src_b = dict(f["slot_a"]), dict(f["slot_b"])
        for s in (src_a, src_b):
            if s["type"] == "group_rank":
                s["group_id"] = group_objs[s["group"]].pk
        fx = TournamentFixture.objects.create(
            tournament=tournament, stage=TournamentFixture.Stage.KNOCKOUT, round_number=f["round"],
            round_name=f["round_name"], position=f["position"], slot_a_source=src_a, slot_b_source=src_b,
            is_bye=f["is_bye"],
        )
        created[(f["round"], f["position"])] = fx
    for (rnd, pos), fx in created.items():  # link feeders -> next fixture
        nxt = created.get((rnd + 1, pos // 2))
        if nxt:
            fx.next_fixture = nxt
            fx.next_slot = pos % 2
            fx.save(update_fields=["next_fixture", "next_slot"])

    tournament.fixtures_confirmed_at = timezone.now()
    tournament.groups_count = plan.get("groups_count", tournament.groups_count)
    if "qualifiers_per_group" in plan:
        tournament.qualifiers_per_group = plan["qualifiers_per_group"]
    tournament.save()
    reconcile(tournament)
    return plan


# ---------------------------------------------------------------- reconciliation
def _winner_of(fx):
    if fx.is_bye:
        return fx.team_a_id or fx.team_b_id
    if fx.match_id and fx.match.status == MatchStatus.COMPLETED:
        return fx.match.winner_id
    return None


def _desired(fx, slot, feeders, rank_cache, tournament):
    feeder = feeders.get((fx.pk, slot))
    if feeder is not None:
        return _winner_of(feeder)
    src = fx.slot_a_source if slot == 0 else fx.slot_b_source
    if src.get("type") == "team":
        return src["team_id"]
    if src.get("type") == "group_rank":
        gid = src["group_id"]
        if gid not in rank_cache:
            rank_cache[gid] = None
            if standings.group_complete(tournament, gid):
                rank_cache[gid] = {s.rank: s.team_id for s in tournament.standings.filter(group_id=gid)}
        ranks = rank_cache[gid]
        return ranks.get(src["rank"]) if ranks else None
    return None


@transaction.atomic
def reconcile(tournament):
    """Make knockout slots, matches, champion and status consistent with current results."""
    standings.recompute(tournament)
    fixtures = list(
        TournamentFixture.objects.filter(tournament=tournament).select_related("match__state", "team_a", "team_b", "tournament")
        .order_by("round_number", "position")
    )
    ko = [f for f in fixtures if f.stage == TournamentFixture.Stage.KNOCKOUT]
    feeders = {(f.next_fixture_id, f.next_slot): f for f in ko if f.next_fixture_id}
    rank_cache = {}
    for fx in ko:  # ascending rounds, so winners of earlier rounds are already resolved
        changed = False
        for slot in (0, 1):
            want = _desired(fx, slot, feeders, rank_cache, tournament)
            attr = "team_a_id" if slot == 0 else "team_b_id"
            if getattr(fx, attr) != want:
                if fx.match_id and (fx.match.status not in PRE_START or fx.match.state.revision > 0):
                    raise ConflictError(
                        "downstream_started",
                        f"This change would alter {fx.round_name}, which has already started or finished.",
                    )
                setattr(fx, attr, want)
                changed = True
        if changed:
            fx.save(update_fields=["team_a", "team_b"])
        if not fx.is_bye and (changed or not fx.match_id):
            _sync_match(fx)
    _update_champion(tournament, fixtures)


def _update_champion(tournament, fixtures):
    champion = None
    ko = [f for f in fixtures if f.stage == TournamentFixture.Stage.KNOCKOUT]
    if ko:
        final = max(ko, key=lambda f: f.round_number)
        fresh = TournamentFixture.objects.select_related("match").get(pk=final.pk)
        champion = _winner_of(fresh)
    elif fixtures:
        if all(f.match_id and f.match.status == MatchStatus.COMPLETED for f in fixtures):
            top = tournament.standings.order_by("rank").first()
            champion = top.team_id if top else None
    tournament.refresh_from_db(fields=["status", "champion"])
    if tournament.champion_id != champion:
        tournament.champion_id = champion
        if champion and tournament.status == TournamentStatus.PUBLISHED:
            tournament.status = TournamentStatus.COMPLETED
        elif not champion and tournament.status == TournamentStatus.COMPLETED:
            tournament.status = TournamentStatus.PUBLISHED
        tournament.save(update_fields=["champion", "status", "updated_at"])


def on_match_completed(match):
    if match.tournament_id:
        reconcile(match.tournament)


def on_match_reopened(match):
    if match.tournament_id:
        reconcile(match.tournament)


def final_rankings(tournament):
    """Champion, runner-up, then semifinal losers (joint third) for knockout tournaments; else table order."""
    out = []
    ko = list(TournamentFixture.objects.filter(tournament=tournament, stage="knockout").select_related("match", "team_a", "team_b"))
    if ko and tournament.champion_id:
        final = max(ko, key=lambda f: f.round_number)
        if final.match and final.match.status == MatchStatus.COMPLETED:
            runner = final.team_b if final.match.winner_id == final.team_a_id else final.team_a
            out.append((1, tournament.champion))
            if runner:
                out.append((2, runner))
            semis = [f for f in ko if f.round_number == final.round_number - 1 and f.match and f.match.status == MatchStatus.COMPLETED]
            for f in semis:
                loser = f.team_b if f.match.winner_id == f.team_a_id else f.team_a
                if loser:
                    out.append((3, loser))
    elif not ko and tournament.champion_id:
        for s in tournament.standings.select_related("team").order_by("rank"):
            out.append((s.rank, s.team))
    return out
