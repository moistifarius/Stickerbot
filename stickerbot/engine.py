"""The hoover: takes jobs off the persistent queue and puts stickers into packs.

Design notes
* Telegram is the source of truth for pack contents; local counts are refreshed
  from get_sticker_set after every add.
* Sticker sets may mix static/animated/video stickers (format is per sticker),
  up to `pack_limit` (120) stickers per set.
* One worker, strictly sequential: no races over "which pack is current".
* Flood control (RetryAfter) is waited out, network errors back off, and the job
  stays in SQLite until it has a definite outcome, so restarts lose nothing.
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import BufferedInputFile, InputSticker, ReactionTypeEmoji

from . import media
from .config import Config
from .store import Store

log = logging.getLogger("stickerbot")

TROLLS = [
    "Yo <a href='tg://user?id={luke}'>Luke</a>, another one for you. 10‑second job, remember?",
    "Adding stickers so Luke doesn't have to. Classic.",
    "<a href='tg://user?id={luke}'>Luke</a> could've done this in his sleep by now 😂",
    "Luke, buddy, this is what procrastination looks like in JSON.",
]

OK, DUP, FAIL = "ok", "dup", "fail"
REACTIONS = {OK: "👍", DUP: "👌", FAIL: "👎"}

MAX_FILE_BYTES = 20 * 1024 * 1024  # Bot API download limit


class Transient(Exception):
    """Temporary failure (network / Telegram 5xx); retry the job later."""


class Permanent(Exception):
    """This job can never succeed; report and drop it."""


def _has(msg: str, *needles: str) -> bool:
    m = msg.upper()
    return any(n in m for n in needles)


def is_full_error(msg: str) -> bool:
    return _has(msg, "TOO_MUCH", "TOO_MANY", "STICKERSET_FULL", "STICKERS_TOO")


def is_missing_set_error(msg: str) -> bool:
    return _has(msg, "STICKERSET_INVALID", "STICKERSET_NOT_FOUND", "STICKER_SET_INVALID")


def is_file_error(msg: str) -> bool:
    return _has(
        msg, "FILE", "STICKER_PNG", "STICKER_VIDEO", "STICKER_TGS", "DIMENSIONS",
        "IDENTIFIER", "WEBM", "IMAGE_PROCESS", "STICKER_INVALID", "STICKER_GIF",
    )


def is_emoji_error(msg: str) -> bool:
    return _has(msg, "EMOJI")


def clean(txt: str) -> str:
    """Letters/digits/single underscores, starting with a letter."""
    s = re.sub(r"[^a-z0-9_]", "", txt.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "s" + s
    return s


def pack_name(base: str, index: int, bot_username: str) -> str:
    """'{base}_{index}_by_{bot}', truncating the base (never the required suffix)."""
    suffix = f"_{index}_by_{bot_username.lower()}"
    head = clean(base)[: max(1, 64 - len(suffix))].rstrip("_") or "s"
    return f"{head}{suffix}"


def emoji_from_caption(caption: str | None) -> str | None:
    """If a caption starts with an emoji-ish token, use it as the sticker emoji."""
    if not caption:
        return None
    tok = caption.strip().split()[0] if caption.strip() else ""
    if tok and len(tok) <= 16 and not re.search(r"[A-Za-z0-9/@#]", tok):
        return tok
    return None


@dataclass
class Placed:
    pack: str
    created: bool
    count: int


class Hoover:
    def __init__(self, bot: Any, cfg: Config, store: Store, sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.bot = bot
        self.cfg = cfg
        self.store = store
        self.sleep = sleep
        self.wake = asyncio.Event()
        self.bot_username = ""
        self.stats = {"ok": 0, "dup": 0, "fail": 0, "flood_waits": 0, "last_error": ""}
        self.started = time.time()

    # ------------------------------------------------------------------ calls
    async def tg(self, make: Callable[[], Awaitable], what: str = "call") -> Any:
        """Run a Telegram call, waiting out flood control and retrying blips."""
        net_tries = 0
        flood_tries = 0
        while True:
            try:
                return await make()
            except TelegramRetryAfter as e:
                flood_tries += 1
                self.stats["flood_waits"] += 1
                wait = float(e.retry_after) + 0.5
                log.warning("Flood control on %s: waiting %.0fs (try %d)", what, wait, flood_tries)
                if flood_tries > 30:
                    raise Transient(f"flood control persisted on {what}") from e
                await self.sleep(wait)
            except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as e:
                net_tries += 1
                if net_tries > 5:
                    raise Transient(f"{what}: {e}") from e
                wait = 2 ** net_tries
                log.warning("%s failed (%s); retry in %ss", what, e, wait)
                await self.sleep(wait)

    # -------------------------------------------------------------- startup
    async def start(self) -> None:
        me = await self.tg(lambda: self.bot.get_me(), "get_me")
        self.bot_username = me.username
        self.import_legacy()
        await self.discover()

    def import_legacy(self) -> None:
        """Import the v1 pack_state.json once (seen ids + current pack)."""
        path = self.cfg.legacy_state
        if not path or self.store.get("legacy_imported") or not Path(path).exists():
            return
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError) as e:
            log.warning("Could not read legacy state %s: %s", path, e)
            return
        self.store.mark_seen(*raw.get("seen", []))
        legacy_packs = [p for p in [raw.get("current_pack")] if p]
        self.store.put("legacy_packs", legacy_packs)
        self.store.put("legacy_imported", True)
        log.info("Imported legacy state: %d seen ids, packs %s", len(raw.get("seen", [])), legacy_packs)

    async def _get_set(self, name: str):
        try:
            return await self.tg(lambda: self.bot.get_sticker_set(name=name), f"get_sticker_set {name}")
        except TelegramBadRequest as e:
            if is_missing_set_error(e.message):
                return None
            raise

    def _index_of(self, name: str) -> int | None:
        m = re.search(r"_(\d+)_by_", name)
        return int(m.group(1)) if m else None

    async def discover(self) -> None:
        """Find our existing packs on Telegram and load their stickers into dedup."""
        found: dict[str, Any] = {}
        misses, i = 0, 1
        # probe sequential names; tolerate gaps
        while misses < 5 and i < 1000:
            name = pack_name(self.cfg.pack_basename, i, self.bot_username)
            sset = await self._get_set(name)
            if sset is None:
                misses += 1
            else:
                misses = 0
                found[name] = sset
            i += 1
        # plus anything we already knew about (legacy, db) that the probe missed
        known = set(self.store.get("legacy_packs", [])) | self.store.pack_names()
        for name in known - set(found):
            if not name.lower().endswith(f"_by_{self.bot_username.lower()}"):
                continue
            sset = await self._get_set(name)
            if sset is None:
                log.warning("Pack %s no longer exists; forgetting it", name)
                self.store.drop_pack(name)
            else:
                found[name] = sset

        for name, sset in found.items():
            idx = self._index_of(name) or (self.store.max_index() + 1)
            n = len(sset.stickers)
            self.store.upsert_pack(name, idx, sset.title, n, n >= self.cfg.pack_limit)
            self.store.mark_seen(*(s.file_unique_id for s in sset.stickers))
        log.info("Discovered %d own packs: %s", len(found),
                 ", ".join(f"{n}({len(s.stickers)})" for n, s in sorted(found.items())) or "none")

        for name in self.cfg.extra_packs:
            sset = await self._get_set(name)
            if sset is None:
                log.warning("Reference pack %s not found – skipping", name)
                continue
            self.store.mark_seen(*(s.file_unique_id for s in sset.stickers))
            log.info("Loaded %d stickers from reference pack %s", len(sset.stickers), name)

    # ---------------------------------------------------------------- inputs
    async def _download(self, file_id: str) -> bytes:
        f = await self.tg(lambda: self.bot.get_file(file_id), "get_file")
        if f.file_size and f.file_size > MAX_FILE_BYTES:
            raise Permanent("file is bigger than the 20 MB bot download limit")
        buf = io.BytesIO()
        await self.tg(lambda: self.bot.download_file(f.file_path, destination=buf), "download")
        return buf.getvalue()

    async def build_input(self, job: dict, upload: bool, emoji: str) -> InputSticker:
        kind = job["kind"]
        emoji_list = [emoji]
        if kind == "sticker":
            fmt = job["format"]
            if not upload:
                return InputSticker(sticker=job["file_id"], format=fmt, emoji_list=emoji_list)
            data = await self._download(job["file_id"])
            if fmt == "static":
                body, fname = media.to_static(data)
            elif fmt == "video":
                body, fname = await media.to_video(data, ".webm"), "sticker.webm"
            else:  # animated .tgs: upload as-is
                body, fname = data, "sticker.tgs"
            return InputSticker(sticker=BufferedInputFile(body, fname), format=fmt, emoji_list=emoji_list)

        # media: photo / image / gif / video
        data = await self._download(job["file_id"])
        try:
            if job["media"] in ("photo", "image"):
                body, fname = media.to_static(data)
                return InputSticker(sticker=BufferedInputFile(body, fname), format="static", emoji_list=emoji_list)
            body = await media.to_video(data, job.get("suffix") or ".mp4")
        except media.ConversionError as e:
            raise Permanent(f"conversion failed: {e}") from e
        return InputSticker(sticker=BufferedInputFile(body, "sticker.webm"), format="video", emoji_list=emoji_list)

    # ------------------------------------------------------------- placement
    async def _create_pack(self, inp: InputSticker) -> str:
        idx = self.store.max_index() + 1
        for _ in range(200):
            name = pack_name(self.cfg.pack_basename, idx, self.bot_username)
            existing = await self._get_set(name)
            if existing is not None:
                # a pack of ours we didn't know about: adopt it
                n = len(existing.stickers)
                self.store.upsert_pack(name, idx, existing.title, n, n >= self.cfg.pack_limit)
                self.store.mark_seen(*(s.file_unique_id for s in existing.stickers))
                if n < self.cfg.pack_limit:
                    raise _Adopted(name)
                idx += 1
                continue
            title = self.cfg.title_for(idx)
            try:
                await self.tg(lambda: self.bot.create_new_sticker_set(
                    user_id=self.cfg.owner_id, name=name, title=title, stickers=[inp]
                ), f"create {name}")
            except TelegramBadRequest as e:
                if _has(e.message, "NAME_OCCUPIED", "SHORTNAME_OCCUPY"):
                    idx += 1
                    continue
                if _has(e.message, "PEER_ID_INVALID", "USER_ID_INVALID", "USER_NOT_FOUND", "CHAT_NOT_FOUND"):
                    raise Permanent("owner must open a DM with the bot and press Start first") from e
                raise
            self.store.upsert_pack(name, idx, title, 1, False)
            log.info("Created pack %s", name)
            return name
        raise Permanent("no free pack name")

    async def place(self, inp: InputSticker) -> Placed:
        """Add to the current pack, or create a new one with this sticker."""
        for _ in range(5):
            row = self.store.current_pack(self.cfg.pack_limit)
            if row is None:
                try:
                    name = await self._create_pack(inp)
                except _Adopted:
                    continue
                return Placed(name, True, 1)
            name = row["name"]
            try:
                await self.tg(lambda: self.bot.add_sticker_to_set(
                    user_id=self.cfg.owner_id, name=name, sticker=inp
                ), f"add to {name}")
            except TelegramBadRequest as e:
                if is_full_error(e.message):
                    log.info("Pack %s is full according to Telegram; rolling over", name)
                    self.store.mark_full(name)
                    continue
                if is_missing_set_error(e.message):
                    log.warning("Pack %s vanished; forgetting it", name)
                    self.store.drop_pack(name)
                    continue
                raise
            return Placed(name, False, row["count"] + 1)
        raise Permanent("could not find a pack with room")

    async def _refresh(self, placed: Placed) -> tuple[str, str]:
        """Best-effort: sync pack count from Telegram and return (file_id, file_unique_id)
        of the sticker just added, or ("", "") if it can't be identified with confidence.
        Never raises: the add already succeeded and must not be retried."""
        try:
            sset = await self._get_set(placed.pack)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not refresh %s after add: %s", placed.pack, e)
            self.store.set_pack_count(placed.pack, placed.count, self.cfg.pack_limit)
            return "", ""
        if sset is None or not sset.stickers:
            return "", ""
        self.store.set_pack_count(placed.pack, len(sset.stickers), self.cfg.pack_limit)
        last = sset.stickers[-1]
        # if the listing lags, the last entry is an older sticker we already recorded
        if self.store.sticker_by_pack_uid(last.file_unique_id) or len(sset.stickers) < placed.count:
            log.warning("Pack %s listing looks stale; not recording file id for undo", placed.pack)
            return "", ""
        return last.file_id, last.file_unique_id

    # --------------------------------------------------------------- process
    async def process(self, job: dict) -> tuple[str, str]:
        """Return (outcome, detail). Raises Transient for retry-later."""
        uid = job["uid"]
        if self.store.is_seen(uid):
            return DUP, "already hoovered"

        upload = job["kind"] != "sticker"
        emoji = job.get("emoji") or self.cfg.default_emoji
        last_err = ""
        for _ in range(4):
            try:
                inp = await self.build_input(job, upload, emoji)
                placed = await self.place(inp)
            except media.ConversionError as e:
                raise Permanent(f"conversion failed: {e}") from e
            except TelegramBadRequest as e:
                last_err = e.message
                if is_file_error(e.message) and not upload:
                    log.info("file_id reuse rejected (%s); re-uploading %s", e.message, uid)
                    upload = True
                    continue
                if is_emoji_error(e.message) and emoji != self.cfg.default_emoji:
                    emoji = self.cfg.default_emoji
                    continue
                raise Permanent(e.message) from e
            except TelegramForbiddenError as e:
                raise Permanent(e.message) from e

            self.store.mark_seen(uid)  # before anything else can fail: never add twice
            file_id, pack_uid = await self._refresh(placed)
            self.store.mark_seen(pack_uid)
            self.store.record_sticker(uid, placed.pack, file_id, pack_uid)
            if placed.created:
                await self._announce_pack(placed.pack, job)
            return OK, placed.pack
        raise Permanent(last_err or "gave up")

    async def _announce_pack(self, name: str, job: dict) -> None:
        chat = job.get("chat_id") or self.cfg.owner_id
        try:
            await self.tg(lambda: self.bot.send_message(
                chat, f"New pack created 👉 https://t.me/addstickers/{name}"
            ), "announce")
        except Exception as e:  # noqa: BLE001
            log.warning("Could not announce new pack: %s", e)

    # ----------------------------------------------------------------- worker
    def enqueue(self, job: dict) -> int:
        jid = self.store.enqueue(job)
        self.wake.set()
        return jid

    async def run_once(self) -> bool:
        """Process one due job. Returns False if the queue had nothing due."""
        nxt = self.store.next_job()
        if nxt is None:
            return False
        job_id, job, attempts = nxt
        try:
            outcome, detail = await self.process(job)
        except Transient as e:
            if attempts < 8:
                delay = min(600, 15 * 2 ** attempts)
                log.warning("Job %s transient failure (%s); retry in %ss", job_id, e, delay)
                self.store.defer_job(job_id, delay)
                return True
            outcome, detail = FAIL, str(e)
        except Permanent as e:
            outcome, detail = FAIL, str(e)
        except Exception as e:  # noqa: BLE001 — unexpected bug: retry a couple of times
            log.exception("Job %s crashed", job_id)
            if attempts < 2:
                self.store.defer_job(job_id, 30)
                return True
            outcome, detail = FAIL, f"internal error: {e}"

        self.store.finish_job(job_id)
        self.stats[outcome] += 1
        if outcome == FAIL:
            self.stats["last_error"] = f"{job.get('uid')}: {detail}"
            log.error("Sticker %s failed: %s", job.get("uid"), detail)
        else:
            log.info("Sticker %s: %s (%s)", job.get("uid"), outcome, detail)
        await self._feedback(job, outcome, detail)
        return True

    async def _feedback(self, job: dict, outcome: str, detail: str) -> None:
        chat, msg_id = job.get("chat_id"), job.get("message_id")
        if chat and msg_id and (outcome != DUP or job.get("react_dup", True)):
            try:
                await self.tg(lambda: self.bot.set_message_reaction(
                    chat_id=chat, message_id=msg_id, reaction=[ReactionTypeEmoji(emoji=REACTIONS[outcome])]
                ), "react")
            except Exception as e:  # noqa: BLE001 — message deleted, reactions off, etc.
                log.debug("Reaction failed: %s", e)
        if chat and outcome == FAIL and job.get("private"):
            await self._say(chat, f"Couldn't add that one: <code>{html.escape(detail[:300])}</code>")
        if chat and outcome == OK and job.get("troll") and self.cfg.luke_id \
                and random.random() < self.cfg.troll_chance:
            await self._say(chat, random.choice(TROLLS).format(luke=self.cfg.luke_id))
        if job.get("batch"):
            await self._batch_progress(job["batch"], outcome)

    async def _say(self, chat: int, text: str) -> None:
        try:
            await self.tg(lambda: self.bot.send_message(chat, text), "send")
        except Exception as e:  # noqa: BLE001
            log.warning("send_message failed: %s", e)

    # ------------------------------------------------------------ grab batches
    def start_batch(self, chat_id: int, label: str, total: int) -> str:
        key = f"batch:{int(time.time() * 1000)}:{random.randint(0, 9999)}"
        self.store.put(key, {"chat": chat_id, "label": label, "total": total, OK: 0, DUP: 0, FAIL: 0})
        return key

    async def _batch_progress(self, key: str, outcome: str) -> None:
        b = self.store.get(key)
        if not b:
            return
        b[outcome] += 1
        if b[OK] + b[DUP] + b[FAIL] >= b["total"]:
            self.store.delete(key)
            await self._say(b["chat"], (
                f"Done with <b>{html.escape(b['label'])}</b>: {b[OK]} added, "
                f"{b[DUP]} already had, {b[FAIL]} failed."
            ))
        else:
            self.store.put(key, b)

    async def worker(self) -> None:
        log.info("Worker started (%d jobs pending)", self.store.queue_size())
        hb = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._work_loop()
        finally:
            hb.cancel()

    async def _heartbeat_loop(self) -> None:
        # separate task so long flood waits / ffmpeg runs don't look like a hang
        while True:
            self.heartbeat()
            await asyncio.sleep(20)

    async def _work_loop(self) -> None:
        while True:
            try:
                did = await self.run_once()
            except Exception:  # noqa: BLE001 — never let the worker die
                log.exception("Worker loop error")
                did = False
                await self.sleep(5)
            if not did:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=20)
                except asyncio.TimeoutError:
                    pass

    def heartbeat(self) -> None:
        try:
            self.cfg.heartbeat_path.write_text(str(int(time.time())))
        except OSError:
            pass

    # ----------------------------------------------------------------- removal
    async def remove(self, pack_file_id: str, pack_uid: str) -> str:
        await self.tg(lambda: self.bot.delete_sticker_from_set(sticker=pack_file_id), "delete")
        row = self.store.sticker_by_pack_uid(pack_uid)
        self.store.unsee(pack_uid)
        if row:
            self.store.unsee(row["source_uid"])
            self.store.forget_sticker(row["id"])
            pack = row["pack_name"]
            sset = await self._get_set(pack)
            if sset is not None:
                self.store.set_pack_count(pack, len(sset.stickers), self.cfg.pack_limit)
            return pack
        return ""


class _Adopted(Exception):
    """Internal: _create_pack found an existing pack with room; retry placement."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name
