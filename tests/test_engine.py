import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from stickerbot.config import Config
from stickerbot.engine import Hoover, pack_name, emoji_from_caption, DUP, OK
from stickerbot.store import Store

from fakebot import FakeBot, bad, flood


def run(coro):
    return asyncio.run(coro)


class Env:
    def __init__(self, tmp_path, limit=120, **cfg):
        self.bot = FakeBot(limit=limit)
        self.cfg = Config(bot_token="x", owner_id=42, data_dir=tmp_path, pack_limit=limit,
                          legacy_state=tmp_path / "pack_state.json", **cfg)
        self.store = Store(self.cfg.db_path)
        self.slept = []

        async def fake_sleep(s):
            self.slept.append(s)

        self.h = Hoover(self.bot, self.cfg, self.store, sleep=fake_sleep)

    async def start(self):
        await self.h.start()

    def sticker(self, n, fmt="static", **kw):
        j = {"kind": "sticker", "file_id": f"src{n}", "uid": f"su{n}", "format": fmt, "emoji": "😀",
             "chat_id": -100, "message_id": n}
        j.update(kw)
        return j

    async def feed(self, *jobs):
        for j in jobs:
            self.h.enqueue(j)
        while await self.h.run_once():
            pass

    def pack_sizes(self):
        return {n: len(s["stickers"]) for n, s in self.bot.sets.items()}


def test_first_sticker_creates_pack_with_exactly_one(tmp_path):
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(1))

    run(go())
    assert e.pack_sizes() == {"stickies_1_by_hooverbot": 1}
    assert e.bot.reactions == [(-100, 1, "👍")]
    assert "New pack created" in e.bot.messages[0][1]


def test_mixed_formats_share_one_pack(tmp_path):
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(1, "static"), e.sticker(2, "animated"), e.sticker(3, "video"), e.sticker(4, "static"))

    run(go())
    assert e.pack_sizes() == {"stickies_1_by_hooverbot": 4}


def test_duplicates_are_skipped(tmp_path):
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(1), e.sticker(1, message_id=2))

    run(go())
    assert e.pack_sizes() == {"stickies_1_by_hooverbot": 1}
    assert [r[2] for r in e.bot.reactions] == ["👍", "👌"]


def test_flood_control_is_waited_out_not_dropped(tmp_path):
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(1))
        e.bot.fail_next["add"] = [flood(7), flood(3)]
        await e.feed(e.sticker(2))

    run(go())
    assert e.pack_sizes() == {"stickies_1_by_hooverbot": 2}
    assert 7.5 in e.slept and 3.5 in e.slept
    assert [r[2] for r in e.bot.reactions] == ["👍", "👍"]


def test_rolls_over_when_local_count_hits_limit(tmp_path):
    e = Env(tmp_path, limit=3)

    async def go():
        await e.start()
        await e.feed(*[e.sticker(i) for i in range(1, 8)])

    run(go())
    assert e.pack_sizes() == {"stickies_1_by_hooverbot": 3, "stickies_2_by_hooverbot": 3, "stickies_3_by_hooverbot": 1}


def test_rolls_over_when_telegram_says_full(tmp_path):
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(1))
        # someone filled the pack outside the bot; local count is stale
        e.bot.fail_next["add"] = [bad("STICKERS_TOO_MUCH")]
        await e.feed(e.sticker(2))

    run(go())
    assert e.pack_sizes() == {"stickies_1_by_hooverbot": 1, "stickies_2_by_hooverbot": 1}


def test_rejected_file_id_falls_back_to_upload(tmp_path):
    e = Env(tmp_path)
    img = io.BytesIO()
    Image.new("RGBA", (300, 200), (255, 0, 0, 128)).save(img, "PNG")
    e.bot.files["src1"] = img.getvalue()
    e.bot.reject_file_ids = True

    async def go():
        await e.start()
        await e.feed(e.sticker(1))

    run(go())
    st = e.bot.sets["stickies_1_by_hooverbot"]["stickers"]
    assert len(st) == 1 and st[0].source == "upload"


def test_permanent_failure_gets_thumbs_down_and_is_not_marked_seen(tmp_path):
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(1))
        e.bot.fail_next["add"] = [bad("SOMETHING_WEIRD")]
        await e.feed(e.sticker(2, private=True))

    run(go())
    assert [r[2] for r in e.bot.reactions] == ["👍", "👎"]
    assert not e.store.is_seen("su2")
    assert any("SOMETHING_WEIRD" in m[1] for m in e.bot.messages)


def test_discovery_continues_existing_packs_with_gaps(tmp_path):
    e = Env(tmp_path, limit=3)
    from types import SimpleNamespace as NS
    mk = lambda u: NS(file_id="f" + u, file_unique_id=u, type="regular", is_animated=False, is_video=False, emoji="x")
    e.bot.sets["stickies_1_by_hooverbot"] = {"title": "Stickies 1", "stickers": [mk("a"), mk("b"), mk("c")]}
    e.bot.sets["stickies_3_by_hooverbot"] = {"title": "Stickies 3", "stickers": [mk("d")]}

    async def go():
        await e.start()
        await e.feed(e.sticker(1), {"kind": "sticker", "file_id": "fa", "uid": "a", "format": "static"})

    run(go())
    assert e.pack_sizes()["stickies_3_by_hooverbot"] == 2  # continued the newest pack
    assert "stickies_2_by_hooverbot" not in e.bot.sets


def test_legacy_state_imported(tmp_path):
    (tmp_path / "pack_state.json").write_text(json.dumps(
        {"index": 1, "count": 0, "current_pack": "", "is_animated": False, "seen": ["old1", "old2"]}))
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(9, uid="old1"))

    run(go())
    assert e.bot.sets == {}
    assert e.store.is_seen("old2")


def test_jobs_survive_restart(tmp_path):
    e = Env(tmp_path)
    e.store.enqueue(e.sticker(1))
    e2 = Env(tmp_path)

    async def go():
        await e2.start()
        while await e2.h.run_once():
            pass

    run(go())
    assert e2.pack_sizes() == {"stickies_1_by_hooverbot": 1}


def test_network_errors_defer_job_instead_of_dropping(tmp_path):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetMe
    e = Env(tmp_path)

    async def go():
        await e.start()
        e.bot.fail_next["create"] = [TelegramNetworkError(GetMe(), "boom")] * 6
        e.h.enqueue(e.sticker(1))
        await e.h.run_once()

    run(go())
    assert e.store.queue_size() == 1  # still queued for later
    assert e.bot.reactions == []


def test_grab_batch_reports_summary(tmp_path):
    e = Env(tmp_path)

    async def go():
        await e.start()
        b = e.h.start_batch(-5, "Cool Pack", 3)
        await e.feed(*[e.sticker(i, chat_id=-5, message_id=None, batch=b) for i in (1, 2, 1)])

    run(go())
    assert any("2 added, 1 already had, 0 failed" in m[1] for m in e.bot.messages)


def test_undo_removes_and_unsees(tmp_path):
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(1), e.sticker(2))
        row = e.store.last_sticker()
        await e.h.remove(row["pack_file_id"], row["pack_uid"])

    run(go())
    assert e.pack_sizes() == {"stickies_1_by_hooverbot": 1}
    assert not e.store.is_seen("su2")


def test_troll_only_when_enabled(tmp_path):
    e = Env(tmp_path, luke_id=7)

    async def go():
        await e.start()
        await e.feed(e.sticker(1, troll=True), e.sticker(2))

    run(go())
    trolls = [m for m in e.bot.messages if "Luke" in m[1]]
    assert len(trolls) == 1


def test_pack_name_keeps_suffix():
    n = pack_name("A" * 80, 12, "My_Long_Bot")
    assert len(n) <= 64 and n.endswith("_12_by_my_long_bot") and n[0].isalpha()
    assert pack_name("9 lives!!", 1, "b") == "s9lives_1_by_b"


def test_emoji_from_caption():
    assert emoji_from_caption("🔥 lol") == "🔥"
    assert emoji_from_caption("lol") is None
    assert emoji_from_caption(None) is None


def test_refresh_failure_after_add_does_not_add_twice(tmp_path):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetMe
    e = Env(tmp_path)

    async def go():
        await e.start()
        await e.feed(e.sticker(1))
        # add succeeds, then the follow-up get_sticker_set keeps failing
        e.bot.fail_next["get_sticker_set"] = [TelegramNetworkError(GetMe(), "boom")] * 6
        await e.feed(e.sticker(2))
        await e.feed(e.sticker(2, message_id=3))  # re-sent: must be a dupe

    run(go())
    assert e.pack_sizes() == {"stickies_1_by_hooverbot": 2}
    assert [r[2] for r in e.bot.reactions] == ["👍", "👍", "👌"]


def test_stale_listing_does_not_record_wrong_sticker_for_undo(tmp_path):
    e = Env(tmp_path)
    real_get = e.bot.get_sticker_set

    async def go():
        await e.start()
        await e.feed(e.sticker(1))
        first = e.store.last_sticker()["pack_file_id"]

        async def lagging(name):  # returns the set as it was before the add
            s = await real_get(name)
            s.stickers = s.stickers[:-1]
            return s
        e.bot.get_sticker_set = lagging
        await e.feed(e.sticker(2))
        return first

    first = run(go())
    last = e.store.last_sticker()
    assert last["source_uid"] == "su2" and last["pack_file_id"] == ""
    assert first != ""
