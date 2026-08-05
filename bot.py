"""
Productivity Bot — Telegram bot for structured input + reminders, backed by MongoDB.

Send it anything. If it can find a date, it auto-saves a Task with that
deadline and reminds you multiple times as it approaches (3 days before,
1 day before, and on the day). No date found -> it asks whether to file it
as a Task / Note / Habit / Event. Recurring weekly items (classes, gym,
etc.) go through /addschedule.

Setup:
  1. Message @BotFather on Telegram -> /newbot -> copy the token it gives you.
  2. Get a MongoDB connection string (MongoDB Atlas free cluster, or local mongod).
  3. pip install -r requirements.txt
  4. Set env vars:
       $env:BOT_TOKEN="123456:ABC-DEF..."
       $env:MONGODB_URI="mongodb+srv://user:pass@cluster.mongodb.net/?..."
  5. python bot.py
  6. Open a chat with your bot on Telegram and send /start.

Commands:
  /start          - welcome + instructions
  /today          - everything due or scheduled today
  /list           - all pending entries
  /tasks          - pending tasks only, with added date + deadline
  /donetasks      - completed tasks only
  /addschedule    - add a recurring weekly item (pick days + time)
  /myschedule     - list your recurring weekly items
  /done <id>      - mark a task/event done
  /cancel         - cancel whatever it's currently asking you
  (plain text)    - auto-detected as a dated Task, or classified via buttons
                    also try "tasks" / "done tasks"
"""

import os
import random
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

TYPE_EMOJI = {"Task": "\U0001F4CC", "Note": "\U0001F4DD", "Habit": "\U0001F501", "Event": "\U0001F4C5", "Schedule": "\U0001F5D3\uFE0F"}
TIME_HINT_RE = re.compile(r"\d{1,2}(:\d{2})?\s*(am|pm)\b|\d{1,2}:\d{2}\b", re.I)
FILLER_RE = re.compile(r"\b(deadline|due|by|on)\b\.?$", re.I)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Stages before a deadline to send a reminder at: (label, how long before due)
DEADLINE_STAGES = [
    ("3d", timedelta(days=3)),
    ("1d", timedelta(days=1)),
    ("due", timedelta(0)),
]

TASK_TEMPLATES = {
    "3d": [
        "\u23F3 \"{text}\" is due in 3 days ({due}). Plenty of time if you start now.",
        "\U0001F4CC Heads up \u2014 \"{text}\" is due {due}. 3 days left.",
    ],
    "1d": [
        "\u26A0\uFE0F \"{text}\" is due tomorrow ({due}). Might be worth starting today.",
        "\U0001F514 One day left for \"{text}\" \u2014 due {due}.",
    ],
    "due": [
        "\U0001F6A8 \"{text}\" is due today. You've got this.",
        "\U0001F3AF Today's the day \u2014 \"{text}\" is due now.",
    ],
}
HABIT_TEMPLATES = [
    "\U0001F501 Time for: {text}",
    "\u2705 Reminder \u2014 don't skip: {text}",
    "\U0001F4AA Keep the streak going: {text}",
]
SCHEDULE_TEMPLATES = [
    "\U0001F5D3\uFE0F Coming up: {text}",
    "\u23F0 Reminder \u2014 {text} is starting soon.",
    "\U0001F4C5 Don't forget: {text}",
]


# ---------- storage (MongoDB) ----------

async def get_next_id(db):
    counter = await db.counters.find_one_and_update(
        {"_id": "entry_id"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER,
    )
    return counter["seq"]


async def add_entry(db, entry_type, text, chat_id, due=None, daily_time=None, weekdays=None):
    entry_id = await get_next_id(db)
    entry = {
        "id": entry_id,
        "type": entry_type,
        "text": text,
        "created": datetime.now().isoformat(timespec="minutes"),
        "due": due,                # ISO datetime string, for Task/Event
        "daily_time": daily_time,  # "HH:MM", for Habit/Schedule
        "weekdays": weekdays,      # list of ints 0=Mon..6=Sun, for Schedule
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
    results = search_dates(text, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.now()})
    if not results:
        return None
    matched_str, dt = results[-1]
    has_time = bool(TIME_HINT_RE.search(matched_str))
    title = text.replace(matched_str, "").strip(" -,:")
    title = FILLER_RE.sub("", title).strip(" -,:")
    if not title:
        title = text.strip()
    if not has_time:
        dt = dt.replace(hour=23, minute=59, second=0, microsecond=0)  # end of due day
    return dt, title, has_time


# ---------- reminder scheduling ----------

async def send_task_reminder(context: ContextTypes.DEFAULT_TYPE):
    entry = context.job.data["entry"]
    stage = context.job.data["stage"]
    due_dt = datetime.fromisoformat(entry["due"])
    template = random.choice(TASK_TEMPLATES[stage])
    msg = template.format(text=entry["text"], due=due_dt.strftime("%d %b, %I:%M %p"))
    await context.bot.send_message(chat_id=entry["chat_id"], text=f"{msg}  (#{entry['id']})")


async def send_habit_reminder(context: ContextTypes.DEFAULT_TYPE):
    entry = context.job.data
    msg = random.choice(HABIT_TEMPLATES).format(text=entry["text"])
    await context.bot.send_message(chat_id=entry["chat_id"], text=f"{msg}  (#{entry['id']})")


async def send_schedule_reminder(context: ContextTypes.DEFAULT_TYPE):
    entry = context.job.data
    msg = random.choice(SCHEDULE_TEMPLATES).format(text=entry["text"])
    await context.bot.send_message(chat_id=entry["chat_id"], text=f"{msg}  (#{entry['id']})")


def clear_jobs(app, entry_id):
    for j in app.job_queue.jobs():
        if j.name and j.name.startswith(f"entry-{entry_id}-"):
            j.schedule_removal()


def schedule_entry(app, entry):
    if entry.get("done"):
        return
    clear_jobs(app, entry["id"])
    t = entry["type"]

    if t == "Habit" and entry.get("daily_time"):
        h, mnt = parse_hhmm(entry["daily_time"])
        app.job_queue.run_daily(send_habit_reminder, time=dtime(hour=h, minute=mnt), data=entry, name=f"entry-{entry['id']}-daily")

    elif t == "Schedule" and entry.get("weekdays") and entry.get("daily_time"):
        h, mnt = parse_hhmm(entry["daily_time"])
        for wd in entry["weekdays"]:
            app.job_queue.run_daily(
                send_schedule_reminder, time=dtime(hour=h, minute=mnt), days=(wd,), data=entry, name=f"entry-{entry['id']}-day{wd}"
            )

    elif t in ("Task", "Event") and entry.get("due"):
        due_dt = datetime.fromisoformat(entry["due"])
        now = datetime.now()
        scheduled_any = False
        for label, offset in DEADLINE_STAGES:
            fire_at = due_dt - offset
            if fire_at > now:
                app.job_queue.run_once(
                    send_task_reminder, when=fire_at, data={"entry": entry, "stage": label}, name=f"entry-{entry['id']}-{label}"
                )
                scheduled_any = True
        if not scheduled_any and due_dt > now - timedelta(days=1):
            # deadline is imminent/just passed — fire one immediate reminder
            app.job_queue.run_once(
                send_task_reminder, when=timedelta(seconds=10), data={"entry": entry, "stage": "due"}, name=f"entry-{entry['id']}-due"
            )


async def reschedule_all(app):
    db = app.bot_data["db"]
    for entry in await get_all_entries(db):
        schedule_entry(app, entry)


# ---------- day-picker keyboard (for /addschedule) ----------

def build_day_keyboard(selected):
    row1, row2 = [], []
    for i, name in enumerate(DAY_NAMES):
        label = f"\u2705 {name}" if i in selected else name
        btn = InlineKeyboardButton(label, callback_data=f"day:{i}")
        (row1 if i < 4 else row2).append(btn)
    done_row = [InlineKeyboardButton("Done \u2192", callback_data="day:done")]
    return InlineKeyboardMarkup([row1, row2, done_row])


# ---------- handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! Just text me things naturally:\n"
        "  \u2022 \"assignment 10 aug deadline\" \u2192 saved as a Task, with reminders "
        "3 days before, 1 day before, and on the day\n"
        "  \u2022 anything without a date \u2192 I'll ask Task / Note / Habit / Event\n\n"
        "/addschedule \u2014 add a recurring weekly item (class, gym, etc.)\n"
        "/myschedule \u2014 see your recurring items\n"
        "/today \u2014 what's due or scheduled today\n"
        "/tasks or \"tasks\" \u2014 pending tasks\n"
        "/donetasks or \"done tasks\" \u2014 completed tasks\n"
        "/list \u2014 everything pending\n"
        "/done <id> \u2014 mark done\n"
        "/cancel \u2014 cancel a pending question"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")


async def addschedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["awaiting_schedule_text"] = True
    await update.message.reply_text("What's the recurring item? (e.g. \"Gym\", \"DBMS class\")")


async def myschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data["db"]
    entries = await get_entries(db, entry_type="Schedule", done=False)
    if not entries:
        await update.message.reply_text("No recurring schedule items yet. Try /addschedule.")
        return
    lines = []
    for e in sorted(entries, key=lambda x: x["id"]):
        days = ", ".join(DAY_NAMES[d] for d in sorted(e.get("weekdays", [])))
        lines.append(f"#{e['id']} {e['text']} \u2014 {days} @ {e['daily_time']}")
    await update.message.reply_text("\U0001F5D3\uFE0F Weekly schedule:\n" + "\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data["db"]
    text = update.message.text.strip()

    if context.user_data.get("awaiting_schedule_text"):
        context.user_data["pending_text"] = text
        context.user_data["awaiting_schedule_text"] = False
        context.user_data["schedule_days"] = set()
        await update.message.reply_text("Which days?", reply_markup=build_day_keyboard(set()))
        return

    if context.user_data.get("awaiting_schedule_time"):
        parsed = parse_hhmm(text)
        if not parsed:
            await update.message.reply_text("Use HH:MM (24h), e.g. 18:00.")
            return
        h, mnt = parsed
        weekdays = sorted(context.user_data.get("schedule_days", set()))
        entry = await add_entry(
            db, "Schedule", context.user_data.get("pending_text", ""), update.effective_chat.id,
            daily_time=f"{h:02d}:{mnt:02d}", weekdays=weekdays,
        )
        schedule_entry(context.application, entry)
        days_str = ", ".join(DAY_NAMES[d] for d in weekdays)
        context.user_data.clear()
        await update.message.reply_text(f"\U0001F5D3\uFE0F Saved (#{entry['id']}) \u2014 {days_str} @ {h:02d}:{mnt:02d}.")
        return

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
        due_dt, title, has_time = parsed
        entry = await add_entry(db, "Task", title, update.effective_chat.id, due=due_dt.isoformat())
        schedule_entry(context.application, entry)
        due_str = due_dt.strftime("%d %b, %I:%M %p") if has_time else due_dt.strftime("%d %b")
        await update.message.reply_text(
            f"\U0001F4CC Saved Task #{entry['id']}: {title}\nDue {due_str} \u2014 "
            "I'll remind you 3 days before, 1 day before, and on the day."
        )
        return

    context.user_data["pending_text"] = text
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("\U0001F4CC Task", callback_data="type:Task"),
                InlineKeyboardButton("\U0001F4DD Note", callback_data="type:Note"),
            ],
            [
                InlineKeyboardButton("\U0001F501 Habit", callback_data="type:Habit"),
                InlineKeyboardButton("\U0001F4C5 Event", callback_data="type:Event"),
            ],
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
    prompt = "What time should I remind you?" if entry_type == "Habit" else "When's it due/happening? Give a time (24h, e.g. 18:30)."
    await query.edit_message_text(f"Filed as {entry_type}. {prompt}\nSend 'skip' for no reminder.")


async def day_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data.split(":")[1]

    if data == "done":
        if not context.user_data.get("schedule_days"):
            await query.answer("Pick at least one day first!", show_alert=True)
            return
        await query.answer()
        context.user_data["awaiting_schedule_time"] = True
        await query.edit_message_text("Got it. What time? (24h, e.g. 18:00)")
        return

    await query.answer()
    wd = int(data)
    days = context.user_data.setdefault("schedule_days", set())
    if wd in days:
        days.remove(wd)
    else:
        days.add(wd)
    await query.edit_message_reply_markup(reply_markup=build_day_keyboard(days))


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
        suffix = "daily"
    else:  # Task or Event with an explicit time, no date given -> next occurrence of that time
        now = datetime.now()
        due = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        entry = await add_entry(db, entry_type, pending_text, update.effective_chat.id, due=due.isoformat())
        suffix = "staged reminders leading up to it"

    schedule_entry(context.application, entry)
    context.user_data.clear()
    await update.message.reply_text(f"{TYPE_EMOJI[entry_type]} Saved (#{entry['id']}) \u2014 {hhmm}, {suffix}.")


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
        elif e.get("weekdays"):
            days = ", ".join(DAY_NAMES[d] for d in sorted(e["weekdays"]))
            info = f" ({days} @ {e['daily_time']})"
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
        elif e.get("weekdays") and now.weekday() in e["weekdays"]:
            due_today.append((e["daily_time"], e))
        elif e.get("daily_time") and e["type"] == "Habit":
            due_today.append((e["daily_time"], e))
    if not due_today:
        await update.message.reply_text("Nothing due or scheduled today.")
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
        clear_jobs(context.application, entry_id)
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
    app.add_handler(CommandHandler("addschedule", addschedule_start))
    app.add_handler(CommandHandler("myschedule", myschedule))
    app.add_handler(CallbackQueryHandler(type_chosen, pattern=r"^type:"))
    app.add_handler(CallbackQueryHandler(day_toggle, pattern=r"^day:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()