import asyncio
import html
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
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
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
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
DELETE_IMAGE = os.getenv("DELETE_IMAGE", "https://files.catbox.moe/o8t836.jpg")
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


# ---- messages of the running flow are remembered, so Cancel can delete them again
TRASH = {}
FLOW_PREFIXES = ("NewPost:", "RepeatNew:", "RepeatEdit:")


def trash_add(chat_id, *mids):
    s = TRASH.setdefault(chat_id, set())
    for mid in mids:
        if mid:
            s.add(mid)


def trash_forget(chat_id):
    TRASH.pop(chat_id, None)


async def safe_delete(bot, chat_id, ids):
    ids = sorted({i for i in ids if i})
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
        except Exception:
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception:
                    pass


async def trash_clear(bot, chat_id):
    await safe_delete(bot, chat_id, TRASH.pop(chat_id, set()))


class TrackUserMessages(BaseMiddleware):
    """While a flow (New Post / Repeat) is running, remember every message the user sends."""

    async def __call__(self, handler, event, data):
        try:
            state = data.get("state")
            st = await state.get_state() if state else None
            if st and st.startswith(FLOW_PREFIXES):
                trash_add(event.chat.id, event.message_id)
        except Exception:
            pass
        return await handler(event, data)


router.message.outer_middleware(TrackUserMessages())


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
            m2 = await msg.answer(text, reply_markup=markup, link_preview_options=NOPREV)
            trash_add(m2.chat.id, m2.message_id)
            return
        try:
            await msg.edit_text(text, reply_markup=markup, link_preview_options=NOPREV)
            trash_add(msg.chat.id, msg.message_id)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        m2 = await msg.answer(text, reply_markup=markup, link_preview_options=NOPREV)
        trash_add(m2.chat.id, m2.message_id)
    else:
        m2 = await ev.answer(text, reply_markup=markup, link_preview_options=NOPREV)
        trash_add(m2.chat.id, m2.message_id)


async def show_photo(ev, url, caption, markup=None):
    """Show a screen as photo + caption (edits the caption if the screen is already a photo)."""
    caption = bold(caption)
    msg = ev.message
    await safe_answer(ev)
    if getattr(msg, "photo", None):
        try:
            await msg.edit_caption(caption=caption, reply_markup=markup)
            trash_add(msg.chat.id, msg.message_id)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    try:
        await msg.delete()
    except Exception:
        pass
    try:
        m2 = await msg.answer_photo(photo=url, caption=caption, reply_markup=markup)
    except Exception:
        log.warning("Could not send screen image, falling back to text")
        m2 = await msg.answer(caption, reply_markup=markup, link_preview_options=NOPREV)
    trash_add(m2.chat.id, m2.message_id)


async def notify(bot, uid, text, markup=None):
    try:
        await bot.send_message(uid, bold(text), reply_markup=markup, link_preview_options=NOPREV)
    except Exception:
        log.warning("Could not notify %s", uid)


async def reply(target, text, reply_markup=None):
    """Send a new message (bold, monospace kept)."""
    m2 = await target.answer(bold(text), reply_markup=reply_markup, link_preview_options=NOPREV)
    trash_add(m2.chat.id, m2.message_id)
    return m2


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
    if c["type"] == "album":
        return f"[album x{len(c['items'])}] {t}".strip()
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


ALBUMS = {}
SEEN_ALBUMS = set()
SKIP = object()
ALBUM_TYPES = ("photo", "video", "document", "audio")
ALBUM_MEDIA = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}


def album_item(m):
    for t in ALBUM_TYPES:
        obj = getattr(m, t, None)
        if obj:
            return {"type": t, "file_id": obj[-1].file_id if t == "photo" else obj.file_id}
    return None


async def gather_album(m):
    """Telegram sends an album as separate messages. The first one waits briefly and collects them all.
    Returns the content dict (first message only), SKIP (other messages of the album) or None (unsupported)."""
    gid = m.media_group_id
    first = gid not in ALBUMS
    ALBUMS.setdefault(gid, {"msgs": []})["msgs"].append(m)
    if not first:
        return SKIP
    await asyncio.sleep(1.2)
    msgs = sorted(ALBUMS.pop(gid)["msgs"], key=lambda x: x.message_id)
    items = [album_item(x) for x in msgs]
    if any(i is None for i in items):
        return None
    caption = next((x.html_text for x in msgs if x.caption), None)
    mids = [x.message_id for x in msgs]
    if len(items) == 1:
        return {**items[0], "text": caption, "_mids": mids}
    return {"type": "album", "items": items, "text": caption, "_mids": mids}


async def get_content(m):
    if m.media_group_id:
        return await gather_album(m)
    return extract(m)


async def send_content(bot: Bot, chat_id, c, preview, silent, markup=None):
    kw = {"chat_id": chat_id, "disable_notification": bool(silent)}
    t = c["type"]
    if t == "album":
        media = [
            ALBUM_MEDIA[it["type"]](media=it["file_id"], caption=c.get("text") if i == 0 else None)
            for i, it in enumerate(c["items"])
        ]
        return await bot.send_media_group(media=media, **kw)  # returns a list of messages
    if markup is not None:
        kw["reply_markup"] = markup
    if t == "text":
        return await bot.send_message(
            text=c["text"], link_preview_options=LinkPreviewOptions(is_disabled=not preview), **kw
        )
    if t in NO_TEXT_TYPES:
        return await getattr(bot, f"send_{t}")(**{t: c["file_id"]}, **kw)
    return await getattr(bot, f"send_{t}")(**{t: c["file_id"]}, caption=c.get("text"), **kw)


async def delete_live(bot: Bot, p):
    """Delete a post from the channel (all messages, for albums)."""
    ids = p.get("message_ids") or [p["message_id"]]
    await bot.delete_messages(chat_id=p["chat_id"], message_ids=ids)


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
    channels = State()
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
    channels = State()
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
        [("🗓 Scheduled", "sch"), ("📢 My Channels", "chs")],
    ]
)


def menu_text(user):
    name = esc(user.first_name or "there")
    return (
        f'Hey <a href="tg://user?id={user.id}">{name}</a> !! 💌\n\n'
        "Welcome To The Post Scheduler Bot! ❤️✨\n\n"
        "Schedule Posts To Your Channels, Set Auto-Delete Timers And Manage Everything From One Place.\n\n"
        "⏰ Timed • 🗑 Auto-Delete • ✏️ Editable\n\n"
        "👇 Choose An Option Below."
    )


CONTENT_PROMPT = "Send the message you want to post."
PROMPT_MULTI = "Send the messages you want to post."
REPEAT_PROMPT = "🔁 <b>Repeat Post</b>\n\n" + PROMPT_MULTI
NO_CHANNELS = "You Have No Channels Yet. Add One First."


def when_text():
    now = datetime.now(TZ).strftime("%I:%M %p")
    return (
        "🕒 <b>When should this be posted?</b>\n"
        f"Now: {now}\n"
        "Send <code>18:30</code>, <code>25-12-2026 18:30</code> or <code>2h</code>."
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
    text = bold(menu_text(ev.from_user))
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


async def abort_flow(ev, state):
    """Cancel the running flow and delete every message that belongs to it."""
    msg = ev if isinstance(ev, Message) else ev.message
    cur = await state.get_state()
    if cur and cur.startswith(FLOW_PREFIXES):
        await trash_clear(msg.bot, msg.chat.id)
    else:
        trash_forget(msg.chat.id)
    await state.clear()


@router.message(CommandStart())
@router.message(Command("cancel"))
async def cmd_start(m: Message, state: FSMContext):
    await abort_flow(m, state)
    await show_menu(m)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await abort_flow(cb, state)
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
    text = "📢 <b>My Channels !!</b>\n\n" + (
        "Select A Channel To Manage It:" if chans else "No Channels Added Yet."
    )
    rows = [[(c["title"], f"ch:{c['chat_id']}")] for c in chans]
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
        "➕ <b>Add Channel</b>\n\n"
        "1. Make this bot an admin of your channel (Post, Edit, Delete). It is added automatically.\n\n"
        "2. Or forward any message from the channel here, or send its @username or ID.",
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
async def channel_pick_screen(ev, state, head, prefix):
    d = await state.get_data()
    sel = d.get("sel", [])
    chans = await db.channels()
    rows = [[(("✅ " if c["chat_id"] in sel else "") + c["title"], f"{prefix}:t:{c['chat_id']}")] for c in chans]
    rows.append([("☑️ Unselect All" if chans and len(sel) == len(chans) else "☑️ Select All", f"{prefix}:all")])
    rows.append([("⬅️ Back", "menu"), ("➡️ Next", f"{prefix}:go")])
    text = f"{head}\n\nSelect The Channels To Post In:"
    if sel:
        text += f"\n\nSelected: {len(sel)}"
    await show(ev, text, kb(rows))


async def pick_channels(cb, state, prefix):
    """Toggle / select-all buttons. Returns the chosen channel ids when Next is tapped, else None."""
    parts = cb.data.split(":")
    d = await state.get_data()
    sel = list(d.get("sel", []))
    if parts[1] == "t":
        cid = int(parts[2])
        sel = [x for x in sel if x != cid] if cid in sel else sel + [cid]
    elif parts[1] == "all":
        ids = [c["chat_id"] for c in await db.channels()]
        sel = [] if len(sel) == len(ids) else ids
    else:
        if not sel:
            await safe_answer(cb, "Select at least one channel.", True)
            return None
        return sel
    await state.update_data(sel=sel)
    return None


async def new_chooser(ev, state):
    msg = ev if isinstance(ev, Message) else ev.message
    trash_forget(msg.chat.id)
    await state.clear()
    chans = await db.channels()
    if not chans:
        return await show(
            ev, NO_CHANNELS, kb([[("➕ Add Channel", "addch")], [("⬅️ Back", "menu")]])
        )
    await state.set_data({"sel": [], "silent": 0, "preview": 0})
    await state.set_state(NewPost.channels)
    await channel_pick_screen(ev, state, "➕ <b>New Post !!</b>", "npc")


@router.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await new_chooser(cb, state)


@router.callback_query(NewPost.channels, F.data.startswith("npc:"))
async def np_pick_channels(cb: CallbackQuery, state: FSMContext):
    chosen = await pick_channels(cb, state, "npc")
    if chosen:
        return await np_start_collect(cb, state)
    await channel_pick_screen(cb, state, "➕ <b>New Post !!</b>", "npc")


async def np_start_collect(ev, state):
    d = await state.get_data()
    sel = d["sel"]
    await state.update_data(
        chat_ids=sel,
        chat_id=sel[0],
        pfx="np",
        coll_back="np_bk_ch",
        coll_prompt=PROMPT_MULTI,
        items=d.get("items", []),
        ctrl_id=None,
        phase="collect",
    )
    await state.set_state(NewPost.content)
    if d.get("items"):
        await coll_edit(ev, state, "np")
    else:
        await show(ev, PROMPT_MULTI, kb([[("⬅️ Back", "np_bk_ch")]]))


@router.callback_query(F.data.startswith("np_ch:"))
async def np_channel(cb: CallbackQuery, state: FSMContext):
    trash_forget(cb.message.chat.id)
    await state.clear()
    await state.set_data({"sel": [int(cb.data.split(":")[1])], "silent": 0, "preview": 0, "items": []})
    await np_start_collect(cb, state)


@router.callback_query(NewPost.content, F.data == "np_bk_ch")
async def np_back_channels(cb: CallbackQuery, state: FSMContext):
    await coll_drop_previews(cb.bot, cb.message.chat.id, state, keep=cb.message.message_id)
    await state.set_state(NewPost.channels)
    await channel_pick_screen(cb, state, "➕ <b>New Post !!</b>", "npc")


# ---- "send several messages, review the preview, then continue" (New Post, Repeat, Repeat edit)
USER_LOCKS = {}


def user_lock(uid):
    return USER_LOCKS.setdefault(uid, asyncio.Lock())


def coll_text(n):
    return f"📥 <b>{n} message{'s' if n != 1 else ''} added</b>\n\nSend more messages, or tap Done."


def coll_kb(pfx, back_cb):
    return kb([[("✅ Done", f"{pfx}_done")], [("⬅️ Back", back_cb), ("❌ Cancel", "menu")]])


def prev_text(n):
    return (
        f"👀 <b>{n} message{'s' if n != 1 else ''} ready</b>\n\n"
        "Tap 🗑 Remove under any message you do not want, then tap Continue."
    )


def prev_kb(pfx, back_cb):
    return kb(
        [
            [("➕ Add More", f"{pfx}_more"), ("✅ Continue", f"{pfx}_go")],
            [("⬅️ Back", back_cb), ("❌ Cancel", "menu")],
        ]
    )


async def coll_send(target, state, pfx):
    """Send a fresh 'N messages added' message at the bottom (the old one is removed)."""
    d = await state.get_data()
    if d.get("ctrl_id"):
        await safe_delete(target.bot, target.chat.id, [d["ctrl_id"]])
    msg = await reply(target, coll_text(len(d.get("items", []))), coll_kb(pfx, d["coll_back"]))
    await state.update_data(ctrl_id=msg.message_id, phase="collect")


async def coll_edit(cb, state, pfx):
    """Turn the current screen into the 'N messages added' screen."""
    d = await state.get_data()
    await show(cb, coll_text(len(d.get("items", []))), coll_kb(pfx, d["coll_back"]))
    await state.update_data(ctrl_id=cb.message.message_id, phase="collect")


async def coll_drop_previews(bot, chat_id, state, keep=None):
    """Delete the preview messages (and the preview control message, unless it is `keep`)."""
    d = await state.get_data()
    items = list(d.get("items", []))
    ids = []
    for it in items:
        ids += it.get("pv", [])
        it["pv"] = []
    if d.get("phase") == "preview" and d.get("ctrl_id") and d["ctrl_id"] != keep:
        ids.append(d["ctrl_id"])
    await safe_delete(bot, chat_id, ids)
    await state.update_data(items=items, phase="collect", ctrl_id=None)


async def coll_add(m, state, pfx):
    c = await get_content(m)
    if c is SKIP:
        return
    if not c:
        return await reply(
            m,
            "This message type is not supported. Send text, photo, video, document, GIF, "
            "audio, voice or sticker.",
        )
    mids = c.pop("_mids", None) or [m.message_id]
    async with user_lock(m.from_user.id):
        d = await state.get_data()
        if d.get("phase") == "preview":
            await coll_drop_previews(m.bot, m.chat.id, state)
            d = await state.get_data()
        items = list(d.get("items", []))
        items.append({"mid": min(mids), "c": c, "orig": mids, "pv": []})
        items.sort(key=lambda x: x["mid"])
        await state.update_data(items=items)
        await coll_send(m, state, pfx)


async def coll_preview(cb, state, pfx):
    """Done: show every message as it will be posted, each with its own 🗑 Remove button."""
    async with user_lock(cb.from_user.id):
        d = await state.get_data()
        items = sorted(d.get("items", []), key=lambda x: x["mid"])
        if not items:
            return await safe_answer(cb, "Send at least one message first.", True)
        await safe_answer(cb)
        bot, chat_id = cb.bot, cb.message.chat.id
        gone = [cb.message.message_id]
        for it in items:
            gone += it.get("orig", [])
        await safe_delete(bot, chat_id, gone)  # the originals are replaced by the previews
        new_ids = []
        for it in items:
            pv = []
            mk = kb([[("🗑 Remove", f"pv_rm:{it['mid']}")]])
            try:
                if it["c"]["type"] == "album":
                    res = await send_content(bot, chat_id, it["c"], False, True)
                    pv += [x.message_id for x in res]
                    note = await bot.send_message(chat_id, bold("⬆️ Album"), reply_markup=mk)
                    pv.append(note.message_id)
                else:
                    res = await send_content(bot, chat_id, it["c"], False, True, markup=mk)
                    pv.append(res.message_id)
            except Exception as e:
                log.warning("Preview failed: %s", e)
                note = await bot.send_message(chat_id, bold(esc(plain(it["c"], 80))), reply_markup=mk)
                pv.append(note.message_id)
            it["pv"] = pv
            new_ids += pv
        ctrl = await bot.send_message(
            chat_id, bold(prev_text(len(items))), reply_markup=prev_kb(pfx, d["coll_back"]), link_preview_options=NOPREV
        )
        trash_add(chat_id, *new_ids, ctrl.message_id)
        await state.update_data(items=items, ctrl_id=ctrl.message_id, phase="preview")


async def coll_more(cb, state, pfx):
    await coll_drop_previews(cb.bot, cb.message.chat.id, state, keep=cb.message.message_id)
    await coll_edit(cb, state, pfx)


async def coll_go(cb, state):
    """Continue: returns the messages in order (previews are removed from the chat)."""
    async with user_lock(cb.from_user.id):
        d = await state.get_data()
        items = sorted(d.get("items", []), key=lambda x: x["mid"])
        if not items:
            await safe_answer(cb, "Send at least one message first.", True)
            return None
        ids = []
        for it in items:
            ids += it.get("pv", [])
            it["pv"] = []
        await safe_delete(cb.bot, cb.message.chat.id, ids)
        contents = [i["c"] for i in items]
        await state.update_data(items=items, contents=contents, phase="done", ctrl_id=cb.message.message_id)
        return contents


@router.callback_query(F.data.startswith("pv_rm:"))
async def pv_remove(cb: CallbackQuery, state: FSMContext):
    mid = int(cb.data.split(":")[1])
    bot, chat_id = cb.bot, cb.message.chat.id
    async with user_lock(cb.from_user.id):
        d = await state.get_data()
        items = list(d.get("items", []))
        it = next((x for x in items if x["mid"] == mid), None)
        if d.get("phase") != "preview" or it is None:
            await safe_answer(cb, "This preview is no longer active.", True)
            await safe_delete(bot, chat_id, [cb.message.message_id])
            return
        items.remove(it)
        await safe_delete(bot, chat_id, it.get("pv", []))
        await state.update_data(items=items)
        await safe_answer(cb, "Removed.")
        if not items:
            await safe_delete(bot, chat_id, [d.get("ctrl_id")])
            m2 = await reply(cb.message, d["coll_prompt"], kb([[("⬅️ Back", d["coll_back"])]]))
            await state.update_data(phase="collect", ctrl_id=m2.message_id)
            return
        try:
            await bot.edit_message_text(
                text=bold(prev_text(len(items))),
                chat_id=chat_id,
                message_id=d["ctrl_id"],
                reply_markup=prev_kb(d.get("pfx", "np"), d["coll_back"]),
            )
        except Exception:
            pass


# ---- New Post: messages
@router.message(NewPost.content)
async def np_content(m: Message, state: FSMContext):
    await coll_add(m, state, "np")


@router.callback_query(NewPost.content, F.data == "np_done")
async def np_done(cb: CallbackQuery, state: FSMContext):
    await coll_preview(cb, state, "np")


@router.callback_query(NewPost.content, F.data == "np_more")
async def np_more(cb: CallbackQuery, state: FSMContext):
    await coll_more(cb, state, "np")


@router.callback_query(NewPost.content, F.data == "np_go")
async def np_go(cb: CallbackQuery, state: FSMContext):
    contents = await coll_go(cb, state)
    if not contents:
        return
    if any(has_link(c) for c in contents):
        await np_ask_preview(cb, state)
    else:
        await ask_when(cb, state)


async def np_ask_preview(ev, state):
    d = await state.get_data()
    await state.set_state(NewPost.preview)
    text = (
        "🔗 This post contains a link.\nDo you want to show the link preview?"
        if len(d["contents"]) == 1
        else "🔗 Some messages contain links.\nDo you want to show the link previews?"
    )
    await show(
        ev,
        text,
        kb(
            [
                [("✅ Preview ON", "np_pre:1"), ("🚫 Preview OFF", "np_pre:0")],
                [("⬅️ Back", "np_bk_c"), ("❌ Cancel", "menu")],
            ]
        ),
    )


@router.callback_query(NewPost.preview, F.data == "np_bk_c")
async def np_back_collect(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewPost.content)
    await coll_edit(cb, state, "np")


@router.callback_query(NewPost.preview, F.data.startswith("np_pre:"))
async def np_preview(cb: CallbackQuery, state: FSMContext):
    await state.update_data(preview=int(cb.data.split(":")[1]))
    await ask_when(cb, state)


# ---- New Post: time
async def ask_when(ev, state):
    await state.set_state(NewPost.when)
    await show(
        ev,
        when_text(),
        kb(
            [
                [("⚡ Send Now", "np_now"), ("🔁 Repeat", "np_rep")],
                [("⬅️ Back", "np_bk_w"), ("❌ Cancel", "menu")],
            ]
        ),
    )


@router.callback_query(NewPost.when, F.data == "np_bk_w")
async def np_back_when(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if any(has_link(c) for c in d["contents"]):
        await np_ask_preview(cb, state)
    else:
        await state.set_state(NewPost.content)
        await coll_edit(cb, state, "np")


async def np_set_time(ev, state, dt, send_now=False):
    ts = int(dt.timestamp())
    if ts < time.time() - 60:
        return await show(ev, "That time is in the past. Send a future time.")
    d = await state.get_data()
    n = len(d["contents"])
    await state.update_data(
        send_at=max(ts, int(time.time())), send_now=send_now, delete_afters=[None] * n, pins=[0] * n
    )
    await np_ask_msg(ev, state, 0)


@router.callback_query(NewPost.when, F.data == "np_now")
async def np_now(cb: CallbackQuery, state: FSMContext):
    await np_set_time(cb, state, datetime.now(TZ), True)


@router.message(NewPost.when)
async def np_when(m: Message, state: FSMContext):
    dt = parse_when(m.text or "")
    if not dt:
        return await reply(m, "Invalid time. Examples: <code>18:30</code>, <code>25-12-2026 18:30</code>, <code>2h</code>")
    await np_set_time(m, state, dt)


# ---- New Post: auto-delete + pin, one message at a time
def msg_head(c, idx, n):
    if n <= 1:
        return ""
    return f"<b>Message {idx + 1} of {n}</b>\n📝 {esc(plain(c, 50))}\n\n"


def pin_btn(cb_data, on):
    return ("📌 Pin: ON" if on else "📌 Pin: OFF", cb_data)


async def np_ask_msg(ev, state, idx):
    d = await state.get_data()
    await state.update_data(idx=idx)
    await state.set_state(NewPost.delete)
    rows = del_rows("np_del:") + [
        [pin_btn("np_pin", d["pins"][idx])],
        [("⬅️ Back", "np_bk_d"), ("❌ Cancel", "menu")],
    ]
    text = msg_head(d["contents"][idx], idx, len(d["contents"])) + del_text()
    if d.get("send_now") and isinstance(ev, CallbackQuery):
        await show_photo(ev, DELETE_IMAGE, text, kb(rows))
    else:
        await show(ev, text, kb(rows))


@router.callback_query(NewPost.delete, F.data == "np_pin")
async def np_toggle_pin(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pins, i = list(d["pins"]), d["idx"]
    pins[i] = 0 if pins[i] else 1
    await state.update_data(pins=pins)
    await np_ask_msg(cb, state, i)


@router.callback_query(NewPost.delete, F.data == "np_bk_d")
async def np_back_delete(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if d["idx"] > 0:
        await np_ask_msg(cb, state, d["idx"] - 1)
    else:
        await ask_when(cb, state)


async def np_set_delete(ev, state, sec):
    d = await state.get_data()
    das, i = list(d["delete_afters"]), d["idx"]
    das[i] = sec or None
    await state.update_data(delete_afters=das)
    if i + 1 < len(d["contents"]):
        await np_ask_msg(ev, state, i + 1)
    else:
        await ask_confirm(ev, state)


@router.callback_query(NewPost.delete, F.data.startswith("np_del:"))
async def np_del_cb(cb: CallbackQuery, state: FSMContext):
    await np_set_delete(cb, state, int(cb.data.split(":")[1]))


@router.message(NewPost.delete)
async def np_del_msg(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if not sec:
        return await reply(m, "Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    await np_set_delete(m, state, sec)


# ---- New Post: review + create
async def ask_confirm(ev, state):
    d = await state.get_data()
    names = {c["chat_id"]: c["title"] for c in await db.channels()}
    titles = [names[c] for c in d["chat_ids"] if c in names]
    if not titles:
        await state.clear()
        return await show(ev, "That channel no longer exists.", kb([[("⬅️ Menu", "menu")]]))
    await state.set_state(NewPost.confirm)
    contents, das, pins = d["contents"], d["delete_afters"], d["pins"]
    ch_line = (
        f"📺 Channel: {esc(titles[0])}"
        if len(titles) == 1
        else f"📺 Channels ({len(titles)}): {esc(', '.join(titles))}"
    )
    if len(contents) == 1:
        L = ["<b>Review your post</b>", "", ch_line, f"📝 Content: {esc(plain(contents[0], 120))}"]
    else:
        L = ["<b>Review your posts</b>", "", ch_line, f"📝 Messages: {len(contents)} (sent in this order)"]
        for i, c in enumerate(contents[:5]):
            L.append(f"{i + 1}. {esc(plain(c, 40))} · 🗑 {da_short(das[i])}" + (" · 📌" if pins[i] else ""))
        if len(contents) > 5:
            L.append(f"…and {len(contents) - 5} more")
    if any(has_link(c) for c in contents):
        L.append("🔗 Link preview: " + ("ON" if d["preview"] else "OFF"))
    L.append("🕒 Posting: " + ("Immediately" if d["send_at"] <= time.time() + 5 else fmt_dt(d["send_at"])))
    if len(contents) == 1:
        L.append("🗑 Auto-delete: " + (f"{fmt_dur(das[0])} after posting" if das[0] else "Off"))
        L.append("📌 Pin: " + ("ON" if pins[0] else "OFF"))
    L.append("🔔 Notification: " + ("Silent" if d["silent"] else "Normal"))
    sil = "🔕 Silent: ON" if d["silent"] else "🔔 Silent: OFF"
    await show(
        ev,
        "\n".join(L),
        kb([[(sil, "np_sil")], [("⬅️ Back", "np_bk_cf"), ("✅ Schedule", "np_ok"), ("❌ Cancel", "menu")]]),
    )


@router.callback_query(NewPost.confirm, F.data == "np_bk_cf")
async def np_back_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await np_ask_msg(cb, state, len(d["contents"]) - 1)


@router.callback_query(NewPost.confirm, F.data == "np_sil")
async def np_silent(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(silent=0 if d["silent"] else 1)
    await ask_confirm(cb, state)


@router.callback_query(NewPost.confirm, F.data == "np_ok")
async def np_ok(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    names = {c["chat_id"] for c in await db.channels()}
    cids = [c for c in d["chat_ids"] if c in names]
    ids = []
    for cid in cids:
        for i, c in enumerate(d["contents"]):
            ids.append(
                await db.create_post(
                    owner_id=cb.from_user.id,
                    chat_id=cid,
                    content=c,
                    link_preview=d["preview"],
                    silent=d["silent"],
                    send_at=d["send_at"],
                    delete_after=d["delete_afters"][i],
                    pin=d["pins"][i],
                )
            )
    trash_forget(cb.message.chat.id)
    await state.clear()
    when = "right now" if d["send_at"] <= time.time() + 5 else f"at {fmt_dt(d['send_at'])}"
    if len(ids) == 1:
        text = f"✅ Post <b>#{ids[0]}</b> scheduled. It will be published {when}."
    elif len(cids) == 1:
        text = f"✅ <b>{len(ids)} posts</b> scheduled (#{ids[0]} to #{ids[-1]}). They will be published in order {when}."
    else:
        text = (
            f"✅ <b>{len(ids)} posts</b> scheduled in <b>{len(cids)} channels</b> "
            f"(#{ids[0]} to #{ids[-1]}). They will be published {when}."
        )
    await show(
        cb,
        text,
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
    rows = [[(f"All Channels ({sum(counts.values())})", "list:0:0")]]
    rows += [[(f"{c['title']} ({counts.get(c['chat_id'], 0)})", f"list:0:{c['chat_id']}")] for c in chans]
    rows.append([("⬅️ Back", "menu")])
    await show(
        ev,
        "🗓 <b>Scheduled Posts !!</b>\n\nSelect A Channel (Number = Pending Posts):",
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
        mark = (" 🗑" if p["delete_at"] else "") + (" 📌" if p.get("pinned") else "")
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
    if st in EDITABLE:
        L.append("📌 Pin: " + ("ON" if p.get("pin") else "OFF"))
    elif st == "sent":
        L.append("📌 Pinned: " + ("Yes" if p.get("pinned") else "No"))

    back = ("⬅️ Back", back_cb(p))
    if st in EDITABLE:
        tog = []
        if has_link(c):
            tog.append((f"🔗 Preview: {'ON' if p['link_preview'] else 'OFF'}", f"pt_pre:{pid}"))
        tog.append(("🔕 Silent: ON" if p["silent"] else "🔔 Silent: OFF", f"pt_sil:{pid}"))
        tog.append(pin_btn(f"pt_pin:{pid}", p.get("pin")))
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
        rows.append([("📍 Unpin" if p.get("pinned") else "📌 Pin Now", f"pp:{pid}")])
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


@router.callback_query(F.data.startswith("pt_pin:"))
async def cb_toggle_pin(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    p = await editable(cb, pid)
    if p:
        await db.set(pid, pin=0 if p.get("pin") else 1)
        await show_post(cb, pid)


@router.callback_query(F.data.startswith("pp:"))
async def cb_pin_now(cb: CallbackQuery, bot: Bot):
    """Pin / Unpin a post that is live in the channel."""
    pid = int(cb.data.split(":")[1])
    p = await db.post(pid)
    if not p or p["status"] != "sent":
        return await safe_answer(cb, "This post is not live.", True)
    was = bool(p.get("pinned"))
    try:
        if was:
            await bot.unpin_chat_message(chat_id=p["chat_id"], message_id=p["message_id"])
        else:
            await bot.pin_chat_message(chat_id=p["chat_id"], message_id=p["message_id"], disable_notification=True)
        await db.set(pid, pinned=not was)
        await safe_answer(cb, "Unpinned." if was else "Pinned.")
    except Exception as e:
        await safe_answer(cb, f"Could not {'unpin' if was else 'pin'}: {str(e)[:150]}", True)
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
        await delete_live(bot, p)
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
    c = await get_content(m)
    if c is SKIP:
        return
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
            reply_markup=kb([[("✅ Preview ON", f"ep_pre:{pid}:1"), ("🚫 Preview OFF", f"ep_pre:{pid}:0")], [("⬅️ Back", f"p:{pid}")]]),
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
            reply_markup=kb([[("✅ Preview ON", f"ep_pre:{pid}:1"), ("🚫 Preview OFF", f"ep_pre:{pid}:0")], [("⬅️ Back", f"p:{pid}")]]),
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


def rule_contents(r):
    return r.get("contents") or [r["content"]]


def rule_has_link(r):
    return any(has_link(c) for c in rule_contents(r))


def rule_title(r, n=14):
    cs = rule_contents(r)
    return plain(cs[0], n) + (f" (+{len(cs) - 1})" if len(cs) > 1 else "")


def rule_da(r, i):
    das = r.get("delete_afters")
    if das and i < len(das):
        return das[i]
    return r.get("delete_after")


def rule_pin(r, i):
    ps = r.get("pins")
    return ps[i] if ps and i < len(ps) else 0


def da_short(sec):
    return fmt_dur(sec) if sec else "Off"


def rule_lines(d, title, label="Channel"):
    cs = rule_contents(d)
    L = [f"📺 {label}: {esc(title)}"]
    if len(cs) == 1:
        L.append(f"📝 Content: {esc(plain(cs[0], 120))}")
    else:
        L.append(f"📝 Messages: {len(cs)} (sent in this order)")
        for i, c in enumerate(cs[:5]):
            L.append(f"{i + 1}. {esc(plain(c, 40))} · 🗑 {da_short(rule_da(d, i))}" + (" · 📌" if rule_pin(d, i) else ""))
        if len(cs) > 5:
            L.append(f"…and {len(cs) - 5} more")
    L += [
        f"📅 Days: {days_label(d['days'])}",
        f"🕒 Times: {', '.join(t12(t) for t in d['times'])}",
        f"🏁 Ends: {end_label(d)}",
    ]
    if len(cs) == 1:
        da = rule_da(d, 0)
        L.append("🗑 Auto-delete: " + (f"{fmt_dur(da)} after posting" if da else "Off"))
        L.append("📌 Pin: " + ("ON" if rule_pin(d, 0) else "OFF"))
    L.append("♻️ Replace previous: " + ("ON" if d.get("replace_prev") else "OFF"))
    if rule_has_link(d):
        L.append("🔗 Link preview: " + ("ON" if d.get("link_preview", d.get("preview", 0)) else "OFF"))
    L.append("🔔 Notification: " + ("Silent" if d.get("silent") else "Normal"))
    return L


def day_kb(sel, P, *bottom):
    def lab(i):
        return ("✅ " if i in sel else "⬜ ") + DAY_NAMES[i]

    return kb(
        [
            [(lab(i), f"{P}:t:{i}") for i in range(0, 4)],
            [(lab(i), f"{P}:t:{i}") for i in range(4, 7)],
            [("📆 Every day", f"{P}:all"), ("💼 Weekdays", f"{P}:wk"), ("🏖 Weekend", f"{P}:we")],
            list(bottom),
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
def fresh_rule_fields():
    return {
        "days": [],
        "times": [],
        "end_type": "never",
        "end_count": None,
        "end_date": None,
        "replace_prev": 0,
        "origin": None,
    }


async def rep_chooser(ev, state):
    msg = ev if isinstance(ev, Message) else ev.message
    trash_forget(msg.chat.id)
    await state.clear()
    chans = await db.channels()
    if not chans:
        return await show(ev, NO_CHANNELS, kb([[("➕ Add Channel", "addch")], [("⬅️ Back", "menu")]]))
    await state.set_data({"sel": [], "silent": 0, "preview": 0})
    await state.set_state(RepeatNew.channels)
    await channel_pick_screen(ev, state, "🔁 <b>Repeat Post !!</b>", "rpm")


@router.callback_query(F.data == "rep")
async def cb_rep(cb: CallbackQuery, state: FSMContext):
    await rep_chooser(cb, state)


@router.callback_query(RepeatNew.channels, F.data.startswith("rpm:"))
async def rp_pick_channels(cb: CallbackQuery, state: FSMContext):
    chosen = await pick_channels(cb, state, "rpm")
    if chosen:
        return await rp_start_collect(cb, state)
    await channel_pick_screen(cb, state, "🔁 <b>Repeat Post !!</b>", "rpm")


async def rp_start_collect(ev, state):
    d = await state.get_data()
    sel = d["sel"]
    await state.update_data(
        chat_ids=sel,
        chat_id=sel[0],
        pfx="rp",
        coll_back="rp_bk_ch",
        coll_prompt=REPEAT_PROMPT,
        items=d.get("items", []),
        ctrl_id=None,
        phase="collect",
        **fresh_rule_fields(),
    )
    await state.set_state(RepeatNew.content)
    if d.get("items"):
        await coll_edit(ev, state, "rp")
    else:
        await show(ev, REPEAT_PROMPT, kb([[("⬅️ Back", "rp_bk_ch")]]))


@router.callback_query(RepeatNew.content, F.data == "rp_bk_ch")
async def rp_back_channels(cb: CallbackQuery, state: FSMContext):
    await coll_drop_previews(cb.bot, cb.message.chat.id, state, keep=cb.message.message_id)
    await state.set_state(RepeatNew.channels)
    await channel_pick_screen(cb, state, "🔁 <b>Repeat Post !!</b>", "rpm")


@router.callback_query(NewPost.when, F.data == "np_rep")
async def np_to_repeat(cb: CallbackQuery, state: FSMContext):
    """Repeat button on the time screen: keep everything chosen so far, Back returns to the time screen."""
    fields = fresh_rule_fields()
    fields["origin"] = "np"
    await state.update_data(**fields)
    await rep_ask_days(cb, state)


# ---- messages
@router.message(RepeatNew.content)
async def rp_content(m: Message, state: FSMContext):
    await coll_add(m, state, "rp")


@router.callback_query(RepeatNew.content, F.data == "rp_done")
async def rp_done(cb: CallbackQuery, state: FSMContext):
    await coll_preview(cb, state, "rp")


@router.callback_query(RepeatNew.content, F.data == "rp_more")
async def rp_more(cb: CallbackQuery, state: FSMContext):
    await coll_more(cb, state, "rp")


@router.callback_query(RepeatNew.content, F.data == "rp_go")
async def rp_go(cb: CallbackQuery, state: FSMContext):
    contents = await coll_go(cb, state)
    if not contents:
        return
    if any(has_link(c) for c in contents):
        await rp_ask_preview(cb, state)
    else:
        await rep_ask_days(cb, state)


async def rp_ask_preview(ev, state):
    d = await state.get_data()
    await state.set_state(RepeatNew.preview)
    text = (
        "🔗 This post contains a link.\nDo you want to show the link preview?"
        if len(d["contents"]) == 1
        else "🔗 Some messages contain links.\nDo you want to show the link previews?"
    )
    await show(
        ev,
        text,
        kb(
            [
                [("✅ Preview ON", "rp_pre:1"), ("🚫 Preview OFF", "rp_pre:0")],
                [("⬅️ Back", "rp_bk_c"), ("❌ Cancel", "menu")],
            ]
        ),
    )


@router.callback_query(RepeatNew.preview, F.data == "rp_bk_c")
async def rp_back_collect(cb: CallbackQuery, state: FSMContext):
    await state.set_state(RepeatNew.content)
    await coll_edit(cb, state, "rp")


@router.callback_query(RepeatNew.preview, F.data.startswith("rp_pre:"))
async def rp_preview(cb: CallbackQuery, state: FSMContext):
    await state.update_data(preview=int(cb.data.split(":")[1]))
    await rep_ask_days(cb, state)


# ---- days
async def rep_ask_days(ev, state):
    d = await state.get_data()
    await state.set_state(RepeatNew.days)
    await show(
        ev,
        "📅 <b>Which days?</b>\n\nTap the days to select or unselect them.",
        day_kb(d["days"], "rpd", ("➡️ Next", "rpd:ok"), ("⬅️ Back", "rpd_bk"), ("❌ Cancel", "menu")),
    )


@router.callback_query(RepeatNew.days, F.data == "rpd_bk")
async def rp_days_back(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if d.get("origin") == "np":
        return await ask_when(cb, state)  # back to the Send Now / Repeat screen
    if any(has_link(c) for c in d["contents"]):
        await rp_ask_preview(cb, state)
    else:
        await state.set_state(RepeatNew.content)
        await coll_edit(cb, state, "rp")


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


# ---- times
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


# ---- end
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
        return await rep_ask_start(cb, state)
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
    await rep_ask_start(m, state)


@router.callback_query(RepeatNew.end_date, F.data.startswith("rpc:"))
async def rp_calendar(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    if parts[1] == "m":
        return await show_calendar(cb, int(parts[2]), int(parts[3]))
    s = parts[2]
    await state.update_data(end_type="date", end_date=f"{s[:4]}-{s[4:6]}-{s[6:]}", end_count=None)
    await rep_ask_start(cb, state)


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
    await rep_ask_start(m, state)


# ---- auto-delete + pin, one message at a time
async def rep_ask_start(ev, state):
    d = await state.get_data()
    n = len(d["contents"])
    await state.update_data(delete_afters=[None] * n, pins=[0] * n)
    await rep_ask_msg(ev, state, 0)


async def rep_ask_msg(ev, state, idx):
    d = await state.get_data()
    await state.update_data(idx=idx)
    await state.set_state(RepeatNew.delete)
    rows = del_rows("rp_del:")
    rows.append([pin_btn("rp_pin", d["pins"][idx])])
    rows.append([("♻️ Replace previous: ON" if d["replace_prev"] else "♻️ Replace previous: OFF", "rp_rep")])
    rows.append([("⬅️ Back", "rpx_back"), ("❌ Cancel", "menu")])
    await show(
        ev,
        msg_head(d["contents"][idx], idx, len(d["contents"]))
        + del_text()
        + "\n\n♻️ Replace previous: when ON, the previous post of this repeat is deleted "
        "as soon as the new one is posted.",
        kb(rows),
    )


@router.callback_query(RepeatNew.delete, F.data == "rpx_back")
async def rp_del_back(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if d["idx"] > 0:
        await rep_ask_msg(cb, state, d["idx"] - 1)
    else:
        await rep_ask_end(cb, state)


@router.callback_query(RepeatNew.delete, F.data == "rp_rep")
async def rp_toggle_replace(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(replace_prev=0 if d["replace_prev"] else 1)
    await rep_ask_msg(cb, state, d["idx"])


@router.callback_query(RepeatNew.delete, F.data == "rp_pin")
async def rp_toggle_pin(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pins, i = list(d["pins"]), d["idx"]
    pins[i] = 0 if pins[i] else 1
    await state.update_data(pins=pins)
    await rep_ask_msg(cb, state, i)


async def rp_set_delete(ev, state, sec):
    d = await state.get_data()
    das, i = list(d["delete_afters"]), d["idx"]
    das[i] = sec or None
    await state.update_data(delete_afters=das)
    if i + 1 < len(d["contents"]):
        await rep_ask_msg(ev, state, i + 1)
    else:
        await rep_ask_confirm(ev, state)


@router.callback_query(RepeatNew.delete, F.data.startswith("rp_del:"))
async def rp_del_cb(cb: CallbackQuery, state: FSMContext):
    await rp_set_delete(cb, state, int(cb.data.split(":")[1]))


@router.message(RepeatNew.delete)
async def rp_del_msg(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if not sec:
        return await reply(m, "Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    await rp_set_delete(m, state, sec)


# ---- review + create
async def rep_ask_confirm(ev, state):
    d = await state.get_data()
    names = {c["chat_id"]: c["title"] for c in await db.channels()}
    titles = [names[c] for c in d["chat_ids"] if c in names]
    if not titles:
        await state.clear()
        return await show(ev, "That channel no longer exists.", kb([[("⬅️ Menu", "menu")]]))
    await state.set_state(RepeatNew.confirm)
    nxt = occurrences(d["days"], d["times"], int(time.time()), TZ, end_date_of(d), 3)
    label = "Channel" if len(titles) == 1 else f"Channels ({len(titles)})"
    L = ["<b>Review your repeat post</b>", ""] + rule_lines(d, ", ".join(titles), label) + ["", "⏭ Next posts:"]
    L += [f"• {fmt_dt(t)}" for t in nxt] or ["• none (check days, times and end date)"]
    sil = "🔕 Silent: ON" if d["silent"] else "🔔 Silent: OFF"
    await show(
        ev,
        "\n".join(L),
        kb([[(sil, "rp_sil")], [("⬅️ Back", "rpx_bk_cf"), ("✅ Create", "rp_ok"), ("❌ Cancel", "menu")]]),
    )


@router.callback_query(RepeatNew.confirm, F.data == "rp_sil")
async def rp_silent(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(silent=0 if d["silent"] else 1)
    await rep_ask_confirm(cb, state)


@router.callback_query(RepeatNew.confirm, F.data == "rpx_bk_cf")
async def rp_back_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await rep_ask_msg(cb, state, len(d["contents"]) - 1)


@router.callback_query(RepeatNew.confirm, F.data == "rp_ok")
async def rp_ok(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    nxt = occurrences(d["days"], d["times"], int(time.time()), TZ, end_date_of(d), 1)
    if not nxt:
        return await safe_answer(cb, "No upcoming post matches these settings. Change the days, times or end date.", True)
    known = {c["chat_id"] for c in await db.channels()}
    cids = [c for c in d["chat_ids"] if c in known]
    rids = []
    for cid in cids:
        rids.append(
            await db.create_repeat(
                owner_id=cb.from_user.id,
                chat_id=cid,
                content=d["contents"][0],
                contents=d["contents"],
                link_preview=d["preview"],
                silent=d["silent"],
                days=d["days"],
                times=d["times"],
                end_type=d["end_type"],
                end_count=d["end_count"],
                end_date=d["end_date"],
                delete_after=d["delete_afters"][0],
                delete_afters=d["delete_afters"],
                pins=d["pins"],
                replace_prev=d.get("replace_prev", 0),
                next_run=nxt[0],
            )
        )
    trash_forget(cb.message.chat.id)
    await state.clear()
    if len(rids) == 1:
        text = f"✅ Repeat <b>R{rids[0]}</b> created.\n\n⏭ First post: {fmt_dt(nxt[0])}"
    else:
        text = f"✅ Repeats <b>R{rids[0]} to R{rids[-1]}</b> created in {len(rids)} channels.\n\n⏭ First post: {fmt_dt(nxt[0])}"
    await show(
        cb,
        text,
        kb([[("🔁 Repeating", f"rlist:0:{cids[0] if len(cids) == 1 else 0}"), ("⬅️ Menu", "menu")]]),
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
        label += rule_title(r)
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
        if rule_has_link(r):
            tog.append((f"🔗 Preview: {'ON' if r['link_preview'] else 'OFF'}", f"rp:lnk:{rid}"))
        tog.append(("🔕 Silent: ON" if r["silent"] else "🔔 Silent: OFF", f"rp:sil:{rid}"))
        rows = [
            [("▶️ Resume" if r["paused"] else "⏸ Pause", f"rp:pause:{rid}"), ("✏️ Edit", f"rp:edit:{rid}")],
            [("📅 Days", f"rp:days:{rid}"), ("🕒 Times", f"rp:times:{rid}")],
            [("🗑 Delete & 📌 Pin", f"rp:del:{rid}"), (f"♻️ Replace: {'ON' if r['replace_prev'] else 'OFF'}", f"rp:rep:{rid}")],
            [("⏭ Skip Next", f"rp:skip:{rid}"), ("⚡ Send Now", f"rp:now:{rid}")],
            tog,
            [("👁 View", f"rp:view:{rid}"), ("❌ Delete", f"rp:delask:{rid}")],
            [back],
        ]
    await show(ev, "\n".join(L), kb(rows))


@router.callback_query(F.data.startswith("r:"))
async def cb_repeat(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if d.get("phase") == "preview":
        await coll_drop_previews(cb.bot, cb.message.chat.id, state, keep=cb.message.message_id)
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
            for c in rule_contents(r):
                await send_content(bot, cb.from_user.id, c, r["link_preview"], 1)
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
        for i, c in enumerate(rule_contents(r)):
            await db.create_post(
                owner_id=cb.from_user.id,
                chat_id=r["chat_id"],
                content=c,
                link_preview=r["link_preview"],
                silent=r["silent"],
                send_at=now,
                delete_after=rule_da(r, i),
                pin=rule_pin(r, i),
                repeat_id=rid,
                replace_prev=r["replace_prev"],
                run_id=now,
            )
        await safe_answer(cb, "Sending…")
    elif act == "rep":
        await db.set_repeat(rid, replace_prev=0 if r["replace_prev"] else 1)
    elif act == "lnk":
        await db.set_repeat(rid, link_preview=0 if r["link_preview"] else 1)
    elif act == "sil":
        await db.set_repeat(rid, silent=0 if r["silent"] else 1)
    elif act == "edit":
        trash_forget(cb.message.chat.id)
        prompt = f"✏️ <b>Edit repeat R{rid}</b>\n\n{PROMPT_MULTI}\n\nThe new messages replace the old ones."
        await state.set_state(RepeatEdit.content)
        await state.set_data(
            {
                "rid": rid,
                "items": [],
                "ctrl_id": None,
                "pfx": "re",
                "phase": "collect",
                "coll_back": f"r:{rid}",
                "coll_prompt": prompt,
            }
        )
        return await show(cb, prompt, kb([[("⬅️ Back", f"r:{rid}")]]))
    elif act == "days":
        await state.set_state(RepeatEdit.days)
        await state.set_data({"rid": rid, "days": r["days"]})
        return await show_edit_days(cb, rid, r["days"])
    elif act == "times":
        await state.set_state(RepeatEdit.times)
        await state.set_data({"rid": rid})
        return await show(cb, times_text(r["times"]), kb([[("⬅️ Back", f"r:{rid}")]]))
    elif act == "del":
        n = len(rule_contents(r))
        await state.set_state(RepeatEdit.delete)
        await state.set_data(
            {
                "rid": rid,
                "idx": 0,
                "das": [rule_da(r, i) for i in range(n)],
                "pins": [rule_pin(r, i) for i in range(n)],
            }
        )
        return await re_ask_msg(cb, state)
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
    await coll_add(m, state, "re")


@router.callback_query(RepeatEdit.content, F.data == "re_done")
async def re_done(cb: CallbackQuery, state: FSMContext):
    await coll_preview(cb, state, "re")


@router.callback_query(RepeatEdit.content, F.data == "re_more")
async def re_more(cb: CallbackQuery, state: FSMContext):
    await coll_more(cb, state, "re")


@router.callback_query(RepeatEdit.content, F.data == "re_go")
async def re_go(cb: CallbackQuery, state: FSMContext):
    contents = await coll_go(cb, state)
    if not contents:
        return
    rid = (await state.get_data())["rid"]
    old = await db.repeat(rid)
    n_old = len(rule_contents(old)) if old else 0
    das = [rule_da(old, i) if i < n_old else None for i in range(len(contents))]
    pins = [rule_pin(old, i) if i < n_old else 0 for i in range(len(contents))]
    await db.set_repeat(rid, content=contents[0], contents=contents, link_preview=0, delete_afters=das, pins=pins)
    await state.clear()
    if any(has_link(c) for c in contents):
        return await show(
            cb,
            "✅ Messages updated.\n🔗 Some messages contain links. Show the link previews?",
            kb(
                [
                    [("✅ Preview ON", f"rep_pre:{rid}:1"), ("🚫 Preview OFF", f"rep_pre:{rid}:0")],
                    [("⬅️ Back", f"r:{rid}")],
                ]
            ),
        )
    await show_repeat(cb, rid)


@router.callback_query(F.data.startswith("rep_pre:"))
async def re_preview(cb: CallbackQuery):
    _, rid, val = cb.data.split(":")
    await db.set_repeat(int(rid), link_preview=int(val))
    await show_repeat(cb, int(rid))


async def re_ask_msg(ev, state):
    d = await state.get_data()
    r = await db.repeat(d["rid"])
    if not r:
        await state.clear()
        return await show(ev, "Repeat not found.", kb([[("⬅️ Back", "sch")]]))
    cs, idx = rule_contents(r), d["idx"]
    rows = del_rows("rd:") + [[pin_btn("rd_pin", d["pins"][idx])], [("⬅️ Back", "rd_bk")]]
    await show(ev, msg_head(cs[idx], idx, len(cs)) + del_text(), kb(rows))


@router.callback_query(RepeatEdit.delete, F.data == "rd_pin")
async def rd_toggle_pin(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pins = list(d["pins"])
    pins[d["idx"]] = 0 if pins[d["idx"]] else 1
    await state.update_data(pins=pins)
    await re_ask_msg(cb, state)


@router.callback_query(RepeatEdit.delete, F.data == "rd_bk")
async def rd_back(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if d["idx"] > 0:
        await state.update_data(idx=d["idx"] - 1)
        return await re_ask_msg(cb, state)
    await state.clear()
    await show_repeat(cb, d["rid"])


async def re_set_delete(ev, state, sec):
    d = await state.get_data()
    das, i = list(d["das"]), d["idx"]
    das[i] = sec or None
    await state.update_data(das=das)
    if i + 1 < len(das):
        await state.update_data(idx=i + 1)
        return await re_ask_msg(ev, state)
    await db.set_repeat(d["rid"], delete_afters=das, pins=d["pins"], delete_after=das[0])
    await state.clear()
    await show_repeat(ev, d["rid"])


@router.callback_query(RepeatEdit.delete, F.data.startswith("rd:"))
async def rd_cb(cb: CallbackQuery, state: FSMContext):
    await re_set_delete(cb, state, int(cb.data.split(":")[2]))


@router.message(RepeatEdit.delete)
async def re_del_msg(m: Message, state: FSMContext):
    sec = parse_duration(m.text or "")
    if not sec:
        return await reply(m, "Invalid duration. Examples: <code>45m</code>, <code>3h</code>, <code>2d 12h</code>")
    await re_set_delete(m, state, sec)


# ----------------------------------------------------------------- repeat: firing
async def fire_repeat(bot: Bot, r):
    """Called by the scheduler when a repeat rule is due. Creates one normal post per message."""
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
        for i, c in enumerate(rule_contents(r)):
            await db.create_post(
                owner_id=r["owner_id"],
                chat_id=r["chat_id"],
                content=c,
                link_preview=r["link_preview"],
                silent=r["silent"],
                send_at=now,
                delete_after=rule_da(r, i),
                pin=rule_pin(r, i),
                repeat_id=r["id"],
                replace_prev=r["replace_prev"],
                run_id=now,
            )
    else:
        log.warning("Repeat %s skipped a run that was %ss late", r["id"], late)
    if ended:
        await notify(bot, r["owner_id"], f"🏁 Repeat R{r['id']} has finished. No more posts will be created.")


# ---- fallbacks (must stay last)
@router.message()
async def fallback_msg(m: Message):
    if m.media_group_id:
        if m.media_group_id in SEEN_ALBUMS:
            return
        SEEN_ALBUMS.add(m.media_group_id)
        if len(SEEN_ALBUMS) > 500:
            SEEN_ALBUMS.clear()
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
    msgs = msg if isinstance(msg, list) else [msg]
    await db.set(
        p["id"],
        status="sent",
        message_id=msgs[0].message_id,
        message_ids=[x.message_id for x in msgs],
        sent_at=now,
        delete_at=da,
        error=None,
    )
    pin_note = ""
    if p.get("pin"):
        try:
            await bot.pin_chat_message(chat_id=p["chat_id"], message_id=msgs[0].message_id, disable_notification=True)
            await db.set(p["id"], pinned=True)
            pin_note = "\n📌 Pinned"
        except Exception as e:
            pin_note = f"\n⚠️ Could not pin: {esc(str(e)[:120])}"
    ch = await db.channel(p["chat_id"])
    text = f"✅ Posted to <b>{esc(ch['title']) if ch else p['chat_id']}</b>\n📝 {esc(plain(p['content'], 60))}"
    if da:
        text += f"\n🗑 Auto-deletes at {fmt_dt(da)}"
    text += pin_note
    st = await status_task
    await edit_status(bot, st, uid, text, kb([[("📋 Manage Post", f"p:{p['id']}")]]))
    # repeat option "Replace previous": remove the older post(s) of the same repeat
    if p.get("replace_prev") and p.get("repeat_id"):
        for old in await db.live_of_repeat(p["repeat_id"], p["id"], p.get("run_id")):
            try:
                await delete_live(bot, old)
            except Exception as e:
                if "not found" not in str(e).lower():
                    log.warning("Could not replace previous post %s: %s", old["id"], e)
            await db.set(old["id"], status="deleted", delete_at=None)


async def remove_live(bot: Bot, p):
    try:
        await delete_live(bot, p)
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
    """Sleeps until the exact next due time (or until something changes), so posts go out