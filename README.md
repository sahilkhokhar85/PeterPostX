# Telegram Channel Post Scheduler Bot

Schedule posts to your Telegram channels, with auto-delete timers, link preview control,
and a full manager for scheduled posts.

## Features
- Pick the channel from buttons, then send any message (text, photo, video, document, GIF, audio, voice, sticker)
- Schedule with a time (`18:30`, `6:30pm`, `25-12-2026 18:30`) or a delay (`30m`, `2h`, `1d 6h`), or post now
- Auto-delete timer (presets or custom)
- Link preview ON/OFF, asked every time a post contains a link
- Scheduled Posts manager: edit content, change time, change auto-delete, toggle link preview / silent,
  preview, post now, delete. Live posts with a pending delete timer are listed too.
- Silent (no notification) option
- Failed posts are kept so you can retry or edit them
- Owner-only access

## Setup
1. Create a bot with @BotFather and copy the token.
2. Push this folder to a GitHub repo.
3. On Railway: New Project > Deploy from GitHub repo.
4. Add a **Volume** to the service, mount path `/data` (keeps your database across deploys).
5. Set variables:
   - `BOT_TOKEN`
   - `OWNER_IDS` (your Telegram ID; run the bot once and send `/id` to it if you don't know it)
   - `TIMEZONE` (default `Asia/Kolkata`)
   - `DB_PATH=/data/bot.db`
6. Start command: `python bot.py` (the Procfile already has it). Deploy.
7. Add the bot as **admin** of your channel with "Post messages" and "Delete messages" permissions.
   The channel is detected automatically. Or use My Channels > Add Channel.

## Notes
- Without a Volume the database is lost on every redeploy.
- If the bot was offline at a scheduled time, the post is published as soon as it is back online.
- Telegram may refuse to delete very old messages (around 48 hours); the bot will notify you if that happens.
- Albums (multiple photos in one message) are not supported.

## Run locally
```
pip install -r requirements.txt
export BOT_TOKEN=... OWNER_IDS=... 
python bot.py
```
