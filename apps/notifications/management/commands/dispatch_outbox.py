import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.notifications.models import OutboxEvent
from apps.notifications.outbox import dispatch_pending


class Command(BaseCommand):
    help = "Publish pending outbox events; --loop keeps running; old dispatched rows are pruned."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=float, default=2.0)

    def handle(self, *args, **opts):
        while True:
            n = dispatch_pending()
            OutboxEvent.objects.filter(dispatched_at__lt=timezone.now() - timedelta(days=2)).delete()
            if n:
                self.stdout.write(f"dispatched {n}")
            if not opts["loop"]:
                break
            time.sleep(opts["interval"])
