# ==========================
# فایل: beneficial1bot.py
# نسخه اصلاح‌شده کامل — اضافه شدن درخواست‌ها، اصلاح خرید و کسب درآمد، پنل ادمین برای کد تخفیف
# تغییرات اصلی (خلاصه):
# - هنگام خرید محصول هم "balance" و هم "withdrawable" کاربر کم می‌شود.
# - جایگزینی دکمه "بازگشت به لیست" در حالت کمبود موجودی با "💳 افزایش موجودی" (callback: add_balance).
# - اصلاح جریان کسب درآمد: حذف پیام قبلی هنگام کلیک روی "بله، ثبت‌نام کردم" یا "خیر"، ارسال مرحله اول به کاربر، و ارسال نوتیفیکیشن هنگام تایید/رد توسط ادمین.
# - افزودن صفحه "درخواست‌ها" در پروفایل برای مشاهده درخواست‌های در انتظار (واریز/برداشت/کسب درآمد).
# - اضافه شدن ذخیره‌سازی و نگهداری "user_requests" برای نمایش در UI کاربر.
# - افزودن پنل ادمین "ارسال کد تخفیف" و "درخواست‌های کاربر" و پیاده‌سازی ارسال کد تخفیف به کاربر.
# - هماهنگی حذف درخواست‌ها پس از تایید/رد در بخش‌های مربوطه تا از لیست درخواست‌ها حذف شوند.
# - چند بهبود کوچک در مدیریت state و پیام‌ها.
# ==========================

import json
import os
import io
import time
import hashlib
import threading
import secrets
import string
import traceback
import asyncio
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)

# ==========================
# تنظیمات
# ==========================

TOKEN = "8857326920:AAENFh77wSBVyzGQ4zs06bb26UwzkJPTcQw"
BACKUP_CHANNEL = "@avqgaiqpzm"
CHANNEL_LINK = "https://t.me/OffChiii"
CHANNEL_USERNAME = "OffChiii"
VARIZ_CHANNEL = "@variziaaaha"
ORDER_CHANNEL = "@codmoddod"
CONFIRM_CHANNEL = "@taaaiiiiddd"
MAIN_ADMIN_ID = 8877968535
SECOND_ADMIN_ID = 1345486939
ADMIN_USERNAME = "@admin_username"

# دیکشنری‌های پشتیبانی
SUPPORT_SESSIONS = {}  # {user_id: True}
SUPPORT_REPLY_MAP = {}  # {admin_message_id: user_id}

# قفل برای جلوگیری از race condition هنگام خرید (چک موجودی + کسر آن باید اتمیک باشد)
_purchase_lock = asyncio.Lock()

# ==========================
# ابزار کمکی
# ==========================

def schedule_coro(coro):
    """
    اجرا کردن coroutine به صورت ایمن:
    - اگر حلقه فعالی وجود داشته باشد از run_coroutine_threadsafe استفاده می‌کنیم (thread-safe)
    - در غیر این صورت یک حلقه جدید در یک Thread ساخته و coroutine را اجرا می‌کنیم
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        def _run_in_new_loop(c):
            try:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                new_loop.run_until_complete(c)
            except Exception as e:
                print(f"❌ schedule_coro background loop failed: {e}")
                traceback.print_exc()
            finally:
                try:
                    new_loop.close()
                except Exception:
                    pass
        threading.Thread(target=_run_in_new_loop, args=(coro,), daemon=True).start()
    else:
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            try:
                loop.create_task(coro)
            except Exception as e:
                print(f"❌ schedule_coro failed to schedule: {e}")
                traceback.print_exc()

async def safe_delete_message(msg):
    try:
        if msg:
            await msg.delete()
    except Exception:
        pass

# ==========================
# دیتابیس فایل/کَش
# ==========================

_cache_thread_lock = threading.Lock()
_cache = {}
_cache_time = {}

def get_text_from_channel(context, filename):
    try:
        path = f"data_{filename}"
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Error loading {filename}: {e}")
        traceback.print_exc()
        return {}

async def save_text_to_channel(context, filename, data):
    try:
        final_path = f"data_{filename}"
        temp_path = f"{final_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, final_path)
        print(f"✅ Saved locally: {final_path}")

        # try send as document to backup channel
        try:
            with open(final_path, "rb") as doc:
                caption = f"📁 {filename}\n🕐 {datetime.now().strftime('%Y/%m/%d - %H:%M:%S')}"
                result = await context.bot.send_document(chat_id=BACKUP_CHANNEL, document=doc, caption=caption)
                print(f"✅ Sent document to channel - Message ID: {getattr(result, 'message_id', 'unknown')}")
        except Exception:
            # fallback: send as text in chunks
            json_str = json.dumps(data, ensure_ascii=False, indent=2)
            if len(json_str) > 4000:
                chunks = [json_str[i:i+4000] for i in range(0, len(json_str), 4000)]
                for i, chunk in enumerate(chunks):
                    text = f"📁 {filename} (بخش {i+1}/{len(chunks)})\n```json\n{chunk}\n```\n🕐 {datetime.now().strftime('%Y/%m/%d - %H:%M:%S')}"
                    try:
                        await context.bot.send_message(chat_id=BACKUP_CHANNEL, text=text, parse_mode="Markdown")
                    except Exception as ex:
                        print(f"❌ Failed to send chunk {i+1}: {ex}")
                    await asyncio.sleep(0.25)
            else:
                text = f"📁 {filename}\n```json\n{json_str}\n```\n🕐 {datetime.now().strftime('%Y/%m/%d - %H:%M:%S')}"
                try:
                    await context.bot.send_message(chat_id=BACKUP_CHANNEL, text=text, parse_mode="Markdown")
                except Exception as ex:
                    print(f"❌ Failed to send JSON text: {ex}")
        return True
    except Exception as e:
        print(f"❌ Error saving {filename}: {e}")
        traceback.print_exc()
        return False

def delete_text_from_channel(context, filename):
    try:
        if os.path.exists(f"data_{filename}"):
            os.remove(f"data_{filename}")
        return True
    except Exception as e:
        print(f"❌ Error deleting {filename}: {e}")
        traceback.print_exc()
        return False

# ==========================
# load/save helpers for different JSON files
# ==========================

def _load_with_cache(context, key, filename, default):
    with _cache_thread_lock:
        if key in _cache and time.time() - _cache_time.get(key, 0) < 5:
            return _cache[key]
    data = get_text_from_channel(context, filename)
    if not data:
        data = default
        schedule_coro(globals().get(f"save_{key}")(context, data))
    with _cache_thread_lock:
        _cache[key] = data
        _cache_time[key] = time.time()
    return data

def load_users(context):
    return _load_with_cache(context, 'users', "users_db.json", {})

async def save_users(context, users):
    with _cache_thread_lock:
        _cache['users'] = users
        _cache_time['users'] = time.time()
    return await save_text_to_channel(context, "users_db.json", users)

def load_admins(context):
    return _load_with_cache(context, 'admins', "admins.json", {"admins": [MAIN_ADMIN_ID, SECOND_ADMIN_ID]})

async def save_admins(context, admins):
    with _cache_thread_lock:
        _cache['admins'] = admins
        _cache_time['admins'] = time.time()
    return await save_text_to_channel(context, "admins.json", admins)

def load_products(context):
    return _load_with_cache(context, 'products', "products.json", {"items": []})

async def save_products(context, products):
    with _cache_thread_lock:
        _cache['products'] = products
        _cache_time['products'] = time.time()
    return await save_text_to_channel(context, "products.json", products)

def load_earnings(context):
    return _load_with_cache(context, 'earnings', "earnings.json", {"items": []})

async def save_earnings(context, earnings):
    with _cache_thread_lock:
        _cache['earnings'] = earnings
        _cache_time['earnings'] = time.time()
    return await save_text_to_channel(context, "earnings.json", earnings)

def load_pending_requests(context):
    return _load_with_cache(context, 'pending_requests', "pending_requests.json", {})

async def save_pending_requests(context, requests):
    with _cache_thread_lock:
        _cache['pending_requests'] = requests
        _cache_time['pending_requests'] = time.time()
    return await save_text_to_channel(context, "pending_requests.json", requests)

def load_earn_requests(context):
    return _load_with_cache(context, 'earn_requests', "earn_requests.json", {})

async def save_earn_requests(context, requests):
    with _cache_thread_lock:
        _cache['earn_requests'] = requests
        _cache_time['earn_requests'] = time.time()
    return await save_text_to_channel(context, "earn_requests.json", requests)

# new: unified user-visible requests (for profile -> Requests)
def load_user_requests(context):
    return _load_with_cache(context, 'user_requests', "user_requests.json", {})

async def save_user_requests(context, requests):
    with _cache_thread_lock:
        _cache['user_requests'] = requests
        _cache_time['user_requests'] = time.time()
    return await save_text_to_channel(context, "user_requests.json", requests)

# new: discount requests for admin to send codes
def load_discount_requests(context):
    return _load_with_cache(context, 'discount_requests', "discount_requests.json", {})

async def save_discount_requests(context, requests):
    with _cache_thread_lock:
        _cache['discount_requests'] = requests
        _cache_time['discount_requests'] = time.time()
    return await save_text_to_channel(context, "discount_requests.json", requests)

# new: help FAQ items
def load_help_faq(context):
    return _load_with_cache(context, 'help_faq', "help_faq.json", {"items": []})

async def save_help_faq(context, data):
    with _cache_thread_lock:
        _cache['help_faq'] = data
        _cache_time['help_faq'] = time.time()
    return await save_text_to_channel(context, "help_faq.json", data)

# new: per-section emoji/sticker settings
EMOJI_SECTIONS = {
    "profile": "⚡️ پروفایل من",
    "help": "📖 راهنما",
    "subteam": "👥 زیرمجموعه گیری",
    "earn_list": "💰 کسب درآمد (لیست روش‌ها)",
    "buy_list": "🛍 خرید محصول تخفیف (لیست محصولات)",
    "support": "🛠 پشتیبانی",
    "reports": "📊 گزارش‌ها شما",
    "add_balance": "💳 افزایش موجودی",
    "withdraw": "💸 برداشت موجودی",
    "requests": "📂 درخواست‌ها",
    "product_selected": "🎯 انتخاب یک محصول خاص",
    "earn_selected": "🎯 انتخاب یک روش کسب‌درآمد خاص",
}

def load_emoji_settings(context):
    return _load_with_cache(context, 'emoji_settings', "emoji_settings.json", {})

async def save_emoji_settings(context, data):
    with _cache_thread_lock:
        _cache['emoji_settings'] = data
        _cache_time['emoji_settings'] = time.time()
    return await save_text_to_channel(context, "emoji_settings.json", data)

async def send_section_emoji(context, chat_id, section_key):
    """اگر ادمین برای این بخش ایموجی یا استیکری تنظیم کرده باشد، آن را قبل از محتوای اصلی ارسال می‌کند."""
    try:
        settings = load_emoji_settings(context)
        item = settings.get(section_key)
        if not item:
            return
        if item.get("type") == "sticker":
            await context.bot.send_sticker(chat_id=chat_id, sticker=item.get("value"))
        else:
            await context.bot.send_message(chat_id=chat_id, text=item.get("value", ""))
    except Exception:
        pass

# ==========================
# توابع کمکی
# ==========================

def generate_referral_code():
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(6))

def generate_request_id(request_type, user_id, amount):
    data = f"{request_type}_{user_id}_{amount}_{time.time()}"
    return hashlib.md5(data.encode()).hexdigest()[:10]

def create_user(user, context):
    users = load_users(context)
    uid = str(user.id)
    if uid not in users:
        referral_code = generate_referral_code()
        existing_codes = [u.get('referral_code') for u in users.values() if u.get('referral_code')]
        while referral_code in existing_codes:
            referral_code = generate_referral_code()
        users[uid] = {
            "name": user.first_name,
            "username": user.username if user.username else "",
            "balance": 0,
            "withdrawable": 0,
            "profits": 0,
            "orders": 0,
            "subscribers": 0,
            "earnings": 0,
            "deposits_count": 0,
            "deposits_sum": 0,
            "withdrawals_count": 0,
            "withdrawals_sum": 0,
            "join_date": datetime.now().strftime("%Y/%m/%d - %H:%M:%S"),
            "is_member": False,
            "referral_code": referral_code,
            "referrer_id": "",
            "referred_by": ""
        }
        schedule_coro(save_users(context, users))
    return users

def is_admin(user_id, context):
    admins = load_admins(context)
    return user_id in admins.get("admins", [])

def find_user_by_username_or_id(search_text, context):
    users = load_users(context)
    search = search_text.strip()
    if not search:
        return None, None, "❌ متن جستجو خالی است!"
    if search.startswith('@'):
        search = search[1:]
    if search.isdigit():
        uid = search
        if uid in users:
            return uid, users[uid], None
    for uid, info in users.items():
        if info.get("username", "").lower() == search.lower():
            return uid, info, None
    for uid, info in users.items():
        if info.get("name", "").lower() == search.lower():
            return uid, info, None
    return None, None, f"❌ کاربری با مشخصات '{search_text}' یافت نشد!"

# ==========================
# کیبوردها
# ==========================

def get_panel_with_back_keyboard():
    return ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True)

def get_main_keyboard():
    return ReplyKeyboardMarkup([
        ["💰 کسب درآمد"],
        ["⚡️ پروفایل من", "🛍 خرید محصول تخفیف"],
        ["👥 زیرمجموعه گیری", "🛠 پشتیبانی"],
        ["📖 راهنما"]
    ], resize_keyboard=True)

def get_admin_main_keyboard():
    return ReplyKeyboardMarkup([
        ["💰 کسب درآمد"],
        ["⚡️ پروفایل من", "🛍 خرید محصول تخفیف"],
        ["👥 زیرمجموعه گیری", "🛠 پشتیبانی"],
        ["📖 راهنما"],
        ["👑 پنل ادمین"]
    ], resize_keyboard=True)

def get_admin_panel_keyboard():
    # added: ارسال کد تخفیف and درخواست های کاربر
    return ReplyKeyboardMarkup([
        ["👑 مدیریت کاربران", "📊 آمار کلی"],
        ["📨 ارسال پیام همگانی", "✉️ ارسال کد تخفیف"],
        ["👥 مدیریت ادمین‌ها", "📂 درخواست‌های کاربر"],
        ["📦 مدیریت محصولات", "💎 مدیریت کسب درآمد"],
        ["⚙️ تنظیم راهنما", "🎨 تنظیم ایموجی"],
        ["🔙 بازگشت"]
    ], resize_keyboard=True)

# Inline keyboards (unchanged largely)
def get_user_management_keyboard(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 افزایش موجودی", callback_data=f"add_balance_{uid}"),
         InlineKeyboardButton("➖ کاهش موجودی", callback_data=f"remove_balance_{uid}")],
        [InlineKeyboardButton("💳 افزایش قابل برداشت", callback_data=f"add_withdrawable_{uid}"),
         InlineKeyboardButton("➖ کاهش قابل برداشت", callback_data=f"remove_withdrawable_{uid}")],
        [InlineKeyboardButton("📤 افزایش تسویه", callback_data=f"add_profit_{uid}"),
         InlineKeyboardButton("👑 افزودن ادمین", callback_data=f"make_admin_{uid}")],
        [InlineKeyboardButton("📨 ارسال پیام", callback_data=f"send_msg_{uid}")]
    ])

def get_confirm_keyboard(request_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تایید", callback_data=f"c_{request_id}"),
         InlineKeyboardButton("❌ رد", callback_data=f"r_{request_id}")]
    ])

def get_earn_confirm_keyboard(request_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تایید", callback_data=f"ec_{request_id}"),
         InlineKeyboardButton("❌ رد", callback_data=f"er_{request_id}")]
    ])

def get_product_buy_keyboard(product_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖", callback_data=f"dec_{product_id}"),
         InlineKeyboardButton("1", callback_data=f"count_{product_id}"),
         InlineKeyboardButton("➕", callback_data=f"inc_{product_id}")],
        [InlineKeyboardButton("✅ تایید خرید", callback_data=f"confirm_buy_{product_id}")],
        # removed "بازگشت به لیست" here (we handle products_back elsewhere)
    ])

def get_products_admin_keyboard(context):
    products = load_products(context)
    keyboard = []
    for item in products.get("items", []):
        keyboard.append([InlineKeyboardButton(f"📦 {item.get('name', 'بدون نام')} - {item.get('price', 0):,} تومان",
                                             callback_data=f"admin_product_{item.get('id')}")])
    keyboard.append([InlineKeyboardButton("➕ افزودن محصول جدید", callback_data="add_product")])
    return InlineKeyboardMarkup(keyboard)

def get_products_list_keyboard(context):
    products = load_products(context)
    keyboard = []
    for item in products.get("items", []):
        keyboard.append([InlineKeyboardButton(f"🛍 {item.get('name', 'بدون نام')} - {item.get('price', 0):,} تومان",
                                             callback_data=f"buy_product_{item.get('id')}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="products_back")])
    return InlineKeyboardMarkup(keyboard)

def get_product_edit_keyboard(product_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"edit_name_{product_id}"),
         InlineKeyboardButton("✏️ ویرایش توضیحات", callback_data=f"edit_desc_{product_id}")],
        [InlineKeyboardButton("✏️ ویرایش قیمت", callback_data=f"edit_price_{product_id}"),
         InlineKeyboardButton("🗑️ حذف محصول", callback_data=f"delete_product_{product_id}")]
    ])

REQUEST_CATEGORIES = {
    "earn": "💎 کسب درآمدها",
    "order": "🛍 سفارشات محصولات",
    "deposit": "📥 واریز پول‌ها",
    "withdraw": "📤 برداشت پول‌ها",
}

REQUESTS_PAGE_SIZE = 10

def request_status_label(status):
    mapping = {
        "pending": "در حال بررسی",
        "confirmed": "انجام شده",
        "rejected": "رد شده",
        "completed": "انجام شده",
        "sent": "انجام شده",
    }
    return mapping.get(status, status or "در حال بررسی")

def build_request_item_label(category, r):
    status = request_status_label(r.get('status', 'pending'))
    if category == "deposit":
        return f"واریز {r.get('amount', 0):,} تومان - {status}"
    if category == "withdraw":
        return f"برداشت {r.get('amount', 0):,} تومان - {status}"
    if category == "order":
        return f"سفارش {r.get('product_name', '')} - {status}"
    if category == "earn":
        return f"کسب درآمد {r.get('earn_name', '')} - {status}"
    return f"{category} - {status}"

def get_requests_home_keyboard():
    keyboard = [[InlineKeyboardButton(label, callback_data=f"reqlist_{key}_0")] for key, label in REQUEST_CATEGORIES.items()]
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="profile_back")])
    return InlineKeyboardMarkup(keyboard)

def get_user_requests_by_category(context, uid, category):
    user_reqs = load_user_requests(context)
    items = [(rid, r) for rid, r in user_reqs.items() if r.get('user_id') == uid and r.get('type') == category]

    def sort_key(pair):
        try:
            return datetime.strptime(pair[1].get('date', ''), "%Y/%m/%d - %H:%M:%S")
        except Exception:
            return datetime.min
    items.sort(key=sort_key, reverse=True)
    return items

def build_profile_view(uid, info):
    balance = info.get("balance", 0)
    withdrawable = info.get("withdrawable", 0)
    keyboard = [
        [InlineKeyboardButton("📊 گزارش ها شما✨", callback_data="reports")],
        [InlineKeyboardButton("💳 افزایش موجودی", callback_data="add_balance"),
         InlineKeyboardButton("💸 برداشت موجودی", callback_data="withdraw")],
        [InlineKeyboardButton("📂 درخواست ها", callback_data="requests")]
    ]
    text_msg = f"━━━━━━━━━━━━━━━\n🏷 شناسه کاربری:\n<code>{uid}</code>\n💰 موجودی:\n<code>{balance:,}</code> تومان\n💳 قابل برداشت:\n<code>{withdrawable:,}</code> تومان\n📅 تاریخ عضویت:\n<code>{info.get('join_date')}</code>\n━━━━━━━━━━━━━━━"
    return text_msg, InlineKeyboardMarkup(keyboard)

def get_earnings_admin_keyboard(context):
    earnings = load_earnings(context)
    keyboard = []
    for item in earnings.get("items", []):
        keyboard.append([InlineKeyboardButton(f"💎 {item.get('name', 'بدون نام')}", callback_data=f"admin_earn_{item.get('id')}")])
    keyboard.append([InlineKeyboardButton("➕ افزودن کسب درآمد جدید", callback_data="add_earn")])
    return InlineKeyboardMarkup(keyboard)

def get_earnings_list_keyboard(context):
    earnings = load_earnings(context)
    keyboard = []
    for item in earnings.get("items", []):
        keyboard.append([InlineKeyboardButton(f"💎 {item.get('name', 'بدون نام')}", callback_data=f"do_earn_{item.get('id')}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="earnings_back")])
    return InlineKeyboardMarkup(keyboard)

def get_earn_detail_keyboard(earn_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 آموزش تصویری", callback_data=f"tutorial_earn_{earn_id}")],
        [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="earnings_show_list")]
    ])

def get_earn_edit_keyboard(earn_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"edit_earn_name_{earn_id}"),
         InlineKeyboardButton("✏️ ویرایش توضیحات", callback_data=f"edit_earn_desc_{earn_id}")],
        [InlineKeyboardButton("✏️ ویرایش سود", callback_data=f"edit_earn_reward_{earn_id}"),
         InlineKeyboardButton("✏️ ویرایش کد معرف", callback_data=f"edit_earn_code_{earn_id}")],
        [InlineKeyboardButton("✏️ ویرایش لینک", callback_data=f"edit_earn_link_{earn_id}"),
         InlineKeyboardButton("🖼️ ویرایش آموزش", callback_data=f"edit_earn_tutorial_{earn_id}")],
        [InlineKeyboardButton("✏️ ویرایش مراحل تایید", callback_data=f"edit_earn_steps_{earn_id}")],
        [InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_earn_{earn_id}")]
    ])

# ==========================
# بررسی عضویت
# ==========================

async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
        return False
    except Exception as e:
        print(f"⚠️ Membership check error: {e}")
        return False

async def show_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📢 عضویت در کانال", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ عضو شدم", callback_data="check_membership")]
    ]
    await update.message.reply_text(
        "📌 لطفاً برای ادامه کار با ربات، عضو کانال ما شوید:\n\n🔹 برای عضویت روی دکمه زیر کلیک کنید.\n🔹 پس از عضویت، گزینه «عضو شدم✅» را بزنید.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ==========================
# پشتیبانی — ارسال پیام‌ها به ادمین‌ها
# ==========================

async def notify_admins(context, text: str):
    admins = load_admins(context)
    for admin_id in admins.get("admins", []):
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
        except Exception as e:
            print(f"Error notifying admin {admin_id}: {e}")

async def forward_to_admins(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user = update.effective_user
    uid = str(user.id)
    admins = load_admins(context)

    for admin_id in admins.get("admins", []):
        try:
            message = await context.bot.send_message(
                chat_id=admin_id,
                text=f"💬 پیام جدید از پشتیبانی\n\n"
                     f"👤 کاربر: {user.first_name}\n"
                     f"🆔 آیدی: <code>{uid}</code>\n"
                     f"📱 یوزرنیم: @{user.username if user.username else 'ندارد'}\n\n"
                     f"📝 متن پیام:\n{text}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✉️ پاسخ به کاربر", callback_data=f"support_reply_{uid}")]
                ])
            )
            SUPPORT_REPLY_MAP[str(message.message_id)] = uid
        except Exception as e:
            print(f"Error forwarding to admin {admin_id}: {e}")

async def forward_photo_to_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    admins = load_admins(context)

    for admin_id in admins.get("admins", []):
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo.file_id,
                caption=f"📸 پیام جدید از پشتیبانی\n\n"
                        f"👤 کاربر: {user.first_name}\n"
                        f"🆔 آیدی: <code>{uid}</code>\n"
                        f"📱 یوزرنیم: @{user.username if user.username else 'ندارد'}\n\n"
                        f"📝 متن: {caption if caption else 'بدون متن'}",
                parse_mode="HTML"
            )
        except Exception as e:
            print(f"Error sending photo to admin {admin_id}: {e}")

# ==========================
# دستورات تست
# ==========================

async def test_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id, context):
        await update.message.reply_text("❌ شما دسترسی ندارید!")
        return
    await update.message.reply_text("🔍 در حال تست ارتباط با کانال...")
    try:
        result = await context.bot.send_message(chat_id=BACKUP_CHANNEL, text="🧪 پیام تست از ربات\n\n✅ اگر این پیام را می‌بینید، ارتباط برقرار است!")
        await update.message.reply_text(f"✅ پیام تست به {BACKUP_CHANNEL} ارسال شد!\n📝 Message ID: {getattr(result, 'message_id', 'unknown')}")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ارسال به کانال:\n\n{str(e)}")

async def test_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id, context):
        await update.message.reply_text("❌ شما دسترسی ندارید!")
        return
    await update.message.reply_text("🔍 در حال تست ذخیره دیتابیس...")
    try:
        test_data = {
            "test": "Hello from bot",
            "time": datetime.now().strftime("%Y/%m/%d - %H:%M:%S"),
            "user": update.effective_user.first_name,
            "id": str(update.effective_user.id)
        }
        result = await save_text_to_channel(context, "test_db.json", test_data)
        if result:
            await update.message.reply_text(
                f"✅ دیتابیس تست با موفقیت ذخیره شد!\n\n"
                f"📁 فایل: test_db.json\n"
                f"📝 دیتا: {test_data}\n\n"
                f"🔍 لطفاً کانال {BACKUP_CHANNEL} رو چک کن."
            )
        else:
            await update.message.reply_text("❌ خطا در ذخیره دیتابیس تست!")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {str(e)}")
        traceback.print_exc()

# ==========================
# استارت
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    users = load_users(context)
    uid = str(user.id)
    if uid not in users:
        create_user(user, context)
        users = load_users(context)
        username_part = f"@{user.username}" if user.username else "ندارد"
        await notify_admins(context, f"🆕 کاربر جدیدی ربات را استارت کرد!\n\n👤 نام: {user.first_name}\n📱 یوزرنیم: {username_part}\n🆔 آیدی عددی: <code>{uid}</code>")
    # referral
    if context.args:
        ref_param = context.args[0]
        if ref_param.startswith("ref_"):
            referral_code = ref_param.replace("ref_", "")
            referrer_id = None
            for ref_uid, info in users.items():
                if info.get("referral_code") == referral_code:
                    referrer_id = ref_uid
                    break
            if referrer_id and referrer_id != uid:
                users[uid]["referrer_id"] = referrer_id
                users[uid]["referred_by"] = referral_code
                users[referrer_id]["subscribers"] = users[referrer_id].get("subscribers", 0) + 1
                schedule_coro(save_users(context, users))
                try:
                    await context.bot.send_message(
                        chat_id=int(referrer_id),
                        text=f"🎉 یک کاربر جدید با لینک دعوت شما عضو شد!\n\n"
                             f"👤 کاربر: {user.first_name}\n"
                             f"🆔 آیدی: <code>{uid}</code>\n"
                             f"👥 تعداد زیرمجموعه شما: {users[referrer_id].get('subscribers', 0)} نفر",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    print(f"Error sending referral notification: {e}")
                await update.message.reply_text("✅ شما با لینک دعوت عضو شدید!\nاز حضور شما خوشحالیم! 🎉")
    if is_admin(user.id, context):
        await update.message.reply_text(
            "🌟 به ربات خوش آمدید\n✨ از منوی زیر انتخاب کنید:",
            reply_markup=get_admin_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "🌟 به ربات خوش آمدید\n✨ از منوی زیر انتخاب کنید:",
            reply_markup=get_main_keyboard()
        )

# ==========================
# پردازش اکشن‌های ادمین (متن)
# ==========================

async def process_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user = update.effective_user
    if not is_admin(user.id, context):
        return False
    action = context.user_data.get('admin_action')
    if not action:
        return False
    if text == "🔙 بازگشت":
        context.user_data.pop('admin_action', None)
        context.user_data.pop('target_uid', None)
        context.user_data.pop('discount_target', None)
        return False

    # search_user (same)
    if action == "search_user":
        uid, info, error = find_user_by_username_or_id(text, context)
        if error:
            await update.message.reply_text(f"{error}\n\n🔍 لطفاً دوباره تلاش کنید یا از دکمه بازگشت استفاده کنید.", reply_markup=get_panel_with_back_keyboard())
            return True
        if uid and info:
            is_user_admin = is_admin(int(uid), context)
            await update.message.reply_text(
                f"🔍 اطلاعات کاربر:\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🆔 آیدی: <code>{uid}</code>\n"
                f"👤 نام: {info.get('name', 'نامشخص')}\n"
                f"👤 نام کاربری: @{info.get('username', 'ندارد')}\n"
                f"💰 موجودی: <code>{info.get('balance', 0):,}</code> تومان\n"
                f"💳 قابل برداشت: <code>{info.get('withdrawable', 0):,}</code> تومان\n"
                f"📤 تسویه شده: <code>{info.get('profits', 0):,}</code> تومان\n"
                f"☎️ تعداد خرید: {info.get('orders', 0)} عدد\n"
                f"👥 زیرمجموعه: {info.get('subscribers', 0)} نفر\n"
                f"📅 تاریخ عضویت: {info.get('join_date', 'نامشخص')}\n"
                f"👑 وضعیت: {'✅ ادمین' if is_user_admin else '❌ کاربر عادی'}\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"از دکمه‌های زیر برای مدیریت کاربر استفاده کنید:",
                parse_mode="HTML",
                reply_markup=get_user_management_keyboard(uid)
            )
            context.user_data['admin_action'] = None
        else:
            await update.message.reply_text("❌ خطا در پیدا کردن کاربر!", reply_markup=get_panel_with_back_keyboard())
        return True

    # financial actions (add/remove)
    if action in ["add_balance", "remove_balance", "add_withdrawable", "remove_withdrawable", "add_profit"]:
        target_uid = context.user_data.get('target_uid')
        if not target_uid:
            await update.message.reply_text("❌ خطا! کاربر مشخص نشده است.", reply_markup=get_admin_panel_keyboard())
            context.user_data['admin_action'] = None
            return True
        try:
            amount = int(text.replace(',', '').strip())
            if amount <= 0:
                await update.message.reply_text("❌ مبلغ باید بزرگتر از 0 باشد!", reply_markup=get_panel_with_back_keyboard())
                return True
            users = load_users(context)
            if target_uid not in users:
                await update.message.reply_text("❌ کاربر مورد نظر یافت نشد!", reply_markup=get_admin_panel_keyboard())
                context.user_data['admin_action'] = None
                return True
            if action == "add_balance":
                users[target_uid]["balance"] = users[target_uid].get("balance", 0) + amount
                schedule_coro(save_users(context, users))
                await update.message.reply_text(f"✅ مبلغ <code>{amount:,}</code> تومان به موجودی کاربر {target_uid} اضافه شد.\n\n💰 موجودی جدید: <code>{users[target_uid]['balance']:,}</code> تومان", parse_mode="HTML", reply_markup=get_admin_panel_keyboard())
            elif action == "remove_balance":
                if users[target_uid].get("balance", 0) < amount:
                    await update.message.reply_text("❌ موجودی کاربر کافی نیست!", reply_markup=get_panel_with_back_keyboard())
                    return True
                users[target_uid]["balance"] = users[target_uid].get("balance", 0) - amount
                schedule_coro(save_users(context, users))
                await update.message.reply_text(f"✅ مبلغ <code>{amount:,}</code> تومان از موجودی کاربر {target_uid} کم شد.\n\n💰 موجودی جدید: <code>{users[target_uid]['balance']:,}</code> تومان", parse_mode="HTML", reply_markup=get_admin_panel_keyboard())
            elif action == "add_withdrawable":
                users[target_uid]["withdrawable"] = users[target_uid].get("withdrawable", 0) + amount
                schedule_coro(save_users(context, users))
                await update.message.reply_text(f"✅ مبلغ <code>{amount:,}</code> تومان به قابل برداشت کاربر {target_uid} اضافه شد.\n\n💳 قابل برداشت جدید: <code>{users[target_uid]['withdrawable']:,}</code> تومان", parse_mode="HTML", reply_markup=get_admin_panel_keyboard())
            elif action == "remove_withdrawable":
                if users[target_uid].get("withdrawable", 0) < amount:
                    await update.message.reply_text("❌ موجودی قابل برداشت کاربر کافی نیست!", reply_markup=get_panel_with_back_keyboard())
                    return True
                users[target_uid]["withdrawable"] = users[target_uid].get("withdrawable", 0) - amount
                schedule_coro(save_users(context, users))
                await update.message.reply_text(f"✅ مبلغ <code>{amount:,}</code> تومان از قابل برداشت کاربر {target_uid} کم شد.\n\n💳 قابل برداشت جدید: <code>{users[target_uid]['withdrawable']:,}</code> تومان", parse_mode="HTML", reply_markup=get_admin_panel_keyboard())
            elif action == "add_profit":
                users[target_uid]["profits"] = users[target_uid].get("profits", 0) + amount
                schedule_coro(save_users(context, users))
                await update.message.reply_text(f"✅ مبلغ <code>{amount:,}</code> تومان به تسویه شده کاربر {target_uid} اضافه شد.\n\n📤 تسویه شده جدید: <code>{users[target_uid]['profits']:,}</code> تومان", parse_mode="HTML", reply_markup=get_admin_panel_keyboard())
            context.user_data['admin_action'] = None
            context.user_data['target_uid'] = None
        except ValueError:
            await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید!", reply_markup=get_panel_with_back_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {str(e)}", reply_markup=get_admin_panel_keyboard())
            context.user_data['admin_action'] = None
        return True

    # send_to_all (same, with RetryAfter handling)
    if action == "send_to_all":
        users = load_users(context)
        sent = 0
        failed = 0
        await update.message.reply_text("📨 در حال ارسال پیام به همه کاربران...")
        for uid in users.keys():
            try:
                await context.bot.send_message(chat_id=int(uid), text=f"{text}")
                sent += 1
                await asyncio.sleep(0.05)
            except RetryAfter as r:
                wait = int(getattr(r, "retry_after", 1))
                print(f"FloodWait: sleeping {wait}s")
                await asyncio.sleep(wait)
            except Exception:
                failed += 1
        await update.message.reply_text(f"✅ ارسال پیام همگانی کامل شد!\n\n📨 ارسال شده: {sent} کاربر\n❌ ناموفق: {failed} کاربر", reply_markup=get_admin_panel_keyboard())
        context.user_data['admin_action'] = None
        return True

    # send_to_user
    if action == "send_to_user":
        target_uid = context.user_data.get('target_uid')
        if target_uid:
            try:
                await context.bot.send_message(chat_id=int(target_uid), text=f"{text}")
                await update.message.reply_text("✅ پیام با موفقیت به کاربر ارسال شد!", reply_markup=get_admin_panel_keyboard())
            except Exception as e:
                await update.message.reply_text(f"❌ خطا در ارسال پیام: {str(e)}", reply_markup=get_admin_panel_keyboard())
        else:
            await update.message.reply_text("❌ خطا! کاربر مشخص نشده است.", reply_markup=get_admin_panel_keyboard())
        context.user_data['admin_action'] = None
        return True

    # add_admin/remove_admin
    if action == "add_admin":
        try:
            new_admin_id = int(text.strip())
            admins = load_admins(context)
            if new_admin_id in admins["admins"]:
                await update.message.reply_text("❌ این کاربر قبلاً ادمین است!", reply_markup=get_admin_panel_keyboard())
            else:
                admins["admins"].append(new_admin_id)
                schedule_coro(save_admins(context, admins))
                await update.message.reply_text(f"✅ ادمین با آیدی {new_admin_id} با موفقیت اضافه شد!", reply_markup=get_admin_panel_keyboard())
        except Exception:
            await update.message.reply_text("❌ لطفاً یک آیدی عددی معتبر وارد کنید!", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = None
        return True

    if action == "remove_admin":
        try:
            remove_id = int(text.strip())
            admins = load_admins(context)
            if remove_id in admins["admins"]:
                admins["admins"].remove(remove_id)
                schedule_coro(save_admins(context, admins))
                await update.message.reply_text(f"✅ ادمین با آیدی {remove_id} با موفقیت حذف شد!", reply_markup=get_admin_panel_keyboard())
            else:
                await update.message.reply_text("❌ این کاربر ادمین نیست!", reply_markup=get_admin_panel_keyboard())
        except Exception:
            await update.message.reply_text("❌ لطفاً یک آیدی عددی معتبر وارد کنید!", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = None
        return True

    # send discount code (admin action)
    if action == "send_discount_code":
        discount_id = context.user_data.get('discount_target')
        if not discount_id:
            await update.message.reply_text("❌ هیچ درخواست کد تخفیفی برای ارسال مشخص نشده است!", reply_markup=get_admin_panel_keyboard())
            context.user_data.pop('admin_action', None)
            return True
        code = text.strip()
        discount_requests = load_discount_requests(context)
        req = discount_requests.get(discount_id)
        if not req:
            await update.message.reply_text("❌ درخواست یافت نشد یا قبلاً پردازش شده!", reply_markup=get_admin_panel_keyboard())
            context.user_data.pop('admin_action', None)
            context.user_data.pop('discount_target', None)
            return True
        target_uid = req.get('user_id')
        product_name = req.get('product_name', 'محصول شما')
        needed_count = req.get('count', 1)
        codes = [line.strip() for line in code.split("\n") if line.strip()]
        if len(codes) != needed_count:
            await update.message.reply_text(f"❌ تعداد کدهای ارسالی ({len(codes)}) با تعداد سفارش ({needed_count}) برابر نیست!\n\nلطفاً دقیقاً {needed_count} کد را ارسال کنید، هرکدام در یک خط جداگانه:")
            return True
        # هر کد جداگانه داخل تگ <code> خودش قرار می‌گیرد تا کاربر بتواند هرکدام را جدا کپی کند
        codes_block = "\n".join([f"<code>{c}</code>" for c in codes])
        # send code to the user
        try:
            await context.bot.send_message(chat_id=int(target_uid), text=f"✅ سفارش شما آماده شد!\n\n🎁 کد{'های' if needed_count > 1 else ''} تخفیف شما:\n{codes_block}\n\n🛍 محصول: {product_name}", parse_mode="HTML")
            # update discount request status
            req['status'] = 'sent'
            req['code'] = code
            req['codes'] = codes
            req['sent_by'] = user.id
            req['sent_at'] = datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
            schedule_coro(save_discount_requests(context, discount_requests))
            # به‌روزرسانی همون رکورد سفارش در درخواست‌های کاربر (نوع رو عوض نمی‌کنیم که از دسته «سفارشات» خارج نشه)
            user_reqs = load_user_requests(context)
            if discount_id in user_reqs:
                user_reqs[discount_id]['code_sent'] = True
                user_reqs[discount_id]['code'] = code
                user_reqs[discount_id]['confirmed_at'] = req['sent_at']
                user_reqs[discount_id]['updated_at'] = req['sent_at']
            else:
                user_reqs[discount_id] = {
                    "id": discount_id,
                    "user_id": target_uid,
                    "type": "order",
                    "product_name": product_name,
                    "amount": req.get('amount', 0),
                    "code": code,
                    "status": "completed",
                    "date": req.get('date'),
                    "updated_at": req['sent_at'],
                    "confirmed_at": req['sent_at']
                }
            schedule_coro(save_user_requests(context, user_reqs))
            await update.message.reply_text("✅ کد تخفیف ارسال شد و وضعیت به‌روز شد.", reply_markup=get_admin_panel_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ خطا در ارسال کد: {e}", reply_markup=get_admin_panel_keyboard())
        context.user_data.pop('admin_action', None)
        context.user_data.pop('discount_target', None)
        return True

    return False

# ==========================
# منو اصلی — پردازش پیام‌های متنی
# ==========================

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user = update.effective_user
    users = load_users(context)
    uid = str(user.id)
    if uid not in users:
        create_user(user, context)
    info = users.get(uid, {})

    # admin replying to user (support)
    if context.user_data.get('reply_to_user') and is_admin(user.id, context):
        target_uid = context.user_data.get('reply_to_user')
        if text == "🔙 بازگشت":
            context.user_data['reply_to_user'] = None
            await update.message.reply_text("❌ پاسخ لغو شد.", reply_markup=get_admin_panel_keyboard())
            return
        try:
            await context.bot.send_message(chat_id=int(target_uid), text=f"📨 پاسخ از طرف پشتیبانی:\n\n{text}")
            await update.message.reply_text("✅ پاسخ با موفقیت ارسال شد.", reply_markup=get_admin_panel_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ خطا در ارسال پاسخ: {e}", reply_markup=get_admin_panel_keyboard())
        context.user_data['reply_to_user'] = None
        return

    # admin actions (text)
    if is_admin(user.id, context):
        handled = await process_admin_action(update, context, text)
        if handled:
            return

    # edit product or earn fields are handled here (if admin previously set state)
    if context.user_data.get('edit_field') in ["name", "description", "price"]:
        if not is_admin(user.id, context):
            context.user_data.pop('edit_field', None)
            context.user_data.pop('edit_product_id', None)
            await update.message.reply_text("❌ شما دسترسی ندارید!", reply_markup=get_panel_with_back_keyboard())
            return
        edit_field = context.user_data.get('edit_field')
        product_id = context.user_data.get('edit_product_id')
        if not product_id:
            context.user_data.pop('edit_field', None)
            await update.message.reply_text("❌ شناسه محصول معتبر نیست!", reply_markup=get_admin_panel_keyboard())
            return
        text_val = text
        products = load_products(context)
        found = False
        for item in products.get("items", []):
            if item.get("id") == product_id:
                if edit_field == "name":
                    item["name"] = text_val.strip()
                elif edit_field == "description":
                    item["description"] = text_val.strip()
                elif edit_field == "price":
                    try:
                        price = int(text_val.replace(',', '').strip())
                        if price <= 0:
                            await update.message.reply_text("❌ قیمت باید بزرگتر از 0 باشد!", reply_markup=get_panel_with_back_keyboard())
                            return
                        item["price"] = price
                    except ValueError:
                        await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید!", reply_markup=get_panel_with_back_keyboard())
                        return
                schedule_coro(save_products(context, products))
                found = True
                break
        if found:
            context.user_data.pop('edit_field', None)
            context.user_data.pop('edit_product_id', None)
            await update.message.reply_text("✅ اطلاعات محصول با موفقیت ویرایش شد!", reply_markup=get_admin_panel_keyboard())
        else:
            await update.message.reply_text("❌ خطا در ویرایش!", reply_markup=get_panel_with_back_keyboard())
        return

    if context.user_data.get('edit_earn_field') in ["name", "description", "reward", "code", "link"]:
        if not is_admin(user.id, context):
            context.user_data.pop('edit_earn_field', None)
            context.user_data.pop('edit_earn_id', None)
            await update.message.reply_text("❌ شما دسترسی ندارید!", reply_markup=get_panel_with_back_keyboard())
            return
        edit_field = context.user_data.get('edit_earn_field')
        earn_id = context.user_data.get('edit_earn_id')
        if not earn_id:
            context.user_data.pop('edit_earn_field', None)
            await update.message.reply_text("❌ شناسه روش معتبر نیست!", reply_markup=get_admin_panel_keyboard())
            return
        text_val = text
        earnings = load_earnings(context)
        found = False
        for item in earnings.get("items", []):
            if item.get("id") == earn_id:
                if edit_field == "name":
                    item["name"] = text_val.strip()
                elif edit_field == "description":
                    item["description"] = text_val.strip()
                elif edit_field == "reward":
                    try:
                        reward = int(text_val.replace(',', '').strip())
                        if reward <= 0:
                            await update.message.reply_text("❌ سود باید بزرگتر از 0 باشد!", reply_markup=get_panel_with_back_keyboard())
                            return
                        item["reward"] = reward
                    except ValueError:
                        await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید!", reply_markup=get_panel_with_back_keyboard())
                        return
                elif edit_field == "code":
                    item["code"] = text_val.strip()
                elif edit_field == "link":
                    item["link"] = text_val.strip()
                schedule_coro(save_earnings(context, earnings))
                found = True
                break
        if found:
            context.user_data.pop('edit_earn_field', None)
            context.user_data.pop('edit_earn_id', None)
            await update.message.reply_text("✅ اطلاعات روش کسب درآمد با موفقیت ویرایش شد!", reply_markup=get_admin_panel_keyboard())
        else:
            await update.message.reply_text("❌ خطا در ویرایش!", reply_markup=get_panel_with_back_keyboard())
        return

    # جریان افزودن روش کسب درآمد جدید (قبلاً هیچ‌جا صدا زده نمی‌شد - همین باعث می‌شد بعد از وارد کردن نام هیچ اتفاقی نیفتد)
    if context.user_data.get('adding_earn'):
        if not is_admin(user.id, context):
            context.user_data.pop('adding_earn', None)
            context.user_data.pop('earn_step', None)
            context.user_data.pop('earn_data', None)
            await update.message.reply_text("❌ شما دسترسی ندارید!", reply_markup=get_panel_with_back_keyboard())
            return
        handled = await handle_add_earn_text(update, context)
        if handled:
            return

    # جریان افزودن محصول جدید (قبلاً اصلاً پیاده‌سازی نشده بود)
    if context.user_data.get('adding_product'):
        if not is_admin(user.id, context):
            context.user_data.pop('adding_product', None)
            context.user_data.pop('add_step', None)
            context.user_data.pop('product_data', None)
            await update.message.reply_text("❌ شما دسترسی ندارید!", reply_markup=get_panel_with_back_keyboard())
            return
        handled = await handle_add_product_text(update, context)
        if handled:
            return

    # جریان افزودن/ویرایش سوالات راهنما
    if context.user_data.get('faq_action'):
        if not is_admin(user.id, context):
            context.user_data.pop('faq_action', None)
            context.user_data.pop('faq_data', None)
            context.user_data.pop('faq_target', None)
            await update.message.reply_text("❌ شما دسترسی ندارید!", reply_markup=get_panel_with_back_keyboard())
            return
        handled = await handle_faq_admin_text(update, context)
        if handled:
            return

    # دریافت ایموجی متنی برای تنظیم ایموجی بخش‌ها (استیکر در handler جداگانه‌ای گرفته می‌شود)
    if context.user_data.get('emoji_target'):
        if not is_admin(user.id, context):
            context.user_data.pop('emoji_target', None)
            await update.message.reply_text("❌ شما دسترسی ندارید!", reply_markup=get_panel_with_back_keyboard())
            return
        key = context.user_data.pop('emoji_target')
        emoji_settings = load_emoji_settings(context)
        emoji_settings[key] = {"type": "emoji", "value": text}
        schedule_coro(save_emoji_settings(context, emoji_settings))
        await update.message.reply_text("✅ ایموجی این بخش با موفقیت تنظیم شد.", reply_markup=get_admin_panel_keyboard())
        return

    # back button
    if text == "🔙 بازگشت":
        context.user_data.clear()
        SUPPORT_SESSIONS.pop(uid, None)
        try:
            await safe_delete_message(update.message)
        except Exception:
            pass
        if is_admin(user.id, context):
            await update.message.reply_text("✨ به منوی اصلی بازگشتید:", reply_markup=get_admin_main_keyboard())
        else:
            await update.message.reply_text("✨ به منوی اصلی بازگشتید:", reply_markup=get_main_keyboard())
        return

    # support end
    if text == "🔚 پایان پشتیبانی":
        SUPPORT_SESSIONS.pop(uid, None)
        context.user_data['in_support'] = False
        await update.message.reply_text(
            "✅ پشتیبانی به پایان رسید.\n\nدر صورت نیاز مجدداً روی دکمه «🛠 پشتیبانی» کلیک کنید.",
            reply_markup=get_main_keyboard() if not is_admin(user.id, context) else get_admin_main_keyboard()
        )
        return

    if context.user_data.get('in_support'):
        if text == "🛠 پشتیبانی":
            await update.message.reply_text("🔗 شما در حال حاضر به پشتیبانی متصل هستید.\nپیام خود را بنویسید.")
            return
        await forward_to_admins(update, context, text)
        await update.message.reply_text("✅ پیام شما به پشتیبانی ارسال شد.\n\nمنتظر پاسخ ادمین باشید.", reply_markup=ReplyKeyboardMarkup([["🔚 پایان پشتیبانی"]], resize_keyboard=True))
        return

    if context.user_data.get('earning_submit'):
        await handle_earn_submit(update, context)
        return

    # commands: testchannel/testdb handled by CommandHandlers

    # membership check
    is_member = await check_membership(update, context)
    if not is_member:
        await show_join_message(update, context)
        return

    # admin panel
    if text == "👑 پنل ادمین":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        await update.message.reply_text("👑 به پنل مدیریت خوش آمدید!\n\nاز منوی زیر انتخاب کنید:", reply_markup=get_admin_panel_keyboard())
        return

    if text == "👑 مدیریت کاربران":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        await update.message.reply_text("🔍 لطفاً آیدی عددی یا نام کاربری یا نام کاربر را وارد کنید:\n\n📌 مثال: 123456789 یا @username یا علی", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "search_user"
        return

    if text == "📨 ارسال پیام همگانی":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        await update.message.reply_text("📢 لطفاً متن پیام همگانی را وارد کنید:", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "send_to_all"
        return

    if text == "✉️ ارسال کد تخفیف":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        # show list of pending discount requests - fully glass (no text list)
        discount_requests = load_discount_requests(context)
        pending = [ (k,v) for k,v in discount_requests.items() if v.get('status') == 'pending' ]
        if not pending:
            await update.message.reply_text("📭 در حال حاضر درخواست کد تخفیفی وجود ندارد.", reply_markup=get_admin_panel_keyboard())
            return
        keyboard = []
        for k, v in pending:
            product_name = v.get('product_name', 'محصول')
            count = v.get('count', 1)
            qty_suffix = f" ({count} عدد)" if count > 1 else ""
            keyboard.append([InlineKeyboardButton(f"ارسال کد → {v.get('user_name','کاربر')} - {product_name}{qty_suffix}", callback_data=f"send_discount_{k}")])
        await update.message.reply_text("✉️ درخواست‌های کد تخفیف در انتظار ارسال:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if text == "⚙️ تنظیم راهنما":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        faq = load_help_faq(context)
        items = faq.get("items", [])
        keyboard = [[InlineKeyboardButton(it.get("title", "-"), callback_data=f"admin_faq_{it.get('id')}")] for it in items]
        keyboard.append([InlineKeyboardButton("➕ افزودن سوال جدید", callback_data="add_faq")])
        await update.message.reply_text("⚙️ تنظیم راهنما:\n\nسوال مورد نظر را برای ویرایش/حذف انتخاب کنید، یا سوال جدید اضافه کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if text == "🎨 تنظیم ایموجی":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        emoji_settings = load_emoji_settings(context)
        keyboard = []
        for key, label in EMOJI_SECTIONS.items():
            mark = " ✅" if key in emoji_settings else ""
            keyboard.append([InlineKeyboardButton(f"{label}{mark}", callback_data=f"emoji_sec_{key}")])
        await update.message.reply_text("🎨 تنظیم ایموجی/استیکر برای هر بخش:\n\nیکی از بخش‌ها را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if text == "📂 درخواست‌های کاربر":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        user_reqs = load_user_requests(context)
        if not user_reqs:
            await update.message.reply_text("📭 در حال حاضر هیچ درخواستی وجود ندارد.", reply_markup=get_admin_panel_keyboard())
            return
        lines = "📋 درخواست‌های کاربران:\n\n"
        keyboard = []
        # show a few recent
        for rid, r in list(user_reqs.items())[-50:]:
            lines += f"• {r.get('type')}-{rid} — user:{r.get('user_id')} — status:{r.get('status')}\n"
            keyboard.append([InlineKeyboardButton(f"مشاهده {rid}", callback_data=f"admin_view_userreq_{rid}")])
        await update.message.reply_text(lines, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if text == "📦 مدیریت محصولات":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        products = load_products(context)
        if not products.get("items"):
            await update.message.reply_text("📦 هیچ محصولی وجود ندارد!\n\nبرای افزودن محصول جدید روی دکمه زیر کلیک کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ افزودن محصول جدید", callback_data="add_product")]]))
            return
        await update.message.reply_text("📦 مدیریت محصولات:\n\nروی هر محصول کلیک کنید تا ویرایش یا حذف کنید.\n➕ برای افزودن محصول جدید کلیک کنید.", reply_markup=get_products_admin_keyboard(context))
        return

    if text == "💎 مدیریت کسب درآمد":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        earnings = load_earnings(context)
        if not earnings.get("items"):
            await update.message.reply_text("💎 هیچ روش کسب درآمدی وجود ندارد!\n\nبرای افزودن روش جدید روی دکمه زیر کلیک کنید.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ افزودن کسب درآمد جدید", callback_data="add_earn")]]))
            return
        await update.message.reply_text("💎 مدیریت کسب درآمد:\n\nروی هر روش کلیک کنید تا ویرایش یا حذف کنید.\n➕ برای افزودن روش جدید کلیک کنید.", reply_markup=get_earnings_admin_keyboard(context))
        return

    if text == "📊 آمار کلی":
        if not is_admin(user.id, context):
            await update.message.reply_text("❌ شما دسترسی ندارید!")
            return
        users = load_users(context)
        total_users = len(users)
        total_balance = sum(u.get("balance", 0) for u in users.values())
        total_withdrawable = sum(u.get("withdrawable", 0) for u in users.values())
        total_profits = sum(u.get("profits", 0) for u in users.values())
        total_orders = sum(u.get("orders", 0) for u in users.values())
        total_subscribers = sum(u.get("subscribers", 0) for u in users.values())
        await update.message.reply_text(f"📊 آمار کلی ربات:\n\n━━━━━━━━━━━━━━━\n👥 تعداد کاربران: {total_users} نفر\n💰 مجموع موجودی: {total_balance:,} تومان\n💳 مجموع قابل برداشت: {total_withdrawable:,} تومان\n📤 مجموع تسویه شده: {total_profits:,} تومان\n☎️ مجموع خریدها: {total_orders} عدد\n👥 مجموع زیرمجموعه‌ها: {total_subscribers} نفر\n━━━━━━━━━━━━━━━", parse_mode="HTML")
        return

    # User main features
    if text == "💰 کسب درآمد":
        earnings = load_earnings(context)
        if not earnings.get("items"):
            await update.message.reply_text("❌ در حال حاضر هیچ روش کسب درآمدی وجود ندارد!", reply_markup=get_panel_with_back_keyboard())
            return
        await send_section_emoji(context, user.id, "earn_list")
        await update.message.reply_text("💎 لیست روش‌های کسب درآمد:\n\nروی هر گزینه کلیک کنید تا جزئیات را ببینید:", reply_markup=get_earnings_list_keyboard(context))
        return

    if text == "🛍 خرید محصول تخفیف":
        products = load_products(context)
        if not products.get("items"):
            await update.message.reply_text("❌ در حال حاضر هیچ محصولی برای خرید وجود ندارد!", reply_markup=get_panel_with_back_keyboard())
            return
        await send_section_emoji(context, user.id, "buy_list")
        await update.message.reply_text("🛍 لیست محصولات تخفیف:\n\nروی هر محصول کلیک کنید تا جزئیات و خرید کنید:", reply_markup=get_products_list_keyboard(context))
        return

    if text == "⚡️ پروفایل من":
        text_msg, keyboard = build_profile_view(uid, info)
        await send_section_emoji(context, user.id, "profile")
        await update.message.reply_text(
            text_msg,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return

    if text == "🛠 پشتیبانی":
        SUPPORT_SESSIONS[uid] = True
        context.user_data['in_support'] = True
        await send_section_emoji(context, user.id, "support")
        await update.message.reply_text("🛠 شما به پشتیبانی متصل شدید.\n\nلطفاً پیام خود را بنویسید. تمام پیام‌های شما به ادمین‌ها ارسال خواهد شد.\n\nبرای پایان دادن به پشتیبانی، روی دکمه زیر کلیک کنید:", reply_markup=ReplyKeyboardMarkup([["🔚 پایان پشتیبانی"]], resize_keyboard=True))
        await notify_admins(context, f"🔔 کاربر جدید به پشتیبانی متصل شد!\n\n👤 نام: {user.first_name}\n🆔 آیدی: <code>{uid}</code>\n📱 یوزرنیم: @{user.username if user.username else 'ندارد'}")
        return

    if text == "👥 زیرمجموعه گیری":
        referral_code = info.get("referral_code")
        if not referral_code:
            referral_code = generate_referral_code()
            users[uid]["referral_code"] = referral_code
            schedule_coro(save_users(context, users))
        referral_link = f"https://t.me/beneficial1bot?start=ref_{referral_code}"
        await send_section_emoji(context, user.id, "subteam")
        await update.message.reply_text(f"🔗 ربات کسب سود آنی و مطمئن\n\n━━━━━━━━━━━━━━━\n👥 لینک زیرمجموعه گیری شما:\n<code>{referral_link}</code>\n\n💡 با اشتراک‌گذاری این لینک، از هر کاربری که عضو شود، سود دریافت خواهید کرد!\n👥 تعداد زیرمجموعه شما: {info.get('subscribers', 0)} نفر\n━━━━━━━━━━━━━━━", parse_mode="HTML", reply_markup=get_panel_with_back_keyboard())
        return

    if text == "📖 راهنما":
        faq = load_help_faq(context)
        items = faq.get("items", [])
        await send_section_emoji(context, user.id, "help")
        if not items:
            await update.message.reply_text("📖 در حال حاضر سوالی ثبت نشده است.", reply_markup=get_panel_with_back_keyboard())
            return
        keyboard = [[InlineKeyboardButton(it.get("title", "-"), callback_data=f"faqitem_{it.get('id')}")] for it in items]
        await update.message.reply_text("📖 رایج ترین سوال ها:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # deposit, withdraw flows
    if context.user_data.get('pending_deposit'):
        try:
            amount = int(text.replace(',', '').strip())
            if amount <= 0:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید:", reply_markup=get_panel_with_back_keyboard())
                return
            context.user_data['pending_amount'] = amount
            await update.message.reply_text(f"💵 مبلغ واریز مد نظر: <code>{amount:,}</code> تومان\n\n💳 شماره کارت:\n<code>6666555544443333</code>\n(برای کپی کردن روی شماره کارت کلیک کنید)\n\n🏦 بانک: بلو بانک\n\n📤 پس از واریز، تصویر رسید را ارسال کنید.\n\n⚠️ توجه: پس از تایید توسط پشتیبانی، موجودی شما اضافه خواهد شد.", parse_mode="HTML", reply_markup=get_panel_with_back_keyboard())
            context.user_data['waiting_for_payment_image'] = True
        except ValueError:
            await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید:", reply_markup=get_panel_with_back_keyboard())
        return

    if context.user_data.get('pending_withdraw'):
        try:
            amount = int(text.replace(',', '').strip())
            if amount <= 0:
                await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید:", reply_markup=get_panel_with_back_keyboard())
                return
            withdrawable = info.get("withdrawable", 0)
            if amount > withdrawable:
                await update.message.reply_text(f"❌ موجودی قابل برداشت شما کافی نیست!\n\n💳 موجودی قابل برداشت شما: <code>{withdrawable:,}</code> تومان", parse_mode="HTML", reply_markup=get_panel_with_back_keyboard())
                return
            context.user_data['withdraw_amount'] = amount
            context.user_data['pending_withdraw'] = False
            context.user_data['waiting_for_card_number'] = True
            await update.message.reply_text("💳 لطفاً شماره کارت خود را برای واریز وارد کنید:\n\n📌 شماره کارت باید ۱۶ رقمی باشد.\nمثال: 6037991801203645", reply_markup=get_panel_with_back_keyboard())
        except ValueError:
            await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید:", reply_markup=get_panel_with_back_keyboard())
        return

    if context.user_data.get('waiting_for_card_number'):
        card_number = text.strip().replace(' ', '').replace('-', '')
        if not card_number.isdigit() or len(card_number) != 16:
            await update.message.reply_text("❌ شماره کارت وارد شده صحیح نیست!\n\n📌 شماره کارت باید ۱۶ رقمی باشد.\nمثال: 6037991801203645\n\nلطفاً مجدداً شماره کارت خود را وارد کنید:", reply_markup=get_panel_with_back_keyboard())
            return
        amount = context.user_data.get('withdraw_amount')
        if not amount:
            await update.message.reply_text("❌ خطا در پردازش، لطفاً مجدداً اقدام کنید.", reply_markup=get_panel_with_back_keyboard())
            context.user_data.clear()
            return
        request_id = generate_request_id("withdraw", uid, amount)
        pending_requests = load_pending_requests(context)
        pending_requests[request_id] = {
            "type": "withdraw",
            "user_id": uid,
            "amount": amount,
            "card_number": card_number,
            "user_name": user.first_name,
            "date": datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
        }
        schedule_coro(save_pending_requests(context, pending_requests))
        # add to user_requests for visibility in profile
        user_reqs = load_user_requests(context)
        user_reqs[request_id] = {
            "id": request_id,
            "user_id": uid,
            "type": "withdraw",
            "amount": amount,
            "status": "pending",
            "date": pending_requests[request_id]["date"]
        }
        schedule_coro(save_user_requests(context, user_reqs))
        # deduct withdrawable AND balance immediately (holds both until admin approves/rejects)
        users = load_users(context)
        if uid in users:
            users[uid]["withdrawable"] = users[uid].get("withdrawable", 0) - amount
            users[uid]["balance"] = users[uid].get("balance", 0) - amount
            schedule_coro(save_users(context, users))
        context.user_data.clear()
        await update.message.reply_text("✅ درخواست برداشت شما ثبت شد!\n\n⏳ در حال انتظار برای تایید ادمین...", reply_markup=get_panel_with_back_keyboard())
        # send to variz channel for admin confirmation
        caption = f"📤 درخواست برداشت موجودی\n\n👤 کاربر: {user.first_name}\n🆔 شناسه: <code>{uid}</code>\n💳 مبلغ: <code>{amount:,}</code> تومان\n🏦 شماره کارت مقصد:\n<code>{card_number}</code>\n📅 تاریخ: {pending_requests[request_id]['date']}"
        try:
            await context.bot.send_message(chat_id=VARIZ_CHANNEL, text=caption, parse_mode="HTML", reply_markup=get_confirm_keyboard(request_id))
        except Exception as e:
            print(f"Error sending withdraw to VARIZ_CHANNEL: {e}")
        return

    # admin adding product handled earlier

# ==========================
# افزودن روش کسب درآمد (متن‌ها)
# ==========================

async def handle_add_earn_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('adding_earn'):
        return False
    text = update.message.text
    if text == "🔙 بازگشت":
        context.user_data.pop('adding_earn', None)
        context.user_data.pop('earn_step', None)
        context.user_data.pop('earn_data', None)
        await update.message.reply_text("❌ افزودن روش کسب درآمد لغو شد.", reply_markup=get_admin_panel_keyboard())
        return True
    step = context.user_data.get('earn_step', 0)
    earn_data = context.user_data.get('earn_data', {})
    if step == 0:
        earn_data['name'] = text.strip()
        context.user_data['earn_step'] = 1
        await update.message.reply_text("✏️ لطفاً توضیحات روش کسب درآمد را وارد کنید:", reply_markup=get_panel_with_back_keyboard())
    elif step == 1:
        earn_data['description'] = text.strip()
        context.user_data['earn_step'] = 2
        await update.message.reply_text("✏️ لطفاً میزان سود کاربر را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=get_panel_with_back_keyboard())
    elif step == 2:
        try:
            reward = int(text.replace(',', '').strip())
            if reward <= 0:
                await update.message.reply_text("❌ سود باید بزرگتر از 0 باشد!", reply_markup=get_panel_with_back_keyboard())
                return True
            earn_data['reward'] = reward
            context.user_data['earn_step'] = 3
            await update.message.reply_text("✏️ لطفاً کد معرف را وارد کنید:", reply_markup=get_panel_with_back_keyboard())
        except ValueError:
            await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید!", reply_markup=get_panel_with_back_keyboard())
    elif step == 3:
        earn_data['code'] = text.strip()
        context.user_data['earn_step'] = 4
        await update.message.reply_text("✏️ لطفاً لینک دانلود یا لینک سایت را وارد کنید:\n\nمثال: https://example.com", reply_markup=get_panel_with_back_keyboard())
    elif step == 4:
        earn_data['link'] = text.strip()
        context.user_data['earn_step'] = 5
        context.user_data['earn_tutorial'] = []
        await update.message.reply_text("📎 لطفاً عکس یا ویدیوی آموزش را ارسال کنید:\n\n(میتوانید چندین عکس یا یک ویدیو ارسال کنید)\nپس از اتمام، دکمه «پایان» را بزنید.", reply_markup=ReplyKeyboardMarkup([["✅ پایان آموزش"]], resize_keyboard=True))
    elif step == 5:
        if text == "✅ پایان آموزش":
            context.user_data['earn_data'] = earn_data
            default_steps = [
                {"name": "اسکرین شات", "type": "photo", "selected": True},
                {"name": "شماره موبایل ثبت نام شده", "type": "text", "selected": True},
                {"name": "ساعت و دقیقه دقیق ثبت نام", "type": "text", "selected": True},
                {"name": "۴ رقم آخر شماره موبایل", "type": "text", "selected": True}
            ]
            context.user_data['temp_steps'] = default_steps
            keyboard = []
            for i, step_item in enumerate(default_steps):
                status = "✅" if step_item.get("selected", False) else "⬜"
                keyboard.append([InlineKeyboardButton(f"{status} {step_item.get('name')}", callback_data=f"temp_step_toggle_{i}")])
            keyboard.append([InlineKeyboardButton("💾 ذخیره نهایی", callback_data="temp_step_save")])
            keyboard.append([InlineKeyboardButton("🔙 انصراف", callback_data="temp_step_cancel")])
            await update.message.reply_text("✏️ مراحل تایید را انتخاب کنید:\n\nروی هر گزینه کلیک کنید تا فعال/غیرفعال شود.\nسپس روی «ذخیره نهایی» کلیک کنید.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text("لطفاً برای اتمام، دکمه «✅ پایان آموزش» را بزنید.", reply_markup=ReplyKeyboardMarkup([["✅ پایان آموزش"]], resize_keyboard=True))
    context.user_data['earn_data'] = earn_data
    return True

# ==========================
# افزودن محصول جدید (مرحله به مرحله: نام -> قیمت -> توضیحات)
# ==========================

async def handle_add_product_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('adding_product'):
        return False
    text = update.message.text
    if text == "🔙 بازگشت":
        context.user_data.pop('adding_product', None)
        context.user_data.pop('add_step', None)
        context.user_data.pop('product_data', None)
        await update.message.reply_text("❌ افزودن محصول لغو شد.", reply_markup=get_admin_panel_keyboard())
        return True

    step = context.user_data.get('add_step', 0)
    product_data = context.user_data.get('product_data', {})

    if step == 0:
        name = text.strip()
        if not name:
            await update.message.reply_text("❌ نام محصول نمی‌تواند خالی باشد!", reply_markup=get_panel_with_back_keyboard())
            return True
        product_data['name'] = name
        context.user_data['add_step'] = 1
        await update.message.reply_text("✏️ لطفاً قیمت محصول را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=get_panel_with_back_keyboard())

    elif step == 1:
        try:
            price = int(text.replace(',', '').strip())
            if price <= 0:
                await update.message.reply_text("❌ قیمت باید بزرگتر از 0 باشد!", reply_markup=get_panel_with_back_keyboard())
                return True
        except ValueError:
            await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید!", reply_markup=get_panel_with_back_keyboard())
            return True
        product_data['price'] = price
        context.user_data['add_step'] = 2
        await update.message.reply_text("✏️ لطفاً توضیحات محصول را وارد کنید:", reply_markup=get_panel_with_back_keyboard())

    elif step == 2:
        description = text.strip()
        product_data['description'] = description

        products = load_products(context)
        products.setdefault("items", [])
        product_id = f"prod_{len(products['items']) + 1}_{int(time.time())}"
        products["items"].append({
            "id": product_id,
            "name": product_data.get('name'),
            "price": product_data.get('price'),
            "description": product_data.get('description'),
        })
        schedule_coro(save_products(context, products))

        context.user_data.pop('adding_product', None)
        context.user_data.pop('add_step', None)
        context.user_data.pop('product_data', None)

        await update.message.reply_text(
            f"✅ محصول با موفقیت اضافه شد!\n\n📌 نام: {product_data.get('name')}\n💰 قیمت: {product_data.get('price'):,} تومان\n📝 توضیحات: {product_data.get('description')}",
            reply_markup=get_admin_panel_keyboard()
        )
        return True

    context.user_data['product_data'] = product_data
    return True

# ==========================
# افزودن/ویرایش سوالات راهنما (FAQ)
# ==========================

async def handle_faq_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get('faq_action')
    if not action:
        return False
    text = update.message.text
    if text == "🔙 بازگشت":
        context.user_data.pop('faq_action', None)
        context.user_data.pop('faq_data', None)
        context.user_data.pop('faq_target', None)
        await update.message.reply_text("❌ عملیات لغو شد.", reply_markup=get_admin_panel_keyboard())
        return True

    if action == "add_title":
        context.user_data['faq_data'] = {"title": text.strip()}
        context.user_data['faq_action'] = "add_desc"
        await update.message.reply_text("✏️ لطفاً توضیحات این سوال را وارد کنید:", reply_markup=get_panel_with_back_keyboard())
        return True

    if action == "add_desc":
        faq_data = context.user_data.get('faq_data', {})
        faq_data['description'] = text.strip()
        faq = load_help_faq(context)
        faq.setdefault("items", [])
        new_id = f"faq_{len(faq['items']) + 1}_{int(time.time())}"
        faq["items"].append({"id": new_id, "title": faq_data.get("title"), "description": faq_data.get("description")})
        schedule_coro(save_help_faq(context, faq))
        context.user_data.pop('faq_action', None)
        context.user_data.pop('faq_data', None)
        await update.message.reply_text(f"✅ سوال جدید اضافه شد!\n\n📌 {faq_data.get('title')}", reply_markup=get_admin_panel_keyboard())
        return True

    if action == "edit_title":
        faq_id = context.user_data.get('faq_target')
        faq = load_help_faq(context)
        item = next((it for it in faq.get("items", []) if it.get("id") == faq_id), None)
        if item:
            item['title'] = text.strip()
            schedule_coro(save_help_faq(context, faq))
        context.user_data.pop('faq_action', None)
        context.user_data.pop('faq_target', None)
        await update.message.reply_text("✅ عنوان با موفقیت ویرایش شد.", reply_markup=get_admin_panel_keyboard())
        return True

    if action == "edit_desc":
        faq_id = context.user_data.get('faq_target')
        faq = load_help_faq(context)
        item = next((it for it in faq.get("items", []) if it.get("id") == faq_id), None)
        if item:
            item['description'] = text.strip()
            schedule_coro(save_help_faq(context, faq))
        context.user_data.pop('faq_action', None)
        context.user_data.pop('faq_target', None)
        await update.message.reply_text("✅ توضیحات با موفقیت ویرایش شد.", reply_markup=get_admin_panel_keyboard())
        return True

    return False

# ==========================
# دریافت رسانه (عکس/ویدیو) برای آموزش‌ها
# ==========================

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id, context):
        await update.message.reply_text("❌ شما دسترسی به این بخش ندارید!", reply_markup=get_panel_with_back_keyboard())
        return
    if context.user_data.get('earn_step') == 5:
        tutorial = context.user_data.get('earn_tutorial', [])
        if update.message.photo:
            photo = update.message.photo[-1]
            tutorial.append({"type": "photo", "file_id": photo.file_id})
            context.user_data['earn_tutorial'] = tutorial
            await update.message.reply_text(f"✅ عکس با موفقیت دریافت شد! ({len(tutorial)} فایل)\nمی‌توانید فایل‌های بیشتری ارسال کنید یا دکمه «پایان» را بزنید.", reply_markup=ReplyKeyboardMarkup([["✅ پایان آموزش"]], resize_keyboard=True))
        elif update.message.video:
            video = update.message.video
            tutorial.append({"type": "video", "file_id": video.file_id})
            context.user_data['earn_tutorial'] = tutorial
            await update.message.reply_text(f"✅ ویدیو با موفقیت دریافت شد! ({len(tutorial)} فایل)\nمی‌توانید فایل‌های بیشتری ارسال کنید یا دکمه «پایان» را بزنید.", reply_markup=ReplyKeyboardMarkup([["✅ پایان آموزش"]], resize_keyboard=True))
        else:
            await update.message.reply_text("❌ لطفاً یک عکس یا ویدیو ارسال کنید.", reply_markup=ReplyKeyboardMarkup([["✅ پایان آموزش"]], resize_keyboard=True))
        return

# ==========================
# دریافت عکس رسید و پشتیبانی
# ==========================

async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.user_data.get('emoji_target'):
        return
    if not is_admin(user.id, context):
        context.user_data.pop('emoji_target', None)
        return
    key = context.user_data.pop('emoji_target')
    emoji_settings = load_emoji_settings(context)
    emoji_settings[key] = {"type": "sticker", "value": update.message.sticker.file_id}
    schedule_coro(save_emoji_settings(context, emoji_settings))
    await update.message.reply_text("✅ استیکر این بخش با موفقیت تنظیم شد.", reply_markup=get_admin_panel_keyboard())

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)

    if context.user_data.get('earning_submit'):
        await handle_earn_submit(update, context)
        return

    if context.user_data.get('earn_step') == 5:
        await handle_media(update, context)
        return

    if context.user_data.get('in_support'):
        await forward_photo_to_admins(update, context)
        await update.message.reply_text("✅ عکس شما به پشتیبانی ارسال شد.\n\nمنتظر پاسخ ادمین باشید.", reply_markup=ReplyKeyboardMarkup([["🔚 پایان پشتیبانی"]], resize_keyboard=True))
        return

    if not context.user_data.get('waiting_for_payment_image'):
        await update.message.reply_text("❌ لطفاً ابتدا از طریق دکمه «افزایش موجودی» اقدام کنید.", reply_markup=get_panel_with_back_keyboard())
        return

    photo = update.message.photo[-1]
    amount = context.user_data.get('pending_amount')
    if not amount:
        await update.message.reply_text("❌ خطا در پردازش، لطفاً مجدداً اقدام کنید.", reply_markup=get_panel_with_back_keyboard())
        context.user_data['waiting_for_payment_image'] = False
        return
    request_id = generate_request_id("deposit", uid, amount)
    pending_requests = load_pending_requests(context)
    pending_requests[request_id] = {
        "type": "deposit",
        "user_id": uid,
        "amount": amount,
        "user_name": user.first_name,
        "date": datetime.now().strftime("%Y/%m/%d - %H:%M:%S"),
        "photo_id": photo.file_id
    }
    schedule_coro(save_pending_requests(context, pending_requests))
    # add to user_requests
    user_reqs = load_user_requests(context)
    user_reqs[request_id] = {
        "id": request_id,
        "user_id": uid,
        "type": "deposit",
        "amount": amount,
        "status": "pending",
        "date": pending_requests[request_id]['date']
    }
    schedule_coro(save_user_requests(context, user_reqs))
    # notify admins channel
    caption = f"📥 درخواست افزایش موجودی\n\n👤 کاربر: {user.first_name}\n🆔 شناسه: <code>{uid}</code>\n💳 مبلغ: <code>{amount:,}</code> تومان\n📅 تاریخ: {pending_requests[request_id]['date']}"
    try:
        await context.bot.send_photo(chat_id=VARIZ_CHANNEL, photo=photo.file_id, caption=caption, parse_mode="HTML", reply_markup=get_confirm_keyboard(request_id))
        context.user_data['waiting_for_payment_image'] = False
        context.user_data['pending_amount'] = None
        context.user_data['pending_deposit'] = False
        await update.message.reply_text("✅ رسید شما با موفقیت ارسال شد!\n\n⏳ در حال انتظار برای تایید ادمین...", reply_markup=get_panel_with_back_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ارسال رسید: {str(e)}", reply_markup=get_panel_with_back_keyboard())

# ==========================
# درخواست‌های کسب درآمد (ارسال مرحله به مرحله)
# ==========================

async def handle_earn_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    text = (update.message.text or "").strip()

    if text == "🔙 بازگشت":
        context.user_data.clear()
        try:
            await safe_delete_message(update.message)
        except Exception:
            pass
        if is_admin(user.id, context):
            await update.message.reply_text("✨ به منوی اصلی بازگشتید:", reply_markup=get_admin_main_keyboard())
        else:
            await update.message.reply_text("✨ به منوی اصلی بازگشتید:", reply_markup=get_main_keyboard())
        return

    earn_id = context.user_data.get('current_earn_id')
    if not earn_id:
        await update.message.reply_text("❌ خطا در پردازش، لطفاً مجدداً اقدام کنید.", reply_markup=get_panel_with_back_keyboard())
        return

    earnings = load_earnings(context)
    item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
    if not item:
        await update.message.reply_text("❌ این روش کسب درآمد وجود ندارد!", reply_markup=get_panel_with_back_keyboard())
        return

    steps = item.get("steps", [])
    current_step = context.user_data.get('earn_step_index', 0)

    # if there are zero steps, just submit
    if not steps:
        await submit_earn_request(update, context, item)
        return

    if current_step >= len(steps):
        await show_earn_review(update, context, item)
        return

    step = steps[current_step]
    step_type = step.get("type", "text")

    if step_type == "text":
        if not update.message.text:
            await update.message.reply_text(f"❌ لطفاً {step.get('name')} را به صورت متن وارد کنید.", reply_markup=get_panel_with_back_keyboard())
            return
        value = update.message.text
    elif step_type == "photo":
        if not update.message.photo:
            await update.message.reply_text(f"❌ لطفاً {step.get('name')} را به صورت عکس ارسال کنید.", reply_markup=get_panel_with_back_keyboard())
            return
        value = update.message.photo[-1].file_id
    else:
        await update.message.reply_text(f"❌ نوع مرحله '{step.get('name')}' نامعتبر است!", reply_markup=get_panel_with_back_keyboard())
        return

    step_data = context.user_data.get('earn_step_data', {})
    step_data[f"step_{current_step}"] = value
    context.user_data['earn_step_data'] = step_data
    context.user_data['earn_step_index'] = current_step + 1

    if current_step + 1 < len(steps):
        next_step = steps[current_step + 1]
        guide = " (لطفاً یک عکس ارسال کنید)" if next_step.get("type") == "photo" else " (لطفاً متن را وارد کنید)"
        await update.message.reply_text(f"📝 مرحله {current_step + 2} از {len(steps)}: {next_step.get('name')}{guide}", reply_markup=get_panel_with_back_keyboard())
    else:
        await show_earn_review(update, context, item)

async def show_earn_review(update: Update, context: ContextTypes.DEFAULT_TYPE, item):
    """صفحه مرور نهایی: قبل از ارسال به کانال، همه‌ی مقادیر وارد شده توسط کاربر را نشان می‌دهد."""
    user = update.effective_user
    step_data = context.user_data.get('earn_step_data', {})
    steps = item.get("steps", [])
    lines = ["📋 لطفاً اطلاعات وارد شده رو بررسی کن:\n"]
    for i, step in enumerate(steps):
        value = step_data.get(f"step_{i}", "نامشخص")
        if step.get("type") == "photo":
            value = "📷 عکس ارسال شد"
        lines.append(f"📌 {step.get('name')}: {value}")
    text_msg = "\n".join(lines)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تایید و ارسال درخواست", callback_data="earn_review_confirm")],
        [InlineKeyboardButton("✏️ ورود مجدد اطلاعات", callback_data="earn_review_restart")]
    ])
    await context.bot.send_message(chat_id=user.id, text=text_msg, reply_markup=keyboard)

async def submit_earn_request(update: Update, context: ContextTypes.DEFAULT_TYPE, item):
    user = update.effective_user
    uid = str(user.id)
    earn_id = item.get("id")
    step_data = context.user_data.get('earn_step_data', {})

    request_text = f"📥 درخواست ثبت نام - {item.get('name')}\n\n"
    request_text += f"👤 کاربر: {user.first_name}\n"
    request_text += f"🆔 شناسه: <code>{uid}</code>\n"
    request_text += f"📅 تاریخ: {datetime.now().strftime('%Y/%m/%d - %H:%M:%S')}\n\n"
    request_text += "━━━━━━━━━━━━━━━\n"
    steps = item.get("steps", [])
    for i, step in enumerate(steps):
        value = step_data.get(f"step_{i}", "نامشخص")
        request_text += f"📌 {step.get('name')}: {value}\n"
    request_text += "━━━━━━━━━━━━━━━"

    earn_requests = load_earn_requests(context)
    request_id = generate_request_id("earn", uid, 0)
    earn_requests[request_id] = {
        "user_id": uid,
        "earn_id": earn_id,
        "earn_name": item.get("name"),
        "data": step_data,
        "reward": item.get("reward", 0),
        "status": "pending",
        "date": datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
    }
    schedule_coro(save_earn_requests(context, earn_requests))

    # add to user_requests
    user_reqs = load_user_requests(context)
    user_reqs[request_id] = {
        "id": request_id,
        "user_id": uid,
        "type": "earn",
        "earn_id": earn_id,
        "earn_name": item.get("name"),
        "amount": item.get("reward", 0),
        "status": "pending",
        "date": earn_requests[request_id]['date']
    }
    schedule_coro(save_user_requests(context, user_reqs))

    # send to confirm channel
    try:
        await context.bot.send_message(chat_id=CONFIRM_CHANNEL, text=request_text, parse_mode="HTML", reply_markup=get_earn_confirm_keyboard(request_id))
    except Exception as e:
        print(f"Error sending to confirm channel: {e}")
        # fallback: در صورت خطا در ارسال به کانال، مستقیم برای ادمین‌ها ارسال شود تا درخواست گم نشود
        try:
            for admin_id in load_admins(context).get("admins", []):
                await context.bot.send_message(chat_id=admin_id, text=f"⚠️ ارسال به کانال تایید با خطا مواجه شد!\n\n{request_text}", parse_mode="HTML", reply_markup=get_earn_confirm_keyboard(request_id))
        except Exception as e2:
            print(f"Error sending fallback to admins: {e2}")

    context.user_data.clear()
    await context.bot.send_message(chat_id=user.id, text=f"✅ درخواست شما برای {item.get('name')} ارسال شد!\n\n⏳ طی 30 ثانیه الی 1 ساعت بررسی میشود و در صورت ثبت نام، هدیه برای شما واریز خواهد شد.", reply_markup=get_panel_with_back_keyboard())

# ==========================
# مدیریت دکمه‌های شیشه‌ای (CallbackQueryHandler)
# ==========================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = update.effective_user
    try:
        await query.answer()
    except Exception:
        pass

    def split_tail(payload):
        if "_" in payload:
            head, tail = payload.rsplit("_", 1)
            return head, tail
        return payload, ""

    # temp step toggles/save/cancel (same as before)
    if data.startswith("temp_step_toggle_"):
        idx = int(data.replace("temp_step_toggle_", ""))
        steps = context.user_data.get('temp_steps', [])
        if 0 <= idx < len(steps):
            steps[idx]["selected"] = not steps[idx].get("selected", False)
            context.user_data['temp_steps'] = steps
        keyboard = []
        for i, step in enumerate(steps):
            status = "✅" if step.get("selected", False) else "⬜"
            keyboard.append([InlineKeyboardButton(f"{status} {step.get('name')}", callback_data=f"temp_step_toggle_{i}")])
        keyboard.append([InlineKeyboardButton("💾 ذخیره نهایی", callback_data="temp_step_save")])
        keyboard.append([InlineKeyboardButton("🔙 انصراف", callback_data="temp_step_cancel")])
        try:
            await query.edit_message_text("✏️ مراحل تایید را انتخاب کنید:\n\nروی هر گزینه کلیک کنید تا فعال/غیرفعال شود.\nسپس روی «ذخیره نهایی» کلیک کنید.", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise e
        return

    if data == "temp_step_save":
        earn_data = context.user_data.get('earn_data', {})
        steps = context.user_data.get('temp_steps', [])
        selected_steps = [step for step in steps if step.get("selected", False)]
        if not selected_steps:
            await query.edit_message_text("❌ حداقل یک مرحله باید انتخاب شود!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="temp_step_cancel")]]))
            return
        earnings = load_earnings(context)
        earn_id = f"earn_{len(earnings.get('items', [])) + 1}_{int(time.time())}"
        earnings["items"].append({
            "id": earn_id,
            "name": earn_data.get('name'),
            "description": earn_data.get('description'),
            "reward": earn_data.get('reward'),
            "code": earn_data.get('code'),
            "link": earn_data.get('link'),
            "tutorial": context.user_data.get('earn_tutorial', []),
            "steps": selected_steps
        })
        schedule_coro(save_earnings(context, earnings))
        context.user_data.pop('earn_data', None)
        context.user_data.pop('temp_steps', None)
        context.user_data.pop('earn_tutorial', None)
        context.user_data.pop('earn_step', None)
        context.user_data.pop('adding_earn', None)
        try:
            await query.edit_message_text(f"✅ روش کسب درآمد با موفقیت اضافه شد!\n\n📌 نام: {earn_data.get('name')}\n📝 توضیحات: {earn_data.get('description')}\n💰 سود: {earn_data.get('reward'):,} تومان\n🔑 کد معرف: {earn_data.get('code')}\n🔗 لینک: {earn_data.get('link')}\n📎 تعداد فایل‌های آموزشی: {len(context.user_data.get('earn_tutorial', []))} عدد\n📋 مراحل تایید: {len(selected_steps)} مرحله")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text="به پنل ادمین بازگشتید:", reply_markup=get_admin_panel_keyboard())
        except Exception:
            pass
        return

    if data == "temp_step_cancel":
        context.user_data.pop('earn_data', None)
        context.user_data.pop('temp_steps', None)
        context.user_data.pop('earn_tutorial', None)
        context.user_data.pop('earn_step', None)
        context.user_data.pop('adding_earn', None)
        try:
            await query.edit_message_text("❌ افزودن روش کسب درآمد لغو شد.")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text="به پنل ادمین بازگشتید:", reply_markup=get_admin_panel_keyboard())
        except Exception:
            pass
        return

    # edit existing steps (same logic, omitted for brevity - keep as before)
    if data.startswith("edit_earn_steps_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("edit_earn_steps_", "")
        earnings = load_earnings(context)
        item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
        if not item:
            await query.edit_message_text("❌ این روش کسب درآمد وجود ندارد!", reply_markup=get_earnings_admin_keyboard(context))
            return
        steps = item.get("steps", [])
        context.user_data[f'steps_{earn_id}'] = steps
        keyboard = []
        for i, step in enumerate(steps):
            status = "✅" if step.get("selected", False) else "⬜"
            keyboard.append([InlineKeyboardButton(f"{status} {step.get('name')}", callback_data=f"toggle_existing_step_{earn_id}_{i}")])
        keyboard.append([InlineKeyboardButton("💾 ذخیره مراحل", callback_data=f"save_existing_steps_{earn_id}")])
        try:
            await query.edit_message_text("✏️ مراحل تایید را انتخاب کنید:\n\nروی هر گزینه کلیک کنید تا فعال/غیرفعال شود.\nسپس روی «ذخیره مراحل» کلیک کنید.", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise e
        return

    if data.startswith("toggle_existing_step_"):
        if not is_admin(user.id, context):
            return
        payload = data.replace("toggle_existing_step_", "")
        if "_" not in payload:
            return
        earn_id, idx_str = payload.rsplit("_",1)
        try:
            step_index = int(idx_str)
        except Exception:
            return
        steps = context.user_data.get(f'steps_{earn_id}', [])
        if not steps:
            earnings = load_earnings(context)
            item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
            if item:
                steps = item.get("steps", [])
        if step_index < len(steps):
            steps[step_index]["selected"] = not steps[step_index].get("selected", False)
        context.user_data[f'steps_{earn_id}'] = steps
        keyboard = []
        for i, step in enumerate(steps):
            status = "✅" if step.get("selected", False) else "⬜"
            keyboard.append([InlineKeyboardButton(f"{status} {step.get('name')}", callback_data=f"toggle_existing_step_{earn_id}_{i}")])
        keyboard.append([InlineKeyboardButton("💾 ذخیره مراحل", callback_data=f"save_existing_steps_{earn_id}")])
        try:
            await query.edit_message_text("✏️ مراحل تایید را انتخاب کنید:\n\nروی هر گزینه کلیک کنید تا فعال/غیرفعال شود.\nسپس روی «ذخیره مراحل» کلیک کنید.", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise e
        return

    if data.startswith("save_existing_steps_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("save_existing_steps_", "")
        steps = context.user_data.get(f'steps_{earn_id}', [])
        if not steps:
            await query.edit_message_text("❌ هیچ مرحله‌ای برای ذخیره وجود ندارد!", reply_markup=get_earnings_admin_keyboard(context))
            return
        earnings = load_earnings(context)
        for item in earnings.get("items", []):
            if item.get("id") == earn_id:
                item["steps"] = steps
                schedule_coro(save_earnings(context, earnings))
                break
        context.user_data.pop(f'steps_{earn_id}', None)
        try:
            await query.edit_message_text("✅ مراحل تایید با موفقیت ذخیره شد!")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text="به مدیریت کسب درآمد بازگشتید:", reply_markup=get_earnings_admin_keyboard(context))
        except Exception:
            pass
        return

    # ==========================
    # تایید/رد واریز و برداشت
    # ==========================
    if data.startswith("c_") or data.startswith("r_"):
        if not is_admin(user.id, context):
            return
        action = data[0]
        request_id = data[2:]
        pending_requests = load_pending_requests(context)
        if request_id not in pending_requests:
            await query.answer("❌ این درخواست قبلاً پردازش شده!", show_alert=True)
            return
        request_info = pending_requests[request_id]
        request_type = request_info.get("type")
        target_uid = request_info.get("user_id")
        amount = request_info.get("amount")
        users = load_users(context)

        if request_type == "deposit":
            if action == "c":
                if target_uid in users:
                    users[target_uid]["balance"] = users[target_uid].get("balance", 0) + amount
                    users[target_uid]["withdrawable"] = users[target_uid].get("withdrawable", 0) + amount
                    users[target_uid]["deposits_count"] = users[target_uid].get("deposits_count", 0) + 1
                    users[target_uid]["deposits_sum"] = users[target_uid].get("deposits_sum", 0) + amount
                    schedule_coro(save_users(context, users))
                    # update user_requests
                    confirmed_at = datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
                    user_reqs = load_user_requests(context)
                    if request_id in user_reqs:
                        user_reqs[request_id]['status'] = 'confirmed'
                        user_reqs[request_id]['updated_at'] = confirmed_at
                        user_reqs[request_id]['confirmed_at'] = confirmed_at
                        schedule_coro(save_user_requests(context, user_reqs))
                    try:
                        await context.bot.send_message(chat_id=int(target_uid), text=f"✅ واریز شما به مبلغ <code>{amount:,}</code> تومان تایید شد!\n\n💰 موجودی جدید: <code>{users[target_uid]['balance']:,}</code> تومان\n💳 قابل برداشت جدید: <code>{users[target_uid]['withdrawable']:,}</code> تومان", parse_mode="HTML")
                    except Exception:
                        pass
                    # edit caption/message to show processed
                    try:
                        if query.message and getattr(query.message, 'caption', None):
                            await query.edit_message_caption(caption=f"{query.message.caption}\n\n✅ تایید شد توسط: {user.first_name}", reply_markup=None)
                        else:
                            await query.edit_message_text(text=f"{query.message.text}\n\n✅ تایید شد توسط: {user.first_name}", reply_markup=None)
                    except Exception:
                        pass
                    await query.answer("✅ واریز تایید شد!", show_alert=True)
            elif action == "r":
                # return notification to user and restore nothing (deposit was not deducted yet)
                try:
                    await context.bot.send_message(chat_id=int(target_uid), text=f"❌ واریز شما به مبلغ <code>{amount:,}</code> تومان رد شد!\n\nلطفاً با پشتیبانی تماس بگیرید.", parse_mode="HTML")
                except Exception:
                    pass
                # update user_requests
                user_reqs = load_user_requests(context)
                if request_id in user_reqs:
                    user_reqs[request_id]['status'] = 'rejected'
                    user_reqs[request_id]['updated_at'] = datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
                    schedule_coro(save_user_requests(context, user_reqs))
                try:
                    if query.message and getattr(query.message, 'caption', None):
                        await query.edit_message_caption(caption=f"{query.message.caption}\n\n❌ رد شد توسط: {user.first_name}", reply_markup=None)
                    else:
                        await query.edit_message_text(text=f"{query.message.text}\n\n❌ رد شد توسط: {user.first_name}", reply_markup=None)
                except Exception:
                    pass
                await query.answer("❌ واریز رد شد!", show_alert=True)
            # finally remove pending request
            del pending_requests[request_id]
            schedule_coro(save_pending_requests(context, pending_requests))
            return

        if request_type == "withdraw":
            if action == "c":
                if target_uid in users:
                    # balance و withdrawable از قبل (لحظه ثبت درخواست) کم شده‌اند - اینجا دوباره کم نمی‌کنیم
                    users[target_uid]["profits"] = users[target_uid].get("profits", 0) + amount
                    users[target_uid]["withdrawals_count"] = users[target_uid].get("withdrawals_count", 0) + 1
                    users[target_uid]["withdrawals_sum"] = users[target_uid].get("withdrawals_sum", 0) + amount
                    schedule_coro(save_users(context, users))
                    confirmed_at = datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
                    user_reqs = load_user_requests(context)
                    if request_id in user_reqs:
                        user_reqs[request_id]['status'] = 'confirmed'
                        user_reqs[request_id]['updated_at'] = confirmed_at
                        user_reqs[request_id]['confirmed_at'] = confirmed_at
                        schedule_coro(save_user_requests(context, user_reqs))
                    try:
                        await context.bot.send_message(chat_id=int(target_uid), text=f"✅ برداشت شما به مبلغ <code>{amount:,}</code> تومان تایید شد!\n\n⏳ طی 30 ثانیه الی 30 دقیقه برای شما واریز میشود.", parse_mode="HTML")
                    except Exception:
                        pass
                    try:
                        await query.edit_message_text(text=f"{query.message.text}\n\n✅ تایید شد توسط: {user.first_name}", reply_markup=None)
                    except Exception:
                        pass
                    await query.answer("✅ برداشت تایید شد!", show_alert=True)
            elif action == "r":
                # refund: هم withdrawable هم balance که موقع ثبت درخواست کم شده بودند برگردانده می‌شوند
                if target_uid in users:
                    users[target_uid]["withdrawable"] = users[target_uid].get("withdrawable", 0) + amount
                    users[target_uid]["balance"] = users[target_uid].get("balance", 0) + amount
                    schedule_coro(save_users(context, users))
                    user_reqs = load_user_requests(context)
                    if request_id in user_reqs:
                        user_reqs[request_id]['status'] = 'rejected'
                        user_reqs[request_id]['updated_at'] = datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
                        schedule_coro(save_user_requests(context, user_reqs))
                    try:
                        await context.bot.send_message(chat_id=int(target_uid), text=f"❌ برداشت شما به مبلغ <code>{amount:,}</code> تومان رد شد!\n\n💳 قابل برداشت جدید: <code>{users[target_uid]['withdrawable']:,}</code> تومان", parse_mode="HTML")
                    except Exception:
                        pass
                else:
                    await query.answer("❌ کاربر یافت نشد!", show_alert=True)
                    return
                try:
                    await query.edit_message_text(text=f"{query.message.text}\n\n❌ رد شد توسط: {user.first_name}", reply_markup=None)
                except Exception:
                    pass
                await query.answer("❌ برداشت رد شد!", show_alert=True)
            del pending_requests[request_id]
            schedule_coro(save_pending_requests(context, pending_requests))
            return

    # ==========================
    # تایید/رد درخواست کسب درآمد (ec_/er_)
    # ==========================
    if data.startswith("ec_") or data.startswith("er_"):
        if not is_admin(user.id, context):
            return
        action = data[1]  # 'c' یا 'r' - قبلاً data[0] بود که همیشه 'e' می‌شد (باگ)
        request_id = data[3:]  # قبلاً data[2:] بود که یک زیرخط اضافه باقی می‌گذاشت (باگ)
        earn_requests = load_earn_requests(context)
        if request_id not in earn_requests:
            await query.answer("❌ این درخواست قبلاً پردازش شده!", show_alert=True)
            return
        request_info = earn_requests[request_id]
        target_uid = request_info.get("user_id")
        reward = request_info.get("reward", 0)
        earn_name = request_info.get("earn_name", "کسب درآمد")
        users = load_users(context)
        # confirm
        if action == "c":
            if target_uid in users:
                users[target_uid]["balance"] = users[target_uid].get("balance", 0) + reward
                users[target_uid]["withdrawable"] = users[target_uid].get("withdrawable", 0) + reward
                users[target_uid]["earnings"] = users[target_uid].get("earnings", 0) + 1
                schedule_coro(save_users(context, users))
                # update user_requests
                confirmed_at = datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
                user_reqs = load_user_requests(context)
                if request_id in user_reqs:
                    user_reqs[request_id]['status'] = 'confirmed'
                    user_reqs[request_id]['updated_at'] = confirmed_at
                    user_reqs[request_id]['confirmed_at'] = confirmed_at
                    schedule_coro(save_user_requests(context, user_reqs))
                try:
                    await context.bot.send_message(chat_id=int(target_uid), text=f"✅ ثبت نام شما در {earn_name} تایید شد!\n\n💰 مبلغ <code>{reward:,}</code> تومان به حساب شما اضافه شد.\n\n💳 موجودی جدید: {users[target_uid].get('balance', 0):,} تومان", parse_mode="HTML")
                except Exception:
                    pass
                try:
                    await query.edit_message_text(text=f"{query.message.text}\n\n✅ تایید شد توسط: {user.first_name}", reply_markup=None)
                except Exception:
                    pass
                await query.answer("✅ درخواست تایید شد!", show_alert=True)
            else:
                await query.answer("❌ کاربر یافت نشد!", show_alert=True)
                return
        elif action == "r":
            try:
                await context.bot.send_message(chat_id=int(target_uid), text=f"❌ ثبت نام شما در {earn_name} رد شد!\n\nتوضیح: مدرکی از ثبت نام شما موجود نیست. لطفاً بررسی کنید یا با پشتیبانی تماس بگیرید.")
            except Exception:
                pass
            # update user_requests
            user_reqs = load_user_requests(context)
            if request_id in user_reqs:
                user_reqs[request_id]['status'] = 'rejected'
                user_reqs[request_id]['updated_at'] = datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
                schedule_coro(save_user_requests(context, user_reqs))
            try:
                await query.edit_message_text(text=f"{query.message.text}\n\n❌ رد شد توسط: {user.first_name}", reply_markup=None)
            except Exception:
                pass
            await query.answer("❌ درخواست رد شد!", show_alert=True)
        del earn_requests[request_id]
        schedule_coro(save_earn_requests(context, earn_requests))
        return

    # ==========================
    # محصولات - کاربران
    # ==========================
    if data == "products_back":
        products = load_products(context)
        if not products.get("items"):
            try:
                await query.edit_message_text("❌ در حال حاضر هیچ محصولی برای خرید وجود ندارد!", reply_markup=get_panel_with_back_keyboard())
            except Exception:
                pass
            return
        try:
            await query.edit_message_text("🛍 لیست محصولات تخفیف:\n\nروی هر محصول کلیک کنید تا جزئیات و خرید کنید:", reply_markup=get_products_list_keyboard(context))
        except Exception:
            pass
        return

    if data.startswith("buy_product_"):
        product_id = data.replace("buy_product_", "")
        products = load_products(context)
        item = next((p for p in products.get("items", []) if p.get("id") == product_id), None)
        if not item:
            await query.edit_message_text("❌ این محصول وجود ندارد!", reply_markup=get_products_list_keyboard(context))
            return
        context.user_data['product_count'] = 1
        context.user_data['product_id'] = product_id
        await send_section_emoji(context, user.id, "product_selected")
        await query.edit_message_text(f"🛍 {item.get('name')}\n\n📝 توضیحات:\n{item.get('description', 'توضیحی وجود ندارد')}\n\n💰 قیمت هر عدد: {item.get('price', 0):,} تومان\n📦 تعداد: 1\n💳 مجموع قیمت: {item.get('price', 0):,} تومان\n\nتعداد مورد نظر را با دکمه‌های +/- تنظیم کنید:", reply_markup=get_product_buy_keyboard(product_id))
        return

    if data.startswith("inc_"):
        product_id = data.replace("inc_", "")
        count = context.user_data.get('product_count', 1) + 1
        context.user_data['product_count'] = count
        products = load_products(context)
        item = next((p for p in products.get("items", []) if p.get("id") == product_id), None)
        if item:
            total = item.get('price', 0) * count
            await query.edit_message_text(f"🛍 {item.get('name')}\n\n📝 توضیحات:\n{item.get('description', 'توضیحی وجود ندارد')}\n\n💰 قیمت هر عدد: {item.get('price', 0):,} تومان\n📦 تعداد: {count}\n💳 مجموع قیمت: {total:,} تومان\n\nتعداد مورد نظر را با دکمه‌های +/- تنظیم کنید:", reply_markup=get_product_buy_keyboard(product_id))
        return

    if data.startswith("dec_"):
        product_id = data.replace("dec_", "")
        count = context.user_data.get('product_count', 1) - 1
        if count < 1:
            count = 1
        context.user_data['product_count'] = count
        products = load_products(context)
        item = next((p for p in products.get("items", []) if p.get("id") == product_id), None)
        if item:
            total = item.get('price', 0) * count
            await query.edit_message_text(f"🛍 {item.get('name')}\n\n📝 توضیحات:\n{item.get('description', 'توضیحی وجود ندارد')}\n\n💰 قیمت هر عدد: {item.get('price', 0):,} تومان\n📦 تعداد: {count}\n💳 مجموع قیمت: {total:,} تومان\n\nتعداد مورد نظر را با دکمه‌های +/- تنظیم کنید:", reply_markup=get_product_buy_keyboard(product_id))
        return

    if data.startswith("confirm_buy_"):
        product_id = data.replace("confirm_buy_", "")
        count = context.user_data.get('product_count', 1)
        products = load_products(context)
        item = next((p for p in products.get("items", []) if p.get("id") == product_id), None)
        if not item:
            await query.edit_message_text("❌ این محصول وجود ندارد!", reply_markup=get_products_list_keyboard(context))
            return
        total_price = item.get('price', 0) * count
        uid = str(user.id)
        users = load_users(context)
        balance = users.get(uid, {}).get("balance", 0)
        if balance < total_price:
            # show Increase Balance button instead of Back
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💳 افزایش موجودی", callback_data="add_balance")]])
            await query.edit_message_text(f"❌ موجودی شما کافی نیست!\n\n💰 موجودی شما: {balance:,} تومان\n💳 مبلغ مورد نیاز: {total_price:,} تومان\n\nلطفاً ابتدا موجودی خود را افزایش دهید.", reply_markup=keyboard)
            return
        keyboard = [[InlineKeyboardButton("✅ تایید نهایی", callback_data=f"final_buy_{product_id}_{count}"), InlineKeyboardButton("❌ انصراف", callback_data="products_back")]]
        await query.edit_message_text(f"⚠️ تایید نهایی خرید\n\n🛍 محصول: {item.get('name')}\n📦 تعداد: {count} عدد\n💰 قیمت هر عدد: {item.get('price', 0):,} تومان\n💳 مجموع قیمت: {total_price:,} تومان\n\nآیا از خرید این محصول با این تعداد و قیمت مطمئن هستید؟", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("final_buy_"):
        payload = data.replace("final_buy_", "")
        if "_" not in payload:
            await query.edit_message_text("❌ خطا در پردازش خرید!", reply_markup=get_products_list_keyboard(context))
            return
        product_id, count_str = payload.rsplit("_", 1)
        try:
            count = int(count_str)
        except Exception:
            await query.edit_message_text("❌ تعداد نامعتبر است!", reply_markup=get_products_list_keyboard(context))
            return
        products = load_products(context)
        item = next((p for p in products.get("items", []) if p.get("id") == product_id), None)
        if not item:
            await query.edit_message_text("❌ این محصول وجود ندارد!", reply_markup=get_products_list_keyboard(context))
            return
        total_price = item.get('price', 0) * count
        uid = str(user.id)

        # قفل خرید: جلوگیری از کلیک همزمان/چندباره که می‌توانست موجودی را منفی کند
        async with _purchase_lock:
            users = load_users(context)
            if uid not in users:
                await query.edit_message_text("❌ خطا در پردازش خرید!", reply_markup=get_products_list_keyboard(context))
                return
            # چک مجدد موجودی درست قبل از کسر (چون بین confirm_buy و final_buy ممکن است موجودی تغییر کرده باشد)
            current_balance = users[uid].get("balance", 0)
            if current_balance < total_price:
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💳 افزایش موجودی", callback_data="add_balance")]])
                await query.edit_message_text(f"❌ موجودی شما کافی نیست!\n\n💰 موجودی شما: {current_balance:,} تومان\n💳 مبلغ مورد نیاز: {total_price:,} تومان\n\nلطفاً ابتدا موجودی خود را افزایش دهید.", reply_markup=keyboard)
                return
            # Deduct both balance and withdrawable
            users[uid]["balance"] = current_balance - total_price
            users[uid]["withdrawable"] = max(0, users[uid].get("withdrawable", 0) - total_price)
            users[uid]["orders"] = users[uid].get("orders", 0) + 1
            schedule_coro(save_users(context, users))

            # Create an 'order' user request for visibility in Requests
            req_id = generate_request_id("order", uid, total_price)
            user_reqs = load_user_requests(context)
            user_reqs[req_id] = {
                "id": req_id,
                "user_id": uid,
                "type": "order",
                "product_id": product_id,
                "product_name": item.get("name"),
                "amount": total_price,
                "status": "completed",
                "date": datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
            }
            schedule_coro(save_user_requests(context, user_reqs))

            # ایجاد درخواست ارسال کد تخفیف برای پنل ادمین (چون فعلاً همه محصولات از نوع کد تخفیف هستند)
            discount_requests = load_discount_requests(context)
            discount_requests[req_id] = {
                "id": req_id,
                "user_id": uid,
                "user_name": user.first_name,
                "product_id": product_id,
                "product_name": item.get("name"),
                "amount": total_price,
                "count": count,
                "status": "pending",
                "date": datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
            }
            schedule_coro(save_discount_requests(context, discount_requests))

            order_text = (f"🛍 سفارش جدید\n\n"
                          f"👤 کاربر: {user.first_name}\n"
                          f"🆔 شناسه: <code>{uid}</code>\n"
                          f"📦 محصول: {item.get('name')}\n"
                          f"📦 تعداد: {count} عدد\n"
                          f"💰 قیمت هر عدد: {item.get('price', 0):,} تومان\n"
                          f"💳 مجموع قیمت: {total_price:,} تومان\n"
                          f"📅 تاریخ: {datetime.now().strftime('%Y/%m/%d - %H:%M:%S')}")
            try:
                await context.bot.send_message(chat_id=ORDER_CHANNEL, text=order_text, parse_mode="HTML")
            except Exception as e:
                print(f"Error sending order to channel: {e}")
            await query.edit_message_text(f"✅ سفارش شما با موفقیت ثبت شد!\n\n🛍 محصول: {item.get('name')}\n📦 تعداد: {count} عدد\n💳 مبلغ پرداختی: {total_price:,} تومان\n\n⏳ سفارش شما تایید شد!\nطی 30 ثانیه الی 2 ساعت کد برای شما ارسال میشود.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="products_back")]]))
        return

    # ==========================
    # مدیریت محصولات - admin (edit/delete/add)
    # ==========================
    if data == "products_admin_back":
        try:
            await query.edit_message_text("📦 مدیریت محصولات:\n\nروی هر محصول کلیک کنید تا ویرایش یا حذف کنید.\n➕ برای افزودن محصول جدید کلیک کنید.", reply_markup=get_products_admin_keyboard(context))
        except Exception:
            pass
        return

    if data.startswith("admin_product_"):
        if not is_admin(user.id, context):
            return
        product_id = data.replace("admin_product_", "")
        products = load_products(context)
        item = next((p for p in products.get("items", []) if p.get("id") == product_id), None)
        if not item:
            await query.edit_message_text("❌ این محصول وجود ندارد!", reply_markup=get_products_admin_keyboard(context))
            return
        await query.edit_message_text(f"📦 {item.get('name')}\n\n📝 توضیحات: {item.get('description')}\n💰 قیمت: {item.get('price'):,} تومان\n\nروی هر گزینه کلیک کنید:", reply_markup=get_product_edit_keyboard(product_id))
        return

    if data.startswith("edit_name_"):
        if not is_admin(user.id, context):
            return
        product_id = data.replace("edit_name_", "")
        context.user_data['edit_product_id'] = product_id
        context.user_data['edit_field'] = "name"
        await query.edit_message_text("✏️ نام جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data=f"admin_product_{product_id}")]]))
        return

    if data.startswith("edit_desc_"):
        if not is_admin(user.id, context):
            return
        product_id = data.replace("edit_desc_", "")
        context.user_data['edit_product_id'] = product_id
        context.user_data['edit_field'] = "description"
        await query.edit_message_text("✏️ توضیحات جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data=f"admin_product_{product_id}")]]))
        return

    if data.startswith("edit_price_"):
        if not is_admin(user.id, context):
            return
        product_id = data.replace("edit_price_", "")
        context.user_data['edit_product_id'] = product_id
        context.user_data['edit_field'] = "price"
        await query.edit_message_text("✏️ قیمت جدید را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data=f"admin_product_{product_id}")]]))
        return

    if data.startswith("delete_product_"):
        if not is_admin(user.id, context):
            return
        product_id = data.replace("delete_product_", "")
        await query.edit_message_text(f"⚠️ آیا از حذف این محصول مطمئن هستید؟\n\nاین عمل غیرقابل بازگشت است!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"confirm_del_prod_{product_id}"), InlineKeyboardButton("❌ نه، انصراف", callback_data=f"admin_product_{product_id}")]]))
        return

    if data.startswith("confirm_del_prod_"):
        if not is_admin(user.id, context):
            return
        product_id = data.replace("confirm_del_prod_", "")
        products = load_products(context)
        products["items"] = [p for p in products.get("items", []) if p.get("id") != product_id]
        schedule_coro(save_products(context, products))
        try:
            await query.edit_message_text("✅ محصول با موفقیت حذف شد!")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text="به مدیریت محصولات بازگشتید:", reply_markup=get_products_admin_keyboard(context))
        except Exception:
            pass
        return

    if data == "add_product":
        if not is_admin(user.id, context):
            return
        context.user_data['adding_product'] = True
        context.user_data['add_step'] = 0
        await query.edit_message_text("➕ افزودن محصول جدید\n\nلطفاً نام محصول را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data="products_admin_back")]]))
        return

    # ==========================
    # کسب درآمد - کاربران (tutorial flow)
    # ==========================
    if data == "earnings_back":
        # طبق درخواست: فقط پیام حذف بشه، چیزی جایگزینش نشه
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        return

    if data == "earnings_show_list":
        earnings = load_earnings(context)
        if not earnings.get("items"):
            await query.edit_message_text("❌ در حال حاضر هیچ روش کسب درآمدی وجود ندارد!", reply_markup=get_panel_with_back_keyboard())
            return
        await query.edit_message_text("💎 لیست روش‌های کسب درآمد:\n\nروی هر گزینه کلیک کنید تا جزئیات را ببینید:", reply_markup=get_earnings_list_keyboard(context))
        return

    if data.startswith("faqitem_"):
        faq_id = data.replace("faqitem_", "")
        faq = load_help_faq(context)
        item = next((it for it in faq.get("items", []) if it.get("id") == faq_id), None)
        if not item:
            await query.answer("❌ این سوال پیدا نشد!", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="faq_list")]])
        try:
            await query.edit_message_text(f"📖 {item.get('title')}\n\n{item.get('description')}", reply_markup=keyboard)
        except Exception:
            pass
        return

    if data == "faq_list":
        faq = load_help_faq(context)
        items = faq.get("items", [])
        if not items:
            try:
                await query.edit_message_text("📖 در حال حاضر سوالی ثبت نشده است.")
            except Exception:
                pass
            return
        keyboard = [[InlineKeyboardButton(it.get("title", "-"), callback_data=f"faqitem_{it.get('id')}")] for it in items]
        try:
            await query.edit_message_text("📖 رایج ترین سوال ها:", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            pass
        return

    if data == "add_faq":
        if not is_admin(user.id, context):
            return
        context.user_data['faq_action'] = "add_title"
        await query.edit_message_text("✏️ لطفاً عنوان این سوال را وارد کنید:\n\nمثال: آموزش واریز پول")
        return

    if data.startswith("admin_faq_"):
        if not is_admin(user.id, context):
            return
        faq_id = data.replace("admin_faq_", "")
        faq = load_help_faq(context)
        item = next((it for it in faq.get("items", []) if it.get("id") == faq_id), None)
        if not item:
            await query.answer("❌ این سوال پیدا نشد!", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ ویرایش عنوان", callback_data=f"faq_edit_title_{faq_id}"),
             InlineKeyboardButton("✏️ ویرایش توضیحات", callback_data=f"faq_edit_desc_{faq_id}")],
            [InlineKeyboardButton("🗑 حذف", callback_data=f"faq_delete_{faq_id}")]
        ])
        await query.edit_message_text(f"📌 {item.get('title')}\n\n{item.get('description')}", reply_markup=keyboard)
        return

    if data.startswith("faq_edit_title_"):
        if not is_admin(user.id, context):
            return
        faq_id = data.replace("faq_edit_title_", "")
        context.user_data['faq_action'] = "edit_title"
        context.user_data['faq_target'] = faq_id
        await query.edit_message_text("✏️ عنوان جدید را وارد کنید:")
        return

    if data.startswith("faq_edit_desc_"):
        if not is_admin(user.id, context):
            return
        faq_id = data.replace("faq_edit_desc_", "")
        context.user_data['faq_action'] = "edit_desc"
        context.user_data['faq_target'] = faq_id
        await query.edit_message_text("✏️ توضیحات جدید را وارد کنید:")
        return

    if data.startswith("faq_delete_confirm_"):
        if not is_admin(user.id, context):
            return
        faq_id = data.replace("faq_delete_confirm_", "")
        faq = load_help_faq(context)
        faq["items"] = [it for it in faq.get("items", []) if it.get("id") != faq_id]
        schedule_coro(save_help_faq(context, faq))
        await query.edit_message_text("✅ سوال حذف شد.")
        return

    if data.startswith("faq_delete_"):
        if not is_admin(user.id, context):
            return
        faq_id = data.replace("faq_delete_", "")
        await query.edit_message_text("⚠️ آیا از حذف این سوال مطمئن هستید؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"faq_delete_confirm_{faq_id}"), InlineKeyboardButton("❌ انصراف", callback_data=f"admin_faq_{faq_id}")]]))
        return

    if data.startswith("emoji_sec_"):
        if not is_admin(user.id, context):
            return
        key = data.replace("emoji_sec_", "")
        emoji_settings = load_emoji_settings(context)
        label = EMOJI_SECTIONS.get(key, key)
        if key in emoji_settings:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ تغییر", callback_data=f"emoji_change_{key}"),
                 InlineKeyboardButton("🗑 حذف", callback_data=f"emoji_delete_{key}")]
            ])
            await query.edit_message_text(f"🎨 {label}\n\nبرای این بخش از قبل ایموجی/استیکر تنظیم شده.", reply_markup=keyboard)
        else:
            context.user_data['emoji_target'] = key
            await query.edit_message_text(f"🎨 {label}\n\nلطفاً ایموجی یا استیکری که می‌خواهید برای این بخش ارسال شود را بفرستید:")
        return

    if data.startswith("emoji_change_"):
        if not is_admin(user.id, context):
            return
        key = data.replace("emoji_change_", "")
        context.user_data['emoji_target'] = key
        label = EMOJI_SECTIONS.get(key, key)
        await query.edit_message_text(f"🎨 {label}\n\nلطفاً ایموجی یا استیکر جدید را بفرستید:")
        return

    if data.startswith("emoji_delete_"):
        if not is_admin(user.id, context):
            return
        key = data.replace("emoji_delete_", "")
        emoji_settings = load_emoji_settings(context)
        emoji_settings.pop(key, None)
        schedule_coro(save_emoji_settings(context, emoji_settings))
        await query.edit_message_text("✅ حذف شد.")
        return

    if data == "earn_review_confirm":
        earn_id = context.user_data.get('current_earn_id')
        earnings = load_earnings(context)
        item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
        if not item:
            await query.edit_message_text("❌ خطا در پردازش، لطفاً مجدداً اقدام کنید.")
            return
        await query.edit_message_text("✅ درخواست شما ثبت شد و برای بررسی ارسال گردید.")
        await submit_earn_request(await update_from_query(query), context, item)
        return

    if data == "earn_review_restart":
        earn_id = context.user_data.get('current_earn_id')
        earnings = load_earnings(context)
        item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
        if not item:
            await query.edit_message_text("❌ خطا در پردازش، لطفاً مجدداً اقدام کنید.")
            return
        context.user_data['earn_step_index'] = 0
        context.user_data['earn_step_data'] = {}
        steps = item.get("steps", [])
        first_step = steps[0]
        guide = " (لطفاً یک عکس ارسال کنید)" if first_step.get("type") == "photo" else " (لطفاً متن را وارد کنید)"
        if len(steps) > 1:
            prompt = f"📝 مرحله 1 از {len(steps)}: {first_step.get('name')}{guide}"
        else:
            prompt = f"📝 لطفاً {first_step.get('name')} را وارد کنید:{guide}"
        await query.edit_message_text(prompt)
        return

    if data.startswith("do_earn_"):
        earn_id = data.replace("do_earn_", "")
        earnings = load_earnings(context)
        item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
        if not item:
            await query.edit_message_text("❌ این روش کسب درآمد وجود ندارد!", reply_markup=get_earnings_list_keyboard(context))
            return
        await send_section_emoji(context, user.id, "earn_selected")
        await query.edit_message_text(f"💎 {item.get('name')}\n\n📝 توضیحات:\n{item.get('description', 'توضیحی وجود ندارد')}\n\n💰 سود: {item.get('reward', 0):,} تومان\n🔑 کد معرف: <code>{item.get('code', 'ندارد')}</code>\n🔗 لینک: {item.get('link', 'ندارد')}\n\nبرای مشاهده آموزش تصویری و شروع ثبت نام روی دکمه آموزش تصویری زیر کلیک کنید:", parse_mode="HTML", reply_markup=get_earn_detail_keyboard(earn_id))
        return

    if data.startswith("tutorial_earn_"):
        earn_id = data.replace("tutorial_earn_", "")
        earnings = load_earnings(context)
        item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
        if not item:
            await query.edit_message_text("❌ این روش کسب درآمد وجود ندارد!", reply_markup=get_earnings_list_keyboard(context))
            return

        tutorial = item.get("tutorial", [])
        if not tutorial:
            await query.edit_message_text("❌ برای این روش آموزشی ثبت نشده است!", reply_markup=get_earn_detail_keyboard(earn_id))
            return

        # send tutorial media to user
        for media in tutorial:
            try:
                if media.get("type") == "photo":
                    await context.bot.send_photo(chat_id=user.id, photo=media.get("file_id"))
                    await asyncio.sleep(0.2)
                elif media.get("type") == "video":
                    await context.bot.send_video(chat_id=user.id, video=media.get("file_id"))
                    await asyncio.sleep(0.2)
            except Exception as e:
                print(f"Error sending tutorial: {e}")

        # edit inline message to show options, then pin it so the user doesn't lose it among the tutorial images
        try:
            sent_msg = await query.edit_message_text(f"✅ آموزش تصویری ارسال شد!\n\n💎 {item.get('name')}\n🔑 کد معرف: {item.get('code', 'ندارد')}\n\nثبت‌نام‌تان انجام دادید؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، ثبت‌نام کردم", callback_data=f"yes_earn_{earn_id}")],[InlineKeyboardButton("❌ خیر", callback_data=f"no_earn_{earn_id}")]]))
            try:
                await context.bot.pin_chat_message(chat_id=user.id, message_id=query.message.message_id, disable_notification=True)
            except Exception:
                pass
        except Exception:
            pass
        return

    # When user clicks "✅ بله، ثبت‌نام کردم" — delete the inline message and send step1 prompt as a fresh message
    if data.startswith("yes_earn_"):
        earn_id = data.replace("yes_earn_", "")
        earnings = load_earnings(context)
        item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
        if not item:
            await query.edit_message_text("❌ این روش کسب درآمد وجود ندارد!", reply_markup=get_earnings_list_keyboard(context))
            return
        steps = item.get("steps", [])
        if not steps:
            # no steps defined -> directly submit
            context.user_data['current_earn_id'] = earn_id
            context.user_data['earn_step_index'] = 0
            context.user_data['earn_step_data'] = {}
            context.user_data['earning_submit'] = True
            await query.edit_message_text("✅ آموزش تصویری ارسال شد!\n\nاما این روش مراحل تایید ندارد؛ درخواست ثبت شد.", reply_markup=get_panel_with_back_keyboard())
            await submit_earn_request(await update_from_query(query), context, item)  # helper below
            return
        # delete inline message (unpin first), then send fresh prompt for step1
        try:
            await context.bot.unpin_chat_message(chat_id=user.id, message_id=query.message.message_id)
        except Exception:
            pass
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        context.user_data['current_earn_id'] = earn_id
        context.user_data['earn_step_index'] = 0
        context.user_data['earn_step_data'] = {}
        context.user_data['earning_submit'] = True
        first_step = steps[0]
        first_step_type = first_step.get("type", "text")
        guide = " (لطفاً یک عکس ارسال کنید)" if first_step_type == "photo" else " (لطفاً متن را وارد کنید)"
        if len(steps) > 1:
            prompt = f"📝 مرحله 1 از {len(steps)}: {first_step.get('name')}{guide}"
        else:
            prompt = f"📝 لطفاً {first_step.get('name')} را وارد کنید:{guide}"
        await context.bot.send_message(chat_id=user.id, text=prompt, reply_markup=get_panel_with_back_keyboard())
        return

    # When user clicks "❌ خیر"
    if data.startswith("no_earn_"):
        earn_id = data.replace("no_earn_", "")
        # unpin then delete inline message and send thank you
        try:
            await context.bot.unpin_chat_message(chat_id=user.id, message_id=query.message.message_id)
        except Exception:
            pass
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text="متشکریم؛ اگر خواستید بعداً مجدداً امتحان کنید.", reply_markup=get_panel_with_back_keyboard())
        return

    # ==========================
    # مدیریت کسب درآمد - admin (edit/delete/add)
    # ==========================
    if data == "earnings_admin_back":
        try:
            await query.edit_message_text("💎 مدیریت کسب درآمد:\n\nروی هر روش کلیک کنید تا ویرایش یا حذف کنید.\n➕ برای افزودن روش جدید کلیک کنید.", reply_markup=get_earnings_admin_keyboard(context))
        except Exception:
            pass
        return

    if data.startswith("admin_earn_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("admin_earn_", "")
        earnings = load_earnings(context)
        item = next((e for e in earnings.get("items", []) if e.get("id") == earn_id), None)
        if not item:
            await query.edit_message_text("❌ این روش کسب درآمد وجود ندارد!", reply_markup=get_earnings_admin_keyboard(context))
            return
        steps_text = ""
        for i, step in enumerate(item.get("steps", [])):
            status = "✅" if step.get("selected", False) else "⬜"
            steps_text += f"{status} {step.get('name')}\n"
        await query.edit_message_text(f"💎 {item.get('name')}\n\n📝 توضیحات: {item.get('description')}\n💰 سود: {item.get('reward'):,} تومان\n🔑 کد معرف: {item.get('code', 'ندارد')}\n🔗 لینک: {item.get('link', 'ندارد')}\n📎 تعداد فایل‌های آموزشی: {len(item.get('tutorial', []))} عدد\n📋 مراحل تایید:\n{steps_text}\nروی هر گزینه کلیک کنید:", reply_markup=get_earn_edit_keyboard(earn_id))
        return

    # edit earn fields
    if data.startswith("edit_earn_name_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("edit_earn_name_", "")
        context.user_data['edit_earn_id'] = earn_id
        context.user_data['edit_earn_field'] = "name"
        await query.edit_message_text("✏️ نام جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data=f"admin_earn_{earn_id}")]]))
        return

    if data.startswith("edit_earn_desc_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("edit_earn_desc_", "")
        context.user_data['edit_earn_id'] = earn_id
        context.user_data['edit_earn_field'] = "description"
        await query.edit_message_text("✏️ توضیحات جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data=f"admin_earn_{earn_id}")]]))
        return

    if data.startswith("edit_earn_reward_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("edit_earn_reward_", "")
        context.user_data['edit_earn_id'] = earn_id
        context.user_data['edit_earn_field'] = "reward"
        await query.edit_message_text("✏️ سود جدید را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data=f"admin_earn_{earn_id}")]]))
        return

    if data.startswith("edit_earn_code_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("edit_earn_code_", "")
        context.user_data['edit_earn_id'] = earn_id
        context.user_data['edit_earn_field'] = "code"
        await query.edit_message_text("✏️ کد معرف جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data=f"admin_earn_{earn_id}")]]))
        return

    if data.startswith("edit_earn_link_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("edit_earn_link_", "")
        context.user_data['edit_earn_id'] = earn_id
        context.user_data['edit_earn_field'] = "link"
        await query.edit_message_text("✏️ لینک جدید را وارد کنید:\n\nمثال: https://example.com", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data=f"admin_earn_{earn_id}")]]))
        return

    if data.startswith("edit_earn_tutorial_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("edit_earn_tutorial_", "")
        context.user_data['edit_earn_id'] = earn_id
        context.user_data['edit_earn_field'] = "tutorial"
        context.user_data['earn_tutorial'] = []
        context.user_data['earn_step'] = 5
        try:
            await query.edit_message_text("📎 لطفاً عکس یا ویدیوی جدید برای آموزش ارسال کنید:\n\n(میتوانید چندین عکس یا یک ویدیو ارسال کنید)\nپس از اتمام، دکمه «پایان» را بزنید.")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text="جهت ارسال فایل‌های آموزشی، پیام‌های مدیای خود را ارسال کنید.", reply_markup=ReplyKeyboardMarkup([["✅ پایان آموزش"]], resize_keyboard=True))
        except Exception:
            pass
        return

    if data.startswith("delete_earn_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("delete_earn_", "")
        await query.edit_message_text(f"⚠️ آیا از حذف این روش کسب درآمد مطمئن هستید؟\n\nاین عمل غیرقابل بازگشت است!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"confirm_del_earn_{earn_id}"), InlineKeyboardButton("❌ نه، انصراف", callback_data=f"admin_earn_{earn_id}")]]))
        return

    if data.startswith("confirm_del_earn_"):
        if not is_admin(user.id, context):
            return
        earn_id = data.replace("confirm_del_earn_", "")
        earnings = load_earnings(context)
        earnings["items"] = [e for e in earnings.get("items", []) if e.get("id") != earn_id]
        schedule_coro(save_earnings(context, earnings))
        try:
            await query.edit_message_text("✅ روش کسب درآمد با موفقیت حذف شد!")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text="به مدیریت کسب درآمد بازگشتید:", reply_markup=get_earnings_admin_keyboard(context))
        except Exception:
            pass
        return

    if data == "add_earn":
        if not is_admin(user.id, context):
            return
        context.user_data['adding_earn'] = True
        context.user_data['earn_step'] = 0
        context.user_data['earn_data'] = {}
        await query.edit_message_text("➕ افزودن روش کسب درآمد جدید\n\nلطفاً نام روش را وارد کنید:\n(این نام به کاربران نمایش داده می‌شود)")
        try:
            await context.bot.send_message(chat_id=user.id, text="لطفاً نام روش را وارد کنید:", reply_markup=get_panel_with_back_keyboard())
        except Exception:
            pass
        return

    # ==========================
    # سایر دکمه‌های عمومی و ادمین
    # (membership check, reports, add_balance, withdraw, admin add/remove/list, make_admin)
    # ==========================
    if data.startswith("send_msg_"):
        if not is_admin(user.id, context):
            return
        target_uid = data.replace("send_msg_", "")
        context.user_data['target_uid'] = target_uid
        context.user_data['admin_action'] = "send_to_user"
        try:
            await query.edit_message_text(f"📨 لطفاً متن پیام خود را برای کاربر با آیدی {target_uid} وارد کنید:")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text=f"📨 لطفاً متن پیام خود را برای کاربر با آیدی {target_uid} وارد کنید:", reply_markup=get_panel_with_back_keyboard())
        except Exception:
            pass
        return

    if data == "check_membership":
        is_member = await check_membership(update, context)
        if is_member:
            users = load_users(context)
            uid = str(user.id)
            if uid in users:
                users[uid]["is_member"] = True
                schedule_coro(save_users(context, users))
            try:
                await safe_delete_message(query.message)
            except Exception:
                pass
            if is_admin(user.id, context):
                await context.bot.send_message(chat_id=user.id, text="✅ عضویت شما تأیید شد!\n\n✨ از منوی زیر انتخاب کنید:", reply_markup=get_admin_main_keyboard())
            else:
                await context.bot.send_message(chat_id=user.id, text="✅ عضویت شما تأیید شد!\n\n✨ از منوی زیر انتخاب کنید:", reply_markup=get_main_keyboard())
        else:
            await query.answer("❌ شما هنوز عضو کانال نشده‌اید!", show_alert=True)
        return

    if data == "reports":
        users = load_users(context)
        uid = str(user.id)
        info = users.get(uid, {})
        profits = info.get("profits", 0)
        orders = info.get("orders", 0)
        subscribers = info.get("subscribers", 0)
        earnings_count = info.get("earnings", 0)
        deposits_count = info.get("deposits_count", 0)
        deposits_sum = info.get("deposits_sum", 0)
        withdrawals_count = info.get("withdrawals_count", 0)
        withdrawals_sum = info.get("withdrawals_sum", 0)
        await send_section_emoji(context, user.id, "reports")
        try:
            await query.edit_message_text(
                f"━━━━━━━━━━━━━━━\n💸 » مبالغ تسویه شده : <code>{profits:,}</code> تومان\n☎️ » تعداد خرید : <code>{orders}</code> عدد\n👥 » تعداد زیرمجموعه : <code>{subscribers}</code> نفر\n📤 » تعداد کسب درآمد : <code>{earnings_count}</code> عدد\n💳 » تعداد افزایش موجودی : <code>{deposits_count}</code> عدد\n💰 » جمع افزایش موجودی : <code>{deposits_sum:,}</code> تومان\n💸 » تعداد برداشت موجودی : <code>{withdrawals_count}</code> عدد\n📉 » جمع برداشت موجودی : <code>{withdrawals_sum:,}</code> تومان\n━━━━━━━━━━━━━━━",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="profile_back")]])
            )
        except Exception:
            pass
        return

    if data == "profile_back":
        users = load_users(context)
        uid = str(user.id)
        info = users.get(uid, {})
        text_msg, keyboard = build_profile_view(uid, info)
        try:
            await query.edit_message_text(text_msg, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            pass
        return

    if data == "add_balance":
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await send_section_emoji(context, user.id, "add_balance")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("20,000 تومان", callback_data="deposit_amt_20000"),
             InlineKeyboardButton("30,000 تومان", callback_data="deposit_amt_30000"),
             InlineKeyboardButton("40,000 تومان", callback_data="deposit_amt_40000")],
            [InlineKeyboardButton("50,000 تومان", callback_data="deposit_amt_50000"),
             InlineKeyboardButton("60,000 تومان", callback_data="deposit_amt_60000"),
             InlineKeyboardButton("💬 مبلغ دلخواه", callback_data="deposit_amt_custom")]
        ])
        await context.bot.send_message(chat_id=user.id, text="💳 افزایش موجودی\n\nلطفاً مبلغ واریز مد نظر خود را انتخاب کنید:", reply_markup=keyboard)
        return

    if data.startswith("deposit_amt_"):
        payload = data.replace("deposit_amt_", "")
        if payload == "custom":
            await query.edit_message_text("💳 افزایش موجودی\n\nلطفاً مبلغ واریز مد نظر خود را به تومان تایپ و ارسال کنید:\n\nمثال: 75000", reply_markup=None)
            context.user_data['pending_deposit'] = True
            return
        try:
            amount = int(payload)
        except ValueError:
            return
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        context.user_data['pending_amount'] = amount
        await context.bot.send_message(chat_id=user.id, text=f"💵 مبلغ واریز مد نظر: <code>{amount:,}</code> تومان\n\n💳 شماره کارت:\n<code>6666555544443333</code>\n(برای کپی کردن روی شماره کارت کلیک کنید)\n\n🏦 بانک: بلو بانک\n\n📤 پس از واریز، تصویر رسید را ارسال کنید.\n\n⚠️ توجه: پس از تایید توسط پشتیبانی، موجودی شما اضافه خواهد شد.", parse_mode="HTML", reply_markup=get_panel_with_back_keyboard())
        context.user_data['waiting_for_payment_image'] = True
        return

    if data == "withdraw":
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await send_section_emoji(context, user.id, "withdraw")
        users = load_users(context)
        uid = str(user.id)
        info = users.get(uid, {})
        withdrawable = info.get("withdrawable", 0)
        await context.bot.send_message(chat_id=user.id, text=f"💸 برداشت موجودی\n\n💳 موجودی قابل برداشت شما: <code>{withdrawable:,}</code> تومان\n\nلطفاً مبلغ مد نظرتون برای برداشت رو به تومان وارد کنید:\n\nمثال: 50,000", parse_mode="HTML", reply_markup=get_panel_with_back_keyboard())
        context.user_data['pending_withdraw'] = True
        return

    if data == "admin_back":
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text="👑 به پنل مدیریت بازگشتید:", reply_markup=get_admin_panel_keyboard())
        return

    if data == "admin_add_admin":
        if user.id != MAIN_ADMIN_ID:
            await query.answer("❌ فقط ادمین اصلی دسترسی دارد!", show_alert=True)
            return
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text="➕ افزودن ادمین جدید\n\nلطفاً آیدی عددی ادمین جدید را وارد کنید:\n\nمثال: 123456789", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "add_admin"
        return

    if data == "admin_remove_admin":
        if user.id != MAIN_ADMIN_ID:
            await query.answer("❌ فقط ادمین اصلی دسترسی دارد!", show_alert=True)
            return
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text="➖ حذف ادمین\n\nلطفاً آیدی عددی ادمین مورد نظر را وارد کنید:\n\nمثال: 123456789", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "remove_admin"
        return

    if data == "admin_list_admins":
        if user.id != MAIN_ADMIN_ID:
            await query.answer("❌ فقط ادمین اصلی دسترسی دارد!", show_alert=True)
            return
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        admins = load_admins(context)
        admin_list = ""
        for admin_id in admins.get("admins", []):
            admin_list += f"• {admin_id}\n"
        await context.bot.send_message(chat_id=user.id, text=f"📋 لیست ادمین‌ها:\n\n{admin_list}", reply_markup=get_panel_with_back_keyboard())
        return

    if data.startswith("add_balance_"):
        target_uid = data.replace("add_balance_", "")
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text=f"💰 افزایش موجودی کاربر {target_uid}\n\nلطفاً مبلغ مورد نظر را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "add_balance"
        context.user_data['target_uid'] = target_uid
        return

    if data.startswith("remove_balance_"):
        target_uid = data.replace("remove_balance_", "")
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text=f"➖ کاهش موجودی کاربر {target_uid}\n\nلطفاً مبلغ مورد نظر را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "remove_balance"
        context.user_data['target_uid'] = target_uid
        return

    if data.startswith("add_withdrawable_"):
        target_uid = data.replace("add_withdrawable_", "")
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text=f"💳 افزایش قابل برداشت کاربر {target_uid}\n\nلطفاً مبلغ مورد نظر را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "add_withdrawable"
        context.user_data['target_uid'] = target_uid
        return

    if data.startswith("remove_withdrawable_"):
        target_uid = data.replace("remove_withdrawable_", "")
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text=f"➖ کاهش قابل برداشت کاربر {target_uid}\n\nلطفاً مبلغ مورد نظر را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "remove_withdrawable"
        context.user_data['target_uid'] = target_uid
        return

    if data.startswith("add_profit_"):
        target_uid = data.replace("add_profit_", "")
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text=f"📤 افزایش تسویه شده کاربر {target_uid}\n\nلطفاً مبلغ مورد نظر را به تومان وارد کنید:\n\nمثال: 50000", reply_markup=get_panel_with_back_keyboard())
        context.user_data['admin_action'] = "add_profit"
        context.user_data['target_uid'] = target_uid
        return

    if data.startswith("make_admin_"):
        target_uid = data.replace("make_admin_", "")
        if user.id != MAIN_ADMIN_ID:
            await query.answer("❌ فقط ادمین اصلی دسترسی دارد!", show_alert=True)
            return
        admins = load_admins(context)
        try:
            target_id = int(target_uid)
        except Exception:
            await query.answer("❌ آیدی نامعتبر است!", show_alert=True)
            return
        if target_id in admins["admins"]:
            await query.answer("❌ این کاربر قبلاً ادمین است!", show_alert=True)
            return
        admins["admins"].append(target_id)
        schedule_coro(save_admins(context, admins))
        try:
            await safe_delete_message(query.message)
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text=f"✅ کاربر با آیدی {target_uid} با موفقیت به لیست ادمین‌ها اضافه شد!", reply_markup=get_admin_panel_keyboard())
        return

    # support reply to user
    if data.startswith("support_reply_"):
        target_uid = data.replace("support_reply_", "")
        context.user_data['reply_to_user'] = int(target_uid)
        try:
            await query.edit_message_text(f"✉️ در حال پاسخ به کاربر با آیدی {target_uid}\n\nلطفاً متن پاسخ خود را وارد کنید:")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text=f"✉️ در حال پاسخ به کاربر با آیدی {target_uid}\n\nلطفاً متن پاسخ خود را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف", callback_data="support_cancel_reply")]]))
        except Exception:
            pass
        return

    if data == "support_cancel_reply":
        context.user_data['reply_to_user'] = None
        try:
            await query.edit_message_text("❌ پاسخ به کاربر لغو شد.")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user.id, text="به پنل ادمین بازگشتید:", reply_markup=get_admin_panel_keyboard())
        except Exception:
            pass
        return

    # ==========================
    # view user's Requests (from profile)
    # ==========================
    if data == "requests":
        await send_section_emoji(context, user.id, "requests")
        try:
            await query.edit_message_text("📂 درخواست‌های شما:\n\nیکی از دسته‌ها را انتخاب کنید:", reply_markup=get_requests_home_keyboard())
        except Exception:
            pass
        return

    if data.startswith("reqlist_"):
        payload = data.replace("reqlist_", "")
        category, page_str = payload.rsplit("_", 1)
        try:
            page = int(page_str)
        except ValueError:
            page = 0
        uid = str(user.id)
        items = get_user_requests_by_category(context, uid, category)
        if not items:
            await query.answer("📭 درخواستی در این دسته ندارید!", show_alert=True)
            return
        total_pages = max(1, (len(items) + REQUESTS_PAGE_SIZE - 1) // REQUESTS_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * REQUESTS_PAGE_SIZE
        page_items = items[start:start + REQUESTS_PAGE_SIZE]
        keyboard = []
        for rid, r in page_items:
            label = build_request_item_label(category, r)
            keyboard.append([InlineKeyboardButton(label, callback_data=f"reqitem_{category}_{rid}")])
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"reqlist_{category}_{page-1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"reqlist_{category}_{page+1}"))
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="requests")])
        title = REQUEST_CATEGORIES.get(category, category)
        try:
            await query.edit_message_text(f"{title}\n\n(صفحه {page+1} از {total_pages})", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            pass
        return

    if data.startswith("reqitem_"):
        payload = data.replace("reqitem_", "")
        if "_" not in payload:
            await query.answer("❌ خطا", show_alert=True)
            return
        category, rid = payload.split("_", 1)
        user_reqs = load_user_requests(context)
        req = user_reqs.get(rid)
        if not req:
            await query.answer("❌ درخواست پیدا نشد!", show_alert=True)
            return
        status = request_status_label(req.get('status', 'pending'))
        confirmed_at = req.get('confirmed_at') or req.get('updated_at') or "-"
        if req.get('status', 'pending') == 'pending':
            confirmed_at = "-"
        title = REQUEST_CATEGORIES.get(category, category)
        extra = ""
        if category == "order":
            extra = f"🛍 محصول: {req.get('product_name','')}\n"
        elif category == "earn":
            extra = f"💎 روش: {req.get('earn_name','')}\n"
        txt = (f"{title}\n\n"
               f"{extra}"
               f"💰 مبلغ: {req.get('amount', 0):,} تومان\n"
               f"🕐 ساعت ثبت درخواست: {req.get('date', '-')}\n"
               f"✅ ساعت تایید: {confirmed_at}\n"
               f"📌 وضعیت: {status}")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"reqlist_{category}_0")]])
        try:
            await query.edit_message_text(txt, reply_markup=keyboard)
        except Exception:
            pass
        return

    # ==========================
    # admin: send discount (callback)
    # ==========================
    if data.startswith("send_discount_"):
        if not is_admin(user.id, context):
            return
        discount_id = data.replace("send_discount_", "")
        discount_requests = load_discount_requests(context)
        req = discount_requests.get(discount_id)
        if not req:
            await query.answer("❌ درخواست پیدا نشد!", show_alert=True)
            return
        # set admin state to input code
        context.user_data['discount_target'] = discount_id
        context.user_data['admin_action'] = "send_discount_code"
        product_name = req.get('product_name', 'محصول')
        count = req.get('count', 1)
        if count > 1:
            prompt = f"🟢 آماده ارسال کد تخفیف\n\n👤 کاربر: {req.get('user_name','-')}\n🛍 محصول: {product_name}\n📦 تعداد: {count} عدد\n\nلطفاً {count} کد تخفیف را ارسال کنید، هرکدام در یک خط جداگانه (با اینتر از هم جدا شده):"
        else:
            prompt = f"🟢 آماده ارسال کد تخفیف\n\n👤 کاربر: {req.get('user_name','-')}\n🛍 محصول: {product_name}\n\nلطفاً فقط خود کد تخفیف را ارسال کنید:"
        await query.edit_message_text(prompt)
        return

    # ==========================
    # admin: view user requests (from list)
    # ==========================
    if data.startswith("admin_view_userreq_"):
        if not is_admin(user.id, context):
            return
        rid = data.replace("admin_view_userreq_", "")
        user_reqs = load_user_requests(context)
        req = user_reqs.get(rid)
        if not req:
            await query.answer("❌ یافت نشد!", show_alert=True)
            return
        await query.edit_message_text(json.dumps(req, ensure_ascii=False, indent=2), reply_markup=get_admin_panel_keyboard())
        return

    # fallback: ignore unknown callback
    return

# helper to create an Update-like object for submitting earn request when inline triggered immediate submission
async def update_from_query(query):
    """
    Create a fake update.message with chat/user to use in submit_earn_request.
    We'll create a small object with needed attributes.
    """
    class FakeMessage:
        def __init__(self, user):
            self.from_user = user
            self.chat = type("C", (), {"id": user.id})
            self.text = ""
        async def reply_text(self, *args, **kwargs):
            # send to user
            pass
    class FakeUpdate:
        def __init__(self, query):
            self.message = FakeMessage(query.from_user)
            self.effective_user = query.from_user
    return FakeUpdate(query)

# ==========================
# اجرای اصلی
# ==========================

if __name__ == "__main__":
    print("=" * 60)
    print("🤖 در حال راه‌اندازی ربات...")
    print(f"📡 متصل به ربات: {TOKEN[:10]}...")
    print(f"📡 کانال دیتابیس: {BACKUP_CHANNEL}")
    print("=" * 60)

    app = ApplicationBuilder().token(TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("testchannel", test_channel))
    app.add_handler(CommandHandler("testdb", test_db))

    # Message handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(buttons))

    print("✅ ربات با موفقیت راه‌اندازی شد!")
    print("👑 VIP Bot Started...")
    print("=" * 60)
    app.run_polling()