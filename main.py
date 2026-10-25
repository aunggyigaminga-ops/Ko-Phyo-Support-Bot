import logging
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from html import escape

from dotenv import load_dotenv
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
TOKEN = "8621763485:AAHFOPZEF4nfXcro2tXiMiMZEiKBu4wWug"

OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@PTWK_1XBetMM").strip()
DB_PATH = os.getenv("DB_PATH", "moderation.db")
WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3"))
MUTE_MINUTES = int(os.getenv("MUTE_MINUTES", "30"))
FLOOD_MAX_MESSAGES = int(os.getenv("FLOOD_MAX_MESSAGES", "8"))
FLOOD_WINDOW_SECONDS = int(os.getenv("FLOOD_WINDOW_SECONDS", "10"))
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID", "").strip()
BLACKLIST = [x.strip().lower() for x in os.getenv("BLACKLIST_WORDS", "scam,phishing,free money").split(",") if x.strip()]

URL_RE = re.compile(r"(?i)(https?://\S+|t\.me/\S+|telegram\.me/\S+|tg://\S+|@[a-zA-Z0-9_]{4,})")
CHANNEL_RE = re.compile(r"(?i)(https?://t\.me/\+?\S+|t\.me/\S+|telegram\.me/\S+|@[a-zA-Z0-9_]{4,})")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("moderation-bot")
flood_tracker = defaultdict(deque)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                anti_link INTEGER NOT NULL DEFAULT 1,
                anti_flood INTEGER NOT NULL DEFAULT 1,
                captcha INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS pending_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            );
            """
        )
        conn.commit()


def setting(chat_id: int, name: str, default: int = 1) -> bool:
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
            conn.commit()
            return bool(default)
        return bool(row[name])


def set_setting(chat_id: int, name: str, value: bool):
    if name not in {"anti_link", "anti_flood", "captcha"}:
        return
    with closing(db()) as conn:
        conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
        conn.execute(f"UPDATE settings SET {name}=? WHERE chat_id=?", (int(value), chat_id))
        conn.commit()


def warnings(chat_id: int, user_id: int) -> int:
    with closing(db()) as conn:
        row = conn.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        return int(row[0]) if row else 0


def add_warning(chat_id: int, user_id: int) -> int:
    with closing(db()) as conn:
        conn.execute("INSERT OR IGNORE INTO warnings(chat_id,user_id,count) VALUES(?,?,0)", (chat_id, user_id))
        conn.execute("UPDATE warnings SET count=count+1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        conn.commit()
    return warnings(chat_id, user_id)


def is_admin(member) -> bool:
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


async def admin_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat or not update.effective_user:
        return False
    member = await update.effective_chat.get_member(update.effective_user.id)
    return is_admin(member)


async def log_event(context: ContextTypes.DEFAULT_TYPE, text: str):
    if LOG_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=LOG_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Unable to send log")


async def mute(context, chat_id: int, user_id: int, minutes: int):
    until = int(time.time()) + minutes * 60
    await context.bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until,
    )


async def punish(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str):
    msg = update.effective_message
    user = msg.from_user
    chat = update.effective_chat
    try:
        await msg.delete()
    except Exception:
        pass
    count = add_warning(chat.id, user.id)
    if count >= WARN_LIMIT:
        await mute(context, chat.id, user.id, MUTE_MINUTES)
        action = f"muted for {MUTE_MINUTES} minutes"
    else:
        action = f"warning {count}/{WARN_LIMIT}"
    await log_event(context, f"<b>Moderation</b>\nChat: <code>{chat.id}</code>\nUser: {escape(user.full_name)}\nReason: {escape(reason)}\nAction: {action}")
    try:
        notice = await context.bot.send_message(chat.id, f"⚠️ {user.mention_html()} — {reason}. {action}.", parse_mode=ParseMode.HTML)
        context.job_queue.run_once(delete_later, 15, data=(chat.id, notice.message_id))
    except Exception:
        pass


async def delete_later(context: ContextTypes.DEFAULT_TYPE):
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = msg.from_user
    if not msg or not user or chat.type == "private":
        return
    try:
        member = await chat.get_member(user.id)
        if is_admin(member):
            return
    except Exception:
        return

    text = (msg.text or msg.caption or "").strip()
    if setting(chat.id, "captcha"):
        with closing(db()) as conn:
            pending = conn.execute("SELECT 1 FROM pending_users WHERE chat_id=? AND user_id=? AND expires_at>?", (chat.id, user.id, int(time.time()))).fetchone()
        if pending:
            await punish(update, context, "Please verify before sending messages")
            return

    if setting(chat.id, "anti_link") and CHANNEL_RE.search(text):
        await punish(update, context, "Channel/link advertising is not allowed")
        return
    lower = text.lower()
    if any(word in lower for word in BLACKLIST):
        await punish(update, context, "Blocked advertising or scam content")
        return
    if setting(chat.id, "anti_flood"):
        key = (chat.id, user.id)
        now = time.time()
        q = flood_tracker[key]
        while q and q[0] < now - FLOOD_WINDOW_SECONDS:
            q.popleft()
        q.append(now)
        if len(q) > FLOOD_MAX_MESSAGES:
            await punish(update, context, "Flood/spam detected")
            q.clear()


async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    for member in update.chat_member.new_chat_members:
        if member.is_bot:
            continue
        if not setting(chat.id, "captcha"):
            continue
        expires = int(time.time()) + 300
        with closing(db()) as conn:
            conn.execute("INSERT OR REPLACE INTO pending_users(chat_id,user_id,expires_at) VALUES(?,?,?)", (chat.id, member.id, expires))
            conn.commit()
        keyboard = [[InlineKeyboardButton("✅ Verify / အတည်ပြုရန်", callback_data=f"verify:{chat.id}:{member.id}")]]
        try:
            sent = await context.bot.send_message(chat.id, f"မင်္ဂလာပါ {member.mention_html()}။ စာပို့ရန် အောက်က Verify ကိုနှိပ်ပါ။ ၅ မိနစ်အတွင်း မနှိပ်လျှင် mute လုပ်ပါမည်။", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
            context.job_queue.run_once(delete_later, 300, data=(chat.id, sent.message_id))
        except Exception:
            pass


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, chat_id, user_id = query.data.split(":")
    if query.from_user.id != int(user_id):
        await query.answer("ဒီခလုတ်က သင့်အတွက်မဟုတ်ပါ", show_alert=True)
        return
    with closing(db()) as conn:
        conn.execute("DELETE FROM pending_users WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        conn.commit()
    await query.edit_message_text("✅ Verified. ယခု စာပို့နိုင်ပါပြီ။")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Anti-spam moderation bot is running. Add me as an admin and grant Delete messages, Ban users, and Restrict users permissions.")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("စည်းကမ်းများ\n1. Channel/link ကြော်ငြာ မပို့ရ\n2. Spam/flood မလုပ်ရ\n3. Scam/phishing link မပို့ရ\n4. Admin approval မရှိဘဲ ကြော်ငြာမတင်ရ")


async def private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the owner contact and forward the user's private message to the owner."""
    if not update.effective_message or not update.effective_user:
        return
    button = InlineKeyboardButton("📩 Owner ကို ဆက်သွယ်ရန်", url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}")
    keyboard = InlineKeyboardMarkup([[button]])
    await update.effective_message.reply_text(
        f"မင်္ဂလာပါ။ အကူအညီလိုပါက Owner {OWNER_USERNAME} ကို ဆက်သွယ်ပါ။",
        reply_markup=keyboard,
    )
    try:
        await update.effective_message.forward(chat_id=OWNER_USERNAME)
        await context.bot.send_message(
            chat_id=OWNER_USERNAME,
            text=f"အသုံးပြုသူ {update.effective_user.full_name} ({update.effective_user.id}) က Bot ကို ဆက်သွယ်လာပါတယ်။",
        )
    except Exception:
        logger.info("Owner has not started the bot or cannot receive forwarded messages")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update, context):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"Anti-link: {'ON' if setting(chat_id, 'anti_link') else 'OFF'}\nAnti-flood: {'ON' if setting(chat_id, 'anti_flood') else 'OFF'}\nCaptcha: {'ON' if setting(chat_id, 'captcha') else 'OFF'}")


async def toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update, context):
        return
    if len(context.args) != 2 or context.args[0] not in {"link", "flood", "captcha"} or context.args[1].lower() not in {"on", "off"}:
        await update.message.reply_text("အသုံးပြုပုံ: /toggle link on|off\n/toggle flood on|off\n/toggle captcha on|off")
        return
    key = {"link": "anti_link", "flood": "anti_flood", "captcha": "captcha"}[context.args[0]]
    set_setting(update.effective_chat.id, key, context.args[1].lower() == "on")
    await update.message.reply_text("ပြင်ဆင်ပြီးပါပြီ။")


async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update, context) or not update.message.reply_to_message:
        await update.message.reply_text("Admin သာသုံးနိုင်ပြီး user message ကို reply လုပ်သုံးပါ။")
        return
    target = update.message.reply_to_message.from_user
    count = add_warning(update.effective_chat.id, target.id)
    await update.message.reply_text(f"{target.full_name}: warning {count}/{WARN_LIMIT}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Update failed", exc_info=context.error)


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN မထည့်ရသေးပါ။ .env.example ကို .env အဖြစ် copy ပြီး token ထည့်ပါ။")
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("toggle", toggle))
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CallbackQueryHandler(verify, pattern=r"^verify:"))
    app.add_handler(ChatMemberHandler(new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderate_message))
    app.add_error_handler(error_handler)
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
