import os
import time
import zipfile
import random
import uuid

import config
import storage

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def next_delay(settings: dict) -> int:
    if settings["mode"] == "random":
        lo, hi = settings["min_delay"], settings["max_delay"]
        if lo > hi:
            lo, hi = hi, lo
        return random.randint(lo, hi)
    return settings["fixed_delay"]


async def notify_admin(bot, text: str):
    """Best-effort DM to the configured admin. Silently ignores failure
    (e.g. admin never started the bot). `bot` is a telegram.Bot instance
    (e.g. context.bot or application.bot)."""
    if not config.ADMIN_ID:
        return
    try:
        await bot.send_message(config.ADMIN_ID, text)
    except Exception:
        pass


def is_duplicate(chat: dict, unique_id: str) -> bool:
    return unique_id in chat.get("seen_unique_ids", [])


def mark_seen(chat: dict, unique_id: str):
    chat.setdefault("seen_unique_ids", [])
    chat["seen_unique_ids"].append(unique_id)
    # keep this bounded so the JSON file doesn't grow forever
    if len(chat["seen_unique_ids"]) > 5000:
        chat["seen_unique_ids"] = chat["seen_unique_ids"][-5000:]


def active_queue(chat: dict) -> list:
    return chat["queues"][chat["active_queue"]]


def queue_len(chat: dict, name: str = None) -> int:
    name = name or chat["active_queue"]
    return len(chat["queues"].get(name, []))


async def extract_zip_media(zip_path: str, extract_dir: str) -> list:
    """Extract image/video files from a zip. Returns list of
    {'type': 'photo'|'video', 'path': str} dicts. Skips anything that
    isn't a recognized image/video extension, and guards against
    zip-slip path traversal."""
    os.makedirs(extract_dir, exist_ok=True)
    results = []

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            ext = os.path.splitext(name)[1].lower()
            if ext not in IMAGE_EXT and ext not in VIDEO_EXT:
                continue

            # zip-slip guard
            target = os.path.normpath(os.path.join(extract_dir, os.path.basename(name)))
            if not target.startswith(os.path.normpath(extract_dir)):
                continue

            # avoid collisions
            base, extn = os.path.splitext(target)
            if os.path.exists(target):
                target = f"{base}_{uuid.uuid4().hex[:6]}{extn}"

            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())

            media_type = "photo" if ext in IMAGE_EXT else "video"
            results.append({"type": media_type, "path": target})

    return results
