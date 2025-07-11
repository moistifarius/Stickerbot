#!/usr/bin/env python3
"""
Async Sticker Hoover Bot (aiogram 3.7 • Bot API 7.x)
===================================================
✅ Dedup across packs   🛠 Self‑healing   🚀 Auto‑resize   👍 Thumbs‑up ack   📜 /logs command

**2025‑07‑11 → ack+logs patch**
--------------------------------
* After a sticker is successfully filed the bot sends a 👍 reply to the
  original message.
* `/logs` (OWNER only) dumps the last ~30 log lines right in chat so you don't
  need to SSH in.
* Tiny in‑memory ring buffer keeps recent logs; normal logging unchanged.

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
from typing import Any, Deque, Dict, List, Set

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputSticker

try:
    from PIL import Image  # type: ignore
except ImportError:
    Image = None  # resize will raise if Pillow missing

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
MAX_SIDE_STATIC = 512

TROLLS = [
    "Yo <a href='tg://user?id={luke}'>Luke</a>, another one for you. 10‑second job, remember?",
    "Adding stickers so Luke doesn't have to. Classic.",
    "<a href='tg://user?id={luke}'>Luke</a> could’ve done this in his sleep by now 😂",
    "Luke, buddy, this is what procrastination looks like in JSON.",
]

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _blank_state() -> Dict[str, Any]:
    return {"index": 1, "count": 0, "current_pack": "", "is_animated": False, "seen": []}


def load_state() -> Dict[str, Any]:
    if DATA_FILE.exists():
        raw = json.loads(DATA_FILE.read_text())
        raw.setdefault("seen", [])
        return raw
    return _blank_state()


def save_state(st: Dict[str, Any]) -> None:
    DATA_FILE.write_text(json.dumps(st, indent=2))

state = load_state()
_seen: Set[str] = set(state["seen"])

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------------------------------------------------------------------------
# Helper fns (slug, resize, etc.)
# ---------------------------------------------------------------------------

def _tg_format(st: types.Sticker) -> str:
    if st.is_animated:
        return "animated"
    if st.is_video:
        return "video"
    return "static"


def _clean(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", s.lower())


async def _slug(base: str, bot_user: str, start: int) -> str:
    base_c, bot_c = _clean(base), _clean(bot_user)
    for i in range(start, start + 1000):
        slug = f"{base_c}_{i}_by_{bot_c}"[:64]
        try:
            await bot.get_sticker_set(name=slug)
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                return slug
    raise RuntimeError("Ran out of slugs")


async def _bootstrap_dedup(packs: List[str]) -> None:
    for p in packs:
        if not p:
            continue
        try:
            sset = await bot.get_sticker_set(name=p)
            _seen.update(s.file_unique_id for s in sset.stickers)
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                log.warning("Pack %s not found – skipping", p)
            else:
                raise
    state["seen"] = list(_seen)
    save_state(state)


async def _resize_static(st: types.Sticker) -> BufferedInputFile | str:
    if st.is_animated or st.is_video:
        return st.file_id
    if st.width <= MAX_SIDE_STATIC and st.height <= MAX_SIDE_STATIC:
        return st.file_id
    if Image is None:
        raise ValueError("Pillow not installed")
    file_info = await bot.get_file(st.file_id)
    buf = io.BytesIO()
    await bot.download_file(file_info.file_path, buf)
    buf.seek(0)
    img = Image.open(buf).convert("RGBA")
    img.thumbnail((MAX_SIDE_STATIC, MAX_SIDE_STATIC), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return BufferedInputFile(out.read(), filename="resized.png")


def _input_sticker(st: types.Sticker, source) -> InputSticker:
    return InputSticker(sticker=source, emoji_list=[st.emoji or "🙂"], format=_tg_format(st))

# ---------------------------------------------------------------------------
# Sync state on startup
# ---------------------------------------------------------------------------
async def _sync_state() -> None:
    packs = EXTRA_PACKS + ([state["current_pack"]] if state["current_pack"] else [])
    await _bootstrap_dedup(packs)
    if state["current_pack"]:
        try:
            await bot.get_sticker_set(name=state["current_pack"])
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                log.warning("Persisted pack missing; resetting")
                state.update(_blank_state())
                save_state(state)

# ---------------------------------------------------------------------------
# Core sticker operations
# ---------------------------------------------------------------------------
async def _new_pack(st: types.Sticker, chat_id: int) -> str:
    slug = await _slug(PACK_BASENAME, (await bot.me()).username, state["index"])
    title = f"{PACK_BASENAME.capitalize()} {state['index']}"
    src = await _resize_static(st)
    await bot.create_new_sticker_set(OWNER_ID, slug, title, [_input_sticker(st, src)])
    await bot.send_message(chat_id, f"New pack created 👉 https://t.me/addstickers/{slug}")
    return slug


async def _add_to_pack(st: types.Sticker, pack: str) -> bool:
    try:
        src = await _resize_static(st)
        await bot.add_sticker_to_set(OWNER_ID, pack, _input_sticker(st, src))
        return True
    except TelegramBadRequest as e:
        if "STICKERSET_INVALID" in e.message:
            return False
        raise

# ---------------------------------------------------------------------------
# Command: /logs
# ---------------------------------------------------------------------------
@dp.message(commands={"logs"})
async def _cmd_logs(msg: types.Message) -> None:
    if msg.from_user.id != OWNER_ID:
        await msg.reply("Fuck off, only daddy matt can run that command")
        return
    lines = list(_RECENT)[-30:]
    if not lines:
        await msg.reply("No logs yet.")
        return
    text = "\n".join(lines)
    await msg.reply(f"<pre>{types.utils.escape(text)}</pre>", parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Sticker handler
# ---------------------------------------------------------------------------
@dp.message(F.content_type == ContentType.STICKER)
async def hoover(msg: types.Message) -> None:
    st = msg.sticker
    if st.file_unique_id in _seen:
        return

    anim = st.is_animated or st.is_video
    limit = MAX_ANIM if anim else MAX_STATIC

    # ensure pack ready
    if not state["current_pack"] or state["count"] >= limit or state["is_animated"] != anim:
        state["index"] += 1 if state["current_pack"] else 0
        state["count"] = 0
        state["is_animated"] = anim
        state["current_pack"] = await _new_pack
