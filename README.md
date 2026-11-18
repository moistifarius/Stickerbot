# Stickerbot

A Telegram "sticker hoover": every sticker posted where the bot can see it is
added to your own sticker packs, with no duplicates. It can also turn photos, GIFs and
videos into stickers, and grab whole packs.

## What it does

| | |
|---|---|
| **Hoover** | Any sticker in a group the bot is in (or sent to it in DM) goes into the current pack. Static, animated and video stickers share packs (Telegram allows mixed sets of up to 120). When a pack is full, a new one is created and its link posted. |
| **Feedback** | 👍 added · 👌 already had (DM only, to keep groups quiet) · 👎 failed (in DM you also get the reason) |
| **Make stickers** | Trusted users can DM a photo, image file, GIF or short video. Images are scaled so the long side is 512 px. Video is converted to VP9 WEBM: no audio, 512 px, ≤30 fps, ≤3 s (clips up to ~9 s are sped up, longer ones trimmed), ≤256 KB. An emoji at the start of the caption sets the sticker's emoji. |
| `/steal` | Reply to a photo, GIF, video or sticker in a group to add it. |
| `/grab` | Reply to a sticker (or `/grab https://t.me/addstickers/NAME`) to hoover the whole pack. Reports a summary when finished. |
| `/packs` | Lists our packs with counts. |
| Owner | `/undo` removes the last added sticker, `/remove` (as a reply) removes that sticker from our pack, `/status`, `/logs [n]`. |

## Reliability (what v1 got wrong)

- **Flood control is waited out.** Telegram's `RetryAfter` used to 👎 and drop the sticker. Network and 5xx errors now back off and retry.
- **The queue is persistent** (SQLite in `/data`). A restart or crash loses nothing, and there is no 50-item cap.
- **Telegram is the source of truth.** At startup the bot finds existing `<base>_<n>_by_<bot>` packs, reads their real counts and stickers, and carries on in the newest pack that has room. If Telegram says a pack is full, the bot rolls over to a new pack.
- **Fixed:**
  - The first sticker of every new pack was added twice.
  - Switching between static and animated stickers started a new pack each time.
  - Failed resizes were marked 👍 and never retried.
  - The "circuit breaker" slowed every sticker forever once tripped.
  - Pack names could lose their `_by_<bot>` suffix.
- **Fallbacks:** if Telegram rejects reusing a sticker's `file_id`, the bot downloads, normalizes and re-uploads it. If it rejects an emoji, the bot uses the default emoji instead.
- **Migration:** a v1 `pack_state.json` in the data dir is imported once (its seen list and current pack).

## Run (Docker)

```bash
cp .env.example .env    # fill BOT_TOKEN and OWNER_ID
mkdir -p data && chown 99:100 data
docker compose up -d --build
docker compose logs -f
```

Requirements on the Telegram side:
1. Use the **same bot token** as before. Packs belong to the bot that created them.
2. The owner (`OWNER_ID`) must have opened a DM with the bot and pressed **Start**.
3. To see stickers in groups, turn off **Group Privacy** in @BotFather (`/setprivacy` → Disable) and re-add the bot, or make it a group admin.

Configuration is documented in `.env.example`. The container runs as 99:100 and has
a heartbeat healthcheck.

## Develop

```bash
docker build -t stickerbot:local .
docker run --rm -u 0 -v "$PWD/tests:/app/tests:ro" stickerbot:local \
  sh -c 'pip install -q pytest && python -m pytest -q tests'
```

The tests use a fake Telegram bot (`tests/fakebot.py`) for the queue, pack and retry
logic, and real ffmpeg and Pillow for the media conversion.
