import os

from django.core.management.base import BaseCommand

from apps.accounts.models import Role, User


class Command(BaseCommand):
    help = "Create the initial Super Admin from ADMIN_USERNAME / ADMIN_EMAIL / ADMIN_PASSWORD (idempotent; never overwrites)."

    def handle(self, *args, **opts):
        username = os.environ.get("ADMIN_USERNAME")
        password = os.environ.get("ADMIN_PASSWORD")
        email = os.environ.get("ADMIN_EMAIL", f"{username}@example.com" if username else "")
        if not (username and password):
            self.stdout.write("ADMIN_USERNAME/ADMIN_PASSWORD not set; skipping.")
            return
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Admin '{username}' already exists; leaving untouched.")
            return
        User.objects.create_user(username=username, email=email, password=password, role=Role.SUPER_ADMIN,
                                 is_staff=True, is_superuser=True)
        self.stdout.write(self.style.SUCCESS(f"Created Super Admin '{username}'."))
