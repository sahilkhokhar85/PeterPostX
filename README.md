# Telegram Channel Post Scheduler Bot

Schedule posts to your Telegram channels, with auto-delete timers, link preview control,
and a full manager for scheduled posts.

## Features
- Pick the channel from buttons, then send any message (text, photo, video, document, GIF, audio, voice, sticker, album)
- Send as many messages as you want for one go, then tap Done (Undo Last removes the last one). Each message becomes its own post and they are published in the order you sent them
- Schedule with a time (`18:30`, `6:30pm`, `25-12-2026 18:30`) or a delay (`30m`, `2h`, `1d 6h`), or post now
- Auto-delete timer (presets or custom)
- Link preview ON/OFF, asked every time a post contains a link
- Scheduled Posts manager: edit content, change time, change auto-delete, toggle link preview / silent,
  preview, post now, delete. Live posts with a pending delete timer are listed too.
- Silent (no notification) option
- Failed posts are kept so you can retry or edit them
- Edit already-published posts (text/caption) from Scheduled > channel > Published Posts
- **Repeat posts**: pick weekdays (Mon-Sun, or Every day / Weekdays / Weekend), one or more times per day,
  an end rule (never / after N posts / on a date picked from a calendar), auto-delete, and "Replace previous"
  (the old post is deleted when the new one goes out). Pause, resume, skip next, edit message/days/times, send now.
- Status message when a post goes out: "Posting... Please wait", then "Posted to <channel>"
- All bot messages are bold; monospace parts stay monospace
- Commands: /start /new /repeat /scheduled /channels /cancel /id
- Owner-only access

## Setup
1. Create a bot with @BotFather and copy the token.
2. Push this folder to a GitHub repo (bot.py, db.py, helpers.py, requirements.txt, Procfile).
3. On Railway: New Project > Deploy from GitHub repo.
4. Create a MongoDB database (MongoDB Atlas free tier, or the Railway MongoDB plugin) and copy the connection string.
5. Set variables:
   - `BOT_TOKEN`
   - `MONGODB_URI` (connection string)
   - `OWNER_IDS` (your Telegram ID; leave empty on first run, send `/id` to the bot, then set it and redeploy)
   - `TIMEZONE` (default `Asia/Kolkata`)
   - `MONGODB_DB` (optional, default `peterpostx`)
6. Start command: `python bot.py` (the Procfile already has it). Deploy.
7. Add the bot as **admin** of your channel with "Post messages" and "Delete messages" permissions.
   The channel is detected automatically. Or use My Channels > Add Channel.

## Notes
- All data is stored in MongoDB, so redeploys do not lose scheduled posts.
- If the bot was offline at a scheduled time, the post is published as soon as it is back online.
- A repeat run that is more than 1 hour late (bot was offline) is skipped, so old posts are not sent in a burst.
- Telegram may refuse to delete very old messages (around 48 hours); the bot will notify you if that happens.
- Albums (2-10 photos/videos, or documents, or audio files) are supported. Send the whole album at once; its caption is used as the post caption.

## Run locally
```
pip install -r requirements.txt
export BOT_TOKEN=... OWNER_IDS=... 
python bot.py
```
