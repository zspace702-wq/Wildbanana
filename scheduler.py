"""
One AsyncIOScheduler shared across all chats. Each chat that has its
scheduler "running" gets a single one-shot job scheduled at a time;
after it fires we look at how much is left in the active queue and
either reschedule (fixed or random delay) or stop and notify.

This gives: fixed interval, random interval range, countdown to next
post (next_post_time is stored so /next can read it), queue completion
notification, scheduler started/stopped notifications, and posting to
every enabled destination (multi-channel/group support).
"""

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import storage
import utils

scheduler = AsyncIOScheduler()
_jobs = {}  # chat_id -> job id


def start():
    if not scheduler.running:
        scheduler.start()


def _job_id(chat_id):
    return f"post_{chat_id}"


async def _send_one_item(bot, chat_id: int):
    chat = await storage.get_chat(chat_id)
    queue = utils.active_queue(chat)

    if not queue:
        return chat, None

    item = queue.pop(0)
    settings = chat["settings"]

    original_caption = item.get("caption")
    if settings.get("use_original_caption", True) and original_caption:
        caption = original_caption
    else:
        caption = settings.get("caption") or None

    enabled_dests = {
        name: d for name, d in chat["destinations"].items() if d.get("enabled", True)
    }
    if not enabled_dests:
        # nowhere to send -> fall back to the chat itself
        enabled_dests = {"this chat": {"chat_id": chat_id, "enabled": True,
                                        "sent": 0, "failed": 0}}
        chat["destinations"].setdefault("this chat", enabled_dests["this chat"])

    for name, dest in enabled_dests.items():
        dest_chat_id = dest["chat_id"]
        try:
            if item.get("source") == "local":
                path = item["path"]
                if item["type"] == "video":
                    with open(path, "rb") as f:
                        await bot.send_video(dest_chat_id, f, caption=caption)
                else:
                    with open(path, "rb") as f:
                        await bot.send_photo(dest_chat_id, f, caption=caption)
            else:
                file_id = item["file_id"]
                if item["type"] == "video":
                    await bot.send_video(dest_chat_id, file_id, caption=caption)
                else:
                    await bot.send_photo(dest_chat_id, file_id, caption=caption)

            dest["sent"] = dest.get("sent", 0) + 1
            chat["stats"]["total_sent"] += 1

        except Exception as e:
            dest["failed"] = dest.get("failed", 0) + 1
            chat["stats"]["total_failed"] += 1
            await bot.send_message(chat_id, f"❌ Upload failed to {name}: {e}")
            await utils.notify_admin(
                bot, f"⚠️ Upload failed for chat {chat_id} -> {name}: {e}"
            )

    await storage.save()
    return chat, item


async def _post_job(app, chat_id: int):
    bot = app.bot
    chat = await storage.get_chat(chat_id)

    if not chat.get("scheduler_running"):
        return

    queue_before = utils.queue_len(chat)
    if queue_before == 0:
        chat["scheduler_running"] = False
        chat["next_post_time"] = None
        await storage.save()
        await bot.send_message(chat_id, "✅ Queue completed — scheduler stopped.")
        if _cfg_admin():
            await bot.send_message(_cfg_admin(), f"✅ Queue for chat {chat_id} completed.")
        return

    chat, item = await _send_one_item(bot, chat_id)

    remaining = utils.queue_len(chat)
    if remaining > 0:
        delay = utils.next_delay(chat["settings"])
        run_at = datetime.now() + timedelta(seconds=delay)
        chat["next_post_time"] = run_at.isoformat()
        await storage.save()
        scheduler.add_job(
            _post_job, "date", run_date=run_at, args=[app, chat_id],
            id=_job_id(chat_id), replace_existing=True,
        )
        await bot.send_message(
            chat_id,
            f"📤 Sent 1 item. {remaining} left. Next post in {utils.fmt_duration(delay)}.",
        )
    else:
        chat["scheduler_running"] = False
        chat["next_post_time"] = None
        await storage.save()
        await bot.send_message(chat_id, "✅ Queue completed — scheduler stopped.")


def _cfg_admin():
    import config
    return config.ADMIN_ID or None


async def start_for_chat(app, chat_id: int):
    chat = await storage.get_chat(chat_id)
    if utils.queue_len(chat) == 0:
        return False, "Queue is empty — nothing to schedule."

    chat["scheduler_running"] = True
    delay = utils.next_delay(chat["settings"])
    run_at = datetime.now() + timedelta(seconds=delay)
    chat["next_post_time"] = run_at.isoformat()
    await storage.save()

    scheduler.add_job(
        _post_job, "date", run_date=run_at, args=[app, chat_id],
        id=_job_id(chat_id), replace_existing=True,
    )
    return True, f"⏱ Scheduler started. First post in {utils.fmt_duration(delay)}."


async def stop_for_chat(chat_id: int):
    chat = await storage.get_chat(chat_id)
    chat["scheduler_running"] = False
    chat["next_post_time"] = None
    await storage.save()
    try:
        scheduler.remove_job(_job_id(chat_id))
    except Exception:
        pass
    return "🛑 Scheduler stopped."
