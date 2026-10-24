import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator

from apps.notifications.models import OutboxEvent
from apps.notifications.outbox import dispatch_pending
from config.asgi import application
from tests.conftest import play_points

pytestmark = pytest.mark.django_db(transaction=True)


def connect(public_id):
    comm = WebsocketCommunicator(application, f"/ws/matches/{public_id}/")
    return comm


def test_spectator_receives_snapshot_then_updates(admin_api, make_match):
    m = make_match()

    async def run():
        comm = connect(m["publicId"])
        ok, _ = await comm.connect()
        assert ok
        first = await comm.receive_json_from()
        assert first["type"] == "snapshot" and first["snapshot"]["revision"] == 0
        await comm.disconnect()

    async_to_sync(run)()


def test_update_is_pushed_after_commit(admin_api, make_match):
    m = make_match()

    async def run():
        comm = connect(m["publicId"])
        await comm.connect()
        await comm.receive_json_from()
        from asgiref.sync import sync_to_async

        await sync_to_async(play_points)(admin_api, m["id"], [0, 1])
        a = await comm.receive_json_from()
        b = await comm.receive_json_from()
        assert (a["type"], a["snapshot"]["revision"]) == ("update", 1)
        assert b["snapshot"]["revision"] == 2 and b["snapshot"]["state"]["points"] == [1, 1]
        await comm.disconnect()

    async_to_sync(run)()


def test_snapshots_never_contain_private_data(admin_api, make_match):
    m = make_match()

    async def run():
        comm = connect(m["publicId"])
        await comm.connect()
        msg = await comm.receive_json_from()
        raw = str(msg).lower()
        assert "umpire" not in raw and "email" not in raw
        await comm.disconnect()

    async_to_sync(run)()


def test_private_or_unknown_matches_refuse_connection(admin_api, make_match):
    hidden = make_match(is_public=False)

    async def run():
        for pid in (hidden["publicId"], "NOSUCHMATCH"):
            comm = connect(pid)
            ok, code = await comm.connect()
            assert not ok and code == 4404

    async_to_sync(run)()


def test_stale_messages_are_dropped_and_sync_recovers(admin_api, make_match):
    m = make_match()
    play_points(admin_api, m["id"], [0, 0, 0])

    async def run():
        from channels.layers import get_channel_layer

        comm = connect(m["publicId"])
        await comm.connect()
        first = await comm.receive_json_from()
        assert first["snapshot"]["revision"] == 3  # late joiner gets the authoritative state immediately
        layer = get_channel_layer()
        stale = dict(first["snapshot"], revision=1)
        await layer.group_send(f"match_{m['publicId']}", {"type": "match.update", "payload": stale})
        assert await comm.receive_nothing(timeout=0.2)  # stale revision ignored
        await comm.send_json_to({"type": "sync"})
        again = await comm.receive_json_from()
        assert again["snapshot"]["revision"] == 3
        await comm.send_json_to({"type": "ping"})
        assert (await comm.receive_json_from())["type"] == "pong"
        await comm.disconnect()

    async_to_sync(run)()


def test_outbox_rows_written_and_redelivery(admin_api, make_match):
    m = make_match()
    play_points(admin_api, m["id"], [0])
    assert OutboxEvent.objects.filter(group=f"match_{m['publicId']}").count() == 1
    OutboxEvent.objects.update(dispatched_at=None)  # simulate a publish that failed (e.g. Redis down)
    assert dispatch_pending() == 1
    assert OutboxEvent.objects.filter(dispatched_at__isnull=True).count() == 0


def test_rest_keeps_working_if_channel_layer_fails(admin_api, make_match, monkeypatch):
    m = make_match()

    def boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr("apps.notifications.outbox.get_channel_layer", boom)
    r = play_points(admin_api, m["id"], [0, 0])
    assert r["revision"] == 2
    assert OutboxEvent.objects.filter(dispatched_at__isnull=True).count() == 2  # retained for retry
