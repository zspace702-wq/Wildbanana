# Telegram Media Scheduler Bot

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: set BOT_TOKEN (from @BotFather) and ADMIN_ID (your own Telegram user id, get it via /id)
python bot.py
```

State (queues, destinations, settings, stats) persists to `data.json` next
to the script, so restarting the bot doesn't wipe your queue — **except on
Railway and similar platforms, see below.**

## Deploying on Railway

1. Push this folder to a GitHub repo. **Do not commit `.env`** — it's in
   `.gitignore`. Set `BOT_TOKEN` and `ADMIN_ID` as environment variables in
   the Railway dashboard instead (Variables tab), plus any other settings
   from `.env.example` you want to override.
2. Railway will detect Python via `requirements.txt` and use the `Procfile`
   (`worker: python bot.py`) to start it as a background worker — it does
   **not** need a public port, since `run_polling()` isn't an HTTP server.
   If Railway shows a "no open port detected" warning, ignore it or set the
   service type to "Worker" explicitly in settings.
3. **Attach a Volume.** Railway's container filesystem resets on every
   redeploy. Without a volume, `data.json` (all your queues/settings/stats)
   and `media_cache/` (extracted zip files) disappear each time you push.
   In Railway: your service → Settings → Volumes → add one, mount it at
   e.g. `/data`, then set `DATA_FILE=/data/data.json` and
   `MEDIA_DIR=/data/media_cache` as environment variables.
4. Deploy. Check the Railway logs for `🤖 Bot running...` — if the token or
   admin id is wrong you'll see the error there immediately.

## How it fits together

- `config.py` — loads `.env`
- `storage.py` — JSON persistence, one record per chat
- `utils.py` — duplicate detection, zip extraction, delay math, admin DM helper
- `scheduler.py` — APScheduler engine: posts one item at a time, reschedules
  itself with a fixed or random delay, stops + notifies when the queue empties
- `bot.py` — all Telegram command handlers + entrypoint

## Commands

**Queues**
- Send a photo/video → added to the active queue (duplicates auto-skipped)
- Send a `.zip` of photos/videos → bulk-extracted into the active queue
- `/queue` — active queue size + destinations
- `/clear` — empty active queue
- `/newqueue <name>` — create and switch to a new named queue
- `/queues` — list all queues, tap to switch

**Destinations** (channels/groups you post to)
- `/adddest <name> <chat_id>` — register a destination
- `/destinations` — list, tap to enable/disable
- `/checkdest <name>` — health check (confirms the bot can still reach it)

If no destination is added, the bot posts back into the current chat.
Every enabled destination gets a copy of each queued item.

**Sending**
- `/sendnow` — dump the whole active queue immediately, ignoring the interval
- `/startscheduler` — begin auto-posting at the configured interval
- `/stopscheduler` — stop auto-posting
- `/next` — countdown to the next scheduled post

**Dashboards**
- `/dashboard` — queue sizes, scheduler status, countdown, totals
- `/stats` — sent/failed counts and last health check per destination

**Captions**
- Every photo/video you send keeps its own caption automatically.
- `/togglecaption` — flip between: ON (default) = forward each item with the
  caption it originally had; OFF = always use the default caption instead.
- `/setcaption <text|clear>` — sets the fallback caption, used when
  original-caption mode is OFF, or when an item had no caption to begin with
  (e.g. media extracted from a zip never has one).

**Settings**
- `/setinterval fixed <seconds>`
- `/setinterval random <min> <max>`
- `/setcaption <text|clear>` — caption applied to every post
- `/setmaxqueue <n>`
- `/setfiletypes photo,video`
- `/settimezone Asia/Kolkata` — any IANA timezone name
- `/setdatetimeformat <strftime format>`
- `/settings` — show all current settings

**Notifications** (sent automatically)
- Queue completed / scheduler stopped
- Upload finished (`/sendnow`) and upload failed (per item)
- ZIP extraction completed
- Scheduler started / stopped
- Next-post progress after each auto-post
- Admin DMs (`ADMIN_ID`) on zip uploads, scheduler start/stop, and failures

## Known limitations / things to verify yourself

- **Not live-tested against Telegram's servers.** This was built and syntax/import
  checked in a sandbox with no network access to `api.telegram.org`, so please
  run it against your real bot token and watch the console for errors on first launch.
- Duplicate detection is per-chat, based on Telegram's `file_unique_id` for
  bot-sent media, and on extracted filenames for zips — it won't catch a
  pixel-identical image re-uploaded under a different file.
- Broadcasting to *every* enabled destination on each post is a deliberate
  choice for "multiple channels/groups" — if you instead want different
  queues going to different single destinations, that's a small change to
  `scheduler._send_one_item`, happy to add it.
- `ADMIN_ID=0` (default) silently disables admin notifications — the admin
  must have started a DM with the bot at least once for `send_message` to work.
