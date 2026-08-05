import os
import re
from datetime import datetime, timedelta, time as dtime

from dateparser.search import search_dates
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.environ.get("MONGODB_DB", "productivity_bot")

TYPE_EMOJI = {"Task": "\U0001F4CC", "Note": "\U0001F4DD", "Habit": "\U0001F501"}
TIME_HINT_RE = re.compile(r"\d{1,2}(:\d{2})?\s*(am|pm)\b|\d{1,2}:\d{2}\b", re.I)
FILLER_RE = re.compile(r"\b(deadline|due|by|on)\b\.?$", re.I)


# ---------- storage (MongoDB) ----------

async def get_next_id(db):
    counter = await db.counters.find_one_and_update(
        {"_id": "entry_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return counter["seq"]


async def add_entry(db, entry_type, text, chat_id, due=None, remind_at=None, daily_time=None):
    entry_id = await get_next_id(db)
    entry = {
        "id": entry_id,
        "type": entry_type,
        "text": text,
        "created": datetime.now().isoformat(timespec="minutes"),
        "due": due,                # ISO date/datetime string, or None
        "remind_at": remind_at,    # ISO datetime string for one-off reminders
        "daily_time": daily_time,  # "HH:MM" for recurring habit reminders
        "done": False,
        "chat_id": chat_id,
    }
    await db.entries.insert_one(entry)
    return entry


async def get_all_entries(db):
    return [doc async for doc in db.entries.find({})]


async def get_entries(db, entry_type=None, done=None):
    query = {}
    if entry_type is not None:
        query["type"] = entry_type
    if done is not None:
        query["done"] = done
    return [doc async for doc in db.entries.find(query)]


async def mark_done(db, entry_id):
    result = await db.entries.update_one({"id": entry_id}, {"$set": {"done": True}})
    return result.modified_count > 0


# ---------- date/time parsing ----------

def parse_hhmm(text):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mnt <= 59:
        return h, mnt
    return None


def extract_date(text):
    """Try to find a date/deadline in freeform text. Returns
    (due_datetime, remind_at_datetime, clean_title, has_explicit_time) or None."""
    results = search_dates(
        text,
        settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.now()},
    )
    if not results:
        return None
    matched_str, dt = results[-1]
    has_time = bool(TIME_HINT_RE.search(matched_str))

    title = text.replace(matched_str, "").strip(" -,:")
    title = FILLER_RE.sub("", title).strip(" -,:")
    if not title:
        title = text.strip()

    remind_at = dt if has_time else dt.replace(hour=9, minute=0, second=0, microsecond=0)
    return dt, remind_at, title, has_time


# ---------- reminder scheduling ----------

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    entry = context.job.data
    emoji = TYPE_EMOJI.get(entry["type"], "\u23F0")
    await context.bot.send_message(
        chat_id=entry["chat_id"],
        text=f"{emoji} Reminder ({entry['type']} #{entry['id']}): {entry['text']}",
    )


def schedule_entry(app, entry):
    if entry.get("done"):
        return
    job_name = f"entry-{entry['id']}"
    for j in app.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()

    if entry["type"] == "Habit" and entry.get("daily_time"):
        h, mnt = parse_hhmm(entry["daily_time"])
        app.job_queue.run_daily(send_reminder, time=dtime(hour=h, minute=mnt), data=entry, name=job_name)
    elif entry.get("remind_at"):
        due = datetime.fromisoformat(entry["remind_at"])
        if due <= datetime.now():
            due = datetime.now() + timedelta(seconds=10)
        app.job_queue.run_once(send_reminder, when=due, data=entry, name=job_name)


async def reschedule_all(app):
    db = app.bot_data["db"]
    for entry in await get_all_entries(db):
        schedule_entry(app, entry)


# ---------- handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! Just text me things naturally:\n"
        "  \u2022 \"assignment 10 aug deadline\" \u2192 auto-saved as a Task, reminder set\n"
        "  \u2022 anything without a date \u2192 I'll ask if it's a Task / Note / Habit\n\n"
        "/today \u2014 what's due today\n"
        "/tasks or \"tasks\" \u2014 pending tasks\n"
        "/donetasks or \"done tasks\" \u2014 completed tasks\n"
        "/list \u2014 everything pending\n"
        "/done <id> \u2014 mark a task done\n"
        "/cancel \u2014 cancel a pending question"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data["db"]
    text = update.message.text.strip()

    if context.user_data.get("awaiting_time"):
        await process_time_reply(update, context, text)
        return

    lower = text.lower().strip()
    if lower in ("tasks", "task list", "pending tasks", "my tasks"):
        await list_tasks(update, context, completed=False)
        return
    if lower in ("done tasks", "completed tasks", "finished tasks"):
        await list_tasks(update, context, completed=True)
        return

    parsed = extract_date(text)
    if parsed:
        due_dt, remind_dt, title, has_time = parsed
        entry = await add_entry(
            db, "Task", title, update.effective_chat.id,
            due=due_dt.isoformat(), remind_at=remind_dt.isoformat(),
        )
        schedule_entry(context.application, entry)
        due_str = due_dt.strftime("%d %b, %I:%M %p") if has_time else due_dt.strftime("%d %b")
        remind_str = remind_dt.strftime("%d %b, %I:%M %p")
        await update.message.reply_text(
            f"\U0001F4CC Saved Task #{entry['id']}: {title}\nDue {due_str} \u2014 I'll remind you {remind_str}."
        )
        return

    context.user_data["pending_text"] = text
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("\U0001F4CC Task", callback_data="type:Task"),
                InlineKeyboardButton("\U0001F4DD Note", callback_data="type:Note"),
                InlineKeyboardButton("\U0001F501 Habit", callback_data="type:Habit"),
            ]
        ]
    )
    await update.message.reply_text("Didn't catch a date — how should I file this?", reply_markup=keyboard)


async def type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data["db"]
    query = update.callback_query
    await query.answer()
    entry_type = query.data.split(":")[1]
    pending_text = context.user_data.get("pending_text", "")

    if entry_type == "Note":
        entry = await add_entry(db, "Note", pending_text, update.effective_chat.id)
        context.user_data.clear()
        await query.edit_message_text(f"Noted \U0001F4DD (#{entry['id']}). No reminder set.")
        return

    context.user_data["pending_type"] = entry_type
    context.user_data["awaiting_time"] = True
    await query.edit_message_text(
        f"Filed as {entry_type}. What time should I remind you? (24h, e.g. 18:30)\n"
        "Send 'skip' for no reminder."
    )


async def process_time_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    db = context.bot_data["db"]
    entry_type = context.user_data.get("pending_type", "Task")
    pending_text = context.user_data.get("pending_text", "")

    if text.lower() == "skip":
        entry = await add_entry(db, entry_type, pending_text, update.effective_chat.id)
        context.user_data.clear()
        await update.message.reply_text(f"{TYPE_EMOJI[entry_type]} Saved (#{entry['id']}) \u2014 no reminder.")
        return

    parsed = parse_hhmm(text)
    if not parsed:
        await update.message.reply_text("Didn't catch that time \u2014 use HH:MM (e.g. 07:30) or 'skip'.")
        return

    h, mnt = parsed
    hhmm = f"{h:02d}:{mnt:02d}"

    if entry_type == "Habit":
        entry = await add_entry(db, entry_type, pending_text, update.effective_chat.id, daily_time=hhmm)
    else:
        now = datetime.now()
        due = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        entry = await add_entry(db, entry_type, pending_text, update.effective_chat.id, due=due.isoformat(), remind_at=due.isoformat())

    schedule_entry(context.application, entry)
    context.user_data.clear()
    suffix = "daily" if entry_type == "Habit" else "once"
    await update.message.reply_text(f"{TYPE_EMOJI[entry_type]} Saved (#{entry['id']}) \u2014 reminder at {hhmm} ({suffix}).")


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, completed: bool):
    db = context.bot_data["db"]
    tasks = await get_entries(db, entry_type="Task", done=completed)
    if not tasks:
        await update.message.reply_text("No completed tasks yet." if completed else "No pending tasks.")
        return
    lines = []
    for e in sorted(tasks, key=lambda x: x["id"]):
        added = datetime.fromisoformat(e["created"]).strftime("%d %b")
        due = datetime.fromisoformat(e["due"]).strftime("%d %b") if e.get("due") else "no deadline"
        lines.append(f"#{e['id']} {e['text']} \u2014 added {added}, due {due}")
    header = "\u2705 Completed tasks:\n" if completed else "\U0001F4CC Pending tasks:\n"
    await update.message.reply_text(header + "\n".join(lines))


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await list_tasks(update, context, completed=False)


async def donetasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await list_tasks(update, context, completed=True)


async def list_entries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data["db"]
    pending = await get_entries(db, done=False)
    if not pending:
        await update.message.reply_text("Nothing pending. Clean slate.")
        return
    lines = []
    for e in sorted(pending, key=lambda x: x["id"]):
        info = ""
        if e.get("due"):
            d = datetime.fromisoformat(e["due"])
            info = f" (due {d.strftime('%d %b')})"
        elif e.get("daily_time"):
            info = f" (daily @ {e['daily_time']})"
        lines.append(f"#{e['id']} {TYPE_EMOJI.get(e['type'],'')} {e['text']}{info}")
    await update.message.reply_text("\n".join(lines))


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data["db"]
    now = datetime.now()
    entries = await get_entries(db, done=False)
    due_today = []
    for e in entries:
        if e.get("due"):
            d = datetime.fromisoformat(e["due"])
            if d.date() == now.date():
                due_today.append((d.strftime("%I:%M %p"), e))
        elif e.get("daily_time"):
            due_today.append((e["daily_time"], e))
    if not due_today:
        await update.message.reply_text("Nothing due today.")
        return
    due_today.sort(key=lambda x: x[0])
    lines = [f"{t} \u2014 {TYPE_EMOJI.get(e['type'],'')} {e['text']} (#{e['id']})" for t, e in due_today]
    await update.message.reply_text("\n".join(lines))


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data["db"]
    if not context.args:
        await update.message.reply_text("Usage: /done <id>")
        return
    try:
        entry_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: /done <id>")
        return
    ok = await mark_done(db, entry_id)
    if ok:
        for j in context.application.job_queue.get_jobs_by_name(f"entry-{entry_id}"):
            j.schedule_removal()
        await update.message.reply_text(f"Marked #{entry_id} done \u2705")
    else:
        await update.message.reply_text(f"No entry #{entry_id}.")


# ---------- app setup ----------

async def post_init(application):
    client = AsyncIOMotorClient(MONGODB_URI)
    application.bot_data["mongo_client"] = client
    application.bot_data["db"] = client[MONGODB_DB]
    await reschedule_all(application)


def main():
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise SystemExit("Set your BOT_TOKEN (env var or in bot.py) before running.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_entries))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("donetasks", donetasks_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(type_chosen, pattern=r"^type:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()