import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.matches.models import Match
from apps.notifications.outbox import match_group

from .snapshot import build_snapshot, load_match
from .visibility import is_match_public

MAX_MSG_BYTES = 512


@database_sync_to_async
def _public_snapshot(public_id):
    try:
        match = load_match(public_id=public_id)
    except Match.DoesNotExist:
        return None
    if not is_match_public(match):
        return None
    return build_snapshot(match)


class MatchConsumer(AsyncJsonWebsocketConsumer):
    """Read-only spectator feed. Only public-safe snapshots are ever sent. Clients cannot write."""

    async def connect(self):
        self.public_id = self.scope["url_route"]["kwargs"]["public_id"]
        self.group = match_group(self.public_id)
        self.last_revision = -1
        self.checked_at = time.monotonic()
        snap = await _public_snapshot(self.public_id)
        if snap is None:
            await self.close(code=4404)
            return
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self._send_snapshot(snap)

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # Only keep-alive and resync requests are understood; everything else is ignored.
        kind = content.get("type") if isinstance(content, dict) else None
        if kind == "ping":
            await self.send_json({"type": "pong"})
        elif kind == "sync":
            snap = await _public_snapshot(self.public_id)
            if snap is None:
                await self.close(code=4404)
            else:
                await self.send_json({"type": "snapshot", "snapshot": snap})
                self.last_revision = max(self.last_revision, snap["revision"])

    async def _send_snapshot(self, snap):
        self.last_revision = snap["revision"]
        await self.send_json({"type": "snapshot", "snapshot": snap})

    async def match_update(self, event):
        snap = event["payload"]
        if snap["revision"] <= self.last_revision:
            return  # stale or duplicate message: never let old state overwrite newer state
        # Payload is already public-safe; re-check visibility at most every 30s so that a match whose
        # tournament gets un-published stops streaming without a DB hit per spectator per update.
        if time.monotonic() - self.checked_at > 30:
            self.checked_at = time.monotonic()
            if not await _still_public(self.public_id):
                await self.close(code=4404)
                return
        self.last_revision = snap["revision"]
        await self.send_json({"type": "update", "snapshot": snap})


@database_sync_to_async
def _still_public(public_id):
    try:
        return is_match_public(load_match(public_id=public_id))
    except Match.DoesNotExist:
        return False
