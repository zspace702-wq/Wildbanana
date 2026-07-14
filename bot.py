import os
import time
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import config
import storage
import scheduler
import utils

# =========================
# START / HELP
# =========================
HELP_TEXT = (
    "👋 Media Scheduler Bot\n\n"
    "QUEUE\n"
    "Send photos/videos to add them to the active queue.\n"
    "/queue — show active queue\n"
    "/clear — clear active queue\n"
    "/newqueue <name> — create + switch to a new queue\n"
    "/queues — list queues, tap to switch\n\n"
    "DESTINATIONS\n"
    "/adddest <name> <chat_id> — add a channel/group\n"
    "/destinations — list + enable/disable\n"
    "/checkdest <name> — health check a destination\n\n"
    "SENDING\n"
    "/sendnow — send whole queue immediately\n"
    "/startscheduler — begin auto-posting\n"
    "/stopscheduler — stop auto-posting\n"
    "/next — countdown to next scheduled post\n\n"
    "DASHBOARDS\n"
    "/dashboard — progress overview\n"
    "/stats — per-destination statistics\n\n"
    "SETTINGS\n"
    "/setinterval fixed <seconds>\n"
    "/setinterval random <min> <max>\n"
    "/setcaption <text|clear> — default caption used as fallback\n"
    "/togglecaption — ON: keep each item's own caption when forwarding.\n"
    "  OFF: always use the default caption instead\n"
    "/setmaxqueue <n>\n"
    "/setfiletypes photo,video\n"
    "/settimezone <tz e.g. Asia/Kolkata>\n"
    "/setdatetimeformat <strftime format>\n"
    "/settings — show current settings\n\n"
    "OTHER\n"
    "/id — show this chat's ID\n"
    "Send a .zip of photos/videos to bulk-add to the queue."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📌 Chat ID:\n{update.effective_chat.id}")


# =========================
# QUEUE MANAGEMENT
# =========================
async def new_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /newqueue <name>")
        return

    name = context.args[0]
    chat["queues"].setdefault(name, [])
    chat["active_queue"] = name
    await storage.save()
    await update.message.reply_text(f"✅ Queue '{name}' created and set active.")


async def list_queues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    buttons = []
    for name, items in chat["queues"].items():
        marker = "⭐ " if name == chat["active_queue"] else ""
        buttons.append([InlineKeyboardButton(
            f"{marker}{name} ({len(items)})", callback_data=f"useq:{name}"
        )])

    await update.message.reply_text(
        "📚 Queues (tap to switch):", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def switch_queue_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    chat = await storage.get_chat(query.message.chat_id)
    if name in chat["queues"]:
        chat["active_queue"] = name
        await storage.save()
        await query.edit_message_text(f"✅ Active queue is now '{name}'.")


async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    items = utils.active_queue(chat)

    dest_summary = ", ".join(
        f"{n}({'on' if d.get('enabled', True) else 'off'})"
        for n, d in chat["destinations"].items()
    ) or "none set — will send to this chat"

    await update.message.reply_text(
        f"📦 Queue '{chat['active_queue']}': {len(items)} items\n"
        f"📤 Destinations: {dest_summary}"
    )


async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    chat["queues"][chat["active_queue"]] = []
    await storage.save()
    await update.message.reply_text("🗑 Cleared active queue!")


# =========================
# MEDIA INGESTION
# =========================
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    settings = chat["settings"]

    media_type = None
    unique_id = None
    file_id = None

    if update.message.photo:
        media_type = "photo"
        p = update.message.photo[-1]
        file_id, unique_id = p.file_id, p.file_unique_id
    elif update.message.video:
        media_type = "video"
        v = update.message.video
        file_id, unique_id = v.file_id, v.file_unique_id

    if not file_id:
        return

    if media_type not in settings["allowed_types"]:
        await update.message.reply_text(f"❌ {media_type} not allowed by current settings.")
        return

    if utils.is_duplicate(chat, unique_id):
        await update.message.reply_text("⚠️ Duplicate detected — skipped.")
        return

    queue = chat["queues"][chat["active_queue"]]
    if len(queue) >= settings["max_queue_size"]:
        await update.message.reply_text(
            f"❌ Queue full (max {settings['max_queue_size']}). Use /clear or /newqueue."
        )
        return

    queue.append({
        "type": media_type,
        "file_id": file_id,
        "unique_id": unique_id,
        "source": "telegram",
        "caption": update.message.caption,
        "added_at": storage.now_str(chat),
    })
    utils.mark_seen(chat, unique_id)
    await storage.save()

    await update.message.reply_text(
        f"✅ Added to '{chat['active_queue']}'! Total: {len(queue)}"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles .zip uploads: extract media inside and bulk-add to queue."""
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".zip"):
        return

    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    settings = chat["settings"]

    await update.message.reply_text("📥 Downloading zip...")
    os.makedirs(config.MEDIA_DIR, exist_ok=True)
    zip_path = os.path.join(config.MEDIA_DIR, f"{chat_id}_{int(time.time())}.zip")
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(zip_path)

    extract_dir = os.path.join(config.MEDIA_DIR, f"{chat_id}_{int(time.time())}")
    try:
        extracted = await utils.extract_zip_media(zip_path, extract_dir)
    except Exception as e:
        await update.message.reply_text(f"❌ ZIP extraction failed: {e}")
        return
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    queue = chat["queues"][chat["active_queue"]]
    added, skipped = 0, 0
    for item in extracted:
        if item["type"] not in settings["allowed_types"]:
            skipped += 1
            continue
        if len(queue) >= settings["max_queue_size"]:
            skipped += 1
            continue
        queue.append({
            "type": item["type"],
            "path": item["path"],
            "source": "local",
            "added_at": storage.now_str(chat),
        })
        added += 1

    await storage.save()
    await update.message.reply_text(
        f"✅ ZIP extraction completed: {added} added, {skipped} skipped.\n"
        f"📦 Queue '{chat['active_queue']}': {len(queue)} items"
    )
    await utils.notify_admin(
        context.bot, f"📦 Chat {chat_id} bulk-uploaded a zip: {added} items added."
    )


# =========================
# DESTINATIONS
# =========================
async def add_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: /adddest <name> <chat_id>")
        return

    name = context.args[0]
    try:
        dest_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid chat_id")
        return

    chat["destinations"][name] = {
        "chat_id": dest_id, "enabled": True, "sent": 0, "failed": 0,
        "last_checked": None, "healthy": None,
    }
    await storage.save()
    await update.message.reply_text(f"✅ Destination '{name}' -> {dest_id} added.")


async def list_destinations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not chat["destinations"]:
        await update.message.reply_text("No destinations set. Use /adddest <name> <chat_id>.")
        return

    buttons = []
    lines = ["📤 Destinations"]
    for name, d in chat["destinations"].items():
        state = "🟢 on" if d.get("enabled", True) else "🔴 off"
        health = {"True": "✅", "False": "⚠️"}.get(str(d.get("healthy")), "❔")
        lines.append(f"• {name} ({d['chat_id']}) — {state} {health}  sent:{d['sent']} failed:{d['failed']}")
        buttons.append([InlineKeyboardButton(
            f"Toggle {name}", callback_data=f"toggledest:{name}"
        )])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def toggle_destination_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    chat = await storage.get_chat(query.message.chat_id)
    dest = chat["destinations"].get(name)
    if not dest:
        return
    dest["enabled"] = not dest.get("enabled", True)
    await storage.save()
    await query.edit_message_text(
        f"✅ '{name}' is now {'enabled 🟢' if dest['enabled'] else 'disabled 🔴'}."
    )


async def check_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /checkdest <name>")
        return

    name = context.args[0]
    dest = chat["destinations"].get(name)
    if not dest:
        await update.message.reply_text("❌ No such destination.")
        return

    try:
        await context.bot.get_chat(dest["chat_id"])
        dest["healthy"] = True
        msg = f"✅ '{name}' is reachable."
    except Exception as e:
        dest["healthy"] = False
        msg = f"⚠️ '{name}' health check failed: {e}"

    dest["last_checked"] = storage.now_str(chat)
    await storage.save()
    await update.message.reply_text(msg)


# =========================
# SENDING (manual immediate dump)
# =========================
async def send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    queue = utils.active_queue(chat)

    if not queue:
        await update.message.reply_text("⚠ Empty queue")
        return

    total = len(queue)
    await update.message.reply_text(f"🚀 Sending {total} items now (ignores interval)...")

    sent = 0
    while utils.queue_len(chat) > 0:
        _, item = await scheduler._send_one_item(context.bot, chat_id)
        if item is None:
            break
        sent += 1
        chat = await storage.get_chat(chat_id)

    await update.message.reply_text(f"✅ Upload finished: {sent}/{total} sent.")
    await utils.notify_admin(context.bot, f"✅ Chat {chat_id} sent {sent}/{total} via /sendnow.")


async def start_scheduler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ok, msg = await scheduler.start_for_chat(context.application, chat_id)
    await update.message.reply_text(msg)
    if ok:
        await utils.notify_admin(context.bot, f"⏱ Scheduler started for chat {chat_id}.")


async def stop_scheduler_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await scheduler.stop_for_chat(chat_id)
    await update.message.reply_text(msg)
    await utils.notify_admin(context.bot, f"🛑 Scheduler stopped for chat {chat_id}.")


async def next_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not chat.get("scheduler_running") or not chat.get("next_post_time"):
        await update.message.reply_text("⏱ Scheduler is not running.")
        return

    run_at = datetime.fromisoformat(chat["next_post_time"])
    remaining = (run_at - datetime.now()).total_seconds()
    await update.message.reply_text(
        f"⏳ Next post in {utils.fmt_duration(remaining)} "
        f"(around {run_at.strftime(chat['settings']['datetime_format'])})."
    )


# =========================
# DASHBOARDS
# =========================
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    s = chat["settings"]

    q_lines = [f"  • {n}: {len(items)} items" for n, items in chat["queues"].items()]

    if chat.get("scheduler_running") and chat.get("next_post_time"):
        run_at = datetime.fromisoformat(chat["next_post_time"])
        remaining = max(0, (run_at - datetime.now()).total_seconds())
        sched_line = f"🟢 running — next post in {utils.fmt_duration(remaining)}"
    else:
        sched_line = "🔴 stopped"

    mode_line = (
        f"fixed {s['fixed_delay']}s" if s["mode"] == "fixed"
        else f"random {s['min_delay']}-{s['max_delay']}s"
    )

    text = (
        "📊 Progress Dashboard\n\n"
        "Queues:\n" + "\n".join(q_lines) + "\n\n"
        f"Active queue: {chat['active_queue']}\n"
        f"Scheduler: {sched_line}\n"
        f"Interval mode: {mode_line}\n"
        f"Destinations: {len(chat['destinations'])} "
        f"({sum(1 for d in chat['destinations'].values() if d.get('enabled', True))} enabled)\n"
        f"Total sent: {chat['stats']['total_sent']} | "
        f"Total failed: {chat['stats']['total_failed']}"
    )
    await update.message.reply_text(text)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not chat["destinations"]:
        await update.message.reply_text("No destinations set yet.")
        return

    lines = ["📈 Destination statistics"]
    for name, d in chat["destinations"].items():
        lines.append(
            f"• {name} ({d['chat_id']}): sent {d['sent']}, failed {d['failed']}, "
            f"{'enabled' if d.get('enabled', True) else 'disabled'}, "
            f"last check: {d.get('last_checked') or 'never'}"
        )
    await update.message.reply_text("\n".join(lines))


# =========================
# SETTINGS
# =========================
async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    args = context.args

    if not args:
        await update.message.reply_text(
            "❌ Usage:\n/setinterval fixed <seconds>\n/setinterval random <min> <max>"
        )
        return

    mode = args[0].lower()
    if mode == "fixed" and len(args) >= 2:
        try:
            chat["settings"]["mode"] = "fixed"
            chat["settings"]["fixed_delay"] = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ Seconds must be a number.")
            return
        await storage.save()
        await update.message.reply_text(f"✅ Fixed interval set to {args[1]}s.")
    elif mode == "random" and len(args) >= 3:
        try:
            lo, hi = int(args[1]), int(args[2])
            chat["settings"]["mode"] = "random"
            chat["settings"]["min_delay"] = lo
            chat["settings"]["max_delay"] = hi
        except ValueError:
            await update.message.reply_text("❌ Min/max must be numbers.")
            return
        await storage.save()
        await update.message.reply_text(f"✅ Random interval set to {lo}-{hi}s.")
    else:
        await update.message.reply_text(
            "❌ Usage:\n/setinterval fixed <seconds>\n/setinterval random <min> <max>"
        )


async def toggle_original_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    chat["settings"]["use_original_caption"] = not chat["settings"].get("use_original_caption", True)
    await storage.save()

    state = "ON" if chat["settings"]["use_original_caption"] else "OFF"
    await update.message.reply_text(
        f"✅ Forwarding with original caption is now {state}.\n"
        f"(When ON: each photo/video keeps the caption it was sent with. "
        f"When OFF, or if an item had no caption: the default caption from "
        f"/setcaption is used instead.)"
    )


async def set_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /setcaption <text|clear>")
        return

    text = " ".join(context.args)
    chat["settings"]["caption"] = "" if text.lower() == "clear" else text
    await storage.save()
    await update.message.reply_text("✅ Default caption updated.")


async def set_max_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Usage: /setmaxqueue <n>")
        return

    chat["settings"]["max_queue_size"] = int(context.args[0])
    await storage.save()
    await update.message.reply_text(f"✅ Max queue size set to {context.args[0]}.")


async def set_file_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /setfiletypes photo,video")
        return

    types = [t.strip().lower() for t in " ".join(context.args).split(",") if t.strip()]
    valid = {"photo", "video"}
    if not types or not set(types).issubset(valid):
        await update.message.reply_text("❌ Allowed values: photo, video")
        return

    chat["settings"]["allowed_types"] = types
    await storage.save()
    await update.message.reply_text(f"✅ Allowed file types: {', '.join(types)}")


async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /settimezone Asia/Kolkata")
        return

    tz = context.args[0]
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        await update.message.reply_text("❌ Unknown timezone.")
        return

    chat["settings"]["timezone"] = tz
    await storage.save()
    await update.message.reply_text(f"✅ Timezone set to {tz}.")


async def set_datetime_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)

    if not context.args:
        await update.message.reply_text("❌ Usage: /setdatetimeformat %Y-%m-%d %H:%M:%S")
        return

    fmt = " ".join(context.args)
    try:
        datetime.now().strftime(fmt)
    except Exception:
        await update.message.reply_text("❌ Invalid strftime format.")
        return

    chat["settings"]["datetime_format"] = fmt
    await storage.save()
    await update.message.reply_text(f"✅ Datetime format set to: {fmt}")


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = await storage.get_chat(chat_id)
    s = chat["settings"]

    text = (
        "⚙️ Settings\n"
        f"Mode: {s['mode']}\n"
        f"Fixed delay: {s['fixed_delay']}s\n"
        f"Random range: {s['min_delay']}-{s['max_delay']}s\n"
        f"Default caption: {s['caption'] or '(none)'}\n"
        f"Forward with original caption: {'ON' if s.get('use_original_caption', True) else 'OFF'}\n"
        f"Max queue size: {s['max_queue_size']}\n"
        f"Allowed types: {', '.join(s['allowed_types'])}\n"
        f"Timezone: {s['timezone']}\n"
        f"Datetime format: {s['datetime_format']}"
    )
    await update.message.reply_text(text)


# =========================
# MAIN
# =========================
def main():
    async def _on_startup(app):
        # AsyncIOScheduler needs a running event loop to attach to.
        # PTB doesn't create one until run_polling() starts, so we
        # start APScheduler here instead of in main().
        scheduler.start()

    app = ApplicationBuilder().token(config.BOT_TOKEN).post_init(_on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_chat_id))

    app.add_handler(CommandHandler("newqueue", new_queue))
    app.add_handler(CommandHandler("queues", list_queues))
    app.add_handler(CommandHandler("queue", queue_status))
    app.add_handler(CommandHandler("clear", clear_queue))

    app.add_handler(CommandHandler("adddest", add_destination))
    app.add_handler(CommandHandler("destinations", list_destinations))
    app.add_handler(CommandHandler("checkdest", check_destination))

    app.add_handler(CommandHandler("sendnow", send_now))
    app.add_handler(CommandHandler("startscheduler", start_scheduler_cmd))
    app.add_handler(CommandHandler("stopscheduler", stop_scheduler_cmd))
    app.add_handler(CommandHandler("next", next_post))

    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(CommandHandler("setinterval", set_interval))
    app.add_handler(CommandHandler("setcaption", set_caption))
    app.add_handler(CommandHandler("togglecaption", toggle_original_caption))
    app.add_handler(CommandHandler("setmaxqueue", set_max_queue))
    app.add_handler(CommandHandler("setfiletypes", set_file_types))
    app.add_handler(CommandHandler("settimezone", set_timezone))
    app.add_handler(CommandHandler("setdatetimeformat", set_datetime_format))
    app.add_handler(CommandHandler("settings", show_settings))

    app.add_handler(CallbackQueryHandler(switch_queue_cb, pattern=r"^useq:"))
    app.add_handler(CallbackQueryHandler(toggle_destination_cb, pattern=r"^toggledest:"))

    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("🤖 Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
