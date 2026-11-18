"""Drive the real aiogram Dispatcher with a Bot whose network layer is replaced."""
import asyncio
import itertools
import time
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot
from aiogram.types import Update

from stickerbot.bot import build, memory_handler
from stickerbot.config import Config
from stickerbot.store import Store

from fakebot import FakeBot

OWNER, FRIEND, RANDO = 42, 43, 44
_upd = itertools.count(1)


class RecBot(Bot):
    def __init__(self, fake: FakeBot):
        super().__init__("123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.fake = fake
        self.sent = []

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        self.sent.append((name, method))
        f = self.fake
        if name == "GetMe":
            return await f.get_me()
        if name == "GetStickerSet":
            return await f.get_sticker_set(method.name)
        if name == "AddStickerToSet":
            return await f.add_sticker_to_set(method.user_id, method.name, method.sticker)
        if name == "CreateNewStickerSet":
            return await f.create_new_sticker_set(method.user_id, method.name, method.title, method.stickers)
        if name == "DeleteStickerFromSet":
            return await f.delete_sticker_from_set(method.sticker)
        if name == "GetFile":
            return await f.get_file(method.file_id)
        return True

    async def download_file(self, file_path, destination=None, **kw):
        await self.fake.download_file(file_path, destination)
        return destination

    def texts(self):
        return [m.text for n, m in self.sent if n == "SendMessage"]

    def reacts(self):
        return [m.reaction[0].emoji for n, m in self.sent if n in ("SetMessageReaction",)]


class H:
    def __init__(self, tmp_path):
        self.fake = FakeBot()
        self.bot = RecBot(self.fake)
        self.cfg = Config(bot_token="x", owner_id=OWNER, data_dir=tmp_path, allowed_users={FRIEND})
        self.store = Store(self.cfg.db_path)
        self.dp, self.h = build(self.cfg, self.bot, self.store)

        async def nosleep(s):
            pass
        self.h.sleep = nosleep

    def msg(self, user=OWNER, chat=-100, text=None, reply=None, **extra):
        i = next(_upd)
        m = {"message_id": i, "date": int(time.time()),
             "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup", "title": "g"},
             "from": {"id": user, "is_bot": False, "first_name": "u"}}
        if text:
            m["text"] = text
            if text.startswith("/"):
                m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply:
            m["reply_to_message"] = reply
        m.update(extra)
        return m

    @staticmethod
    def sticker(uid, set_name="theirs", video=False):
        return {"sticker": {"file_id": "F" + uid, "file_unique_id": uid, "type": "regular", "width": 512,
                            "height": 512, "is_animated": False, "is_video": video, "emoji": "😂",
                            "set_name": set_name}}

    async def send(self, m):
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id": next(_upd), "message": m}))
        return m

    async def drain(self):
        while await self.h.run_once():
            pass


def run(c):
    return asyncio.run(c)


def test_group_sticker_hoovered_then_packs_status_logs(tmp_path):
    t = H(tmp_path)

    async def go():
        await t.h.start()
        await t.send(t.msg(user=RANDO, **t.sticker("a")))
        await t.drain()
        await t.send(t.msg(text="/packs"))
        await t.send(t.msg(text="/status"))
        await t.send(t.msg(user=RANDO, text="/status"))
        import logging
        logging.getLogger("stickerbot").addHandler(memory_handler)
        logging.getLogger("stickerbot").warning("<b>not html</b>")
        await t.send(t.msg(text="/logs 5"))

    run(go())
    texts = t.bot.texts()
    assert any("New pack created" in x for x in texts)
    packs = [x for x in texts if "Packs" in x][0]
    assert "stickies_1_by_hooverbot" in packs and "1/120" in packs
    assert sum("Status" in x for x in texts) == 1  # rando gets nothing
    status = [x for x in texts if "Status" in x][0]
    assert "stickies_1_by_hooverbot (1)" in status
    logs = [x for x in texts if x.startswith("<pre>")][0]
    assert "&lt;b&gt;not html" in logs
    assert "👍" in t.bot.reacts()


def test_grab_whole_pack(tmp_path):
    t = H(tmp_path)
    ns = lambda u: SimpleNamespace(file_id="f" + u, file_unique_id=u, type="regular", is_animated=False,
                                   is_video=False, emoji="🙂")
    t.fake.sets["coolpack"] = {"title": "Cool", "stickers": [ns("x"), ns("y"), ns("z")]}

    async def go():
        await t.h.start()
        t.store.mark_seen("y")
        await t.send(t.msg(user=FRIEND, text="/grab", reply=t.msg(user=RANDO, **t.sticker("x", "coolpack"))))
        await t.drain()
        await t.send(t.msg(user=RANDO, text="/grab https://t.me/addstickers/coolpack"))  # untrusted: ignored

    run(go())
    texts = t.bot.texts()
    assert any("Hoovering 2 stickers" in x for x in texts)
    assert any("2 added, 0 already had, 0 failed" in x for x in texts)
    assert len(t.fake.sets["stickies_1_by_hooverbot"]["stickers"]) == 2
    assert sum("Hoovering" in x for x in texts) == 1


def test_steal_photo_in_group_and_dm_media_rules(tmp_path):
    import io
    from PIL import Image
    t = H(tmp_path)
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "red").save(buf, "JPEG")
    t.fake.files["PH1"] = buf.getvalue()
    photo = {"photo": [{"file_id": "PH1", "file_unique_id": "ph1", "width": 800, "height": 600}]}

    async def go():
        await t.h.start()
        await t.send(t.msg(user=FRIEND, text="/steal", reply=t.msg(user=RANDO, **photo)))
        await t.drain()
        await t.send(t.msg(user=RANDO, chat=RANDO, **photo))   # untrusted DM
        await t.send(t.msg(user=OWNER, chat=OWNER, **photo))   # dupe now

    run(go())
    st = t.fake.sets["stickies_1_by_hooverbot"]["stickers"]
    assert len(st) == 1 and st[0].source == "upload"
    assert any("Only the owner" in x for x in t.bot.texts())
    assert t.store.queue_size() == 0


def test_undo_and_remove(tmp_path):
    t = H(tmp_path)

    async def go():
        await t.h.start()
        for u in ("a", "b", "c"):
            await t.send(t.msg(user=RANDO, **t.sticker(u)))
        await t.drain()
        await t.send(t.msg(text="/undo"))
        first = t.fake.sets["stickies_1_by_hooverbot"]["stickers"][0]
        ours = t.sticker(first.file_unique_id, "stickies_1_by_hooverbot")
        ours["sticker"]["file_id"] = first.file_id
        await t.send(t.msg(text="/remove", reply=t.msg(user=RANDO, **ours)))
        await t.send(t.msg(user=RANDO, text="/undo"))  # not owner: ignored

    run(go())
    left = t.fake.sets["stickies_1_by_hooverbot"]["stickers"]
    assert len(left) == 1
    assert not t.store.is_seen("c") and not t.store.is_seen("a")
    assert any("Removed the last sticker" in x for x in t.bot.texts())
    assert any("Removed 🗑" in x for x in t.bot.texts())
