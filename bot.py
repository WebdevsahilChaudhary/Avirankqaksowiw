import os
import re
import gc
import time
import html
import shutil
import sqlite3
import logging
import asyncio
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import Forbidden, BadRequest, Conflict, RetryAfter, TelegramError
from telegram.warnings import PTBUserWarning
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import secret_engine as eng

warnings.filterwarnings("ignore", category=PTBUserWarning)

# ── STRICT ENVIRONMENT VARIABLES (SET IN RENDER DASHBOARD) ──
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "6681497425"))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "@Monsterlevi")
BRAND_NAME = eng.BRAND_NAME
BRAND_URL = eng.BRAND_URL

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── LIGHTWEIGHT KEEP-ALIVE WEB SERVER FOR RENDER & UPTIMEROBOT ──
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"AVIRANKORA SECRET GROUP BOT IS ALIVE 24/7!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"🌐 Render Keep-Alive Web Server listening on port {port}")


def clean(text: str) -> str:
    return html.escape(str(text or ""))


async def safe_answer(query, text: str | None = None, show_alert: bool = False) -> None:
    if not query:
        return
    try:
        await query.answer(text=text, show_alert=show_alert)
    except TelegramError:
        pass


def get_setting(key: str, default: str = "") -> str:
    return eng.SETTINGS_CACHE.get(key, default)


def set_setting(key: str, value: str) -> None:
    conn = eng.get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    eng.SETTINGS_CACHE[key] = value
    eng.mark_db_dirty()


def is_maintenance_mode() -> bool:
    return get_setting("maintenance_mode", "0") == "1"


def is_ocr_strict() -> bool:
    return get_setting("ocr_strict", "1") == "1"


def is_proofs_enabled() -> bool:
    return get_setting("proofs_enabled", "1") == "1"


def is_fomo_enabled() -> bool:
    return get_setting("fomo_enabled", "1") == "1"


def get_log_channel_id() -> int | None:
    val = get_setting("log_channel_id", eng.DEFAULT_LOG_CHANNEL).strip()
    if val and val.lstrip("-").isdigit():
        return int(val)
    return None


def upsert_user(user) -> None:
    if not user:
        return
    uname = user.username or ""
    fname = user.full_name or ""
    if eng.KNOWN_USERS_CACHE.get(user.id) == (uname, fname):
        return
    eng.KNOWN_USERS_CACHE[user.id] = (uname, fname)
    conn = eng.get_db()
    conn.execute(
        """
        INSERT INTO users (user_id, username, full_name) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, full_name = excluded.full_name
        """,
        (user.id, uname, fname),
    )
    conn.commit()


def get_strikes(user_id: int, paper_id: int) -> int:
    row = eng.get_db().execute(
        "SELECT strikes FROM paper_strikes WHERE user_id = ? AND paper_id = ?", (user_id, paper_id)
    ).fetchone()
    return row["strikes"] if row else 0


def increment_strikes(user_id: int, paper_id: int) -> int:
    conn = eng.get_db()
    conn.execute(
        """
        INSERT INTO paper_strikes (user_id, paper_id, strikes) VALUES (?, ?, 1)
        ON CONFLICT(user_id, paper_id) DO UPDATE SET strikes = strikes + 1
        """,
        (user_id, paper_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT strikes FROM paper_strikes WHERE user_id = ? AND paper_id = ?", (user_id, paper_id)
    ).fetchone()
    eng.mark_db_dirty()
    return row["strikes"]


def get_leave_status(user_id: int, channel_id: int) -> tuple[int, bool]:
    row = eng.get_db().execute(
        "SELECT warns, banned FROM leave_warns WHERE user_id = ? AND channel_id = ?",
        (user_id, channel_id),
    ).fetchone()
    if not row:
        return 0, False
    return int(row["warns"]), bool(row["banned"])


def pick_next_qr(paper: sqlite3.Row) -> tuple[str, str, int | None]:
    today = eng.today_ist_str()
    conn = eng.get_db()
    conn.execute("UPDATE qr_codes SET today_count = 0, last_reset_date = ? WHERE last_reset_date != ?", (today, today))
    conn.commit()

    active_qrs = conn.execute(
        "SELECT * FROM qr_codes WHERE active = 1 AND (daily_limit = 0 OR today_count < daily_limit) ORDER BY id ASC"
    ).fetchall()

    if active_qrs:
        last_idx = int(get_setting("qr_rr_idx", "0") or "0")
        chosen = active_qrs[last_idx % len(active_qrs)]
        set_setting("qr_rr_idx", str((last_idx + 1) % len(active_qrs)))
        limit_txt = f"{chosen['today_count'] + 1}/{chosen['daily_limit']}" if chosen["daily_limit"] > 0 else f"{chosen['today_count'] + 1}/∞"
        return chosen["file_id"], f"{chosen['label']} ({limit_txt} today)", chosen["id"]

    if paper["qr_file_id"]:
        return paper["qr_file_id"], f"Plan QR ({paper['name']})", None
    global_qr = get_setting("global_qr", "")
    if global_qr:
        return global_qr, "Global Default QR", None
    return "", "", None


def increment_qr_usage(qr_id: int | None) -> None:
    if not qr_id:
        return
    today = eng.today_ist_str()
    conn = eng.get_db()
    conn.execute(
        "UPDATE qr_codes SET today_count = today_count + 1, total_used = total_used + 1, last_reset_date = ? WHERE id = ?",
        (today, qr_id),
    )
    conn.commit()


def format_private_channel_open_link(channel_id: str | int, fallback_url: str) -> str:
    cid_str = str(channel_id).strip()
    if cid_str.startswith("-100") and cid_str[4:].isdigit():
        return f"https://t.me/c/{cid_str[4:]}/1"
    return fallback_url or BRAND_URL


def extract_utr_from_text(text: str) -> str | None:
    if not text:
        return None
    cleaned = text.strip()
    if re.fullmatch(r"[A-Za-z0-9]{6,30}", cleaned) and any(c.isdigit() for c in cleaned):
        return cleaned
    tokens = re.findall(r"\b[A-Za-z0-9]{6,30}\b", cleaned)
    for tok in tokens:
        if len(tok) == 12 and tok.isdigit():
            return tok
    for tok in tokens:
        if sum(c.isdigit() for c in tok) >= 4:
            return tok
    return None


async def is_user_in_chat(bot, chat_identifier: str | int, user_id: int, use_cache: bool = True) -> bool:
    now = time.monotonic()
    cache_key = (user_id, str(chat_identifier))
    if use_cache and cache_key in eng.CHANNEL_MEMBER_CACHE:
        cached_val, exp = eng.CHANNEL_MEMBER_CACHE[cache_key]
        if now < exp:
            return cached_val
    try:
        cid = int(str(chat_identifier)) if str(chat_identifier).lstrip("-").isdigit() else str(chat_identifier)
        member = await bot.get_chat_member(chat_id=cid, user_id=user_id)
        is_mem = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ) or (member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False))
        eng.CHANNEL_MEMBER_CACHE[cache_key] = (is_mem, now + (300.0 if is_mem else 5.0))
        return is_mem
    except TelegramError:
        return False


async def ui_render(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    photo: str | None = None,
) -> None:
    query = update.callback_query
    if query:
        msg = query.message
        has_media = bool(msg.photo or msg.document or msg.video or msg.animation)
        try:
            if photo:
                try:
                    await msg.delete()
                except TelegramError:
                    pass
                await msg.chat.send_photo(photo=photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            elif has_media:
                try:
                    await msg.delete()
                except TelegramError:
                    pass
                await msg.chat.send_message(
                    text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True
                )
            else:
                await query.edit_message_text(
                    text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True
                )
            return
        except (Forbidden, BadRequest, TelegramError):
            pass

    target_msg = update.effective_message
    if target_msg:
        try:
            if photo:
                await target_msg.reply_photo(photo=photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            else:
                await target_msg.reply_text(
                    text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True
                )
        except (Forbidden, TelegramError):
            return


# ── LOG CHANNEL CLOUD BACKUP & AUTO-RESTORE FOR RENDER ──
async def send_log_message(bot, text: str) -> None:
    log_cid = get_log_channel_id()
    if not log_cid:
        return
    try:
        await bot.send_message(chat_id=log_cid, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def auto_restore_from_log_channel(bot) -> bool:
    local_score = eng.count_orders_in_db(eng.DB_FILE)
    log_cid = get_log_channel_id()
    if not log_cid:
        return False

    try:
        chat = await bot.get_chat(log_cid)
        pinned = chat.pinned_message
        if pinned and pinned.document and pinned.document.file_name.endswith(".db"):
            tmp_pinned = "pinned_check.db"
            tg_file = await bot.get_file(pinned.document.file_id)
            await tg_file.download_to_drive(custom_path=tmp_pinned)
            pinned_score = eng.count_orders_in_db(tmp_pinned)
            if pinned_score >= local_score:
                eng.close_db()
                shutil.copy2(tmp_pinned, eng.DB_FILE)
                eng.init_db()
                print(f"🎉 Auto-Restored DB from Log Channel Pinned Backup (Score: {pinned_score})!")
                os.remove(tmp_pinned)
                return True
            if os.path.exists(tmp_pinned):
                os.remove(tmp_pinned)
    except Exception as e:
        logger.warning(f"Auto-restore check skipped: {e}")
    return False


async def send_log_channel_backup(bot, reason: str = "Auto-Backup") -> bool:
    log_cid = get_log_channel_id()
    if not log_cid:
        return False
    backup_path = f"{eng.DB_FILE}.backup.db"
    try:
        def _make_backup():
            src = sqlite3.connect(eng.DB_FILE)
            dst = sqlite3.connect(backup_path)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        await asyncio.to_thread(_make_backup)

        conn = eng.get_db()
        u_cnt = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        p_cnt = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        o_cnt = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        prf_cnt = conn.execute("SELECT COUNT(*) FROM proofs").fetchone()[0]
        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p IST")
        caption = (
            f"☁️ <b>Secret Group DB Backup ({clean(reason)})</b>\n"
            f"🕒 <b>Time:</b> {now_str}\n"
            f"📊 <b>Stats:</b> {p_cnt} Plans | {o_cnt} Orders | {u_cnt} Users | {prf_cnt} Proofs"
        )
        with open(backup_path, "rb") as f:
            sent = await bot.send_document(
                chat_id=log_cid,
                document=f,
                filename="avirankora_secret.db",
                caption=caption,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
        try:
            await bot.pin_chat_message(chat_id=log_cid, message_id=sent.message_id, disable_notification=True)
        except TelegramError:
            pass
        eng.dirty_flag = False
        return True
    except Exception:
        return False
    finally:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass
        gc.collect()


async def send_order_card_reliably(
    bot, target_chat_id: int, photo_id: str, caption: str, reply_markup: InlineKeyboardMarkup, order_id: int
) -> bool:
    for _ in range(2):
        try:
            await bot.send_photo(
                chat_id=target_chat_id, photo=photo_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1.0)
        except TelegramError as e:
            try:
                await bot.send_message(
                    chat_id=target_chat_id,
                    text=caption + f"\n\n⚠️ <i>(Photo preview error: {clean(str(e))})</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                return True
            except TelegramError:
                break
    if target_chat_id != OWNER_ID:
        try:
            await bot.send_photo(
                chat_id=OWNER_ID,
                photo=photo_id,
                caption="🚨 <b>[LOG CHANNEL FALLBACK]</b>\n" + caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return True
        except TelegramError:
            pass
    return False


# ── JOIN REQUEST AUTO-APPROVER ──
async def handle_secret_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req = update.chat_join_request
    if not req:
        return

    user = req.from_user
    chat_id = req.chat.id
    conn = eng.get_db()

    plan = conn.execute("SELECT * FROM papers WHERE channel_id = ? LIMIT 1", (chat_id,)).fetchone()
    if not plan:
        return

    warns, is_banned = get_leave_status(user.id, chat_id)
    if is_banned or warns >= 3:
        try:
            await req.decline()
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    f"🚫 <b>Join Request Declined (Permanent Ban)!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Aapne <b>{clean(plan['name'])}</b> ko 3 baar leave kiya tha, isliye aapki entry permanently block ho chuki hai.\n"
                    f"👉 Contact {clean(OWNER_USERNAME)} for help."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    paid_order = conn.execute(
        """
        SELECT o.id FROM orders o
        JOIN papers p ON o.paper_id = p.id
        WHERE o.user_id = ? AND p.channel_id = ? AND o.status = 'accepted'
        LIMIT 1
        """,
        (user.id, chat_id),
    ).fetchone()

    if paid_order or user.id == OWNER_ID:
        try:
            await req.approve()
            eng.CHANNEL_MEMBER_CACHE[(user.id, str(chat_id))] = (True, time.monotonic() + 300.0)
            open_url = format_private_channel_open_link(chat_id, BRAND_URL)
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    f"🎉 <b>Welcome Inside {clean(plan['name'])}!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Aapki Join Request <b>Automatically Approve</b> ho gayi hai!\n\n"
                    f"⚠️ <b>Strict Rule:</b> Is Secret Channel ko kabhi leave mat karna! Agar aap 3 baar leave karoge toh bot aapko <b>Permanently Ban</b> kar dega.\n\n"
                    f"👇 <i>Neeche click karke Secret Group open karein:</i>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"📂 Open {plan['name']}", url=open_url)]]),
            )
            await send_log_message(
                context.bot,
                f"✅ <b>Auto-Approved Join Request:</b> <a href=\"tg://user?id={user.id}\">{clean(user.full_name)}</a> (<code>{user.id}</code>) joined <b>{clean(plan['name'])}</b>.",
            )
        except TelegramError as e:
            logger.error(f"Failed to approve join request for {user.id}: {e}")
    else:
        try:
            await req.decline()
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    f"🚫 <b>Unauthorized Join Request Declined!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Dost, <b>{clean(plan['name'])}</b> me sirf wahi ID approve hoti hai jisne bot me payment kiya ho!\n\n"
                    f"👇 <i>Apni VIP entry lene ke liye neeche click karein:</i>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_kb(),
            )
        except TelegramError:
            pass
        await send_log_message(
            context.bot,
            f"🛡️ <b>Leaked Link Blocked!</b> Unauthorized user <a href=\"tg://user?id={user.id}\">{clean(user.full_name)}</a> (<code>{user.id}</code>) tried to join <b>{clean(plan['name'])}</b> without paying. Auto-Declined!",
        )


# ── SECRET CHANNEL LEAVE TRACKER ──
async def handle_secret_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    if not cmu:
        return

    chat_id = cmu.chat.id
    user = cmu.new_chat_member.user
    if not user or user.is_bot or user.id == OWNER_ID:
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    was_member = old_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED)
    left_now = new_status == ChatMemberStatus.LEFT

    if not (was_member and left_now):
        return

    conn = eng.get_db()
    plan = conn.execute("SELECT * FROM papers WHERE channel_id = ? LIMIT 1", (chat_id,)).fetchone()
    if not plan:
        return

    eng.CHANNEL_MEMBER_CACHE[(user.id, str(chat_id))] = (False, time.monotonic() + 5.0)

    paid_order = conn.execute(
        """
        SELECT o.* FROM orders o
        JOIN papers p ON o.paper_id = p.id
        WHERE o.user_id = ? AND p.channel_id = ? AND o.status = 'accepted'
        ORDER BY o.id DESC LIMIT 1
        """,
        (user.id, chat_id),
    ).fetchone()
    if not paid_order:
        return

    conn.execute(
        """
        INSERT INTO leave_warns (user_id, channel_id, warns, banned)
        VALUES (?, ?, 1, 0)
        ON CONFLICT(user_id, channel_id) DO UPDATE SET warns = warns + 1
        """,
        (user.id, chat_id),
    )
    conn.commit()
    warns, _ = get_leave_status(user.id, chat_id)
    eng.mark_db_dirty()

    if warns >= 3:
        conn.execute("UPDATE leave_warns SET banned = 1 WHERE user_id = ? AND channel_id = ?", (user.id, chat_id))
        conn.commit()
        if paid_order["invite_link"]:
            try:
                await context.bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=paid_order["invite_link"])
            except TelegramError:
                pass
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
        except TelegramError:
            pass

        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    f"🚫 <b>PERMANENT BAN — Warning 3/3 Reached!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Aapne <b>{clean(plan['name'])}</b> ko 3 baar leave kiya hai!\n"
                    f"Security rules ke mutabik ab aapko Secret Group se <b>Permanently Ban</b> kar diya gaya hai.\n\n"
                    f"👉 Contact {clean(OWNER_USERNAME)} for help."
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

        await send_log_message(
            context.bot,
            f"🚨 <b>3/3 Leaves — Student Permanently Banned!</b>\n"
            f"👤 <a href=\"tg://user?id={user.id}\">{clean(user.full_name)}</a> (<code>{user.id}</code>) left <b>{clean(plan['name'])}</b> 3 times. Banned & link revoked!",
        )
    else:
        inv_link = paid_order["invite_link"] or BRAND_URL
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    f"⚠️ <b>WARNING {warns}/3 — Secret Channel Leave Detected!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Aapne abhi <b>{clean(plan['name'])}</b> leave kiya hai!\n"
                    f"🚨 Sirf <b>{3 - warns} warning(s)</b> bachi hain! 3 baar leave karne par <b>Permanent Ban</b> lag jayega.\n\n"
                    f"👇 <i>Wapis join karne ke liye neeche click karein:</i>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🔄 Re-Join {plan['name']} Now", url=inv_link)]]),
            )
        except TelegramError:
            pass

        await send_log_message(
            context.bot,
            f"⚠️ <b>Secret Channel Leave Warning ({warns}/3):</b> <a href=\"tg://user?id={user.id}\">{clean(user.full_name)}</a> (<code>{user.id}</code>) left <b>{clean(plan['name'])}</b>.",
        )


# ── ACCESS GATE ──
async def check_access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, force_fresh: bool = False) -> bool:
    user = update.effective_user
    if not user:
        return False

    allowed, wait_s = eng.check_rate_limit(user.id, OWNER_ID, is_photo=False)
    if not allowed:
        await safe_answer(update.callback_query, f"⏳ Thoda aaram se click karo bhai! ({wait_s}s wait)", show_alert=False)
        return False

    upsert_user(user)
    if user.id == OWNER_ID:
        return True

    if is_maintenance_mode():
        maint_text = (
            f"🛠️ <b>Bot Under Maintenance!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>{clean(BRAND_NAME)} Bot</b> abhi upgrade ho raha hai! ⚡\n"
            f"⏳ <i>Kripya 5 minute baad dubara try karein.</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Official Channel", url=BRAND_URL)],
            [InlineKeyboardButton("🔄 Check Again", callback_data="menu_main")],
        ])
        await safe_answer(update.callback_query, "🛠️ Bot abhi maintenance me hai!", show_alert=True)
        await ui_render(update, maint_text, reply_markup=kb)
        return False

    if not eng.FORCE_CHANNELS_CACHE:
        return True

    now = time.monotonic()
    if not force_fresh and eng.FORCE_JOIN_CACHE.get(user.id, 0.0) > now:
        return True

    results = await asyncio.gather(
        *(is_user_in_chat(context.bot, ch["chat_id"], user.id, use_cache=not force_fresh) for ch in eng.FORCE_CHANNELS_CACHE)
    )
    missing = [ch for ch, ok in zip(eng.FORCE_CHANNELS_CACHE, results) if not ok]
    if not missing:
        eng.FORCE_JOIN_CACHE[user.id] = now + 600.0
        return True

    buttons = [[InlineKeyboardButton(f"📢 Join {ch['title']}", url=ch["invite_url"])] for ch in missing]
    buttons.append([InlineKeyboardButton("✅ Maine Join Kar Liya (Verify)", callback_data="verify_join")])
    msg_text = (
        f"🔒 <b>Ruko Dost! Official Channel Join Karna Zaroori Hai</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{clean(BRAND_NAME)}</b> me entry lene ke liye pehle neeche diye gaye <b>{len(missing)} channel/group</b> ko join karo 👇"
    )
    await safe_answer(update.callback_query, f"⚠️ Pehle {len(missing)} channel/group join karo!", show_alert=True)
    await ui_render(update, msg_text, reply_markup=InlineKeyboardMarkup(buttons))
    return False


# ── DYNAMIC MAIN MENU ──
def main_menu_kb() -> InlineKeyboardMarkup:
    _, _, seats_left = eng.get_fomo_data()
    join_lbl = f"🔥 JOIN SECRET GROUP ({seats_left} Seats Left!)" if is_fomo_enabled() else "🔥 JOIN SECRET GROUP NOW"
    rows = [
        [InlineKeyboardButton(join_lbl, callback_data="menu_browse")],
    ]
    if is_proofs_enabled():
        rows.append([InlineKeyboardButton("🏆 Last Year 90%+ Proofs & Reviews", callback_data="menu_proofs:0")])
    rows.append([
        InlineKeyboardButton("📦 My VIP Entry Status", callback_data="menu_orders"),
        InlineKeyboardButton("💬 24/7 Support", callback_data="menu_support"),
    ])
    return InlineKeyboardMarkup(rows)


def render_welcome_message(first_name: str) -> str:
    fomo_banner, _, _ = eng.get_fomo_data()
    tpl = eng.get_custom_text("welcome_text")
    return (
        tpl.replace("{first_name}", clean(first_name))
        .replace("{brand_name}", clean(BRAND_NAME))
        .replace("{fomo_banner}", fomo_banner)
    )


# ── USER FLOW HANDLERS ──
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_message.chat.type != "private":
        return
    context.user_data.clear()
    if not await check_access_gate(update, context):
        return
    await safe_answer(update.callback_query)
    await ui_render(update, render_welcome_message(update.effective_user.first_name), reply_markup=main_menu_kb())


async def verify_join_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await check_access_gate(update, context, force_fresh=True):
        await safe_answer(query, "🎉 Verified! Welcome to Avirankora Secret Group!")
        await ui_render(update, render_welcome_message(query.from_user.first_name), reply_markup=main_menu_kb())


async def show_proofs_gallery_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await check_access_gate(update, context):
        return

    if not is_proofs_enabled() and query.from_user.id != OWNER_ID:
        await safe_answer(query, "Proofs section abhi off hai!", show_alert=True)
        await ui_render(update, render_welcome_message(query.from_user.first_name), reply_markup=main_menu_kb())
        return

    idx = int(query.data.split(":")[1]) if ":" in query.data else 0
    proofs = eng.get_db().execute("SELECT * FROM proofs ORDER BY id ASC").fetchall()

    if not proofs:
        await safe_answer(query, "Abhi proofs upload ho rahe hain!", show_alert=True)
        await ui_render(
            update,
            "🏆 <b>Last Year 90%+ Results & Student Reviews</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🌟 Last year <b>100+ students</b> scored <b>90%+ in Boards</b> studying from our Secret Group!\n\n"
            "<i>(Owner abhi screenshots update kar rahe hain — tab tak aap niche click karke Secret Group join kar sakte hain!)</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔥 JOIN SECRET GROUP NOW", callback_data="menu_browse")],
                [InlineKeyboardButton("⬅️ Back to Home", callback_data="menu_main")],
            ]),
        )
        return

    idx = idx % len(proofs)
    item = proofs[idx]
    await safe_answer(query, f"🏆 Proof {idx + 1} of {len(proofs)}")

    header = f"🏆 <b>Last Year 90%+ Proof & Review ({idx + 1}/{len(proofs)})</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    cap = header + clean(item["caption"])

    rows = []
    if len(proofs) > 1:
        prev_idx = (idx - 1) % len(proofs)
        next_idx = (idx + 1) % len(proofs)
        rows.append([
            InlineKeyboardButton("⬅️ Prev Proof", callback_data=f"menu_proofs:{prev_idx}"),
            InlineKeyboardButton(f"📄 {idx + 1}/{len(proofs)}", callback_data=f"menu_proofs:{next_idx}"),
            InlineKeyboardButton("Next Proof ➡️", callback_data=f"menu_proofs:{next_idx}"),
        ])
    rows.append([InlineKeyboardButton("🔥 JOIN SECRET GROUP NOW", callback_data="menu_browse")])
    rows.append([InlineKeyboardButton("⬅️ Back to Home", callback_data="menu_main")])
    kb = InlineKeyboardMarkup(rows)

    if item["media_type"] == "photo" and item["file_id"]:
        await ui_render(update, cap, reply_markup=kb, photo=item["file_id"])
    elif item["media_type"] == "video" and item["file_id"]:
        try:
            await query.message.delete()
        except TelegramError:
            pass
        await query.message.chat.send_video(video=item["file_id"], caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await ui_render(update, cap, reply_markup=kb)


async def menu_router_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    context.user_data.clear()
    if not await check_access_gate(update, context):
        return
    data = query.data

    if data == "menu_main":
        await safe_answer(query)
        await ui_render(update, render_welcome_message(query.from_user.first_name), reply_markup=main_menu_kb())

    elif data.startswith("menu_proofs"):
        await show_proofs_gallery_cb(update, context)

    elif data == "menu_browse":
        papers = eng.get_db().execute("SELECT * FROM papers WHERE active = 1 ORDER BY id ASC").fetchall()
        if not papers:
            await safe_answer(query, "Secret Group entry abhi open ho rahi hai!", show_alert=True)
            await ui_render(
                update,
                "⏳ <b>Secret Group Entry Starting Soon!</b>\nKripya thodi der baad check karein.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Home", callback_data="menu_main")]]),
            )
            return

        if len(papers) == 1:
            await safe_answer(query, "⚡ Loading Secret Group Details...")
            await render_paper_view(update, context, papers[0], single_paper_mode=True)
            return

        await safe_answer(query)
        uid = query.from_user.id
        fomo_banner, _, _ = eng.get_fomo_data()
        buttons = []
        for p in papers:
            final_p, base_p, _ = eng.calculate_student_price(uid, p)
            price_str = f"₹{final_p} (OFF! 🎁)" if final_p < base_p else f"₹{base_p}"
            buttons.append([InlineKeyboardButton(f"👑 {p['name']}  •  {price_str}", callback_data=f"view_paper:{p['id']}")])
        if is_proofs_enabled():
            buttons.append([InlineKeyboardButton("🏆 View 90%+ Proofs", callback_data="menu_proofs:0")])
        buttons.append([InlineKeyboardButton("⬅️ Back to Home", callback_data="menu_main")])
        await ui_render(
            update,
            f"🔥 <b>Select Your Secret Group Batch</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n{fomo_banner}\n\nApna class/batch select karke VIP entry lein 👇",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "menu_orders":
        await safe_answer(query)
        await show_user_orders(update, context)

    elif data == "menu_support":
        await safe_answer(query)
        sup_txt = (
            eng.get_custom_text("support_text")
            .replace("{owner_username}", clean(OWNER_USERNAME))
            .replace("{brand_url}", clean(BRAND_URL))
        )
        await ui_render(
            update,
            sup_txt,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💬 Chat with {OWNER_USERNAME}", url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}")],
                [InlineKeyboardButton("⬅️ Back to Home", callback_data="menu_main")],
            ]),
        )


# ── SECRET GROUP PLAN VIEW ──
async def render_paper_view(
    update: Update, context: ContextTypes.DEFAULT_TYPE, paper: sqlite3.Row, single_paper_mode: bool = False
) -> None:
    user_id = update.effective_user.id
    paper_id = paper["id"]
    back_cb = "menu_main" if single_paper_mode else "menu_browse"
    back_label = "⬅️ Back to Home" if single_paper_mode else "⬅️ All Batches"

    warns, is_banned = get_leave_status(user_id, paper["channel_id"])
    if is_banned or warns >= 3:
        await ui_render(
            update,
            f"🚫 <b>PERMANENTLY BANNED (3/3 Channel Leaves)!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Aapne <b>{clean(paper['name'])}</b> ko 3 baar leave kiya tha, isliye aapki VIP entry cancel ho chuki hai.\n\n"
            f"👉 Contact {clean(OWNER_USERNAME)} for assistance.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💬 Contact {OWNER_USERNAME}", url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}")],
                [InlineKeyboardButton(back_label, callback_data=back_cb)],
            ]),
        )
        return

    conn = eng.get_db()
    accepted_order = conn.execute(
        "SELECT * FROM orders WHERE user_id = ? AND paper_id = ? AND status = 'accepted' ORDER BY id DESC LIMIT 1",
        (user_id, paper_id),
    ).fetchone()
    pending_order = conn.execute(
        "SELECT * FROM orders WHERE user_id = ? AND paper_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (user_id, paper_id),
    ).fetchone()

    in_paper_channel = await is_user_in_chat(context.bot, paper["channel_id"], user_id)

    if in_paper_channel:
        fallback_inv = accepted_order["invite_link"] if accepted_order and accepted_order["invite_link"] else BRAND_URL
        direct_url = format_private_channel_open_link(paper["channel_id"], fallback_inv)
        text = (
            f"✅ <b>Aap {clean(paper['name'])} Me Joined Ho!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>Batch:</b> {clean(paper['name'])}\n"
            f"🔓 <b>Status:</b> Active VIP Member ✅\n"
            f"⚠️ <b>Leave Warnings:</b> {warns}/3 <i>(Never leave the channel!)</i>\n\n"
            f"👇 Neeche button pe tap karke seedha Secret Group open karein:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📂 Open {paper['name']}", url=direct_url)],
            [InlineKeyboardButton("🔄 Refresh Status", callback_data=f"chk_paper_join:{paper_id}")],
            [InlineKeyboardButton(back_label, callback_data=back_cb)],
        ])
        await ui_render(update, text, reply_markup=kb)
        return

    if accepted_order:
        inv_link = accepted_order["invite_link"]
        if not inv_link:
            try:
                inv = await context.bot.create_chat_invite_link(
                    chat_id=paper["channel_id"],
                    creates_join_request=True,
                    name=f"VIP Order #{accepted_order['id']} - {user_id}",
                )
                inv_link = inv.invite_link
                conn.execute("UPDATE orders SET invite_link = ? WHERE id = ?", (inv_link, accepted_order["id"]))
                conn.commit()
            except TelegramError:
                inv_link = BRAND_URL

        text = (
            f"🎉 <b>Aapki VIP Entry Approved Hai! (Order #{accepted_order['id']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>Batch:</b> {clean(paper['name'])}\n"
            f"⚠️ <b>Leave Warnings:</b> {warns}/3\n\n"
            f"👇 Neeche <b>Request to Join Secret Group</b> pe click karo — <b>Bot aapki ID ko 1 second me Automatically Approve kar dega!</b>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🚀 Request to Join {paper['name']}", url=inv_link)],
            [InlineKeyboardButton("✅ Maine Join Kar Liya (Check Status)", callback_data=f"chk_paper_join:{paper_id}")],
            [InlineKeyboardButton(back_label, callback_data=back_cb)],
        ])
        await ui_render(update, text, reply_markup=kb)
        return

    if pending_order:
        text = (
            f"⏳ <b>Payment Verification Chal Raha Hai (Order #{pending_order['id']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>Batch:</b> {clean(paper['name'])} (₹{pending_order['amount'] or paper['price']})\n\n"
            f"Aapka screenshot owner ke paas bhej diya gaya hai. Verify hote hi aapko turant Secret Group ka Auto-Approval Link mil jayega! 🚀"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Check Status Again", callback_data=f"view_paper:{paper_id}")],
            [InlineKeyboardButton(back_label, callback_data=back_cb)],
        ])
        await ui_render(update, text, reply_markup=kb)
        return

    if get_strikes(user_id, paper_id) >= 3:
        await ui_render(
            update,
            f"🚫 <b>Access Blocked!</b>\n3 fake payment proofs ki wajah se aapko block kar diya gaya hai.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💬 Contact {OWNER_USERNAME}", url=f"https://t.me/{OWNER_USERNAME.lstrip('@')}")],
                [InlineKeyboardButton(back_label, callback_data=back_cb)],
            ]),
        )
        return

    final_price, base_price, disc_reason = eng.calculate_student_price(user_id, paper)
    fomo_banner, _, seats_left = eng.get_fomo_data()
    if final_price < base_price:
        price_line = f"💰 <b>Entry Fee:</b> <s>₹{base_price}</s>  👉  <b>₹{final_price} ONLY!</b> 🔥\n{clean(disc_reason)}\n"
    else:
        anchor_mrp = base_price * 2
        price_line = f"💰 <b>Entry Fee:</b> <s>₹{anchor_mrp}</s>  👉  <b>₹{base_price} ONLY!</b> <i>(Limited Offer)</i>\n"

    preview_fid = (paper["preview_file_id"] or "").strip()
    preview_type = (paper["preview_type"] or "").strip()

    tpl = eng.get_custom_text("group_details_text")
    text = (
        tpl.replace("{plan_name}", clean(paper["name"]))
        .replace("{price_line}", price_line)
        .replace("{description}", clean(paper["description"]))
        .replace("{fomo_banner}", fomo_banner)
    )

    pay_btn_lbl = f"⚡ Pay ₹{final_price} & Join ({seats_left} Seats Left!)" if is_fomo_enabled() else f"⚡ Pay ₹{final_price} & Join Secret Group"
    rows = [
        [InlineKeyboardButton(pay_btn_lbl, callback_data=f"pay_paper:{paper['id']}")],
    ]
    sub_row = []
    if is_proofs_enabled():
        sub_row.append(InlineKeyboardButton("🏆 90%+ Proofs", callback_data="menu_proofs:0"))
    sub_row.append(InlineKeyboardButton("🎟️ Promo Code", callback_data=f"apply_code:{paper['id']}"))
    rows.append(sub_row)

    if preview_fid:
        rows.append([InlineKeyboardButton("👁️ View Sample / Demo", callback_data=f"preview_paper:{paper['id']}")])
    rows.append([InlineKeyboardButton(back_label, callback_data=back_cb)])
    kb = InlineKeyboardMarkup(rows)

    if preview_fid and preview_type == "photo":
        await ui_render(update, text, reply_markup=kb, photo=preview_fid)
    else:
        await ui_render(update, text, reply_markup=kb)


async def apply_code_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await check_access_gate(update, context):
        return
    paper_id = int(query.data.split(":")[1])
    context.user_data.clear()
    context.user_data["mode"] = "AWAIT_COUPON"
    context.user_data["coupon_paper_id"] = paper_id
    await safe_answer(query, "🎟️ Type your Promo Code!")
    await ui_render(
        update,
        "🎟️ <b>Enter Promo / Discount Code</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\nApna Discount Code yahan type karke bhejo:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"view_paper:{paper_id}")]]),
    )


async def preview_paper_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    paper_id = int(query.data.split(":")[1])
    paper = eng.get_db().execute("SELECT * FROM papers WHERE id = ? AND active = 1", (paper_id,)).fetchone()
    if not paper or not paper["preview_file_id"]:
        await safe_answer(query, "Demo preview abhi available nahi hai!", show_alert=True)
        return
    final_price, _, _ = eng.calculate_student_price(query.from_user.id, paper)
    await safe_answer(query, "👁️ Loading sample...")
    cap = f"👁️ <b>Sample Demo — {clean(paper['name'])}</b>\n💰 <b>VIP Entry Fee:</b> ₹{final_price}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚡ Pay ₹{final_price} & Join Now", callback_data=f"pay_paper:{paper['id']}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"view_paper:{paper['id']}")],
    ])
    try:
        if paper["preview_type"] == "document":
            await query.message.reply_document(document=paper["preview_file_id"], caption=cap, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await ui_render(update, cap, reply_markup=kb, photo=paper["preview_file_id"])
    except TelegramError:
        pass


async def check_paper_join_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    paper_id = int(query.data.split(":")[1])
    conn = eng.get_db()
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    active_count = conn.execute("SELECT COUNT(*) FROM papers WHERE active = 1").fetchone()[0]
    if not paper:
        return
    joined = await is_user_in_chat(context.bot, paper["channel_id"], query.from_user.id, use_cache=False)
    if joined:
        await safe_answer(query, "✅ Confirmed! Aap Secret Group me joined ho!", show_alert=True)
    else:
        await safe_answer(query, "❌ Aapne abhi tak Join Request send nahi ki hai! Upar Join button pe click karo.", show_alert=True)
    await render_paper_view(update, context, paper, single_paper_mode=(active_count == 1))


async def view_paper_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    context.user_data.clear()
    if not await check_access_gate(update, context):
        return
    await safe_answer(query)
    paper_id = int(query.data.split(":")[1])
    conn = eng.get_db()
    paper = conn.execute("SELECT * FROM papers WHERE id = ? AND active = 1", (paper_id,)).fetchone()
    active_count = conn.execute("SELECT COUNT(*) FROM papers WHERE active = 1").fetchone()[0]
    if not paper:
        await ui_render(update, "⚠️ <b>Yeh batch abhi available nahi hai!</b>", reply_markup=main_menu_kb())
        return
    await render_paper_view(update, context, paper, single_paper_mode=(active_count == 1))


async def myorders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_message.chat.type != "private":
        return
    context.user_data.clear()
    if not await check_access_gate(update, context):
        return
    await show_user_orders(update, context)


async def show_user_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = eng.get_db().execute(
        """
        SELECT o.id, o.utr, o.status, o.invite_link, o.paper_id, o.amount,
               COALESCE(p.name, 'Batch #' || o.paper_id) AS paper_name,
               COALESCE(p.price, o.amount) AS price, p.channel_id
        FROM orders o LEFT JOIN papers p ON o.paper_id = p.id
        WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 10
        """,
        (user_id,),
    ).fetchall()

    if not rows:
        await ui_render(
            update,
            "📦 <b>My VIP Entry Status</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\nAapne abhi tak Secret Group join nahi kiya hai.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔥 JOIN SECRET GROUP NOW", callback_data="menu_browse")],
                [InlineKeyboardButton("⬅️ Back to Home", callback_data="menu_main")],
            ]),
        )
        return

    lines = ["📦 <b>Your Secret Group Entry Status</b>", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    status_map = {"pending": "⏳ Verification Pending", "accepted": "✅ Approved", "rejected": "❌ Rejected"}
    btns = []
    for r in rows:
        st = status_map.get(r["status"], r["status"])
        paid_amt = r["amount"] if r["amount"] and r["amount"] > 0 else r["price"]
        warns, is_banned = get_leave_status(user_id, r["channel_id"]) if r["channel_id"] else (0, False)
        warn_str = " | 🚫 BANNED (3/3 Leaves)" if is_banned else (f" | ⚠️ Leaves: {warns}/3" if warns > 0 else "")
        line = f"🔹 <b>Order #{r['id']}</b> — {clean(r['paper_name'])} (₹{paid_amt}) | {st}{warn_str}"
        if r["status"] == "accepted" and r["channel_id"] and not is_banned:
            joined = await is_user_in_chat(context.bot, r["channel_id"], user_id)
            if joined:
                open_url = format_private_channel_open_link(r["channel_id"], r["invite_link"] or BRAND_URL)
                line += f"\n   ✅ <a href=\"{clean(open_url)}\">Open Secret Group</a>"
                btns.append([InlineKeyboardButton(f"📂 Open {r['paper_name']}", url=open_url)])
            elif r["invite_link"]:
                line += f"\n   ⚠️ <a href=\"{clean(r['invite_link'])}\">Click Here to Request Join (Auto-Approve)</a>"
                btns.append([InlineKeyboardButton(f"🚀 Request Join: {r['paper_name']}", url=r["invite_link"])])
        lines.append(line)
    btns.append([InlineKeyboardButton("🔄 Refresh Status", callback_data="menu_orders")])
    btns.append([InlineKeyboardButton("⬅️ Back to Home", callback_data="menu_main")])
    await ui_render(update, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


# ── PAY NOW & PROOF SUBMISSION ──
async def pay_paper_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    context.user_data.clear()
    if not await check_access_gate(update, context):
        return

    paper_id = int(query.data.split(":")[1])
    user_id = query.from_user.id
    conn = eng.get_db()
    paper = conn.execute("SELECT * FROM papers WHERE id = ? AND active = 1", (paper_id,)).fetchone()
    pending = conn.execute(
        "SELECT id FROM orders WHERE user_id = ? AND paper_id = ? AND status = 'pending'", (user_id, paper_id)
    ).fetchone()
    active_count = conn.execute("SELECT COUNT(*) FROM papers WHERE active = 1").fetchone()[0]

    if not paper:
        await safe_answer(query, "Yeh batch ab available nahi hai!", show_alert=True)
        return
    warns, is_banned = get_leave_status(user_id, paper["channel_id"])
    if is_banned or warns >= 3 or await is_user_in_chat(context.bot, paper["channel_id"], user_id) or get_strikes(user_id, paper_id) >= 3 or pending:
        await render_paper_view(update, context, paper, single_paper_mode=(active_count == 1))
        return

    qr_file_id, qr_label, qr_pool_id = pick_next_qr(paper)
    if not qr_file_id:
        await safe_answer(query, "QR code set nahi hai! Contact Owner.", show_alert=True)
        return

    final_price, base_price, disc_reason = eng.calculate_student_price(user_id, paper)
    _, _, seats_left = eng.get_fomo_data()
    await safe_answer(query, f"⚡ Scan QR & Pay ₹{final_price}!")
    context.user_data["mode"] = "AWAIT_PROOF"
    context.user_data["proof_paper_id"] = paper_id
    context.user_data["proof_amount"] = final_price
    context.user_data["proof_qr_label"] = qr_label
    context.user_data["proof_qr_pool_id"] = qr_pool_id

    disc_line = f"🎁 <b>Discount Applied:</b> {clean(disc_reason)} (<s>₹{base_price}</s> → <b>₹{final_price}</b>)\n\n" if final_price < base_price else ""
    tpl = eng.get_custom_text("payment_caption")
    caption = (
        tpl.replace("{final_price}", str(final_price))
        .replace("{plan_name}", clean(paper["name"]))
        .replace("{disc_line}", disc_line)
        .replace("{seats_left}", str(seats_left))
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back / Cancel", callback_data=f"view_paper:{paper_id}")]])
    await ui_render(update, caption, reply_markup=kb, photo=qr_file_id)


def build_owner_order_kb(order_id: int, user_id: int, username: str | None) -> InlineKeyboardMarkup:
    chat_url = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accept", callback_data=f"order_act:accept:{order_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"order_act:reject:{order_id}"),
        ],
        [
            InlineKeyboardButton("💬 Direct Chat", url=chat_url),
            InlineKeyboardButton("✉️ Bot Se Msg Bhejo", callback_data=f"adm_dm_user:{user_id}:{order_id}"),
        ],
    ])


async def create_and_dispatch_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    paper_id: int,
    photo_id: str,
    photo_unique_id: str,
    utr: str,
    qr_label: str = "Default QR",
    qr_pool_id: int | None = None,
) -> None:
    msg = update.effective_message
    user = update.effective_user

    ok_rate, wait_s = eng.check_rate_limit(user.id, OWNER_ID, is_photo=True)
    if not ok_rate:
        await msg.reply_text(f"⏳ Kripya {wait_s} seconds wait karein!")
        return

    conn = eng.get_db()
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if not paper:
        await msg.reply_text("⚠️ Batch nahi mila. /start dabayein.")
        context.user_data.clear()
        return

    final_price, base_price, disc_reason = eng.calculate_student_price(user.id, paper)

    scan_res = await eng.analyze_screenshot_async(context.bot, photo_id, final_price, strict_ocr=is_ocr_strict())
    if not scan_res["is_valid"]:
        await msg.reply_text(scan_res["reject_reason"], parse_mode=ParseMode.HTML)
        return

    img_hash = scan_res["img_hash"]
    final_utr = utr if utr != "NO-UTR" else (scan_res["extracted_utr"] or "NO-UTR")

    dup = eng.find_duplicate_order(final_utr, photo_unique_id, img_hash)
    if dup:
        await msg.reply_text(
            f"🚨 <b>Duplicate Payment Screenshot Detected!</b>\n"
            f"Yeh screenshot ya UTR pehle hi <b>Order #{dup['id']}</b> me use ho chuka hai!",
            parse_mode=ParseMode.HTML,
        )
        return

    used_coupon = eng.consume_user_coupon_on_order(user.id, paper_id)
    cur = conn.execute(
        """
        INSERT INTO orders (user_id, paper_id, amount, screenshot_file_id, photo_unique_id, img_hash, utr, qr_label, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (user.id, paper_id, final_price, photo_id, photo_unique_id, img_hash, final_utr, qr_label),
    )
    order_id = cur.lastrowid
    conn.commit()

    increment_qr_usage(qr_pool_id)
    eng.mark_db_dirty()

    await ui_render(
        update,
        f"🎉 <b>Screenshot Verified & Sent for VIP Approval!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Order ID:</b> <code>#{order_id}</code>\n"
        f"👑 <b>Batch:</b> {clean(paper['name'])} (₹{final_price})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <i>Owner verify karte hi aapko <b>Secret Group ka Join-Request Link</b> mil jayega jisme aapki ID <b>Automatically Approve</b> ho jayegi!</i>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Check Entry Status", callback_data="menu_orders")],
            [InlineKeyboardButton("🏠 Back to Home", callback_data="menu_main")],
        ]),
    )

    log_cid = get_log_channel_id() or OWNER_ID
    uname = f"@{clean(user.username)}" if user.username else "No username"
    ocr_badge = f"✅ ₹{final_price} Matched" if scan_res["amount_found"] else f"⚠️ Verify ₹{final_price}"
    utr_disp = f"<code>{clean(final_utr)}</code>" if final_utr != "NO-UTR" else "<i>Not found</i>"
    disc_tag = f"\n🎁 <b>Discount:</b> {clean(disc_reason or used_coupon)}" if final_price < base_price else ""

    log_caption = (
        f"🔔 <b>Secret Group Entry Proof — Order #{order_id}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Student:</b> <a href=\"tg://user?id={user.id}\">{clean(user.full_name)}</a> ({uname})\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"📘 <b>Paper:</b> {clean(paper['name'])} (₹{final_price}){disc_tag}\n"
        f"🎯 <b>Paid On QR:</b> <b>{clean(qr_label)}</b>\n"
        f"🤖 <b>OCR:</b> {ocr_badge} | 🔢 <b>UTR:</b> {utr_disp}"
    )
    await send_order_card_reliably(
        context.bot, log_cid, photo_id, log_caption, build_owner_order_kb(order_id, user.id, user.username), order_id
    )
    context.user_data.clear()


# ── SELF-HEALING ORDER RECOVERY FROM LOG MESSAGE CAPTION ──
def recover_missing_order_from_message(message, order_id: int) -> sqlite3.Row | None:
    if not message:
        return None
    text = message.caption or message.text or ""
    user_id = None
    if message.reply_markup and message.reply_markup.inline_keyboard:
        for row in message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.callback_data and btn.callback_data.startswith("adm_dm_user:"):
                    parts = btn.callback_data.split(":")
                    if len(parts) >= 2 and parts[1].isdigit():
                        user_id = int(parts[1])
    if not user_id:
        m_uid = re.search(r"User ID:\s*(\d+)", text)
        if m_uid:
            user_id = int(m_uid.group(1))
    if not user_id:
        return None

    paper_name, amount = "", 99
    m_paper = re.search(r"Paper:\s*(.+?)\s*\(₹(\d+)\)", text)
    if m_paper:
        paper_name, amount = m_paper.group(1).strip(), int(m_paper.group(2))

    conn = eng.get_db()
    paper_row = conn.execute("SELECT * FROM papers WHERE LOWER(name) LIKE ? LIMIT 1", (f"%{paper_name.lower()}%",)).fetchone() if paper_name else None
    if not paper_row:
        paper_row = conn.execute("SELECT * FROM papers WHERE active = 1 ORDER BY id ASC LIMIT 1").fetchone()
    if not paper_row:
        return None

    photo_id = message.photo[-1].file_id if message.photo else ""
    conn.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, '', ?)", (user_id, f"Student {user_id}"))
    conn.execute(
        "INSERT OR IGNORE INTO orders (id, user_id, paper_id, amount, screenshot_file_id, utr, status) VALUES (?, ?, ?, ?, ?, 'RECOVERED', 'pending')",
        (order_id, user_id, paper_row["id"], amount, photo_id),
    )
    conn.commit()
    return conn.execute(
        """
        SELECT o.*, COALESCE(p.name, 'Secret Group') AS paper_name, p.channel_id, COALESCE(p.price, o.amount, 0) AS price,
               u.username AS u_username, u.full_name AS u_fullname
        FROM orders o LEFT JOIN papers p ON o.paper_id = p.id LEFT JOIN users u ON o.user_id = u.user_id WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()


# ── OWNER ACCEPT / REJECT ──
async def order_decision_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await safe_answer(query, "Unauthorized!", show_alert=True)
        return

    _, action, order_id_str = query.data.split(":")
    order_id = int(order_id_str)
    conn = eng.get_db()
    order = conn.execute(
        """
        SELECT o.*, COALESCE(p.name, 'Batch #' || o.paper_id) AS paper_name, p.channel_id,
               COALESCE(p.price, o.amount, 0) AS price, u.username AS u_username, u.full_name AS u_fullname
        FROM orders o LEFT JOIN papers p ON o.paper_id = p.id LEFT JOIN users u ON o.user_id = u.user_id WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()

    if not order:
        order = recover_missing_order_from_message(query.message, order_id)
    if not order:
        await safe_answer(query, "Order not found.", show_alert=True)
        return

    chat_url = f"https://t.me/{order['u_username']}" if order["u_username"] else f"tg://user?id={order['user_id']}"
    post_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Direct Chat", url=chat_url),
        InlineKeyboardButton("✉️ Bot Se Msg Bhejo", callback_data=f"adm_dm_user:{order['user_id']}:{order_id}"),
    ]])

    if order["status"] != "pending":
        await safe_answer(query, f"Order #{order_id} pehle hi {order['status']} ho chuka hai!", show_alert=True)
        return

    if action == "accept":
        conn.execute("DELETE FROM leave_warns WHERE user_id = ? AND channel_id = ?", (order["user_id"], order["channel_id"]))
        already_joined = await is_user_in_chat(context.bot, order["channel_id"], order["user_id"], use_cache=False)
        invite_url = ""
        if not already_joined:
            try:
                inv = await context.bot.create_chat_invite_link(
                    chat_id=order["channel_id"],
                    creates_join_request=True,
                    expire_date=datetime.now(timezone.utc) + timedelta(days=7),
                    name=f"VIP Order #{order_id} - ID {order['user_id']}",
                )
                invite_url = inv.invite_link
            except TelegramError as e:
                await safe_answer(query, "❌ Invite link error!", show_alert=True)
                await query.message.reply_text(
                    f"⚠️ <b>Invite Link Error:</b> <code>{clean(str(e))}</code>\n"
                    f"👉 Make sure Bot is <b>Admin</b> in the Secret Group channel with <b>Invite Users via Link</b> permission ON!",
                    parse_mode=ParseMode.HTML,
                )
                return

        conn.execute("UPDATE orders SET status = 'accepted', invite_link = ? WHERE id = ?", (invite_url, order_id))
        conn.commit()
        eng.mark_db_dirty()
        await safe_answer(query, "✅ Approved & Auto-Join Link Sent!")

        base_txt = query.message.caption or query.message.text or ""
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=base_txt + "\n\n✅ <b>STATUS: ACCEPTED (Join-Request Link Sent 🔒)</b>", parse_mode=ParseMode.HTML, reply_markup=post_kb)
            else:
                await query.edit_message_text(text=base_txt + "\n\n✅ <b>STATUS: ACCEPTED</b>", parse_mode=ParseMode.HTML, reply_markup=post_kb)
        except TelegramError:
            pass

        open_url = format_private_channel_open_link(order["channel_id"], BRAND_URL) if already_joined else invite_url
        user_msg = (
            f"🥳 <b>Welcome to {clean(order['paper_name'])}! (Order #{order_id} Approved)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔒 <b>Aapka Personal Approval Link:</b>\n{clean(open_url)}\n\n"
            f"⚡ <b>Kaise Join Karein:</b>\n"
            f"1️⃣ Neeche <b>Request to Join Secret Group</b> button pe tap karein.\n"
            f"2️⃣ Bot aapki ID (<code>{order['user_id']}</code>) pehchan kar <b>1 second me Automatically Approve</b> kar dega!\n\n"
            f"⚠️ <b>Strict Warning:</b> Yeh link kisi ko share mat karna (dusri ID auto-decline ho jayegi) aur channel join karne ke baad leave mat karna (3 leaves = Permanent Ban)!"
        )
        u_rows = [[InlineKeyboardButton(f"🚀 Request to Join {order['paper_name']}", url=open_url)]]
        try:
            await context.bot.send_message(chat_id=order["user_id"], text=user_msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(u_rows))
        except TelegramError:
            pass

        asyncio.create_task(send_log_channel_backup(context.bot, reason=f"Order #{order_id} Accepted"))

    elif action == "reject":
        conn.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))
        conn.commit()
        strikes = increment_strikes(order["user_id"], order["paper_id"])
        await safe_answer(query, f"❌ Rejected (Strike {strikes}/3)")
        base_txt = query.message.caption or query.message.text or ""
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=base_txt + f"\n\n❌ <b>REJECTED ({strikes}/3)</b>", parse_mode=ParseMode.HTML, reply_markup=post_kb)
            else:
                await query.edit_message_text(text=base_txt + f"\n\n❌ <b>REJECTED ({strikes}/3)</b>", parse_mode=ParseMode.HTML, reply_markup=post_kb)
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=f"❌ <b>Payment Proof Rejected (Order #{order_id}) — Warning {strikes}/3</b>\nKripya valid payment screenshot bhejein.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def pending_orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    log_cid = get_log_channel_id()
    if not ((user and user.id == OWNER_ID) or (chat and log_cid and chat.id == log_cid)):
        return
    await safe_answer(update.callback_query, "⏳ Fetching pending orders...")
    target_chat_id = chat.id if chat else OWNER_ID

    rows = eng.get_db().execute(
        """
        SELECT o.*, COALESCE(p.name, 'Batch #' || o.paper_id) AS paper_name,
               COALESCE(p.price, o.amount, 0) AS current_price, u.full_name, u.username
        FROM orders o LEFT JOIN papers p ON o.paper_id = p.id LEFT JOIN users u ON o.user_id = u.user_id
        WHERE o.status = 'pending' ORDER BY o.id ASC
        """
    ).fetchall()

    if not rows:
        await context.bot.send_message(chat_id=target_chat_id, text="✅ <b>Zero Pending Orders!</b> 🎉", parse_mode=ParseMode.HTML)
        return

    for r in rows:
        amt = r["amount"] if r["amount"] and r["amount"] > 0 else r["current_price"]
        uname = f"@{clean(r['username'])}" if r["username"] else "No username"
        fname = r["full_name"] or f"User {r['user_id']}"
        caption = (
            f"⏳ <b>PENDING — Order #{r['id']}</b>\n"
            f"👤 <b>Student:</b> <a href=\"tg://user?id={r['user_id']}\">{clean(fname)}</a> ({uname})\n"
            f"🆔 <b>User ID:</b> <code>{r['user_id']}</code>\n"
            f"📘 <b>Paper:</b> {clean(r['paper_name'])} (₹{amt})\n"
            f"🎯 <b>QR:</b> {clean(r['qr_label'])} | 🔢 <b>UTR:</b> <code>{clean(r['utr'])}</code>"
        )
        await send_order_card_reliably(
            context.bot, target_chat_id, r["screenshot_file_id"], caption, build_owner_order_kb(r["id"], r["user_id"], r["username"]), r["id"]
        )
        await asyncio.sleep(0.15)


# ── ADMIN PANEL ──
def admin_menu_kb() -> InlineKeyboardMarkup:
    maint_lbl = "🔴 Maintenance: ON" if is_maintenance_mode() else "🟢 Maintenance: OFF"
    ocr_lbl = "🤖 AI OCR: ON ✅" if is_ocr_strict() else "🤖 AI OCR: OFF ❌"
    proofs_lbl = "🏆 Proofs Btn: ON ✅" if is_proofs_enabled() else "🏆 Proofs Btn: OFF ❌"
    fomo_lbl = "🔥 FOMO Mode: ON ✅" if is_fomo_enabled() else "🔥 FOMO Mode: OFF ❌"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(maint_lbl, callback_data="adm_toggle_maint"),
            InlineKeyboardButton(ocr_lbl, callback_data="adm_toggle_ocr"),
        ],
        [
            InlineKeyboardButton(proofs_lbl, callback_data="adm_toggle_proofs"),
            InlineKeyboardButton(fomo_lbl, callback_data="adm_toggle_fomo"),
        ],
        [InlineKeyboardButton("⏳ View Pending Orders (/pending)", callback_data="adm_show_pending")],
        [
            InlineKeyboardButton("➕ Add Secret Batch", callback_data="adm_add_paper"),
            InlineKeyboardButton("📋 Manage Batches", callback_data="adm_list_papers"),
        ],
        [
            InlineKeyboardButton("🏆 Add/Manage Proofs", callback_data="adm_proofs_menu"),
            InlineKeyboardButton("✏️ Edit Bot Texts/Msgs", callback_data="adm_texts_menu"),
        ],
        [
            InlineKeyboardButton("🎁 Discounts & Coupons", callback_data="adm_disc_menu"),
            InlineKeyboardButton("🔄 Multi-QR Pool", callback_data="adm_qr_pool"),
        ],
        [
            InlineKeyboardButton("📡 Set Log Channel", callback_data="adm_set_log"),
            InlineKeyboardButton("📢 Force-Join Manager", callback_data="adm_fj_menu"),
        ],
        [
            InlineKeyboardButton("📊 Live Store Stats", callback_data="adm_stats"),
            InlineKeyboardButton("📣 Broadcast to All", callback_data="adm_broadcast"),
        ],
        [
            InlineKeyboardButton("🖼️ Fallback QR", callback_data="adm_set_qr"),
            InlineKeyboardButton("☁️ Backup DB Now", callback_data="adm_backup_now"),
        ],
    ])


def admin_home_text() -> str:
    conn = eng.get_db()
    pend_cnt = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'").fetchone()[0]
    p_cnt = conn.execute("SELECT COUNT(*) FROM papers WHERE active = 1").fetchone()[0]
    prf_cnt = conn.execute("SELECT COUNT(*) FROM proofs").fetchone()[0]
    ban_cnt = conn.execute("SELECT COUNT(*) FROM leave_warns WHERE banned = 1").fetchone()[0]
    _, total_vip, seats_left = eng.get_fomo_data()
    return (
        f"🛠️ <b>{clean(BRAND_NAME)} — Render Cloud Panel</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ <b>Pending Proofs:</b> <b>{pend_cnt}</b> | 👑 <b>Active Batches:</b> {p_cnt}\n"
        f"🏆 <b>Proofs Section:</b> {'ON ✅' if is_proofs_enabled() else 'OFF ❌'} ({prf_cnt} uploaded)\n"
        f"🔥 <b>FOMO Counter:</b> {'ON ✅' if is_fomo_enabled() else 'OFF ❌'} ({total_vip} joined, {seats_left} seats left)\n"
        f"🚫 <b>3-Leave Banned Users:</b> {ban_cnt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def render_proofs_admin_menu(update: Update) -> None:
    proofs = eng.get_db().execute("SELECT * FROM proofs ORDER BY id ASC").fetchall()
    st_lbl = "🟢 ON (Visible to Students)" if is_proofs_enabled() else "🔴 OFF (Hidden from Students)"
    lines = [
        "🏆 <b>Last Year 90%+ Proofs & Reviews Manager</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>Visibility Status:</b> {st_lbl}",
        f"<b>Total Proofs Uploaded:</b> {len(proofs)}\n",
    ]
    btns = [
        [InlineKeyboardButton("🔴 Hide Proofs Button" if is_proofs_enabled() else "🟢 Show Proofs Button", callback_data="adm_toggle_proofs")],
    ]
    for idx, pr in enumerate(proofs, start=1):
        short_cap = (pr["caption"] or "")[:28].replace("\n", " ")
        lines.append(f"{idx}. [{pr['media_type'].upper()}] {clean(short_cap)}...")
        btns.append([InlineKeyboardButton(f"🗑️ Delete Proof #{idx}: {short_cap[:16]}", callback_data=f"adm_del_proof:{pr['id']}")])

    btns.append([InlineKeyboardButton("➕ Add New Proof / Result / Review", callback_data="adm_add_proof")])
    if proofs:
        btns.append([InlineKeyboardButton("👁️ Preview Proofs Gallery", callback_data="menu_proofs:0")])
    btns.append([InlineKeyboardButton("⬅️ Back to Admin", callback_data="adm_home")])
    await ui_render(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def render_texts_admin_menu(update: Update) -> None:
    _, total_vip, seats_left = eng.get_fomo_data()
    text = (
        "✏️ <b>Live Bot Messages & FOMO Editor</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <b>Current FOMO Numbers:</b> {total_vip} Joined | {seats_left} Seats Left\n\n"
        "Kisi bhi message ya FOMO counter ko edit karne ke liye neeche click karein:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Welcome Message", callback_data="adm_edit_txt:welcome_text")],
        [InlineKeyboardButton("✏️ Edit Secret Group Details Text", callback_data="adm_edit_txt:group_details_text")],
        [InlineKeyboardButton("✏️ Edit Payment QR Caption", callback_data="adm_edit_txt:payment_caption")],
        [InlineKeyboardButton("✏️ Edit Support Message", callback_data="adm_edit_txt:support_text")],
        [InlineKeyboardButton("🔥 Set FOMO Joined & Seat Numbers", callback_data="adm_edit_fomo_nums")],
        [InlineKeyboardButton("🔄 Reset All Texts to Default", callback_data="adm_reset_txts")],
        [InlineKeyboardButton("⬅️ Back to Admin", callback_data="adm_home")],
    ])
    await ui_render(update, text, reply_markup=kb)


async def render_discounts_menu(update: Update) -> None:
    conn = eng.get_db()
    coupons = conn.execute("SELECT * FROM coupons WHERE active = 1 ORDER BY code ASC").fetchall()
    cross = conn.execute(
        """
        SELECT cd.id, cd.discount_percent, p1.name AS b_name, p2.name AS t_name
        FROM cross_discounts cd JOIN papers p1 ON p1.id = cd.bought_paper_id JOIN papers p2 ON p2.id = cd.target_paper_id
        """
    ).fetchall()

    lines = ["🎁 <b>Discounts, Promo Codes & Auto Cross-Sell</b>", "━━━━━━━━━━━━━━━━━━━━━━━━"]
    btns = []
    for cr in cross:
        lines.append(f"🔗 Bought <b>{clean(cr['b_name'])}</b> ➡️ <b>{cr['discount_percent']}% OFF</b> on <b>{clean(cr['t_name'])}</b>")
        btns.append([InlineKeyboardButton(f"🗑️ Remove: {cr['b_name']} -> {cr['t_name']}", callback_data=f"adm_del_cross:{cr['id']}")])
    for c in coupons:
        lines.append(f"🎟️ <code>{clean(c['code'])}</code> ({c['discount_percent']}% OFF) — Used: {c['used_count']}/{c['max_uses']}")
        btns.append([InlineKeyboardButton(f"🗑️ Delete Coupon: {c['code']}", callback_data=f"adm_del_coup:{c['code']}")])

    btns.append([
        InlineKeyboardButton("➕ Create Promo Code", callback_data="adm_add_coupon"),
        InlineKeyboardButton("🔗 Add Cross-Sell Rule", callback_data="adm_add_cross"),
    ])
    btns.append([InlineKeyboardButton("⬅️ Back to Admin", callback_data="adm_home")])
    await ui_render(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    context.user_data.clear()
    await ui_render(update, admin_home_text(), reply_markup=admin_menu_kb())


async def admin_router_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await safe_answer(query, "Unauthorized", show_alert=True)
        return
    data = query.data
    conn = eng.get_db()

    if data == "adm_home":
        context.user_data.clear()
        await safe_answer(query)
        await ui_render(update, admin_home_text(), reply_markup=admin_menu_kb())

    elif data == "adm_toggle_maint":
        set_setting("maintenance_mode", "0" if is_maintenance_mode() else "1")
        await safe_answer(query, "Maintenance Mode Toggled!", show_alert=True)
        await ui_render(update, admin_home_text(), reply_markup=admin_menu_kb())

    elif data == "adm_toggle_ocr":
        set_setting("ocr_strict", "0" if is_ocr_strict() else "1")
        await safe_answer(query, "AI OCR Filter Toggled!", show_alert=True)
        await ui_render(update, admin_home_text(), reply_markup=admin_menu_kb())

    elif data == "adm_toggle_proofs":
        new_val = "0" if is_proofs_enabled() else "1"
        set_setting("proofs_enabled", new_val)
        await safe_answer(
            query,
            "🏆 Proofs Button is now ON (Visible to students)!" if new_val == "1" else "🏆 Proofs Button is now OFF (Hidden)!",
            show_alert=True,
        )
        await ui_render(update, admin_home_text(), reply_markup=admin_menu_kb())

    elif data == "adm_toggle_fomo":
        new_val = "0" if is_fomo_enabled() else "1"
        set_setting("fomo_enabled", new_val)
        await safe_answer(
            query,
            "🔥 FOMO Urgency Banners ON!" if new_val == "1" else "🔥 FOMO Urgency Banners OFF!",
            show_alert=True,
        )
        await ui_render(update, admin_home_text(), reply_markup=admin_menu_kb())

    elif data == "adm_edit_fomo_nums":
        context.user_data.clear()
        context.user_data["mode"] = "EDIT_FOMO_NUMS"
        await safe_answer(query)
        await ui_render(
            update,
            "🔥 <b>Set FOMO Counter Numbers</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Is format me bhejo: <code>BaseJoinedCount | TotalSeatsCap</code>\n\n"
            "📌 <b>Example:</b> Agar aap dikhana chahte ho ki <code>180+</code> bache join ho chuke hain aur total cap <code>195</code> hai (yani 15 seats left):\n"
            "<code>180 | 195</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_texts_menu")]]),
        )

    elif data == "adm_proofs_menu":
        context.user_data.clear()
        await safe_answer(query)
        await render_proofs_admin_menu(update)

    elif data == "adm_add_proof":
        context.user_data.clear()
        context.user_data["mode"] = "ADD_PROOF"
        await safe_answer(query)
        await ui_render(
            update,
            "🏆 <b>Add Last Year 90%+ Proof / Result / Student Review</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Ab yahan koi bhi <b>Result Photo (Caption ke saath)</b>, <b>Video Review</b>, ya <b>Text Review</b> bhejo!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_proofs_menu")]]),
        )

    elif data.startswith("adm_del_proof:"):
        pid = int(data.split(":")[1])
        conn.execute("DELETE FROM proofs WHERE id = ?", (pid,))
        conn.commit()
        eng.mark_db_dirty()
        await safe_answer(query, "🗑️ Proof Deleted!")
        await render_proofs_admin_menu(update)

    elif data == "adm_texts_menu":
        context.user_data.clear()
        await safe_answer(query)
        await render_texts_admin_menu(update)

    elif data.startswith("adm_edit_txt:"):
        t_key = data.split(":")[1]
        context.user_data.clear()
        context.user_data["mode"] = "EDIT_BOT_TEXT"
        context.user_data["edit_text_key"] = t_key
        cur_val = eng.get_custom_text(t_key)
        await safe_answer(query)
        await ui_render(
            update,
            f"✏️ <b>Editing: <code>{clean(t_key)}</code></b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Current Text:</b>\n{cur_val}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👇 <i>Ab naya message text yahan type karke bhejo (Default pe wapis jaane ke liye <code>default</code> likho):</i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_texts_menu")]]),
        )

    elif data == "adm_reset_txts":
        for k in eng.DEFAULT_TEXTS:
            set_setting(f"txt_{k}", "")
        await safe_answer(query, "✅ All texts reset to default!", show_alert=True)
        await render_texts_admin_menu(update)

    elif data == "adm_disc_menu":
        context.user_data.clear()
        await safe_answer(query)
        await render_discounts_menu(update)

    elif data == "adm_add_coupon":
        context.user_data.clear()
        context.user_data["mode"] = "ADD_COUPON"
        await safe_answer(query)
        await ui_render(
            update,
            "➕ <b>Create Promo Code</b>\nFormat: <code>CODE | Discount% | MaxUses | HoursValid | TargetUserID | PlanID</code>\nExample: <code>SECRET50 | 50 | 20 | 24 | 0 | 0</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_disc_menu")]]),
        )

    elif data == "adm_add_cross":
        context.user_data.clear()
        context.user_data["mode"] = "ADD_CROSS"
        papers = conn.execute("SELECT id, name, price FROM papers ORDER BY id ASC").fetchall()
        p_list = "\n".join([f"• ID <code>{p['id']}</code> = <b>{clean(p['name'])}</b> (₹{p['price']})" for p in papers])
        await safe_answer(query)
        await ui_render(
            update,
            f"🔗 <b>Add Auto Cross-Sell Rule</b>\n{p_list}\n\nFormat: <code>BoughtPlanID | TargetPlanID | Discount%</code>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_disc_menu")]]),
        )

    elif data.startswith("adm_del_coup:"):
        conn.execute("DELETE FROM coupons WHERE code = ?", (data.split(":", 1)[1],))
        conn.commit()
        await render_discounts_menu(update)

    elif data.startswith("adm_del_cross:"):
        conn.execute("DELETE FROM cross_discounts WHERE id = ?", (int(data.split(":")[1]),))
        conn.commit()
        await render_discounts_menu(update)

    elif data == "adm_show_pending":
        await pending_orders_cmd(update, context)

    elif data == "adm_add_paper":
        context.user_data.clear()
        context.user_data["mode"] = "ADD_NAME"
        context.user_data["new_paper"] = {}
        await safe_answer(query)
        await ui_render(
            update,
            "➕ <b>Add Secret Group Batch / Plan</b>\n"
            "Batch ka Naam bhejo (e.g. <code>AVIRANKORA 10th SECRET GROUP</code>)\n\n"
            "⚡ <i>1-Step Shortcut: <code>Name | Extra Details | Price | -100ChannelID</code></i>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_home")]]),
        )

    elif data == "adm_list_papers":
        context.user_data.clear()
        await safe_answer(query)
        papers = conn.execute("SELECT * FROM papers ORDER BY id ASC").fetchall()
        btns = [[InlineKeyboardButton(f"{'🟢' if p['active'] else '🔴'} #{p['id']} {p['name']} — ₹{p['price']}", callback_data=f"adm_paper:{p['id']}")] for p in papers]
        btns.append([InlineKeyboardButton("⬅️ Back to Admin", callback_data="adm_home")])
        await ui_render(update, "📋 <b>All Secret Group Batches/Plans:</b>", reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("adm_paper:"):
        pid = int(data.split(":")[1])
        p = conn.execute("SELECT * FROM papers WHERE id = ?", (pid,)).fetchone()
        if not p:
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔴 Disable" if p["active"] else "🟢 Enable", callback_data=f"adm_toggle:{pid}")],
            [InlineKeyboardButton("✏️ Name", callback_data=f"adm_edit:{pid}:name"), InlineKeyboardButton("✏️ Price", callback_data=f"adm_edit:{pid}:price")],
            [InlineKeyboardButton("✏️ Description", callback_data=f"adm_edit:{pid}:description"), InlineKeyboardButton("✏️ Channel ID", callback_data=f"adm_edit:{pid}:channel_id")],
            [InlineKeyboardButton("👁️ Set Sample Demo", callback_data=f"adm_edit:{pid}:preview"), InlineKeyboardButton("🖼️ Change QR", callback_data=f"adm_edit:{pid}:qr_file_id")],
            [InlineKeyboardButton("⬅️ Back", callback_data="adm_list_papers")],
        ])
        await ui_render(update, f"👑 <b>Plan #{p['id']}: {clean(p['name'])}</b> (₹{p['price']})\nSecret Channel ID: <code>{p['channel_id']}</code>", reply_markup=kb)

    elif data.startswith("adm_toggle:"):
        pid = int(data.split(":")[1])
        conn.execute("UPDATE papers SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END WHERE id = ?", (pid,))
        conn.commit()
        query.data = f"adm_paper:{pid}"
        await admin_router_cb(update, context)

    elif data.startswith("adm_edit:"):
        _, pid_str, field = data.split(":")
        context.user_data["mode"] = "EDIT_FIELD_VALUE"
        context.user_data["edit_pid"] = int(pid_str)
        context.user_data["edit_field"] = field
        await safe_answer(query)
        await ui_render(update, f"✏️ Send new value for <b>{clean(field)}</b>:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_paper:{pid_str}")]]))

    elif data == "adm_qr_pool":
        qrs = conn.execute("SELECT * FROM qr_codes ORDER BY id ASC").fetchall()
        lines = ["🔄 <b>Multi-QR Pool</b>"]
        btns = []
        for q in qrs:
            st = "🟢" if q["active"] else "🔴"
            lines.append(f"{st} #{q['id']} {clean(q['label'])} ({q['today_count']}/{q['daily_limit']} today)")
            btns.append([InlineKeyboardButton(f"{st} {q['label']}", callback_data=f"adm_qr_tog:{q['id']}"), InlineKeyboardButton("🗑️ Del", callback_data=f"adm_qr_del:{q['id']}")])
        btns.append([InlineKeyboardButton("➕ Add QR (Photo + Caption: Name | Limit)", callback_data="adm_qr_pool_add")])
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_home")])
        await ui_render(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(btns))

    elif data == "adm_qr_pool_add":
        context.user_data["mode"] = "ADD_POOL_QR_PHOTO"
        await ui_render(update, "➕ Send QR Photo with caption: <code>FamApp | 20</code>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_qr_pool")]]))

    elif data.startswith("adm_qr_tog:"):
        conn.execute("UPDATE qr_codes SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END WHERE id = ?", (int(data.split(":")[1]),))
        conn.commit()
        query.data = "adm_qr_pool"
        await admin_router_cb(update, context)

    elif data.startswith("adm_qr_del:"):
        conn.execute("DELETE FROM qr_codes WHERE id = ?", (int(data.split(":")[1]),))
        conn.commit()
        query.data = "adm_qr_pool"
        await admin_router_cb(update, context)

    elif data == "adm_set_qr":
        context.user_data["mode"] = "SET_GLOBAL_QR"
        await ui_render(update, "🖼️ Send Fallback UPI QR Photo:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_home")]]))

    elif data == "adm_set_log":
        context.user_data["mode"] = "SET_LOG_CHANNEL"
        await ui_render(update, "📡 Send Log Channel ID (<code>-100...</code>) or forward a message from it:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_home")]]))

    elif data == "adm_fj_menu":
        eng.reload_memory_caches()
        btns = [[InlineKeyboardButton(f"🗑️ {ch['title']}", callback_data=f"adm_fj_del:{ch['id']}")] for ch in eng.FORCE_CHANNELS_CACHE]
        btns.append([InlineKeyboardButton("➕ Add Force-Join Chat", callback_data="adm_fj_add")])
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_home")])
        await ui_render(update, "📢 <b>Force-Join Manager</b>", reply_markup=InlineKeyboardMarkup(btns))

    elif data == "adm_fj_add":
        context.user_data["mode"] = "ADD_FJ_CHANNEL"
        await ui_render(update, "Send: <code>@username | Title | https://t.me/username</code>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_fj_menu")]]))

    elif data.startswith("adm_fj_del:"):
        conn.execute("DELETE FROM force_channels WHERE id = ?", (int(data.split(":")[1]),))
        conn.commit()
        eng.reload_memory_caches()
        eng.FORCE_JOIN_CACHE.clear()
        query.data = "adm_fj_menu"
        await admin_router_cb(update, context)

    elif data == "adm_stats":
        u_cnt = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        sales_cnt = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'accepted'").fetchone()[0]
        pend_cnt = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'").fetchone()[0]
        p_stats = conn.execute(
            """
            SELECT p.name, p.price, COUNT(CASE WHEN o.status='accepted' THEN 1 END) AS sold,
                   COALESCE(SUM(CASE WHEN o.status='accepted' THEN CASE WHEN o.amount>0 THEN o.amount ELSE p.price END END), 0) AS rev
            FROM papers p LEFT JOIN orders o ON p.id = o.paper_id GROUP BY p.id
            """
        ).fetchall()
        tot_rev = sum(r["rev"] for r in p_stats)
        b_txt = "\n".join([f"👑 <b>{clean(r['name'])}</b>: {r['sold']} members = ₹{r['rev']}" for r in p_stats])
        await ui_render(
            update,
            f"📊 <b>Secret Group Live Stats</b>\n👥 Total Users: {u_cnt} | ✅ Paid Members: {sales_cnt}\n💰 Total Revenue: ₹{tot_rev} | ⏳ Pending: {pend_cnt}\n\n{b_txt}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm_home")]]),
        )

    elif data == "adm_broadcast":
        context.user_data["mode"] = "BROADCAST_MSG"
        await ui_render(update, "📣 Send any message/photo to broadcast to all users:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm_home")]]))

    elif data.startswith("adm_dm_user:"):
        _, uid_s, oid_s = data.split(":")
        context.user_data["mode"] = "OWNER_DM_USER"
        context.user_data["dm_target_uid"] = int(uid_s)
        context.user_data["dm_order_id"] = int(oid_s)
        await safe_answer(query, "Type message below!")
        await query.message.reply_text(f"✉️ Send message for User <code>{uid_s}</code>:", parse_mode=ParseMode.HTML)

    elif data == "adm_backup_now":
        await safe_answer(query, "☁️ Backing up...")
        await send_log_channel_backup(context.bot, reason="Manual Backup")
        await ui_render(update, "✅ <b>Backup Sent & Pinned in Log Channel!</b>\n\n" + admin_home_text(), reply_markup=admin_menu_kb())


# ── UNIFIED MESSAGE ROUTER ──
async def unified_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.channel_post and update.channel_post.text and update.channel_post.text.strip().startswith("/pending"):
        await pending_orders_cmd(update, context)
        return

    msg = update.message
    if not msg:
        return
    user = update.effective_user
    mode = context.user_data.get("mode", "")
    conn = eng.get_db()

    if user and user.id == OWNER_ID and mode:
        if mode == "OWNER_DM_USER":
            t_uid = context.user_data.get("dm_target_uid")
            try:
                await context.bot.copy_message(chat_id=t_uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
                await msg.reply_text(f"✅ Sent to <code>{t_uid}</code>!", parse_mode=ParseMode.HTML)
            except TelegramError as e:
                await msg.reply_text(f"❌ Error: {clean(str(e))}")
            context.user_data.clear()
            return

        if msg.chat.type == "private":
            if mode == "EDIT_FOMO_NUMS":
                parts = [p.strip() for p in (msg.text or "").split("|")]
                if len(parts) != 2 or not all(p.isdigit() for p in parts):
                    await msg.reply_text("⚠️ Format: <code>BaseJoined | TotalSeatsCap</code> (e.g. <code>180 | 195</code>)", parse_mode=ParseMode.HTML)
                    return
                set_setting("fomo_base_joined", parts[0])
                set_setting("fomo_seats_cap", parts[1])
                context.user_data.clear()
                await msg.reply_text("✅ <b>FOMO Numbers Updated!</b>", parse_mode=ParseMode.HTML)
                await render_texts_admin_menu(update)
                return

            if mode == "ADD_PROOF":
                if msg.photo:
                    m_type, fid, cap = "photo", msg.photo[-1].file_id, (msg.caption or "🌟 90%+ Student Result Proof!")
                elif msg.video:
                    m_type, fid, cap = "video", msg.video.file_id, (msg.caption or "🌟 Student Video Review!")
                else:
                    m_type, fid, cap = "text", "", (msg.text or "🌟 Student Review")
                conn.execute("INSERT INTO proofs (media_type, file_id, caption) VALUES (?, ?, ?)", (m_type, fid, cap))
                conn.commit()
                eng.mark_db_dirty()
                context.user_data.clear()
                await msg.reply_text("✅ <b>Proof/Review Added to Gallery!</b>", parse_mode=ParseMode.HTML)
                await render_proofs_admin_menu(update)
                return

            if mode == "EDIT_BOT_TEXT":
                t_key = context.user_data.get("edit_text_key", "")
                new_txt = (msg.text or "").strip()
                if new_txt.lower() == "default":
                    set_setting(f"txt_{t_key}", "")
                else:
                    set_setting(f"txt_{t_key}", new_txt)
                context.user_data.clear()
                await msg.reply_text(f"✅ <b>{clean(t_key)} Updated Successfully!</b>", parse_mode=ParseMode.HTML)
                await render_texts_admin_menu(update)
                return

            if mode == "ADD_COUPON":
                parts = [p.strip() for p in (msg.text or "").split("|")]
                if len(parts) < 4 or not parts[1].isdigit() or not parts[2].isdigit() or not parts[3].isdigit():
                    await msg.reply_text("⚠️ Format: <code>CODE | Discount% | MaxUses | HoursValid | TargetUserID | PlanID</code>", parse_mode=ParseMode.HTML)
                    return
                code, pct, max_u, hrs = parts[0].upper(), int(parts[1]), int(parts[2]), int(parts[3])
                t_uid = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else 0
                p_id = int(parts[5]) if len(parts) >= 6 and parts[5].isdigit() else 0
                exp = (datetime.now(timezone.utc) + timedelta(hours=hrs)).isoformat() if hrs > 0 else None
                conn.execute(
                    "INSERT OR REPLACE INTO coupons (code, discount_percent, max_uses, used_count, target_user_id, paper_id, expires_at, active) VALUES (?, ?, ?, 0, ?, ?, ?, 1)",
                    (code, pct, max_u, t_uid, p_id, exp),
                )
                conn.commit()
                eng.mark_db_dirty()
                context.user_data.clear()
                await render_discounts_menu(update)
                return

            if mode == "ADD_CROSS":
                parts = [p.strip() for p in (msg.text or "").split("|")]
                if len(parts) != 3 or not all(p.isdigit() for p in parts):
                    await msg.reply_text("⚠️ Format: <code>BoughtPlanID | TargetPlanID | Discount%</code>", parse_mode=ParseMode.HTML)
                    return
                conn.execute(
                    "INSERT OR REPLACE INTO cross_discounts (bought_paper_id, target_paper_id, discount_percent) VALUES (?, ?, ?)",
                    (int(parts[0]), int(parts[1]), int(parts[2])),
                )
                conn.commit()
                eng.mark_db_dirty()
                context.user_data.clear()
                await render_discounts_menu(update)
                return

            if mode == "BROADCAST_MSG":
                context.user_data.clear()
                users = conn.execute("SELECT user_id FROM users").fetchall()
                st_m = await msg.reply_text(f"🚀 Broadcasting to {len(users)} users...")
                sent = 0
                for u in users:
                    try:
                        await context.bot.copy_message(chat_id=u["user_id"], from_chat_id=msg.chat_id, message_id=msg.message_id, reply_markup=main_menu_kb())
                        sent += 1
                    except RetryAfter as e:
                        await asyncio.sleep(e.retry_after + 1.0)
                    except TelegramError:
                        pass
                    await asyncio.sleep(0.035)
                await st_m.edit_text(f"🎉 Broadcast Done! Delivered: {sent}/{len(users)}", reply_markup=admin_menu_kb())
                return

            if mode == "ADD_NAME":
                raw = ((msg.caption if msg.photo else msg.text) or "").strip()
                if "|" in raw:
                    parts = [p.strip() for p in raw.split("|")]
                    if len(parts) == 4 and parts[2].isdigit():
                        qr_id = msg.photo[-1].file_id if msg.photo else ""
                        conn.execute("INSERT INTO papers (name, description, price, qr_file_id, channel_id, active) VALUES (?, ?, ?, ?, ?, 1)", (parts[0], parts[1], int(parts[2]), qr_id, int(parts[3])))
                        conn.commit()
                        context.user_data.clear()
                        await send_log_channel_backup(context.bot, reason=f"Added Batch: {parts[0]}")
                        await msg.reply_text("✅ Secret Group Batch Added!", reply_markup=admin_menu_kb())
                        return
                context.user_data["new_paper"] = {"name": raw}
                context.user_data["mode"] = "ADD_DESC"
                await msg.reply_text("Step 2: Send Extra Details/Description:")
                return

            if mode == "ADD_DESC":
                context.user_data.setdefault("new_paper", {})["description"] = (msg.text or "").strip()
                context.user_data["mode"] = "ADD_PRICE"
                await msg.reply_text("Step 3: Send Entry Fee (₹):")
                return

            if mode == "ADD_PRICE":
                context.user_data.setdefault("new_paper", {})["price"] = int((msg.text or "99").strip())
                context.user_data["mode"] = "ADD_CHANNEL"
                await msg.reply_text("Step 4: Send Secret Channel ID (-100...):")
                return

            if mode == "ADD_CHANNEL":
                np = context.user_data.get("new_paper", {})
                conn.execute("INSERT INTO papers (name, description, price, channel_id, active) VALUES (?, ?, ?, ?, 1)", (np.get("name", "Secret Group"), np.get("description", ""), np.get("price", 99), int((msg.text or "").strip())))
                conn.commit()
                context.user_data.clear()
                await send_log_channel_backup(context.bot, reason="Created Secret Batch")
                await msg.reply_text("🎉 Secret Group Plan Created!", reply_markup=admin_menu_kb())
                return

            if mode == "EDIT_FIELD_VALUE":
                pid, field = context.user_data.get("edit_pid"), context.user_data.get("edit_field")
                if field == "preview":
                    pfid = msg.photo[-1].file_id if msg.photo else (msg.document.file_id if msg.document else "")
                    ptype = "photo" if msg.photo else ("document" if msg.document else "")
                    conn.execute("UPDATE papers SET preview_file_id = ?, preview_type = ? WHERE id = ?", (pfid, ptype, pid))
                elif field == "qr_file_id":
                    conn.execute("UPDATE papers SET qr_file_id = ? WHERE id = ?", (msg.photo[-1].file_id if msg.photo else "", pid))
                else:
                    val = int(msg.text.strip()) if field in ("price", "channel_id") else msg.text.strip()
                    conn.execute(f"UPDATE papers SET {field} = ? WHERE id = ?", (val, pid))
                conn.commit()
                context.user_data.clear()
                await send_log_channel_backup(context.bot, reason=f"Edited Plan #{pid}")
                await msg.reply_text("✅ Updated!", reply_markup=admin_menu_kb())
                return

            if mode == "ADD_POOL_QR_PHOTO" and msg.photo:
                cap = (msg.caption or "QR | 25").split("|")
                lbl = cap[0].strip()
                lim = int(cap[1].strip()) if len(cap) > 1 and cap[1].strip().isdigit() else 25
                conn.execute("INSERT INTO qr_codes (label, file_id, daily_limit, last_reset_date, active) VALUES (?, ?, ?, ?, 1)", (lbl, msg.photo[-1].file_id, lim, eng.today_ist_str()))
                conn.commit()
                context.user_data.clear()
                await send_log_channel_backup(context.bot, reason=f"Added Pool QR {lbl}")
                await msg.reply_text("✅ Pool QR Added!", reply_markup=admin_menu_kb())
                return

            if mode == "SET_GLOBAL_QR" and msg.photo:
                set_setting("global_qr", msg.photo[-1].file_id)
                context.user_data.clear()
                await send_log_channel_backup(context.bot, reason="Updated Fallback QR")
                await msg.reply_text("✅ Global QR Updated!", reply_markup=admin_menu_kb())
                return

            if mode == "SET_LOG_CHANNEL":
                cid = str(msg.forward_origin.chat.id) if getattr(msg, "forward_origin", None) and getattr(msg.forward_origin, "chat", None) else (msg.text or "").strip()
                set_setting("log_channel_id", cid)
                context.user_data.clear()
                await send_log_channel_backup(context.bot, reason="Connected Log Channel")
                await msg.reply_text(f"✅ Log Channel Set & Backed Up: <code>{clean(cid)}</code>", parse_mode=ParseMode.HTML, reply_markup=admin_menu_kb())
                return

            if mode == "ADD_FJ_CHANNEL":
                parts = [p.strip() for p in (msg.text or "").split("|")]
                if len(parts) == 3:
                    conn.execute("INSERT OR REPLACE INTO force_channels (chat_id, title, invite_url) VALUES (?, ?, ?)", (parts[0], parts[1], parts[2]))
                    conn.commit()
                    eng.reload_memory_caches()
                    eng.FORCE_JOIN_CACHE.clear()
                context.user_data.clear()
                await send_log_channel_backup(context.bot, reason="Updated Force Join")
                await msg.reply_text("✅ Force-Join Updated!", reply_markup=admin_menu_kb())
                return

    if msg.chat.type != "private":
        return
    if not await check_access_gate(update, context):
        return

    if mode == "AWAIT_COUPON":
        code = (msg.text or "").strip().upper()
        paper_id = context.user_data.get("coupon_paper_id", 0)
        ok, status_msg, _ = eng.validate_coupon(code, user.id, paper_id)
        if not ok:
            await msg.reply_text(status_msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"view_paper:{paper_id}")]]))
            return
        conn.execute("INSERT OR REPLACE INTO user_applied_coupons (user_id, paper_id, code) VALUES (?, ?, ?)", (user.id, paper_id, code))
        conn.commit()
        context.user_data.clear()
        await msg.reply_text(f"🎉 <b>{status_msg}</b>", parse_mode=ParseMode.HTML)
        paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if paper:
            await render_paper_view(update, context, paper)
        return

    if mode == "AWAIT_PROOF" and msg.photo:
        photo_obj = msg.photo[-1]
        caption_utr = extract_utr_from_text(msg.caption or "") or "NO-UTR"
        await create_and_dispatch_order(
            update, context, context.user_data.get("proof_paper_id"), photo_obj.file_id, photo_obj.file_unique_id,
            caption_utr, context.user_data.get("proof_qr_label", "Default QR"), context.user_data.get("proof_qr_pool_id")
        )
        return

    if msg.photo:
        papers = conn.execute("SELECT * FROM papers WHERE active = 1 ORDER BY id ASC").fetchall()
        if len(papers) == 1:
            await create_and_dispatch_order(
                update, context, papers[0]["id"], msg.photo[-1].file_id, msg.photo[-1].file_unique_id,
                extract_utr_from_text(msg.caption or "") or "NO-UTR", "Direct Screenshot"
            )
            return
        elif len(papers) > 1:
            context.user_data["random_photo_id"] = msg.photo[-1].file_id
            context.user_data["random_photo_unique_id"] = msg.photo[-1].file_unique_id
            context.user_data["random_photo_utr"] = extract_utr_from_text(msg.caption or "") or "NO-UTR"
            btns = [[InlineKeyboardButton(f"👑 {p['name']}", callback_data=f"quick_claim:{p['id']}")] for p in papers]
            await msg.reply_text("📸 <b>Screenshot Mil Gaya!</b> Yeh kis Secret Batch ke liye hai? 👇", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(btns))
            return

    await ui_render(update, render_welcome_message(user.first_name), reply_markup=main_menu_kb())


async def quick_claim_paper_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    paper_id = int(query.data.split(":")[1])
    photo_id = context.user_data.get("random_photo_id")
    if not photo_id:
        await safe_answer(query, "Screenshot expire ho gaya, dubara bhejein!", show_alert=True)
        return
    await safe_answer(query, "🤖 Scanning screenshot...")
    await create_and_dispatch_order(
        update, context, paper_id, photo_id, context.user_data.get("random_photo_unique_id", ""),
        context.user_data.get("random_photo_utr", "NO-UTR"), "Quick-Claimed Screenshot"
    )


async def manual_restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    msg = update.effective_message
    reply = msg.reply_to_message if msg else None
    if not reply or not reply.document or not reply.document.file_name.endswith(".db"):
        await msg.reply_text("ℹ️ Kisi bhi backup <code>.db</code> file pe reply karke <code>/restore</code> bhejo!", parse_mode=ParseMode.HTML)
        return
    eng.close_db()
    tg_file = await context.bot.get_file(reply.document.file_id)
    await tg_file.download_to_drive(custom_path=eng.DB_FILE)
    eng.init_db()
    await msg.reply_text("✅ <b>Database Successfully Restored!</b>", parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, (Forbidden, Conflict)):
        return
    if isinstance(context.error, BadRequest) and "Query is too old" in str(context.error):
        return
    logger.error("Unhandled exception:", exc_info=context.error)


async def post_init_hook(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=False)
    await auto_restore_from_log_channel(application.bot)
    if application.job_queue:
        application.job_queue.run_repeating(
            lambda ctx: send_log_channel_backup(ctx.bot, "Periodic Cloud Sync") if eng.dirty_flag else None,
            interval=600,
            first=600,
        )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("❌ BOT_TOKEN environment variable is not set in Render!")

    start_health_server()
    eng.init_db()

    t_req = HTTPXRequest(connection_pool_size=64, read_timeout=20.0, write_timeout=20.0, connect_timeout=15.0, pool_timeout=15.0)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(t_req)
        .get_updates_request(HTTPXRequest(connection_pool_size=16, read_timeout=30.0))
        .concurrent_updates(64)
        .post_init(post_init_hook)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("myorders", myorders_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("pending", pending_orders_cmd))
    app.add_handler(CommandHandler("restore", manual_restore_cmd))
    app.add_handler(CommandHandler("cancel", start_cmd))

    app.add_handler(ChatJoinRequestHandler(handle_secret_join_request))
    app.add_handler(ChatMemberHandler(handle_secret_member_update, ChatMemberHandler.CHAT_MEMBER))

    app.add_handler(CallbackQueryHandler(verify_join_cb, pattern=r"^verify_join$"))
    app.add_handler(CallbackQueryHandler(check_paper_join_cb, pattern=r"^chk_paper_join:\d+$"))
    app.add_handler(CallbackQueryHandler(quick_claim_paper_cb, pattern=r"^quick_claim:\d+$"))
    app.add_handler(CallbackQueryHandler(menu_router_cb, pattern=r"^menu_"))
    app.add_handler(CallbackQueryHandler(view_paper_cb, pattern=r"^view_paper:\d+$"))
    app.add_handler(CallbackQueryHandler(apply_code_start_cb, pattern=r"^apply_code:\d+$"))
    app.add_handler(CallbackQueryHandler(preview_paper_cb, pattern=r"^preview_paper:\d+$"))
    app.add_handler(CallbackQueryHandler(pay_paper_start_cb, pattern=r"^pay_paper:\d+$"))
    app.add_handler(CallbackQueryHandler(order_decision_cb, pattern=r"^order_act:(accept|reject):\d+$"))
    app.add_handler(CallbackQueryHandler(admin_router_cb, pattern=r"^adm_"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, unified_message_router))
    app.add_error_handler(error_handler)

    print("🚀 AVIRANKORA SECRET GROUP BOT (Render 512MB Optimized + FOMO + Proofs Toggle) IS LIVE!")
    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=["message", "callback_query", "channel_post", "chat_join_request", "chat_member"],
    )


if __name__ == "__main__":
    main()
