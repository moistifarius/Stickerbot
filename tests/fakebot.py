"""In-memory stand-in for aiogram.Bot covering the methods the hoover uses."""
from __future__ import annotations

import io
import itertools
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import GetMe

_ids = itertools.count(1)
M = GetMe()  # any method object will do for exception construction


def bad(msg: str) -> TelegramBadRequest:
    return TelegramBadRequest(M, f"Bad Request: {msg}")


def flood(sec: int = 3) -> TelegramRetryAfter:
    return TelegramRetryAfter(M, "Flood control exceeded", sec)


class FakeBot:
    def __init__(self, username="hooverbot", limit=120):
        self.username = username
        self.limit = limit
        self.sets: dict[str, dict] = {}  # name -> {"title", "stickers": [ns]}
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple] = []
        self.fail_next: dict[str, list[Exception]] = {}  # method -> queued exceptions
        self.reject_file_ids = False
        self.reactions: list[tuple] = []
        self.messages: list[tuple] = []

    def _maybe_fail(self, method: str):
        q = self.fail_next.get(method)
        if q:
            raise q.pop(0)

    def _mk(self, inp, fmt=None):
        n = next(_ids)
        src = inp.sticker
        kind = "upload" if not isinstance(src, str) else "file_id"
        return SimpleNamespace(file_id=f"pk{n}", file_unique_id=f"pu{n}", type="regular",
                               is_animated=(inp.format == "animated"), is_video=(inp.format == "video"),
                               emoji=inp.emoji_list[0], source=kind, format=inp.format)

    def _check_input(self, inp):
        if isinstance(inp.sticker, str) and self.reject_file_ids:
            raise bad("STICKER_PNG_DIMENSIONS")

    async def get_me(self):
        return SimpleNamespace(username=self.username, id=999)

    async def get_sticker_set(self, name):
        self.calls.append(("get_sticker_set", name))
        self._maybe_fail("get_sticker_set")
        if name not in self.sets:
            raise bad("STICKERSET_INVALID")
        s = self.sets[name]
        return SimpleNamespace(name=name, title=s["title"], stickers=list(s["stickers"]))

    async def create_new_sticker_set(self, user_id, name, title, stickers):
        self.calls.append(("create", name, len(stickers)))
        self._maybe_fail("create")
        if name in self.sets:
            raise bad("SHORTNAME_OCCUPY_FAILED")
        for i in stickers:
            self._check_input(i)
        self.sets[name] = {"title": title, "stickers": [self._mk(i) for i in stickers]}
        return True

    async def add_sticker_to_set(self, user_id, name, sticker):
        self.calls.append(("add", name, sticker.format))
        self._maybe_fail("add")
        if name not in self.sets:
            raise bad("STICKERSET_INVALID")
        if len(self.sets[name]["stickers"]) >= self.limit:
            raise bad("STICKERS_TOO_MUCH")
        self._check_input(sticker)
        self.sets[name]["stickers"].append(self._mk(sticker))
        return True

    async def delete_sticker_from_set(self, sticker):
        self.calls.append(("delete", sticker))
        for s in self.sets.values():
            for st in s["stickers"]:
                if st.file_id == sticker:
                    s["stickers"].remove(st)
                    return True
        raise bad("STICKER_INVALID")

    async def get_file(self, file_id):
        return SimpleNamespace(file_id=file_id, file_path=f"path/{file_id}", file_size=len(self.files.get(file_id, b"")))

    async def download_file(self, file_path, destination):
        destination.write(self.files[file_path.split("/", 1)[1]])

    async def set_message_reaction(self, chat_id, message_id, reaction):
        self.reactions.append((chat_id, message_id, reaction[0].emoji))

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))
