#!/usr/bin/env python3
"""
Async Sticker Hoover Bot (aiogram 3.7 • Bot API 7.x)
===================================================
✅ Dedup across packs   🛠 Self‑healing   🚀 Auto‑resize   👍/👎 Ack   📜 /logs command

Changelog 2025‑07‑11 — *thumbs patch*
------------------------------------
* **Thumbs‑up** 👍 reply when a sticker is successfully added.
* **Thumbs‑down** 👎 reply when the bot tried but couldn’t add (e.g. resize
  failed or pack vanished after retry limit).
* Fixed `/logs` decorator for aiogram 3 (`commands=["logs"]`).
* Ring‑buffer logger remains (last 200 lines).

Env vars unchanged.
"""

import asyncio
import io
import json
import logging
import os
import random
import re
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Set, Union

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputSticker

try:
    from PIL import Image  # type: ignore
except ImportError:
    Image = None

# ---------------------------------------------------------------------------
# Logging (console + ring buffer for /logs)
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("sticker-hoover")

_RECENT: Deque[str] = deque(maxlen=200)
class _MemHandler(logging.Handler):
    def emit(self, record):
        _RECENT.append(self.format(record))

log.addHandler(_MemHandler())

# ---------------------------------------------------------------------------
# Config & env
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
if not BOT_TOKEN or not OWNER_ID:
    raise SystemExit("Must set BOT_TOKEN and OWNER_ID env vars")
OWNER_ID = int(OWNER_ID)

LUKE_ID = int(os.getenv("LUKE_ID", "0"))
PACK_BASENAME = os.getenv("PACK_BASENAME", "stickies")
DATA_FILE = Path(os.getenv("DATA_FILE", "pack_state.json"))
EXTRA_PACKS = [p.strip() for p in os.getenv("EXTRA_PACKS", "").split(",") if p.strip()]

MAX_STATIC = 120
MAX_ANIM = 50
MAX_SIDE = 512  # max width/height for static

TROLLS = [
    "Yo <a href='tg://user?id={luke}'>Luke</a>, another one for you. 10‑second job, remember?",
    "Adding stickers so Luke doesn't have to. Classic.",
    "<a href='tg://user?id={luke}'>Luke</a> could’ve done this in his sleep by now 😂",
    "Luke, buddy, this is what procrastination looks like in JSON.",
]

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _blank() -> Dict[str, Any]:
    return {"index": 1, "count": 0, "current_pack": "", "is_animated": False, "seen": []}

state: Dict[str, Any]
if DATA_FILE.exists():
    state = json.loads(DATA_FILE.read_text())
    state.setdefault("seen", [])
else:
    state = _blank()

_seen: Set[str] = set(state["seen"])

def _save() -> None:
    DATA_FILE.write_text(json.dumps(state, indent=2))

# ---------------------------------------------------------------------------
# Bot + Dispatcher
# ---------------------------------------------------------------------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------------------------------------------------------------------------
# Helper utils
# ---------------------------------------------------------------------------

def _tg_fmt(st: types.Sticker) -> str:
    return "animated" if st.is_animated else "video" if st.is_video else "static"


def _slug(base: str, user: str, start: int) -> str:
    b, u = re.sub(r"[^a-z0-9_]", "", base.lower()), re.sub(r"[^a-z0-9_]", "", user.lower())
    for i in range(start, start + 1000):
        s = f"{b}_{i}_by_{u}"[:64]
        try:
            awaitable = bot.get_sticker_set(name=s)
            # can't await inside sync func; instead mark invalid by exception below
            asyncio.get_event_loop().run_until_complete(awaitable)
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                return s
    raise RuntimeError("slug space exhausted")


async def _ensure_seen_from_packs(packs: List[str]) -> None:
    for p in packs:
        try:
            ss = await bot.get_sticker_set(name=p)
            _seen.update(s.file_unique_id for s in ss.stickers)
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                log.warning("Pack %s missing; skip", p)
            else:
                raise
    state["seen"] = list(_seen)
    _save()


async def _src(st: types.Sticker) -> Union[str, BufferedInputFile]:
    if st.is_animated or st.is_video:
        return st.file_id
    if st.width <= MAX_SIDE and st.height <= MAX_SIDE:
        return st.file_id
    if Image is None:
        raise ValueError("Pillow missing for resize")
    fi = await bot.get_file(st.file_id)
    buf = io.BytesIO()
    await bot.download_file(fi.file_path, buf)
    buf.seek(0)
    im = Image.open(buf).convert("RGBA")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG")
    out.seek(0)
    return BufferedInputFile(out.read(), filename="resized.png")


def _input(st: types.Sticker, source) -> InputSticker:
    return InputSticker(sticker=source, emoji_list=[st.emoji or "🙂"], format=_tg_fmt(st))

# ---------------------------------------------------------------------------
# Startup sync
# ---------------------------------------------------------------------------
async def _startup_sync():
    await _ensure_seen_from_packs(EXTRA_PACKS + ([state["current_pack"]] if state["current_pack"] else []))
    if state["current_pack"]:
        try:
            await bot.get_sticker_set(name=state["current_pack"])
        except TelegramBadRequest:
            state.update(_blank())
            _save()

# ---------------------------------------------------------------------------
# Pack ops
# ---------------------------------------------------------------------------
async def _new_pack(st: types.Sticker, chat: types.Chat) -> str:
    slug = await _slug(PACK_BASENAME, (await bot.me()).username, state["index"])
    title = f"{PACK_BASENAME.capitalize()} {state['index']}"
    await bot.create_new_sticker_set(OWNER_ID, slug, title, [_input(st, await _src(st))])
    await chat.send_message(f"New pack created 👉 https://t.me/addstickers/{slug}")
    return slug


async def _add(st: types.Sticker, pack: str) -> bool:
    try:
        await bot.add_sticker_to_set(OWNER_ID, pack, _input(st, await _src(st)))
        return True
    except TelegramBadRequest as e:
        if "STICKERSET_INVALID" in e.message:
            return False
        raise

# ---------------------------------------------------------------------------
# /logs command (OWNER only)
# ---------------------------------------------------------------------------
@dp.message(commands=["logs"])
async def cmd_logs(msg: types.Message):
    if msg.from_user.id != OWNER_ID:
        return
    text = "\n".join(list(_RECENT)[-30:]) or "No logs yet."
    await msg.reply(f"<pre>{types.utils.escape(text)}</pre>", parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Main sticker handler
# ---------------------------------------------------------------------------
@dp.message(F.content_type == ContentType.STICKER)
async def hoover(msg: types.Message):
    st = msg.sticker
    if st.file_unique_id in _seen:
        return  # duplicate → no ack

    anim = st.is_animated or st.is_video
    limit = MAX_ANIM if anim else MAX_STATIC

    # Ensure current pack fits
    if not state["current_pack"] or state["count"] >= limit or state["is_animated"] != anim:
        state["index"] += 1 if state["current_pack"] else 0
        state["count"] = 0
        state["is_animated"] = anim
        state["current_pack"] = await _new_pack(st, msg.chat)
        _save()

    success = await _add(st, state["current_pack"])
    if not success:
        # pack deleted mid-flight → reset once and retry
        state["current_pack"] = ""
        _save()
        success = await _add(st, state["current_pack"]) if state["current_pack"] else False

    if success:
        state["count"] += 1
        _seen.add(st.file_unique_id)
        state["seen"] = list(_seen)
        _save()
        await msg.reply("👍", disable_notification=True)

        if LUKE_ID:
            await msg.chat.send_message(random.choice(TROLLS).format(luke=LUKE_ID))
    else:
        await msg.reply("👎", disable_notification=True)

# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
async def main():
    await _startup_sync()
    log.info("Sticker hoover running… (acks & /logs)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
