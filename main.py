import asyncio
import re
import sqlite3
import logging
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
import pytz  # برای مدیریت منطقه زمانی

from telethon import TelegramClient, events, Button
# ⭐️ ایمپورت‌های جدید برای ساخت دستی کیبورد (KeyboardButton اضافه شد)
from telethon.tl.types import User, ReplyKeyboardMarkup, KeyboardButtonRow, KeyboardButton
from telethon.errors.rpcerrorlist import UserIsBlockedError, ChatAdminRequiredError, FloodWaitError
from telethon.tl.functions.users import GetFullUserRequest

from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- ⚠️ بخش تنظیمات - این قسمت را حتما ویرایش کنید ---


# API ID و API HASH خود را از my.telegram.org دریافت کنید
API_ID = 216948  # YOUR_API_ID
API_HASH = "4fdd31884493fdc49128f91216879765"  # YOUR_API_HASH
BOT_TOKEN = '8480342183:AAGlyxfMw6nWrrqQilnlGaOPN2BxmOiospg'  # ❗️ توکن واقعی را جایگزین کنید
OWNER_ID = 162999305  # ❗️ شناسه عددی اکانت خود را جایگزین کنید
DB_NAME = 'bot_stats_persistent.sqlite'

# --- پایان بخش تنظیمات ---

logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
                    level=logging.INFO)
getcontext().prec = 18
TEHRAN_TZ = pytz.timezone('Asia/Tehran')

# --- ⭐️ مدیریت دیتابیس (به‌روز شده) ⭐️ ---

class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self.setup()

    def get_conn(self):
        """ایجاد یک کانکشن جدید به دیتابیس"""
        return sqlite3.connect(self.db_file)

    def setup(self):
        """ایجاد جداول مورد نیاز در دیتابیس در صورت عدم وجود"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            # جدول تنظیمات کلیدی (تتر، دستمزد)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)
            ''')
            # جدول ارزش تتر هر S
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS s_values (s_key TEXT PRIMARY KEY, usdt_value REAL)
            ''')
            # جدول ادمین‌های مجاز ربات
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)
            ''')
            # ⭐️ جدول گروه‌های فعال (جدید) ⭐️
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_groups (chat_id INTEGER PRIMARY KEY)
            ''')
            # جدول آمار کلی روزانه گروه
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_daily_stats (
                date TEXT,
                chat_id INTEGER,
                total_toman REAL,
                PRIMARY KEY(date, chat_id)
            )
            ''')
            # جدول آمار کلی روزانه ادمین
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_daily_stats (
                date TEXT,
                chat_id INTEGER,
                admin_id INTEGER,
                total_s_sum INTEGER,
                PRIMARY KEY(date, chat_id, admin_id)
            )
            ''')
            # جدول آمار تفکیکی S برای هر ادمین
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS s_key_daily_stats (
                date TEXT,
                chat_id INTEGER,
                admin_id INTEGER,
                s_key TEXT,
                s_key_count INTEGER,
                s_key_sum INTEGER,
                PRIMARY KEY(date, chat_id, admin_id, s_key)
            )
            ''')
            # افزودن مالک به عنوان ادمین پیش‌فرض
            cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
            conn.commit()
            logging.info("Database setup complete. All tables checked/created.")

    # --- توابع تنظیمات ---
    def set_setting(self, key, value):
        with self.get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    def get_setting(self, key, default=None):
        with self.get_conn() as conn:
            cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default

    # --- توابع ارزش S ---
    def set_s_value(self, s_key, usdt_value):
        with self.get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO s_values (s_key, usdt_value) VALUES (?, ?)", (s_key, usdt_value))
    def get_s_value(self, s_key):
        with self.get_conn() as conn:
            cursor = conn.execute("SELECT usdt_value FROM s_values WHERE s_key = ?", (s_key,))
            row = cursor.fetchone()
            return Decimal(str(row[0])) if row else None
    def get_all_s_values(self):
        with self.get_conn() as conn:
            cursor = conn.execute("SELECT s_key, usdt_value FROM s_values ORDER BY s_key")
            return cursor.fetchall()

    # --- توابع ادمین ---
    def add_admin(self, user_id):
        with self.get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
    def remove_admin(self, user_id):
        if user_id == OWNER_ID: return False
        with self.get_conn() as conn:
            conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            return conn.total_changes > 0
    def get_all_admin_ids(self):
        with self.get_conn() as conn:
            cursor = conn.execute("SELECT user_id FROM admins")
            return {row[0] for row in cursor.fetchall()}

    # --- ⭐️ توابع گروه‌های فعال (جدید) ⭐️ ---
    def add_active_group(self, chat_id):
        with self.get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO active_groups (chat_id) VALUES (?)", (chat_id,))
    def remove_active_group(self, chat_id):
        with self.get_conn() as conn:
            conn.execute("DELETE FROM active_groups WHERE chat_id = ?", (chat_id,))
            return conn.total_changes > 0
    def get_all_active_groups(self):
        with self.get_conn() as conn:
            cursor = conn.execute("SELECT chat_id FROM active_groups")
            return {row[0] for row in cursor.fetchall()}

    # --- توابع آمار (محاسبات) ---
    def get_today_date(self):
        return datetime.now(TEHRAN_TZ).strftime('%Y-%m-%d')
    
    def get_yesterday_date(self):
        # دیروز واقعی
        yesterday = datetime.now(TEHRAN_TZ) - timedelta(days=1)
        # yesterday = datetime.now(TEHRAN_TZ) # ❗️ برای تست
        return yesterday.strftime('%Y-%m-%d')

    def update_group_stat(self, date, chat_id, toman_to_add):
        """افزودن یا کاستن مبلغ تومانی گروه. toman_to_add می‌تواند منفی باشد."""
        with self.get_conn() as conn:
            conn.execute('''
            INSERT INTO group_daily_stats (date, chat_id, total_toman)
            VALUES (?, ?, ?)
            ON CONFLICT(date, chat_id) DO UPDATE SET
            total_toman = total_toman + excluded.total_toman
            ''', (date, chat_id, float(toman_to_add)))

    def update_admin_stat(self, date, chat_id, admin_id, s_sum_to_add):
        """افزودن یا کاستن مجموع S ادمین. s_sum_to_add می‌تواند منفی باشد."""
        with self.get_conn() as conn:
            conn.execute('''
            INSERT INTO admin_daily_stats (date, chat_id, admin_id, total_s_sum)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date, chat_id, admin_id) DO UPDATE SET
            total_s_sum = total_s_sum + excluded.total_s_sum
            ''', (date, chat_id, admin_id, s_sum_to_add))

    def update_s_key_stat(self, date, chat_id, admin_id, s_key, s_number):
        """ذخیره آمار تفکیکی برای هر s_key (افزایشی)"""
        with self.get_conn() as conn:
            conn.execute('''
            INSERT INTO s_key_daily_stats (date, chat_id, admin_id, s_key, s_key_count, s_key_sum)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(date, chat_id, admin_id, s_key) DO UPDATE SET
            s_key_count = s_key_count + 1,
            s_key_sum = s_key_sum + excluded.s_key_sum
            ''', (date, chat_id, admin_id, s_key, s_number))

    # --- ⭐️ توابع جدید برای F (کاهشی) ⭐️ ---
    def reduce_s_key_stat(self, date, chat_id, admin_id, s_key, s_number_to_reduce):
        """
        تلاش برای کاهش آمار تفکیکی برای f<number>.
        اگر موفق (تعداد > 0 بود) True برمی‌گرداند.
        """
        with self.get_conn() as conn:
            cursor = conn.cursor()
            # ابتدا بررسی می‌کنیم که آیا رکوردی با تعداد > 0 وجود دارد یا خیر
            cursor.execute(
                "SELECT s_key_count FROM s_key_daily_stats "
                "WHERE date = ? AND chat_id = ? AND admin_id = ? AND s_key = ?",
                (date, chat_id, admin_id, s_key)
            )
            row = cursor.fetchone()
            
            # اگر رکوردی وجود نداشت یا تعداد آن صفر یا کمتر بود، False برگردان
            if not row or row[0] <= 0:
                return False 
            
            # اگر وجود داشت، یکی از تعداد و مقدار متناظر را از مجموع کم کن
            conn.execute('''
            UPDATE s_key_daily_stats SET
            s_key_count = s_key_count - 1,
            s_key_sum = s_key_sum - ?
            WHERE date = ? AND chat_id = ? AND admin_id = ? AND s_key = ?
            ''', (s_number_to_reduce, date, chat_id, admin_id, s_key))
            return True # عملیات موفقیت‌آمیز بود

    def get_admin_available_s_keys(self, date, chat_id, admin_id):
        """دریافت لیست S های موجود ادمین (که تعدادشان در آمار تفکیکی > 0 است)"""
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT s_key FROM s_key_daily_stats "
                "WHERE date = ? AND chat_id = ? AND admin_id = ? AND s_key_count > 0 "
                "ORDER BY s_key",
                (date, chat_id, admin_id)
            )
            return [row[0] for row in cursor.fetchall()]

    # --- توابع گزارش‌گیری (بدون تغییر) ---
    def get_group_stat(self, date, chat_id):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT total_toman FROM group_daily_stats WHERE date = ? AND chat_id = ?",
                (date, chat_id)
            )
            row = cursor.fetchone()
            return Decimal(str(row[0])) if row else Decimal('0')

    def get_admin_stats_for_group(self, date, chat_id):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT admin_id, total_s_sum FROM admin_daily_stats WHERE date = ? AND chat_id = ?",
                (date, chat_id)
            )
            return cursor.fetchall()

    def get_active_groups_for_report(self, date):
        """دریافت لیست گروه‌هایی که *دیروز* فعالیت داشته‌اند (برای گزارش)"""
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT DISTINCT chat_id FROM group_daily_stats WHERE date = ?", (date,)
            )
            return [row[0] for row in cursor.fetchall()]

    def get_group_s_key_breakdown(self, date, chat_id):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT s_key, SUM(s_key_count), SUM(s_key_sum) FROM s_key_daily_stats "
                "WHERE date = ? AND chat_id = ? GROUP BY s_key ORDER BY s_key",
                (date, chat_id)
            )
            return cursor.fetchall() 

    def get_all_admin_salary_stats(self, date):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT admin_id, SUM(total_s_sum) FROM admin_daily_stats "
                "WHERE date = ? GROUP BY admin_id", (date,)
            )
            return cursor.fetchall() 

    def get_all_group_income_stats(self, date):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT chat_id, total_toman FROM group_daily_stats WHERE date = ?", (date,)
            )
            return cursor.fetchall()

# --- متغیرهای گلوبال و نمونه‌سازی ---

client = TelegramClient('bot_session_name', API_ID, API_HASH)
db = Database(DB_NAME)

# ⭐️ کش‌های مموری (جدید) ⭐️
bot_admins_cache = set()
active_groups_cache = set() # کش گروه‌های فعال

# ⭐️ الگوهای Regex (جدید) ⭐️
S_PATTERN = re.compile(r'^[sS](\d+)$')
F_PATTERN = re.compile(r'^[fF](\d+)$') # الگوی جدید برای F

scheduler = AsyncIOScheduler() 

# --- توابع کمکی ---

async def get_user_from_event(event):
    if event.reply_to_msg_id:
        try:
            reply_msg = await event.get_reply_message()
            return await client.get_entity(reply_msg.sender_id)
        except Exception: return None
    args = event.text.split(maxsplit=1)
    if len(args) < 2: return None
    return await get_user_by_id_or_username(args[1])

async def get_user_by_id_or_username(target):
    try:
        if target.startswith('@'): return await client.get_entity(target)
        elif target.isdigit(): return await client.get_entity(int(target))
    except Exception: return None
    return None

async def load_initial_data():
    """ ⭐️ بارگذاری ادمین‌ها و گروه‌های فعال در کش (به‌روز شده) ⭐️"""
    global bot_admins_cache, active_groups_cache
    bot_admins_cache = db.get_all_admin_ids()
    active_groups_cache = db.get_all_active_groups()
    logging.info(f"Loaded {len(bot_admins_cache)} admins from DB.")
    logging.info(f"Loaded {len(active_groups_cache)} active groups from DB.")

# تابع ارسال پیام طولانی (بدون تغییر)
async def send_long_message(peer, text, max_len=4000):
    try:
        if len(text) <= max_len:
            await client.send_message(peer, text, parse_mode='md')
            return
        parts = []
        current_part = ""
        for line in text.splitlines():
            if len(current_part) + len(line) + 1 > max_len:
                parts.append(current_part)
                current_part = line
            else:
                if current_part:
                    current_part += "\n" + line
                else:
                    current_part = line
        parts.append(current_part.strip())
        for part in parts:
            if part:
                await client.send_message(peer, part, parse_mode='md')
                await asyncio.sleep(0.5) 
    except FloodWaitError as e:
        logging.warning(f"Flood wait error: {e.seconds}s. Sleeping.")
        await asyncio.sleep(e.seconds + 1)
        await send_long_message(peer, text) 
    except Exception as e:
        logging.error(f"Failed to send long message to {peer}: {e}")

# --- ⭐️ کنترل‌کننده‌های رویداد (Handlers) (به‌روز شده) ⭐️ ---

def build_main_keyboard_menu():
    """ساخت دکمه‌های کیبورد پنل اصلی مالک"""
    # ⭐️ استفاده مستقیم از KeyboardButton به جای Button.text
    return [
        [KeyboardButton("💵 تنظیم نرخ تتر"), KeyboardButton("💰 تنظیم دستمزد S")],
        [KeyboardButton("📊 مدیریت ارزش S"), KeyboardButton("👮‍♂️ مدیریت ادمین‌ها")],
        [KeyboardButton("📋 نمایش وضعیت")],
        [KeyboardButton("✖️ بستن کیبورد ✖️")]
    ]

@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if event.sender_id == OWNER_ID:
        if event.is_private:
            # ⭐️ نمایش کیبورد در پی‌وی مالک (ساخت دستی) ⭐️
            
            # ۱. دریافت لایوت دکمه‌ها
            button_layout = build_main_keyboard_menu()
            
            # ۲. تبدیل لایوت به لیست ردیف‌ها (KeyboardButtonRow)
            keyboard_rows = []
            for row_buttons in button_layout:
                # row_buttons لیستی از اشیاء KeyboardButton است
                keyboard_rows.append(KeyboardButtonRow(buttons=row_buttons))
                
            # ۳. ساخت Markup نهایی با resize=True
            manual_markup = ReplyKeyboardMarkup(rows=keyboard_rows, resize=True)

            await event.reply(
                "سلام مالک! 👋\nبه پنل مدیریت ربات آمار S خوش آمدید.\n\n"
                "از دکمه‌های زیر برای مدیریت استفاده کنید:",
                buttons=manual_markup  # ⭐️ استفاده از مارک‌آپ دستی
            )
        else:
            # نمایش دکمه شیشه‌ای در گروه
            await event.reply(
                "سلام مالک! 👋\nبرای دسترسی به پنل، از /panel در همینجا یا از کیبورد در پی‌وی استفاده کنید:",
                buttons=Button.inline("👑 باز کردن پنل مدیریت", b"panel_main")
            )
    else:
        await event.reply(
            "سلام! 👋\nمن ربات مدیریت آمار S هستم.\n"
            "برای مشاهده لیست دستورات، /help را ارسال کنید."
        )

@client.on(events.NewMessage(pattern='/help'))
async def help_handler(event):
    sender_id = event.sender_id
    help_text = "راهنمای ربات آمار S 📈\n\n"
    
    if sender_id == OWNER_ID:
        help_text += (
            "**دستورات مالک (شما):**\n\n"
            
            "**- راه‌اندازی گروه:**\n"
            "`/add_scoreandroid`\n"
            " (در گروه ارسال شود) فعال‌سازی ربات در این گروه.\n\n"
            "`/remove_scoreandroid`\n"
            " (در گروه ارسال شود) غیرفعال‌سازی ربات در این گروه.\n\n"

            "**- مدیریت ربات (دستوری):**\n"
            "`/panel`\n"
            " باز کردن پنل مدیریت شیشه‌ای.\n\n"
            "`/status`\n"
            " نمایش وضعیت لحظه‌ای تنظیمات ربات.\n\n"
            "`/stats`\n"
            " نمایش آمار لحظه‌ای *امروز* گروه (در گروه فعال ارسال شود).\n\n"
            "`/addadmin <ID/@user/Reply>`\n"
            " افزودن ادمین به ربات.\n\n"
            "`/deladmin <ID/@user/Reply>`\n"
            " حذف ادمین از ربات.\n\n"
            "`/setvalue <s_key> <value>`\n"
            " مثال: `/setvalue s1 0.7`\n\n"
            "`/settether <price>`\n"
            " مثال: `/settether 50000`\n\n"
            "`/setsalary <amount>`\n"
            " مثال: `/setsalary 10000`\n\n"
            "`/listadmins`\n"
            " نمایش لیست ادمین‌های ربات.\n\n"
            
            "**- پنل کیبورد (در PV):**\n"
            "با ارسال /start در پی‌وی، پنل کیبوردی برای دسترسی سریع به تنظیمات باز می‌شود.\n"
        )
    else:
        help_text += "شما به دستورات مدیریتی دسترسی ندارید.\n"
        if sender_id in bot_admins_cache:
            help_text += "شما ادمین ربات هستید و پیام‌های `s<عدد>` و `f<عدد>` شما در گروه‌های فعال محاسبه می‌شود."
        else:
            help_text += "شما کاربر عادی هستید."
            
    await event.respond(help_text, parse_mode='md')

# --- ⭐️ دستورات فعال‌سازی گروه (جدید) ⭐️ ---

@client.on(events.NewMessage(pattern=r'/add_scoreandroid', from_users=OWNER_ID))
async def add_group_handler(event):
    if not event.is_group:
        await event.reply("❌ این دستور فقط باید در گروه استفاده شود.")
        return
    
    chat_id = event.chat_id
    try:
        db.add_active_group(chat_id)
        active_groups_cache.add(chat_id)
        chat_title = (await event.get_chat()).title
        await event.reply(f"✅ ربات با موفقیت در گروه '{chat_title}' (ID: `{chat_id}`) فعال شد.")
        logging.info(f"Bot activated in group: {chat_title} ({chat_id})")
    except Exception as e:
        await event.reply(f"❌ خطا در فعال‌سازی گروه: {e}")

@client.on(events.NewMessage(pattern=r'/remove_scoreandroid', from_users=OWNER_ID))
async def remove_group_handler(event):
    if not event.is_group:
        await event.reply("❌ این دستور فقط باید در گروه استفاده شود.")
        return
        
    chat_id = event.chat_id
    try:
        if db.remove_active_group(chat_id):
            active_groups_cache.discard(chat_id)
            chat_title = (await event.get_chat()).title
            await event.reply(f"✅ ربات با موفقیت در گروه '{chat_title}' (ID: `{chat_id}`) غیرفعال شد.")
            logging.info(f"Bot deactivated in group: {chat_title} ({chat_id})")
        else:
            await event.reply("ℹ️ ربات از قبل در این گروه فعال نبود.")
    except Exception as e:
        await event.reply(f"❌ خطا در غیرفعال‌سازی گروه: {e}")


# --- ⭐️ پنل کیبوردی مالک (اصلاح‌شده) ⭐️ ---
# (اینها معادل دکمه‌های شیشه‌ای برای پنل کیبوردی هستند)

# ❗️❗️ اصلاحیه: `private=True` حذف شد و چک `event.is_private` اضافه شد ❗️❗️
@client.on(events.NewMessage(pattern='^💵 تنظیم نرخ تتر$', from_users=OWNER_ID))
async def text_set_tether(event):
    """(کیبورد) شروع مکالمه تنظیم تتر"""
    if not event.is_private: return # ⭐️ چک کردن پی‌وی در داخل تابع
    # این تابع از start_conversation_helper استفاده می‌کند که برای هر دو نوع رویداد (Message و Callback) طراحی شده
    await start_conversation_helper(
        event,
        key_name='tether_price',
        prompt_message="لطفاً نرخ جدید تتر به تومان را وارد کنید:",
        success_message_template="✅ نرخ تتر با موفقیت روی {value} تومان تنظیم شد."
    )

# ❗️❗️ اصلاحیه: `private=True` حذف شد و چک `event.is_private` اضافه شد ❗️❗️
@client.on(events.NewMessage(pattern='^💰 تنظیم دستمزد S$', from_users=OWNER_ID))
async def text_set_salary(event):
    """(کیبورد) شروع مکالمه تنظیم دستمزد"""
    if not event.is_private: return # ⭐️ چک کردن پی‌وی در داخل تابع
    await start_conversation_helper(
        event,
        key_name='salary_rate',
        prompt_message="لطفاً دستمزد به ازای هر واحد S (به تومان) را وارد کنید:",
        success_message_template="✅ دستمزد هر واحد S با موفقیت روی {value} تومان تنظیم شد."
    )

# ❗️❗️ اصلاحیه: `private=True` حذف شد و چک `event.is_private` اضافه شد ❗️❗️
@client.on(events.NewMessage(pattern='^📊 مدیریت ارزش S$', from_users=OWNER_ID))
async def text_s_value_panel(event):
    """(کیبورد) نمایش پنل شیشه‌ای مدیریت S"""
    if not event.is_private: return # ⭐️ چک کردن پی‌وی در داخل تابع
    buttons = [
        [Button.inline("➕ تنظیم/تغییر ارزش", b"panel_s_value_set")],
        [Button.inline("📋 لیست ارزش‌ها", b"panel_s_value_list")],
    ]
    await event.reply("📊 **مدیریت ارزش S**\n\n(از دکمه‌های شیشه‌ای زیر استفاده کنید):", buttons=buttons)

# ❗️❗️ اصلاحیه: `private=True` حذف شد و چک `event.is_private` اضافه شد ❗️❗️
@client.on(events.NewMessage(pattern='^👮‍♂️ مدیریت ادمین‌ها$', from_users=OWNER_ID))
async def text_admin_panel(event):
    """(کیبورد) نمایش پنل شیشه‌ای مدیریت ادمین"""
    if not event.is_private: return # ⭐️ چک کردن پی‌وی در داخل تابع
    buttons = [
        [Button.inline("➕ افزودن ادمین", b"panel_add_admin")],
        [Button.inline("➖ حذف ادمین", b"panel_del_admin")],
        [Button.inline("📋 لیست ادمین‌ها", b"panel_list_admins")],
    ]
    await event.reply("👮‍♂️ **مدیریت ادمین‌ها**\n\n(از دکمه‌های شیشه‌ای زیر استفاده کنید):", buttons=buttons)

# ❗️❗️ اصلاحیه: `private=True` حذف شد و چک `event.is_private` اضافه شد ❗️❗️
@client.on(events.NewMessage(pattern='^📋 نمایش وضعیت$', from_users=OWNER_ID))
async def text_status_handler(event):
    """(کیبورد) اجرای دستور status"""
    if not event.is_private: return # ⭐️ چک کردن پی‌وی در داخل تابع
    # برای جلوگیری از تکرار کد، به سادگی تابع هندلر /status را فراخوانی می‌کنیم
    await status_handler(event)

# ❗️❗️ اصلاحیه: `private=True` حذف شد و چک `event.is_private` اضافه شد ❗️❗️
@client.on(events.NewMessage(pattern='^✖️ بستن کیبورد ✖️$', from_users=OWNER_ID))
async def text_close_keyboard(event):
    """(کیبورد) بستن کیبورد"""
    if not event.is_private: return # ⭐️ چک کردن پی‌وی در داخل تابع
    await event.reply("کیبورد بسته شد.", buttons=Button.clear())


# --- پنل مدیریت شیشه‌ای (بدون تغییر) ---
# (این پنل همچنان برای /panel و برای زیرمنوهای پنل کیبوردی کار می‌کند)

def build_main_panel_menu():
    """ساخت دکمه‌های پنل اصلی شیشه‌ای"""
    return [
        [Button.inline("💵 تنظیم نرخ تتر", b"panel_tether"), Button.inline("💰 تنظیم دستمزد S", b"panel_salary")],
        [Button.inline("📊 مدیریت ارزش S", b"panel_s_values"), Button.inline("👮‍♂️ مدیریت ادمین‌ها", b"panel_admins")],
        [Button.inline(" بستن پنل ✖️", b"panel_close")]
    ]

@client.on(events.NewMessage(pattern='/panel', from_users=OWNER_ID))
async def owner_panel_handler(event):
    """نمایش پنل اصلی مدیریت شیشه‌ای"""
    await event.reply(
        "**👑 پنل مدیریت مالک**\n\n"
        "لطفاً عملیات مورد نظر خود را انتخاب کنید:",
        buttons=build_main_panel_menu()
    )

@client.on(events.CallbackQuery(data=b'panel_main'))
async def main_panel_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    try:
        await event.edit(
            "**👑 پنل مدیریت مالک**\n\n"
            "لطفاً عملیات مورد نظر خود را انتخاب کنید:",
            buttons=build_main_panel_menu()
        )
    except Exception:
        await event.answer("پنل باز است.")


@client.on(events.CallbackQuery(data=b'panel_close'))
async def close_panel_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await event.delete()

# --- جریان مکالمه (Conversation) برای تنظیمات (به‌روز شده) ---

async def start_conversation_helper(event, key_name, prompt_message, success_message_template, validation_regex=r'([\d\.]+)'):
    """
    تابع کمکی برای شروع مکالمه.
    اکنون هم event از نوع NewMessage (برای کیبورد) و هم CallbackQuery (برای دکمه شیشه‌ای) را می‌پذیرد.
    """
    try:
        # اگر دکمه شیشه‌ای بود، ابتدا answer() می‌دهیم
        if isinstance(event, events.CallbackQuery.Event):
            await event.answer("منتظر ورودی...")
        
        chat_id = event.chat_id
        async with client.conversation(chat_id, timeout=120) as conv:
            await conv.send_message(f"💬 {prompt_message}\n\n(برای لغو /cancel را ارسال کنید.)")
            
            try:
                response = await conv.get_response()
                if response.text == '/cancel':
                    await conv.send_message("عملیات لغو شد.")
                    if isinstance(event, events.CallbackQuery.Event): await event.answer()
                    return

                match = re.match(validation_regex, response.text)
                if not match:
                    await conv.send_message("❌ ورودی نامعتبر است. عملیات لغو شد.")
                    if isinstance(event, events.CallbackQuery.Event): await event.answer()
                    return

                value = Decimal(match.group(1))
                if value <= 0:
                    raise ValueError("Value must be positive")
                
                db.set_setting(key_name, str(value))
                
                # فرمت‌دهی زیبا برای نمایش
                try:
                    formatted_value = f"{value:,.0f}"
                except Exception:
                    formatted_value = str(value)

                await conv.send_message(success_message_template.format(value=formatted_value))
                if isinstance(event, events.CallbackQuery.Event): await event.answer(f"✅ با موفقیت ثبت شد.")

            except asyncio.TimeoutError:
                await conv.send_message("زمان شما تمام شد. عملیات لغو شد.")
                if isinstance(event, events.CallbackQuery.Event): await event.answer("زمان تمام شد")
            except (ValueError, Exception) as e:
                await conv.send_message(f"❌ خطا در پردازش ورودی: {e}. عملیات لغو شد.")
                if isinstance(event, events.CallbackQuery.Event): await event.answer("خطا")

    except Exception as e:
        logging.error(f"Error starting conversation: {e}")
        if isinstance(event, events.CallbackQuery.Event): await event.answer(f"خطا در شروع مکالمه: {e}")
        else: await event.reply(f"خطا در شروع مکالمه: {e}")


@client.on(events.CallbackQuery(data=b'panel_tether'))
async def set_tether_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await start_conversation_helper(
        event,
        key_name='tether_price',
        prompt_message="لطفاً نرخ جدید تتر به تومان را وارد کنید:",
        success_message_template="✅ نرخ تتر با موفقیت روی {value} تومان تنظیم شد."
    )

@client.on(events.CallbackQuery(data=b'panel_salary'))
async def set_salary_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await start_conversation_helper(
        event,
        key_name='salary_rate',
        prompt_message="لطفاً دستمزد به ازای هر واحد S (به تومان) را وارد کنید:",
        success_message_template="✅ دستمزد هر واحد S با موفقیت روی {value} تومان تنظیم شد."
    )

# --- پنل مدیریت ادمین‌ها (بدون تغییر) ---
# (اینها توسط پنل کیبوردی و /panel فراخوانی می‌شوند)
@client.on(events.CallbackQuery(data=b'panel_admins'))
async def admin_panel_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    buttons = [
        [Button.inline("➕ افزودن ادمین", b"panel_add_admin")],
        [Button.inline("➖ حذف ادمین", b"panel_del_admin")],
        [Button.inline("📋 لیست ادمین‌ها", b"panel_list_admins")],
        [Button.inline(" بازگشت 🔙", b"panel_main")]
    ]
    await event.edit("👮‍♂️ **مدیریت ادمین‌ها**", buttons=buttons)

@client.on(events.CallbackQuery(data=b'panel_add_admin'))
async def add_admin_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await event.answer("منتظر ورودی...")
    try:
        async with client.conversation(event.chat_id, timeout=120) as conv:
            await conv.send_message(
                "💬 لطفاً شناسه عددی (ID) یا یوزرنیم (@username) کاربر مورد نظر را ارسال کنید.\n"
                "(برای افزودن با ریپلای، همچنان می‌توانید از دستور `/addadmin` استفاده کنید.)\n\n"
                "(برای لغو /cancel را ارسال کنید.)"
            )
            try:
                response = await conv.get_response()
                if response.text == '/cancel':
                    await conv.send_message("عملیات لغو شد.")
                    return
                user = await get_user_by_id_or_username(response.text.strip())
                if not user:
                    await conv.send_message("❌ کاربر یافت نشد. عملیات لغو شد.")
                    return
                db.add_admin(user.id)
                bot_admins_cache.add(user.id)
                await conv.send_message(f"✅ کاربر {user.first_name} (ID: `{user.id}`) با موفقیت به ادمین‌ها اضافه شد.")
                await event.answer("✅ اضافه شد")
            except asyncio.TimeoutError:
                await conv.send_message("زمان شما تمام شد. عملیات لغو شد.")
            except Exception as e:
                await conv.send_message(f"❌ خطا: {e}. عملیات لغو شد.")
    except Exception as e:
        await event.answer(f"خطا: {e}")

@client.on(events.CallbackQuery(data=b'panel_del_admin'))
async def del_admin_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await event.answer("منتظر ورودی...")
    try:
        async with client.conversation(event.chat_id, timeout=120) as conv:
            await conv.send_message(
                "💬 لطفاً شناسه عددی (ID) یا یوزرنیم (@username) کاربری که می‌خواهید حذف کنید را ارسال کنید.\n"
                "(برای حذف با ریپلای، از دستور `/deladmin` استفاده کنید.)\n\n"
                "(برای لغو /cancel را ارسال کنید.)"
            )
            try:
                response = await conv.get_response()
                if response.text == '/cancel':
                    await conv.send_message("عملیات لغو شد.")
                    return
                user = await get_user_by_id_or_username(response.text.strip())
                if not user:
                    await conv.send_message("❌ کاربر یافت نشد. عملیات لغو شد.")
                    return
                if user.id == OWNER_ID:
                    await conv.send_message("❌ شما نمی‌توانید مالک را حذف کنید.")
                    return
                if db.remove_admin(user.id):
                    bot_admins_cache.discard(user.id)
                    await conv.send_message(f"✅ کاربر {user.first_name} (ID: `{user.id}`) با موفقیت حذف شد.")
                    await event.answer("✅ حذف شد")
                else:
                    await conv.send_message("❌ این کاربر در لیست ادمین‌ها وجود نداشت.")
            except asyncio.TimeoutError:
                await conv.send_message("زمان شما تمام شد. عملیات لغو شد.")
            except Exception as e:
                await conv.send_message(f"❌ خطا: {e}. عملیات لغو شد.")
    except Exception as e:
        await event.answer(f"خطا: {e}")

@client.on(events.CallbackQuery(data=b'panel_list_admins'))
async def list_admins_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    if not bot_admins_cache:
        await event.answer("لیست ادمین‌ها خالی است (به جز شما).", alert=True)
        return
    msg = "**لیست ادمین‌های ربات:**\n\n"
    count = 0
    # به جای حلقه، از gather برای دریافت موازی اطلاعات کاربران استفاده می‌کنیم
    tasks = [client.get_entity(admin_id) for admin_id in bot_admins_cache]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    admin_list_lines = []
    for admin_id, result in zip(bot_admins_cache, results):
        if isinstance(result, User):
            name = result.first_name
            username = f"(@{result.username})" if result.username else ""
            if admin_id == OWNER_ID:
                name += " (👑 مالک)"
            admin_list_lines.append(f"- {name} {username} (ID: `{admin_id}`)")
            count += 1
        else:
            admin_list_lines.append(f"- (کاربر یافت نشد) (ID: `{admin_id}`)")
            
    msg += "\n".join(sorted(admin_list_lines)) # مرتب‌سازی بر اساس نام
    msg += f"\n\nتعداد کل: {count} نفر"
    await event.answer()
    await event.edit(msg, buttons=[Button.inline(" بازگشت 🔙", b"panel_admins")])


# --- پنل مدیریت ارزش S (بدون تغییر) ---
# (اینها توسط پنل کیبوردی و /panel فراخوانی می‌شوند)
@client.on(events.CallbackQuery(data=b'panel_s_values'))
async def s_value_panel_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    buttons = [
        [Button.inline("➕ تنظیم/تغییر ارزش", b"panel_s_value_set")],
        [Button.inline("📋 لیست ارزش‌ها", b"panel_s_value_list")],
        [Button.inline(" بازگشت 🔙", b"panel_main")]
    ]
    await event.edit("📊 **مدیریت ارزش S**", buttons=buttons)

@client.on(events.CallbackQuery(data=b'panel_s_value_set'))
async def set_s_value_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await event.answer("منتظر ورودی...")
    try:
        async with client.conversation(event.chat_id, timeout=120) as conv:
            await conv.send_message(
                "💬 لطفاً کلید و ارزش تتر آن را با یک فاصله وارد کنید.\n"
                "مثال: `s1 0.7`\n"
                "مثال: `s5 1.2`\n\n"
                "(برای لغو /cancel را ارسال کنید.)"
            )
            try:
                response = await conv.get_response()
                if response.text == '/cancel':
                    await conv.send_message("عملیات لغو شد.")
                    return
                match = re.match(r'(\S+)\s+([\d\.]+)', response.text.strip())
                if not match:
                    await conv.send_message("❌ فرمت ورودی اشتباه است. مثال: `s1 0.7`")
                    return
                s_key = match.group(1).lower()
                value_str = match.group(2)
                if not s_key.startswith('s') or not s_key[1:].isdigit():
                    await conv.send_message("❌ خطا: فرمت کلید S صحیح نیست. مثال: `s1`, `s2`")
                    return
                value = Decimal(value_str)
                if value < 0: raise ValueError("Value must be non-negative")
                db.set_s_value(s_key, float(value))
                await conv.send_message(f"✅ ارزش {s_key} با موفقیت روی {value} تتر تنظیم شد.")
                await event.answer("✅ ثبت شد")
            except asyncio.TimeoutError:
                await conv.send_message("زمان شما تمام شد. عملیات لغو شد.")
            except Exception as e:
                await conv.send_message(f"❌ خطا: {e}. عملیات لغو شد.")
    except Exception as e:
        await event.answer(f"خطا: {e}")

@client.on(events.CallbackQuery(data=b'panel_s_value_list'))
async def list_s_values_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await event.answer()
    all_values = db.get_all_s_values()
    if not all_values:
        await event.edit(
            "هنوز هیچ ارزشی برای Sها تنظیم نشده است.",
            buttons=[Button.inline(" بازگشت 🔙", b"panel_s_values")]
        )
        return
    msg = "**📋 لیست ارزش‌های S (به تتر):**\n\n"
    for key, value in all_values:
        msg += f"`{key}` = `{value}` USDT\n"
    await event.edit(msg, buttons=[Button.inline(" بازگشت 🔙", b"panel_s_values")])


# --- دستورات متنی (همچنان فعال) ---
# (اینها برای استفاده سریع مالک مفید هستند)

@client.on(events.NewMessage(pattern=r'/addadmin(?: |$)(.*)', from_users=OWNER_ID))
async def add_admin_handler(event):
    user = await get_user_from_event(event)
    if not user:
        await event.reply("❌ خطا: کاربر یافت نشد. (از ID، @یوزرنیم یا ریپلای استفاده کنید)")
        return
    db.add_admin(user.id)
    bot_admins_cache.add(user.id)
    await event.reply(f"✅ کاربر {user.first_name} (ID: `{user.id}`) با موفقیت به ادمین‌ها اضافه شد.")

@client.on(events.NewMessage(pattern=r'/deladmin(?: |$)(.*)', from_users=OWNER_ID))
async def del_admin_handler(event):
    user = await get_user_from_event(event)
    if not user:
        await event.reply("❌ خطا: کاربر یافت نشد. (از ID، @یوزرنیم یا ریپلای استفاده کنید)")
        return
    if user.id == OWNER_ID:
        await event.reply("❌ خطا: شما نمی‌توانید مالک ربات را حذف کنید.")
        return
    if db.remove_admin(user.id):
        bot_admins_cache.discard(user.id)
        await event.reply(f"✅ کاربر {user.first_name} (ID: `{user.id}`) با موفقیت از ادمین‌ها حذف شد.")
    else:
        await event.reply("❌ خطا: این کاربر در لیست ادمین‌ها وجود نداشت.")

@client.on(events.NewMessage(pattern=r'/listadmins', from_users=OWNER_ID))
async def list_admins_handler(event):
    # این تابع از منطق list_admins_callback کپی شده است
    if not bot_admins_cache:
        await event.reply("لیست ادمین‌ها خالی است (به جز شما).")
        return
    msg = "**لیست ادمین‌های ربات:**\n\n"
    count = 0
    tasks = [client.get_entity(admin_id) for admin_id in bot_admins_cache]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    admin_list_lines = []
    for admin_id, result in zip(bot_admins_cache, results):
        if isinstance(result, User):
            name = result.first_name
            username = f"(@{result.username})" if result.username else ""
            if admin_id == OWNER_ID:
                name += " (👑 مالک)"
            admin_list_lines.append(f"- {name} {username} (ID: `{admin_id}`)")
            count += 1
        else:
            admin_list_lines.append(f"- (کاربر یافت نشد) (ID: `{admin_id}`)")
    msg += "\n".join(sorted(admin_list_lines))
    msg += f"\n\nتعداد کل: {count} نفر"
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r'/setvalue (\S+) ([\d\.]+)', from_users=OWNER_ID))
async def set_value_handler(event):
    s_key = event.pattern_match.group(1).lower()
    value_str = event.pattern_match.group(2)
    if not s_key.startswith('s') or not s_key[1:].isdigit():
        await event.reply("❌ خطا: فرمت کلید S صحیح نیست. مثال: `s1`, `s2`")
        return
    try:
        value = Decimal(value_str)
        if value < 0: raise ValueError()
        db.set_s_value(s_key, float(value))
        await event.reply(f"✅ ارزش {s_key} با موفقیت روی {value} تتر تنظیم شد.")
    except Exception:
        await event.reply("❌ خطا: مقدار وارد شده نامعتبر است.")

@client.on(events.NewMessage(pattern=r'/settether ([\d\.]+)', from_users=OWNER_ID))
async def set_tether_handler(event):
    price_str = event.pattern_match.group(1)
    try:
        price = Decimal(price_str)
        if price <= 0: raise ValueError()
        db.set_setting('tether_price', str(price))
        await event.reply(f"✅ نرخ تتر با موفقیت روی {price:,.0f} تومان تنظیم شد.")
    except Exception:
        await event.reply("❌ خطا: قیمت وارد شده نامعتبر است.")

@client.on(events.NewMessage(pattern=r'/setsalary ([\d\.]+)', from_users=OWNER_ID))
async def set_salary_handler(event):
    amount_str = event.pattern_match.group(1)
    try:
        amount = Decimal(amount_str)
        if amount <= 0: raise ValueError()
        db.set_setting('salary_rate', str(amount))
        await event.reply(f"✅ دستمزد هر واحد S با موفقیت روی {amount:,.0f} تومان تنظیم شد.")
    except Exception:
        await event.reply("❌ خطا: مبلغ وارد شده نامعتبر است.")

# --- آمار و پردازش S/F (به‌روز شده) ---
@client.on(events.NewMessage(pattern=r'/status', from_users=OWNER_ID))
async def status_handler(event):
    """نمایش وضعیت لحظه‌ای تنظیمات ربات"""
    try:
        tether_price_str = db.get_setting('tether_price', 'تنظیم نشده')
        salary_rate_str = db.get_setting('salary_rate', 'تنظیم نشده')
        
        tether_price = f"{Decimal(tether_price_str):,.0f} تومان" if tether_price_str != 'تنظیم نشده' else "تنظیم نشده"
        salary_rate = f"{Decimal(salary_rate_str):,.0f} تومان" if salary_rate_str != 'تنظیم نشده' else "تنظیم نشده"

        msg = "📊 **وضعیت لحظه‌ای تنظیمات ربات**\n\n"
        msg += f"💵 **نرخ تتر:** {tether_price}\n"
        msg += f"💰 **دستمزد هر S:** {salary_rate}\n\n"
        
        msg += "--- **ارزش‌های S (USDT)** ---\n"
        all_values = db.get_all_s_values()
        if not all_values:
            msg += "هیچ ارزشی تنظیم نشده است."
        else:
            for key, value in all_values:
                msg += f"`{key}` = `{value}` USDT\n"
                
        msg += "\n--- **گروه‌های فعال** ---\n"
        if not active_groups_cache:
            msg += "هیچ گروهی فعال نشده است.\n(از /add_scoreandroid در گروه استفاده کنید)"
        else:
            tasks = [client.get_entity(chat_id) for chat_id in active_groups_cache]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for chat_id, result in zip(active_groups_cache, results):
                if hasattr(result, 'title'):
                    msg += f"- {result.title} (`{chat_id}`)\n"
                else:
                    msg += f"- (گروه ناشناس) (`{chat_id}`)\n"

        
        await event.reply(msg, parse_mode='md')
    except Exception as e:
        await event.reply(f"❌ خطایی رخ داد: {e}")


@client.on(events.NewMessage(pattern=r'/stats', from_users=OWNER_ID))
async def stats_handler(event):
    """نمایش آمار لحظه‌ای *امروز* گروه"""
    if not event.is_group:
        await event.reply("❌ این دستور فقط باید در گروه استفاده شود.")
        return
        
    chat_id = event.chat_id
    if chat_id not in active_groups_cache:
        await event.reply("❌ ربات در این گروه فعال نیست. (از /add_scoreandroid استفاده کنید)")
        return

    today_date = db.get_today_date()
    
    salary_rate_str = db.get_setting('salary_rate', '0')
    salary_rate = Decimal(salary_rate_str)
    group_total = db.get_group_stat(today_date, chat_id)
    admin_stats = db.get_admin_stats_for_group(today_date, chat_id)

    if group_total == 0 and not admin_stats:
        await event.reply(f"📊 آمار امروز ({today_date}) برای این گروه هنوز خالی است.")
        return

    try:
        chat_entity = await event.get_chat()
        chat_title = chat_entity.title
    except Exception:
        chat_title = f"گروه (ID: {chat_id})"

    msg = f"📊 **آمار لحظه‌ای امروز ({today_date})**\n"
    msg += f"**گروه: {chat_title}**\n\n"
    # ⭐️ اصلاحیه: حذف اعشار
    msg += f"💰 **مجموع فروش گروه (تومان):** `{group_total:,.0f}`\n\n"
    msg += "--- 👨‍💻 آمار ادمین‌ها ---\n"
    msg += f"(نرخ دستمزد هر S: {salary_rate:,.0f} تومان)\n\n"

    if not admin_stats:
        msg += "فعالیتی از ادمین‌ها ثبت نشده است."
    else:
        admin_ids = [admin_id for admin_id, s_sum in admin_stats]
        tasks = [client.get_entity(admin_id) for admin_id in admin_ids]
        user_results = await asyncio.gather(*tasks, return_exceptions=True)
        user_map = {}
        for admin_id, result in zip(admin_ids, user_results):
            if isinstance(result, User): user_map[admin_id] = result.first_name
            else: user_map[admin_id] = f"(کاربر {admin_id})"
            
        admin_stats_sorted = sorted(admin_stats, key=lambda x: x[1], reverse=True) # مرتب‌سازی بر اساس مجموع S
        
        for admin_id, s_sum in admin_stats_sorted:
            name = user_map.get(admin_id, f"(کاربر {admin_id})")
            admin_salary = s_sum * salary_rate
            msg += f"👤 **{name}** (ID: `{admin_id}`)\n"
            msg += f"   - مجموع S: **{s_sum}**\n"
            msg += f"   - دستمزد (تومان): `{admin_salary:,.0f}`\n\n"
    await event.reply(msg)

# --- ⭐️ هندلر S (به‌روز شده با چک گروه فعال) ⭐️ ---
@client.on(events.NewMessage(pattern=S_PATTERN))
async def s_message_handler(event):
    """
    هندلر اصلی پیام‌های s<number> (اکنون گروه فعال را چک می‌کند)
    """
    sender_id = event.sender_id
    chat_id = event.chat_id
    
    # ⭐️ ۱. چک کردن گروه فعال
    if chat_id not in active_groups_cache: return
    # ۲. چک کردن ادمین
    if sender_id not in bot_admins_cache: return
    # ۳. چک کردن اینکه در گروه است (توسط چک ۱ پوشش داده می‌شود اما برای اطمینان)
    if not event.is_group: return

    match = event.pattern_match
    s_key = match.group(0).lower()
    try:
        s_number = int(match.group(1))
    except ValueError: return
    if s_number == 0: return

    usdt_value = db.get_s_value(s_key)
    tether_price_str = db.get_setting('tether_price')
    
    # اعتبارسنجی با ریپلای خطا
    if usdt_value is None:
        msg = f"⚠️ **خطا:** ارزش {s_key} تنظیم نشده است."
        if sender_id == OWNER_ID:
            await event.reply(msg + "\n(از /setvalue یا /panel استفاده کنید)")
        else:
            await event.reply(msg + "\n(لطفا به مالک اطلاع دهید)")
        return
    if tether_price_str is None:
        msg = "⚠️ **خطا:** نرخ تتر تنظیم نشده است."
        if sender_id == OWNER_ID:
            await event.reply(msg + "\n(از /settether یا /panel استفاده کنید)")
        else:
            await event.reply(msg + "\n(لطفا به مالک اطلاع دهید)")
        return
        
    tether_price = Decimal(tether_price_str)
    # محاسبه مبلغ تومانی برای افزودن به گروه
    group_add_tomans = (usdt_value * Decimal(s_number)) * tether_price
    # محاسبه S برای افزودن به ادمین (فقط عدد)
    admin_s_sum_to_add = s_number
    today_date = db.get_today_date()
    
    try:
        # ذخیره آمار کلی گروه
        db.update_group_stat(today_date, chat_id, group_add_tomans)
        # ذخیره آمار کلی ادمین
        db.update_admin_stat(today_date, chat_id, sender_id, admin_s_sum_to_add)
        # ذخیره آمار تفکیکی S
        db.update_s_key_stat(today_date, chat_id, sender_id, s_key, s_number)
        
    except Exception as e:
        logging.error(f"Failed to write stats to DB: {e}")
        await event.reply(f"❌ **خطای سیستمی:**\nهنگام ثبت در دیتابیس مشکلی پیش آمد.\n`{e}`")
        return

    # ریپلای تایید
    try:
        # ⭐️ اصلاحیه: حذف اعشار
        reply_msg = (
            f"✅ **ثبت شد** (`{s_key}`)\n"
            f"• مبلغ افزوده شده به گروه: `{group_add_tomans:,.0f}` تومان\n"
            f"• S افزوده شده به شما: `{admin_s_sum_to_add}`"
        )
        await event.reply(reply_msg)
    except Exception as e:
        logging.warning(f"Failed to send reply confirmation: {e}")


# --- ⭐️ هندلر F (جدید) ⭐️ ---
@client.on(events.NewMessage(pattern=F_PATTERN))
async def f_message_handler(event):
    """
    هندلر پیام‌های f<number> (کاهشی)
    """
    sender_id = event.sender_id
    chat_id = event.chat_id

    # ۱. چک کردن گروه فعال
    if chat_id not in active_groups_cache: return
    # ۲. چک کردن ادمین
    if sender_id not in bot_admins_cache: return
    # ۳. چک کردن اینکه در گروه است
    if not event.is_group: return

    match = event.pattern_match
    f_key = match.group(0).lower() # f1
    s_key = 's' + match.group(1) # s1
    try:
        s_number_to_reduce = int(match.group(1)) # 1
    except ValueError: return
    if s_number_to_reduce == 0: return

    # دریافت تنظیمات برای محاسبه معکوس
    usdt_value = db.get_s_value(s_key)
    tether_price_str = db.get_setting('tether_price')
    today_date = db.get_today_date()
    
    # اعتبارسنجی تنظیمات
    if usdt_value is None:
        # ⭐️ اصلاحیه: متن خطای روان‌تر
        await event.reply(f"⚠️ **خطا:** ارزش `{s_key}` (مورد نیاز برای `{f_key}`) هنوز تنظیم نشده است. عملیات لغو شد.")
        return
    if tether_price_str is None:
        await event.reply(f"⚠️ **خطا:** نرخ تتر تنظیم نشده است. عملیات لغو شد.")
        return

    try:
        # تلاش برای کاهش آمار تفکیکی
        success = db.reduce_s_key_stat(today_date, chat_id, sender_id, s_key, s_number_to_reduce)
        
        if success:
            # اگر کاهش موفق بود، آمار کلی را هم معکوس کن
            tether_price = Decimal(tether_price_str)
            toman_to_remove = (usdt_value * Decimal(s_number_to_reduce)) * tether_price
            
            # ارسال مقادیر منفی برای کاهش
            db.update_group_stat(today_date, chat_id, -toman_to_remove)
            db.update_admin_stat(today_date, chat_id, sender_id, -s_number_to_reduce)
            
            # ⭐️ اصلاحیه: حذف اعشار
            await event.reply(
                f"✅ **کسر شد** (`{f_key}`)\n"
                f"• یک مورد `{s_key}` از آمار شما کسر شد.\n"
                f"• مبلغ کسر شده از گروه: `{toman_to_remove:,.0f}` تومان\n"
                f"• S کسر شده از شما: `{s_number_to_reduce}`"
            )
        else:
            # اگر کاهشی انجام نشد (تعداد 0 بود)
            available_keys = db.get_admin_available_s_keys(today_date, chat_id, sender_id)
            if not available_keys:
                await event.reply(f"❌ **خطا:** شما هیچ S ثبت‌شده‌ای برای امروز در این گروه ندارید که بتوانید `{f_key}` را ثبت کنید.")
            else:
                keys_str = ", ".join(f"`{k}`" for k in available_keys)
                await event.reply(f"❌ **خطا:** شما آمار `{s_key}` برای کسر کردن ندارید.\n\nS های موجود شما: {keys_str}")
                
    except Exception as e:
        logging.error(f"Failed to reduce stats with F command: {e}")
        await event.reply(f"❌ **خطای سیستمی:**\nهنگام کسر آمار مشکلی پیش آمد.\n`{e}`")


# --- ⭐️ تابع گزارش‌دهی خودکار نیمه‌شب (به‌روز شده) ⭐️ ---

async def send_daily_reports():
    """
    در ساعت 00:01 اجرا می‌شود، آمار روز گذشته را جمع‌آوری و ارسال می‌کند.
    """
    yesterday_date = db.get_yesterday_date()
    logging.info(f"Running daily reports for date: {yesterday_date}")
    
    # دریافت تنظیمات نهایی روز گذشته
    tether_price_str = db.get_setting('tether_price', '0')
    salary_rate_str = db.get_setting('salary_rate', '0')
    tether_price = Decimal(tether_price_str)
    salary_rate = Decimal(salary_rate_str)

    # --- ۱. ارسال گزارش به گروه‌ها ---
    # فقط به گروه‌هایی گزارش بده که دیروز فعالیت داشتند
    active_groups_yesterday = db.get_active_groups_for_report(yesterday_date)
    logging.info(f"Found {len(active_groups_yesterday)} active groups for daily report.")
    
    group_stats_for_owner = []
    total_all_groups_income = Decimal('0')

    for chat_id in active_groups_yesterday:
        # فقط به گروه‌هایی که *هنوز* در لیست فعال هستند پیام بده
        if chat_id not in active_groups_cache:
            logging.info(f"Skipping report for {chat_id}, as it's no longer in active_groups_cache.")
            continue
            
        try:
            total_toman = db.get_group_stat(yesterday_date, chat_id)
            if total_toman == 0:
                continue

            total_all_groups_income += total_toman
            
            try:
                chat_entity = await client.get_entity(chat_id)
                chat_title = chat_entity.title
            except Exception as e:
                chat_title = f"گروه (ID: {chat_id})"
                logging.warning(f"Could not get chat title for {chat_id}: {e}")
            
            group_stats_for_owner.append((chat_title, chat_id, total_toman))
            
            # ساخت پیام گزارش گروه
            msg = f"📊 **خلاصه آمار روزانه** 📊\n"
            msg += f"🗓 **تاریخ:** `{yesterday_date}`\n"
            msg += f"🏠 **گروه:** {chat_title}\n\n"
            # ⭐️ اصلاحیه: حذف اعشار
            msg += f"💰 **جمع کل فروش (تومان):** `{total_toman:,.0f}`\n\n"
            msg += "--- **جزئیات بازه‌ها (S)** ---\n"
            
            breakdown = db.get_group_s_key_breakdown(yesterday_date, chat_id)
            if not breakdown:
                msg += "آماری ثبت نشده است.\n"
            else:
                for s_key, s_count, s_sum in breakdown:
                    # نمایش آمار فقط در صورتی که تعداد یا مجموع > 0 باشد (پس از F زدن)
                    if s_count > 0 or s_sum > 0:
                        msg += f"• `{s_key}`: **{s_count}** عدد (مجموع S: **{s_sum}**)\n"
            
            msg += "\n--- **تنظیمات محاسبه (نهایی)** ---\n"
            msg += f"💵 **نرخ تتر:** `{tether_price:,.0f}` تومان\n"
            
            await send_long_message(chat_id, msg)
            await asyncio.sleep(1) 
            
        except ChatAdminRequiredError:
            logging.warning(f"Failed to send report to group {chat_id}: Bot is not admin.")
            # اگر ربات ادمین نیست، گروه را غیرفعال کن
            db.remove_active_group(chat_id)
            active_groups_cache.discard(chat_id)
            await send_long_message(OWNER_ID, f"⚠️ **خطا در ارسال گزارش**\nربات در گروه `{chat_id}` ادمین نیست و دسترسی ارسال پیام ندارد. گروه به صورت خودکار غیرفعال شد.")
        except UserIsBlockedError:
             logging.warning(f"Bot is blocked in group {chat_id}. Deactivating.")
             db.remove_active_group(chat_id)
             active_groups_cache.discard(chat_id)
        except Exception as e:
            logging.error(f"Failed to send report to group {chat_id}: {e}")
            await send_long_message(OWNER_ID, f"⚠️ **خطا در ارسال گزارش به گروه {chat_id}**\n`{e}`")


    # --- ۲. ارسال گزارش به پی‌وی مالک ---
    
    # گزارش الف: آمار ادمین‌ها
    admin_stats_msg = f"🔔 **گزارش دستمزد ادمین‌ها**\n"
    admin_stats_msg += f"🗓 **تاریخ:** `{yesterday_date}`\n"
    admin_stats_msg += f"💰 **نرخ دستمزد هر S:** `{salary_rate:,.0f}` تومان\n\n"
    
    all_admin_stats = db.get_all_admin_salary_stats(yesterday_date)
    total_all_salary = Decimal('0')
    
    if not all_admin_stats:
        admin_stats_msg += "هیچ فعالیتی از ادمین‌ها ثبت نشده است."
    else:
        admin_ids = [stat[0] for stat in all_admin_stats]
        tasks = [client.get_entity(admin_id) for admin_id in admin_ids]
        user_results = await asyncio.gather(*tasks, return_exceptions=True)
        user_map = {}
        for admin_id, result in zip(admin_ids, user_results):
            if isinstance(result, User): user_map[admin_id] = result.first_name
            else: user_map[admin_id] = f"(کاربر {admin_id})"
            
        # فیلتر کردن آمارهای صفر (در صورت F زدن) و مرتب‌سازی
        valid_admin_stats = [stat for stat in all_admin_stats if stat[1] > 0]
        sorted_admin_stats = sorted(valid_admin_stats, key=lambda x: x[1], reverse=True)
        
        if not sorted_admin_stats:
             admin_stats_msg += "هیچ فعالیتی از ادمین‌ها (با مجموع S > 0) ثبت نشده است."
        
        for admin_id, total_s_sum in sorted_admin_stats:
            name = user_map.get(admin_id, f"(کاربر {admin_id})")
            salary = total_s_sum * salary_rate
            total_all_salary += salary
            admin_stats_msg += f"👤 **{name}** (ID: `{admin_id}`)\n"
            admin_stats_msg += f"   - مجموع S: **{total_s_sum}**\n"
            admin_stats_msg += f"   - دستمزد (تومان): `{salary:,.0f}`\n\n"
    
    admin_stats_msg += f"--------------------\n"
    admin_stats_msg += f"💸 **جمع کل دستمزدها:** `{total_all_salary:,.0f}` **تومان**"
    
    await send_long_message(OWNER_ID, admin_stats_msg)
    await asyncio.sleep(1) 

    # گزارش ب: آمار گروه‌ها
    group_stats_msg = f"🔔 **گزارش درآمد گروه‌ها**\n"
    group_stats_msg += f"🗓 **تاریخ:** `{yesterday_date}`\n\n"
    
    if not group_stats_for_owner:
        group_stats_msg += "هیچ فعالیتی در گروه‌ها ثبت نشده است."
    else:
        # مرتب‌سازی گروه‌ها بر اساس درآمد
        sorted_group_stats = sorted(group_stats_for_owner, key=lambda x: x[2], reverse=True)
        for chat_title, chat_id, total_toman in sorted_group_stats:
            # ⭐️ اصلاحیه: حذف اعشار
            group_stats_msg += f"🏠 **{chat_title}** (ID: `{chat_id}`)\n"
            group_stats_msg += f"   - درآمد (تومان): `{total_toman:,.0f}`\n\n"
    
    group_stats_msg += f"--------------------\n"
    # ⭐️ اصلاحیه: حذف اعشار
    group_stats_msg += f"💰 **جمع کل درآمد گروه‌ها:** `{total_all_groups_income:,.0f}` **تومان**\n"
    # محاسبه سود خالص
    profit = total_all_groups_income - total_all_salary
    # ⭐️ اصلاحیه: حذف اعشار
    group_stats_msg += f"📈 **سود خالص (درآمد - دستمزد):** `{profit:,.0f}` **تومان**"
    
    await send_long_message(OWNER_ID, group_stats_msg)
    
    logging.info(f"Daily reports for {yesterday_date} sent successfully to owner.")


# --- تابع اصلی (Main) ---

async def main():
    """تابع اصلی برای اجرای ربات"""
    # ۱. بارگذاری ادمین‌ها و گروه‌های فعال
    await load_initial_data()
    
    # ۲. تنظیم زمان‌بند 
    # اجرا در ساعت 00:01 بامداد به وقت تهران
    scheduler.add_job(send_daily_reports, 'cron', hour=0, minute=1, second=0, timezone='Asia/Tehran')
    # scheduler.add_job(send_daily_reports, 'cron', hour=1, minute=35, second=0, timezone='Asia/Tehran') # ❗️ زمان تست
    
    scheduler.start()
    logging.info("Scheduler started for daily reports at 00:01 Tehran time.")
    
    # ۳. شروع کلاینت ربات
    try:
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        logging.info(f"Bot started successfully as @{me.username}.")
    except Exception as e:
        logging.critical(f"Failed to start bot: {e}")
        return

    # ۴. اجرای ربات
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped manually.")
