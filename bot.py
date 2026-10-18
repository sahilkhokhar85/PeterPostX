import asyncio
import html
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from db import db
from helpers import (
    DAY_NAMES,
    apply_day_action,
    bold,
    days_label,
    days_short,
    month_matrix,
    occurrences,
    parse_times,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
OWNERS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
START_IMAGE = os.getenv("START_IMAGE", "https://files.catbox.moe/7121om.jpg")
PAGE = 8
EDITABLE = ("scheduled", "failed")
NO_TEXT_TYPES = ("sticker", "video_note")
NOPREV = LinkPreviewOptions(is_disabled=True)

public = Router()
router = Router()
router.message.filter(F.chat.type == "private", F.from_user.id.in_(OWNERS))
router.callback_query.filter(F.from_user.id.in_(OWNERS))


# ----------------------------------------------------------------- helpers
def esc(s):
    return html.escape(str(s))


def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


async def safe_answer(cb, text=None, alert=False):
    try:
        await cb.answer(text, show_alert=alert)
    except Exception:
        pass


async def show(ev, text, markup=None):
    """Edit the current bot message (callback) or send a new one (message)."""
    text = bold(text)
    if isinstance(ev, CallbackQuery):
        await safe_answer(ev)
        msg = ev.message
        if getattr(msg, "photo", None):
            try:
                await msg.delete()
            except Exception:
                pass
            await msg.answer(text, reply_markup=markup, link_preview_options=NOPREV)
            return
        try:
            await msg.edit_text(text, reply_markup=markup, link_preview_options=NOPREV)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        await msg.answer(text, reply_markup=markup, link_preview_options=NOPREV)
    else:
        await ev.answer(text, reply_markup=markup, link_preview_options=NOPREV)


async def notify(bot, uid, text, markup=None):
    try:
        await bot.send_message(uid, bold(text), reply_markup=markup, link_preview_options=NOPREV)
    except Exception:
        log.warning("Could not notify %s", uid)


async def reply(target, text, reply_markup=None):
    """Send a new message (bold, monospace kept)."""
    return await target.answer(bold(text), reply_markup=reply_markup, link_preview_options=NOPREV)


async def safe_send(bot, uid, text, markup=None):
    try:
        return await bot.send_message(uid, bold(text), reply_markup=markup, link_preview_options=NOPREV)
    except Exception:
        log.warning("Could not send to %s", uid)
        return None


async def edit_status(bot, st, uid, text, markup=None):
    """Edit the 'Posting… Please wait' message; send a new one if it is missing."""
    if st:
        try:
            await st.edit_text(bold(text), reply_markup=markup, link_preview_options=NOPREV)
            return
        except Exception:
            pass
    await safe_send(bot, uid, text, markup)


def fmt_dt(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d %b %Y, %I:%M %p")


def fmt_dur(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else ""]
    return " ".join(p for p in parts if p) or "under 1m"


def parse_duration(s):
    s = s.strip().lower()
    if s.startswith("in "):
        s = s[3:].strip()
    if not re.fullmatch(r"(\d+\s*[mhd]\s*)+", s):
        return None
    mult = {"m": 60, "h": 3600, "d": 86400}
    total = sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)\s*([mhd])", s))
    return total or None


def parse_when(s):
    s = s.strip().lower()
    now = datetime.now(TZ)
    if s == "now":
        return now
    d = parse_duration(s)
    if d:
        return now + timedelta(seconds=d)
    m = re.fullmatch(
        r"(?:(\d{1,2})[-/](\d{1,2})(?:[-/](\d{4}))?\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?", s
    )
    if not m:
        return None
    day, mon, year, hh, mm, ap = m.groups()
    hh, mm = int(hh), int(mm)
    if ap:
        if not 1 <= hh <= 12:
            return None
        hh = hh % 12 + (12 if ap == "pm" else 0)
    if hh > 23 or mm > 59:
        return None
    try:
        if day:
            return datetime(int(year) if year else now.year, int(mon), int(day), hh, mm, tzinfo=TZ)
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    except ValueError:
        return None


LINK_RE = re.compile(r"https?://|www\.|t\.me/|<a\s+href=", re.I)


def has_link(c):
    return c["type"] == "text" and bool(LINK_RE.search(c.get("text") or ""))


def plain(c, n=80):
    t = re.sub(r"<[^>]+>", "", c.get("text") or "")
    t = html.unescape(t).replace("\n", " ").strip()
    if len(t) > n:
        t = t[:n] + "…"
    if c["type"] == "text":
        return t or "(empty)"
    return f"[{c['type']}] {t}".strip()


def extract(m: Message):
    if m.text:
        return {"type": "text", "text": m.html_text}
    cap = m.html_text if m.caption else None
    for t in ("photo", "video", "animation", "document", "audio", "voice", "video_note", "sticker"):
        obj = getattr(m, t, None)
        if obj:
            fid = obj[-1].file_id if t == "photo" else obj.file_id
            return {"type": t, "file_id": fid, "text": cap}
    return None


async def send_content(bot: Bot, chat_id, c, preview, silent):
    kw = {"chat_id": chat_id, "disable_notification": bool(silent)}
    t = c["type"]
    if t == "text":
        return await bot.send_message(
            text=c["text"], link_preview_options=LinkPreviewOptions(is_disabled=not preview), **kw
        )
    if t in NO_TEXT_TYPES:
        return await getattr(bot, f"send_{t}")(**{t: c["file_id"]}, **kw)
    return await getattr(bot, f"send_{t}")(**{t: c["file_id"]}, caption=c.get("text"), **kw)


async def live_edit(bot: Bot, p):
    """Edit a message that is already live in the channel. Returns an error string or None."""
    c = p["content"]
    try:
        if c["type"] == "text":
            await bot.edit_message_text(
                text=c["text"],
                chat_id=p["chat_id"],
                message_id=p["message_id"],
                link_preview_options=LinkPreviewOptions(is_disabled=not p["link_preview"]),
            )
        elif c["type"] in NO_TEXT_TYPES:
            return "This post type has no text to edit."
        else:
            await bot.edit_message_caption(
                chat_id=p["chat_id"], message_id=p["message_id"], caption=c.get("text")
            )
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return None
        return str(e)
    except Exception as e:
        return str(e)
    return None


# ----------------------------------------------------------------- states
class NewPost(StatesGroup):
    content = State()
    preview = State()
    when = State()
    delete = State()
    confirm = State()


class EditPost(StatesGroup):
    content = State()
    live = State()
    when = State()
    delete = State()


class AddChannel(StatesGroup):
    waiting = State()


class RepeatNew(StatesGroup):
    content = State()
    preview = State()
    days = State()
    times = State()
    end = State()
    end_count = State()
    end_date = State()
    delete = State()
    confirm = State()


class RepeatEdit(StatesGroup):
    content = State()
    days = State()
    times = State()
    delete = State()


# ----------------------------------------------------------------- texts / keyboards
MENU = kb(
    [
        [("➕ New Post", "new"), ("🔁 Repeat", "rep")],
        [("📋 Scheduled", "sch"), ("📺 My Channels", "chs")],
    ]
)


MENU_TEXT = (
    "👋 <b>Welcome!</b>\n\n"
    "Schedule posts to your channels, set auto-delete timers and manage everything from one place.\n\n"
    "⏰ Timed · 🗑 Auto-delete · ✏️ Editable\n\n"
    "Choose an option below 👇"
)

CONTENT_PROMPT = (
    "Send the message you want to post.\n\n"
    "Supported: text, photo, video, document, GIF, audio, voice, sticker. "
    "Formatting and captions are preserved.\n"
    "Send one message at a time (albums are not supported)."
)
NO_CHANNELS = "You have no channels yet. Add one first."


def when_text():
    now = datetime.now(TZ).strftime("%d %b %Y, %I:%M %p")
    return (
        "🕒 <b>When should this be posted?</b>\n\n"
        f"Current time: <b>{now}</b> ({TZ.key})\n\n"
        "Send one of these:\n"
        "• <code>18:30</code> or <code>6:30pm</code> – today (tomorrow if already passed)\n"
        "• <code>25-12-2026 18:30</code> – exact date and time\n"
        "• <code>30m</code>, <code>2h</code>, <code>1d 6h</code> – after a delay"
    )


def del_text(live=False):
    if live:
        return (
            "🗑 <b>Delete this post from the channel after how long?</b>\n"
            "(counted from now)\n\n"
            "Choose a duration or send a custom one like <code>45m</code>, <code>3h</code>, "
            "<code>2d 12h</code>."
        )
    return (
        "🗑 <b>Auto-delete after posting?</b>\n\n"
        "Choose a duration or send a custom one like <code>45m</code>, <code>3h</code>, "
        "<code>2d 12h</code>."
    )


def del_rows(prefix, extra=None):
    row2 = [("📅 24h", f"{prefix}86400"), ("📅 48h", f"{prefix}172800"), ("📅 7d", f"{prefix}604800")]
    if extra:
        row2.append(extra)
    return [
        [("🚫 None", f"{prefix}0"), ("⏱ 1h", f"{prefix}3600"), ("⏱ 6h", f"{prefix}21600"), ("⏱ 12h", f"{prefix}43200")],
        row2,
    ]


def del_kb(prefix, cancel_text, cancel_cb):
    return kb(del_rows(prefix, (cancel_text, cancel_cb)))


# ----------------------------------------------------------------- public router
@public.message(Command("id"))
async def cmd_id(m: Message):
    await reply(m, f"Your Telegram ID: <code>{m.from_user.id}</code>")


@public.message(F.chat.type == "private", F.from_user.id.not_in(OWNERS))
async def not_owner(m: Message):
    await reply(m, "🔒 This is a private bot.")


# ----------------------------------------------------------------- menu & commands
async def show_menu(ev):
    text = bold(MENU_TEXT)
    if isinstance(ev, CallbackQuery):
        await safe_answer(ev)
        msg = ev.message
        if getattr(msg, "photo", None):
            try:
                await msg.edit_caption(caption=text, reply_markup=MENU)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
        try:
            await msg.delete()
        except Exception:
            pass
        target = msg
    else:
        target = ev
    try:
        await target.answer_photo(photo=START_IMAGE, caption=text, reply_markup=MENU)
    except Exception:
        log.warning("Could not send start image, falling back to text")
        await target.answer(text, reply_markup=MENU)


@router.message(CommandStart())
@router.message(Command("cancel"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_menu(cb)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.message(Command("new"))
async def cmd_new(m: Message, state: FSMContext):
    await new_chooser(m, state)


@router.message(Command("scheduled"))
async def cmd_scheduled(m: Message, state: FSMContext):
    await sch_chooser(m, state)


@router.message(Command("channels"))
async def cmd_channels(m: Message, state: FSMContext):
    await channels_screen(m, state)


@router.message(Command("repeat"))
async def cmd_repeat(m: Message, state: FSMContext):
    await rep_chooser(m, state)


# ----------------------------------------------------------------- channels
async def channels_screen(ev, state):
    await state.clear()
    chans = await db.channels()
    text = "📺 <b>My Channels</b>\n\n" + (
        "Select a channel to manage it." if chans else "No channels added yet."
    )
    rows = [[(f"📺 {c['title']}", f"ch:{c['chat_id']}")] for c in chans]
    rows.append([("➕ Add Channel", "addch")])
    rows.append([("⬅️ Back", "menu")])
    await show(ev, text, kb(rows))


@router.callback_query(F.data == "chs")
async def cb_channels(cb: CallbackQuery, state: FSMContext):
    await channels_screen(cb, state)


@router.callback_query(F.data.startswith("ch:"))
async def cb_channel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    cid = int(cb.data.split(":")[1])
    ch = await db.channel(cid)
    if not ch:
        return await channels_screen(cb, state)
    try:
        me = await bot.get_chat_member(cid, bot.id)
        if me.status == "creator":
            access = "✅ Post · ✅ Delete · ✅ Edit"
        elif me.status == "administrator":
            yes = lambda v: "✅" if v else "❌"
            access = (
                f"{yes(getattr(me, 'can_post_messages', None))} Post · "
                f"{yes(getattr(me, 'can_delete_messages', None))} Delete · "
                f"{yes(getattr(me, 'can_edit_messages', None))} Edit"
            )
        else:
            access = "⚠️ Bot is not an admin of this channel"
    except Exception:
        access = "⚠️ Could not check (is the bot still in the channel?)"
    pending = (await db.counts_by_channel()).get(cid, 0)
    L = [
        f"📺 <b>{esc(ch['title'])}</b>",
        f"Username: {'@' + esc(ch['username']) if ch['username'] else 'private channel'}",
        f"ID: <code>{cid}</code>",
        f"Bot access: {access}",
        f"Pending posts: {pending}",
    ]
    await show(
        cb,
        "\n".join(L),
        kb(
            [
                [("➕ New Post", f"np_ch:{cid}"), ("📋 Scheduled", f"list:0:{cid}")],
                [("🗑 Remove Channel", f"chr:{cid}")],
                [("⬅️ Back", "chs")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("chr:"))
async def cb_channel_remove_ask(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    ch = await db.channel(cid)
    if not ch:
        return await show(cb, "Channel not found.", kb([[("⬅️ Back", "chs")]]))
    pending = await db.db.posts.count_documents(
        {"chat_id": cid, "status": {"$in": list(EDITABLE)}}
    )
    await show(
        cb,
        f"Remove <b>{esc(ch['title'])}</b> from the bot?\n\n"
        f"{pending} scheduled/failed post(s) for this channel will be deleted. "
        "Posts already in the channel are not touched.",
        kb([[("✅ Yes, remove", f"chr_y:{cid}"), ("⬅️ No", f"ch:{cid}")]]),
    )


@router.callback_query(F.data.startswith("chr_y:"))
async def cb_channel_remove(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    await db.delete_pending_for_channel(cid)
    await db.remove_channel(cid)
    await channels_screen(cb, state)


@router.callback_query(F.data == "addch")
async def cb_add_channel(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.waiting)
    await show(
        cb,
        "➕ <b>Add a channel</b>\n\n"
        "1. Add this bot as an <b>admin</b> of your channel with permission to "
        "<b>Post</b>, <b>Edit</b> and <b>Delete</b> messages. It will be detected automatically.\n\n"
        "2. Or, to add it manually, forward any message from the channel here, or send the "
        "channel @username / numeric ID.",
        kb([[("⬅️ Back", "chs")]]),
    )


@router.message(AddChannel.waiting)
async def msg_add_channel(m: Message, state: FSMContext, bot: Bot):
    chat = None
    try:
        if m.forward_origin and m.forward_origin.type == "channel":
            chat = m.forward_origin.chat
        elif m.text:
            ref = m.text.strip()
            if re.fullmatch(r"-?\d+", ref):
                ref = int(ref)
            chat = await bot.get_chat(ref)
    except Exception:
        chat = None
    if not chat or chat.type != "channel":
        return await reply(m, 
            "Could not find that channel. Forward a message from it, or send its @username / ID."
        )
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except Exception:
        me = None
    if not me or me.status != "administrator":
        return await reply(m, "The bot is not an admin in that channel. Add it as admin and try again.")
    await db.add_channel(chat.id, chat.title or str(chat.id), chat.username)
    await state.clear()
    warn = ""
    if not getattr(me, "can_post_messages", True):
        warn += "\n⚠️ The bot does not have permission to post messages."
    if not getattr(me, "can_delete_messages", True):
        warn += "\n⚠️ The bot does not have permission to delete messages (auto-delete will fail)."
    await reply(m, 
        f"✅ Channel <b>{esc(chat.title)}</b> added.{warn}",
        reply_markup=kb([[("📺 My Channels", "chs"), ("⬅️ Menu", "menu")]]),
    )


@router.my_chat_member(F.chat.type == "channel")
async def on_channel_member(ev: ChatMemberUpdated, bot: Bot):
    new = ev.new_chat_member
    if new.status == "administrator" and ev.from_user.id in OWNERS:
        await db.add_channel(ev.chat.id, ev.chat.title or str(ev.chat.id), ev.chat.username)
        warn = ""
        if not getattr(new, "can_post_messages", True):
            warn += "\n⚠️ Missing permission: Post messages."
        if not getattr(new, "can_delete_messages", True):
            warn += "\n⚠️ Missing permission: Delete messages."
        await notify(bot, ev.from_user.id, f"✅ Channel <b>{esc(ev.chat.title)}</b> added.{warn}")
    elif new.status in ("left", "kicked"):
        await db.remove_channel(ev.chat.id)


# ----------------------------------------------------------------- new post flow
async def new_chooser(ev, state):
    await state.clear()
    chans = await db.channels()
    if not chans:
        return await show(
            ev, NO_CHANNELS, kb([[("➕ Add Channel", "addch")], [("⬅️ Back", "menu")]])
        )
    rows = [[(f"📺 {c['title']}", f"np_ch:{c['chat_id']}")] for c in chans]
    rows.append([("⬅️ Back", "menu")])
    await show(ev, "➕ <b>New Post</b>\n\nSelect the channel to post in:", kb(rows))


@router.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await new_chooser(cb, state)


@router.callback_query(F.data.startswith("np_ch:"))
async def np_channel(cb: CallbackQuery, state: FSMContext):
    await state.set_data({"chat_id": int(cb.data.split(":")[1]), "silent": 0, "preview": 0})
    await state.set_state(NewPost.content)
    await show(cb, CONTENT_PROMPT, kb([[("⬅️ Back", "new")]]))


@router.message(NewPost.content)
async def np_content(m: Message, state: FSMContext):
    if m.media_group_id:
        return await reply(m, "Albums are not supported. Please send a single message.")
    c = extract(m)
    if not c:
        return await reply(m, 
            "This message type is not supported. Send text, photo, video, document, GIF, "
            "audio, voice or sticker."
        )
    await state.update_data(content=c)
    if has_link(c):
        await state.set_state(NewPost.preview)
        return await reply(m, 
            "🔗 This post contains a link.\nDo you want to show the link preview?",
            reply_markup=kb(
                [[("✅ Preview ON", "np_pre:1"), ("🚫 Preview OFF", "np_pre:0")], [("❌ Cancel", "menu")]]
            ),
        )
    await ask_when(m, state)


@router.callback_query(NewPost.preview, F.data.startswith("np_pre:"))
async def np_preview(cb: CallbackQuery, state: FSMContext):
    await state.update_data(preview=int(cb.data.split(":")[1]))
    await ask_when(cb, state)


async def ask_when(ev, state):
    await state.set_state(NewPost.when)
    await show(
        ev,
        when_text(),
        kb([[("⚡ Send Now", "np_now"), ("🔁 Repeat", "np_rep")], [("❌ Cancel", "menu")]]),
    )


async def np_set_time(ev, state, dt):
    ts = int(dt.timestamp())
    if ts < time.time() - 60:
        return await show(ev, "That time is in the past. Send a future time.")
    await state.update_data(send_at=max(ts, int(time.time())))
    await state.set_state(NewPost.delete)
    await show(ev, del_text(), del_kb("np_del:", "❌ Cancel", "menu"))


@router.callback_query(NewPost.when, F.data == "np_now")
async def np_now(cb: CallbackQuery, state: FSMContext):
    await np_set_time(cb, state, datetime.now(TZ))


@router.message(NewPost.when)
async def np_when(m: Message, state: FSMContext):
    dt = parse_when(m.text or "")
    if not dt:
        return await reply(m, "Invalid time. Examples: <code>18:30</code>, <code>25-12-2026 18:30</code>, <code>2h</code>")
    await np_set_time(m, state, dt)


@router.callback_query(NewPost.delete, F.data.startswith("np_del:"))
async def np_del_cb(cb: CallbackQuery, state: FSMContext):
    sec = int(cb.data.split(":")[1])
    await state.update_data(delete_after=sec or None)
    await ask_confirm(cb, state)


@router.message(NewPost.delete)
async def np_del_msg(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if not sec:
        return await reply(m, "Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    await state.update_data(delete_after=sec)
    await ask_confirm(m, state)


async def ask_confirm(ev, state):
    d = await state.get_data()
    ch = await db.channel(d["chat_id"])
    if not ch:
        await state.clear()
        return await show(ev, "That channel no longer exists.", kb([[("⬅️ Menu", "menu")]]))
    await state.set_state(NewPost.confirm)
    c = d["content"]
    L = ["<b>Review your post</b>", "", f"📺 Channel: {esc(ch['title'])}", f"📝 Content: {esc(plain(c, 120))}"]
    if has_link(c):
        L.append("🔗 Link preview: " + ("ON" if d["preview"] else "OFF"))
    L.append("🕒 Posting: " + ("Immediately" if d["send_at"] <= time.time() + 5 else fmt_dt(d["send_at"])))
    da = d.get("delete_after")
    L.append("🗑 Auto-delete: " + (f"{fmt_dur(da)} after posting" if da else "Off"))
    L.append("🔔 Notification: " + ("Silent" if d["silent"] else "Normal"))
    sil = "🔕 Silent: ON" if d["silent"] else "🔔 Silent: OFF"
    await show(
        ev,
        "\n".join(L),
        kb([[(sil, "np_sil")], [("✅ Schedule", "np_ok"), ("❌ Cancel", "menu")]]),
    )


@router.callback_query(NewPost.confirm, F.data == "np_sil")
async def np_silent(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(silent=0 if d["silent"] else 1)
    await ask_confirm(cb, state)


@router.callback_query(NewPost.confirm, F.data == "np_ok")
async def np_ok(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pid = await db.create_post(
        owner_id=cb.from_user.id,
        chat_id=d["chat_id"],
        content=d["content"],
        link_preview=d["preview"],
        silent=d["silent"],
        send_at=d["send_at"],
        delete_after=d.get("delete_after"),
    )
    await state.clear()
    when = "right now" if d["send_at"] <= time.time() + 5 else f"at {fmt_dt(d['send_at'])}"
    await show(
        cb,
        f"✅ Post <b>#{pid}</b> scheduled. It will be published {when}.",
        kb([[("📋 Scheduled", "sch"), ("➕ New Post", "new")], [("⬅️ Menu", "menu")]]),
    )


# ----------------------------------------------------------------- scheduled: channel chooser
async def sch_chooser(ev, state):
    await state.clear()
    chans = await db.channels()
    if not chans:
        return await show(
            ev, NO_CHANNELS, kb([[("➕ Add Channel", "addch")], [("⬅️ Back", "menu")]])
        )
    counts = await db.counts_by_channel()
    rows = [[(f"📋 All Channels ({sum(counts.values())})", "list:0:0")]]
    rows += [[(f"📺 {c['title']} ({counts.get(c['chat_id'], 0)})", f"list:0:{c['chat_id']}")] for c in chans]
    rows.append([("⬅️ Back", "menu")])
    await show(
        ev,
        "📋 <b>Scheduled Posts</b>\n\nSelect a channel (number = pending posts):",
        kb(rows),
    )


@router.callback_query(F.data == "sch")
async def cb_sch(cb: CallbackQuery, state: FSMContext):
    await sch_chooser(cb, state)


def parse_list(data):
    parts = data.split(":")
    return int(parts[1]), (int(parts[2]) if len(parts) > 2 else 0)


def nav_row(prefix, page, pages, cid):
    nav = []
    if page > 0:
        nav.append(("⬅️", f"{prefix}:{page - 1}:{cid}"))
    nav.append((f"📄 {page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("➡️", f"{prefix}:{page + 1}:{cid}"))
    return nav


@router.callback_query(F.data.startswith("list:"))
async def cb_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    page, cid = parse_list(cb.data)
    total = await db.count_active(cid)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    ch = await db.channel(cid) if cid else None
    rows = []
    for p in await db.active(PAGE, page * PAGE, cid):
        if p["status"] == "sent":
            icon, ts = "🗑", p["delete_at"]
        elif p["status"] == "failed":
            icon, ts = "⚠️", p["send_at"]
        else:
            icon, ts = "⏳", p["send_at"]
        when = datetime.fromtimestamp(ts, TZ).strftime("%d %b %H:%M")
        label = f"{icon} #{p['id']} · {when} · "
        if not cid:
            label += f"{(p['ch_title'] or '?')[:12]} · "
        label += plain(p["content"], 20)
        rows.append([(label, f"p:{p['id']}")])
    if pages > 1:
        rows.append(nav_row("list", page, pages, cid))
    nrep = await db.count_repeats(cid)
    extra = [(f"🔁 Repeating ({nrep})", f"rlist:0:{cid}")]
    if cid:
        extra.append(("✅ Published", f"pub:0:{cid}"))
    rows.append(extra)
    rows.append([("⬅️ Back", "sch")])
    head = f"📋 <b>{esc(ch['title']) if ch else 'All channels'}</b> ({total})\n"
    if total:
        text = (
            head
            + "⏳ waiting to post · 🗑 live, will auto-delete · ⚠️ failed\n"
            + f"Times are in {esc(TZ.key)}.\n\nTap a post to manage it."
        )
    else:
        text = head + "No scheduled or auto-deleting posts here."
        if cid:
            text += "\nUse Published Posts to edit or delete posts already in the channel."
    await show(cb, text, kb(rows))


@router.callback_query(F.data.startswith("pub:"))
async def cb_published(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    page, cid = parse_list(cb.data)
    ch = await db.channel(cid)
    total = await db.count_published(cid)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for p in await db.published(PAGE, page * PAGE, cid):
        when = datetime.fromtimestamp(p["sent_at"], TZ).strftime("%d %b %H:%M")
        mark = " 🗑" if p["delete_at"] else ""
        rows.append([(f"✅ #{p['id']} · {when} · {plain(p['content'], 22)}{mark}", f"p:{p['id']}")])
    if pages > 1:
        rows.append(nav_row("pub", page, pages, cid))
    rows.append([("⬅️ Back", f"list:0:{cid}")])
    text = f"✅ <b>Published in {esc(ch['title']) if ch else cid}</b> ({total})\n"
    text += "Newest first. Tap a post to edit its text/caption or delete it." if total else "No published posts yet."
    await show(cb, text, kb(rows))


# ----------------------------------------------------------------- post detail
def back_cb(p):
    if p["status"] in EDITABLE or (p["status"] == "sent" and p["delete_at"]):
        return f"list:0:{p['chat_id']}"
    return f"pub:0:{p['chat_id']}"


async def show_post(ev, pid):
    p = await db.post(pid)
    if not p:
        return await show(ev, "Post not found.", kb([[("⬅️ Back", "sch")]]))
    ch = await db.channel(p["chat_id"])
    c, st = p["content"], p["status"]
    L = [
        f"<b>Post #{pid}</b>",
        f"📺 Channel: {esc(ch['title']) if ch else p['chat_id']}",
        f"📝 Content: {esc(plain(c, 120))}",
    ]
    if st in EDITABLE:
        if st == "failed":
            L.append(f"⚠️ Status: Failed\n<code>{esc(p['error'] or '')}</code>")
        else:
            L.append("⏳ Status: Scheduled")
        L.append(f"🕒 Posts at: {fmt_dt(p['send_at'])}")
        da = p["delete_after"]
        L.append("🗑 Auto-delete: " + (f"{fmt_dur(da)} after posting" if da else "Off"))
    elif st == "sent":
        L.append("🟢 Status: Live in channel")
        L.append(f"📤 Posted at: {fmt_dt(p['sent_at'])}")
        L.append(f"🗑 Deletes at: {fmt_dt(p['delete_at'])}" if p["delete_at"] else "🗑 Auto-delete: Off")
    else:
        L.append(f"Status: {st}")
    if has_link(c):
        L.append("🔗 Link preview: " + ("ON" if p["link_preview"] else "OFF"))
    L.append("🔔 Notification: " + ("Silent" if p["silent"] else "Normal"))

    back = ("⬅️ Back", back_cb(p))
    if st in EDITABLE:
        tog = []
        if has_link(c):
            tog.append((f"🔗 Preview: {'ON' if p['link_preview'] else 'OFF'}", f"pt_pre:{pid}"))
        tog.append(("🔕 Silent: ON" if p["silent"] else "🔔 Silent: OFF", f"pt_sil:{pid}"))
        rows = [
            [("✏️ Edit", f"pe_c:{pid}"), ("🕒 Time", f"pe_t:{pid}")],
            [("🗑 Auto-delete", f"pe_d:{pid}"), ("👁 View", f"pv:{pid}")],
            tog,
            [("🔁 Retry" if st == "failed" else "⚡ Send Now", f"pn:{pid}"), ("❌ Delete", f"pdel:{pid}")],
            [back],
        ]
    elif st == "sent":
        first = []
        if c["type"] not in NO_TEXT_TYPES:
            first.append(("✏️ Edit Post", f"pe_c:{pid}"))
        if has_link(c):
            first.append((f"🔗 Preview: {'ON' if p['link_preview'] else 'OFF'}", f"pt_pre:{pid}"))
        rows = []
        if first:
            rows.append(first)
        rows.append([("🗑 Auto-delete", f"pe_d:{pid}"), ("👁 View", f"pv:{pid}")])
        rows.append([("🗑 Delete Now", f"pdn:{pid}"), back])
    else:
        rows = [[back]]
    await show(ev, "\n".join(L), kb(rows))


async def editable(cb, pid):
    p = await db.post(pid)
    if not p or p["status"] not in EDITABLE:
        await safe_answer(cb, "This post is no longer scheduled.", True)
        await show(cb, "Post not found or already published.", kb([[("⬅️ Back", "sch")]]))
        return None
    return p


@router.callback_query(F.data.startswith("p:"))
async def cb_post(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_post(cb, int(cb.data.split(":")[1]))


@router.callback_query(F.data.startswith("pv:"))
async def cb_view(cb: CallbackQuery, bot: Bot):
    p = await db.post(int(cb.data.split(":")[1]))
    if not p:
        return await safe_answer(cb, "Post not found.", True)
    await safe_answer(cb, "Sending a copy…")
    try:
        await send_content(bot, cb.from_user.id, p["content"], p["link_preview"], 1)
    except Exception as e:
        await reply(cb.message, f"Could not send preview: <code>{esc(e)}</code>")


@router.callback_query(F.data.startswith("pt_pre:"))
async def cb_toggle_preview(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[1])
    p = await db.post(pid)
    if not p or p["status"] not in (*EDITABLE, "sent"):
        return await safe_answer(cb, "Post not found.", True)
    p["link_preview"] = 0 if p["link_preview"] else 1
    await db.set(pid, link_preview=p["link_preview"])
    if p["status"] == "sent":
        err = await live_edit(bot, p)
        if err:
            await safe_answer(cb, f"Could not update the channel post: {err[:150]}", True)
    await show_post(cb, pid)


@router.callback_query(F.data.startswith("pt_sil:"))
async def cb_toggle_silent(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = await editable(cb, pid)
    if p:
        await db.set(pid, silent=0 if p["silent"] else 1)
        await show_post(cb, pid)


@router.callback_query(F.data.startswith("pn:"))
async def cb_send_now(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    if await editable(cb, pid):
        await db.set(pid, status="scheduled", send_at=int(time.time()), error=None)
        await safe_answer(cb, "Sending…")
        await show_post(cb, pid)


@router.callback_query(F.data.startswith("pdel:"))
async def cb_delete_ask(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    await show(
        cb,
        f"Delete scheduled post <b>#{pid}</b>? It will not be published.",
        kb([[("✅ Yes, delete", f"pdel_y:{pid}"), ("⬅️ No", f"p:{pid}")]]),
    )


@router.callback_query(F.data.startswith("pdel_y:"))
async def cb_delete_yes(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = await editable(cb, pid)
    if p:
        await db.delete_post(pid)
        await show(
            cb,
            f"🗑 Post #{pid} deleted.",
            kb([[("📋 Scheduled", f"list:0:{p['chat_id']}"), ("⬅️ Menu", "menu")]]),
        )


@router.callback_query(F.data.startswith("pdn:"))
async def cb_delnow_ask(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    await show(
        cb,
        f"Delete post <b>#{pid}</b> from the channel now?",
        kb([[("✅ Yes, delete", f"pdn_y:{pid}"), ("⬅️ No", f"p:{pid}")]]),
    )


@router.callback_query(F.data.startswith("pdn_y:"))
async def cb_delnow_yes(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data.split(":")[1])
    p = await db.post(pid)
    if not p or p["status"] != "sent":
        return await show(cb, "Post not found or not live.", kb([[("⬅️ Back", "sch")]]))
    try:
        await bot.delete_message(p["chat_id"], p["message_id"])
    except Exception as e:
        if "not found" not in str(e).lower():
            return await show(cb, f"Could not delete: <code>{esc(e)}</code>", kb([[("⬅️ Back", f"p:{pid}")]]))
    await db.set(pid, status="deleted", delete_at=None)
    await show(
        cb,
        f"🗑 Post #{pid} deleted from the channel.",
        kb([[("📋 Scheduled", f"list:0:{p['chat_id']}"), ("⬅️ Menu", "menu")]]),
    )


# ---- edit content (scheduled posts: replace everything; live posts: text/caption only)
@router.callback_query(F.data.startswith("pe_c:"))
async def pe_content(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    p = await db.post(pid)
    if not p:
        return await show(cb, "Post not found.", kb([[("⬅️ Back", "sch")]]))
    if p["status"] in EDITABLE:
        await state.set_state(EditPost.content)
        await state.set_data({"post_id": pid})
        return await show(cb, f"✏️ <b>Edit post #{pid}</b>\n\n{CONTENT_PROMPT}", kb([[("⬅️ Back", f"p:{pid}")]]))
    if p["status"] == "sent":
        if p["content"]["type"] in NO_TEXT_TYPES:
            return await safe_answer(cb, "This post type has no text to edit.", True)
        kind = "text" if p["content"]["type"] == "text" else "caption"
        await state.set_state(EditPost.live)
        await state.set_data({"post_id": pid})
        return await show(
            cb,
            f"✏️ <b>Edit live post #{pid}</b>\n\n"
            f"Send the new {kind}. The post in the channel will be edited right away.\n"
            "The media itself cannot be changed after posting.",
            kb([[("⬅️ Back", f"p:{pid}")]]),
        )
    await safe_answer(cb, "This post can no longer be edited.", True)


@router.message(EditPost.content)
async def ep_content(m: Message, state: FSMContext):
    pid = (await state.get_data())["post_id"]
    if m.media_group_id:
        return await reply(m, "Albums are not supported. Please send a single message.")
    c = extract(m)
    if not c:
        return await reply(m, "This message type is not supported.")
    p = await db.post(pid)
    await state.clear()
    if not p or p["status"] not in EDITABLE:
        return await show(m, "This post is no longer editable.", kb([[("⬅️ Menu", "menu")]]))
    await db.set(pid, content=c, link_preview=0)
    if has_link(c):
        return await reply(m, 
            "✅ Content updated.\n🔗 This post contains a link. Show the link preview?",
            reply_markup=kb([[("✅ Preview ON", f"ep_pre:{pid}:1"), ("🚫 Preview OFF", f"ep_pre:{pid}:0")]]),
        )
    await show_post(m, pid)


@router.message(EditPost.live)
async def ep_live(m: Message, state: FSMContext, bot: Bot):
    pid = (await state.get_data())["post_id"]
    p = await db.post(pid)
    if not p or p["status"] != "sent":
        await state.clear()
        return await show(m, "This post is no longer live.", kb([[("⬅️ Menu", "menu")]]))
    if not m.text:
        return await reply(m, "Please send text only. (Media cannot be changed after posting.)")
    c = dict(p["content"])
    c["text"] = m.html_text
    p["content"] = c
    err = await live_edit(bot, p)
    if err:
        return await reply(m, f"Could not edit the post:\n<code>{esc(err)}</code>\nSend another text or press Back.")
    await db.set(pid, content=c)
    await state.clear()
    if has_link(c):
        return await reply(m, 
            "✅ Post edited.\n🔗 This post contains a link. Show the link preview?",
            reply_markup=kb([[("✅ Preview ON", f"ep_pre:{pid}:1"), ("🚫 Preview OFF", f"ep_pre:{pid}:0")]]),
        )
    await reply(m, "✅ Post edited in the channel.")
    await show_post(m, pid)


@router.callback_query(F.data.startswith("ep_pre:"))
async def ep_preview(cb: CallbackQuery, bot: Bot):
    _, pid, val = cb.data.split(":")
    pid = int(pid)
    await db.set(pid, link_preview=int(val))
    p = await db.post(pid)
    if p and p["status"] == "sent":
        err = await live_edit(bot, p)
        if err:
            await safe_answer(cb, f"Could not update the channel post: {err[:150]}", True)
    await show_post(cb, pid)


# ---- edit time
@router.callback_query(F.data.startswith("pe_t:"))
async def pe_time(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    if await editable(cb, pid):
        await state.set_state(EditPost.when)
        await state.set_data({"post_id": pid})
        await show(cb, when_text(), kb([[("⬅️ Back", f"p:{pid}")]]))


@router.message(EditPost.when)
async def ep_when(m: Message, state: FSMContext):
    pid = (await state.get_data())["post_id"]
    dt = parse_when(m.text or "")
    if not dt:
        return await reply(m, "Invalid time. Examples: <code>18:30</code>, <code>25-12-2026 18:30</code>, <code>2h</code>")
    ts = int(dt.timestamp())
    if ts < time.time() - 60:
        return await reply(m, "That time is in the past. Send a future time.")
    p = await db.post(pid)
    await state.clear()
    if not p or p["status"] not in EDITABLE:
        return await show(m, "This post is no longer editable.", kb([[("⬅️ Menu", "menu")]]))
    await db.set(pid, send_at=max(ts, int(time.time())), status="scheduled", error=None)
    await show_post(m, pid)


# ---- edit auto-delete (works for scheduled and live posts)
@router.callback_query(F.data.startswith("pe_d:"))
async def pe_delete(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    p = await db.post(pid)
    if not p or p["status"] not in (*EDITABLE, "sent"):
        return await show(cb, "Post not found.", kb([[("⬅️ Back", "sch")]]))
    await state.set_state(EditPost.delete)
    await state.set_data({"post_id": pid})
    await show(cb, del_text(live=p["status"] == "sent"), del_kb(f"ed:{pid}:", "⬅️ Back", f"p:{pid}"))


async def apply_delete(pid, sec):
    p = await db.post(pid)
    if not p:
        return False
    if p["status"] == "sent":
        await db.set(pid, delete_at=(int(time.time()) + sec) if sec else None)
    elif p["status"] in EDITABLE:
        await db.set(pid, delete_after=sec or None)
    else:
        return False
    return True


@router.callback_query(F.data.startswith("ed:"))
async def ed_cb(cb: CallbackQuery, state: FSMContext):
    _, pid, sec = cb.data.split(":")
    await state.clear()
    await apply_delete(int(pid), int(sec))
    await show_post(cb, int(pid))


@router.message(EditPost.delete)
async def ed_msg(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if not sec:
        return await reply(m, "Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    pid = (await state.get_data())["post_id"]
    await state.clear()
    await apply_delete(pid, sec)
    await show_post(m, pid)


# ================================================================= REPEAT POSTS
LATE_LIMIT = 3600  # a repeat run that is more than 1 hour late (bot was offline) is skipped


def t12(t):
    return datetime.strptime(t, "%H:%M").strftime("%I:%M %p")


def end_date_of(d):
    return date.fromisoformat(d["end_date"]) if d.get("end_date") else None


def rule_next(r, after_ts):
    res = occurrences(r["days"], r["times"], after_ts, TZ, end_date_of(r), 1)
    return res[0] if res else None


def end_label(d):
    if d["end_type"] == "count":
        return f"After {d['end_count']} posts"
    if d["end_type"] == "date":
        return "On " + end_date_of(d).strftime("%d %b %Y")
    return "Never"


def rule_lines(d, title):
    c = d["content"]
    da = d.get("delete_after")
    L = [
        f"📺 Channel: {esc(title)}",
        f"📝 Content: {esc(plain(c, 120))}",
        f"📅 Days: {days_label(d['days'])}",
        f"🕒 Times: {', '.join(t12(t) for t in d['times'])}",
        f"🏁 Ends: {end_label(d)}",
        "🗑 Auto-delete: " + (f"{fmt_dur(da)} after posting" if da else "Off"),
        "♻️ Replace previous: " + ("ON" if d.get("replace_prev") else "OFF"),
    ]
    if has_link(c):
        L.append("🔗 Link preview: " + ("ON" if d.get("link_preview", d.get("preview", 0)) else "OFF"))
    L.append("🔔 Notification: " + ("Silent" if d.get("silent") else "Normal"))
    return L


def day_kb(sel, P, ok, cancel):
    def lab(i):
        return ("✅ " if i in sel else "⬜ ") + DAY_NAMES[i]

    return kb(
        [
            [(lab(i), f"{P}:t:{i}") for i in range(0, 4)],
            [(lab(i), f"{P}:t:{i}") for i in range(4, 7)],
            [("📆 Every day", f"{P}:all"), ("💼 Weekdays", f"{P}:wk"), ("🏖 Weekend", f"{P}:we")],
            [ok, cancel],
        ]
    )


def times_text(current=None):
    now = datetime.now(TZ).strftime("%d %b %Y, %I:%M %p")
    s = (
        "🕒 <b>At what time?</b>\n\n"
        f"Current time: {now} ({TZ.key})\n\n"
        "Send one or more times, separated by commas.\n\n"
        "Examples:\n"
        "• <code>09:00</code>\n"
        "• <code>09:00, 18:30</code>\n"
        "• <code>6:30pm</code>"
    )
    if current:
        s += "\n\nCurrent: " + ", ".join(t12(t) for t in current)
    return s


def calendar_kb(year, month, back_cb):
    today = datetime.now(TZ).date()
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    can_prev = (year, month) > (today.year, today.month)
    rows = [
        [
            ("◀️", f"rpc:m:{py}:{pm}") if can_prev else ("·", "noop"),
            (datetime(year, month, 1).strftime("%B %Y"), "noop"),
            ("▶️", f"rpc:m:{ny}:{nm}"),
        ],
        [(n, "noop") for n in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")],
    ]
    for week in month_matrix(year, month):
        row = []
        for dd in week:
            if dd == 0 or date(year, month, dd) < today:
                row.append(("·", "noop"))
            else:
                row.append((str(dd), f"rpc:d:{year}{month:02d}{dd:02d}"))
        rows.append(row)
    rows.append([("⬅️ Back", back_cb), ("❌ Cancel", "menu")])
    return kb(rows)


# ----------------------------------------------------------------- repeat: create flow
async def rep_chooser(ev, state):
    await state.clear()
    chans = await db.channels()
    if not chans:
        return await show(ev, NO_CHANNELS, kb([[("➕ Add Channel", "addch")], [("⬅️ Back", "menu")]]))
    rows = [[(f"📺 {c['title']}", f"rp_ch:{c['chat_id']}")] for c in chans]
    rows.append([("⬅️ Back", "menu")])
    await show(ev, "🔁 <b>Repeat Post</b>\n\nSelect the channel to post in:", kb(rows))


@router.callback_query(F.data == "rep")
async def cb_rep(cb: CallbackQuery, state: FSMContext):
    await rep_chooser(cb, state)


def fresh_rule_data(chat_id):
    return {
        "chat_id": chat_id,
        "silent": 0,
        "preview": 0,
        "days": [],
        "times": [],
        "end_type": "never",
        "end_count": None,
        "end_date": None,
        "delete_after": None,
        "replace_prev": 0,
    }


@router.callback_query(F.data.startswith("rp_ch:"))
async def rp_channel(cb: CallbackQuery, state: FSMContext):
    await state.set_data(fresh_rule_data(int(cb.data.split(":")[1])))
    await state.set_state(RepeatNew.content)
    await show(cb, "🔁 <b>Repeat Post</b>\n\n" + CONTENT_PROMPT, kb([[("⬅️ Back", "rep")]]))


@router.callback_query(NewPost.when, F.data == "np_rep")
async def np_to_repeat(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    base = fresh_rule_data(d["chat_id"])
    base.update({"content": d["content"], "preview": d.get("preview", 0), "silent": d.get("silent", 0)})
    await state.set_data(base)
    await rep_ask_days(cb, state)


@router.message(RepeatNew.content)
async def rp_content(m: Message, state: FSMContext):
    if m.media_group_id:
        return await reply(m, "Albums are not supported. Please send a single message.")
    c = extract(m)
    if not c:
        return await reply(
            m,
            "This message type is not supported. Send text, photo, video, document, GIF, "
            "audio, voice or sticker.",
        )
    await state.update_data(content=c)
    if has_link(c):
        await state.set_state(RepeatNew.preview)
        return await reply(
            m,
            "🔗 This post contains a link.\nDo you want to show the link preview?",
            kb([[("✅ Preview ON", "rp_pre:1"), ("🚫 Preview OFF", "rp_pre:0")], [("❌ Cancel", "menu")]]),
        )
    await rep_ask_days(m, state)


@router.callback_query(RepeatNew.preview, F.data.startswith("rp_pre:"))
async def rp_preview(cb: CallbackQuery, state: FSMContext):
    await state.update_data(preview=int(cb.data.split(":")[1]))
    await rep_ask_days(cb, state)


async def rep_ask_days(ev, state):
    d = await state.get_data()
    await state.set_state(RepeatNew.days)
    await show(
        ev,
        "📅 <b>Which days?</b>\n\nTap the days to select or unselect them.",
        day_kb(d["days"], "rpd", ("➡️ Next", "rpd:ok"), ("❌ Cancel", "menu")),
    )


@router.callback_query(RepeatNew.days, F.data.startswith("rpd:"))
async def rp_days(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    act = parts[1]
    d = await state.get_data()
    if act == "ok":
        if not d["days"]:
            return await safe_answer(cb, "Select at least one day.", True)
        return await rep_ask_times(cb, state)
    await state.update_data(days=apply_day_action(d["days"], act, parts[2] if len(parts) > 2 else None))
    await rep_ask_days(cb, state)


async def rep_ask_times(ev, state):
    await state.set_state(RepeatNew.times)
    await show(ev, times_text(), kb([[("⬅️ Back", "rpt_back"), ("❌ Cancel", "menu")]]))


@router.callback_query(RepeatNew.times, F.data == "rpt_back")
async def rp_times_back(cb: CallbackQuery, state: FSMContext):
    await rep_ask_days(cb, state)


@router.message(RepeatNew.times)
async def rp_times(m: Message, state: FSMContext):
    t = parse_times(m.text or "")
    if not t:
        return await reply(
            m, "Invalid time. Examples: <code>09:00</code>, <code>09:00, 18:30</code>, <code>6:30pm</code>"
        )
    await state.update_data(times=t)
    await rep_ask_end(m, state)


async def rep_ask_end(ev, state):
    await state.set_state(RepeatNew.end)
    await show(
        ev,
        "🏁 <b>When should it stop?</b>",
        kb(
            [
                [("♾ Never", "rp_end:never")],
                [("🔢 After N posts", "rp_end:count"), ("📅 On a date", "rp_end:date")],
                [("⬅️ Back", "rpe_back"), ("❌ Cancel", "menu")],
            ]
        ),
    )


@router.callback_query(RepeatNew.end, F.data == "rpe_back")
async def rp_end_to_times(cb: CallbackQuery, state: FSMContext):
    await rep_ask_times(cb, state)


async def show_calendar(ev, year, month):
    await show(
        ev,
        "📅 <b>Pick the last day</b>\n\nPosts stop after this date (inclusive).\n"
        "You can also type a date like <code>31-12-2026</code>.",
        calendar_kb(year, month, "rp_end_back"),
    )


@router.callback_query(RepeatNew.end, F.data.startswith("rp_end:"))
async def rp_end(cb: CallbackQuery, state: FSMContext):
    k = cb.data.split(":")[1]
    if k == "never":
        await state.update_data(end_type="never", end_count=None, end_date=None)
        return await rep_ask_delete(cb, state)
    if k == "count":
        await state.set_state(RepeatNew.end_count)
        return await show(
            cb,
            "🔢 <b>How many posts in total?</b>\n\nSend a number, for example <code>10</code>.",
            kb([[("⬅️ Back", "rp_end_back"), ("❌ Cancel", "menu")]]),
        )
    await state.set_state(RepeatNew.end_date)
    today = datetime.now(TZ).date()
    await show_calendar(cb, today.year, today.month)


@router.callback_query(RepeatNew.end_count, F.data == "rp_end_back")
@router.callback_query(RepeatNew.end_date, F.data == "rp_end_back")
async def rp_end_back(cb: CallbackQuery, state: FSMContext):
    await rep_ask_end(cb, state)


@router.message(RepeatNew.end_count)
async def rp_count_msg(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not re.fullmatch(r"\d{1,4}", txt) or not 1 <= int(txt) <= 1000:
        return await reply(m, "Send a number between 1 and 1000, for example <code>10</code>.")
    await state.update_data(end_type="count", end_count=int(txt), end_date=None)
    await rep_ask_delete(m, state)


@router.callback_query(RepeatNew.end_date, F.data.startswith("rpc:"))
async def rp_calendar(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    if parts[1] == "m":
        return await show_calendar(cb, int(parts[2]), int(parts[3]))
    s = parts[2]
    await state.update_data(end_type="date", end_date=f"{s[:4]}-{s[4:6]}-{s[6:]}", end_count=None)
    await rep_ask_delete(cb, state)


@router.message(RepeatNew.end_date)
async def rp_end_date_msg(m: Message, state: FSMContext):
    mt = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", (m.text or "").strip())
    dt = None
    if mt:
        try:
            dt = date(int(mt.group(3)), int(mt.group(2)), int(mt.group(1)))
        except ValueError:
            dt = None
    if not dt:
        return await reply(m, "Invalid date. Example: <code>31-12-2026</code>")
    if dt < datetime.now(TZ).date():
        return await reply(m, "That date is in the past. Send a future date.")
    await state.update_data(end_type="date", end_date=dt.isoformat(), end_count=None)
    await rep_ask_delete(m, state)


async def rep_ask_delete(ev, state):
    d = await state.get_data()
    await state.set_state(RepeatNew.delete)
    rows = del_rows("rp_del:")
    rows.append([("♻️ Replace previous: ON" if d["replace_prev"] else "♻️ Replace previous: OFF", "rp_rep")])
    rows.append([("⬅️ Back", "rpx_back"), ("❌ Cancel", "menu")])
    await show(
        ev,
        del_text()
        + "\n\n♻️ Replace previous: when ON, the previous post of this repeat is deleted "
        "as soon as the new one is posted.",
        kb(rows),
    )


@router.callback_query(RepeatNew.delete, F.data == "rpx_back")
async def rp_del_back(cb: CallbackQuery, state: FSMContext):
    await rep_ask_end(cb, state)


@router.callback_query(RepeatNew.delete, F.data == "rp_rep")
async def rp_toggle_replace(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(replace_prev=0 if d["replace_prev"] else 1)
    await rep_ask_delete(cb, state)


@router.callback_query(RepeatNew.delete, F.data.startswith("rp_del:"))
async def rp_del_cb(cb: CallbackQuery, state: FSMContext):
    await state.update_data(delete_after=int(cb.data.split(":")[1]) or None)
    await rep_ask_confirm(cb, state)


@router.message(RepeatNew.delete)
async def rp_del_msg(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if not sec:
        return await reply(m, "Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    await state.update_data(delete_after=sec)
    await rep_ask_confirm(m, state)


async def rep_ask_confirm(ev, state):
    d = await state.get_data()
    ch = await db.channel(d["chat_id"])
    if not ch:
        await state.clear()
        return await show(ev, "That channel no longer exists.", kb([[("⬅️ Menu", "menu")]]))
    await state.set_state(RepeatNew.confirm)
    nxt = occurrences(d["days"], d["times"], int(time.time()), TZ, end_date_of(d), 3)
    L = ["<b>Review your repeat post</b>", ""] + rule_lines(d, ch["title"]) + ["", "⏭ Next posts:"]
    L += [f"• {fmt_dt(t)}" for t in nxt] or ["• none (check days, times and end date)"]
    sil = "🔕 Silent: ON" if d["silent"] else "🔔 Silent: OFF"
    await show(
        ev,
        "\n".join(L),
        kb([[(sil, "rp_sil")], [("✅ Create", "rp_ok"), ("❌ Cancel", "menu")]]),
    )


@router.callback_query(RepeatNew.confirm, F.data == "rp_sil")
async def rp_silent(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(silent=0 if d["silent"] else 1)
    await rep_ask_confirm(cb, state)


@router.callback_query(RepeatNew.confirm, F.data == "rp_ok")
async def rp_ok(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    nxt = occurrences(d["days"], d["times"], int(time.time()), TZ, end_date_of(d), 1)
    if not nxt:
        return await safe_answer(cb, "No upcoming post matches these settings. Change the days, times or end date.", True)
    rid = await db.create_repeat(
        owner_id=cb.from_user.id,
        chat_id=d["chat_id"],
        content=d["content"],
        link_preview=d["preview"],
        silent=d["silent"],
        days=d["days"],
        times=d["times"],
        end_type=d["end_type"],
        end_count=d["end_count"],
        end_date=d["end_date"],
        delete_after=d.get("delete_after"),
        replace_prev=d.get("replace_prev", 0),
        next_run=nxt[0],
    )
    await state.clear()
    await show(
        cb,
        f"✅ Repeat <b>R{rid}</b> created.\n\n⏭ First post: {fmt_dt(nxt[0])}",
        kb([[("🔁 Repeating", f"rlist:0:{d['chat_id']}"), ("⬅️ Menu", "menu")]]),
    )


# ----------------------------------------------------------------- repeat: list & manage
@router.callback_query(F.data.startswith("rlist:"))
async def cb_rlist(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    page, cid = parse_list(cb.data)
    total = await db.count_repeats(cid)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    ch = await db.channel(cid) if cid else None
    rows = []
    for r in await db.repeats(PAGE, page * PAGE, cid):
        icon = "🏁" if r["status"] == "ended" else ("⏸" if r["paused"] else "🔁")
        label = f"{icon} R{r['id']} · {days_short(r['days'])} · {','.join(r['times'])} · "
        if not cid:
            label += f"{(r['ch_title'] or '?')[:10]} · "
        label += plain(r["content"], 14)
        rows.append([(label, f"r:{r['id']}")])
    if pages > 1:
        rows.append(nav_row("rlist", page, pages, cid))
    rows.append([("⬅️ Back", f"list:0:{cid}" if cid else "sch")])
    head = f"🔁 <b>Repeating posts: {esc(ch['title']) if ch else 'All channels'}</b> ({total})\n"
    if total:
        text = head + "🔁 active · ⏸ paused · 🏁 finished\n\nTap a rule to manage it."
    else:
        text = head + "No repeating posts here. Use Repeat in the main menu to create one."
    await show(cb, text, kb(rows))


async def show_repeat(ev, rid):
    r = await db.repeat(rid)
    if not r:
        return await show(ev, "Repeat not found.", kb([[("⬅️ Back", "sch")]]))
    ch = await db.channel(r["chat_id"])
    ended = r["status"] == "ended"
    status = "🏁 Finished" if ended else ("⏸ Paused" if r["paused"] else "🟢 Active")
    L = [f"<b>Repeat R{rid}</b>", f"Status: {status}"] + rule_lines(r, ch["title"] if ch else r["chat_id"])
    if not ended and not r["paused"] and r["next_run"]:
        L.append(f"⏭ Next post: {fmt_dt(r['next_run'])}")
    L.append(f"📊 Posts created: {r['runs']}")
    back = ("⬅️ Back", f"rlist:0:{r['chat_id']}")
    if ended:
        rows = [[("👁 View", f"rp:view:{rid}"), ("❌ Delete", f"rp:delask:{rid}")], [back]]
    else:
        tog = []
        if has_link(r["content"]):
            tog.append((f"🔗 Preview: {'ON' if r['link_preview'] else 'OFF'}", f"rp:lnk:{rid}"))
        tog.append(("🔕 Silent: ON" if r["silent"] else "🔔 Silent: OFF", f"rp:sil:{rid}"))
        rows = [
            [("▶️ Resume" if r["paused"] else "⏸ Pause", f"rp:pause:{rid}"), ("✏️ Edit", f"rp:edit:{rid}")],
            [("📅 Days", f"rp:days:{rid}"), ("🕒 Times", f"rp:times:{rid}")],
            [("🗑 Auto-delete", f"rp:del:{rid}"), (f"♻️ Replace: {'ON' if r['replace_prev'] else 'OFF'}", f"rp:rep:{rid}")],
            [("⏭ Skip Next", f"rp:skip:{rid}"), ("⚡ Send Now", f"rp:now:{rid}")],
            tog,
            [("👁 View", f"rp:view:{rid}"), ("❌ Delete", f"rp:delask:{rid}")],
            [back],
        ]
    await show(ev, "\n".join(L), kb(rows))


@router.callback_query(F.data.startswith("r:"))
async def cb_repeat(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_repeat(cb, int(cb.data.split(":")[1]))


async def show_edit_days(ev, rid, days):
    await show(
        ev,
        f"📅 <b>Edit days for R{rid}</b>\n\nTap the days to select or unselect them.",
        day_kb(days, f"red:{rid}", ("✅ Save", f"red:{rid}:ok"), ("⬅️ Back", f"r:{rid}")),
    )


@router.callback_query(F.data.startswith("rp:"))
async def cb_rp(cb: CallbackQuery, state: FSMContext, bot: Bot):
    _, act, rid = cb.data.split(":")
    rid = int(rid)
    r = await db.repeat(rid)
    if not r:
        return await show(cb, "Repeat not found.", kb([[("⬅️ Back", "sch")]]))
    now = int(time.time())
    ended = r["status"] == "ended"

    if act == "view":
        await safe_answer(cb, "Sending a copy…")
        try:
            await send_content(bot, cb.from_user.id, r["content"], r["link_preview"], 1)
        except Exception as e:
            await reply(cb.message, f"Could not send preview: <code>{esc(e)}</code>")
        return
    if act == "delask":
        return await show(
            cb,
            f"Delete repeat <b>R{rid}</b>? No more posts will be created. "
            "Posts already in the channel are not touched.",
            kb([[("✅ Yes, delete", f"rp:delyes:{rid}"), ("⬅️ No", f"r:{rid}")]]),
        )
    if act == "delyes":
        await db.delete_repeat(rid)
        return await show(
            cb,
            f"🗑 Repeat R{rid} deleted.",
            kb([[("🔁 Repeating", f"rlist:0:{r['chat_id']}"), ("⬅️ Menu", "menu")]]),
        )
    if ended:
        return await safe_answer(cb, "This repeat is finished.", True)

    if act == "pause":
        if r["paused"]:
            nxt = rule_next(r, now)
            await db.set_repeat(rid, paused=False, next_run=nxt, status="active" if nxt else "ended")
        else:
            await db.set_repeat(rid, paused=True)
    elif act == "skip":
        if r["paused"] or not r["next_run"]:
            return await safe_answer(cb, "Resume the repeat first.", True)
        nxt = rule_next(r, r["next_run"])
        await db.set_repeat(rid, next_run=nxt, status="active" if nxt else "ended")
        await safe_answer(cb, "Skipped the next post.")
    elif act == "now":
        await db.create_post(
            owner_id=cb.from_user.id,
            chat_id=r["chat_id"],
            content=r["content"],
            link_preview=r["link_preview"],
            silent=r["silent"],
            send_at=now,
            delete_after=r["delete_after"],
            repeat_id=rid,
            replace_prev=r["replace_prev"],
        )
        await safe_answer(cb, "Sending…")
    elif act == "rep":
        await db.set_repeat(rid, replace_prev=0 if r["replace_prev"] else 1)
    elif act == "lnk":
        await db.set_repeat(rid, link_preview=0 if r["link_preview"] else 1)
    elif act == "sil":
        await db.set_repeat(rid, silent=0 if r["silent"] else 1)
    elif act == "edit":
        await state.set_state(RepeatEdit.content)
        await state.set_data({"rid": rid})
        return await show(
            cb,
            f"✏️ <b>Edit repeat R{rid}</b>\n\n{CONTENT_PROMPT}\n\nThe new message is used for all future posts.",
            kb([[("⬅️ Back", f"r:{rid}")]]),
        )
    elif act == "days":
        await state.set_state(RepeatEdit.days)
        await state.set_data({"rid": rid, "days": r["days"]})
        return await show_edit_days(cb, rid, r["days"])
    elif act == "times":
        await state.set_state(RepeatEdit.times)
        await state.set_data({"rid": rid})
        return await show(cb, times_text(r["times"]), kb([[("⬅️ Back", f"r:{rid}")]]))
    elif act == "del":
        await state.set_state(RepeatEdit.delete)
        await state.set_data({"rid": rid})
        return await show(cb, del_text(), kb(del_rows(f"rd:{rid}:", ("⬅️ Back", f"r:{rid}"))))
    await show_repeat(cb, rid)


@router.callback_query(RepeatEdit.days, F.data.startswith("red:"))
async def re_days(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")  # red:<rid>:t:3  /  red:<rid>:ok
    rid, act = int(parts[1]), parts[2]
    d = await state.get_data()
    if act == "ok":
        if not d["days"]:
            return await safe_answer(cb, "Select at least one day.", True)
        r = await db.repeat(rid)
        if r:
            nxt = rule_next({**r, "days": d["days"]}, int(time.time()))
            await db.set_repeat(rid, days=d["days"], next_run=nxt, status="active" if nxt else "ended")
        await state.clear()
        return await show_repeat(cb, rid)
    days = apply_day_action(d["days"], act, parts[3] if len(parts) > 3 else None)
    await state.update_data(days=days)
    await show_edit_days(cb, rid, days)


@router.message(RepeatEdit.times)
async def re_times(m: Message, state: FSMContext):
    rid = (await state.get_data())["rid"]
    t = parse_times(m.text or "")
    if not t:
        return await reply(
            m, "Invalid time. Examples: <code>09:00</code>, <code>09:00, 18:30</code>, <code>6:30pm</code>"
        )
    r = await db.repeat(rid)
    await state.clear()
    if not r:
        return await show(m, "Repeat not found.", kb([[("⬅️ Menu", "menu")]]))
    nxt = rule_next({**r, "times": t}, int(time.time()))
    await db.set_repeat(rid, times=t, next_run=nxt, status="active" if nxt else "ended")
    await show_repeat(m, rid)


@router.message(RepeatEdit.content)
async def re_content(m: Message, state: FSMContext):
    rid = (await state.get_data())["rid"]
    if m.media_group_id:
        return await reply(m, "Albums are not supported. Please send a single message.")
    c = extract(m)
    if not c:
        return await reply(m, "This message type is not supported.")
    await state.clear()
    await db.set_repeat(rid, content=c, link_preview=0)
    if has_link(c):
        return await reply(
            m,
            "✅ Content updated.\n🔗 This post contains a link. Show the link preview?",
            kb([[("✅ Preview ON", f"rep_pre:{rid}:1"), ("🚫 Preview OFF", f"rep_pre:{rid}:0")]]),
        )
    await show_repeat(m, rid)


@router.callback_query(F.data.startswith("rep_pre:"))
async def re_preview(cb: CallbackQuery):
    _, rid, val = cb.data.split(":")
    await db.set_repeat(int(rid), link_preview=int(val))
    await show_repeat(cb, int(rid))


@router.callback_query(F.data.startswith("rd:"))
async def rd_cb(cb: CallbackQuery, state: FSMContext):
    _, rid, sec = cb.data.split(":")
    await state.clear()
    await db.set_repeat(int(rid), delete_after=int(sec) or None)
    await show_repeat(cb, int(rid))


@router.message(RepeatEdit.delete)
async def re_del_msg(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if not sec:
        return await reply(m, "Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    rid = (await state.get_data())["rid"]
    await state.clear()
    await db.set_repeat(rid, delete_after=sec)
    await show_repeat(m, rid)


# ----------------------------------------------------------------- repeat: firing
async def fire_repeat(bot: Bot, r):
    """Called by the scheduler when a repeat rule is due. Creates a normal post."""
    now = int(time.time())
    late = now - r["next_run"]
    nxt = rule_next(r, now)
    runs = r["runs"]
    create = late <= LATE_LIMIT
    if create:
        runs += 1
    ended = nxt is None or (r["end_type"] == "count" and runs >= (r["end_count"] or 0))
    # update the rule first: a missed post is better than a duplicate post
    await db.set_repeat(
        r["id"],
        runs=runs,
        last_run=now if create else r.get("last_run"),
        next_run=None if ended else nxt,
        status="ended" if ended else "active",
    )
    if create:
        await db.create_post(
            owner_id=r["owner_id"],
            chat_id=r["chat_id"],
            content=r["content"],
            link_preview=r["link_preview"],
            silent=r["silent"],
            send_at=now,
            delete_after=r["delete_after"],
            repeat_id=r["id"],
            replace_prev=r["replace_prev"],
        )
    else:
        log.warning("Repeat %s skipped a run that was %ss late", r["id"], late)
    if ended:
        await notify(bot, r["owner_id"], f"🏁 Repeat R{r['id']} has finished. No more posts will be created.")


# ---- fallbacks (must stay last)
@router.message()
async def fallback_msg(m: Message):
    await reply(m, "Use /start to open the menu.")


@router.callback_query()
async def fallback_cb(cb: CallbackQuery):
    await cb.answer("This button has expired. Use /start.", show_alert=True)


# ----------------------------------------------------------------- scheduler
async def publish(bot: Bot, p):
    uid = p["owner_id"]
    # tell the owner right away, while the post is being sent
    status_task = asyncio.create_task(safe_send(bot, uid, "⏳ Posting… Please wait"))
    try:
        msg = await send_content(bot, p["chat_id"], p["content"], p["link_preview"], p["silent"])
    except TelegramRetryAfter as e:
        await db.set(p["id"], status="scheduled", send_at=int(time.time()) + e.retry_after + 1)
        st = await status_task
        if st:
            try:
                await st.delete()
            except Exception:
                pass
        return
    except Exception as e:
        log.exception("Publish failed for post %s", p["id"])
        await db.set(p["id"], status="failed", error=str(e)[:300])
        st = await status_task
        await edit_status(
            bot,
            st,
            uid,
            f"⚠️ Post #{p['id']} failed to publish:\n<code>{esc(e)}</code>",
            kb([[("🔁 Retry", f"pn:{p['id']}"), ("📋 Manage", f"p:{p['id']}")]]),
        )
        return
    now = int(time.time())
    da = now + p["delete_after"] if p["delete_after"] else None
    await db.set(p["id"], status="sent", message_id=msg.message_id, sent_at=now, delete_at=da, error=None)
    ch = await db.channel(p["chat_id"])
    text = f"✅ Posted to <b>{esc(ch['title']) if ch else p['chat_id']}</b>\n📝 {esc(plain(p['content'], 60))}"
    if da:
        text += f"\n🗑 Auto-deletes at {fmt_dt(da)}"
    st = await status_task
    await edit_status(bot, st, uid, text, kb([[("📋 Manage Post", f"p:{p['id']}")]]))
    # repeat option "Replace previous": remove the older post(s) of the same repeat
    if p.get("replace_prev") and p.get("repeat_id"):
        for old in await db.live_of_repeat(p["repeat_id"], p["id"]):
            try:
                await bot.delete_message(old["chat_id"], old["message_id"])
            except Exception as e:
                if "not found" not in str(e).lower():
                    log.warning("Could not replace previous post %s: %s", old["id"], e)
            await db.set(old["id"], status="deleted", delete_at=None)


async def remove_live(bot: Bot, p):
    try:
        await bot.delete_message(p["chat_id"], p["message_id"])
    except TelegramRetryAfter:
        return
    except Exception as e:
        if "not found" not in str(e).lower():
            log.exception("Delete failed for post %s", p["id"])
            await db.set(p["id"], status="delete_failed", error=str(e)[:300])
            await notify(
                bot,
                p["owner_id"],
                f"⚠️ Could not auto-delete post #{p['id']}:\n<code>{esc(e)}</code>\n"
                "Make sure the bot can delete messages in the channel.",
            )
            return
    await db.set(p["id"], status="deleted", delete_at=None)
    await notify(bot, p["owner_id"], f"🗑 Post #{p['id']} was auto-deleted from the channel.")


async def scheduler(bot: Bot):
    """Sleeps until the exact next due time (or until something changes), so posts go out on the second."""
    while True:
        nxt = None
        try:
            now = int(time.time())
            for r in await db.due_repeats(now):
                await fire_repeat(bot, r)
            for p in await db.due_posts(now):
                if await db.claim(p["id"]):
                    await publish(bot, p)
            for p in await db.due_deletes(now):
                await remove_live(bot, p)
            nxt = await db.next_due()
        except Exception:
            log.exception("Scheduler error")
        delay = 5.0 if nxt is None else min(5.0, max(0.05, nxt - time.time()))
        try:
            await asyncio.wait_for(db.changed.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        db.changed.clear()


async def main():
    if not OWNERS:
        log.warning("OWNER_IDS is empty. Send /id to the bot, then set OWNER_IDS and redeploy.")
    await db.init()
    await db.reset_sending()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(public)
    dp.include_router(router)
    task = asyncio.create_task(scheduler(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot started")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
