import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime, timedelta
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
OWNERS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
PAGE = 8
EDITABLE = ("scheduled", "failed")
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


async def show(ev, text, markup=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.answer()
        except Exception:
            pass
        try:
            await ev.message.edit_text(text, reply_markup=markup, link_preview_options=NOPREV)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        await ev.message.answer(text, reply_markup=markup, link_preview_options=NOPREV)
    else:
        await ev.answer(text, reply_markup=markup, link_preview_options=NOPREV)


async def notify(bot, uid, text):
    try:
        await bot.send_message(uid, text, link_preview_options=NOPREV)
    except Exception:
        log.warning("Could not notify %s", uid)


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
    if t in ("sticker", "video_note"):
        return await getattr(bot, f"send_{t}")(**{t: c["file_id"]}, **kw)
    return await getattr(bot, f"send_{t}")(**{t: c["file_id"]}, caption=c.get("text"), **kw)


# ----------------------------------------------------------------- states
class NewPost(StatesGroup):
    content = State()
    preview = State()
    when = State()
    delete = State()
    confirm = State()


class EditPost(StatesGroup):
    content = State()
    when = State()
    delete = State()


class AddChannel(StatesGroup):
    waiting = State()


# ----------------------------------------------------------------- texts / keyboards
MENU = kb(
    [
        [("➕ New Post", "new")],
        [("📋 Scheduled Posts", "list:0")],
        [("📺 My Channels", "chs")],
    ]
)
MENU_TEXT = "👋 <b>Channel Post Scheduler</b>\n\nChoose an option:"

CONTENT_PROMPT = (
    "Send the message you want to post.\n\n"
    "Supported: text, photo, video, document, GIF, audio, voice, sticker. "
    "Formatting and captions are preserved.\n"
    "Send one message at a time (albums are not supported)."
)


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


def del_kb(prefix, cancel):
    return kb(
        [
            [("🚫 No auto-delete", f"{prefix}0")],
            [("1h", f"{prefix}3600"), ("6h", f"{prefix}21600"), ("12h", f"{prefix}43200")],
            [("24h", f"{prefix}86400"), ("48h", f"{prefix}172800"), ("7d", f"{prefix}604800")],
            [("❌ Cancel", cancel)],
        ]
    )


# ----------------------------------------------------------------- public router
@public.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Your Telegram ID: <code>{m.from_user.id}</code>")


@public.message(F.chat.type == "private", F.from_user.id.not_in(OWNERS))
async def not_owner(m: Message):
    await m.answer("🔒 This is a private bot.")


# ----------------------------------------------------------------- menu
@router.message(CommandStart())
@router.message(Command("cancel"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await show(m, MENU_TEXT, MENU)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, MENU_TEXT, MENU)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ----------------------------------------------------------------- channels
@router.callback_query(F.data == "chs")
async def cb_channels(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    chans = await db.channels()
    text = "📺 <b>My Channels</b>\n\n"
    text += "\n".join(f"• {esc(c['title'])}" for c in chans) if chans else "No channels added yet."
    rows = [[(f"🗑 Remove: {c['title']}", f"chr:{c['chat_id']}")] for c in chans]
    rows.append([("➕ Add Channel", "addch")])
    rows.append([("⬅️ Menu", "menu")])
    await show(cb, text, kb(rows))


@router.callback_query(F.data.startswith("chr:"))
async def cb_channel_remove(cb: CallbackQuery, state: FSMContext):
    await db.remove_channel(int(cb.data.split(":")[1]))
    await cb_channels(cb, state)


@router.callback_query(F.data == "addch")
async def cb_add_channel(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.waiting)
    await show(
        cb,
        "➕ <b>Add a channel</b>\n\n"
        "1. Add this bot as an <b>admin</b> of your channel with permission to "
        "<b>Post messages</b> and <b>Delete messages</b>. It will be detected automatically.\n\n"
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
        return await m.answer(
            "Could not find that channel. Forward a message from it, or send its @username / ID."
        )
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except Exception:
        me = None
    if not me or me.status != "administrator":
        return await m.answer("The bot is not an admin in that channel. Add it as admin and try again.")
    await db.add_channel(chat.id, chat.title or str(chat.id), chat.username)
    await state.clear()
    warn = ""
    if not getattr(me, "can_post_messages", True):
        warn += "\n⚠️ The bot does not have permission to post messages."
    if not getattr(me, "can_delete_messages", True):
        warn += "\n⚠️ The bot does not have permission to delete messages (auto-delete will fail)."
    await m.answer(f"✅ Channel <b>{esc(chat.title)}</b> added.{warn}", reply_markup=MENU)


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
@router.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    chans = await db.channels()
    if not chans:
        return await show(
            cb,
            "You have no channels yet. Add one first.",
            kb([[("➕ Add Channel", "addch")], [("⬅️ Menu", "menu")]]),
        )
    rows = [[(f"📺 {c['title']}", f"np_ch:{c['chat_id']}")] for c in chans]
    rows.append([("❌ Cancel", "menu")])
    await show(cb, "Select the channel to post in:", kb(rows))


@router.callback_query(F.data.startswith("np_ch:"))
async def np_channel(cb: CallbackQuery, state: FSMContext):
    await state.set_data({"chat_id": int(cb.data.split(":")[1]), "silent": 0, "preview": 0})
    await state.set_state(NewPost.content)
    await show(cb, CONTENT_PROMPT, kb([[("❌ Cancel", "menu")]]))


@router.message(NewPost.content)
async def np_content(m: Message, state: FSMContext):
    if m.media_group_id:
        return await m.answer("Albums are not supported. Please send a single message.")
    c = extract(m)
    if not c:
        return await m.answer(
            "This message type is not supported. Send text, photo, video, document, GIF, "
            "audio, voice or sticker."
        )
    await state.update_data(content=c)
    if has_link(c):
        await state.set_state(NewPost.preview)
        return await m.answer(
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
    await show(ev, when_text(), kb([[("⚡ Post now", "np_now")], [("❌ Cancel", "menu")]]))


async def np_set_time(ev, state, dt):
    ts = int(dt.timestamp())
    if ts < time.time() - 60:
        return await show(ev, "That time is in the past. Send a future time.")
    await state.update_data(send_at=max(ts, int(time.time())))
    await state.set_state(NewPost.delete)
    await show(ev, del_text(), del_kb("np_del:", "menu"))


@router.callback_query(NewPost.when, F.data == "np_now")
async def np_now(cb: CallbackQuery, state: FSMContext):
    await np_set_time(cb, state, datetime.now(TZ))


@router.message(NewPost.when)
async def np_when(m: Message, state: FSMContext):
    dt = parse_when(m.text or "")
    if not dt:
        return await m.answer("Invalid time. Examples: <code>18:30</code>, <code>25-12-2026 18:30</code>, <code>2h</code>")
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
        return await m.answer("Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    await state.update_data(delete_after=sec)
    await ask_confirm(m, state)


async def ask_confirm(ev, state):
    d = await state.get_data()
    ch = await db.channel(d["chat_id"])
    if not ch:
        await state.clear()
        return await show(ev, "That channel no longer exists.", MENU)
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
    await show(ev, "\n".join(L), kb([[(sil, "np_sil")], [("✅ Schedule", "np_ok")], [("❌ Cancel", "menu")]]))


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
    when = "within a few seconds" if d["send_at"] <= time.time() + 5 else f"at {fmt_dt(d['send_at'])}"
    await show(
        cb,
        f"✅ Post <b>#{pid}</b> scheduled. It will be published {when}.",
        kb([[("📋 Scheduled Posts", "list:0")], [("➕ New Post", "new")], [("⬅️ Menu", "menu")]]),
    )


# ----------------------------------------------------------------- scheduled list
@router.callback_query(F.data.startswith("list:"))
async def cb_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    total = await db.count_active()
    if not total:
        return await show(
            cb,
            "📋 No scheduled or auto-deleting posts.",
            kb([[("➕ New Post", "new")], [("⬅️ Menu", "menu")]]),
        )
    pages = (total + PAGE - 1) // PAGE
    page = max(0, min(int(cb.data.split(":")[1]), pages - 1))
    rows = []
    for p in await db.active(PAGE, page * PAGE):
        if p["status"] == "sent":
            icon, ts = "🗑", p["delete_at"]
        elif p["status"] == "failed":
            icon, ts = "⚠️", p["send_at"]
        else:
            icon, ts = "⏳", p["send_at"]
        when = datetime.fromtimestamp(ts, TZ).strftime("%d %b %H:%M")
        label = f"{icon} #{p['id']} · {when} · {p['ch_title'] or '?'} · {plain(p['content'], 20)}"
        rows.append([(label, f"p:{p['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(("⬅️", f"list:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"list:{page + 1}"))
        rows.append(nav)
    rows.append([("⬅️ Menu", "menu")])
    text = (
        f"📋 <b>Scheduled posts</b> ({total})\n"
        "⏳ waiting to post · 🗑 live, will auto-delete · ⚠️ failed\n"
        f"Times are in {esc(TZ.key)}.\n\nTap a post to manage it."
    )
    await show(cb, text, kb(rows))


async def show_post(ev, pid):
    p = await db.post(pid)
    if not p:
        return await show(ev, "Post not found.", kb([[("⬅️ Back", "list:0")]]))
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

    if st in EDITABLE:
        rows = [
            [("✏️ Edit content", f"pe_c:{pid}"), ("🕒 Change time", f"pe_t:{pid}")],
            [("🗑 Auto-delete", f"pe_d:{pid}"), ("👁 Preview", f"pv:{pid}")],
        ]
        tog = []
        if has_link(c):
            tog.append((f"🔗 Preview: {'ON' if p['link_preview'] else 'OFF'}", f"pt_pre:{pid}"))
        tog.append(("🔕 Silent: ON" if p["silent"] else "🔔 Silent: OFF", f"pt_sil:{pid}"))
        rows.append(tog)
        rows.append([("🔁 Retry now" if st == "failed" else "⚡ Post now", f"pn:{pid}"), ("❌ Delete", f"pdel:{pid}")])
    else:
        rows = [
            [("🗑 Change auto-delete time", f"pe_d:{pid}")],
            [("👁 Preview", f"pv:{pid}"), ("🗑 Delete from channel now", f"pdn:{pid}")],
        ]
    rows.append([("⬅️ Back", "list:0")])
    await show(ev, "\n".join(L), kb(rows))


async def editable(cb, pid):
    p = await db.post(pid)
    if not p or p["status"] not in EDITABLE:
        try:
            await cb.answer("This post is no longer scheduled.", show_alert=True)
        except Exception:
            pass
        await show(cb, "Post not found or already published.", kb([[("⬅️ Back", "list:0")]]))
        return None
    return p


@router.callback_query(F.data.startswith("p:"))
async def cb_post(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_post(cb, int(cb.data.split(":")[1]))


@router.callback_query(F.data.startswith("pv:"))
async def cb_preview(cb: CallbackQuery, bot: Bot):
    p = await db.post(int(cb.data.split(":")[1]))
    if not p:
        return await cb.answer("Post not found.", show_alert=True)
    await cb.answer("Sending preview…")
    try:
        await send_content(bot, cb.from_user.id, p["content"], p["link_preview"], 1)
    except Exception as e:
        await cb.message.answer(f"Could not send preview: <code>{esc(e)}</code>")


@router.callback_query(F.data.startswith("pt_pre:"))
async def cb_toggle_preview(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = await editable(cb, pid)
    if p:
        await db.set(pid, link_preview=0 if p["link_preview"] else 1)
        await show_post(cb, pid)


@router.callback_query(F.data.startswith("pt_sil:"))
async def cb_toggle_silent(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = await editable(cb, pid)
    if p:
        await db.set(pid, silent=0 if p["silent"] else 1)
        await show_post(cb, pid)


@router.callback_query(F.data.startswith("pn:"))
async def cb_post_now(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    if await editable(cb, pid):
        await db.set(pid, status="scheduled", send_at=int(time.time()), error=None)
        await cb.answer("Posting in a few seconds…", show_alert=True)
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
    if await editable(cb, pid):
        await db.delete_post(pid)
        await show(cb, f"🗑 Post #{pid} deleted.", kb([[("📋 Scheduled Posts", "list:0")], [("⬅️ Menu", "menu")]]))


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
        return await show(cb, "Post not found or not live.", kb([[("⬅️ Back", "list:0")]]))
    try:
        await bot.delete_message(p["chat_id"], p["message_id"])
    except Exception as e:
        if "not found" not in str(e).lower():
            return await show(cb, f"Could not delete: <code>{esc(e)}</code>", kb([[("⬅️ Back", f"p:{pid}")]]))
    await db.set(pid, status="deleted", delete_at=None)
    await show(cb, f"🗑 Post #{pid} deleted from the channel.", kb([[("📋 Scheduled Posts", "list:0")], [("⬅️ Menu", "menu")]]))


# ---- edit content
@router.callback_query(F.data.startswith("pe_c:"))
async def pe_content(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    if await editable(cb, pid):
        await state.set_state(EditPost.content)
        await state.set_data({"post_id": pid})
        await show(cb, f"✏️ <b>Edit post #{pid}</b>\n\n{CONTENT_PROMPT}", kb([[("❌ Cancel", f"p:{pid}")]]))


@router.message(EditPost.content)
async def ep_content(m: Message, state: FSMContext):
    pid = (await state.get_data())["post_id"]
    if m.media_group_id:
        return await m.answer("Albums are not supported. Please send a single message.")
    c = extract(m)
    if not c:
        return await m.answer("This message type is not supported.")
    p = await db.post(pid)
    await state.clear()
    if not p or p["status"] not in EDITABLE:
        return await show(m, "This post is no longer editable.", MENU)
    await db.set(pid, content=c, link_preview=0)
    if has_link(c):
        return await m.answer(
            "✅ Content updated.\n🔗 This post contains a link. Show the link preview?",
            reply_markup=kb([[("✅ Preview ON", f"ep_pre:{pid}:1"), ("🚫 Preview OFF", f"ep_pre:{pid}:0")]]),
        )
    await show_post(m, pid)


@router.callback_query(F.data.startswith("ep_pre:"))
async def ep_preview(cb: CallbackQuery):
    _, pid, val = cb.data.split(":")
    await db.set(int(pid), link_preview=int(val))
    await show_post(cb, int(pid))


# ---- edit time
@router.callback_query(F.data.startswith("pe_t:"))
async def pe_time(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    if await editable(cb, pid):
        await state.set_state(EditPost.when)
        await state.set_data({"post_id": pid})
        await show(cb, when_text(), kb([[("❌ Cancel", f"p:{pid}")]]))


@router.message(EditPost.when)
async def ep_when(m: Message, state: FSMContext):
    pid = (await state.get_data())["post_id"]
    dt = parse_when(m.text or "")
    if not dt:
        return await m.answer("Invalid time. Examples: <code>18:30</code>, <code>25-12-2026 18:30</code>, <code>2h</code>")
    ts = int(dt.timestamp())
    if ts < time.time() - 60:
        return await m.answer("That time is in the past. Send a future time.")
    p = await db.post(pid)
    await state.clear()
    if not p or p["status"] not in EDITABLE:
        return await show(m, "This post is no longer editable.", MENU)
    await db.set(pid, send_at=max(ts, int(time.time())), status="scheduled", error=None)
    await show_post(m, pid)


# ---- edit auto-delete (works for scheduled and live posts)
@router.callback_query(F.data.startswith("pe_d:"))
async def pe_delete(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    p = await db.post(pid)
    if not p or p["status"] not in (*EDITABLE, "sent"):
        return await show(cb, "Post not found.", kb([[("⬅️ Back", "list:0")]]))
    await state.set_state(EditPost.delete)
    await state.set_data({"post_id": pid})
    await show(cb, del_text(live=p["status"] == "sent"), del_kb(f"ed:{pid}:", f"p:{pid}"))


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
        return await m.answer("Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    pid = (await state.get_data())["post_id"]
    await state.clear()
    await apply_delete(pid, sec)
    await show_post(m, pid)


# ---- fallbacks (must stay last)
@router.message()
async def fallback_msg(m: Message):
    await m.answer("Use /start to open the menu.")


@router.callback_query()
async def fallback_cb(cb: CallbackQuery):
    await cb.answer("This button has expired. Use /start.", show_alert=True)


# ----------------------------------------------------------------- scheduler
async def publish(bot: Bot, p):
    try:
        msg = await send_content(bot, p["chat_id"], p["content"], p["link_preview"], p["silent"])
    except TelegramRetryAfter as e:
        await db.set(p["id"], status="scheduled", send_at=int(time.time()) + e.retry_after + 1)
        return
    except Exception as e:
        log.exception("Publish failed for post %s", p["id"])
        await db.set(p["id"], status="failed", error=str(e)[:300])
        await notify(
            bot,
            p["owner_id"],
            f"⚠️ Post #{p['id']} failed to publish:\n<code>{esc(e)}</code>\n"
            "Open Scheduled Posts to retry or edit it.",
        )
        return
    now = int(time.time())
    da = now + p["delete_after"] if p["delete_after"] else None
    await db.set(p["id"], status="sent", message_id=msg.message_id, sent_at=now, delete_at=da, error=None)
    ch = await db.channel(p["chat_id"])
    text = f"✅ Post #{p['id']} published to <b>{esc(ch['title']) if ch else p['chat_id']}</b>."
    if da:
        text += f"\n🗑 Auto-deletes at {fmt_dt(da)}"
    await notify(bot, p["owner_id"], text)


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
    while True:
        try:
            now = int(time.time())
            for p in await db.due_posts(now):
                if await db.claim(p["id"]):
                    await publish(bot, p)
            for p in await db.due_deletes(now):
                await remove_live(bot, p)
        except Exception:
            log.exception("Scheduler error")
        await asyncio.sleep(5)


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
