"""Demo data for local development / staging. Idempotent; refuses to run in production without --force."""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.matches.models import Court, Match
from apps.matches.services import create_match
from apps.players.models import Player
from apps.scoring import services as scoring
from apps.teams.models import Team, TeamPlayer
from apps.tournaments import services as tservices
from apps.tournaments.models import Tournament, TournamentFixture, TournamentRegistration

NAMES = [
    ("Carlos", "Mendez"), ("Diego", "Alvarez"), ("Lucia", "Fernandez"), ("Marta", "Soler"),
    ("Pablo", "Navarro"), ("Javier", "Ortega"), ("Elena", "Ruiz"), ("Sofia", "Castro"),
    ("Mateo", "Vidal"), ("Nico", "Herrera"), ("Ana", "Molina"), ("Irene", "Gil"),
    ("Hugo", "Prieto"), ("Raul", "Santos"), ("Paula", "Romero"), ("Clara", "Dominguez"),
]
TEAM_NAMES = ["Smash Brothers", "Vibora Club", "Net Ninjas", "Bandeja Kings", "Lob Masters", "Wall Walkers",
              "Padel Pirates", "Golden Point"]


class Command(BaseCommand):
    help = "Seed demo users, players, teams, courts, a tournament and a live friendly match."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--password", default="Padel#2026demo")

    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["force"]:
            raise CommandError("Refusing to seed demo data outside DEBUG without --force")
        pw = opts["password"]
        users = {}
        for username, role in (("admin", Role.SUPER_ADMIN), ("organizer", Role.TOURNAMENT_ADMIN),
                               ("umpire1", Role.UMPIRE), ("umpire2", Role.UMPIRE)):
            u, created = User.objects.get_or_create(username=username, defaults={
                "email": f"{username}@padel.local", "role": role, "first_name": username.title(),
                "is_staff": role == Role.SUPER_ADMIN, "is_superuser": role == Role.SUPER_ADMIN})
            if created:
                u.set_password(pw)
                u.save()
            users[username] = u
        courts = [Court.objects.get_or_create(name=f"Court {i}", defaults={"venue": "Main Club"})[0] for i in (1, 2, 3)]

        if Tournament.objects.filter(slug="demo-padel-open").exists():
            self.stdout.write("Demo data already present.")
            self._summary(pw)
            return

        players = [Player.objects.create(first_name=f, last_name=l, created_by=users["admin"]) for f, l in NAMES]
        teams = []
        for i, name in enumerate(TEAM_NAMES):
            t = Team.objects.create(name=name, created_by=users["admin"])
            for pos, p in enumerate(players[i * 2:i * 2 + 2], start=1):
                TeamPlayer.objects.create(team=t, player=p, position=pos)
            teams.append(t)

        today = timezone.localdate()
        t = Tournament.objects.create(
            name="Demo Padel Open", slug="demo-padel-open", venue="Main Club, Marbella", organizer="Padel Federation",
            description="Eight pairs, two groups, one champion. A sample tournament to explore the system.",
            rules="Best of three sets with a deciding super tie-break. Golden point is not used.",
            start_date=today, end_date=today + timedelta(days=2), format="groups_knockout",
            owner=users["organizer"], status="draft",
        )
        for i, team in enumerate(teams, start=1):
            TournamentRegistration.objects.create(tournament=t, team=team, seed=i)
        tservices.confirm_fixtures(t, {"seed": 3, "groups_count": 2, "qualifiers_per_group": 2}, users["organizer"])
        t.status = "published"
        t.save()

        base = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        fixtures = list(TournamentFixture.objects.filter(tournament=t, stage="group").select_related("match"))
        for i, fx in enumerate(fixtures):
            fx.match.court = courts[i % 3]
            fx.match.umpire = users["umpire1" if i % 2 == 0 else "umpire2"]
            fx.match.scheduled_at = base + timedelta(hours=2 * (i // 3))
            fx.match.save()
        # play a handful of group matches so standings are not empty
        for n, fx in enumerate(fixtures[:5]):
            winner = n % 2
            games = [[6, 3], [6, 4]] if winner == 0 else [[3, 6], [4, 6]]
            scoring.perform_action(fx.match_id, users["organizer"], "result", f"seed-result-{fx.match_id:05d}",
                                   sets=[{"games": g} for g in games], reason="Seed data")
        # one live match scored point-by-point
        live_fx = fixtures[5]
        for n in range(11):
            scoring.perform_action(live_fx.match_id, users["umpire1"], "point", f"seed-live-{n:06d}", team=0 if n % 3 else 1)

        friendly = create_match({
            "name": "Friendly: Coaches vs Juniors",
            "team_a": {"players": [{"first_name": "Coach", "last_name": "Alex"}, {"first_name": "Coach", "last_name": "Bea"}]},
            "team_b": {"players": [{"first_name": "Junior", "last_name": "Cal"}, {"first_name": "Junior", "last_name": "Dee"}]},
            "court": courts[2], "umpire": users["umpire2"], "scheduled_at": base + timedelta(hours=8),
            "config": {"goldenPoint": True, "bestOf": 1},
        }, users["admin"])
        self.stdout.write(f"Friendly match public id: {friendly.public_id}")
        self._summary(pw)

    def _summary(self, pw):
        self.stdout.write(self.style.SUCCESS(
            f"Users: admin / organizer / umpire1 / umpire2   password: {pw}\nMatches: {Match.objects.count()}"))
