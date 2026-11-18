"""Telegram wiring: handlers, commands and entry point."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections import deque

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message

from .config import Config
from .engine import Hoover, emoji_from_caption, is_missing_set_error
from .store import Store

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
log = logging.getLogger("stickerbot")


class MemoryLogHandler(logging.Handler):
    def __init__(self, maxlen: int = 300):
        super().__init__()
        self.logs: deque[str] = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(message)s", "%m-%d %H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.logs.append(self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)


memory_handler = MemoryLogHandler()

HELP = """<b>Sticker hoover</b> 🧹
Every sticker posted where I can see it lands in our packs (no duplicates).

<b>Make stickers</b> — DM me a photo, image, GIF or short video (trusted users). Put an emoji in the caption to tag it.
/steal — reply to a photo/GIF/video/sticker in a group to add it
/grab — reply to a sticker (or give a pack link) to hoover the whole pack
/packs — list our packs

<b>Owner</b>: /undo (remove last added), /remove (reply to one of our stickers), /status, /logs"""


def sticker_job(msg: Message, *, troll: bool) -> dict | None:
    st = msg.sticker
    if st is None or st.type == "custom_emoji":
        return None
    fmt = "animated" if st.is_animated else "video" if st.is_video else "static"
    return {
        "kind": "sticker", "file_id": st.file_id, "uid": st.file_unique_id,
        "format": fmt, "emoji": st.emoji, "set_name": st.set_name,
    }


def media_job(msg: Message) -> dict | None:
    emoji = emoji_from_caption(msg.caption)
    if msg.photo:
        p = msg.photo[-1]
        return {"kind": "media", "media": "photo", "file_id": p.file_id, "uid": "m:" + p.file_unique_id, "emoji": emoji}
    if msg.animation:
        a = msg.animation
        return {"kind": "media", "media": "gif", "file_id": a.file_id, "uid": "m:" + a.file_unique_id,
                "emoji": emoji, "suffix": ".gif" if (a.mime_type or "") == "image/gif" else ".mp4"}
    if msg.video:
        v = msg.video
        return {"kind": "media", "media": "video", "file_id": v.file_id, "uid": "m:" + v.file_unique_id, "emoji": emoji}
    if msg.video_note:
        v = msg.video_note
        return {"kind": "media", "media": "video", "file_id": v.file_id, "uid": "m:" + v.file_unique_id, "emoji": emoji}
    if msg.document and msg.document.mime_type:
        d, mt = msg.document, msg.document.mime_type
        if mt == "image/gif":
            return {"kind": "media", "media": "gif", "file_id": d.file_id, "uid": "m:" + d.file_unique_id,
                    "emoji": emoji, "suffix": ".gif"}
        if mt.startswith("image/"):
            return {"kind": "media", "media": "image", "file_id": d.file_id, "uid": "m:" + d.file_unique_id, "emoji": emoji}
        if mt.startswith("video/"):
            return {"kind": "media", "media": "video", "file_id": d.file_id, "uid": "m:" + d.file_unique_id, "emoji": emoji}
    return None


def with_origin(job: dict, msg: Message, **extra) -> dict:
    origin = {"chat_id": msg.chat.id, "message_id": msg.message_id,
              "user_id": msg.from_user.id if msg.from_user else None,
              "private": msg.chat.type == "private"}
    origin.update(extra)
    job.update(origin)
    return job


def build(cfg: Config, bot: Bot, store: Store) -> tuple[Dispatcher, Hoover]:
    dp = Dispatcher()
    hoover = Hoover(bot, cfg, store)

    def uid(msg: Message) -> int | None:
        return msg.from_user.id if msg.from_user else None

    def chat_ok(msg: Message) -> bool:
        if cfg.allowed_chats and msg.chat.id not in cfg.allowed_chats and msg.chat.type != "private":
            return False
        return msg.chat.type == "private" or cfg.hoover_groups

    @dp.message(CommandStart())
    @dp.message(Command("help"))
    async def help_cmd(msg: Message) -> None:
        await msg.reply(HELP)

    @dp.message(Command("packs"))
    async def packs_cmd(msg: Message) -> None:
        rows = store.packs()
        if not rows:
            await msg.reply("No packs yet — send me a sticker.")
            return
        lines = [f"• <a href='https://t.me/addstickers/{r['name']}'>{html.escape(r['title'] or r['name'])}</a>"
                 f" — {r['count']}/{cfg.pack_limit}" for r in rows]
        await msg.reply("📦 <b>Packs</b>\n" + "\n".join(lines), disable_web_page_preview=True)

    @dp.message(Command("status"))
    async def status_cmd(msg: Message) -> None:
        if uid(msg) != cfg.owner_id:
            return
        s = hoover.stats
        cur = store.current_pack(cfg.pack_limit)
        up = int(time.time() - hoover.started)
        await msg.reply(
            "🤖 <b>Status</b>\n"
            f"• Uptime: {up // 3600}h{up % 3600 // 60:02d}m\n"
            f"• Queue: {store.queue_size()}\n"
            f"• This run: {s['ok']} added, {s['dup']} dupes, {s['fail']} failed, {s['flood_waits']} flood waits\n"
            f"• All time added: {store.total_added()} · known stickers: {store.seen_count()}\n"
            f"• Packs: {len(store.packs())} · current: "
            f"{html.escape(cur['name']) + ' (' + str(cur['count']) + ')' if cur else 'none (next sticker makes one)'}\n"
            f"• Last error: <code>{html.escape(s['last_error'][:200] or '—')}</code>"
        )

    @dp.message(Command("logs"))
    async def logs_cmd(msg: Message, command: CommandObject) -> None:
        if uid(msg) != cfg.owner_id:
            return
        n = int(command.args) if command.args and command.args.isdigit() else 25
        lines = list(memory_handler.logs)[-n:]
        if not lines:
            await msg.reply("No logs yet.")
            return
        body = html.escape("\n".join(lines))
        if len(body) > 3900:
            body = "…" + body[-3900:]
        await msg.reply(f"<pre>{body}</pre>")

    @dp.message(Command("undo"))
    async def undo_cmd(msg: Message) -> None:
        if uid(msg) != cfg.owner_id:
            return
        row = store.last_sticker()
        if not row or not row["pack_file_id"]:
            await msg.reply("Nothing to undo.")
            return
        try:
            pack = await hoover.remove(row["pack_file_id"], row["pack_uid"])
        except TelegramBadRequest as e:
            store.forget_sticker(row["id"])
            await msg.reply(f"Telegram said: <code>{html.escape(e.message)}</code> (forgot it locally)")
            return
        await msg.reply(f"Removed the last sticker from {html.escape(pack)}.")

    @dp.message(Command("remove"))
    async def remove_cmd(msg: Message) -> None:
        if uid(msg) != cfg.owner_id:
            return
        target = msg.reply_to_message.sticker if msg.reply_to_message else None
        if not target or target.set_name not in store.pack_names():
            await msg.reply("Reply to a sticker from one of our packs with /remove.")
            return
        try:
            await hoover.remove(target.file_id, target.file_unique_id)
        except TelegramBadRequest as e:
            await msg.reply(f"Telegram said: <code>{html.escape(e.message)}</code>")
            return
        await msg.reply("Removed 🗑")

    @dp.message(Command("steal"))
    async def steal_cmd(msg: Message) -> None:
        if not cfg.is_trusted(uid(msg)):
            return
        src = msg.reply_to_message
        job = src and (sticker_job(src, troll=False) or media_job(src))
        if not job:
            await msg.reply("Reply to a sticker, photo, GIF or video with /steal.")
            return
        if store.is_seen(job["uid"]):
            await msg.reply("Already got that one 👌")
            return
        hoover.enqueue(with_origin(job, src, private=msg.chat.type == "private"))
        await msg.react([_emoji("👀")])

    @dp.message(Command("grab"))
    async def grab_cmd(msg: Message, command: CommandObject) -> None:
        if not cfg.is_trusted(uid(msg)):
            return
        name = None
        if msg.reply_to_message and msg.reply_to_message.sticker:
            name = msg.reply_to_message.sticker.set_name
        elif command.args:
            m = re.search(r"(?:addstickers/)?([A-Za-z0-9_]+)\s*$", command.args.strip())
            name = m.group(1) if m else None
        if not name:
            await msg.reply("Reply to a sticker with /grab, or /grab https://t.me/addstickers/NAME")
            return
        if name in store.pack_names():
            await msg.reply("That's one of our own packs.")
            return
        try:
            sset = await hoover.tg(lambda: bot.get_sticker_set(name=name), "get_sticker_set")
        except TelegramBadRequest as e:
            await msg.reply("Pack not found." if is_missing_set_error(e.message)
                            else f"<code>{html.escape(e.message)}</code>")
            return
        todo = [s for s in sset.stickers if s.type != "custom_emoji" and not store.is_seen(s.file_unique_id)]
        if not todo:
            await msg.reply(f"Already have all {len(sset.stickers)} from {html.escape(sset.title)} 👌")
            return
        batch = hoover.start_batch(msg.chat.id, sset.title, len(todo))
        for s in todo:
            fmt = "animated" if s.is_animated else "video" if s.is_video else "static"
            hoover.enqueue({"kind": "sticker", "file_id": s.file_id, "uid": s.file_unique_id, "format": fmt,
                            "emoji": s.emoji, "set_name": name, "batch": batch, "chat_id": msg.chat.id,
                            "private": msg.chat.type == "private"})
        await msg.reply(f"Hoovering {len(todo)} stickers from <b>{html.escape(sset.title)}</b> "
                        f"({len(sset.stickers) - len(todo)} already had). I'll report when done.")

    @dp.message(F.sticker)
    async def on_sticker(msg: Message) -> None:
        if not chat_ok(msg):
            return
        job = sticker_job(msg, troll=True)
        if not job:
            return
        if store.is_seen(job["uid"]):
            if msg.chat.type == "private":
                await _react(msg, "👌")
            return
        hoover.enqueue(with_origin(job, msg, troll=msg.chat.type != "private",
                                   react_dup=msg.chat.type == "private"))

    @dp.message(F.chat.type == "private", F.photo | F.animation | F.video | F.video_note | F.document)
    async def on_media(msg: Message) -> None:
        if not cfg.is_trusted(uid(msg)):
            await msg.reply("Only the owner's friends can turn media into stickers. Stickers are always welcome.")
            return
        job = media_job(msg)
        if not job:
            await msg.reply("I can only use images, GIFs and videos.")
            return
        if store.is_seen(job["uid"]):
            await _react(msg, "👌")
            return
        hoover.enqueue(with_origin(job, msg))
        await _react(msg, "👀")

    return dp, hoover


def _emoji(e: str):
    from aiogram.types import ReactionTypeEmoji
    return ReactionTypeEmoji(emoji=e)


async def _react(msg: Message, e: str) -> None:
    try:
        await msg.react([_emoji(e)])
    except Exception as ex:  # noqa: BLE001
        log.debug("react failed: %s", ex)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    log.addHandler(memory_handler)

    cfg = Config.from_env()
    store = Store(cfg.db_path)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp, hoover = build(cfg, bot, store)

    await hoover.start()
    try:
        await bot.set_my_commands([
            BotCommand(command="packs", description="List our sticker packs"),
            BotCommand(command="grab", description="Hoover a whole pack (reply to a sticker)"),
            BotCommand(command="steal", description="Add the replied photo/GIF/video/sticker"),
            BotCommand(command="help", description="How this works"),
        ])
    except Exception as e:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", e)

    worker = asyncio.create_task(hoover.worker())
    log.info("Sticker hoover running as @%s", hoover.bot_username)
    try:
        await dp.start_polling(bot, handle_signals=True,
                               allowed_updates=["message"])
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
