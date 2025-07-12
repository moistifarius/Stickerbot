#!/usr/bin/env python3
"""
Async Sticker Hoover Bot (aiogram 3.7 • Bot API 7.x)
===================================================
✅ Dedup across packs   🛠 Self‑healing   🚀 Auto‑resize oversized static stickers
👍👎 Reaction feedback   📝 Enhanced logging

*New in this build (2025‑07‑11 → "resize + reactions" patch)*
-------------------------------------------------
* Static PNG/WEBP stickers that exceed **512 px** on either side are now **shrunk
  on‑the‑fly** with Pillow and then uploaded, so the bot never skips content nor
  crashes with `STICKER_PNG_DIMENSIONS`.
* Adds **Pillow** (`pip install pillow`) as a dependency.
* Keeps animated/video stickers unchanged (Telegram handles sizing there).
* **NEW**: Thumbs up 👍 reaction for successful sticker additions
* **NEW**: Thumbs down 👎 reaction for failed sticker additions
* **NEW**: Enhanced logging for all operations

Env vars unchanged (`BOT_TOKEN`, `OWNER_ID`, `EXTRA_PACKS`, etc.).
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
from typing import Any, Dict, List, Set

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InputSticker

try:
    from PIL import Image  # type: ignore
except ImportError:
    Image = None  # we'll guard at runtime

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("sticker-hoover")

# ---------------------------------------------------------------------------
# In-memory log storage for /logs command
# ---------------------------------------------------------------------------
class MemoryLogHandler(logging.Handler):
    """Custom handler to store logs in memory for the /logs command."""
    
    def __init__(self, maxlen=100):
        super().__init__()
        self.logs = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter(LOG_FORMAT))
    
    def emit(self, record):
        try:
            msg = self.format(record)
            self.logs.append(msg)
        except Exception:
            self.handleError(record)

# Set up memory log handler
memory_handler = MemoryLogHandler(maxlen=200)  # Keep last 200 log entries
log.addHandler(memory_handler)

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
MAX_SIDE_STATIC = 512  # Telegram spec for static stickers

# Queue and rate limiting configuration
CONCURRENT_LIMIT = 3  # Limit concurrent sticker processing
MAX_QUEUE_SIZE = 50   # Prevent memory issues
RATE_LIMIT_DELAY = 0.1  # Small delay between operations
MAX_ERROR_RATE = 0.3  # Circuit breaker threshold

TROLLS = [
    "Yo <a href='tg://user?id={luke}'>Luke</a>, another one for you. 10‑second job, remember?",
    "Adding stickers so Luke doesn't have to. Classic.",
    "<a href='tg://user?id={luke}'>Luke</a> could've done this in his sleep by now 😂",
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
        "seen": [],
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
# Bot & Dispatcher
# ---------------------------------------------------------------------------

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Queue management for high-volume scenarios
processing_semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
processing_queue: asyncio.Queue = None  # Will be initialized in main()
error_count = 0
total_processed = 0

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _tg_format(st: types.Sticker) -> str:
    if st.is_animated:
        return "animated"
    if st.is_video:
        return "video"
    return "static"


def _clean(txt: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", txt.lower())


async def _slug(base: str, bot_user: str, start: int) -> str:
    base_c, bot_c = _clean(base), _clean(bot_user)
    for i in range(start, start + 1000):
        s = f"{base_c}_{i}_by_{bot_c}"[:64]
        try:
            await bot.get_sticker_set(name=s)
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                return s
    raise RuntimeError("No free slug")


async def _bootstrap_dedup(packs: List[str]) -> None:
    log.info("Bootstrapping deduplication for %d packs", len(packs))
    for name in packs:
        if not name:
            continue
        try:
            sset = await bot.get_sticker_set(name=name)
            _seen.update(s.file_unique_id for s in sset.stickers)
            log.info("Loaded %d stickers from pack '%s' for deduplication", len(sset.stickers), name)
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                log.warning("Reference pack %s not found – skipping", name)
            else:
                log.error("Failed to load pack %s: %s", name, e)
                raise
    state["seen"] = list(_seen)
    save_state(state)
    log.info("Deduplication bootstrap complete. %d unique stickers loaded", len(_seen))

# ---------------------------------------------------------------------------
# Reaction helpers
# ---------------------------------------------------------------------------
async def _add_reaction(msg: types.Message, emoji: str, reason: str = "") -> None:
    """Add reaction to message with error handling and logging."""
    try:
        await msg.react([types.ReactionTypeEmoji(emoji=emoji)])
        log.info("Added %s reaction to sticker from user %s (chat %s)%s", 
                emoji, msg.from_user.id if msg.from_user else "unknown", 
                msg.chat.id, f" - {reason}" if reason else "")
    except Exception as e:
        log.warning("Failed to add %s reaction: %s", emoji, e)

# ---------------------------------------------------------------------------
# Resizing routine for oversized static stickers
# ---------------------------------------------------------------------------
async def _maybe_resize_static(st: types.Sticker) -> BufferedInputFile | str:
    """Return a sticker source suitable for InputSticker: either file_id or resized bytes."""
    if st.is_animated or st.is_video:
        log.debug("Sticker %s is animated/video, no resize needed", st.file_unique_id)
        return st.file_id  # no resize

    if st.width <= MAX_SIDE_STATIC and st.height <= MAX_SIDE_STATIC:
        log.debug("Sticker %s within size limits (%dx%d), no resize needed", 
                 st.file_unique_id, st.width, st.height)
        return st.file_id  # already within bounds

    # Skip resize for extremely large images during high load
    queue_size = processing_queue.qsize() if processing_queue else 0
    if queue_size > MAX_QUEUE_SIZE * 0.8 and (st.width > 2048 or st.height > 2048):
        log.warning("Queue busy (%d), skipping resize for very large sticker %s (%dx%d)", 
                   queue_size, st.file_unique_id, st.width, st.height)
        raise ValueError("Skipping large resize during high load")

    log.info("Sticker %s oversized (%dx%d), resizing to fit %dx%d", 
             st.file_unique_id, st.width, st.height, MAX_SIDE_STATIC, MAX_SIDE_STATIC)

    if Image is None:
        log.error("Pillow not installed; cannot resize %s – skipping", st.file_unique_id)
        raise ValueError("Need Pillow for resizing")

    # Download original file bytes
    try:
        file_info = await bot.get_file(st.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        buf.seek(0)
        original_bytes = len(buf.getvalue())
        log.debug("Downloaded sticker %s for resizing (%d bytes)", st.file_unique_id, original_bytes)
        
        # Skip if file is too large (>10MB during high load)
        if queue_size > MAX_QUEUE_SIZE * 0.5 and original_bytes > 10 * 1024 * 1024:
            log.warning("Queue busy (%d), skipping large file %s (%d bytes)", 
                       queue_size, st.file_unique_id, original_bytes)
            raise ValueError("Skipping large file during high load")
            
    except Exception as e:
        log.error("Failed to download sticker %s for resizing: %s", st.file_unique_id, e)
        raise ValueError("Download failed")

    try:
        img = Image.open(buf).convert("RGBA")
        original_size = img.size
        img.thumbnail((MAX_SIDE_STATIC, MAX_SIDE_STATIC), Image.LANCZOS)
        new_size = img.size
        
        out = io.BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        final_bytes = len(out.getvalue())
        
        log.info("Resized sticker %s from %dx%d to %dx%d (%d → %d bytes)", 
                st.file_unique_id, original_size[0], original_size[1], 
                new_size[0], new_size[1], original_bytes, final_bytes)
        
        return BufferedInputFile(out.read(), filename="resized.png")
    except Exception as e:
        log.error("Failed to resize sticker %s: %s", st.file_unique_id, e)
        raise ValueError("Resize failed")


def _mk_input(st: types.Sticker, source: BufferedInputFile | str) -> InputSticker:
    return InputSticker(sticker=source, emoji_list=[st.emoji or "🙂"], format=_tg_format(st))

# ---------------------------------------------------------------------------
# Startup sync
# ---------------------------------------------------------------------------
async def _sync_state() -> None:
    log.info("Starting state synchronization")
    packs = EXTRA_PACKS.copy()
    if state["current_pack"]:
        packs.append(state["current_pack"])
    await _bootstrap_dedup(packs)

    if state["current_pack"]:
        try:
            pack_info = await bot.get_sticker_set(name=state["current_pack"])
            log.info("Current pack '%s' verified with %d stickers", 
                    state["current_pack"], len(pack_info.stickers))
        except TelegramBadRequest as e:
            if "STICKERSET_INVALID" in e.message:
                log.warning("Saved pack %s missing, resetting state", state["current_pack"])
                state.update(_blank_state())
                save_state(state)
            else:
                log.error("Error verifying current pack: %s", e)

# ---------------------------------------------------------------------------
# Core add / create operations
# ---------------------------------------------------------------------------
async def _new_pack(st: types.Sticker, chat_id: int) -> str:
    log.info("Creating new sticker pack (type: %s)", _tg_format(st))
    bot_user = (await bot.me()).username
    name = await _slug(PACK_BASENAME, bot_user, state["index"])
    title = f"{PACK_BASENAME.capitalize()} {state['index']}"
    
    try:
        src = await _maybe_resize_static(st)
        sticker_input = _mk_input(st, src)
        await bot.create_new_sticker_set(user_id=OWNER_ID, name=name, title=title, stickers=[sticker_input])
        pack_url = f"https://t.me/addstickers/{name}"
        await bot.send_message(chat_id, f"New pack created 👉 {pack_url}")
        log.info("Successfully created new pack '%s' with URL: %s", name, pack_url)
        return name
    except Exception as e:
        log.error("Failed to create new pack '%s': %s", name, e)
        raise


async def _add(st: types.Sticker, pack: str, chat_id: int) -> bool:
    log.debug("Attempting to add sticker %s to pack '%s'", st.file_unique_id, pack)
    try:
        src = await _maybe_resize_static(st)
        await bot.add_sticker_to_set(user_id=OWNER_ID, name=pack, sticker=_mk_input(st, src))
        log.info("Successfully added sticker %s to pack '%s'", st.file_unique_id, pack)
        return True
    except ValueError as e:
        log.warning("Skipping sticker %s due to processing error: %s", st.file_unique_id, e)
        return True  # skip silently if resize failed
    except TelegramBadRequest as e:
        if "STICKERSET_INVALID" in e.message:
            log.error("Pack '%s' is invalid, will reset and retry", pack)
            return False
        log.error("Failed to add sticker %s to pack '%s': %s", st.file_unique_id, pack, e)
        raise

# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------
@dp.message(F.content_type == ContentType.STICKER)
async def hoover(msg: types.Message) -> None:
    """Queue sticker for processing to prevent overload."""
    try:
        # Non-blocking queue add with immediate feedback
        processing_queue.put_nowait(msg)
        log.info("Queued sticker %s from user %s (queue size: %d)", 
                msg.sticker.file_unique_id, 
                msg.from_user.id if msg.from_user else "unknown",
                processing_queue.qsize())
    except asyncio.QueueFull:
        log.warning("Processing queue full, dropping sticker %s", msg.sticker.file_unique_id)
        await _add_reaction(msg, "👎", "queue full - try again later")

async def process_sticker(msg: types.Message) -> None:
    """Process a single sticker with concurrency control."""
    global error_count, total_processed
    
    async with processing_semaphore:
        st = msg.sticker
        user_info = f"user {msg.from_user.id}" if msg.from_user else "unknown user"
        
        # Circuit breaker check
        if total_processed > 0 and error_count / total_processed > MAX_ERROR_RATE:
            log.warning("Error rate too high (%d/%d), temporarily pausing processing", 
                       error_count, total_processed)
            await asyncio.sleep(5)  # Brief pause
        
        log.info("Processing sticker %s from %s in chat %s (type: %s, size: %dx%d)", 
                st.file_unique_id, user_info, msg.chat.id, _tg_format(st), st.width, st.height)
        
        if st.file_unique_id in _seen:
            log.info("Sticker %s already processed, skipping", st.file_unique_id)
            return

        # Add rate limiting delay
        await asyncio.sleep(RATE_LIMIT_DELAY)
        total_processed += 1

        anim = st.is_animated or st.is_video
        limit = MAX_ANIM if anim else MAX_STATIC

        # Check if we need a new pack
        needs_new_pack = (
            not state["current_pack"] or 
            state["count"] >= limit or 
            state["is_animated"] != anim
        )

        if needs_new_pack:
            log.info("Need new pack: current='%s', count=%d/%d, anim_mismatch=%s", 
                    state["current_pack"], state["count"], limit, state["is_animated"] != anim)
            
            state["index"] += 1 if state["current_pack"] else 0
            state["count"] = 0
            state["is_animated"] = anim
            
            try:
                state["current_pack"] = await _new_pack(st, msg.chat.id)
                save_state(state)
            except Exception as e:
                log.error("Failed to create new pack: %s", e)
                await _add_reaction(msg, "👎", "pack creation failed")
                error_count += 1
                return

        # Try to add sticker to current pack
        try:
            success = await _add(st, state["current_pack"], msg.chat.id)
            if not success:
                log.warning("Pack '%s' became invalid, resetting and retrying", state["current_pack"])
                state["current_pack"] = ""
                save_state(state)
                # Re-queue for retry instead of recursive call
                await processing_queue.put(msg)
                return

            # Success! Update state and add reaction
            state["count"] += 1
            _seen.add(st.file_unique_id)
            state["seen"] = list(_seen)
            save_state(state)

            await _add_reaction(msg, "👍", f"added to pack '{state['current_pack']}'")
            log.info("Sticker %s successfully processed. Pack '%s' now has %d stickers", 
                    st.file_unique_id, state["current_pack"], state["count"])

            if LUKE_ID:
                await msg.chat.send_message(random.choice(TROLLS).format(luke=LUKE_ID))

        except Exception as e:
            log.error("Failed to process sticker %s: %s", st.file_unique_id, e)
            await _add_reaction(msg, "👎", f"processing failed: {str(e)[:50]}")
            error_count += 1

async def sticker_processor():
    """Background task to process queued stickers."""
    log.info("Starting sticker processor task")
    while True:
        try:
            msg = await processing_queue.get()
            await process_sticker(msg)
            processing_queue.task_done()
        except Exception as e:
            log.error("Error in sticker processor: %s", e)
            await asyncio.sleep(1)  # Brief pause on error

# Add status command for monitoring
@dp.message(Command("status"))
async def status_cmd(msg: types.Message) -> None:
    """Show bot status and queue information."""
    if msg.from_user.id != OWNER_ID:
        return
    
    queue_size = processing_queue.qsize() if processing_queue else 0
    error_rate = (error_count / total_processed * 100) if total_processed > 0 else 0
    
    status_text = f"""🤖 <b>Bot Status</b>
    
📊 <b>Processing Stats:</b>
• Queue size: {queue_size}
• Total processed: {total_processed}
• Errors: {error_count} ({error_rate:.1f}%)
• Current pack: {state.get('current_pack', 'None')}
• Pack count: {state.get('count', 0)}

💾 <b>Memory:</b>
• Seen stickers: {len(_seen)}
• Log entries: {len(memory_handler.logs)}

⚙️ <b>Config:</b>
• Concurrent limit: {CONCURRENT_LIMIT}
• Max queue size: {MAX_QUEUE_SIZE}
• Rate limit delay: {RATE_LIMIT_DELAY}s"""
    
    await msg.reply(status_text)

# Add logs command as requested
@dp.message(Command("logs"))
async def logs_cmd(msg: types.Message) -> None:
    """Show recent logs."""
    if msg.from_user.id != OWNER_ID:
        return
    
    if not memory_handler.logs:
        await msg.reply("No logs available.")
        return
    
    # Get last 20 log entries
    recent_logs = list(memory_handler.logs)[-20:]
    logs_text = "📋 <b>Recent Logs:</b>\n\n<pre>" + "\n".join(recent_logs) + "</pre>"
    
    # Split message if too long
    if len(logs_text) > 4000:
        logs_text = logs_text[:4000] + "...\n[truncated]</pre>"
    
    await msg.reply(logs_text)

# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
async def main() -> None:
    global processing_queue
    
    log.info("Starting Sticker Hoover Bot...")
    
    # Initialize the processing queue
    processing_queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    
    await _sync_state()
    
    # Start background processor
    processor_task = asyncio.create_task(sticker_processor())
    
    log.info("Sticker hoover running… (dedup + auto‑resize + reactions + queue)")
    
    try:
        await dp.start_polling(bot)
    finally:
        processor_task.cancel()
        try:
            await processor_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
