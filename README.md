# Productivity Bot

A Telegram bot that turns anything you send it into a structured entry —
a Task, a Note, a Habit, or an Event — stores it in a database, and
reminds you on your phone at the right time. It also supports recurring
weekly items like classes or gym sessions.

## What it does

Text it naturally, and if it can find a date or deadline in your message,
it saves that as a Task automatically and reminds you as the deadline
approaches — a few days out, the day before, and on the day itself, each
time with a slightly different message so it doesn't feel repetitive.

If there's no date in what you sent, it asks whether to file it as a
Task, a Note, a Habit, or an Event, and then asks what time to remind you.

For things that repeat every week — a class, a gym session, a standing
commitment — there's a separate flow where you name the item, pick which
days of the week it happens on, and give it a time. It will then remind
you on each of those days going forward.

You can also ask it what's pending, what's already done, and what's
happening today, at any time.

## What you need to set it up

- A Telegram bot token, obtained by messaging Telegram's official BotFather
  account and following its prompts to create a new bot.
- A database to store entries in — a small always-free cloud database
  works well, or a locally running one if you prefer.
- Python installed, along with the handful of libraries the project
  depends on (listed in the project's requirements file).
- A place to actually run it continuously, since it can only send
  reminders while it's running. This can be your own computer while
  you're using it, or a small always-on machine or free hosting service
  once you want it running full-time.

## Keeping it running

Reminders are only sent while the bot process is active. For casual use,
running it on your computer while you're working is enough. For it to
reliably remind you day to day, it needs to run somewhere that stays on,
such as a low-power home device or a free tier of a hosting service.

## Data stored

The bot keeps two collections of records: one holding every item you've
saved (tasks, notes, habits, events, and recurring schedule entries), and
a small internal one used only to hand out sequential ID numbers so you
can reference and mark items done easily.