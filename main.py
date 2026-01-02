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
# ⭐️⭐️ (جدید) آدرس دیتابیس حسابداری سراب ⭐️⭐️
EXTERNAL_DB_PATH = '../hesabdar/bot_database.db'


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
            # جدول تنظیمات کلیدی (تتر, دستمزد)
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
            # جدول گروه‌های فعال
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_groups (chat_id INTEGER PRIMARY KEY)
            ''')
            # ⭐️ جدول ادمین‌های ویژه (تغییر یافته) ⭐️
            # اکنون شامل نرخ کمیسیون فردی است
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS special_admins (
                user_id INTEGER PRIMARY KEY,
                rate REAL DEFAULT 0
            )
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
            # ⭐️ جدول آمار تفکیکی E (امتیاز) برای هر ادمین (جدید) ⭐️
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_daily_e_stats (
                date TEXT,
                chat_id INTEGER,
                admin_id INTEGER,
                e_key TEXT,
                e_key_count INTEGER,
                e_key_sum INTEGER,
                PRIMARY KEY(date, chat_id, admin_id, e_key)
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

    # --- ⭐️ توابع ادمین ویژه (تغییر یافته) ⭐️ ---
    def set_special_admin_rate(self, user_id, rate):
        """افزودن یا به‌روزرسانی نرخ کمیسیون ادمین ویژه"""
        with self.get_conn() as conn:
            # اطمینان حاصل شود که ابتدا یک ادمین عادی است
            self.add_admin(user_id)
            conn.execute("INSERT OR REPLACE INTO special_admins (user_id, rate) VALUES (?, ?)", (user_id, rate))
            
    def remove_special_admin(self, user_id):
        if user_id == OWNER_ID: return False
        with self.get_conn() as conn:
            conn.execute("DELETE FROM special_admins WHERE user_id = ?", (user_id,))
            return conn.total_changes > 0
            
    def get_all_special_admin_rates(self):
        """دریافت دیکشنری از ادمین‌های ویژه و نرخ‌های کمیسیونشان"""
        with self.get_conn() as conn:
            cursor = conn.execute("SELECT user_id, rate FROM special_admins")
            # ⭐️ بازگشت به صورت دیکشنری {id: rate}
            return {row[0]: Decimal(str(row[1])) for row in cursor.fetchall()}

    def get_special_admin_rate(self, user_id):
        """دریافت نرخ کمیسیون یک ادمین ویژه خاص"""
        with self.get_conn() as conn:
            cursor = conn.execute("SELECT rate FROM special_admins WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return Decimal(str(row[0])) if row else None

    # --- توابع گروه‌های فعال ---
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
        yesterday = datetime.now(TEHRAN_TZ) - timedelta(days=1)
        return yesterday.strftime('%Y-%m-%d')

    def update_group_stat(self, date, chat_id, toman_to_add):
        with self.get_conn() as conn:
            conn.execute('''
            INSERT INTO group_daily_stats (date, chat_id, total_toman)
            VALUES (?, ?, ?)
            ON CONFLICT(date, chat_id) DO UPDATE SET
            total_toman = total_toman + excluded.total_toman
            ''', (date, chat_id, float(toman_to_add)))

    def update_admin_stat(self, date, chat_id, admin_id, s_sum_to_add):
        with self.get_conn() as conn:
            conn.execute('''
            INSERT INTO admin_daily_stats (date, chat_id, admin_id, total_s_sum)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date, chat_id, admin_id) DO UPDATE SET
            total_s_sum = total_s_sum + excluded.total_s_sum
            ''', (date, chat_id, admin_id, s_sum_to_add))

    def update_s_key_stat(self, date, chat_id, admin_id, s_key, s_number):
        with self.get_conn() as conn:
            conn.execute('''
            INSERT INTO s_key_daily_stats (date, chat_id, admin_id, s_key, s_key_count, s_key_sum)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(date, chat_id, admin_id, s_key) DO UPDATE SET
            s_key_count = s_key_count + 1,
            s_key_sum = s_key_sum + excluded.s_key_sum
            ''', (date, chat_id, admin_id, s_key, s_number))

    def reduce_s_key_stat(self, date, chat_id, admin_id, s_key, s_number_to_reduce):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT s_key_count FROM s_key_daily_stats "
                "WHERE date = ? AND chat_id = ? AND admin_id = ? AND s_key = ?",
                (date, chat_id, admin_id, s_key)
            )
            row = cursor.fetchone()
            if not row or row[0] <= 0:
                return False 
            conn.execute('''
            UPDATE s_key_daily_stats SET
            s_key_count = s_key_count - 1,
            s_key_sum = s_key_sum - ?
            WHERE date = ? AND chat_id = ? AND admin_id = ? AND s_key = ?
            ''', (s_number_to_reduce, date, chat_id, admin_id, s_key))
            return True

    def get_admin_available_s_keys(self, date, chat_id, admin_id):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT s_key FROM s_key_daily_stats "
                "WHERE date = ? AND chat_id = ? AND admin_id = ? AND s_key_count > 0 "
                "ORDER BY s_key",
                (date, chat_id, admin_id)
            )
            return [row[0] for row in cursor.fetchall()]

# --- ⭐️ توابع جدید برای E و R (امتیاز) ⭐️ ---
    def update_e_key_stat(self, date, chat_id, admin_id, e_key, e_number):
        with self.get_conn() as conn:
            conn.execute('''
            INSERT INTO admin_daily_e_stats (date, chat_id, admin_id, e_key, e_key_count, e_key_sum)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(date, chat_id, admin_id, e_key) DO UPDATE SET
            e_key_count = e_key_count + 1,
            e_key_sum = e_key_sum + excluded.e_key_sum
            ''', (date, chat_id, admin_id, e_key, e_number))

    def reduce_e_key_stat(self, date, chat_id, admin_id, e_key, e_number_to_reduce):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT e_key_count FROM admin_daily_e_stats "
                "WHERE date = ? AND chat_id = ? AND admin_id = ? AND e_key = ?",
                (date, chat_id, admin_id, e_key)
            )
            row = cursor.fetchone()
            if not row or row[0] <= 0:
                return False 
            conn.execute('''
            UPDATE admin_daily_e_stats SET
            e_key_count = e_key_count - 1,
            e_key_sum = e_key_sum - ?
            WHERE date = ? AND chat_id = ? AND admin_id = ? AND e_key = ?
            ''', (e_number_to_reduce, date, chat_id, admin_id, e_key))
            return True

    def get_admin_available_e_keys(self, date, chat_id, admin_id):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT e_key FROM admin_daily_e_stats "
                "WHERE date = ? AND chat_id = ? AND admin_id = ? AND e_key_count > 0 "
                "ORDER BY e_key",
                (date, chat_id, admin_id)
            )
            return [row[0] for row in cursor.fetchall()]

    # --- توابع گزارش‌گیری ---
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
        
    def get_admin_e_stats_for_group(self, date, chat_id):
            """ (جدید) دریافت آمار E (امتیاز) برای ادمین‌های یک گروه خاص """
            with self.get_conn() as conn:
                cursor = conn.execute(
                    "SELECT admin_id, SUM(e_key_sum) FROM admin_daily_e_stats "
                    "WHERE date = ? AND chat_id = ? GROUP BY admin_id",
                    (date, chat_id)
                )
                # بازگشت به صورت دیکشنری {id: e_sum}
                return {row[0]: row[1] for row in cursor.fetchall() if row[1] > 0}

    def get_active_groups_for_report(self, date):
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
        """گزارش دستمزد (مبتنی بر S Sum)"""
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT admin_id, SUM(total_s_sum) FROM admin_daily_stats "
                "WHERE date = ? GROUP BY admin_id", (date,)
            )
            return cursor.fetchall() # ⭐️ بازگشت به صورت لیست تاپل‌ها

    def get_all_group_income_stats(self, date):
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT chat_id, total_toman FROM group_daily_stats WHERE date = ?", (date,)
            )
            return cursor.fetchall()

    # --- ⭐️ توابع گزارش‌گیری جدید (مورد استفاده m4 نخواهد بود اما برای خواندن adminsbot باقی میماند) ⭐️ ---
    
    def get_all_admin_e_stats(self, date):
        """دریافت مجموع امتیاز E (e_key_sum) برای همه ادمین‌ها"""
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT admin_id, SUM(e_key_sum) FROM admin_daily_e_stats "
                "WHERE date = ? GROUP BY admin_id", (date,)
            )
            return {row[0]: row[1] for row in cursor.fetchall() if row[1] > 0}

    def get_all_admin_s_counts(self, date):
        """دریافت مجموع تعداد S (s_key_count) برای همه ادمین‌ها"""
        with self.get_conn() as conn:
            cursor = conn.execute(
                "SELECT admin_id, SUM(s_key_count) FROM s_key_daily_stats "
                "WHERE date = ? GROUP BY admin_id", (date,)
            )
            # ⭐️ بازگشت به صورت دیکشنری {id: count}
            return {row[0]: row[1] for row in cursor.fetchall() if row[1] > 0}


# --- متغیرهای گلوبال و نمونه‌سازی ---

client = TelegramClient('bot_session_name', API_ID, API_HASH)
db = Database(DB_NAME)

# ⭐️ کش‌های مموری (به‌روز شده) ⭐️
bot_admins_cache = set()
active_groups_cache = set()
special_admins_cache = {} # ⭐️ کش ادمین‌های ویژه (تغییر به دیکشنری)

# ⭐️ الگوهای Regex (به‌روز شده) ⭐️
S_PATTERN = re.compile(r'^[sS](\d+)$')
F_PATTERN = re.compile(r'^[fF](\d+)$') 
E_PATTERN = re.compile(r'^[eE](\d+)$')
R_PATTERN = re.compile(r'^[rR](\d+)$') # ⭐️ اصلاح شد

scheduler = AsyncIOScheduler() 

# --- ⭐️ (جدید) توابع کمکی دیتابیس خارجی ⭐️ ---

def _blocking_update_external_db(chat_id, chat_title, amount_to_add):
    try:
        if amount_to_add == 0:
            logging.info(f"Skipping external DB update for {chat_id}, amount is zero.")
            return True
            
        with sqlite3.connect(EXTERNAL_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
            INSERT INTO groups (chat_id, chat_title, is_active, balance)
            VALUES (?, ?, 1, 0)
            ON CONFLICT(chat_id) DO UPDATE SET
            chat_title = excluded.chat_title,
            is_active = 1
            ''', (chat_id, chat_title))
            cursor.execute('''
            UPDATE groups SET balance = balance + ?
            WHERE chat_id = ?
            ''', (amount_to_add, chat_id))
            conn.commit()
            logging.info(f"Successfully updated external DB for {chat_id}. Added {amount_to_add} to balance.")
            return True
    except sqlite3.OperationalError as e:
        logging.error(f"External DB OperationalError for {chat_id} at {EXTERNAL_DB_PATH}: {e}")
        return False
    except Exception as e:
        logging.error(f"Failed to update external DB for {chat_id} at {EXTERNAL_DB_PATH}: {e}")
        return False

async def update_external_db_balance(chat_id, chat_title, amount_to_add):
    return await asyncio.to_thread(_blocking_update_external_db, chat_id, chat_title, amount_to_add)


# --- ⭐️⭐️ (حذف شد) توابع دیتابیس خارجی برای ادمین‌ها ⭐️⭐️ ---
# توابع _blocking_update_admin_balance و update_external_admin_balance
# اکنون توسط ربات adminsbot.py مدیریت می‌شوند و از m4.py حذف شدند.


# --- توابع کمکی ---

async def get_user_from_event(event):
    if event.reply_to_msg_id:
        try:
            reply_msg = await event.get_reply_message()
            return await client.get_entity(reply_msg.sender_id)
        except Exception: return None
    args = event.text.split(maxsplit=1)
    if len(args) < 2: return None
    
    user_arg = args[1].split()[0]
    return await get_user_by_id_or_username(user_arg)

async def get_user_by_id_or_username(target):
    try:
        if target.startswith('@'): return await client.get_entity(target)
        elif target.isdigit(): return await client.get_entity(int(target))
    except Exception: return None
    return None

async def load_initial_data():
    """ ⭐️ بارگذاری ادمین‌ها، گروه‌های فعال و ادمین‌های ویژه در کش (به‌روز شده) ⭐️"""
    global bot_admins_cache, active_groups_cache, special_admins_cache
    bot_admins_cache = db.get_all_admin_ids()
    active_groups_cache = db.get_all_active_groups()
    special_admins_cache = db.get_all_special_admin_rates() # ⭐️ (تغییر به دیکشنری)
    logging.info(f"Loaded {len(bot_admins_cache)} admins from DB.")
    logging.info(f"Loaded {len(active_groups_cache)} active groups from DB.")
    logging.info(f"Loaded {len(special_admins_cache)} special admins (with rates) from DB.") # ⭐️ (جدید)

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
    except UserIsBlockedError:
        logging.warning(f"Could not send message to {peer}, user blocked the bot.")
    except Exception as e:
        logging.error(f"Failed to send long message to {peer}: {e}")

# --- ⭐️ کنترل‌کننده‌های رویداد (Handlers) (به‌روز شده) ⭐️ ---

def build_main_keyboard_menu():
    """(اصلاح شده) ساخت دکمه‌های کیبورد پنل اصلی مالک"""
    return [
        [KeyboardButton("💵 تنظیم نرخ تتر"), KeyboardButton("💰 تنظیم دستمزد S")],
        [KeyboardButton("📊 مدیریت ارزش S"), KeyboardButton("👮‍♂️ مدیریت ادمین‌ها")],
        [KeyboardButton("⭐️ مدیریت ادمین ویژه")],
        [KeyboardButton("💵 تنظیم ارزش E (تومان)")],
        [KeyboardButton("📋 نمایش وضعیت")],
        [KeyboardButton("✖️ بستن کیبورد ✖️")]
    ]

@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if event.sender_id == OWNER_ID:
        if event.is_private:
            button_layout = build_main_keyboard_menu()
            keyboard_rows = []
            for row_buttons in button_layout:
                keyboard_rows.append(KeyboardButtonRow(buttons=row_buttons))
            manual_markup = ReplyKeyboardMarkup(rows=keyboard_rows, resize=True)
            await event.reply(
                "سلام مالک! 👋\nبه پنل مدیریت ربات آمار S خوش آمدید.\n\n"
                "از دکمه‌های زیر برای مدیریت استفاده کنید:",
                buttons=manual_markup
            )
        else:
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
            "`/add_scoreandroid` (فعال‌سازی گروه)\n"
            "`/remove_scoreandroid` (غیرفعال‌سازی گروه)\n\n"

            "**- مدیریت ربات (دستوری):**\n"
            "`/panel` (باز کردن پنل شیشه‌ای)\n"
            "`/status` (نمایش وضعیت تنظیمات)\n"
            "`/stats` (آمار لحظه‌ای امروز گروه)\n\n"
            
            "**- مدیریت ادمین:**\n"
            "`/addadmin <ID/@/Reply>`\n"
            "`/deladmin <ID/@/Reply>`\n"
            "`/listadmins`\n\n"
            
            "**- ⭐️ مدیریت ادمین ویژه (جدید):**\n"
            "`/addspecial <ID/@> <rate>` (افزودن/تغییر نرخ کمیسیون S-Sum)\n"
            "   (یا با ریپلای: `/addspecial <rate>`)\n"
            "`/delspecial <ID/@/Reply>` (حذف از ویژه)\n"
            "`/listspecial` (لیست ویژه با نرخ‌ها)\n\n"

            "**- مدیریت مقادیر:**\n"
            "`/setvalue <s_key> <value>` (مثال: /setvalue s1 0.7)\n"
            "`/settether <price>` (مثال: /settether 50000)\n"
            "`/setsalary <amount>` (دستمزد S Sum) (مثال: /setsalary 10000)\n"
            "`/setevalue <price>` (⭐️ نرخ امتیاز E) (مثال: /setevalue 100)\n\n"
            
            "**- پنل کیبورد (در PV):**\n"
            "با ارسال /start در پی‌وی، پنل کیبوردی برای دسترسی سریع به تنظیمات باز می‌شود.\n"
        )
    else:
        help_text += "شما به دستورات مدیریتی دسترسی ندارید.\n"
        if sender_id in bot_admins_cache:
            help_text += "شما ادمین ربات هستید و پیام‌های `s<عدد>`، `f<عدد>`، `e<عدد>` و `r<عدد>` شما در گروه‌های فعال محاسبه می‌شود."
            if sender_id in special_admins_cache: # ⭐️ (تغییر به دیکشنری)
                rate = special_admins_cache.get(sender_id, 0)
                help_text += f"\n⭐️ **شما ادمین ویژه با نرخ کمیسیون {rate:,.0f} تومان هستید.**"
        else:
            help_text += "شما کاربر عادی هستید."
            
    await event.respond(help_text, parse_mode='md')

# --- ⭐️ دستورات فعال‌سازی گروه (بدون تغییر) ⭐️ ---

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


# --- ⭐️ پنل کیبوردی مالک (به‌روز شده) ⭐️ ---

@client.on(events.NewMessage(pattern='^💵 تنظیم نرخ تتر$', from_users=OWNER_ID))
async def text_set_tether(event):
    if not event.is_private: return
    await start_conversation_helper(
        event,
        key_name='tether_price',
        prompt_message="لطفاً نرخ جدید تتر به تومان را وارد کنید:",
        success_message_template="✅ نرخ تتر با موفقیت روی {value} تومان تنظیم شد."
    )

@client.on(events.NewMessage(pattern='^💰 تنظیم دستمزد S$', from_users=OWNER_ID))
async def text_set_salary(event):
    if not event.is_private: return
    await start_conversation_helper(
        event,
        key_name='salary_rate',
        prompt_message="لطفاً دستمزد به ازای هر واحد S (Sum) را وارد کنید:",
        success_message_template="✅ دستمزد هر واحد S (Sum) با موفقیت روی {value} تومان تنظیم شد."
    )

@client.on(events.NewMessage(pattern='^📊 مدیریت ارزش S$', from_users=OWNER_ID))
async def text_s_value_panel(event):
    if not event.is_private: return
    buttons = [
        [Button.inline("➕ تنظیم/تغییر ارزش", b"panel_s_value_set")],
        [Button.inline("📋 لیست ارزش‌ها", b"panel_s_value_list")],
    ]
    await event.reply("📊 **مدیریت ارزش S**\n\n(از دکمه‌های شیشه‌ای زیر استفاده کنید):", buttons=buttons)

# ⭐️ (اصلاح شده)
@client.on(events.NewMessage(pattern='^👮‍♂️ مدیریت ادمین‌ها$', from_users=OWNER_ID))
async def text_admin_panel(event):
    if not event.is_private: return
    buttons = [
        [Button.inline("➕ افزودن ادمین", b"panel_add_admin")],
        [Button.inline("➖ حذف ادمین", b"panel_del_admin")],
        [Button.inline("📋 لیست ادمین‌ها", b"panel_list_admins")],
    ]
    await event.reply("👮‍♂️ **مدیریت ادمین‌ها**\n\n(از دکمه‌های شیشه‌ای زیر استفاده کنید):", buttons=buttons)

# ⭐️ (اصلاح شده)
@client.on(events.NewMessage(pattern='^⭐️ مدیریت ادمین ویژه$', from_users=OWNER_ID))
async def text_special_admin_panel(event):
    if not event.is_private: return
    buttons = [
        [Button.inline("➕ افزودن/تغییر نرخ کمیسیون", b"panel_add_special_admin")],
        [Button.inline("➖ حذف ادمین ویژه", b"panel_del_special_admin")],
        [Button.inline("📋 لیست ادمین‌های ویژه", b"panel_list_special_admins")],
    ]
    await event.reply("⭐️ **مدیریت ادمین‌های ویژه**\n\n(از دکمه‌های شیشه‌ای زیر استفاده کنید):", buttons=buttons)

# ⭐️ (اصلاح شده)
@client.on(events.NewMessage(pattern=r'^💵 تنظیم ارزش E \(تومان\)$', from_users=OWNER_ID))
async def text_set_e_value(event):
    if not event.is_private: return
    await start_conversation_helper(
        event,
        key_name='e_point_value',
        prompt_message="لطفاً ارزش تومانی هر *امتیاز* E را وارد کنید:",
        success_message_template="✅ ارزش هر امتیاز E با موفقیت روی {value} تومان تنظیم شد."
    )

@client.on(events.NewMessage(pattern='^📋 نمایش وضعیت$', from_users=OWNER_ID))
async def text_status_handler(event):
    if not event.is_private: return
    await status_handler(event)

@client.on(events.NewMessage(pattern='^✖️ بستن کیبورد ✖️$', from_users=OWNER_ID))
async def text_close_keyboard(event):
    if not event.is_private: return
    await event.reply("کیبورد بسته شد.", buttons=Button.clear())


# --- ⭐️ پنل مدیریت شیشه‌ای (به‌روز شده) ⭐️ ---

def build_main_panel_menu():
    """(اصلاح شده) ساخت دکمه‌های پنل اصلی شیشه‌ای"""
    return [
        [Button.inline("💵 تنظیم نرخ تتر", b"panel_tether"), Button.inline("💰 تنظیم دستمزد S", b"panel_salary")],
        [Button.inline("📊 مدیریت ارزش S", b"panel_s_values"), Button.inline("👮‍♂️ مدیریت ادمین‌ها", b"panel_admins")],
        [Button.inline("⭐️ مدیریت ادمین ویژه", b"panel_special_admins")],
        [Button.inline("💵 تنظیم ارزش E (تومان)", b"panel_e_value")],
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

# --- ⭐️ جریان مکالمه (Conversation) برای تنظیمات (به‌روز شده) ⭐️ ---

async def start_conversation_helper(event, key_name, prompt_message, success_message_template, validation_regex=r'([\d\.]+)'):
    """
    تابع کمکی برای شروع مکالمه.
    """
    try:
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
                if value < 0: # ⭐️ اجازه تنظیم صفر را می‌دهیم (برای غیرفعال کردن)
                    raise ValueError("Value cannot be negative")
                
                db.set_setting(key_name, str(value))
                
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
        prompt_message="لطفاً دستمزد به ازای هر واحد S (Sum) را وارد کنید:",
        success_message_template="✅ دستمزد هر واحد S (Sum) با موفقیت روی {value} تومان تنظیم شد."
    )

@client.on(events.CallbackQuery(data=b'panel_e_value'))
async def set_e_value_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await start_conversation_helper(
        event,
        key_name='e_point_value',
        prompt_message="لطفاً ارزش تومانی هر *امتیاز* E را وارد کنید:",
        success_message_template="✅ ارزش هر امتیاز E با موفقیت روی {value} تومان تنظیم شد."
    )

# --- ⭐️ پنل مدیریت ادمین‌ها (عادی) (تغییر یافته) ⭐️ ---
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

async def conversation_add_admin_helper(event, conv, action_func, cache_set, success_message_template):
    """تابع کمکی برای افزودن ادمین (عادی)"""
    try:
        response = await conv.get_response()
        if response.text == '/cancel':
            await conv.send_message("عملیات لغو شد.")
            return
        user = await get_user_by_id_or_username(response.text.strip())
        if not user:
            await conv.send_message("❌ کاربر یافت نشد. عملیات لغو شد.")
            return
        action_func(user.id) # db.add_admin(user.id)
        cache_set.add(user.id)
        await conv.send_message(success_message_template.format(name=user.first_name, id=user.id))
        await event.answer("✅ اضافه شد")
    except asyncio.TimeoutError:
        await conv.send_message("زمان شما تمام شد. عملیات لغو شد.")
    except Exception as e:
        await conv.send_message(f"❌ خطا: {e}. عملیات لغو شد.")

async def conversation_del_admin_helper(event, conv, action_func, cache_set, success_message_template):
    """تابع کمکی برای حذف ادمین (عادی و ویژه)"""
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
        if action_func(user.id): # db.remove_admin(user.id) or db.remove_special_admin(user.id)
            cache_set.discard(user.id)
            await conv.send_message(success_message_template.format(name=user.first_name, id=user.id))
            await event.answer("✅ حذف شد")
        else:
            await conv.send_message("❌ این کاربر در لیست مربوطه وجود نداشت.")
    except asyncio.TimeoutError:
        await conv.send_message("زمان شما تمام شد. عملیات لغو شد.")
    except Exception as e:
        await conv.send_message(f"❌ خطا: {e}. عملیات لغو شد.")

@client.on(events.CallbackQuery(data=b'panel_add_admin'))
async def add_admin_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await event.answer("منتظر ورودی...")
    try:
        async with client.conversation(event.chat_id, timeout=120) as conv:
            await conv.send_message(
                "💬 لطفاً شناسه عددی (ID) یا یوزرنیم (@username) کاربر مورد نظر را برای افزودن به **ادمین‌های عادی** ارسال کنید.\n"
                "(برای افزودن با ریپلای، از دستور `/addadmin` استفاده کنید.)\n\n"
                "(برای لغو /cancel را ارسال کنید.)"
            )
            await conversation_add_admin_helper(
                event, conv,
                db.add_admin,
                bot_admins_cache,
                "✅ کاربر {name} (ID: `{id}`) با موفقیت به ادمین‌ها اضافه شد."
            )
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
                "💬 لطفاً شناسه عددی (ID) یا یوزرنیم (@username) کاربری که می‌خواهید از **ادمین‌های عادی** حذف کنید را ارسال کنید.\n"
                "(این کار او را از لیست ویژه حذف *نمی‌کند*.)\n\n"
                "(برای لغو /cancel را ارسال کنید.)"
            )
            await conversation_del_admin_helper(
                event, conv,
                db.remove_admin,
                bot_admins_cache,
                "✅ کاربر {name} (ID: `{id}`) با موفقیت از ادمین‌ها حذف شد."
            )
    except Exception as e:
        await event.answer(f"خطا: {e}")

async def list_admins_helper(event, admin_cache, title, back_button_data):
    """تابع کمکی برای نمایش لیست ادمین‌های عادی (تغییر یافته)"""
    if not admin_cache:
        await event.answer(f"لیست {title} خالی است.", alert=True)
        return
    msg = f"**{title}:**\n\n"
    tasks = [client.get_entity(admin_id) for admin_id in admin_cache]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    admin_list_lines = []
    for admin_id, result in zip(admin_cache, results):
        line = f"- (کاربر یافت نشد) (ID: `{admin_id}`)"
        if isinstance(result, User):
            name = result.first_name
            username = f"(@{result.username})" if result.username else ""
            if admin_id == OWNER_ID:
                name += " (👑 مالک)"
            
            # ⭐️ (تغییر) نمایش نرخ کمیسیون ویژه در لیست ادمین‌های عادی
            if admin_id in special_admins_cache: # special_admins_cache is dict
                 rate = special_admins_cache.get(admin_id, 0)
                 name += f" (⭐️ ویژه - کمیسیون: {rate:,.0f} T)"
            line = f"- {name} {username} (ID: `{admin_id}`)"
        admin_list_lines.append(line)
            
    msg += "\n".join(sorted(admin_list_lines))
    msg += f"\n\nتعداد کل: {len(admin_list_lines)} نفر"
    await event.answer()
    await event.edit(msg, buttons=[Button.inline(" بازگشت 🔙", back_button_data)])

@client.on(events.CallbackQuery(data=b'panel_list_admins'))
async def list_admins_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await list_admins_helper(event, bot_admins_cache, "لیست ادمین‌های ربات", b"panel_admins")


# --- ⭐️⭐️ (جدید) پنل مدیریت ادمین‌های ویژه (تغییر یافته) ⭐️⭐️ ---
@client.on(events.CallbackQuery(data=b'panel_special_admins'))
async def special_admin_panel_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    buttons = [
        [Button.inline("➕ افزودن/تغییر نرخ کمیسیون", b"panel_add_special_admin")],
        [Button.inline("➖ حذف ادمین ویژه", b"panel_del_special_admin")],
        [Button.inline("📋 لیست ادمین‌های ویژه", b"panel_list_special_admins")],
        [Button.inline(" بازگشت 🔙", b"panel_main")]
    ]
    await event.edit("⭐️ **مدیریت ادمین‌های ویژه**", buttons=buttons)

@client.on(events.CallbackQuery(data=b'panel_add_special_admin'))
async def add_special_admin_callback(event):
    """(بازنویسی شده) مکالمه دو مرحله‌ای برای افزودن/تغییر نرخ کمیسیون ادمین ویژه"""
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await event.answer("منتظر ورودی...")
    try:
        async with client.conversation(event.chat_id, timeout=180) as conv:
            await conv.send_message(
                "💬 لطفاً شناسه (ID) یا یوزرنیم (@username) ادمینی که می‌خواهید 'ویژه' شود یا نرخش 'تغییر' کند را ارسال کنید.\n\n"
                "(برای لغو /cancel را ارسال کنید.)"
            )
            try:
                user_response = await conv.get_response()
                if user_response.text == '/cancel':
                    await conv.send_message("عملیات لغو شد.")
                    return
                
                user = await get_user_by_id_or_username(user_response.text.strip())
                if not user:
                    await conv.send_message("❌ کاربر یافت نشد. عملیات لغو شد.")
                    return

                await conv.send_message(
                    f"✅ کاربر: {user.first_name} (`{user.id}`)\n\n"
                    "💬 اکنون، لطفاً **نرخ کمیسیون** فردی او برای هر S (Sum) *کل* را به تومان وارد کنید.\n"
                    "مثال: `1000`\n\n(برای لغو /cancel را ارسال کنید.)"
                )
                
                rate_response = await conv.get_response()
                if rate_response.text == '/cancel':
                    await conv.send_message("عملیات لغو شد.")
                    return
                
                try:
                    rate = Decimal(rate_response.text.strip())
                    if rate < 0:
                        raise ValueError("نرخ نمی‌تواند منفی باشد.")
                except Exception:
                    await conv.send_message("❌ نرخ نامعتبر است. باید عدد باشد. عملیات لغو شد.")
                    return
                
                # موفقیت
                db.set_special_admin_rate(user.id, float(rate)) # تابع DB او را ادمین عادی هم می‌کند
                special_admins_cache[user.id] = rate # آپدیت کش دیکشنری
                bot_admins_cache.add(user.id) # آپدیت کش عادی
                
                await conv.send_message(
                    f"✅ **ثبت شد!**\n"
                    f"کاربر: {user.first_name} (ID: `{user.id}`)\n"
                    f"نرخ کمیسیون: **{rate:,.0f} تومان** به ازای هر S (Sum) *کل*\n"
                    "این کاربر اکنون ادمین ویژه (و عادی) است."
                )
                await event.answer("✅ ثبت شد")

            except asyncio.TimeoutError:
                await conv.send_message("زمان شما تمام شد. عملیات لغو شد.")
            except Exception as e:
                await conv.send_message(f"❌ خطا: {e}. عملیات لغو شد.")
    except Exception as e:
        await event.answer(f"خطا: {e}")


@client.on(events.CallbackQuery(data=b'panel_del_special_admin'))
async def del_special_admin_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await event.answer("منتظر ورودی...")
    try:
        async with client.conversation(event.chat_id, timeout=120) as conv:
            await conv.send_message(
                "💬 لطفاً شناسه عددی (ID) یا یوزرنیم (@username) کاربری که می‌خواهید از **ادمین‌های ویژه** حذف کنید را ارسال کنید.\n"
                "(این کاربر همچنان ادمین عادی باقی می‌ماند.)\n\n"
                "(برای لغو /cancel را ارسال کنید.)"
            )
            # ⭐️ (تغییر) cache_set اکنون یک دیکشنری است، از .pop() استفاده می‌کنیم
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
                if db.remove_special_admin(user.id):
                    special_admins_cache.pop(user.id, None) # ⭐️ حذف از کش دیکشنری
                    await conv.send_message(f"✅ کاربر {user.first_name} (ID: `{user.id}`) با موفقیت از ادمین‌های ویژه حذف شد.")
                    await event.answer("✅ حذف شد")
                else:
                    await conv.send_message("❌ این کاربر در لیست ادمین‌های ویژه وجود نداشت.")
            except asyncio.TimeoutError:
                await conv.send_message("زمان شما تمام شد. عملیات لغو شد.")
            except Exception as e:
                await conv.send_message(f"❌ خطا: {e}. عملیات لغو شد.")
    except Exception as e:
        await event.answer(f"خطا: {e}")

async def list_special_admins_helper(event, admin_cache_dict, title, back_button_data):
    """(جدید) تابع کمکی برای نمایش لیست ادمین‌های ویژه (که دیکشنری است)"""
    if not admin_cache_dict:
        await event.answer(f"لیست {title} خالی است.", alert=True)
        return
    
    msg = f"**{title}:**\n\n"
    
    admin_ids = list(admin_cache_dict.keys())
    tasks = [client.get_entity(admin_id) for admin_id in admin_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    user_map = {admin_id: result for admin_id, result in zip(admin_ids, results)}
    
    admin_list_lines = []
    
    # مرتب‌سازی بر اساس نام برای نمایش
    try:
        sorted_items = sorted(
            admin_cache_dict.items(), 
            key=lambda item: (user_map.get(item[0]).first_name if isinstance(user_map.get(item[0]), User) else str(item[0]))
        )
    except Exception:
        # در صورت خطا در مرتب‌سازی (مثلاً کاربر حذف شده)، عادی مرتب کن
        sorted_items = sorted(admin_cache_dict.items())

    
    for admin_id, rate in sorted_items:
        result = user_map.get(admin_id)
        line = f"- (کاربر یافت نشد) (ID: `{admin_id}`) - نرخ کمیسیون: `{rate:,.0f}` T"
        if isinstance(result, User):
            name = result.first_name
            username = f"(@{result.username})" if result.username else ""
            if admin_id == OWNER_ID:
                name += " (👑 مالک)"
            line = f"- {name} {username} (ID: `{admin_id}`) - **نرخ کمیسیون: {rate:,.0f} تومان**"
        admin_list_lines.append(line)
            
    msg += "\n".join(admin_list_lines)
    msg += f"\n\nتعداد کل: {len(admin_list_lines)} نفر"
    await event.answer()
    await event.edit(msg, buttons=[Button.inline(" بازگشت 🔙", back_button_data)])

@client.on(events.CallbackQuery(data=b'panel_list_special_admins'))
async def list_special_admins_callback(event):
    if event.sender_id != OWNER_ID:
        await event.answer("شما مجاز به استفاده از این دکمه نیستید.", alert=True)
        return
    await list_special_admins_helper(event, special_admins_cache, "لیست ادمین‌های ویژه (کمیسیون S-Sum)", b"panel_special_admins")


# --- پنل مدیریت ارزش S (بدون تغییر) ---
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


# --- ⭐️ دستورات متنی (به‌روز شده) ⭐️ ---

@client.on(events.NewMessage(pattern=r'/addadmin(?: |$)(.*)', from_users=OWNER_ID))
async def add_admin_handler(event):
    user = await get_user_from_event(event) # ⭐️ get_user_from_event اصلاح شده
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
    if not bot_admins_cache:
        await event.reply("لیست ادمین‌ها خالی است (به جز شما).")
        return
    msg = "**لیست ادمین‌های ربات:**\n\n"
    tasks = [client.get_entity(admin_id) for admin_id in bot_admins_cache]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    admin_list_lines = []
    for admin_id, result in zip(bot_admins_cache, results):
        line = f"- (کاربر یافت نشد) (ID: `{admin_id}`)"
        if isinstance(result, User):
            name = result.first_name
            username = f"(@{result.username})" if result.username else ""
            if admin_id == OWNER_ID:
                name += " (👑 مالک)"
            # ⭐️ (تغییر) نمایش نرخ کمیسیون ویژه
            if admin_id in special_admins_cache: # special_admins_cache is dict
                 rate = special_admins_cache.get(admin_id, 0)
                 name += f" (⭐️ ویژه - کمیسیون: {rate:,.0f} T)"
            line = f"- {name} {username} (ID: `{admin_id}`)"
        admin_list_lines.append(line)
    msg += "\n".join(sorted(admin_list_lines))
    msg += f"\n\nتعداد کل: {len(admin_list_lines)} نفر"
    await event.reply(msg)

# ⭐️ (جدید) دستورات متنی ادمین ویژه (تغییر یافته)
@client.on(events.NewMessage(pattern=r'/addspecial(?: |$)(.*)', from_users=OWNER_ID))
async def add_special_admin_handler(event):
    """(بازنویسی شده) افزودن/تغییر نرخ کمیسیون ویژه با دستور متنی"""
    args_str = event.pattern_match.group(1).strip()
    user = None
    rate_str = None

    # ۱. بررسی حالت ریپلای
    if event.reply_to_msg_id:
        reply_msg = await event.get_reply_message()
        user = await client.get_entity(reply_msg.sender_id)
        # در حالت ریپلای، آرگومان باید فقط نرخ باشد
        if args_str and re.match(r'^([\d\.]+)$', args_str):
            rate_str = args_str # /addspecial 500 (reply)
    
    # ۲. بررسی حالت عادی (بدون ریپلای)
    if not user:
        parts = args_str.split()
        if len(parts) >= 2:
            # نرخ را از انتهای آرگومان‌ها می‌خوانیم
            rate_match = re.match(r'^([\d\.]+)$', parts[-1])
            if rate_match:
                rate_str = rate_match.group(1)
                user_str = " ".join(parts[:-1]) # یوزرنیم/ID
                user = await get_user_by_id_or_username(user_str)

    # ۳. اعتبارسنجی
    if not user or not rate_str:
        await event.reply("❌ **فرمت نامعتبر**\n\n"
                          "استفاده صحیح:\n"
                          "`/addspecial <@user/ID> <rate>`\n"
                          "(یا با ریپلای روی کاربر): `/addspecial <rate>`")
        return

    # ۴. پردازش
    try:
        rate = Decimal(rate_str)
        if rate < 0: raise ValueError("نرخ منفی مجاز نیست")
        
        db.set_special_admin_rate(user.id, float(rate)) # تابع DB او را ادمین عادی هم می‌کند
        special_admins_cache[user.id] = rate # آپدیت کش دیکشنری
        bot_admins_cache.add(user.id) # آپدیت کش عادی

        await event.reply(
            f"✅ **ثبت شد!**\n"
            f"کاربر: {user.first_name} (ID: `{user.id}`)\n"
            f"نرخ کمیسیون: **{rate:,.0f} تومان** به ازای هر S (Sum) *کل*\n"
            "این کاربر اکنون ادمین ویژه (و عادی) است."
        )
    except Exception as e:
        await event.reply(f"❌ خطا در پردازش نرخ: {e}")

@client.on(events.NewMessage(pattern=r'/delspecial(?: |$)(.*)', from_users=OWNER_ID))
async def del_special_admin_handler(event):
    user = await get_user_from_event(event) # ⭐️ get_user_from_event اصلاح شده
    if not user:
        await event.reply("❌ خطا: کاربر یافت نشد. (از ID، @یوزرنیم یا ریپلای استفاده کنید)")
        return
    if user.id == OWNER_ID:
        await event.reply("❌ خطا: شما نمی‌توانید مالک را حذف کنید.")
        return
    if db.remove_special_admin(user.id):
        special_admins_cache.pop(user.id, None) # ⭐️ حذف از کش دیکشنری
        await event.reply(f"✅ کاربر {user.first_name} (ID: `{user.id}`) با موفقیت از ادمین‌های ویژه حذف شد (همچنان ادمین عادی است).")
    else:
        await event.reply("❌ خطا: این کاربر در لیست ادمین‌های ویژه وجود نداشت.")

@client.on(events.NewMessage(pattern=r'/listspecial', from_users=OWNER_ID))
async def list_special_admins_handler(event):
    """(بازنویسی شده) نمایش لیست ویژه با نرخ‌های کمیسیون"""
    if not special_admins_cache:
        await event.reply("لیست ادمین‌های ویژه خالی است.")
        return
    
    msg = "**لیست ادمین‌های ویژه (کمیسیون S-Sum کل):**\n\n"
    
    admin_ids = list(special_admins_cache.keys())
    tasks = [client.get_entity(admin_id) for admin_id in admin_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    user_map = {admin_id: result for admin_id, result in zip(admin_ids, results)}

    admin_list_lines = []
    # مرتب‌سازی بر اساس نام
    try:
        sorted_items = sorted(
            special_admins_cache.items(), 
            key=lambda item: (user_map.get(item[0]).first_name if isinstance(user_map.get(item[0]), User) else str(item[0]))
        )
    except Exception:
        sorted_items = sorted(special_admins_cache.items())
    
    for admin_id, rate in sorted_items:
        result = user_map.get(admin_id)
        line = f"- (کاربر یافت نشد) (ID: `{admin_id}`) - نرخ کمیسیون: `{rate:,.0f}` T"
        if isinstance(result, User):
            name = result.first_name
            username = f"(@{result.username})" if result.username else ""
            if admin_id == OWNER_ID:
                name += " (👑 مالک)"
            line = f"- {name} {username} (ID: `{admin_id}`) - **نرخ کمیسیون: {rate:,.0f} تومان**"
        admin_list_lines.append(line)

    msg += "\n".join(admin_list_lines)
    msg += f"\n\nتعداد کل: {len(admin_list_lines)} نفر"
    await event.reply(msg)
# ⭐️ (پایان جدید)


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

async def set_setting_handler(event, key, success_template):
    """تابع کمکی برای دستورات تنظیمات"""
    price_str = event.pattern_match.group(1)
    try:
        price = Decimal(price_str)
        if price < 0: raise ValueError() # اجازه صفر می‌دهیم
        db.set_setting(key, str(price))
        await event.reply(success_template.format(price=f"{price:,.0f}"))
    except Exception:
        await event.reply("❌ خطا: قیمت وارد شده نامعتبر است.")

@client.on(events.NewMessage(pattern=r'/settether ([\d\.]+)', from_users=OWNER_ID))
async def set_tether_handler(event):
    await set_setting_handler(event, 'tether_price', "✅ نرخ تتر با موفقیت روی {price} تومان تنظیم شد.")

@client.on(events.NewMessage(pattern=r'/setsalary ([\d\.]+)', from_users=OWNER_ID))
async def set_salary_handler(event):
    await set_setting_handler(event, 'salary_rate', "✅ دستمزد هر واحد S (Sum) با موفقیت روی {price} تومان تنظیم شد.")

@client.on(events.NewMessage(pattern=r'/setevalue ([\d\.]+)', from_users=OWNER_ID))
async def set_e_value_handler(event):
    await set_setting_handler(event, 'e_point_value', "✅ ارزش هر امتیاز E با موفقیت روی {price} تومان تنظیم شد.")


# --- ⭐️ آمار و پردازش S/F/E/R (به‌روز شده) ⭐️ ---
@client.on(events.NewMessage(pattern=r'/status', from_users=OWNER_ID))
async def status_handler(event):
    """(اصلاح شده) نمایش وضعیت لحظه‌ای تنظیمات ربات"""
    try:
        # دریافت همه تنظیمات
        settings_keys = ['tether_price', 'salary_rate', 'e_point_value']
        settings_values = {}
        for key in settings_keys:
            val_str = db.get_setting(key, 'تنظیم نشده')
            if val_str != 'تنظیم نشده':
                try:
                    settings_values[key] = f"{Decimal(val_str):,.0f} تومان"
                except Exception:
                    settings_values[key] = f"{val_str} (خطا در فرمت)"
            else:
                settings_values[key] = "تنظیم نشده"

        msg = "📊 **وضعیت لحظه‌ای تنظیمات ربات**\n\n"
        msg += f"💵 **نرخ تتر:** {settings_values['tether_price']}\n"
        msg += f"💰 **دستمزد S (Sum):** {settings_values['salary_rate']}\n"
        msg += f"💵 **ارزش هر E (امتیاز):** {settings_values['e_point_value']}\n\n"
        
        msg += "--- **ارزش‌های S (USDT)** ---\n"
        all_values = db.get_all_s_values()
        if not all_values:
            msg += "هیچ ارزشی تنظیم نشده است.\n"
        else:
            for key, value in all_values:
                msg += f"`{key}` = `{value}` USDT\n"
                
        msg += "\n--- **گروه‌های فعال** ---\n"
        if not active_groups_cache:
            msg += "هیچ گروهی فعال نشده است.\n"
        else:
            tasks = [client.get_entity(chat_id) for chat_id in active_groups_cache]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for chat_id, result in zip(active_groups_cache, results):
                msg += f"- {result.title if hasattr(result, 'title') else '(ناشناس)'} (`{chat_id}`)\n"

        msg += "\n--- **ادمین‌های ویژه (با نرخ کمیسیون S-Sum فردی)** ---\n"
        if not special_admins_cache:
            msg += "هیچ ادمین ویژه‌ای تنظیم نشده است.\n"
        else:
            admin_ids = list(special_admins_cache.keys())
            tasks = [client.get_entity(admin_id) for admin_id in admin_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            user_map = {admin_id: result for admin_id, result in zip(admin_ids, results)}
            
            for admin_id, rate in special_admins_cache.items():
                name = f"(کاربر {admin_id})"
                if admin_id in user_map and isinstance(user_map[admin_id], User):
                    name = user_map[admin_id].first_name
                msg += f"- {name} (`{admin_id}`) - **نرخ کمیسیون: {rate:,.0f} تومان**\n"
        
        await event.reply(msg, parse_mode='md')
    except Exception as e:
        await event.reply(f"❌ خطایی رخ داد: {e}")


@client.on(events.NewMessage(pattern=r'/stats', from_users=OWNER_ID))
async def stats_handler(event):
    """(اصلاح شده) نمایش آمار لحظه‌ای *امروز* گروه (شامل S و E)"""
    if not event.is_group:
        await event.reply("❌ این دستور فقط باید در گروه استفاده شود.")
        return
        
    chat_id = event.chat_id
    if chat_id not in active_groups_cache:
        await event.reply("❌ ربات در این گروه فعال نیست. (از /add_scoreandroid استفاده کنید)")
        return

    today_date = db.get_today_date()
    
    # --- دریافت آمار S ---
    salary_rate_str = db.get_setting('salary_rate', '0')
    salary_rate = Decimal(salary_rate_str)
    group_total = db.get_group_stat(today_date, chat_id)
    admin_s_stats_list = db.get_admin_stats_for_group(today_date, chat_id) # List of tuples [(id, s_sum)]
    admin_s_stats = dict(admin_s_stats_list) # Convert to dict {id: s_sum}

    # --- (جدید) دریافت آمار E ---
    e_point_value_str = db.get_setting('e_point_value', '0')
    e_point_value = Decimal(e_point_value_str)
    # (استفاده از تابع جدید دیتابیس)
    admin_e_stats = db.get_admin_e_stats_for_group(today_date, chat_id) # Dict {id: e_sum}

    if group_total == 0 and not admin_s_stats and not admin_e_stats:
        await event.reply(f"📊 آمار امروز ({today_date}) برای این گروه هنوز خالی است.")
        return

    try:
        chat_entity = await event.get_chat()
        chat_title = chat_entity.title
    except Exception:
        chat_title = f"گروه (ID: {chat_id})"

    msg = f"📊 **آمار لحظه‌ای امروز ({today_date})**\n"
    msg += f"**گروه: {chat_title}**\n\n"
    msg += f"💰 **مجموع فروش گروه (تومان):** `{group_total:,.0f}`\n"
    
    # --- (جدید) جمع‌آوری همه ادمین‌ها از هر دو آمار ---
    all_admin_ids = set(admin_s_stats.keys()) | set(admin_e_stats.keys())

    if not all_admin_ids:
        msg += "\nفعالیتی از ادمین‌ها ثبت نشده است."
    else:
        # --- دریافت نام ادمین‌ها ---
        tasks = [client.get_entity(admin_id) for admin_id in all_admin_ids]
        user_results = await asyncio.gather(*tasks, return_exceptions=True)
        user_map = {admin_id: (result.first_name if isinstance(result, User) else f"(کاربر {admin_id})") for admin_id, result in zip(all_admin_ids, user_results)}
        
        # --- (جدید) بخش آمار S ---
        msg += f"\n--- 👨‍💻 آمار ادمین‌ها (S Sum) ---\n"
        msg += f"(نرخ دستمزد: {salary_rate:,.0f} تومان)\n\n"
        
        s_stats_found = False
        sorted_s_admins = sorted(admin_s_stats.items(), key=lambda item: item[1], reverse=True)
        
        for admin_id, s_sum in sorted_s_admins:
            name = user_map.get(admin_id, f"(کاربر {admin_id})")
            admin_salary = s_sum * salary_rate
            msg += f"👤 **{name}** (ID: `{admin_id}`)\n"
            msg += f"   - مجموع S (Sum): **{s_sum}**\n"
            msg += f"   - دستمزد (تومان): `{admin_salary:,.0f}`\n\n"
            s_stats_found = True
            
        if not s_stats_found:
            msg += "فعالیتی (S) ثبت نشده است.\n\n"
        
        # --- (جدید) بخش آمار E ---
        msg += f"--- ⭐️ آمار ادمین‌ها (E امتیاز) ---\n"
        msg += f"(ارزش هر امتیاز: {e_point_value:,.0f} تومان)\n\n"
        
        e_stats_found = False
        sorted_e_admins = sorted(admin_e_stats.items(), key=lambda item: item[1], reverse=True)
        
        for admin_id, e_sum in sorted_e_admins:
            name = user_map.get(admin_id, f"(کاربر {admin_id})")
            e_value = e_sum * e_point_value
            msg += f"👤 **{name}** (ID: `{admin_id}`)\n"
            msg += f"   - مجموع E (امتیاز): **{e_sum}**\n"
            msg += f"   - ارزش (تومان): `{e_value:,.0f}`\n\n"
            e_stats_found = True
            
        if not e_stats_found:
            msg += "فعالیتی (E) ثبت نشده است.\n\n"

    await event.reply(msg)

# --- ⭐️ هندلر S (بدون تغییر) ⭐️ ---
@client.on(events.NewMessage(pattern=S_PATTERN))
async def s_message_handler(event):
    """
    هندلر اصلی پیام‌های s<number>
    """
    sender_id = event.sender_id
    chat_id = event.chat_id
    
    if chat_id not in active_groups_cache: return
    if sender_id not in bot_admins_cache: return
    if not event.is_group: return

    match = event.pattern_match
    s_key = match.group(0).lower()
    try:
        s_number = int(match.group(1))
    except ValueError: return
    if s_number == 0: return

    usdt_value = db.get_s_value(s_key)
    tether_price_str = db.get_setting('tether_price')
    
    if usdt_value is None:
        msg = f"⚠️ **خطا:** ارزش {s_key} تنظیم نشده است."
        await event.reply(msg + (f"\n(از /setvalue یا /panel استفاده کنید)" if sender_id == OWNER_ID else "\n(لطفا به مالک اطلاع دهید)"))
        return
    if tether_price_str is None:
        msg = "⚠️ **خطا:** نرخ تتر تنظیم نشده است."
        await event.reply(msg + (f"\n(از /settether یا /panel استفاده کنید)" if sender_id == OWNER_ID else "\n(لطفا به مالک اطلاع دهید)"))
        return
        
    tether_price = Decimal(tether_price_str)
    group_add_tomans = (usdt_value * Decimal(s_number)) * tether_price
    admin_s_sum_to_add = s_number
    today_date = db.get_today_date()
    
    try:
        db.update_group_stat(today_date, chat_id, group_add_tomans)
        db.update_admin_stat(today_date, chat_id, sender_id, admin_s_sum_to_add)
        db.update_s_key_stat(today_date, chat_id, sender_id, s_key, s_number)
        
    except Exception as e:
        logging.error(f"Failed to write stats to DB: {e}")
        await event.reply(f"❌ **خطای سیستمی:**\nهنگام ثبت در دیتابیس مشکلی پیش آمد.\n`{e}`")
        return

    try:
        reply_msg = (
            f"✅ **ثبت شد** (`{s_key}`)\n"
            f"• مبلغ افزوده شده به گروه: `{group_add_tomans:,.0f}` تومان\n"
            f"• S افزوده شده به شما: `{admin_s_sum_to_add}`"
        )
        await event.reply(reply_msg)
    except Exception as e:
        logging.warning(f"Failed to send reply confirmation: {e}")


# --- ⭐️ هندلر F (بدون تغییر) ⭐️ ---
@client.on(events.NewMessage(pattern=F_PATTERN))
async def f_message_handler(event):
    """
    هندلر پیام‌های f<number> (کاهشی S)
    """
    sender_id = event.sender_id
    chat_id = event.chat_id

    if chat_id not in active_groups_cache: return
    if sender_id not in bot_admins_cache: return
    if not event.is_group: return

    match = event.pattern_match
    f_key = match.group(0).lower() 
    s_key = 's' + match.group(1) 
    try:
        s_number_to_reduce = int(match.group(1))
    except ValueError: return
    if s_number_to_reduce == 0: return

    usdt_value = db.get_s_value(s_key)
    tether_price_str = db.get_setting('tether_price')
    today_date = db.get_today_date()
    
    if usdt_value is None:
        await event.reply(f"⚠️ **خطا:** ارزش `{s_key}` (مورد نیاز برای `{f_key}`) هنوز تنظیم نشده است. عملیات لغو شد.")
        return
    if tether_price_str is None:
        await event.reply(f"⚠️ **خطا:** نرخ تتر تنظیم نشده است. عملیات لغو شد.")
        return

    try:
        success = db.reduce_s_key_stat(today_date, chat_id, sender_id, s_key, s_number_to_reduce)
        
        if success:
            tether_price = Decimal(tether_price_str)
            toman_to_remove = (usdt_value * Decimal(s_number_to_reduce)) * tether_price
            
            db.update_group_stat(today_date, chat_id, -toman_to_remove)
            db.update_admin_stat(today_date, chat_id, sender_id, -s_number_to_reduce)
            
            await event.reply(
                f"✅ **کسر شد** (`{f_key}`)\n"
                f"• یک مورد `{s_key}` از آمار شما کسر شد.\n"
                f"• مبلغ کسر شده از گروه: `{toman_to_remove:,.0f}` تومان\n"
                f"• S کسر شده از شما: `{s_number_to_reduce}`"
            )
        else:
            available_keys = db.get_admin_available_s_keys(today_date, chat_id, sender_id)
            if not available_keys:
                await event.reply(f"❌ **خطا:** شما هیچ S ثبت‌شده‌ای برای امروز در این گروه ندارید که بتوانید `{f_key}` را ثبت کنید.")
            else:
                keys_str = ", ".join(f"`{k}`" for k in available_keys)
                await event.reply(f"❌ **خطا:** شما آمار `{s_key}` برای کسر کردن ندارید.\n\nS های موجود شما: {keys_str}")
                
    except Exception as e:
        logging.error(f"Failed to reduce stats with F command: {e}")
        await event.reply(f"❌ **خطای سیستمی:**\nهنگام کسر آمار مشکلی پیش آمد.\n`{e}`")


# --- ⭐️⭐️ (جدید) هندلرهای E و R (امتیاز) ⭐️⭐️ ---
@client.on(events.NewMessage(pattern=E_PATTERN))
async def e_message_handler(event):
    """
    هندلر پیام‌های e<number> (امتیاز افزایشی)
    """
    sender_id = event.sender_id
    chat_id = event.chat_id
    
    if chat_id not in active_groups_cache: return
    if sender_id not in bot_admins_cache: return
    if not event.is_group: return

    match = event.pattern_match
    e_key = match.group(0).lower()
    try:
        e_number = int(match.group(1))
    except ValueError: return
    if e_number == 0: return

    today_date = db.get_today_date()
    
    try:
        db.update_e_key_stat(today_date, chat_id, sender_id, e_key, e_number)
        
        reply_msg = (
            f"✅ **امتیاز ثبت شد** (`{e_key}`)\n"
            f"• امتیاز افزوده شده به شما: `{e_number}`"
        )
        await event.reply(reply_msg)
        
    except Exception as e:
        logging.error(f"Failed to write E stats to DB: {e}")
        await event.reply(f"❌ **خطای سیستمی:**\nهنگام ثبت امتیاز مشکلی پیش آمد.\n`{e}`")

@client.on(events.NewMessage(pattern=R_PATTERN))
async def r_message_handler(event):
    """
    هندلر پیام‌های r<number> (امتیاز کاهشی)
    """
    sender_id = event.sender_id
    chat_id = event.chat_id

    if chat_id not in active_groups_cache: return
    if sender_id not in bot_admins_cache: return
    if not event.is_group: return

    match = event.pattern_match
    r_key = match.group(0).lower() # r1
    e_key = 'e' + match.group(1) # e1
    try:
        e_number_to_reduce = int(match.group(1)) # 1
    except ValueError: return
    if e_number_to_reduce == 0: return

    today_date = db.get_today_date()
    
    try:
        success = db.reduce_e_key_stat(today_date, chat_id, sender_id, e_key, e_number_to_reduce)
        
        if success:
            await event.reply(
                f"✅ **امتیاز کسر شد** (`{r_key}`)\n"
                f"• یک مورد `{e_key}` از آمار شما کسر شد.\n"
                f"• امتیاز کسر شده از شما: `{e_number_to_reduce}`"
            )
        else:
            available_keys = db.get_admin_available_e_keys(today_date, chat_id, sender_id)
            if not available_keys:
                await event.reply(f"❌ **خطا:** شما هیچ امتیاز E ثبت‌شده‌ای برای امروز در این گروه ندارید که بتوانید `{r_key}` را ثبت کنید.")
            else:
                keys_str = ", ".join(f"`{k}`" for k in available_keys)
                await event.reply(f"❌ **خطا:** شما آمار `{e_key}` برای کسر کردن ندارید.\n\nE های موجود شما: {keys_str}")
                
    except Exception as e:
        logging.error(f"Failed to reduce stats with R command: {e}")
        await event.reply(f"❌ **خطای سیستمی:**\nهنگام کسر امتیاز مشکلی پیش آمد.\n`{e}`")


# --- ⭐️⭐️ (کاملاً بازنویسی شده) تابع گزارش‌دهی خودکار نیمه‌شب ⭐️⭐️ ---

async def send_daily_reports():
    """
    در ساعت 00:01 اجرا می‌شود.
    گزارش‌های گروهی را ارسال می‌کند.
    دیتابیس خارجی گروه را آپدیت می‌کند.
    گزارش‌های خلاصه را برای مالک ارسال می‌کند.
    
    (توجه: پرداخت حقوق ادمین‌ها دیگر در این ربات انجام نمی‌شود)
    """
    # ⭐️⭐️ (جدید) شناسه گروهی که باید مستثنی شود ⭐️⭐️
    GROUP_TO_EXCLUDE_ID = -1003176179034

    yesterday_date = db.get_yesterday_date()
    logging.info(f"Running daily reports for date: {yesterday_date}")
    
    # --- ۱. دریافت تمام تنظیمات نهایی روز گذشته ---
    tether_price = Decimal(db.get_setting('tether_price', '0'))
    salary_rate = Decimal(db.get_setting('salary_rate', '0')) # دستمزد S-Sum عادی
    
    # --- ۲. ارسال گزارش به گروه‌ها و آپدیت دیتابیس خارجی (گروه‌ها) ---
    active_groups_yesterday = db.get_active_groups_for_report(yesterday_date)
    logging.info(f"Found {len(active_groups_yesterday)} active groups for daily report.")
    
    group_stats_for_owner_report = []
    total_all_groups_income = Decimal('0')

    for chat_id in active_groups_yesterday:
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
            
            group_stats_for_owner_report.append((chat_title, chat_id, total_toman))
            
            # --- بخش آپدیت دیتابیس خارجی (گروه) ---
            total_usdt = total_toman / tether_price if tether_price > 0 else Decimal('0')
            int_total_toman = int(total_toman)
            formatted_toman = f"{int_total_toman:,.0f}"
            confirmation_msg = "" 
            
            # ⭐️⭐️ (تغییر) بررسی برای مستثنی کردن گروه ⭐️⭐️
            if chat_id == GROUP_TO_EXCLUDE_ID:
                logging.info(f"Skipping external DB update for excluded group: {chat_id} ({chat_title})")
                confirmation_msg = "\nℹ️ (این گروه از به‌روزرسانی حساب سراب مستثنی شده است)"
            else:
                # اگر گروه مستثنی نبود، آپدیت را انجام بده
                try:
                    update_success = await update_external_db_balance(chat_id, chat_title, int_total_toman)
                    if update_success:
                        confirmation_msg = f"\n✅ `{formatted_toman}` تومان به وب حساب ربات سراب اضافه شد."
                        logging.info(f"External DB update successful for {chat_id}.")
                    else:
                        confirmation_msg = f"\n⚠️ **خطا:** در به‌روزرسانی وب حساب ربات سراب مشکلی پیش آمد."
                        logging.error(f"External DB update FAILED for {chat_id}.")
                        await send_long_message(OWNER_ID, f"🚨 **هشدار عدم بروزرسانی حساب سراب (گروه)** 🚨\n\n"
                                                          f"گروه: {chat_title} (`{chat_id}`)\n"
                                                          f"مبلغ: `{formatted_toman}` تومان\n"
                                                          f"خطا در اتصال یا آپدیت دیتابیس `{EXTERNAL_DB_PATH}` رخ داد.")
                except Exception as e:
                    logging.error(f"Critical error during external DB update call: {e}")
                    confirmation_msg = f"\n⚠️ **خطای سیستمی:** در پردازش حساب سراب مشکلی پیش آمد."
                    await send_long_message(OWNER_ID, f"🚨 **خطای بحرانی در آپدیت حساب سراب (گروه)** 🚨\n"
                                                      f"گروه: {chat_title} (`{chat_id}`)\n"
                                                      f"خطا: {e}")

            # --- ساخت پیام گزارش گروه ---
            msg = f"📊 **خلاصه آمار روزانه** 📊\n"
            msg += f"🗓 **تاریخ:** `{yesterday_date}`\n"
            msg += f"🏠 **گروه:** {chat_title}\n\n"
            msg += f"💰 **جمع کل فروش (تومان):** `{total_toman:,.0f}`\n"
            msg += f"✳️ **جمع کل به دلار:** `{total_usdt:,.2f}` USDT\n\n"
            msg += "--- **جزئیات بازه‌ها (S)** ---\n"
            
            breakdown = db.get_group_s_key_breakdown(yesterday_date, chat_id)
            if not breakdown:
                msg += "آماری ثبت نشده است.\n"
            else:
                for s_key, s_count, s_sum in breakdown:
                    if s_count > 0 or s_sum > 0:
                        msg += f"• `{s_key}`: **{s_count}** عدد (مجموع S: **{s_sum}**)\n"
            
            msg += "\n--- **تنظیمات محاسبه (نهایی)** ---\n"
            msg += f"💵 **نرخ تتر:** `{tether_price:,.0f}` تومان"
            msg += confirmation_msg
            
            await send_long_message(chat_id, msg)
            await asyncio.sleep(1) 
            
        except (ChatAdminRequiredError, UserIsBlockedError):
            logging.warning(f"Bot access lost for group {chat_id}. Deactivating.")
            db.remove_active_group(chat_id)
            active_groups_cache.discard(chat_id)
            await send_long_message(OWNER_ID, f"⚠️ **خطا در ارسال گزارش**\nربات در گروه `{chat_id}` مسدود شده یا ادمین نیست. گروه به صورت خودکار غیرفعال شد.")
        except Exception as e:
            logging.error(f"Failed to send report to group {chat_id}: {e}")
            await send_long_message(OWNER_ID, f"⚠️ **خطا در ارسال گزارش به گروه {chat_id}**\n`{e}`")

    
    # --- ⭐️⭐️ ۳. (حذف شد) محاسبه و پرداخت ادمین‌ها ⭐️⭐️ ---
    # این بخش اکنون توسط adminsbot.py انجام می‌شود.
    logging.info("Admin payout calculations skipped, handled by adminsbot.")


    # --- ۴. ارسال گزارش‌های خلاصه به پی‌وی مالک ---
    
    # گزارش الف: آمار دستمزد (S Sum - برای اطلاع مالک)
    admin_salary_report_msg = f"🔔 **گزارش دستمزد ادمین‌ها (S Sum)**\n"
    admin_salary_report_msg += f"🗓 **تاریخ:** `{yesterday_date}`\n"
    admin_salary_report_msg += f"💰 **نرخ دستمزد هر S (Sum):** `{salary_rate:,.0f}` تومان\n\n"
    
    all_admin_salary_stats = db.get_all_admin_salary_stats(yesterday_date)
    total_all_salary = Decimal('0')
    
    if not all_admin_salary_stats:
        admin_salary_report_msg += "هیچ فعالیتی (S Sum) ثبت نشده است."
    else:
        admin_ids = [stat[0] for stat in all_admin_salary_stats]
        tasks = [client.get_entity(admin_id) for admin_id in admin_ids]
        user_results = await asyncio.gather(*tasks, return_exceptions=True)
        user_map = {admin_id: result for admin_id, result in zip(admin_ids, user_results) if isinstance(result, User)}
            
        valid_admin_stats = [stat for stat in all_admin_salary_stats if stat[1] > 0]
        sorted_admin_stats = sorted(valid_admin_stats, key=lambda x: x[1], reverse=True)
        
        if not sorted_admin_stats:
             admin_salary_report_msg += "هیچ فعالیتی (S Sum > 0) ثبت نشده است."
        
        for admin_id, total_s_sum in sorted_admin_stats:
            name = user_map.get(admin_id, User(id=admin_id, first_name=f"(کاربر {admin_id})")).first_name
            salary = total_s_sum * salary_rate
            total_all_salary += salary
            admin_salary_report_msg += f"👤 **{name}** (ID: `{admin_id}`)\n"
            admin_salary_report_msg += f"   - مجموع S (Sum): **{total_s_sum}**\n"
            admin_salary_report_msg += f"   - دستمزد (تومان): `{salary:,.0f}`\n\n"
    
    admin_salary_report_msg += f"--------------------\n"
    admin_salary_report_msg += f"💸 **جمع کل دستمزدها (S Sum):** `{total_all_salary:,.0f}` **تومان**"
    
    await send_long_message(OWNER_ID, admin_salary_report_msg)
    await asyncio.sleep(1) 

    # گزارش ب: آمار گروه‌ها
    group_stats_msg = f"🔔 **گزارش درآمد گروه‌ها**\n"
    group_stats_msg += f"🗓 **تاریخ:** `{yesterday_date}`\n\n"
    
    if not group_stats_for_owner_report:
        group_stats_msg += "هیچ فعالیتی در گروه‌ها ثبت نشده است."
    else:
        sorted_group_stats = sorted(group_stats_for_owner_report, key=lambda x: x[2], reverse=True)
        for chat_title, chat_id, total_toman in sorted_group_stats:
            group_stats_msg += f"🏠 **{chat_title}** (ID: `{chat_id}`)\n"
            group_stats_msg += f"   - درآمد (تومان): `{total_toman:,.0f}`\n\n"
    
    group_stats_msg += f"--------------------\n"
    group_stats_msg += f"💰 **جمع کل درآمد گروه‌ها:** `{total_all_groups_income:,.0f}` **تومان**\n"
    profit = total_all_groups_income - total_all_salary
    group_stats_msg += f"📈 **سود خالص (درآمد - دستمزد S Sum):** `{profit:,.0f}` **تومان**\n"
    group_stats_msg += f"(توجه: ربات adminsbot پرداخت‌های E و کمیسیون‌های ویژه را به صورت جداگانه مدیریت می‌کند)"
    
    await send_long_message(OWNER_ID, group_stats_msg)
    
    logging.info(f"Daily reports for {yesterday_date} sent successfully to owner.")


# --- تابع اصلی (Main) ---

async def main():
    """تابع اصلی برای اجرای ربات"""
    # ۱. بارگذاری ادمین‌ها، گروه‌ها و ادمین‌های ویژه
    await load_initial_data()
    
    # ۲. تنظیم زمان‌بند 
    # [تغییر] زمان‌بندی به 00:01 تغییر یافت تا قبل از adminsbot (00:05) اجرا شود
    scheduler.add_job(send_daily_reports, 'cron', hour=0, minute=10, second=0, timezone='Asia/Tehran')
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
