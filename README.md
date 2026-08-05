# Productivity Bot

A Telegram bot that turns anything you send it into a structured entry
(Task / Note / Habit), stores it in MongoDB, and reminds you on your phone
at the right time.

## Setup

1. **Create the bot**: open Telegram, message `@BotFather`, send `/newbot`,
   follow the prompts. It gives you a token like `123456:ABC-DEF...`.
2. **Get a MongoDB database** — easiest is a free cluster on
   [MongoDB Atlas](https://www.mongodb.com/cloud/atlas): create a free (M0)
   cluster, add a database user, allow network access from your IP (or
   `0.0.0.0/0` for simplicity), and copy the connection string — it looks
   like `mongodb+srv://user:password@cluster.mongodb.net`.
   (Alternatively, run `mongod` locally and use `mongodb://localhost:27017`.)
3. **Install dependencies**:
   ```
   pip install -r requirements.txt
   ```
4. **Set your config** — env vars:
   ```
   export BOT_TOKEN="123456:ABC-DEF..."
   export MONGODB_URI="mongodb+srv://user:password@cluster.mongodb.net"
   ```
   (or paste them into the constants near the top of `bot.py`)
5. **Run it**:
   ```
   python bot.py
   ```
6. On Telegram, search for your bot's username and send `/start`.

## Using it

- **Text it naturally** — e.g. `assignment 10 aug deadline`, `submit report tomorrow 6pm`.
  If it finds a date, it auto-saves a **Task** with that deadline and sets a
  reminder (9:00 AM on the due date by default, or the exact time if you gave one).
- If no date is found, it asks whether to file the message as a **Task**, **Note**,
  or **Habit**, then asks for a reminder time (`HH:MM`, 24h) or `skip`.
  - Task reminders fire once. Habit reminders repeat every day at that time.
- `/today` — everything with a reminder due today, in order.
- `/list` — all pending entries (tasks, notes, habits together).
- `/tasks` or just texting **"tasks"** — pending tasks only, with added date and deadline.
- `/donetasks` or texting **"done tasks"** — completed tasks, same format.
- `/done <id>` — mark a task finished and cancel its reminder.

## Keeping it running

The bot only sends notifications while `bot.py` is running. For it to remind
you reliably, run it somewhere that stays on:
- A small always-on machine (Raspberry Pi, old laptop), or
- A free-tier host like Railway or Render (run `python bot.py` as a worker
  process — no web server needed; MongoDB Atlas is remote, so this works
  fine even though the host's filesystem isn't persistent), or
- Just leave it running on your PC while you're using it.

## Data

Two MongoDB collections are used, created automatically on first run:
- `entries` — every Task/Note/Habit you've saved.
- `counters` — a single document that hands out sequential `#id` numbers.