# fix_db.py
import sqlite3
import logging

DB_NAME = 'bot_stats_persistent.sqlite'
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

try:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    logging.info(f"Connected to database '{DB_NAME}'. Checking schema...")

    # 1. Check if 'rate' column exists in 'special_admins'
    cursor.execute("PRAGMA table_info(special_admins)")
    columns = [info[1] for info in cursor.fetchall()]

    if 'rate' not in columns:
        logging.warning("Column 'rate' not found in 'special_admins'. Adding it now...")
        # از آنجایی که جدول قبلی شما فقط user_id داشت، آن را حذف و دوباره می سازیم
        # این امن تر از ALTER TABLE در این مورد است
        cursor.execute("DROP TABLE IF EXISTS special_admins")
        logging.info("Dropped old 'special_admins' table.")
        cursor.execute('''
        CREATE TABLE special_admins (
            user_id INTEGER PRIMARY KEY,
            rate REAL DEFAULT 0
        )
        ''')
        logging.info("Re-created 'special_admins' table with 'rate' column.")
    else:
        logging.info("'special_admins' table is already up to date.")

    conn.commit()
    conn.close()

except Exception as e:
    logging.error(f"An unexpected error occurred: {e}")

print("\nDatabase check/fix complete. You can now run your main bot script.")
