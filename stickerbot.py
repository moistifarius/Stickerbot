#!/usr/bin/env python3
"""
Async Sticker Hoover Bot (aiogram 3.7 • Bot API 7.x)
===================================================
Grabs **new‑to‑us** stickers and files them into rolling packs—with dedup across
user‑specified reference packs, self‑healing, and now **dimension sanity** so
Telegram never throws `STICKER_PNG_DIMENSIONS` on oversized static stickers.

What’s new (dimension hot‑fix)
------------------------------
* If a *static* sticker’s width or height exceeds **512 px**, the bot logs a
  warning and skips it instead of crashing the whole service.
* Animated and video stickers are unaffected (Telegram resizes internally).

Env vars unchanged (`BOT_TOKEN`, `OWNER_ID`, `EXTRA_PACKS`, etc.).
"""

import asyncio
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any, Dict, Set, List

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.types import InputSticker
from aiogram.exceptions import TelegramBadRequest

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("sticker-hoover")

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
MAX_SIDE_STATIC = 512  # Telegram requirement for PNG/WEBP

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
    return {
        "index": 1,
        "count": 0,
        "current_pack": "",
        "is_animated": False,
        "seen": [],  # list[str]
    }


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
# Bot / Dispatcher setup
# ---------------------------------------------------------------------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------------------------------------------------------------------------
# Utility fns
# ---------------------------------------------------------------------------

def _tg_format(st: types.Sticker) -> str:
    if st.is_animated:
        return "animated"
    if st.is_video:
        return "video"
    return "static"


def _mk_input(st: types.Sticker) -> InputSticker:
    return InputSticker(sticker=st.file_id, emoji_list=[st.emoji or "🙂"], format=_tg_format(st))


def _clean(txt: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", txt.lower())


async def _slug(base: str, bot_user: str, start: int) -> str:
    base_c = _clean(base)
    bot_c = _clean(bot_user)
    for i in range(start, start + 1000):
        s = f"{base_c}_{i}_by_{bot_c}"[:64]
        try:
            await bot.get_sticker_set(name=s)
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                return s
    raise RuntimeError("No free slug")


async def _bootstrap_dedup(packs: List[str]) -> None:
    for name in packs:
        if not name:
            continue
        try:
            sset = await bot.get_sticker_set(name=name)
            _seen.update(s.file_unique_id for s in sset.stickers)
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                log.warning("Reference pack %s not found – skipping", name)
            else:
                raise
    state["seen"] = list(_seen)
    save_state(state)

# ---------------------------------------------------------------------------
# Startup sync
# ---------------------------------------------------------------------------
async def _sync_state() -> None:
    packs_to_seed = EXTRA_PACKS.copy()
    if state["current_pack"]:
        packs_to_seed.append(state["current_pack"])
    await _bootstrap_dedup(packs_to_seed)

    if state["current_pack"]:
        try:
            await bot.get_sticker_set(name=state["current_pack"])
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                log.warning("Saved pack %s missing, resetting", state["current_pack"])
                state.update(_blank_state())
                save_state(state)

# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------
async def _new_pack(st: types.Sticker, chat_id: int) -> str:
    bot_user = (await bot.me()).username
    name = await _slug(PACK_BASENAME, bot_user, state["index"])
    title = f"{PACK_BASENAME.capitalize()} {state['index']}"
    await bot.create_new_sticker_set(user_id=OWNER_ID, name=name, title=title, stickers=[_mk_input(st)])
    await bot.send_message(chat_id, f"New pack created 👉 https://t.me/addstickers/{name}")
    return name


async def _add(st: types.Sticker, pack: str, chat_id: int) -> bool:
    try:
        await bot.add_sticker_to_set(user_id=OWNER_ID, name=pack, sticker=_mk_input(st))
        return True
    except TelegramBadRequest as e:
        if "STICKERSET_INVALID" in e.message:
            return False
        raise

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
@dp.message(F.content_type == ContentType.STICKER)
async def hoover(msg: types.Message) -> None:
    st = msg.sticker

    # Dedup check
    if st.file_unique_id in _seen:
        return

    # Dimension guard for static stickers
    if not (st.is_animated or st.is_video):
        if st.width > MAX_SIDE_STATIC or st.height > MAX_SIDE_STATIC:
            log.warning("Static sticker %s too big (%dx%d) – skipping", st.file_unique_id, st.width, st.height)
            return

    anim = st.is_animated or st.is_video
    limit = MAX_ANIM if anim else MAX_STATIC

    if not state["current_pack"] or state["count"] >= limit or state["is_animated"] != anim:
        state["index"] += 1 if state["current_pack"] else 0
        state["count"] = 0
        state["is_animated"] = anim
        state["current_pack"] = await _new_pack(st, msg.chat.id)
        save_state(state)

    if not await _add(st, state["current_pack"], msg.chat.id):
        state["current_pack"] = ""
        save_state(state)
        await hoover(msg)
        return

    state["count"] += 1
    _seen.add(st.file_unique_id)
    state["seen"] = list(_seen)
    save_state(state)

    if LUKE_ID:
        await msg.chat.send_message(random.choice(TROLLS).format(luke=LUKE_ID))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    await _sync_state()
    log.info("Sticker hoover running… (dedup + dim‑guard)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
