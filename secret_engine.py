import os
import re
import gc
import time
import html
import sqlite3
import logging
import asyncio
from io import BytesIO
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageOps, ImageEnhance
import pytesseract

BRAND_NAME = os.environ.get("BRAND_NAME", "AVIRANKORA SECRET GROUP")
BRAND_URL = os.environ.get("BRAND_URL", "https://t.me/avirankora")
DEFAULT_FORCE_JOIN_HANDLE = os.environ.get("FORCE_JOIN_HANDLE", "@avirankora")
DEFAULT_LOG_CHANNEL = os.environ.get("LOG_CHANNEL_ID", "")
DB_FILE = "avirankora_secret.db"

logger = logging.getLogger(__name__)

# ── HIGH-CONVERTING FOMO DEFAULT TEXT TEMPLATES ──
DEFAULT_TEXTS = {
    "welcome_text": (
        "🚨 <b>Welcome to {brand_name}, {first_name}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 <b>Maharashtra Board Class 9th & 10th ka Sabse Secret VIP Group!</b>\n\n"
        "💎 <b>Secret Group Me Kya Milega:</b>\n"
        "✅ All Subject Boards <b>100% Confirmed Questions</b>\n"
        "✅ All <b>5 Sets</b> of Original Board Pattern Papers\n"
        "✅ Most IMP Questions & Last-Minute Leak Target Notes\n"
        "✅ <b>24/7 Direct Call Support</b> (Koi bhi doubt ho, seedha call karo!)\n\n"
        "🎯 <i>Jo student sirf humara diya hua material padh lega, wo aaram se <b>90%+ Score</b> karega! Pichle saal 100+ students ne yahi se 90%+ phoda tha!</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "{fomo_banner}\n"
        "👇 <i>Seats full hone se pehle abhi apni VIP Entry book karo:</i>"
    ),
    "group_details_text": (
        "👑 <b>{plan_name}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "{price_line}"
        "🔓 <b>Entry Type:</b> Instant Admin-Approved VIP Entry\n\n"
        "💎 <b>Inside Secret Group Benefits:</b>\n"
        "• All Subject Boards 100% Confirmed Questions\n"
        "• All 5 Sets of Original Boards Papers\n"
        "• Most IMP Questions & Target Notes\n"
        "• 24/7 Direct Call Support for Doubts\n\n"
        "📝 <b>Batch Details:</b>\n{description}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "{fomo_banner}\n"
        "⚠️ <i>Strict Rule: Group join karne ke baad leave mat karna (3 leaves = Permanent Auto-Ban).</i>"
    ),
    "payment_caption": (
        "💳 <b>Pay ₹{final_price} for {plan_name}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "{disc_line}"
        "⏳ <b>HURRY UP!</b> Is price par sirf <b>{seats_left} seats</b> bachi hain!\n\n"
        "1️⃣ Upar diye QR code ko scan karke <b>₹{final_price}</b> pay karo.\n"
        "2️⃣ Payment hote hi <b>seedha yahan Payment Screenshot (Photo) bhej do!</b> 📸\n\n"
        "🔒 <i>Verify hote hi bot aapko Secret Group ka <b>Personal Approval Link</b> dega aur aapki ID ko <b>Automatically Approve</b> kar dega!</i>"
    ),
    "support_text": (
        "💬 <b>24/7 VIP Support & Help Center</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Agar aapko Secret Group join karne ya payment me koi bhi doubt hai, toh seedha niche button daba ke Owner ko message karein:\n\n"
        "👤 <b>Owner:</b> {owner_username}\n"
        "📢 <b>Official Channel:</b> {brand_url}\n\n"
        "<i>Paid members ko Secret Group ke andar 24/7 Direct Call Support number milega! 📞</i>"
    ),
}

# ── RAM CACHES & STEALTH RATE LIMITER ──
FORCE_JOIN_CACHE: dict[int, float] = {}
CHANNEL_MEMBER_CACHE: dict[tuple[int, str], tuple[bool, float]] = {}
SETTINGS_CACHE: dict[str, str] = {}
FORCE_CHANNELS_CACHE: list[dict] = []
KNOWN_USERS_CACHE: dict[int, tuple[str, str]] = {}

_USER_CLICKS: dict[int, list[float]] = {}
_USER_PHOTOS: dict[int, list[float]] = {}


def check_rate_limit(user_id: int, owner_id: int, is_photo: bool = False) -> tuple[bool, int]:
    if user_id == owner_id:
        return True, 0
    now = time.monotonic()
    if is_photo:
        hist = [t for t in _USER_PHOTOS.get(user_id, []) if now - t < 60.0]
        if len(hist) >= 4:
            return False, int(60 - (now - hist[0]))
        hist.append(now)
        _USER_PHOTOS[user_id] = hist
        return True, 0
    else:
        hist = [t for t in _USER_CLICKS.get(user_id, []) if now - t < 10.0]
        if len(hist) >= 12:
            return False, 5
        hist.append(now)
        _USER_CLICKS[user_id] = hist
        return True, 0


# ── ULTRA-LIGHTWEIGHT SQLITE LAYER (OPTIMIZED FOR 512MB RAM) ──
dirty_flag = False
_DB_CONN: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _DB_CONN
    if _DB_CONN is None:
        _DB_CONN = sqlite3.connect(DB_FILE, timeout=30.0, check_same_thread=False)
        _DB_CONN.row_factory = sqlite3.Row
        _DB_CONN.execute("PRAGMA journal_mode=WAL;")
        _DB_CONN.execute("PRAGMA synchronous=NORMAL;")
        _DB_CONN.execute("PRAGMA cache_size=-8000;")  # 8MB RAM cap for Render 512MB limit
        _DB_CONN.execute("PRAGMA temp_store=MEMORY;")
    return _DB_CONN


def close_db() -> None:
    global _DB_CONN
    if _DB_CONN is not None:
        try:
            _DB_CONN.close()
        except Exception:
            pass
        _DB_CONN = None


def count_orders_in_db(path: str) -> int:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return -1
    try:
        c = sqlite3.connect(path)
        cnt = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()[0]
        c.close()
        return max(cnt, max_id)
    except Exception:
        return -1


def mark_db_dirty() -> None:
    global dirty_flag
    dirty_flag = True


def today_ist_str() -> str:
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")


def reload_memory_caches() -> None:
    global SETTINGS_CACHE, FORCE_CHANNELS_CACHE
    conn = get_db()
    SETTINGS_CACHE = {r["key"]: (r["value"] or "") for r in conn.execute("SELECT key, value FROM settings").fetchall()}
    FORCE_CHANNELS_CACHE = [dict(r) for r in conn.execute("SELECT * FROM force_channels ORDER BY id ASC").fetchall()]


def get_custom_text(key: str) -> str:
    val = SETTINGS_CACHE.get(f"txt_{key}", "").strip()
    return val if val else DEFAULT_TEXTS.get(key, "")


# ── LIVE FOMO ENGINE (DYNAMIC SEATS & SOCIAL PROOF COUNTER) ──
def get_fomo_data() -> tuple[str, int, int]:
    """
    Returns (fomo_banner_html, total_vip_joined, seats_left).
    Automatically increases 'joined' count & decreases 'seats left' as real orders come in!
    """
    conn = get_db()
    real_sales = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'accepted'").fetchone()[0]
    base_fake_joined = int(SETTINGS_CACHE.get("fomo_base_joined", "142") or "142")
    max_seats_cap = int(SETTINGS_CACHE.get("fomo_seats_cap", "160") or "160")

    total_joined = base_fake_joined + real_sales
    seats_left = max(3, max_seats_cap - total_joined)  # Never hits 0 so students can still buy, stays urgent (3-18 left)

    if SETTINGS_CACHE.get("fomo_enabled", "1") != "1":
        return "", total_joined, seats_left

    banner = (
        f"🔥 <b>LIVE BATCH STATUS:</b>\n"
        f"├ 👥 <b>{total_joined}+ Students</b> Already Joined VIP!\n"
        f"├ ⚠️ Only <b>{seats_left} Seats Left</b> at Current Discount Price!\n"
        f"└ 📈 <i>Seats full hote hi Entry Fee double ho jayegi!</i>"
    )
    return banner, total_joined, seats_left


def init_db() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS force_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            invite_url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS qr_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            file_id TEXT NOT NULL,
            daily_limit INTEGER DEFAULT 25,
            today_count INTEGER DEFAULT 0,
            last_reset_date TEXT DEFAULT '',
            total_used INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            price INTEGER NOT NULL,
            qr_file_id TEXT DEFAULT '',
            preview_file_id TEXT DEFAULT '',
            preview_type TEXT DEFAULT '',
            channel_id INTEGER NOT NULL,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            paper_id INTEGER NOT NULL,
            amount INTEGER DEFAULT 0,
            screenshot_file_id TEXT NOT NULL,
            photo_unique_id TEXT DEFAULT '',
            img_hash TEXT DEFAULT '',
            utr TEXT NOT NULL,
            qr_label TEXT DEFAULT 'Default QR',
            status TEXT DEFAULT 'pending',
            invite_link TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS paper_strikes (
            user_id INTEGER NOT NULL,
            paper_id INTEGER NOT NULL,
            strikes INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, paper_id)
        );
        CREATE TABLE IF NOT EXISTS leave_warns (
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            warns INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, channel_id)
        );
        CREATE TABLE IF NOT EXISTS proofs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT NOT NULL,
            file_id TEXT DEFAULT '',
            caption TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS coupons (
            code TEXT PRIMARY KEY,
            discount_percent INTEGER DEFAULT 0,
            discount_flat INTEGER DEFAULT 0,
            paper_id INTEGER DEFAULT 0,
            target_user_id INTEGER DEFAULT 0,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            expires_at TIMESTAMP,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS coupon_usages (
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, user_id)
        );
        CREATE TABLE IF NOT EXISTS cross_discounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bought_paper_id INTEGER NOT NULL,
            target_paper_id INTEGER NOT NULL,
            discount_percent INTEGER DEFAULT 50,
            UNIQUE(bought_paper_id, target_paper_id)
        );
        CREATE TABLE IF NOT EXISTS user_applied_coupons (
            user_id INTEGER NOT NULL,
            paper_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            PRIMARY KEY (user_id, paper_id)
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_user_paper ON orders(user_id, paper_id);
        CREATE INDEX IF NOT EXISTS idx_orders_imghash ON orders(img_hash);
        """
    )

    if DEFAULT_LOG_CHANNEL:
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('log_channel_id', ?)", (str(DEFAULT_LOG_CHANNEL),))

    # Default switches (Proofs Toggle, FOMO Toggle, OCR Toggle, Maintenance Toggle)
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_mode', '0')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ocr_strict', '1')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('proofs_enabled', '1')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fomo_enabled', '1')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fomo_base_joined', '142')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fomo_seats_cap', '160')")

    seeded = conn.execute("SELECT value FROM settings WHERE key = 'fj_seeded'").fetchone()
    if not seeded:
        conn.execute(
            "INSERT OR IGNORE INTO force_channels (chat_id, title, invite_url) VALUES (?, ?, ?)",
            (DEFAULT_FORCE_JOIN_HANDLE, "Avirankora Official", BRAND_URL),
        )
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fj_seeded', '1')")
    conn.commit()
    reload_memory_caches()


# ── DISCOUNT & AUTO CROSS-SELL ENGINE ──
def calculate_student_price(user_id: int, paper: sqlite3.Row) -> tuple[int, int, str]:
    base_price = int(paper["price"])
    best_price = base_price
    reason = ""
    conn = get_db()

    cross_rules = conn.execute(
        """
        SELECT cd.discount_percent, p.name AS bought_name
        FROM cross_discounts cd
        JOIN orders o ON o.paper_id = cd.bought_paper_id AND o.user_id = ? AND o.status = 'accepted'
        JOIN papers p ON p.id = cd.bought_paper_id
        WHERE cd.target_paper_id = ?
        ORDER BY cd.discount_percent DESC LIMIT 1
        """,
        (user_id, paper["id"]),
    ).fetchone()

    if cross_rules:
        pct = cross_rules["discount_percent"]
        cand = max(1, int(round(base_price * (100 - pct) / 100.0)))
        if cand < best_price:
            best_price = cand
            reason = f"🎁 Auto {pct}% OFF (VIP Combo Discount!)"

    app_row = conn.execute(
        "SELECT code FROM user_applied_coupons WHERE user_id = ? AND paper_id IN (?, 0)",
        (user_id, paper["id"]),
    ).fetchone()
    if app_row:
        ok, msg, c_row = validate_coupon(app_row["code"], user_id, paper["id"])
        if ok and c_row:
            if c_row["discount_percent"] > 0:
                cand = max(1, int(round(base_price * (100 - c_row["discount_percent"]) / 100.0)))
                lbl = f"🏷️ Promo {c_row['code']} ({c_row['discount_percent']}% OFF)"
            else:
                cand = max(1, base_price - c_row["discount_flat"])
                lbl = f"🏷️ Promo {c_row['code']} (₹{c_row['discount_flat']} OFF)"
            if cand <= best_price:
                best_price = cand
                reason = lbl
        else:
            conn.execute("DELETE FROM user_applied_coupons WHERE user_id = ? AND code = ?", (user_id, app_row["code"]))
            conn.commit()

    return best_price, base_price, reason


def validate_coupon(code: str, user_id: int, paper_id: int) -> tuple[bool, str, sqlite3.Row | None]:
    code = code.strip().upper()
    conn = get_db()
    c = conn.execute("SELECT * FROM coupons WHERE UPPER(code) = ? AND active = 1", (code,)).fetchone()
    if not c:
        return False, "❌ Invalid ya expired promo code!", None
    if c["expires_at"]:
        try:
            exp_dt = datetime.fromisoformat(c["expires_at"])
            if datetime.now(timezone.utc) > exp_dt:
                return False, "⏰ Yeh promo code expire ho chuka hai!", None
        except Exception:
            pass
    if c["max_uses"] > 0 and c["used_count"] >= c["max_uses"]:
        return False, "🚫 Is promo code ki limit poori ho chuki hai! (All seats claimed)", None
    if c["target_user_id"] != 0 and c["target_user_id"] != user_id:
        return False, "🔒 Yeh special code sirf ek specific student ke liye hai!", None
    if c["paper_id"] != 0 and c["paper_id"] != paper_id:
        return False, "⚠️ Yeh code is batch ke liye valid nahi hai!", None
    used = conn.execute("SELECT 1 FROM coupon_usages WHERE UPPER(code) = ? AND user_id = ?", (code, user_id)).fetchone()
    if used:
        return False, "⚠️ Aap yeh promo code pehle hi use kar chuke ho!", None
    return True, "✅ Promo Code Applied Successfully!", c


def consume_user_coupon_on_order(user_id: int, paper_id: int) -> str:
    conn = get_db()
    row = conn.execute(
        "SELECT code FROM user_applied_coupons WHERE user_id = ? AND paper_id IN (?, 0)",
        (user_id, paper_id),
    ).fetchone()
    if not row:
        return ""
    code = row["code"].upper()
    ok, _, _ = validate_coupon(code, user_id, paper_id)
    conn.execute("DELETE FROM user_applied_coupons WHERE user_id = ?", (user_id,))
    if ok:
        conn.execute("INSERT OR IGNORE INTO coupon_usages (code, user_id) VALUES (?, ?)", (code, user_id))
        conn.execute("UPDATE coupons SET used_count = used_count + 1 WHERE UPPER(code) = ?", (code,))
        conn.commit()
        return code
    conn.commit()
    return ""


# ── 512MB RAM OPTIMIZED SMART OCR + PERCEPTUAL dHASH ENGINE ──
def compute_dhash(img: Image.Image, hash_size: int = 8) -> str:
    gray = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
    pixels = list(gray.getdata())
    diff = []
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            diff.append("1" if left > right else "0")
    return hex(int("".join(diff), 2))[2:].zfill(16)


def hamming_distance(h1: str, h2: str) -> int:
    if not h1 or not h2 or len(h1) != len(h2):
        return 99
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def _analyze_screenshot_sync(image_bytes: bytes, expected_amount: int, strict_ocr: bool = True) -> dict:
    """
    Ultra-Fast & Low-RAM OCR Scanner:
    1. Downscales large images to max 1000px & converts to high-contrast grayscale (uses <15MB RAM!).
    2. Extracts UPI Ref/UTR (handles normal 12-digit, spaced 4+4+4 digits, and Paytm/FamApp IDs).
    3. Verifies payment keywords & blocks old years (2019-2025).
    4. Runs gc.collect() immediately to keep Render 512MB RAM crystal clean.
    """
    try:
        with Image.open(BytesIO(image_bytes)) as raw_img:
            img_hash = compute_dhash(raw_img)
            # Downscale to max 1000px for 3x faster Tesseract execution & minimal RAM usage
            w, h = raw_img.size
            if max(w, h) > 1000:
                scale = 1000.0 / max(w, h)
                proc_img = raw_img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)
            else:
                proc_img = raw_img.copy()

            # Convert to high-contrast grayscale so dark-mode & light-mode receipts read accurately
            gray_img = ImageOps.grayscale(proc_img)
            sharp_img = ImageEnhance.Contrast(gray_img).enhance(1.6)

            try:
                ocr_text = pytesseract.image_to_string(sharp_img, config="--oem 1 --psm 6")
            except Exception as e:
                logger.warning(f"OCR warning: {e}")
                ocr_text = ""
    finally:
        gc.collect()

    low = ocr_text.lower()
    payment_keywords = [
        "paid", "payment", "successful", "success", "completed", "upi", "utr",
        "transaction", "sent", "to", "from", "google pay", "phonepe", "paytm",
        "fam", "bharatpe", "bank", "ref", "id", "debited", "credited", "₹", "rs",
        "wallet", "transfer", "done"
    ]
    matched_kw = [kw for kw in payment_keywords if kw in low]

    # Smart UTR extraction (handles 12-digit continuous or spaced digits)
    extracted_utr = ""
    utr_matches = re.findall(r"\b\d{12}\b", ocr_text)
    if utr_matches:
        extracted_utr = utr_matches[0]
    else:
        spaced = re.findall(r"\b(?:\d{4}\s+\d{4}\s+\d{4}|\d{6}\s+\d{6})\b", ocr_text)
        if spaced:
            extracted_utr = re.sub(r"\s+", "", spaced[0])

    # Smart amount verification (checks e.g. '49', '₹49', '49.00')
    amt_str = str(expected_amount)
    amount_found = bool(re.search(rf"(?:₹|rs\.?|inr|\b)\s*{re.escape(amt_str)}(?:\.00|\b)", low)) or (amt_str in ocr_text)

    # Check for fake old years (2019-2025)
    old_year_match = re.search(r"\b(2019|2020|2021|2022|2023|2024|2025)\b", ocr_text)
    old_year = old_year_match.group(1) if old_year_match else ""

    is_valid = True
    reject_reason = ""

    if strict_ocr:
        if len(matched_kw) < 2 and not extracted_utr:
            is_valid = False
            reject_reason = (
                "❌ <b>Invalid Payment Screenshot!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Hamare AI Scanner ko is photo me koi valid UPI Payment details nahi mili!\n\n"
                "👉 <i>Kripya apna asli Payment App (GPay, PhonePe, Paytm, FamApp) ka clear screenshot bhejein jisme Amount aur Transaction ID saaf dikhe.</i>"
            )
        elif old_year:
            is_valid = False
            reject_reason = (
                f"🚨 <b>Old/Fake Screenshot Detected ({old_year})!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Is screenshot me purani date (<b>{old_year}</b>) dikh rahi hai!\n\n"
                f"⚠️ <i>Fake screenshot bhejne par bot aapko permanent block kar dega. Kripya aaj ka original payment screenshot bhejein!</i>"
            )

    return {
        "is_valid": is_valid,
        "reject_reason": reject_reason,
        "img_hash": img_hash,
        "extracted_utr": extracted_utr,
        "amount_found": amount_found,
        "matched_kw_count": len(matched_kw),
    }


async def analyze_screenshot_async(bot, photo_file_id: str, expected_amount: int, strict_ocr: bool = True) -> dict:
    tg_file = await bot.get_file(photo_file_id)
    buf = BytesIO()
    await tg_file.download_to_memory(out=buf)
    img_bytes = buf.getvalue()
    buf.close()
    return await asyncio.to_thread(_analyze_screenshot_sync, img_bytes, expected_amount, strict_ocr)


def find_duplicate_order(utr: str, photo_unique_id: str, img_hash: str) -> sqlite3.Row | None:
    conn = get_db()
    if utr and utr != "NO-UTR":
        row = conn.execute(
            "SELECT id, user_id, status, created_at FROM orders WHERE LOWER(utr) = LOWER(?) AND status != 'rejected' LIMIT 1",
            (utr,),
        ).fetchone()
        if row:
            return row
    if photo_unique_id:
        row = conn.execute(
            "SELECT id, user_id, status, created_at FROM orders WHERE photo_unique_id = ? AND status != 'rejected' LIMIT 1",
            (photo_unique_id,),
        ).fetchone()
        if row:
            return row
    if img_hash:
        recent_hashes = conn.execute(
            "SELECT id, user_id, status, created_at, img_hash FROM orders WHERE img_hash != '' AND status != 'rejected' ORDER BY id DESC LIMIT 400"
        ).fetchall()
        for r in recent_hashes:
            if hamming_distance(img_hash, r["img_hash"]) <= 3:
                return r
    return None
