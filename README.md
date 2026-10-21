<p align="center">
  <img src="https://files.catbox.moe/kazvi3.jpg" width="100%" alt="PeterPostX">
</p>

# 🗓 Telegram Post Scheduler Bot

<p align="center">
  <img src="https://readme-typing-svg.herokuapp.com/?lines=Welcome+To+The+Post+Scheduler+Bot;Schedule+Posts+With+Timers;Repeat+%7C+Auto-Delete+%7C+Edit+%7C+Albums;Thank+You!&center=true&width=560" alt="Typing SVG">
</p>

---

## ✨ What This Bot Can Do

<details>
<summary><b>👉 Tap to open the feature list</b></summary>

|    | Feature                                                                                                               |
| --- | --------------------------------------------------------------------------------------------------------------------- |
| 📺  | Many channels: add one by making the bot an admin (detected automatically), or forward a message / send its ID        |
| 📝  | New Post: send as many messages as you want, tap **Done**. Each message becomes its own post, sent in order           |
| 🖼  | All message types: text, photo, video, document, GIF, audio, voice, sticker and albums                                |
| ⏰  | Schedule by time, exact date or delay (`18:30`, `25-12-2026 18:30`, `2h`), or **Send Now**                             |
| 🗑  | Auto-delete timer for every post (presets or a custom time)                                                           |
| 📣  | Post to many channels with one click: tick the channels (or Select All) and every message goes to all of them |
| 👀  | Preview all your messages before posting, with a 🗑 Remove button under each one                          |
| 🗑  | A separate auto-delete timer for every message, in New Post and in Repeat                                |
| 📌  | Pin any message when it is posted, and Pin / Unpin live posts later from the bot                         |
| 🔁  | Repeat posts: pick Mon to Sun (or Every day / Weekdays / Weekend), one or more times a day, stop never / after N / on a date |
| ⏸  | Pause, resume, skip next, edit messages, days and times of a repeat                                                   |
| ♻️ | Replace previous: the new post goes out, then the old one is deleted, so the channel always shows the latest          |
| 🔗  | Link preview ON / OFF, asked for every post that has a link                                                           |
| 📋  | Manage scheduled posts: edit, change time, change auto-delete, preview, send now or delete                            |
| ✏️ | Edit already published posts (text or caption) and change link preview from the bot                                   |
| 🔕  | Silent posts (no notification)                                                                                        |
| ⏳  | Live status: `Posting… Please wait`, then `Posted`. Failed posts can be retried with one tap                          |
| ⬅️ | Back button on every screen, and Cancel removes the messages of that flow                                                                                           |
| 🔒  | Private bot: only the owners can use it                                                                               |
| 🗄  | MongoDB storage: scheduled posts and repeats are safe across restarts and redeploys                                   |

</details>

## 🔧 Environment Variables

> ⚠️ `BOT_TOKEN` and `MONGODB_URI` have no default value. Set them in your host's variables and never commit them.

<details>
<summary><b>👉 Tap to open the variable list</b></summary>

| Variable       | Meaning                                                                                      |
| -------------- | -------------------------------------------------------------------------------------------- |
| `BOT_TOKEN`    | From [BotFather](https://t.me/BotFather)                                                     |
| `OWNER_IDS`    | Owner Telegram ids, separated by commas. Send `/id` to the bot to find yours                 |
| `MONGODB_URI`  | MongoDB connection URL (`MONGO_URL` also works)                                              |
| `MONGODB_DB`   | MongoDB database name (optional, default `peterpostx`)                                       |
| `TIMEZONE`     | Your time zone (optional, default `Asia/Kolkata`)                                            |
| `START_IMAGE`  | Picture shown with the start message (optional)                                              |
| `DELETE_IMAGE` | Picture shown on the auto-delete question after **Send Now** (optional)                      |

</details>

## 🚀 Deploy On Railway

1. Push this repo to GitHub, then on Railway choose **New Project → Deploy from GitHub repo**.
2. Open **Variables** and add `BOT_TOKEN`, `MONGODB_URI` and `OWNER_IDS`.
3. Using MongoDB Atlas? In **Network Access** allow access from anywhere (`0.0.0.0/0`), or Railway cannot connect.
4. Deploy. When the logs say `Bot started`, send `/start` to your bot.
5. Make the bot an **admin** of your channel with permission to **Post, Edit and Delete** messages.

The start command is already set in `Procfile` (`python bot.py`).

## 🧭 How To Use

```text
➕ New Post   → pick channel → send messages → Done → time → auto-delete → confirm
🔁 Repeat     → pick channel → send messages → Done → days → times → end → auto-delete → confirm
🗓 Scheduled  → pick channel → open a post or a repeat → edit, pause, resume or delete
📢 My Channels → open a channel → New Post / Scheduled / Remove
```

## 🤖 Commands

<details>
<summary><b>👉 Tap to open the command list</b></summary>

| Command      | What it does                                         |
| ------------ | ---------------------------------------------------- |
| `/start`     | Open the main menu                                   |
| `/new`       | Create a new post                                    |
| `/repeat`    | Create a daily or weekly repeating post              |
| `/scheduled` | Manage scheduled and published posts                 |
| `/channels`  | Manage your channels                                 |
| `/cancel`    | Cancel the current action                            |
| `/id`        | Show your Telegram ID (works for everyone)           |

</details>

## 📝 Good To Know

<details>
<summary><b>👉 Tap to open the notes</b></summary>

- If the bot was offline at a scheduled time, the post goes out as soon as it is back online.
- A repeat run that is more than 1 hour late is skipped, so old posts are never sent in a burst.
- Albums can have 2 to 10 photos or videos, or only documents, or only audio files (Telegram rule). Its caption is used as the post caption.
- Telegram may refuse to delete very old messages (around 48 hours). The bot tells you if that happens.
- Pinning needs the **Edit messages** admin right in the channel.
- Published posts can only have their text or caption edited. The media itself cannot be changed.

</details>

## 📁 Files Guide

```text
bot.py            all bot screens, the scheduler and repeat logic
db.py             MongoDB (channels, posts, repeats)
helpers.py        time, days and formatting helpers
requirements.txt  Python packages
Procfile          start command
```

## 💝 Credits

<details>
<summary><b>👉 Tap to see the credits</b></summary>

- 🗓 Built, customised and maintained for the **ChillFlizX** community
- 🧩 Made with [aiogram](https://github.com/aiogram/aiogram) and MongoDB
- ❤️ Thank you to everyone who tested and gave feedback

</details>

---

© ChillFlizX. All rights reserved.
