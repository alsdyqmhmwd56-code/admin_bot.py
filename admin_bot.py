# -*- coding: utf-8 -*-
# admin_bot.py - البوت الإداري الذكي لإدارة حسابات Free Fire
# الإصدار النهائي - مع إضافة رمز الأمان (6 أرقام) وإصلاح مشكلة البحث

import asyncio
import sqlite3
import json
import re
import os
import zipfile
import shutil
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import warnings
from urllib.parse import urlparse, parse_qs

import aiohttp
import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =========================================================
# 📝 إعدادات التسجيل (Logging)
# =========================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=DeprecationWarning)

# =========================================================
# ⚙️ الإعدادات الأساسية
# =========================================================

ADMIN_BOT_TOKEN = "8846182269:AAHjuSwuE_O-YgTjui8uUQYYHcdAcpfnITo"
ADMIN_IDS = [8587386123, 8612276675]

JWT_API_URL = "https://zhogo-eat-to-jwt-2-zh-one.vercel.app/jwt"
CHANGE_BIO_API_URL = "https://zhogo-change-bio-api-2.vercel.app/changebio"

TOKEN_CHECK_INTERVAL_MINUTES = 5
DEFAULT_TIMEZONE = pytz.UTC
DB_NAME = "accounts.db"

NOTIFICATION_SCHEDULE = {
    1: None, 2: None,
    3: 5, 4: 5, 5: 5,
    6: 3, 7: 3, 8: 3
}

# =========================================================
# 🗄️ قاعدة البيانات - مع إضافة security_code
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            account_id TEXT NOT NULL,
            server TEXT DEFAULT 'ME',
            prime_level INTEGER DEFAULT 1,
            eat_token TEXT,
            photo_id TEXT,
            bound_email TEXT,
            pending_email TEXT,
            recovery_end_time TEXT,
            recovery_confirmed INTEGER DEFAULT 0,
            token_status TEXT DEFAULT 'MISSING',
            last_token_check TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_archived INTEGER DEFAULT 0,
            is_completed INTEGER DEFAULT 0,
            extra_data TEXT,
            has_pending INTEGER DEFAULT 0,
            security_code TEXT
        )
    """)
    
    columns_to_add = [
        ("prime_level", "INTEGER DEFAULT 1"),
        ("recovery_confirmed", "INTEGER DEFAULT 0"),
        ("token_status", "TEXT DEFAULT 'MISSING'"),
        ("last_token_check", "TEXT"),
        ("is_archived", "INTEGER DEFAULT 0"),
        ("is_completed", "INTEGER DEFAULT 0"),
        ("extra_data", "TEXT"),
        ("has_pending", "INTEGER DEFAULT 0"),
        ("security_code", "TEXT"),
    ]
    
    for col_name, col_type in columns_to_add:
        try:
            cursor.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            account_data TEXT,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'PENDING'
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            type TEXT,
            sent_at TEXT,
            next_send_at TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            action TEXT,
            details TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("✅ تم تهيئة قاعدة البيانات")

# =========================================================
# 🔗 دوال API
# =========================================================

def extract_eat_token(text: str) -> str:
    text = text.strip("`'\" \n\r\t")
    if text.startswith(('http://', 'https://')):
        parsed_url = urlparse(text)
        query_params = parse_qs(parsed_url.query)
        eat_list = query_params.get('eat')
        if eat_list:
            return eat_list[0]
    return text

_session: Optional[aiohttp.ClientSession] = None

async def get_session():
    global _session
    if _session is None:
        timeout = aiohttp.ClientTimeout(total=30)
        _session = aiohttp.ClientSession(timeout=timeout)
    return _session

async def check_token_validity(eat_token: str) -> Dict:
    try:
        clean_token = extract_eat_token(eat_token)
        if len(clean_token) < 15:
            return {"valid": False, "error": "توكن قصير جداً (أقل من 15 حرف)"}
        
        session = await get_session()
        jwt_url = f"{JWT_API_URL}?eat={clean_token}"
        
        async with session.get(jwt_url) as resp:
            if resp.status != 200:
                return {"valid": False, "error": f"فشل الاتصال بـ JWT API (رمز: {resp.status})"}
            jwt_response = await resp.json()
        
        if not jwt_response.get("success"):
            return {"valid": False, "error": jwt_response.get("message", "التوكن غير صالح")}
        
        jwt_token = jwt_response["data"].get("jwt_token")
        if not jwt_token:
            return {"valid": False, "error": "لم يتم استلام JWT من الخادم"}
        
        return {
            "valid": True,
            "account_id": jwt_response["data"].get("account_id", clean_token[:10]),
            "name": jwt_response["data"].get("nickname", "Unknown"),
            "server": jwt_response["data"].get("region", "ME"),
            "raw_data": jwt_response
        }
                    
    except Exception as e:
        return {"valid": False, "error": str(e)}

# =========================================================
# 📥 دوال التوكنات المنتظرة
# =========================================================

def add_pending_token(token: str, account_data: Dict = None) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO pending_tokens (token, account_data) VALUES (?, ?)",
        (token, json.dumps(account_data) if account_data else None)
    )
    conn.commit()
    token_id = cursor.lastrowid
    conn.close()
    return token_id

def get_pending_tokens() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, token, account_data, received_at, status FROM pending_tokens WHERE status = 'PENDING' ORDER BY received_at ASC"
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "token": row[1],
            "account_data": json.loads(row[2]) if row[2] else None,
            "received_at": row[3],
            "status": row[4]
        }
        for row in rows
    ]

def get_pending_token(token_id: int) -> Optional[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, token, account_data, received_at, status FROM pending_tokens WHERE id = ?",
        (token_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "token": row[1],
            "account_data": json.loads(row[2]) if row[2] else None,
            "received_at": row[3],
            "status": row[4]
        }
    return None

def mark_pending_processed(token_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE pending_tokens SET status = 'PROCESSED' WHERE id = ?", (token_id,))
    conn.commit()
    conn.close()

def clear_all_pending():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_tokens WHERE status = 'PENDING'")
    conn.commit()
    conn.close()

# =========================================================
# 📥 دوال الحسابات - مع security_code
# =========================================================

def add_account(
    name: str,
    account_id: str,
    server: str,
    prime_level: int,
    eat_token: str = None,
    photo_id: str = None,
    bound_email: str = None,
    pending_email: str = None,
    recovery_end_time: str = None,
    recovery_confirmed: int = 0,
    token_status: str = 'MISSING',
    has_pending: int = 0,
    security_code: str = None,
    extra_data: Dict = None
) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO accounts (
            name, account_id, server, prime_level, eat_token, photo_id,
            bound_email, pending_email, recovery_end_time, recovery_confirmed,
            token_status, extra_data, has_pending, security_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        name, account_id, server, prime_level, eat_token,
        photo_id, bound_email, pending_email, recovery_end_time,
        recovery_confirmed, token_status, json.dumps(extra_data) if extra_data else None,
        has_pending, security_code
    ))
    conn.commit()
    acc_id = cursor.lastrowid
    conn.close()
    add_log(acc_id, "ACCOUNT_ADDED", f"تم إضافة الحساب {name} (التوكن: {token_status}, has_pending: {has_pending})")
    return acc_id

def get_account(acc_id: int) -> Optional[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, account_id, server, prime_level, eat_token, photo_id,
               bound_email, pending_email, recovery_end_time, recovery_confirmed,
               token_status, last_token_check, created_at, updated_at,
               is_archived, is_completed, extra_data, has_pending, security_code
        FROM accounts WHERE id = ?
    """, (acc_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "eat_token": row[5] or "لم يتم إضافة توكن",
            "photo_id": row[6],
            "bound_email": row[7] or "لا توجد",
            "pending_email": row[8] or "لا توجد",
            "recovery_end_time": row[9],
            "recovery_confirmed": row[10],
            "token_status": row[11] or "MISSING",
            "last_token_check": row[12],
            "created_at": row[13],
            "updated_at": row[14],
            "is_archived": row[15],
            "is_completed": row[16],
            "extra_data": json.loads(row[17]) if row[17] else {},
            "has_pending": row[18] or 0,
            "security_code": row[19]
        }
    return None

def search_accounts_by_id(account_id: str) -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, account_id, server, prime_level, token_status, 
               recovery_confirmed, has_pending, pending_email, recovery_end_time,
               eat_token, is_archived, is_completed, security_code
        FROM accounts 
        WHERE account_id = ? AND is_archived = 0 AND is_completed = 0
    """, (account_id,))
    rows = cursor.fetchall()
    conn.close()
    
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "token_status": row[5] or "MISSING",
            "recovery_confirmed": row[6],
            "has_pending": row[7],
            "pending_email": row[8],
            "recovery_end_time": row[9],
            "eat_token": row[10],
            "is_archived": row[11],
            "is_completed": row[12],
            "security_code": row[13]
        }
        for row in rows
    ]

def replace_token_only(acc_id: int, new_token: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE accounts 
        SET eat_token = ?, token_status = 'VALID', updated_at = CURRENT_TIMESTAMP 
        WHERE id = ?
    """, (new_token, acc_id))
    conn.commit()
    conn.close()
    add_log(acc_id, "TOKEN_REPLACED", "تم استبدال التوكن القديم بآخر جديد")

def update_account_name(acc_id: int, new_name: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE accounts 
        SET name = ?, updated_at = CURRENT_TIMESTAMP 
        WHERE id = ?
    """, (new_name, acc_id))
    conn.commit()
    conn.close()
    add_log(acc_id, "NAME_UPDATED", f"تم تحديث الاسم إلى: {new_name}")

def update_recovery_only(acc_id: int, pending_email: str, recovery_end_time: str, security_code: str = None):
    """تحديث الاستعادة مع رمز الأمان"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if security_code:
        cursor.execute("""
            UPDATE accounts 
            SET pending_email = ?, recovery_end_time = ?, security_code = ?, updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        """, (pending_email, recovery_end_time, security_code, acc_id))
    else:
        cursor.execute("""
            UPDATE accounts 
            SET pending_email = ?, recovery_end_time = ?, updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        """, (pending_email, recovery_end_time, acc_id))
    conn.commit()
    conn.close()
    add_log(acc_id, "RECOVERY_UPDATED", f"تم تحديث الاستعادة: {pending_email}")

def get_confirmed_accounts_by_prime(prime_level: int) -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, account_id, server, prime_level, token_status, recovery_confirmed, recovery_end_time, is_archived, is_completed, pending_email, has_pending, security_code FROM accounts WHERE prime_level = ? AND recovery_confirmed = 1 AND is_archived = 0 AND is_completed = 0",
        (prime_level,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "token_status": row[5] or "MISSING",
            "recovery_confirmed": row[6],
            "recovery_end_time": row[7],
            "is_archived": row[8],
            "is_completed": row[9],
            "pending_email": row[10],
            "has_pending": row[11],
            "security_code": row[12]
        }
        for row in rows
    ]

def get_pending_accounts_by_prime(prime_level: int) -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, account_id, server, prime_level, token_status, recovery_confirmed, recovery_end_time, is_archived, is_completed, pending_email, has_pending, security_code FROM accounts WHERE prime_level = ? AND has_pending = 1 AND recovery_confirmed = 0 AND is_archived = 0 AND is_completed = 0",
        (prime_level,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "token_status": row[5] or "MISSING",
            "recovery_confirmed": row[6],
            "recovery_end_time": row[7],
            "is_archived": row[8],
            "is_completed": row[9],
            "pending_email": row[10],
            "has_pending": row[11],
            "security_code": row[12]
        }
        for row in rows
    ]

def get_unconfirmed_accounts_by_prime(prime_level: int) -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, account_id, server, prime_level, token_status, recovery_confirmed, recovery_end_time, is_archived, is_completed, pending_email, has_pending, security_code FROM accounts WHERE prime_level = ? AND has_pending = 0 AND recovery_confirmed = 0 AND is_archived = 0 AND is_completed = 0",
        (prime_level,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "token_status": row[5] or "MISSING",
            "recovery_confirmed": row[6],
            "recovery_end_time": row[7],
            "is_archived": row[8],
            "is_completed": row[9],
            "pending_email": row[10],
            "has_pending": row[11],
            "security_code": row[12]
        }
        for row in rows
    ]

def get_all_pending_accounts() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, account_id, server, prime_level, token_status, pending_email, recovery_end_time, is_archived, is_completed, has_pending, security_code FROM accounts WHERE has_pending = 1 AND recovery_confirmed = 0 AND is_archived = 0 AND is_completed = 0"
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "token_status": row[5] or "MISSING",
            "pending_email": row[6],
            "recovery_end_time": row[7],
            "is_archived": row[8],
            "is_completed": row[9],
            "has_pending": row[10],
            "security_code": row[11]
        }
        for row in rows
    ]

def get_all_unconfirmed_accounts() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, account_id, server, prime_level, token_status, pending_email, recovery_end_time, is_archived, is_completed, has_pending, security_code FROM accounts WHERE has_pending = 0 AND recovery_confirmed = 0 AND is_archived = 0 AND is_completed = 0"
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "token_status": row[5] or "MISSING",
            "pending_email": row[6],
            "recovery_end_time": row[7],
            "is_archived": row[8],
            "is_completed": row[9],
            "has_pending": row[10],
            "security_code": row[11]
        }
        for row in rows
    ]

def confirm_account_automatically(acc_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM accounts WHERE id = ?", (acc_id,))
    row = cursor.fetchone()
    name = row[0] if row else "غير معروف"
    
    cursor.execute("""
        UPDATE accounts 
        SET recovery_confirmed = 1, has_pending = 0, updated_at = CURRENT_TIMESTAMP 
        WHERE id = ?
    """, (acc_id,))
    conn.commit()
    conn.close()
    add_log(acc_id, "AUTO_CONFIRMED", f"تم نقل الحساب {name} تلقائياً إلى المؤكد بعد اكتمال الاستعادة")
    return name

def update_account_confirmed(acc_id: int, pending_email: str, recovery_end_time: str, security_code: str = None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if security_code:
        cursor.execute(
            "UPDATE accounts SET recovery_confirmed = 1, pending_email = ?, recovery_end_time = ?, has_pending = 0, security_code = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (pending_email, recovery_end_time, security_code, acc_id)
        )
    else:
        cursor.execute(
            "UPDATE accounts SET recovery_confirmed = 1, pending_email = ?, recovery_end_time = ?, has_pending = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (pending_email, recovery_end_time, acc_id)
        )
    conn.commit()
    conn.close()
    add_log(acc_id, "MOVED_TO_CONFIRMED", "تم نقل الحساب إلى المؤكد")

def search_accounts(query: str) -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, account_id, server, prime_level, token_status, recovery_confirmed, is_archived, is_completed, has_pending, security_code
        FROM accounts 
        WHERE (account_id LIKE ? OR name LIKE ? OR eat_token LIKE ? OR bound_email LIKE ? OR pending_email LIKE ? OR security_code LIKE ?)
        AND is_archived = 0 AND is_completed = 0
    """, (
        f"%{query}%", f"%{query}%", f"%{query}%",
        f"%{query}%", f"%{query}%", f"%{query}%"
    ))
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "token_status": row[5] or "MISSING",
            "recovery_confirmed": row[6],
            "is_archived": row[7],
            "is_completed": row[8],
            "has_pending": row[9],
            "security_code": row[10]
        }
        for row in rows
    ]

def update_account_token(acc_id: int, new_token: str, token_status: str = 'VALID'):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE accounts SET eat_token = ?, token_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_token, token_status, acc_id)
    )
    conn.commit()
    conn.close()
    add_log(acc_id, "TOKEN_UPDATED", f"تم تحديث التوكن إلى {token_status}")

def update_account_status(acc_id: int, token_status: str, check_time: str = None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if check_time:
        cursor.execute(
            "UPDATE accounts SET token_status = ?, last_token_check = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (token_status, check_time, acc_id)
        )
    else:
        cursor.execute(
            "UPDATE accounts SET token_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (token_status, acc_id)
        )
    conn.commit()
    conn.close()

def archive_account(acc_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE accounts SET is_archived = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (acc_id,)
    )
    conn.commit()
    conn.close()
    add_log(acc_id, "ACCOUNT_ARCHIVED", "تم نقل الحساب إلى سلة المهملات")

def delete_account_permanently(acc_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
    conn.commit()
    conn.close()
    add_log(acc_id, "ACCOUNT_DELETED", "تم حذف الحساب نهائياً")

def get_archived_accounts() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, account_id, server, prime_level, token_status, recovery_confirmed, is_completed, has_pending, security_code FROM accounts WHERE is_archived = 1"
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "token_status": row[5] or "MISSING",
            "recovery_confirmed": row[6],
            "is_completed": row[7],
            "has_pending": row[8],
            "security_code": row[9]
        }
        for row in rows
    ]

def restore_from_archive(acc_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE accounts SET is_archived = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (acc_id,)
    )
    conn.commit()
    conn.close()
    add_log(acc_id, "RESTORED_FROM_ARCHIVE", "تم استرجاع الحساب من سلة المهملات")

def get_all_accounts_for_monitoring() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, account_id, eat_token, token_status, last_token_check FROM accounts WHERE is_archived = 0 AND is_completed = 0 AND eat_token IS NOT NULL AND eat_token != 'لم يتم إضافة توكن'"
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "eat_token": row[3],
            "token_status": row[4] or "MISSING",
            "last_token_check": row[5]
        }
        for row in rows
    ]

# =========================================================
# 📝 دوال السجلات والإحصائيات
# =========================================================

def add_log(account_id: int, action: str, details: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO logs (account_id, action, details) VALUES (?, ?, ?)",
        (account_id, action, details)
    )
    conn.commit()
    conn.close()

def get_stats() -> Dict:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE is_archived = 0 AND is_completed = 0")
    total_active = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE token_status = 'VALID' AND is_archived = 0 AND is_completed = 0")
    valid_tokens = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE token_status = 'EXPIRED' AND is_archived = 0 AND is_completed = 0")
    expired_tokens = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE token_status = 'MISSING' AND is_archived = 0 AND is_completed = 0")
    missing_tokens = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE recovery_confirmed = 1 AND is_archived = 0 AND is_completed = 0")
    confirmed = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE has_pending = 1 AND recovery_confirmed = 0 AND is_archived = 0 AND is_completed = 0")
    pending = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE has_pending = 0 AND recovery_confirmed = 0 AND is_archived = 0 AND is_completed = 0")
    unconfirmed = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM accounts WHERE is_archived = 1")
    archived = cursor.fetchone()[0]
    
    prime_stats = {}
    for p in range(1, 9):
        cursor.execute(
            "SELECT COUNT(*) FROM accounts WHERE prime_level = ? AND is_archived = 0 AND is_completed = 0",
            (p,)
        )
        prime_stats[p] = cursor.fetchone()[0]
    
    conn.close()
    return {
        "total_active": total_active,
        "valid_tokens": valid_tokens,
        "expired_tokens": expired_tokens,
        "missing_tokens": missing_tokens,
        "confirmed": confirmed,
        "pending": pending,
        "unconfirmed": unconfirmed,
        "archived": archived,
        "prime_stats": prime_stats
    }

# =========================================================
# 🔍 دوال النسخ الاحتياطي
# =========================================================

def get_all_accounts_for_backup() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM accounts")
    rows = cursor.fetchall()
    conn.close()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_all_logs_for_backup() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM logs")
    rows = cursor.fetchall()
    conn.close()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_all_pending_for_backup() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, token, account_data, received_at, status FROM pending_tokens")
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "token": row[1],
            "account_data": row[2],
            "received_at": row[3],
            "status": row[4]
        }
        for row in rows
    ]

def restore_from_backup(backup_data: Dict) -> Dict:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM accounts")
    cursor.execute("DELETE FROM logs")
    cursor.execute("DELETE FROM pending_tokens")
    cursor.execute("DELETE FROM notifications")
    
    accounts_restored = 0
    for acc in backup_data.get('accounts', []):
        acc.pop('id', None)
        columns = ', '.join(acc.keys())
        placeholders = ', '.join(['?'] * len(acc))
        cursor.execute(f"INSERT INTO accounts ({columns}) VALUES ({placeholders})", list(acc.values()))
        accounts_restored += 1
    
    logs_restored = 0
    for log in backup_data.get('logs', []):
        log.pop('id', None)
        columns = ', '.join(log.keys())
        placeholders = ', '.join(['?'] * len(log))
        cursor.execute(f"INSERT INTO logs ({columns}) VALUES ({placeholders})", list(log.values()))
        logs_restored += 1
    
    pending_restored = 0
    for pending in backup_data.get('pending_tokens', []):
        pending.pop('id', None)
        columns = ', '.join(pending.keys())
        placeholders = ', '.join(['?'] * len(pending))
        cursor.execute(f"INSERT INTO pending_tokens ({columns}) VALUES ({placeholders})", list(pending.values()))
        pending_restored += 1
    
    conn.commit()
    conn.close()
    
    return {
        "accounts_restored": accounts_restored,
        "logs_restored": logs_restored,
        "pending_restored": pending_restored
    }

# =========================================================
# 🔍 دوال تحليل النصوص - مع استخراج رمز الأمان
# =========================================================

def parse_board_message(text: str) -> Dict:
    result = {
        "name": "غير معروف",
        "account_id": "غير معروف",
        "server": "ME"
    }
    
    name_patterns = [
        r"👤\s*الاسم:\s*(.+)",
        r"الاسم:\s*(.+)",
        r"Name:\s*(.+)",
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result["name"] = match.group(1).strip()
            break
    
    id_patterns = [
        r"🆔\s*(?:الأيدي|الآيدي|ID):\s*(\d+)",
        r"(?:الأيدي|الآيدي|ID):\s*(\d+)",
    ]
    for pattern in id_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result["account_id"] = match.group(1).strip()
            break
    
    server_patterns = [
        r"🌍\s*(?:السيرفر|Server):\s*(\w+)",
        r"(?:السيرفر|Server):\s*(\w+)",
    ]
    for pattern in server_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result["server"] = match.group(1).strip().upper()
            break
    
    return result

def parse_recovery_message(text: str) -> Dict:
    """تحليل رسالة الاستعادة مع استخراج رمز الأمان (6 أرقام)"""
    result = {
        "bound_email": "لا توجد",
        "pending_email": "لا توجد",
        "recovery_end_time": None,
        "has_pending": 0,
        "recovery_confirmed": 0,
        "security_code": None
    }
    
    bound_patterns = [
        r"📧\s*(?:الاستعادة المربوطة|Bound Email):\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
        r"(?:الاستعادة المربوطة|Bound Email):\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
    ]
    for pattern in bound_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result["bound_email"] = match.group(1)
            break
    
    pending_patterns = [
        r"⏳\s*(?:استعادة قيد التأكيد|Pending Email):\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
        r"(?:استعادة قيد التأكيد|Pending Email):\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
    ]
    for pattern in pending_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result["pending_email"] = match.group(1)
            break
    
    # ✅ استخراج رمز الأمان (6 أرقام)
    security_patterns = [
        r"رمز الأمان[:\s]+(\d{6})",
        r"Security Code[:\s]+(\d{6})",
        r"كود التأكيد[:\s]+(\d{6})",
        r"(\d{6})",
    ]
    for pattern in security_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            code = match.group(1)
            if len(code) == 6 and code.isdigit():
                result["security_code"] = code
                logger.info(f"🔐 تم استخراج رمز الأمان: {code}")
            break
    
    if result["bound_email"] == "لا توجد" and result["pending_email"] == "لا توجد":
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
        if len(emails) >= 2:
            result["bound_email"] = emails[0]
            result["pending_email"] = emails[1]
        elif len(emails) == 1:
            result["pending_email"] = emails[0]
    
    time_patterns = [
        r'(\d+)\s*(?:يوم|أيام)',
        r'(\d+)\s*(?:ساعة|ساعات)',
        r'(\d+)\s*(?:دقيقة|دقائق)',
    ]
    for pattern in time_patterns:
        match = re.search(pattern, text)
        if match:
            value = int(match.group(1))
            if 'يوم' in pattern or 'أيام' in pattern:
                result["recovery_end_time"] = (datetime.now() + timedelta(days=value)).isoformat()
            elif 'ساعة' in pattern or 'ساعات' in pattern:
                result["recovery_end_time"] = (datetime.now() + timedelta(hours=value)).isoformat()
            elif 'دقيقة' in pattern or 'دقائق' in pattern:
                result["recovery_end_time"] = (datetime.now() + timedelta(minutes=value)).isoformat()
            break
    
    if result["pending_email"] and result["pending_email"] != "لا توجد":
        result["has_pending"] = 1
        result["recovery_confirmed"] = 0
    else:
        result["has_pending"] = 0
        result["recovery_confirmed"] = 0
    
    return result

# =========================================================
# ⏱️ دوال مساعدة
# =========================================================

def format_time_remaining(end_time_str: str) -> str:
    if not end_time_str:
        return "لا توجد استعادة"
    
    try:
        end_time = datetime.fromisoformat(end_time_str)
        now = datetime.now()
        diff = end_time - now
        
        if diff.total_seconds() <= 0:
            return "مكتملة ✅"
        
        days = diff.days
        hours, remainder = divmod(diff.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days} يوم")
        if hours > 0:
            parts.append(f"{hours} ساعة")
        if minutes > 0:
            parts.append(f"{minutes} دقيقة")
        if seconds > 0 and days == 0:
            parts.append(f"{seconds} ثانية")
        
        return " و ".join(parts) if parts else "أقل من دقيقة"
    except Exception:
        return "غير محدد"

def get_prime_emoji(prime: int) -> str:
    emojis = {
        1: "🥉", 2: "🥉", 3: "🥉",
        4: "🥈", 5: "🥈",
        6: "🏆", 7: "👑", 8: "💠"
    }
    return emojis.get(prime, "⭐")

def time_since(timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(timestamp)
        diff = datetime.now() - dt
        minutes = int(diff.total_seconds() / 60)
        
        if minutes < 1:
            return "الآن"
        elif minutes < 60:
            return f"{minutes} دقيقة"
        elif minutes < 1440:
            return f"{minutes // 60} ساعة"
        else:
            return f"{minutes // 1440} يوم"
    except Exception:
        return "غير معروف"

def get_token_status_icon(status: str) -> str:
    if status == "VALID":
        return "🟢"
    elif status == "EXPIRED":
        return "🔴"
    elif status == "MISSING":
        return "❌"
    else:
        return "⚪"

def get_token_status_text(status: str) -> str:
    if status == "VALID":
        return "صالح ✅"
    elif status == "EXPIRED":
        return "محروق 🔴"
    elif status == "MISSING":
        return "لم يتم إضافة توكن ❌"
    else:
        return "غير معروف ⚪"

def get_account_status_icon(has_pending: int, recovery_confirmed: int) -> str:
    if recovery_confirmed == 1:
        return "✅"
    elif has_pending == 1:
        return "⏳"
    else:
        return "❌"

def get_account_status_text(has_pending: int, recovery_confirmed: int) -> str:
    if recovery_confirmed == 1:
        return "مؤكد ✅"
    elif has_pending == 1:
        return "منتظر ⏳"
    else:
        return "غير مؤكد ❌"

def is_eat_token(text: str) -> bool:
    clean = text.strip()
    if len(clean) < 50:
        return False
    if re.search(r'[^a-zA-Z0-9]', clean):
        return False
    return True

# =========================================================
# 📱 القوائم والأزرار
# =========================================================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 الحسابات المؤكدة", callback_data="menu_confirmed")],
        [InlineKeyboardButton("⏳ الحسابات المنتظرة", callback_data="menu_pending")],
        [InlineKeyboardButton("🔐 الحسابات غير المؤكدة", callback_data="menu_unconfirmed")],
        [InlineKeyboardButton("➕ إضافة حساب يدوي", callback_data="add_manual")],
        [
            InlineKeyboardButton("📊 إحصائيات", callback_data="show_stats"),
            InlineKeyboardButton("🔍 بحث", callback_data="search_menu")
        ],
        [
            InlineKeyboardButton("🗑️ سلة المهملات", callback_data="show_archive"),
            InlineKeyboardButton("💾 نسخ احتياطي", callback_data="create_backup")
        ],
        [InlineKeyboardButton("🔄 استرداد نسخة", callback_data="restore_backup")]
    ])

def get_prime_keyboard():
    keyboard = []
    for p in range(1, 9):
        emoji = get_prime_emoji(p)
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} Prime {p}",
                callback_data=f"set_prime_{p}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_token_skip_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ تخطي التوكن (إضافة لاحقاً)", callback_data="skip_token")],
        [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
    ])

# =========================================================
# 🎮 معالجات البوت
# =========================================================

user_steps = {}

# =========================================================
# 📋 دالة مساعدة لتعديل الرسائل بأمان
# =========================================================

async def safe_edit_or_send(query, text, reply_markup=None, parse_mode="Markdown"):
    try:
        if query.message.text:
            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await query.message.delete()
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"❌ خطأ في safe_edit_or_send: {e}")
        try:
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e2:
            logger.error(f"❌ خطأ في الإرسال البديل: {e2}")

# =========================================================
# 📋 دوال حفظ الحساب - مع security_code
# =========================================================

async def save_manual_account_from_context(update_or_query, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    data = context.user_data.get('manual_add_data', {})
    
    if not data.get('name') or not data.get('account_id'):
        if hasattr(update_or_query, 'message'):
            await update_or_query.message.reply_text("❌ بيانات الحساب غير مكتملة.", reply_markup=main_menu_keyboard())
        else:
            await update_or_query.message.edit_text("❌ بيانات الحساب غير مكتملة.", reply_markup=main_menu_keyboard())
        return
    
    name = data.get('name', 'غير معروف')
    account_id = data.get('account_id', 'غير معروف')
    server = data.get('server', 'ME')
    prime_level = data.get('prime_level', 1)
    token = data.get('token')
    token_status = data.get('token_status', 'MISSING')
    photo_id = data.get('photo_id')
    bound_email = data.get('bound_email', 'لا توجد')
    pending_email = data.get('pending_email', 'لا توجد')
    recovery_end_time = data.get('recovery_end_time')
    has_pending = data.get('has_pending', 0)
    recovery_confirmed = data.get('recovery_confirmed', 0)
    security_code = data.get('security_code')
    
    if pending_email and pending_email != 'لا توجد':
        has_pending = 1
        recovery_confirmed = 0
    else:
        has_pending = 0
        recovery_confirmed = 0
    
    add_account(
        name=name,
        account_id=account_id,
        server=server,
        prime_level=prime_level,
        eat_token=token,
        photo_id=photo_id,
        bound_email=bound_email,
        pending_email=pending_email,
        recovery_end_time=recovery_end_time,
        recovery_confirmed=recovery_confirmed,
        token_status=token_status,
        has_pending=has_pending,
        security_code=security_code
    )
    
    context.user_data.pop('manual_add_data', None)
    context.user_data.pop('manual_add_step', None)
    user_steps.pop(user_id, None)
    
    token_text = "✅ صالح" if token_status == "VALID" else "❌ لم يتم إضافة توكن"
    status_text = get_account_status_text(has_pending, recovery_confirmed)
    code_text = f"\n🔐 رمز الأمان: `{security_code}`" if security_code else ""
    
    success_msg = (
        f"🎉 **تم إضافة الحساب يدوياً!**\n\n"
        f"👤 الاسم: {name}\n"
        f"🆔 ID: {account_id}\n"
        f"🏆 Prime: {prime_level} {get_prime_emoji(prime_level)}\n"
        f"🔑 التوكن: {token_text}\n"
        f"📧 البريد الجديد: {pending_email}\n"
        f"🔐 الحالة: {status_text}{code_text}"
    )
    
    if hasattr(update_or_query, 'message'):
        await update_or_query.message.reply_text(success_msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    else:
        await update_or_query.message.edit_text(success_msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

async def finish_account_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    account_data = context.user_data.get('account_data', {})
    recovery_data = context.user_data.get('recovery_data', {})
    token_data = context.user_data.get('processing_token', {})
    
    name = account_data.get('name', 'غير معروف')
    account_id = account_data.get('account_id', 'غير معروف')
    server = account_data.get('server', 'ME')
    prime_level = account_data.get('prime_level', 1)
    photo_id = account_data.get('photo_id')
    token = token_data.get('token', account_data.get('token'))
    token_status = account_data.get('token_status', 'VALID' if token else 'MISSING')
    bound_email = recovery_data.get('bound_email', 'لا توجد')
    pending_email = recovery_data.get('pending_email', 'لا توجد')
    recovery_end_time = recovery_data.get('recovery_end_time')
    has_pending = recovery_data.get('has_pending', 0)
    recovery_confirmed = recovery_data.get('recovery_confirmed', 0)
    security_code = recovery_data.get('security_code')
    
    if pending_email and pending_email != 'لا توجد':
        has_pending = 1
        recovery_confirmed = 0
    else:
        has_pending = 0
        recovery_confirmed = 0
    
    add_account(
        name=name,
        account_id=account_id,
        server=server,
        prime_level=prime_level,
        eat_token=token,
        photo_id=photo_id,
        bound_email=bound_email,
        pending_email=pending_email,
        recovery_end_time=recovery_end_time,
        recovery_confirmed=recovery_confirmed,
        token_status=token_status,
        has_pending=has_pending,
        security_code=security_code
    )
    
    if 'processing_token_id' in context.user_data:
        mark_pending_processed(context.user_data['processing_token_id'])
    
    context.user_data.pop('processing_token', None)
    context.user_data.pop('processing_token_id', None)
    context.user_data.pop('account_data', None)
    context.user_data.pop('recovery_data', None)
    context.user_data.pop('recovery_path', None)
    user_steps.pop(user_id, None)
    
    status_text = get_account_status_text(has_pending, recovery_confirmed)
    token_text = "✅ صالح" if token_status == "VALID" else "❌ لم يتم إضافة توكن" if token_status == "MISSING" else "❌ غير صالح"
    code_text = f"\n🔐 رمز الأمان: `{security_code}`" if security_code else ""
    
    await update.message.reply_text(
        f"🎉 **تم إضافة الحساب بنجاح!**\n\n"
        f"👤 الاسم: {name}\n"
        f"🆔 ID: {account_id}\n"
        f"🏆 Prime: {prime_level} {get_prime_emoji(prime_level)}\n"
        f"🔑 التوكن: {token_text}\n"
        f"📧 البريد الجديد: {pending_email}\n"
        f"🔐 الحالة: {status_text}{code_text}",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

# =========================================================
# 📋 دوال معالجة الصور
# =========================================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    photo_file = update.message.photo[-1].file_id
    
    if context.user_data.get('manual_add_step') == "manual_photo":
        context.user_data['manual_add_data']['photo_id'] = photo_file
        await save_manual_account_from_context(update, context, user_id)
        return
    
    elif context.user_data.get('step') == "waiting_photo":
        context.user_data['account_data']['photo_id'] = photo_file
        await finish_account_creation(update, context)
        return
    
    elif user_id in user_steps and user_steps[user_id].get("step") == "manual_photo":
        step_data = user_steps[user_id]
        step_data["photo_id"] = photo_file
        context.user_data['manual_add_data'] = step_data
        context.user_data['manual_add_step'] = "manual_photo"
        await save_manual_account_from_context(update, context, user_id)
        return
    
    await update.message.reply_text("❌ لا توجد عملية جارية للصورة.")

# =========================================================
# 📋 دوال معالجة الأزرار
# =========================================================

async def set_account_prime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    prime_level = int(query.data.split("_")[2])
    
    if context.user_data.get('manual_add_step') == "manual_prime":
        context.user_data['manual_add_data']['prime_level'] = prime_level
        context.user_data['manual_add_step'] = "manual_photo"
        
        await query.message.edit_text(
            f"✅ **Prime {prime_level} {get_prime_emoji(prime_level)}**\n\n"
            f"🖼️ **أرسل صورة الحساب (اختياري)**\n"
            f"أو اضغط على زر التخطي",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ تخطي الصورة", callback_data="skip_manual_photo")],
                [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    elif user_id in user_steps and user_steps[user_id].get("step") == "manual_prime":
        step_data = user_steps[user_id]
        step_data["prime_level"] = prime_level
        step_data["step"] = "manual_photo"
        context.user_data['manual_add_data'] = step_data
        context.user_data['manual_add_step'] = "manual_photo"
        
        await query.message.edit_text(
            f"✅ **Prime {prime_level} {get_prime_emoji(prime_level)}**\n\n"
            f"🖼️ **أرسل صورة الحساب (اختياري)**\n"
            f"أو اضغط على زر التخطي",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ تخطي الصورة", callback_data="skip_manual_photo")],
                [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    elif context.user_data.get('step') == "waiting_prime":
        context.user_data['account_data']['prime_level'] = prime_level
        context.user_data['step'] = "waiting_photo"
        
        await query.message.edit_text(
            f"✅ **Prime {prime_level} {get_prime_emoji(prime_level)}**\n\n"
            f"🖼️ **أرسل صورة الحساب (اختياري)**\n"
            f"أو اضغط على زر التخطي",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ تخطي الصورة", callback_data="skip_photo")],
                [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    else:
        await query.message.edit_text(
            f"❌ **خطأ:** لا توجد عملية إضافة جارية.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )

async def handle_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if context.user_data.get('manual_add_step') == "manual_photo":
        context.user_data['manual_add_data']['photo_id'] = None
        await save_manual_account_from_context(query, context, user_id)
        return
    
    elif user_id in user_steps and user_steps[user_id].get("step") == "manual_photo":
        step_data = user_steps[user_id]
        step_data["photo_id"] = None
        context.user_data['manual_add_data'] = step_data
        context.user_data['manual_add_step'] = "manual_photo"
        await save_manual_account_from_context(query, context, user_id)
        return
    
    elif context.user_data.get('step') == "waiting_photo":
        context.user_data['account_data']['photo_id'] = None
        await finish_account_creation(update, context)
        return
    
    await query.message.edit_text("❌ لا توجد عملية جارية.")

async def start_manual_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    context.user_data['manual_add_data'] = {}
    context.user_data['manual_add_step'] = "manual_board"
    
    await query.message.edit_text(
        "📋 **أرسل لوحة الإدارة:**\n\nمثال:\n⚙️ لوحة الإدارة:\n👤 الاسم: sløㅤㅤ\n🆔 الأيدي: 985922586\n🌍 السيرفر: ME",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

async def handle_manual_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    
    if 'manual_add_data' not in context.user_data:
        await update.message.reply_text("❌ لا توجد عملية جارية.")
        return
    
    step = context.user_data.get('manual_add_step', 'manual_board')
    data = context.user_data['manual_add_data']
    text = update.message.text.strip()
    
    if step == "manual_board":
        parsed = parse_board_message(text)
        data["name"] = parsed["name"]
        data["account_id"] = parsed["account_id"]
        data["server"] = parsed["server"]
        context.user_data['manual_add_step'] = "manual_token"
        
        await update.message.reply_text(
            f"✅ **تم تحليل اللوحة:**\n\n👤 الاسم: {data['name']}\n🆔 ID: {data['account_id']}\n🌍 السيرفر: {data['server']}\n\n🔑 **أرسل التوكن (سيتم فحصه فوراً)**\nأو اضغط على 'تخطي التوكن' للإضافة بدون توكن:",
            reply_markup=get_token_skip_keyboard(),
            parse_mode="Markdown"
        )
    
    elif step == "manual_token":
        clean_token = extract_eat_token(text)
        data["token"] = clean_token
        await update.message.reply_text("⏳ **جاري فحص التوكن عبر API...**", parse_mode="Markdown")
        result = await check_token_validity(clean_token)
        
        if result.get('valid'):
            data["token_status"] = "VALID"
            await update.message.reply_text(
                f"✅ **التوكن صالح!**\n\n📩 **أرسل الآن رسالة الاستعادة:**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
                ])
            )
            context.user_data['manual_add_step'] = "manual_recovery"
        else:
            await update.message.reply_text(
                f"❌ **التوكن غير صالح!**\n\nالسبب: {result.get('error', 'غير معروف')}\n\n⚠️ يمكنك:\n• إرسال توكن صحيح\n• أو تخطي التوكن وإضافته لاحقاً",
                reply_markup=get_token_skip_keyboard()
            )
    
    elif step == "manual_recovery":
        parsed = parse_recovery_message(text)
        data["bound_email"] = parsed.get('bound_email', 'لا توجد')
        data["pending_email"] = parsed.get('pending_email', 'لا توجد')
        data["recovery_end_time"] = parsed.get('recovery_end_time')
        data["has_pending"] = parsed.get('has_pending', 0)
        data["recovery_confirmed"] = parsed.get('recovery_confirmed', 0)
        data["security_code"] = parsed.get('security_code')
        
        # ✅ إذا كان هناك pending_email وليس هناك رمز أمان، نطلبه
        if data["has_pending"] == 1 and not data.get("security_code"):
            context.user_data['manual_add_step'] = "waiting_security_code"
            await update.message.reply_text(
                f"✅ **تم تحليل الاستعادة:**\n\n"
                f"📧 البريد الحالي: {data['bound_email']}\n"
                f"📧 البريد الجديد: {data['pending_email']}\n"
                f"⏱ المدة: {format_time_remaining(data['recovery_end_time'])}\n"
                f"🔐 الحالة: ⏳ منتظرة\n\n"
                f"🔑 **أرسل رمز الأمان (6 أرقام):**",
                parse_mode="Markdown"
            )
            return
        
        context.user_data['manual_add_step'] = "manual_prime"
        status_text = "⏳ منتظرة" if data["has_pending"] == 1 else "❌ غير مؤكدة"
        code_text = f"\n🔐 رمز الأمان: `{data['security_code']}`" if data.get("security_code") else ""
        
        await update.message.reply_text(
            f"✅ **تم تحليل الاستعادة:**\n\n"
            f"📧 البريد الحالي: {data['bound_email']}\n"
            f"📧 البريد الجديد: {data['pending_email']}\n"
            f"⏱ المدة: {format_time_remaining(data['recovery_end_time'])}\n"
            f"🔐 الحالة: {status_text}{code_text}\n\n"
            f"🏆 **اختر Prime:**",
            reply_markup=get_prime_keyboard(),
            parse_mode="Markdown"
        )

async def handle_security_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج رمز الأمان"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    
    step_data = user_steps[user_id]
    if step_data.get("step") != "waiting_security_code":
        return
    
    code = update.message.text.strip()
    
    # ✅ التحقق من أن الرمز هو 6 أرقام
    if not re.match(r'^\d{6}$', code):
        await update.message.reply_text(
            "❌ **رمز الأمان غير صحيح!**\n\n"
            "يجب أن يكون رمز الأمان **6 أرقام** بالضبط.\n"
            "مثال: `123456`\n\n"
            "🔑 **أرسل رمز الأمان الصحيح (6 أرقام):**",
            parse_mode="Markdown"
        )
        return
    
    # ✅ حفظ رمز الأمان
    data = context.user_data.get('manual_add_data', {})
    data["security_code"] = code
    context.user_data['manual_add_data'] = data
    
    # ✅ الانتقال إلى خطوة Prime
    step_data["step"] = "manual_prime"
    
    status_text = "⏳ منتظرة" if data.get("has_pending") == 1 else "❌ غير مؤكدة"
    await update.message.reply_text(
        f"✅ **تم حفظ رمز الأمان: `{code}`**\n\n"
        f"📧 البريد الحالي: {data.get('bound_email', 'لا توجد')}\n"
        f"📧 البريد الجديد: {data.get('pending_email', 'لا توجد')}\n"
        f"⏱ المدة: {format_time_remaining(data.get('recovery_end_time'))}\n"
        f"🔐 الحالة: {status_text}\n\n"
        f"🏆 **اختر Prime:**",
        reply_markup=get_prime_keyboard(),
        parse_mode="Markdown"
    )

async def handle_skip_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if context.user_data.get('manual_add_step') in ["manual_token", "waiting_token"]:
        context.user_data['manual_add_data']['token'] = None
        context.user_data['manual_add_data']['token_status'] = "MISSING"
        
        if context.user_data.get('manual_add_step') == "manual_token":
            await query.message.edit_text(
                "⏭️ **تم تخطي التوكن**\n\nسيتم إضافة الحساب بدون توكن.\nيمكنك إضافة التوكن لاحقاً من صفحة الحساب.\n\n📩 **أرسل الآن رسالة الاستعادة:**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
                ])
            )
            context.user_data['manual_add_step'] = "manual_recovery"
        return
    
    elif context.user_data.get('step') == "waiting_token":
        context.user_data['account_data']['token'] = None
        context.user_data['account_data']['token_status'] = "MISSING"
        await query.message.edit_text(
            "⏭️ **تم تخطي التوكن**\n\nسيتم إضافة الحساب بدون توكن.\nيمكنك إضافة التوكن لاحقاً من صفحة الحساب.\n\n📩 **أرسل الآن رسالة الاستعادة:**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
            ])
        )
        context.user_data['step'] = "waiting_recovery"
        return
    
    elif user_id in user_steps and user_steps[user_id].get("step") in ["manual_token", "waiting_token"]:
        step_data = user_steps[user_id]
        step_data["token"] = None
        step_data["token_status"] = "MISSING"
        if step_data.get("step") == "manual_token":
            await query.message.edit_text(
                "⏭️ **تم تخطي التوكن**\n\nسيتم إضافة الحساب بدون توكن.\nيمكنك إضافة التوكن لاحقاً من صفحة الحساب.\n\n📩 **أرسل الآن رسالة الاستعادة:**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
                ])
            )
            step_data["step"] = "manual_recovery"
        elif step_data.get("step") == "waiting_token":
            context.user_data['account_data']['token'] = None
            context.user_data['account_data']['token_status'] = "MISSING"
            await query.message.edit_text(
                "⏭️ **تم تخطي التوكن**\n\nسيتم إضافة الحساب بدون توكن.\nيمكنك إضافة التوكن لاحقاً من صفحة الحساب.\n\n📩 **أرسل الآن رسالة الاستعادة:**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
                ])
            )
            step_data["step"] = "waiting_recovery"
        return
    
    await query.message.edit_text(
        "❌ لا يمكن تخطي التوكن في هذه المرحلة.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
        ])
    )

# =========================================================
# 📋 الأوامر الرئيسية
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ عذراً، هذا البوت خاص بالأدمن فقط.")
        return
    
    await update.message.reply_text(
        "👋 **مرحباً في بوت إدارة حسابات Free Fire**\n\n"
        "📋 استخدم القائمة أدناه للتحكم في الحسابات.\n\n"
        "💡 **ملاحظات:**\n"
        "• **📋 مؤكدة:** حسابات تم تأكيد استعادتها\n"
        "• **⏳ منتظرة:** حسابات في استعادة قيد التأكيد\n"
        "• **🔐 غير مؤكدة:** حسابات بدون استعادة قيد التأكيد\n"
        "• **🧠 نظام ذكي:** أرسل توكن وسيطلب الاسم\n"
        "• **🔐 رمز الأمان:** 6 أرقام للحسابات المنتظرة\n"
        "• المراقبة التلقائية كل 5 دقائق\n"
        "• Prime 3-5 تنبيه كل 5 أيام\n"
        "• Prime 6-8 تنبيه كل 3 أيام",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ هذا الأمر للأدمن فقط.")
        return
    await create_backup(update, context)

async def safe_show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="📋 **القائمة الرئيسية:**",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

# =========================================================
# 📋 دوال عرض الحسابات
# =========================================================

async def show_confirmed_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = []
    for p in range(1, 9):
        accounts = get_confirmed_accounts_by_prime(p)
        count = len(accounts)
        emoji = get_prime_emoji(p)
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} Prime {p} ({count})",
                callback_data=f"view_confirmed_{p}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    await query.message.edit_text(
        "📋 **الحسابات المؤكدة**\n\nاختر Prime لعرض الحسابات المؤكدة:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_pending_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = []
    for p in range(1, 9):
        accounts = get_pending_accounts_by_prime(p)
        count = len(accounts)
        emoji = get_prime_emoji(p)
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} Prime {p} ({count})",
                callback_data=f"view_pending_{p}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    await query.message.edit_text(
        "⏳ **الحسابات المنتظرة**\n\nاختر Prime لعرض الحسابات التي في استعادة قيد التأكيد:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_unconfirmed_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = []
    for p in range(1, 9):
        accounts = get_unconfirmed_accounts_by_prime(p)
        count = len(accounts)
        emoji = get_prime_emoji(p)
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} Prime {p} ({count})",
                callback_data=f"view_unconfirmed_{p}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    await query.message.edit_text(
        "🔐 **الحسابات غير المؤكدة**\n\nاختر Prime لعرض الحسابات التي لا يوجد استعادة قيد التأكيد:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def view_accounts_filtered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    parts = data.split("_")
    filter_type = parts[1]
    prime = int(parts[2])
    
    if filter_type == "confirmed":
        accounts = get_confirmed_accounts_by_prime(prime)
        title = f"✅ Prime {prime} - مؤكدة"
        back_callback = "menu_confirmed"
    elif filter_type == "pending":
        accounts = get_pending_accounts_by_prime(prime)
        title = f"⏳ Prime {prime} - منتظرة"
        back_callback = "menu_pending"
    else:
        accounts = get_unconfirmed_accounts_by_prime(prime)
        title = f"❌ Prime {prime} - غير مؤكدة"
        back_callback = "menu_unconfirmed"
    
    if not accounts:
        await query.message.edit_text(
            f"📭 **لا توجد حسابات في {title}**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data=back_callback)]
            ]),
            parse_mode="Markdown"
        )
        return
    
    keyboard = []
    for acc in accounts:
        status_icon = get_token_status_icon(acc['token_status'])
        time_left = format_time_remaining(acc.get('recovery_end_time'))
        btn_text = f"{status_icon} {acc['name']} | {acc['account_id']}"
        if filter_type == "pending":
            btn_text += f" | ⏳ {time_left}"
            if acc.get('security_code'):
                btn_text += f" | 🔐 {acc['security_code'][:3]}***"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_acc_{acc['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=back_callback)])
    await query.message.edit_text(
        f"📁 **{title}** ({len(accounts)})",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# =========================================================
# 📋 دوال التوكنات المنتظرة
# =========================================================

async def show_pending_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_pending_menu(update, context)

async def process_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    token_id = int(query.data.split("_")[2])
    token_data = get_pending_token(token_id)
    if not token_data:
        await query.message.edit_text("❌ هذا التوكن لم يعد موجوداً.")
        return
    context.user_data['processing_token'] = token_data
    context.user_data['processing_token_id'] = token_id
    keyboard = [
        [InlineKeyboardButton("✅ تم تأكيد الاستعادة", callback_data="confirm_recovery")],
        [InlineKeyboardButton("❌ لم يتم تأكيد رمز الأمان", callback_data="not_confirmed")],
        [InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="pending_list")]
    ]
    token_info = f"🔑 **معالجة التوكن:**\n`{token_data['token'][:30]}...`\n\n"
    if token_data['account_data'] and token_data['account_data'].get('valid'):
        info = token_data['account_data']
        token_info += (
            f"📋 **معلومات من API:**\n"
            f"👤 الاسم: {info.get('name', 'غير معروف')}\n"
            f"🆔 ID: {info.get('account_id', 'غير معروف')}\n"
            f"🌍 السيرفر: {info.get('server', 'ME')}\n\n"
        )
    token_info += f"📅 استلم في: {token_data['received_at']}\n\n**اختر مسار الحساب:**"
    await query.message.edit_text(
        token_info,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def clear_pending_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("✅ نعم، امسح الكل", callback_data="confirm_clear_pending")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="pending_list")]
    ]
    await query.message.edit_text(
        "⚠️ **تحذير: مسح جميع التوكنات المنتظرة**\n\nسيتم حذف جميع التوكنات المنتظرة نهائياً.\nهل أنت متأكد؟",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_board_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    text = update.message.text
    step_data = user_steps[user_id]
    
    if step_data.get("step") == "waiting_board":
        parsed = parse_board_message(text)
        context.user_data['account_data'] = parsed
        step_data["step"] = "waiting_token"
        await update.message.reply_text(
            f"✅ **تم تحليل اللوحة:**\n\n👤 الاسم: {parsed['name']}\n🆔 ID: {parsed['account_id']}\n🌍 السيرفر: {parsed['server']}\n\n🔑 **أرسل التوكن (سيتم فحصه فوراً)**\nأو اضغط على 'تخطي التوكن' للإضافة بدون توكن:",
            reply_markup=get_token_skip_keyboard(),
            parse_mode="Markdown"
        )
    elif step_data.get("step") == "waiting_token":
        clean_token = extract_eat_token(text)
        context.user_data['account_data']['token'] = clean_token
        await update.message.reply_text("⏳ **جاري فحص التوكن عبر API...**", parse_mode="Markdown")
        result = await check_token_validity(clean_token)
        if result.get('valid'):
            context.user_data['account_data']['token_status'] = 'VALID'
            await update.message.reply_text(
                f"✅ **التوكن صالح!**\n\n📩 **أرسل الآن رسالة الاستعادة:**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
                ])
            )
            step_data["step"] = "waiting_recovery"
        else:
            await update.message.reply_text(
                f"❌ **التوكن غير صالح!**\n\nالسبب: {result.get('error', 'غير معروف')}\n\n⚠️ يمكنك:\n• إرسال توكن صحيح\n• أو تخطي التوكن وإضافته لاحقاً",
                reply_markup=get_token_skip_keyboard()
            )
    elif step_data.get("step") == "waiting_recovery":
        parsed = parse_recovery_message(text)
        context.user_data['recovery_data'] = parsed
        step_data["step"] = "waiting_prime"
        
        # ✅ إذا كان هناك pending_email وليس هناك رمز أمان، نطلبه
        if parsed.get("has_pending") == 1 and not parsed.get("security_code"):
            step_data["step"] = "waiting_security_code"
            await update.message.reply_text(
                f"✅ **تم تحليل الاستعادة:**\n\n"
                f"📧 البريد الحالي: {parsed['bound_email']}\n"
                f"📧 البريد الجديد: {parsed['pending_email']}\n"
                f"⏱ المدة: {format_time_remaining(parsed['recovery_end_time'])}\n"
                f"🔐 الحالة: ⏳ منتظرة\n\n"
                f"🔑 **أرسل رمز الأمان (6 أرقام):**",
                parse_mode="Markdown"
            )
            return
        
        code_text = f"\n🔐 رمز الأمان: `{parsed.get('security_code')}`" if parsed.get('security_code') else ""
        await update.message.reply_text(
            f"✅ **تم تحليل الاستعادة:**\n\n"
            f"📧 البريد الحالي: {parsed['bound_email']}\n"
            f"📧 البريد الجديد: {parsed['pending_email']}\n"
            f"⏱ المدة: {format_time_remaining(parsed['recovery_end_time'])}\n"
            f"🔐 الحالة: {'⏳ منتظرة' if parsed['has_pending'] else '❌ غير مؤكدة'}{code_text}\n\n"
            f"🏆 **اختر Prime:**",
            reply_markup=get_prime_keyboard(),
            parse_mode="Markdown"
        )

async def handle_security_code_from_board(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج رمز الأمان من لوحة الإدارة"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    
    step_data = user_steps[user_id]
    if step_data.get("step") != "waiting_security_code":
        return
    
    code = update.message.text.strip()
    
    if not re.match(r'^\d{6}$', code):
        await update.message.reply_text(
            "❌ **رمز الأمان غير صحيح!**\n\n"
            "يجب أن يكون رمز الأمان **6 أرقام** بالضبط.\n"
            "مثال: `123456`\n\n"
            "🔑 **أرسل رمز الأمان الصحيح (6 أرقام):**",
            parse_mode="Markdown"
        )
        return
    
    # ✅ حفظ رمز الأمان في recovery_data
    recovery_data = context.user_data.get('recovery_data', {})
    recovery_data["security_code"] = code
    context.user_data['recovery_data'] = recovery_data
    
    step_data["step"] = "waiting_prime"
    
    code_text = f"\n🔐 رمز الأمان: `{code}`"
    await update.message.reply_text(
        f"✅ **تم حفظ رمز الأمان**\n\n"
        f"📧 البريد الحالي: {recovery_data.get('bound_email', 'لا توجد')}\n"
        f"📧 البريد الجديد: {recovery_data.get('pending_email', 'لا توجد')}\n"
        f"⏱ المدة: {format_time_remaining(recovery_data.get('recovery_end_time'))}\n"
        f"🔐 الحالة: ⏳ منتظرة{code_text}\n\n"
        f"🏆 **اختر Prime:**",
        reply_markup=get_prime_keyboard(),
        parse_mode="Markdown"
    )

async def handle_path_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    context.user_data['recovery_path'] = 'confirmed'
    user_steps[user_id] = {"step": "waiting_board"}
    await query.message.edit_text(
        "✅ **تم اختيار: تم تأكيد الاستعادة**\n\n📋 **أرسل الآن نص لوحة الإدارة:**\n\nمثال:\n⚙️ لوحة الإدارة:\n👤 الاسم: sløㅤㅤ\n🆔 الأيدي: 985922586\n🌍 السيرفر: ME",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
        ])
    )

async def handle_path_not_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    context.user_data['recovery_path'] = 'not_confirmed'
    user_steps[user_id] = {"step": "waiting_board"}
    await query.message.edit_text(
        "❌ **تم اختيار: لم يتم تأكيد رمز الأمان**\n\n📋 **أرسل الآن نص لوحة الإدارة:**\n\nمثال:\n⚙️ لوحة الإدارة:\n👤 الاسم: sløㅤㅤ\n🆔 الأيدي: 985922586\n🌍 السيرفر: ME",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
        ])
    )

# =========================================================
# 📋 دوال إدارة الحساب - مع عرض رمز الأمان
# =========================================================

async def view_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[2])
    account = get_account(acc_id)
    if not account:
        await query.message.edit_text("❌ الحساب غير موجود.")
        return
    
    status_icon = get_token_status_icon(account['token_status'])
    status_text = get_token_status_text(account['token_status'])
    account_status_icon = get_account_status_icon(account['has_pending'], account['recovery_confirmed'])
    account_status_text = get_account_status_text(account['has_pending'], account['recovery_confirmed'])
    time_left = format_time_remaining(account['recovery_end_time'])
    last_check = account.get('last_token_check', 'لم يتم الفحص')
    security_code = account.get('security_code')
    code_text = f"\n🔐 **رمز الأمان:** `{security_code}`" if security_code else ""
    
    text = (
        f"⚙️ **تفاصيل الحساب**\n\n"
        f"👤 **الاسم:** {account['name']}\n"
        f"🆔 **ID:** `{account['account_id']}`\n"
        f"🌍 **السيرفر:** {account['server']}\n"
        f"🏆 **Prime:** {account['prime_level']} {get_prime_emoji(account['prime_level'])}\n"
        f"🔐 **الحالة:** {account_status_icon} {account_status_text}{code_text}\n\n"
        f"🔑 **التوكن:** {status_icon} {status_text}\n"
        f"🕐 **آخر فحص:** {last_check}\n"
        f"📧 **البريد الحالي:** `{account['bound_email']}`\n"
        f"📧 **البريد الجديد:** `{account['pending_email']}`\n"
        f"⏱ **المدة المتبقية:** {time_left}\n\n"
        f"📅 **تاريخ الإضافة:** {account['created_at']}\n"
        f"🔄 **آخر تحديث:** {account['updated_at']}"
    )
    
    keyboard = []
    row1 = []
    if account['token_status'] != "MISSING":
        row1.append(InlineKeyboardButton("🔄 إعادة فحص", callback_data=f"recheck_token_{acc_id}"))
    row1.append(InlineKeyboardButton("✏️ تحديث توكن", callback_data=f"update_token_{acc_id}"))
    keyboard.append(row1)
    
    if account['token_status'] == "MISSING":
        keyboard.append([
            InlineKeyboardButton("➕ إضافة توكن", callback_data=f"add_token_{acc_id}")
        ])
    
    if account['has_pending'] == 1 and account['recovery_confirmed'] == 0:
        keyboard.append([
            InlineKeyboardButton("✅ نقل إلى المؤكد", callback_data=f"move_to_confirmed_{acc_id}")
        ])
    
    keyboard.append([
        InlineKeyboardButton("✏️ تعديل الاستعادة", callback_data=f"edit_recovery_{acc_id}")
    ])
    
    if account['is_archived'] == 0 and account['is_completed'] == 0:
        keyboard.append([
            InlineKeyboardButton("🗑️ نقل للمهملات", callback_data=f"archive_acc_{acc_id}")
        ])
    elif account['is_archived'] == 1:
        keyboard.append([
            InlineKeyboardButton("🔄 استرجاع من المهملات", callback_data=f"restore_from_archive_{acc_id}")
        ])
    
    if account['is_completed'] == 1 or account['is_archived'] == 1:
        keyboard.append([
            InlineKeyboardButton("❌ حذف نهائي", callback_data=f"delete_acc_{acc_id}")
        ])
    
    keyboard.append([
        InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")
    ])
    
    if account['photo_id']:
        await query.message.delete()
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=account['photo_id'],
            caption=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def edit_recovery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    acc_id = int(query.data.split("_")[2])
    account = get_account(acc_id)
    
    if not account:
        await query.answer("❌ الحساب غير موجود.", show_alert=True)
        return
    
    user_steps[user_id] = {"step": "waiting_edit_recovery", "acc_id": acc_id}
    
    text = (
        f"✏️ **تعديل الاستعادة**\n\n"
        f"👤 الحساب: {account['name']}\n"
        f"📧 البريد الحالي: {account['bound_email']}\n"
        f"📧 البريد الجديد الحالي: {account['pending_email']}\n"
        f"⏱️ المدة المتبقية: {format_time_remaining(account['recovery_end_time'])}\n"
        f"🔐 رمز الأمان الحالي: `{account.get('security_code', 'لا يوجد')}`\n\n"
        f"📩 **أرسل رسالة الاستعادة الجديدة:**\n\n"
        f"مثال:\n"
        f"📧 الاستعادة المربوطة: old@email.com\n"
        f"⏳ استعادة قيد التأكيد: new@email.com\n"
        f"المدة المتبقية: 3 يوم و 6 ساعة\n"
        f"رمز الأمان: 123456"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 إلغاء", callback_data=f"view_acc_{acc_id}")]
    ])
    
    await safe_edit_or_send(query, text, keyboard)

async def handle_edit_recovery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    
    step_data = user_steps[user_id]
    if step_data.get("step") != "waiting_edit_recovery":
        return
    
    acc_id = step_data["acc_id"]
    text = update.message.text.strip()
    
    parsed = parse_recovery_message(text)
    pending_email = parsed.get('pending_email', 'لا توجد')
    security_code = parsed.get('security_code')
    
    if not pending_email or pending_email == 'لا توجد':
        await update.message.reply_text(
            "❌ **لم يتم العثور على بريد جديد.**\n\nيرجى إرسال رسالة الاستعادة كاملة مع البريد الجديد.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"edit_recovery_{acc_id}")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    # ✅ تحديث الاستعادة مع رمز الأمان
    update_recovery_only(acc_id, pending_email, parsed.get('recovery_end_time'), security_code)
    user_steps.pop(user_id, None)
    
    account = get_account(acc_id)
    status_text = get_account_status_text(account['has_pending'], account['recovery_confirmed'])
    code_text = f"\n🔐 رمز الأمان: `{security_code}`" if security_code else ""
    
    await update.message.reply_text(
        f"✅ **تم تحديث الاستعادة بنجاح!**\n\n"
        f"👤 الحساب: {account['name']}\n"
        f"📧 البريد الجديد: {pending_email}\n"
        f"⏱️ المدة الجديدة: {format_time_remaining(parsed.get('recovery_end_time'))}\n"
        f"🔐 الحالة: {status_text}{code_text}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc_id}")],
            [InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

async def add_token_to_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    try:
        parts = query.data.split("_")
        if len(parts) >= 4:
            acc_id = int(parts[3])
        else:
            await query.answer("❌ حدث خطأ في الرابط.", show_alert=True)
            return
    except (ValueError, IndexError):
        await query.answer("❌ حدث خطأ في الرابط.", show_alert=True)
        return
    
    account = get_account(acc_id)
    if not account:
        await query.answer("❌ الحساب غير موجود.", show_alert=True)
        return
    
    user_steps[user_id] = {"step": "add_token_to_account", "acc_id": acc_id}
    
    text = (
        f"➕ **إضافة توكن للحساب**\n\n"
        f"👤 الحساب: {account['name']}\n\n"
        f"🔑 **أرسل التوكن (سيتم فحصه فوراً):**"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 إلغاء", callback_data=f"view_acc_{acc_id}")]
    ])
    
    await safe_edit_or_send(query, text, keyboard)

async def handle_add_token_to_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    step_data = user_steps[user_id]
    if step_data.get("step") != "add_token_to_account":
        return
    acc_id = step_data["acc_id"]
    token_text = update.message.text.strip()
    clean_token = extract_eat_token(token_text)
    await update.message.reply_text("⏳ **جاري فحص التوكن...**", parse_mode="Markdown")
    result = await check_token_validity(clean_token)
    if result.get('valid'):
        update_account_token(acc_id, clean_token, 'VALID')
        update_account_status(acc_id, 'VALID', datetime.now().isoformat())
        user_steps.pop(user_id, None)
        await update.message.reply_text(
            f"✅ **تم إضافة التوكن بنجاح!**\n\n🔑 التوكن صالح ومفعل.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc_id}")],
                [InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ **التوكن غير صالح!**\n\nالسبب: {result.get('error', 'غير معروف')}\n\n⚠️ يرجى إرسال توكن صحيح.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"add_token_{acc_id}")],
                [InlineKeyboardButton("🔙 إلغاء", callback_data=f"view_acc_{acc_id}")]
            ]),
            parse_mode="Markdown"
        )

async def recheck_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[2])
    account = get_account(acc_id)
    
    if not account:
        await safe_edit_or_send(query, "❌ الحساب غير موجود.")
        return
    
    if account['token_status'] == "MISSING":
        await safe_edit_or_send(
            query,
            "❌ **لا يوجد توكن لهذا الحساب.**\n\nيرجى إضافة توكن أولاً.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ إضافة توكن", callback_data=f"add_token_{acc_id}")],
                [InlineKeyboardButton("🔙 رجوع", callback_data=f"view_acc_{acc_id}")]
            ])
        )
        return
    
    await safe_edit_or_send(query, "⏳ **جاري فحص التوكن عبر API...**", parse_mode="Markdown")
    
    result = await check_token_validity(account['eat_token'])
    
    if result.get('valid'):
        update_account_status(acc_id, 'VALID', datetime.now().isoformat())
        status_text = "✅ صالح"
    else:
        update_account_status(acc_id, 'EXPIRED', datetime.now().isoformat())
        status_text = "❌ غير صالح/محروق"
    
    await safe_edit_or_send(
        query,
        f"🔄 **نتيجة فحص التوكن**\n\n"
        f"👤 الحساب: {account['name']}\n"
        f"🔑 الحالة: {status_text}\n"
        f"📝 التفاصيل: {result.get('error', 'لا توجد تفاصيل')}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع للحساب", callback_data=f"view_acc_{acc_id}")]
        ])
    )

async def start_token_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    try:
        parts = query.data.split("_")
        if len(parts) >= 3:
            acc_id = int(parts[2])
        else:
            await query.answer("❌ حدث خطأ في الرابط.", show_alert=True)
            return
    except (ValueError, IndexError):
        await query.answer("❌ حدث خطأ في الرابط.", show_alert=True)
        return
    
    account = get_account(acc_id)
    if not account:
        await query.answer("❌ الحساب غير موجود.", show_alert=True)
        return
    
    user_steps[user_id] = {"step": "waiting_new_token", "acc_id": acc_id}
    
    text = (
        f"✏️ **تحديث التوكن**\n\n"
        f"👤 الحساب: {account['name']}\n"
        f"🔑 الحالي: {get_token_status_text(account['token_status'])}\n\n"
        f"🔑 **أرسل التوكن الجديد (سيتم فحصه فوراً):**"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 إلغاء", callback_data=f"view_acc_{acc_id}")]
    ])
    
    await safe_edit_or_send(query, text, keyboard)

async def handle_new_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    step_data = user_steps[user_id]
    if step_data.get("step") != "waiting_new_token":
        return
    acc_id = step_data["acc_id"]
    token_text = update.message.text.strip()
    clean_token = extract_eat_token(token_text)
    await update.message.reply_text("⏳ **جاري فحص التوكن الجديد...**", parse_mode="Markdown")
    result = await check_token_validity(clean_token)
    if result.get('valid'):
        update_account_token(acc_id, clean_token, 'VALID')
        update_account_status(acc_id, 'VALID', datetime.now().isoformat())
        user_steps.pop(user_id, None)
        await update.message.reply_text(
            f"✅ **تم تحديث التوكن بنجاح!**\n\n🔑 التوكن صالح ومفعل.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc_id}")],
                [InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"❌ **التوكن غير صالح!**\n\nالسبب: {result.get('error', 'غير معروف')}\n\n⚠️ يرجى إرسال توكن صحيح.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"update_token_{acc_id}")],
                [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc_id}")]
            ]),
            parse_mode="Markdown"
        )

async def start_move_to_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[3])
    user_id = query.from_user.id
    account = get_account(acc_id)
    
    if not account:
        await query.answer("❌ الحساب غير موجود.", show_alert=True)
        return
    
    user_steps[user_id] = {"step": "waiting_move_recovery", "acc_id": acc_id}
    
    text = (
        f"📦 **نقل إلى المؤكد**\n\n"
        f"👤 الحساب: {account['name']}\n"
        f"🔐 رمز الأمان الحالي: `{account.get('security_code', 'لا يوجد')}`\n\n"
        f"📩 **أرسل رسالة الاستعادة الجديدة مع رمز الأمان:**"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 إلغاء", callback_data=f"view_acc_{acc_id}")]
    ])
    
    await safe_edit_or_send(query, text, keyboard)

async def handle_move_recovery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    step_data = user_steps[user_id]
    if step_data.get("step") != "waiting_move_recovery":
        return
    acc_id = step_data["acc_id"]
    text = update.message.text.strip()
    parsed = parse_recovery_message(text)
    
    pending_email = parsed.get('pending_email', 'لا توجد')
    security_code = parsed.get('security_code')
    
    if not pending_email or pending_email == 'لا توجد':
        await update.message.reply_text(
            "❌ **لم يتم العثور على بريد جديد.**\n\nيرجى إرسال رسالة الاستعادة كاملة مع البريد الجديد.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"move_to_confirmed_{acc_id}")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    # ✅ إذا لم يوجد رمز أمان، نطلبه
    if not security_code:
        step_data["step"] = "waiting_move_security_code"
        await update.message.reply_text(
            f"✅ **تم العثور على البريد الجديد:** {pending_email}\n\n"
            f"🔑 **أرسل رمز الأمان (6 أرقام):**",
            parse_mode="Markdown"
        )
        return
    
    update_account_confirmed(acc_id, pending_email, parsed.get('recovery_end_time'), security_code)
    user_steps.pop(user_id, None)
    
    code_text = f"\n🔐 رمز الأمان: `{security_code}`" if security_code else ""
    
    await update.message.reply_text(
        f"✅ **تم نقل الحساب إلى المؤكد!**\n\n"
        f"📧 البريد الجديد: {pending_email}{code_text}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc_id}")],
            [InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

async def handle_move_security_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج رمز الأمان عند نقل الحساب إلى المؤكد"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    
    step_data = user_steps[user_id]
    if step_data.get("step") != "waiting_move_security_code":
        return
    
    code = update.message.text.strip()
    
    if not re.match(r'^\d{6}$', code):
        await update.message.reply_text(
            "❌ **رمز الأمان غير صحيح!**\n\n"
            "يجب أن يكون رمز الأمان **6 أرقام** بالضبط.\n"
            "مثال: `123456`\n\n"
            "🔑 **أرسل رمز الأمان الصحيح (6 أرقام):**",
            parse_mode="Markdown"
        )
        return
    
    acc_id = step_data["acc_id"]
    account = get_account(acc_id)
    
    if not account:
        await update.message.reply_text("❌ الحساب غير موجود.")
        return
    
    # ✅ تحديث رمز الأمان ونقل الحساب للمؤكد
    update_account_confirmed(acc_id, account['pending_email'], account['recovery_end_time'], code)
    user_steps.pop(user_id, None)
    
    await update.message.reply_text(
        f"✅ **تم نقل الحساب إلى المؤكد!**\n\n"
        f"👤 الحساب: {account['name']}\n"
        f"📧 البريد الجديد: {account['pending_email']}\n"
        f"🔐 رمز الأمان: `{code}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc_id}")],
            [InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

async def archive_account_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[2])
    account = get_account(acc_id)
    
    if not account:
        await query.answer("❌ الحساب غير موجود.", show_alert=True)
        return
    
    text = (
        f"⚠️ **نقل إلى سلة المهملات**\n\n"
        f"👤 الحساب: {account['name']}\n\n"
        f"هل أنت متأكد؟"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، انقل للمهملات", callback_data=f"confirm_archive_{acc_id}")],
        [InlineKeyboardButton("❌ إلغاء", callback_data=f"view_acc_{acc_id}")]
    ])
    
    await safe_edit_or_send(query, text, keyboard)

async def confirm_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[2])
    archive_account(acc_id)
    
    text = "🗑️ **تم نقل الحساب إلى سلة المهملات.**"
    await safe_edit_or_send(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
    ]))

async def delete_account_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[2])
    account = get_account(acc_id)
    
    if not account:
        await query.answer("❌ الحساب غير موجود.", show_alert=True)
        return
    
    text = (
        f"🚨 **حذف نهائي**\n\n"
        f"👤 الحساب: {account['name']}\n\n"
        f"⚠️ لا يمكن التراجع عن هذا الإجراء."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 نعم، احذف نهائياً", callback_data=f"confirm_delete_{acc_id}")],
        [InlineKeyboardButton("❌ إلغاء", callback_data=f"view_acc_{acc_id}")]
    ])
    
    await safe_edit_or_send(query, text, keyboard)

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[2])
    delete_account_permanently(acc_id)
    
    text = "❌ **تم حذف الحساب نهائياً.**"
    await safe_edit_or_send(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
    ]))

async def restore_from_archive_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[3])
    restore_from_archive(acc_id)
    
    text = "🔄 **تم استرجاع الحساب من سلة المهملات.**"
    await safe_edit_or_send(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
    ]))

# =========================================================
# 📋 دوال الإحصائيات والبحث
# =========================================================

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    stats = get_stats()
    text = (
        f"📊 **لوحة الإحصائيات الشاملة**\n\n"
        f"📦 **إجمالي الحسابات النشطة:** {stats['total_active']}\n"
        f"🟢 **توكنات صالحة:** {stats['valid_tokens']}\n"
        f"🔴 **توكنات محروقة:** {stats['expired_tokens']}\n"
        f"❌ **بدون توكن:** {stats['missing_tokens']}\n\n"
        f"✅ **مؤكدة:** {stats['confirmed']}\n"
        f"⏳ **منتظرة:** {stats['pending']}\n"
        f"❌ **غير مؤكدة:** {stats['unconfirmed']}\n"
        f"🗑️ **في سلة المهملات:** {stats['archived']}\n\n"
        f"🏆 **توزيع Prime:**\n"
    )
    for p in range(1, 9):
        emoji = get_prime_emoji(p)
        count = stats['prime_stats'].get(p, 0)
        text += f"└── {emoji} Prime {p}: {count}\n"
    
    keyboard = [
        [InlineKeyboardButton("✅ مؤكدة", callback_data="stats_detail_confirmed")],
        [InlineKeyboardButton("⏳ منتظرة", callback_data="stats_detail_pending")],
        [InlineKeyboardButton("❌ غير مؤكدة", callback_data="stats_detail_unconfirmed")],
        [InlineKeyboardButton("🔴 محروقة", callback_data="stats_detail_expired")],
        [InlineKeyboardButton("❌ بدون توكن", callback_data="stats_detail_missing")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
    ]
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_stats_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    detail_type = query.data.split("_")[2]
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    if detail_type == "confirmed":
        cursor.execute("SELECT id, name, account_id, prime_level, has_pending, recovery_confirmed, security_code FROM accounts WHERE recovery_confirmed = 1 AND is_archived = 0 AND is_completed = 0 ORDER BY prime_level ASC")
    elif detail_type == "pending":
        cursor.execute("SELECT id, name, account_id, prime_level, has_pending, recovery_confirmed, pending_email, recovery_end_time, security_code FROM accounts WHERE has_pending = 1 AND recovery_confirmed = 0 AND is_archived = 0 AND is_completed = 0 ORDER BY prime_level ASC")
    elif detail_type == "unconfirmed":
        cursor.execute("SELECT id, name, account_id, prime_level, has_pending, recovery_confirmed, security_code FROM accounts WHERE has_pending = 0 AND recovery_confirmed = 0 AND is_archived = 0 AND is_completed = 0 ORDER BY prime_level ASC")
    elif detail_type == "expired":
        cursor.execute("SELECT id, name, account_id, prime_level FROM accounts WHERE token_status = 'EXPIRED' AND is_archived = 0 AND is_completed = 0 ORDER BY prime_level ASC")
    elif detail_type == "missing":
        cursor.execute("SELECT id, name, account_id, prime_level FROM accounts WHERE token_status = 'MISSING' AND is_archived = 0 AND is_completed = 0 ORDER BY prime_level ASC")
    else:
        await query.message.edit_text("❌ نوع غير معروف.")
        return
    
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        await query.message.edit_text(
            f"📭 **لا توجد حسابات في هذه الفئة.**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع للإحصائيات", callback_data="show_stats")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    prime_groups = {p: [] for p in range(1, 9)}
    for row in rows:
        if len(row) > 3:
            prime_groups[row[3]].append(row)
        else:
            prime_groups[row[2]].append(row)
    
    text = f"📋 **تفاصيل: {detail_type}**\n\n"
    for p in range(1, 9):
        if prime_groups[p]:
            text += f"{get_prime_emoji(p)} **Prime {p}:**\n"
            for row in prime_groups[p]:
                if detail_type == "pending":
                    code = row[8] if len(row) > 8 else None
                    code_text = f" 🔐{code[:3]}***" if code else ""
                    text += f"├── {row[1]} ({row[2]}) - ⏳ {format_time_remaining(row[7])}{code_text}\n"
                else:
                    code = row[6] if len(row) > 6 else None
                    code_text = f" 🔐{code[:3]}***" if code else ""
                    text += f"├── {row[1]} ({row[2]}){code_text}\n"
            text += "\n"
    
    keyboard = [[InlineKeyboardButton("🔙 رجوع للإحصائيات", callback_data="show_stats")]]
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_search_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.edit_text(
        "🔍 **البحث الذكي**\n\nأرسل أي معلومات للبحث:\n• ID الحساب\n• اسم الحساب\n• البريد الإلكتروني\n• جزء من التوكن\n• رمز الأمان\n\n💡 يمكنك أيضاً إرسال ID أو اسم مباشرة في الشات",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )
    user_id = query.from_user.id
    user_steps[user_id] = {"step": "searching"}

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    if user_steps[user_id].get("step") != "searching":
        return
    query = update.message.text.strip()
    results = search_accounts(query)
    if not results:
        await update.message.reply_text(
            f"❌ **لا توجد نتائج للبحث:** `{query}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 بحث جديد", callback_data="search_menu")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    keyboard = []
    for acc in results:
        status_icon = get_account_status_icon(acc.get('has_pending', 0), acc.get('recovery_confirmed', 0))
        token_status_icon = get_token_status_icon(acc['token_status'])
        code = acc.get('security_code')
        code_text = f" 🔐{code[:3]}***" if code else ""
        btn_text = f"{status_icon}{token_status_icon} {acc['name']} | {acc['account_id']}{code_text}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_acc_{acc['id']}")])
    
    keyboard.append([InlineKeyboardButton("🔍 بحث جديد", callback_data="search_menu")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    user_steps.pop(user_id, None)
    
    await update.message.reply_text(
        f"🔍 **نتائج البحث عن:** `{query}`\n📋 عدد النتائج: {len(results)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def show_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    accounts = get_archived_accounts()
    if not accounts:
        await query.message.edit_text(
            "📭 **سلة المهملات فارغة.**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return
    keyboard = []
    for acc in accounts:
        status_icon = get_account_status_icon(acc.get('has_pending', 0), acc.get('recovery_confirmed', 0))
        token_status_icon = get_token_status_icon(acc['token_status'])
        code = acc.get('security_code')
        code_text = f" 🔐{code[:3]}***" if code else ""
        btn_text = f"{status_icon}{token_status_icon} {acc['name']} | {acc['account_id']}{code_text}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_acc_{acc['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    await query.message.edit_text(
        f"🗑️ **سلة المهملات** ({len(accounts)})",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# =========================================================
# 📋 دوال النسخ الاحتياطي
# =========================================================

async def create_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.edit_text("⏳ **جاري إنشاء النسخة الاحتياطية...**", parse_mode="Markdown")
    try:
        accounts = get_all_accounts_for_backup()
        logs = get_all_logs_for_backup()
        pending = get_all_pending_for_backup()
        backup_data = {
            "backup_meta": {
                "version": "3.0",
                "created_at": datetime.now().isoformat(),
                "total_accounts": len(accounts),
                "total_logs": len(logs),
                "total_pending": len(pending)
            },
            "accounts": accounts,
            "logs": logs,
            "pending_tokens": pending
        }
        filename = f"ff_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2, default=str)
        zip_filename = filename.replace('.json', '.zip')
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(filename)
        with open(zip_filename, 'rb') as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=zip_filename,
                caption=f"💾 **النسخة الاحتياطية**\n\n📅 التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n📦 عدد الحسابات: {len(accounts)}"
            )
        os.remove(filename)
        os.remove(zip_filename)
        
        cleanup_temp_files()
        
        await query.message.edit_text(
            "✅ **تم إنشاء النسخة الاحتياطية وإرسالها!**\n\n🧹 تم تنظيف الملفات المؤقتة.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.message.edit_text(f"❌ **خطأ:** `{str(e)}`", parse_mode="Markdown")

async def restore_backup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.edit_text(
        "⚠️ **استرداد النسخة الاحتياطية**\n\nسيتم استبدال جميع البيانات الحالية.\n\n📤 **أرسل ملف ZIP أو JSON**",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ نعم، استرد", callback_data="restore_confirm")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )
    user_id = query.from_user.id
    user_steps[user_id] = {"step": "waiting_backup_file"}

async def confirm_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_steps[user_id] = {"step": "waiting_backup_file"}
    await query.message.edit_text("📤 **يرجى إرسال ملف النسخة الاحتياطية.**", parse_mode="Markdown")

async def handle_backup_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    if user_steps[user_id].get("step") != "waiting_backup_file":
        return
    if not update.message.document:
        await update.message.reply_text("❌ يرجى إرسال ملف ZIP أو JSON.")
        return
    document = update.message.document
    file_name = document.file_name
    if not (file_name.endswith('.json') or file_name.endswith('.zip')):
        await update.message.reply_text("❌ يرجى إرسال ملف JSON أو ZIP صالح.")
        return
    msg = await update.message.reply_text("⏳ **جاري استرداد النسخة...**", parse_mode="Markdown")
    try:
        file = await context.bot.get_file(document.file_id)
        temp_file = f"temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{file_name.split('.')[-1]}"
        await file.download_to_drive(temp_file)
        if temp_file.endswith('.zip'):
            with zipfile.ZipFile(temp_file, 'r') as zipf:
                zipf.extractall('temp_extract')
                json_files = [f for f in os.listdir('temp_extract') if f.endswith('.json')]
                if not json_files:
                    raise Exception("لم يتم العثور على ملف JSON")
                json_file = os.path.join('temp_extract', json_files[0])
        else:
            json_file = temp_file
        with open(json_file, 'r', encoding='utf-8') as f:
            backup_data = json.load(f)
        if 'backup_meta' not in backup_data:
            raise Exception("ملف النسخة غير صالح")
        result = restore_from_backup(backup_data)
        if os.path.exists(temp_file):
            os.remove(temp_file)
        if os.path.exists('temp_extract'):
            shutil.rmtree('temp_extract')
        if os.path.exists(json_file) and json_file != temp_file:
            os.remove(json_file)
        
        cleanup_temp_files()
        
        user_steps.pop(user_id, None)
        await msg.edit_text(
            f"✅ **تم استرداد النسخة!**\n\n📊 الحسابات: {result['accounts_restored']}\n📋 السجلات: {result['logs_restored']}\n⏳ التوكنات المنتظرة: {result['pending_restored']}\n\n🧹 تم تنظيف الملفات المؤقتة.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"❌ **خطأ:** `{str(e)}`", parse_mode="Markdown")

# =========================================================
# 🧹 دوال تنظيف الملفات المؤقتة
# =========================================================

def cleanup_temp_files():
    try:
        for f in os.listdir('.'):
            if f.startswith('temp_') and f.endswith('.zip'):
                os.remove(f)
                logger.info(f"🧹 تم حذف الملف المؤقت: {f}")
        
        for d in os.listdir('.'):
            if d.startswith('extract_') and os.path.isdir(d):
                shutil.rmtree(d)
                logger.info(f"🧹 تم حذف المجلد المؤقت: {d}")
        
        for f in os.listdir('.'):
            if f.endswith('.log') and os.path.isfile(f):
                try:
                    mtime = os.path.getmtime(f)
                    if (datetime.now().timestamp() - mtime) > 30 * 24 * 60 * 60:
                        os.remove(f)
                        logger.info(f"🧹 تم حذف ملف السجل القديم: {f}")
                except Exception:
                    pass
        
        logger.info("🧹 تم تنظيف الملفات المؤقتة بنجاح")
    except Exception as e:
        logger.error(f"❌ خطأ في تنظيف الملفات: {e}")

# =========================================================
# 🔄 المراقبة التلقائية
# =========================================================

def get_last_notification(account_id: int, notification_type: str) -> Optional[str]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT sent_at FROM notifications WHERE account_id = ? AND type = ? ORDER BY sent_at DESC LIMIT 1",
        (account_id, notification_type)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def save_notification(account_id: int, notification_type: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO notifications (account_id, type, sent_at) VALUES (?, ?, ?)",
        (account_id, notification_type, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

async def scheduled_token_monitor(app: Application):
    logger.info("🔄 بدء فحص التوكنات الدوري...")
    accounts = get_all_accounts_for_monitoring()
    if not accounts:
        logger.info("📭 لا توجد حسابات للمراقبة")
        return
    checked_count = 0
    expired_count = 0
    valid_count = 0
    for account in accounts:
        acc_id = account['id']
        name = account['name']
        token = account['eat_token']
        current_status = account['token_status']
        if not token or token == 'لم يتم إضافة توكن':
            continue
        try:
            result = await check_token_validity(token)
            check_time = datetime.now().isoformat()
            if result.get('valid'):
                if current_status != 'VALID':
                    update_account_status(acc_id, 'VALID', check_time)
                    add_log(acc_id, "TOKEN_RESTORED", "تم استعادة التوكن (أصبح صالحاً)")
                    await app.bot.send_message(
                        chat_id=ADMIN_IDS[0],
                        text=f"🟢 **استعادة التوكن**\n\n👤 الحساب: {name}\n🔑 أصبح التوكن صالحاً مرة أخرى.\n🕐 تم الفحص: {check_time}",
                        parse_mode="Markdown"
                    )
                    logger.info(f"✅ {name}: توكن استعاد صلاحيته")
                valid_count += 1
            else:
                if current_status != 'EXPIRED':
                    update_account_status(acc_id, 'EXPIRED', check_time)
                    add_log(acc_id, "TOKEN_EXPIRED", f"انتهت صلاحية التوكن: {result.get('error', 'غير معروف')}")
                    await app.bot.send_message(
                        chat_id=ADMIN_IDS[0],
                        text=f"🚨 **تنبيه: توكن محروق!**\n\n👤 الحساب: {name}\n❌ التوكن أصبح غير صالح!\n📝 السبب: {result.get('error', 'غير معروف')}\n🕐 تم الفحص: {check_time}\n\n⚠️ يرجى تحديث التوكن في أقرب وقت.",
                        parse_mode="Markdown"
                    )
                    logger.info(f"❌ {name}: توكن محروق")
                expired_count += 1
            checked_count += 1
            update_account_status(acc_id, None, check_time)
        except Exception as e:
            logger.error(f"خطأ في فحص التوكن {acc_id} ({name}): {e}")
    logger.info(f"📊 نتائج الفحص الدوري: {checked_count} حساب, {valid_count} صالح, {expired_count} محروق")

async def scheduled_notifications(app: Application):
    logger.info("🔔 بدء فحص التنبيهات...")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, prime_level, token_status, recovery_confirmed, 
               pending_email, recovery_end_time, has_pending, account_id, security_code
        FROM accounts 
        WHERE is_archived = 0 AND is_completed = 0
    """)
    accounts = cursor.fetchall()
    conn.close()
    
    now = datetime.now()
    accounts_to_confirm = []
    
    for acc_id, name, prime, token_status, recovery_confirmed, pending_email, recovery_end_time, has_pending, account_id, security_code in accounts:
        
        if has_pending == 0 and recovery_confirmed == 0 and token_status == 'VALID':
            if prime in [3, 4, 5, 6, 7, 8]:
                schedule = NOTIFICATION_SCHEDULE.get(prime)
                if schedule:
                    last_sent = get_last_notification(acc_id, "UNCONFIRMED_REMINDER")
                    if not last_sent or (now - datetime.fromisoformat(last_sent)).days >= schedule:
                        code_text = f"\n🔐 رمز الأمان: `{security_code}`" if security_code else ""
                        await app.bot.send_message(
                            chat_id=ADMIN_IDS[0],
                            text=f"🔔 **تذكير: حساب غير مؤكد**\n\n"
                                 f"👤 {name}\n"
                                 f"🏆 Prime {prime} {get_prime_emoji(prime)}\n\n"
                                 f"❓ هل تم العثور على استعادة جديدة لهذا الحساب؟{code_text}\n\n"
                                 f"📌 إذا كان لديك استعادة جديدة، أرسلها لتحديث الحساب.",
                            parse_mode="Markdown"
                        )
                        save_notification(acc_id, "UNCONFIRMED_REMINDER")
                        logger.info(f"📤 تم إرسال تذكير غير مؤكد للحساب {name}")
        
        elif has_pending == 1 and recovery_confirmed == 0 and recovery_end_time:
            try:
                end_time = datetime.fromisoformat(recovery_end_time)
                diff = end_time - now
                days_remaining = diff.days
                hours_remaining = diff.total_seconds() / 3600
                
                if 6 < days_remaining <= 8:
                    last_sent = get_last_notification(acc_id, "PENDING_7_DAYS")
                    if not last_sent or (now - datetime.fromisoformat(last_sent)).days >= 1:
                        code_text = f"\n🔐 رمز الأمان: `{security_code}`" if security_code else ""
                        await app.bot.send_message(
                            chat_id=ADMIN_IDS[0],
                            text=f"⏳ **تنبيه: استعادة على وشك الانتهاء**\n\n"
                                 f"👤 {name}\n"
                                 f"🏆 Prime {prime} {get_prime_emoji(prime)}\n"
                                 f"📧 البريد الجديد: {pending_email}{code_text}\n\n"
                                 f"⏱️ **باقي 7 أيام على انتهاء الاستعادة**\n\n"
                                 f"📌 تأكد من متابعة رمز الأمان لتأكيد الاستعادة.",
                            parse_mode="Markdown"
                        )
                        save_notification(acc_id, "PENDING_7_DAYS")
                        logger.info(f"📤 تم إرسال تنبيه 7 أيام للحساب {name}")
                
                elif 0 < hours_remaining <= 24 and days_remaining == 0:
                    last_sent = get_last_notification(acc_id, "PENDING_24_HOURS")
                    if not last_sent or (now - datetime.fromisoformat(last_sent)).seconds >= 3600:
                        code_text = f"\n🔐 رمز الأمان: `{security_code}`" if security_code else ""
                        await app.bot.send_message(
                            chat_id=ADMIN_IDS[0],
                            text=f"⏳ **تنبيه: استعادة على وشك الانتهاء**\n\n"
                                 f"👤 {name}\n"
                                 f"🏆 Prime {prime} {get_prime_emoji(prime)}\n"
                                 f"📧 البريد الجديد: {pending_email}{code_text}\n\n"
                                 f"⏱️ **باقي 24 ساعة فقط على انتهاء الاستعادة!**\n\n"
                                 f"⚠️ يرجى تأكيد رمز الأمان قبل انتهاء الوقت.",
                            parse_mode="Markdown"
                        )
                        save_notification(acc_id, "PENDING_24_HOURS")
                        logger.info(f"📤 تم إرسال تنبيه 24 ساعة للحساب {name}")
                
                elif diff.total_seconds() <= 0 and recovery_confirmed == 0:
                    accounts_to_confirm.append((acc_id, name, prime, pending_email, security_code))
                    
            except Exception as e:
                logger.error(f"❌ خطأ في حساب الوقت للحساب {acc_id}: {e}")
    
    for acc_id, name, prime, pending_email, security_code in accounts_to_confirm:
        try:
            confirm_account_automatically(acc_id)
            
            code_text = f"\n🔐 رمز الأمان: `{security_code}`" if security_code else ""
            
            await app.bot.send_message(
                chat_id=ADMIN_IDS[0],
                text=f"✅ **اكتملت الاستعادة وتم النقل تلقائياً!**\n\n"
                     f"👤 {name}\n"
                     f"🏆 Prime {prime} {get_prime_emoji(prime)}\n"
                     f"📧 البريد الجديد: {pending_email}{code_text}\n\n"
                     f"🎉 **تم نقل الحساب تلقائياً من الحسابات المنتظرة إلى المؤكدة!**\n\n"
                     f"📌 يمكنك الآن عرضه في قسم **📋 الحسابات المؤكدة**.",
                parse_mode="Markdown"
            )
            logger.info(f"✅ تم نقل الحساب {name} تلقائياً إلى المؤكد")
            
        except Exception as e:
            logger.error(f"❌ خطأ في نقل الحساب {acc_id} للمؤكد: {e}")
    
    cleanup_temp_files()
    
    logger.info("✅ انتهى فحص التنبيهات")

# =========================================================
# 📋 معالج الرسائل الذكي
# =========================================================

async def handle_account_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.message.text.strip()
    
    if not name or len(name) < 2:
        await update.message.reply_text(
            "❌ **الاسم قصير جداً.**\n\nيرجى إرسال اسم صحيح (أكثر من حرفين):",
            parse_mode="Markdown"
        )
        return
    
    temp_data = context.user_data.get('temp_token_data', {})
    token_text = temp_data.get('token')
    account_id = temp_data.get('account_id')
    server = temp_data.get('server')
    result = temp_data.get('result')
    
    if not token_text or not account_id:
        await update.message.reply_text(
            "❌ **انتهت الجلسة.** يرجى إعادة إرسال التوكن.",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    existing_accounts = search_accounts_by_id(account_id)
    
    context.user_data.pop('temp_token_data', None)
    user_steps.pop(user_id, None)
    
    if not existing_accounts:
        context.user_data['manual_add_data'] = {
            'name': name,
            'account_id': account_id,
            'server': server,
            'token': token_text,
            'token_status': 'VALID'
        }
        context.user_data['manual_add_step'] = "manual_recovery"
        
        await update.message.reply_text(
            f"✅ **تم حفظ الاسم:** {name}\n\n"
            f"📩 **أرسل الآن رسالة الاستعادة:**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    for acc in existing_accounts:
        if acc.get('token_status') == 'EXPIRED':
            replace_token_only(acc['id'], token_text)
            update_account_name(acc['id'], name)
            
            await update.message.reply_text(
                f"🔄 **تم استبدال التوكن وتحديث الاسم!**\n\n"
                f"👤 الاسم الجديد: {name}\n"
                f"🆔 ID: {account_id}\n"
                f"🔑 **تم استبدال التوكن المحروق بالتوكن الجديد الصالح**\n\n"
                f"✅ يمكنك الآن استخدام الحساب بشكل طبيعي.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc['id']}")],
                    [InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")]
                ]),
                parse_mode="Markdown"
            )
            return
    
    context.user_data['pending_token'] = {
        'token': token_text,
        'account_id': account_id,
        'name': name,
        'server': server
    }
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، استبدل التوكن", callback_data=f"replace_token_confirm_{existing_accounts[0]['id']}")],
        [InlineKeyboardButton("❌ لا، إلغاء", callback_data="replace_token_cancel")],
        [InlineKeyboardButton("➕ إضافة حساب جديد", callback_data=f"add_new_account_{account_id}")]
    ])
    
    await update.message.reply_text(
        f"⚠️ **يوجد حساب بنفس الـ ID**\n\n"
        f"👤 الحساب الحالي: {existing_accounts[0]['name']}\n"
        f"🆔 ID: {account_id}\n"
        f"🔑 حالة التوكن: {get_token_status_text(existing_accounts[0]['token_status'])}\n"
        f"📝 الاسم الجديد: {name}\n"
        f"🔐 رمز الأمان: `{existing_accounts[0].get('security_code', 'لا يوجد')}`\n\n"
        f"📌 التوكن الجديد صالح.\n\n"
        f"هل تريد استبدال التوكن الحالي بالجديد وتحديث الاسم؟",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def handle_token_input(update: Update, context: ContextTypes.DEFAULT_TYPE, token_text: str):
    user_id = update.effective_user.id
    await update.message.reply_text("⏳ **جاري فحص التوكن...**", parse_mode="Markdown")
    
    result = await check_token_validity(token_text)
    
    if not result.get('valid'):
        await update.message.reply_text(
            f"❌ **التوكن غير صالح!**\n\nالسبب: {result.get('error', 'غير معروف')}",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    account_id = result.get('account_id')
    server = result.get('server', 'ME')
    
    context.user_data['temp_token_data'] = {
        'token': token_text,
        'account_id': account_id,
        'server': server,
        'result': result
    }
    
    await update.message.reply_text(
        f"✅ **تم التحقق من التوكن بنجاح!**\n\n"
        f"🆔 ID المستخرج: `{account_id}`\n"
        f"🌍 السيرفر: {server}\n\n"
        f"✏️ **أرسل اسم الحساب الآن:**",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )
    
    user_steps[user_id] = {"step": "waiting_account_name"}

async def handle_auto_search(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    
    results = search_accounts(text)
    
    if not results:
        await update.message.reply_text(
            f"❌ **لم يتم العثور على نتائج للبحث:** `{text}`\n\n"
            f"💡 تأكد من أن الـ ID أو الاسم صحيح.\n"
            f"🔑 إذا كان هذا توكن، تأكد من إرساله بشكل صحيح.",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        return
    
    keyboard = []
    for acc in results:
        status_icon = get_account_status_icon(acc.get('has_pending', 0), acc.get('recovery_confirmed', 0))
        token_status_icon = get_token_status_icon(acc['token_status'])
        code = acc.get('security_code')
        code_text = f" 🔐{code[:3]}***" if code else ""
        btn_text = f"{status_icon}{token_status_icon} {acc['name']} | {acc['account_id']}{code_text}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"view_acc_{acc['id']}")])
    
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    
    await update.message.reply_text(
        f"🔍 **نتائج البحث عن:** `{text}`\n📋 عدد النتائج: {len(results)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_replace_token_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    acc_id = int(query.data.split("_")[3])
    
    pending = context.user_data.get('pending_token', {})
    token_text = pending.get('token')
    new_name = pending.get('name')
    account_id = pending.get('account_id')
    
    if not token_text or not new_name:
        await query.message.edit_text(
            "❌ حدث خطأ، يرجى المحاولة مرة أخرى.",
            reply_markup=main_menu_keyboard()
        )
        return
    
    replace_token_only(acc_id, token_text)
    update_account_name(acc_id, new_name)
    
    context.user_data.pop('pending_token', None)
    
    account = get_account(acc_id)
    await query.message.edit_text(
        f"✅ **تم استبدال التوكن وتحديث الاسم!**\n\n"
        f"👤 الاسم الجديد: {new_name}\n"
        f"🆔 ID: {account['account_id']}\n"
        f"🔑 **تم استبدال التوكن القديم بالجديد**\n"
        f"🔐 رمز الأمان: `{account.get('security_code', 'لا يوجد')}`\n\n"
        f"✅ يمكنك الآن استخدام الحساب بشكل طبيعي.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 عرض الحساب", callback_data=f"view_acc_{acc_id}")],
            [InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

async def handle_replace_token_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data.pop('pending_token', None)
    await query.message.edit_text(
        "❌ **تم إلغاء استبدال التوكن.**",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def handle_add_new_account_from_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    account_id = query.data.split("_")[3]
    
    pending = context.user_data.get('pending_token', {})
    token_text = pending.get('token')
    name = pending.get('name')
    server = pending.get('server')
    
    if not token_text or not name:
        await query.message.edit_text(
            "❌ حدث خطأ، يرجى المحاولة مرة أخرى.",
            reply_markup=main_menu_keyboard()
        )
        return
    
    context.user_data['manual_add_data'] = {
        'name': name,
        'account_id': account_id,
        'server': server or 'ME',
        'token': token_text,
        'token_status': 'VALID'
    }
    context.user_data['manual_add_step'] = "manual_recovery"
    context.user_data.pop('pending_token', None)
    
    await query.message.edit_text(
        f"✅ **تم التحقق من التوكن بنجاح!**\n\n"
        f"👤 الاسم: {name}\n"
        f"🆔 ID: {account_id}\n"
        f"🌍 السيرفر: {server or 'ME'}\n\n"
        f"📩 **أرسل الآن رسالة الاستعادة للحساب الجديد:**",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

# =========================================================
# 🚀 التشغيل
# =========================================================

async def post_init(application: Application):
    scheduler = AsyncIOScheduler(timezone=DEFAULT_TIMEZONE)
    scheduler.add_job(scheduled_token_monitor, 'interval', minutes=TOKEN_CHECK_INTERVAL_MINUTES, args=[application], next_run_time=datetime.now() + timedelta(seconds=30))
    scheduler.add_job(scheduled_notifications, 'interval', hours=1, args=[application])
    scheduler.start()
    logger.info(f"⏰ تم بدء المجدول - فحص التوكنات كل {TOKEN_CHECK_INTERVAL_MINUTES} دقائق")
    logger.info("🔔 نظام التنبيهات يعمل كل ساعة")
    
    cleanup_temp_files()
    logger.info("🧹 تم تنظيف الملفات المؤقتة عند بدء البوت")

async def set_commands(application: Application):
    commands = [
        ("start", "القائمة الرئيسية 🏠"),
        ("backup", "نسخ احتياطي 💾"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("✅ تم تعيين الأوامر")

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    text = update.message.text.strip()
    
    # ✅ 1. التحقق من حالة انتظار الاسم ورمز الأمان (الأولوية القصوى)
    if user_id in user_steps:
        step = user_steps[user_id].get("step")
        
        if step == "waiting_account_name":
            await handle_account_name(update, context)
            return
        elif step == "waiting_security_code":
            await handle_security_code(update, context)
            return
        elif step == "waiting_move_security_code":
            await handle_move_security_code(update, context)
            return
        elif step == "searching":
            await handle_search(update, context)
            return
        elif step == "waiting_board":
            await handle_board_message(update, context)
            return
        elif step == "waiting_token":
            await handle_board_message(update, context)
            return
        elif step == "waiting_recovery":
            await handle_board_message(update, context)
            return
        elif step == "waiting_new_token":
            await handle_new_token(update, context)
            return
        elif step == "waiting_move_recovery":
            await handle_move_recovery(update, context)
            return
        elif step == "add_token_to_account":
            await handle_add_token_to_account(update, context)
            return
        elif step == "waiting_edit_recovery":
            await handle_edit_recovery(update, context)
            return
        elif step == "waiting_backup_file":
            await update.message.reply_text("📤 **يرجى إرسال ملف ZIP أو JSON.**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]]))
            return
    
    # ✅ 2. التحقق من الإضافة اليدوية
    if context.user_data.get('manual_add_step') in ["manual_board", "manual_token", "manual_recovery"]:
        await handle_manual_add(update, context)
        return
    
    # ✅ 3. محاولة التعرف على النص كـ EAT Token
    if is_eat_token(text):
        await handle_token_input(update, context, text)
        return
    
    # ✅ 4. إذا كان النص أرقام فقط - بحث (ولكن بعد التأكد من عدم وجود حالة انتظار)
    if text.isdigit():
        await handle_auto_search(update, context, text)
        return
    
    # ✅ 5. إذا كان النص يحتوي على @ فهو بريد → بحث
    if '@' in text and '.' in text:
        await handle_auto_search(update, context, text)
        return
    
    # ✅ 6. إذا كان النص طويل جداً (أكثر من 40 حرف) → توكن
    if len(text) > 40:
        await handle_token_input(update, context, text)
        return
    
    # ✅ 7. بحث عام
    await handle_auto_search(update, context, text)

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    if user_id not in ADMIN_IDS:
        return
    data = query.data
    
    if data == "main_menu":
        user_steps.pop(user_id, None)
        context.user_data.pop('manual_add_data', None)
        context.user_data.pop('manual_add_step', None)
        context.user_data.pop('pending_token', None)
        context.user_data.pop('temp_token_data', None)
        await safe_show_main_menu(update, context)
    elif data == "menu_confirmed":
        await show_confirmed_menu(update, context)
    elif data == "menu_pending":
        await show_pending_menu(update, context)
    elif data == "menu_unconfirmed":
        await show_unconfirmed_menu(update, context)
    elif data.startswith("view_confirmed_"):
        await view_accounts_filtered(update, context)
    elif data.startswith("view_pending_"):
        await view_accounts_filtered(update, context)
    elif data.startswith("view_unconfirmed_"):
        await view_accounts_filtered(update, context)
    elif data == "pending_list":
        await show_pending_tokens(update, context)
    elif data.startswith("process_token_"):
        await process_token(update, context)
    elif data == "refresh_pending":
        await show_pending_tokens(update, context)
    elif data == "clear_pending":
        await clear_pending_tokens(update, context)
    elif data == "confirm_clear_pending":
        clear_all_pending()
        await query.message.edit_text("🗑️ **تم مسح جميع التوكنات المنتظرة.**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]]), parse_mode="Markdown")
    elif data == "add_manual":
        await start_manual_add(update, context)
    elif data == "skip_token":
        await handle_skip_token(update, context)
    elif data == "confirm_recovery":
        await handle_path_confirmed(update, context)
    elif data == "not_confirmed":
        await handle_path_not_confirmed(update, context)
    elif data.startswith("set_prime_"):
        await set_account_prime(update, context)
    elif data == "skip_photo":
        await handle_skip_photo(update, context)
    elif data == "skip_manual_photo":
        await handle_skip_photo(update, context)
    elif data.startswith("add_token_"):
        await add_token_to_account(update, context)
    elif data.startswith("view_acc_"):
        await view_account(update, context)
    elif data.startswith("recheck_token_"):
        await recheck_token(update, context)
    elif data.startswith("update_token_"):
        await start_token_update(update, context)
    elif data.startswith("move_to_confirmed_"):
        await start_move_to_confirmed(update, context)
    elif data.startswith("edit_recovery_"):
        await edit_recovery(update, context)
    elif data.startswith("archive_acc_"):
        await archive_account_handler(update, context)
    elif data.startswith("confirm_archive_"):
        await confirm_archive(update, context)
    elif data.startswith("delete_acc_"):
        await delete_account_handler(update, context)
    elif data.startswith("confirm_delete_"):
        await confirm_delete(update, context)
    elif data.startswith("restore_from_archive_"):
        await restore_from_archive_handler(update, context)
    elif data == "show_stats":
        await show_stats(update, context)
    elif data.startswith("stats_detail_"):
        await show_stats_detail(update, context)
    elif data == "search_menu":
        await show_search_menu(update, context)
    elif data == "show_archive":
        await show_archive(update, context)
    elif data == "create_backup":
        await create_backup(update, context)
    elif data == "restore_backup":
        await restore_backup_start(update, context)
    elif data == "restore_confirm":
        await confirm_restore(update, context)
    elif data.startswith("replace_token_confirm_"):
        await handle_replace_token_confirm(update, context)
    elif data == "replace_token_cancel":
        await handle_replace_token_cancel(update, context)
    elif data.startswith("add_new_account_"):
        await handle_add_new_account_from_token(update, context)

def main():
    os.makedirs("data", exist_ok=True)
    init_db()
    app = Application.builder().token(ADMIN_BOT_TOKEN).post_init(post_init).build()
    
    app.post_init = set_commands
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_IDS), handle_text_messages))
    app.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_IDS), handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(ADMIN_IDS), handle_backup_file))
    
    logger.info("🤖 البوت الإداري يعمل بنجاح...")
    logger.info(f"⏰ فحص التوكنات التلقائي كل {TOKEN_CHECK_INTERVAL_MINUTES} دقائق")
    logger.info("🔔 نظام التنبيهات يعمل كل ساعة")
    logger.info("📋 تصنيف الحسابات: مؤكدة ✅ | منتظرة ⏳ | غير مؤكدة ❌")
    logger.info("🔄 النقل التلقائي للمؤكد عند اكتمال الاستعادة مفعل")
    logger.info("🧠 النظام الذكي مفعل: أرسل توكن → يطلب الاسم")
    logger.info("🔐 نظام رمز الأمان (6 أرقام) مفعل")
    logger.info("✅ زر /start مفعل مع أوامر سريعة")
    logger.info("🧹 تنظيف الملفات المؤقتة التلقائي مفعل")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()