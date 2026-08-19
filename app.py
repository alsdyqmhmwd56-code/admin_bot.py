# -*- coding: utf-8 -*-
# vault_bot.py - بوت حفظ الحسابات المؤكدة (Backup Vault Bot)
# الإصدار 1.4 - مع عرض رمز الأمان

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

BOT_TOKEN = "8796691336:AAEFxcPG0RF92PrLiWaWkzwwkBbbzHYwb7Y"
ADMIN_IDS = [8587386123, 8612276675]

DB_NAME = "vault_accounts.db"
DEFAULT_TIMEZONE = pytz.UTC

# =========================================================
# 🗄️ قاعدة البيانات - مع إضافة security_code
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vault_accounts (
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
            recovery_confirmed INTEGER DEFAULT 1,
            token_status TEXT DEFAULT 'VALID',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            extra_data TEXT,
            security_code TEXT
        )
    """)
    
    # ✅ إضافة عمود security_code إذا لم يكن موجوداً
    try:
        cursor.execute("ALTER TABLE vault_accounts ADD COLUMN security_code TEXT")
        logger.info("✅ تم إضافة عمود security_code")
    except sqlite3.OperationalError:
        pass
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vault_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            details TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("✅ تم تهيئة قاعدة بيانات البوت الحافظ")

def add_vault_log(action: str, details: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO vault_logs (action, details) VALUES (?, ?)",
        (action, details)
    )
    conn.commit()
    conn.close()

# =========================================================
# 📥 دوال الحسابات - مع security_code
# =========================================================

def add_vault_account(account_data: Dict) -> int:
    """إضافة حساب مؤكد إلى قاعدة بيانات البوت الحافظ"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT id FROM vault_accounts WHERE account_id = ?",
        (account_data.get('account_id'),)
    )
    existing = cursor.fetchone()
    if existing:
        logger.info(f"⚠️ الحساب {account_data.get('account_id')} موجود مسبقاً، تم تخطيه")
        conn.close()
        return 0
    
    photo_id = account_data.get('photo_id')
    if photo_id:
        logger.info(f"ℹ️ تم تجاهل photo_id للحساب {account_data.get('account_id')}")
        photo_id = None
    
    security_code = account_data.get('security_code')
    
    cursor.execute("""
        INSERT INTO vault_accounts (
            name, account_id, server, prime_level, eat_token, photo_id,
            bound_email, pending_email, recovery_end_time, recovery_confirmed,
            token_status, extra_data, security_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        account_data.get('name', 'غير معروف'),
        account_data.get('account_id', ''),
        account_data.get('server', 'ME'),
        account_data.get('prime_level', 1),
        account_data.get('eat_token'),
        photo_id,
        account_data.get('bound_email', 'لا توجد'),
        account_data.get('pending_email', 'لا توجد'),
        account_data.get('recovery_end_time'),
        1,
        account_data.get('token_status', 'VALID'),
        json.dumps(account_data.get('extra_data', {})) if account_data.get('extra_data') else None,
        security_code
    ))
    conn.commit()
    acc_id = cursor.lastrowid
    conn.close()
    add_vault_log("ACCOUNT_ADDED", f"تم إضافة الحساب {account_data.get('name')} (ID: {account_data.get('account_id')})")
    return acc_id

def clear_all_vault_accounts() -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM vault_accounts")
    count = cursor.fetchone()[0]
    cursor.execute("DELETE FROM vault_accounts")
    conn.commit()
    conn.close()
    add_vault_log("ALL_ACCOUNTS_CLEARED", f"تم مسح جميع الحسابات ({count} حساب)")
    return count

def get_all_vault_accounts() -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, account_id, server, prime_level, eat_token, photo_id,
               bound_email, pending_email, recovery_end_time, recovery_confirmed,
               token_status, created_at, updated_at, extra_data, security_code
        FROM vault_accounts ORDER BY prime_level ASC, name ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "account_id": row[2],
            "server": row[3],
            "prime_level": row[4],
            "eat_token": row[5] or "غير متوفر",
            "photo_id": row[6],
            "bound_email": row[7] or "لا توجد",
            "pending_email": row[8] or "لا توجد",
            "recovery_end_time": row[9],
            "recovery_confirmed": row[10],
            "token_status": row[11] or "VALID",
            "created_at": row[12],
            "updated_at": row[13],
            "extra_data": json.loads(row[14]) if row[14] else {},
            "security_code": row[15]
        }
        for row in rows
    ]

def get_vault_account(acc_id: int) -> Optional[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, account_id, server, prime_level, eat_token, photo_id,
               bound_email, pending_email, recovery_end_time, recovery_confirmed,
               token_status, created_at, updated_at, extra_data, security_code
        FROM vault_accounts WHERE id = ?
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
            "eat_token": row[5] or "غير متوفر",
            "photo_id": row[6],
            "bound_email": row[7] or "لا توجد",
            "pending_email": row[8] or "لا توجد",
            "recovery_end_time": row[9],
            "recovery_confirmed": row[10],
            "token_status": row[11] or "VALID",
            "created_at": row[12],
            "updated_at": row[13],
            "extra_data": json.loads(row[14]) if row[14] else {},
            "security_code": row[15]
        }
    return None

def search_vault_accounts(query: str) -> List[Dict]:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, name, account_id, server, prime_level, token_status, security_code
        FROM vault_accounts 
        WHERE (account_id LIKE ? OR name LIKE ? OR bound_email LIKE ? OR pending_email LIKE ? OR security_code LIKE ?)
        ORDER BY prime_level ASC, name ASC
    """, (
        f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%"
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
            "token_status": row[5] or "VALID",
            "security_code": row[6]
        }
        for row in rows
    ]

def get_vault_stats() -> Dict:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM vault_accounts")
    total = cursor.fetchone()[0]
    
    prime_stats = {}
    for p in range(1, 9):
        cursor.execute(
            "SELECT COUNT(*) FROM vault_accounts WHERE prime_level = ?",
            (p,)
        )
        prime_stats[p] = cursor.fetchone()[0]
    
    conn.close()
    return {
        "total": total,
        "prime_stats": prime_stats
    }

def delete_vault_account(acc_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM vault_accounts WHERE id = ?", (acc_id,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    if affected > 0:
        add_vault_log("ACCOUNT_DELETED", f"تم حذف حساب برقم {acc_id}")
        return True
    return False

# =========================================================
# 📥 دوال استيراد النسخ الاحتياطية - مع security_code
# =========================================================

def import_confirmed_accounts_from_backup(backup_data: Dict) -> Dict:
    """استيراد الحسابات المؤكدة فقط من النسخة الاحتياطية"""
    accounts = backup_data.get('accounts', [])
    confirmed_accounts = [acc for acc in accounts if acc.get('recovery_confirmed') == 1]
    
    added = 0
    skipped = 0
    
    for acc in confirmed_accounts:
        acc.pop('id', None)
        acc.pop('is_archived', None)
        acc.pop('is_completed', None)
        acc.pop('has_pending', None)
        acc.pop('last_token_check', None)
        acc.pop('updated_at', None)
        acc.pop('created_at', None)
        acc.pop('photo_id', None)
        # ✅ احتفظ بـ security_code
        security_code = acc.get('security_code')
        
        result = add_vault_account(acc)
        if result > 0:
            added += 1
        else:
            skipped += 1
    
    return {
        "total_confirmed": len(confirmed_accounts),
        "added": added,
        "skipped": skipped
    }

# =========================================================
# 📱 القوائم والأزرار
# =========================================================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 عرض الحسابات المؤكدة", callback_data="menu_confirmed")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="show_stats")],
        [InlineKeyboardButton("🔍 بحث", callback_data="search_menu")],
        [InlineKeyboardButton("📤 استقبال نسخة احتياطية", callback_data="receive_backup")],
        [InlineKeyboardButton("🗑️ مسح جميع الحسابات", callback_data="clear_all_start")],
        [InlineKeyboardButton("💾 تصدير الحسابات", callback_data="export_backup")]
    ])

def get_prime_keyboard():
    keyboard = []
    for p in range(1, 9):
        emoji = get_prime_emoji(p)
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} Prime {p}",
                callback_data=f"view_prime_{p}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_prime_emoji(prime: int) -> str:
    emojis = {
        1: "🥉", 2: "🥉", 3: "🥉",
        4: "🥈", 5: "🥈",
        6: "🏆", 7: "👑", 8: "💠"
    }
    return emojis.get(prime, "⭐")

def get_token_status_icon(status: str) -> str:
    if status == "VALID":
        return "🟢"
    elif status == "EXPIRED":
        return "🔴"
    else:
        return "⚪"

def get_token_status_text(status: str) -> str:
    if status == "VALID":
        return "صالح ✅"
    elif status == "EXPIRED":
        return "محروق 🔴"
    else:
        return "غير معروف ⚪"

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
# 🎮 معالجات البوت
# =========================================================

user_steps = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ عذراً، هذا البوت خاص بالأدمن فقط.")
        return
    
    await update.message.reply_text(
        "🏦 **بوت حفظ الحسابات المؤكدة**\n\n"
        "📌 هذا البوت مخصص لحفظ الحسابات المؤكدة فقط.\n\n"
        "📤 **كيف يعمل؟**\n"
        "• استقبل نسخة احتياطية من البوت الإداري (ملف ZIP)\n"
        "• البوت يستخرج الحسابات المؤكدة ويحفظها\n"
        "• يمكنك عرضها، البحث فيها، أو مسحها\n\n"
        "📋 استخدم القائمة أدناه:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

async def safe_show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="🏦 **القائمة الرئيسية:**",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

# =========================================================
# 📋 عرض الحسابات المؤكدة - مع رمز الأمان
# =========================================================

async def show_confirmed_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = []
    for p in range(1, 9):
        accounts = get_all_vault_accounts()
        count = len([acc for acc in accounts if acc['prime_level'] == p])
        emoji = get_prime_emoji(p)
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} Prime {p} ({count})",
                callback_data=f"view_prime_{p}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    await query.message.edit_text(
        "📋 **الحسابات المؤكدة المحفوظة**\n\nاختر Prime لعرض الحسابات:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def view_prime_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    prime = int(query.data.split("_")[2])
    
    all_accounts = get_all_vault_accounts()
    accounts = [acc for acc in all_accounts if acc['prime_level'] == prime]
    
    if not accounts:
        await query.message.edit_text(
            f"📭 **لا توجد حسابات في Prime {prime}**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="menu_confirmed")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    keyboard = []
    for acc in accounts:
        status_icon = get_token_status_icon(acc['token_status'])
        code = acc.get('security_code')
        code_text = f" 🔐{code}" if code else ""
        keyboard.append([
            InlineKeyboardButton(
                f"{status_icon} {acc['name']} | {acc['account_id']}{code_text}",
                callback_data=f"view_acc_{acc['id']}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_confirmed")])
    
    await query.message.edit_text(
        f"📁 **Prime {prime} - ({len(accounts)})**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def view_account_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[2])
    account = get_vault_account(acc_id)
    
    if not account:
        await query.message.edit_text("❌ الحساب غير موجود.")
        return
    
    status_icon = get_token_status_icon(account['token_status'])
    status_text = get_token_status_text(account['token_status'])
    time_left = format_time_remaining(account['recovery_end_time'])
    security_code = account.get('security_code')
    code_text = f"\n🔐 **رمز الأمان:** `{security_code}`" if security_code else ""
    
    text = (
        f"⚙️ **تفاصيل الحساب (مؤكد)**\n\n"
        f"👤 **الاسم:** {account['name']}\n"
        f"🆔 **ID:** `{account['account_id']}`\n"
        f"🌍 **السيرفر:** {account['server']}\n"
        f"🏆 **Prime:** {account['prime_level']} {get_prime_emoji(account['prime_level'])}\n\n"
        f"🔑 **التوكن:** {status_icon} {status_text}\n"
        f"📧 **البريد الحالي:** `{account['bound_email']}`\n"
        f"📧 **البريد الجديد:** `{account['pending_email']}`\n"
        f"⏱ **المدة المتبقية:** {time_left}{code_text}\n\n"
        f"📅 **تاريخ الإضافة:** {account['created_at']}\n"
        f"🔄 **آخر تحديث:** {account['updated_at']}"
    )
    
    keyboard = [
        [InlineKeyboardButton("🗑️ حذف هذا الحساب", callback_data=f"delete_acc_{acc_id}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu_confirmed")]
    ]
    
    photo_id = account.get('photo_id')
    if photo_id:
        try:
            await query.message.delete()
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo_id,
                caption=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            logger.info(f"✅ تم عرض الصورة للحساب {account['name']}")
            return
        except Exception as e:
            logger.warning(f"⚠️ فشل إرسال الصورة (photo_id غير صالح): {e}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text + "\n\n⚠️ **الصورة غير متوفرة (من بوت آخر).**",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return
    
    await safe_edit_or_send(
        query,
        text + "\n\n📸 **لا توجد صورة محفوظة لهذا الحساب.**",
        InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def delete_single_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[2])
    account = get_vault_account(acc_id)
    
    if not account:
        await query.message.edit_text("❌ الحساب غير موجود.")
        return
    
    keyboard = [
        [InlineKeyboardButton("✅ نعم، احذف", callback_data=f"confirm_delete_acc_{acc_id}")],
        [InlineKeyboardButton("❌ إلغاء", callback_data=f"view_acc_{acc_id}")]
    ]
    
    await safe_edit_or_send(
        query,
        f"⚠️ **حذف حساب**\n\n"
        f"👤 الحساب: {account['name']}\n"
        f"🆔 ID: {account['account_id']}\n"
        f"🔐 رمز الأمان: `{account.get('security_code', 'لا يوجد')}`\n\n"
        f"هل أنت متأكد من حذف هذا الحساب؟",
        InlineKeyboardMarkup(keyboard)
    )

async def confirm_delete_single(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    acc_id = int(query.data.split("_")[3])
    delete_vault_account(acc_id)
    await safe_edit_or_send(
        query,
        "🗑️ **تم حذف الحساب بنجاح.**",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_confirmed")]
        ])
    )

# =========================================================
# 📊 الإحصائيات
# =========================================================

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    stats = get_vault_stats()
    
    text = (
        f"📊 **إحصائيات البوت الحافظ**\n\n"
        f"📦 **إجمالي الحسابات المؤكدة المحفوظة:** {stats['total']}\n\n"
        f"🏆 **توزيع Prime:**\n"
    )
    for p in range(1, 9):
        emoji = get_prime_emoji(p)
        count = stats['prime_stats'].get(p, 0)
        text += f"└── {emoji} Prime {p}: {count}\n"
    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

# =========================================================
# 🔍 البحث الذكي - مع رمز الأمان
# =========================================================

async def show_search_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_steps[user_id] = {"step": "searching"}
    
    await query.message.edit_text(
        "🔍 **البحث**\n\n"
        "أرسل ID أو اسم أو بريد أو رمز أمان للبحث:\n"
        "• ID الحساب\n"
        "• اسم الحساب\n"
        "• البريد الإلكتروني\n"
        "• رمز الأمان (6 أرقام)\n\n"
        "💡 يمكنك أيضاً إرسال ID أو اسم مباشرة في الشات",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    if user_steps[user_id].get("step") != "searching":
        return
    
    query = update.message.text.strip()
    results = search_vault_accounts(query)
    
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
        status_icon = get_token_status_icon(acc['token_status'])
        code = acc.get('security_code')
        code_text = f" 🔐{code}" if code else ""
        keyboard.append([
            InlineKeyboardButton(
                f"{status_icon} {acc['name']} | {acc['account_id']}{code_text}",
                callback_data=f"view_acc_{acc['id']}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔍 بحث جديد", callback_data="search_menu")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    user_steps.pop(user_id, None)
    
    await update.message.reply_text(
        f"🔍 **نتائج البحث عن:** `{query}`\n📋 عدد النتائج: {len(results)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def auto_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بحث تلقائي عند إرسال نص في الشات"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    
    text = update.message.text.strip()
    
    # ✅ التحقق من وجود عملية جارية
    if user_id in user_steps and user_steps[user_id].get("step") == "waiting_backup":
        await update.message.reply_text(
            "📤 **يرجى إرسال ملف ZIP.**\n\n"
            "استخدم زر '📤 استقبال نسخة احتياطية' من القائمة، ثم أرسل الملف.",
            reply_markup=main_menu_keyboard()
        )
        return
    
    results = search_vault_accounts(text)
    
    if not results:
        await update.message.reply_text(
            f"❌ **لم يتم العثور على نتائج للبحث:** `{text}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 بحث جديد", callback_data="search_menu")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    keyboard = []
    for acc in results:
        status_icon = get_token_status_icon(acc['token_status'])
        code = acc.get('security_code')
        code_text = f" 🔐{code}" if code else ""
        keyboard.append([
            InlineKeyboardButton(
                f"{status_icon} {acc['name']} | {acc['account_id']}{code_text}",
                callback_data=f"view_acc_{acc['id']}"
            )
        ])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    
    await update.message.reply_text(
        f"🔍 **نتائج البحث عن:** `{text}`\n📋 عدد النتائج: {len(results)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# =========================================================
# 📤 استقبال النسخة الاحتياطية
# =========================================================

async def receive_backup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_steps[user_id] = {"step": "waiting_backup"}
    
    await query.message.edit_text(
        "📤 **استقبال نسخة احتياطية**\n\n"
        "📌 أرسل ملف ZIP الذي يحتوي على النسخة الاحتياطية من البوت الإداري.\n\n"
        "⚠️ سيتم استخراج **الحسابات المؤكدة فقط** وحفظها في قاعدة البيانات.\n"
        "🔄 الحسابات المكررة سيتم تجاهلها تلقائياً.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 إلغاء", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

async def handle_backup_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or user_id not in user_steps:
        return
    if user_steps[user_id].get("step") != "waiting_backup":
        return
    
    if not update.message.document:
        await update.message.reply_text("❌ يرجى إرسال ملف ZIP.")
        return
    
    document = update.message.document
    file_name = document.file_name
    
    if not file_name.endswith('.zip'):
        await update.message.reply_text("❌ يرجى إرسال ملف ZIP صالح.")
        return
    
    msg = await update.message.reply_text("⏳ **جاري معالجة الملف...**", parse_mode="Markdown")
    
    try:
        file = await context.bot.get_file(document.file_id)
        temp_file = f"temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        await file.download_to_drive(temp_file)
        
        extract_dir = f"extract_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        os.makedirs(extract_dir, exist_ok=True)
        
        with zipfile.ZipFile(temp_file, 'r') as zipf:
            zipf.extractall(extract_dir)
        
        json_files = [f for f in os.listdir(extract_dir) if f.endswith('.json')]
        if not json_files:
            raise Exception("لم يتم العثور على ملف JSON في النسخة الاحتياطية")
        
        json_file = os.path.join(extract_dir, json_files[0])
        
        with open(json_file, 'r', encoding='utf-8') as f:
            backup_data = json.load(f)
        
        if 'backup_meta' not in backup_data:
            raise Exception("ملف النسخة غير صالح")
        
        result = import_confirmed_accounts_from_backup(backup_data)
        
        os.remove(temp_file)
        shutil.rmtree(extract_dir)
        
        user_steps.pop(user_id, None)
        
        await msg.edit_text(
            f"✅ **تم استيراد النسخة الاحتياطية بنجاح!**\n\n"
            f"📊 إجمالي الحسابات المؤكدة في الملف: {result['total_confirmed']}\n"
            f"✅ تم إضافة: {result['added']}\n"
            f"⏭️ تم تخطي (مكرر): {result['skipped']}\n\n"
            f"📌 يمكنك الآن عرض الحسابات من القائمة الرئيسية.",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await msg.edit_text(f"❌ **خطأ:** `{str(e)}`", parse_mode="Markdown")
        logger.error(f"❌ خطأ في استقبال النسخة: {e}")

# =========================================================
# 🗑️ مسح جميع الحسابات (مع تحذيرات)
# =========================================================

async def clear_all_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    stats = get_vault_stats()
    
    if stats['total'] == 0:
        await query.message.edit_text(
            "📭 **لا توجد حسابات محفوظة للمسح.**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    keyboard = [
        [InlineKeyboardButton("⚠️ نعم، أريد المسح", callback_data="clear_all_step2")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
    ]
    
    await query.message.edit_text(
        f"🚨 **تحذير: مسح جميع الحسابات**\n\n"
        f"📦 عدد الحسابات المحفوظة: **{stats['total']}**\n\n"
        f"⚠️ هذا الإجراء **لا يمكن التراجع عنه**.\n\n"
        f"هل أنت متأكد من رغبتك في مسح جميع الحسابات؟",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def clear_all_step2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    stats = get_vault_stats()
    
    keyboard = [
        [InlineKeyboardButton("🚨 نعم، مسح الكل نهائياً", callback_data="clear_all_final")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
    ]
    
    await query.message.edit_text(
        f"🚨 **تأكيد نهائي: مسح جميع الحسابات**\n\n"
        f"📦 عدد الحسابات التي سيتم حذفها: **{stats['total']}**\n\n"
        f"⚠️ **هذا هو التحذير الأخير.**\n"
        f"⚠️ بعد المسح، لا يمكن استعادة الحسابات إلا بإرسال نسخة احتياطية جديدة.\n\n"
        f"هل أنت متأكد تماماً؟",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def clear_all_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    count = clear_all_vault_accounts()
    
    await query.message.edit_text(
        f"🗑️ **تم مسح جميع الحسابات بنجاح!**\n\n"
        f"📦 عدد الحسابات المحذوفة: **{count}**",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
        ]),
        parse_mode="Markdown"
    )

# =========================================================
# 💾 تصدير الحسابات - مع رمز الأمان
# =========================================================

async def export_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.edit_text("⏳ **جاري تصدير الحسابات...**", parse_mode="Markdown")
    
    try:
        accounts = get_all_vault_accounts()
        
        export_data = {
            "backup_meta": {
                "version": "1.0",
                "created_at": datetime.now().isoformat(),
                "total_accounts": len(accounts),
                "source": "Vault Bot (حافظ الحسابات المؤكدة)"
            },
            "accounts": accounts,
            "vault_export": True
        }
        
        filename = f"vault_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2, default=str)
        
        zip_filename = filename.replace('.json', '.zip')
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(filename)
        
        with open(zip_filename, 'rb') as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=zip_filename,
                caption=f"💾 **تصدير الحسابات المؤكدة**\n\n📅 التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n📦 عدد الحسابات: {len(accounts)}"
            )
        
        os.remove(filename)
        os.remove(zip_filename)
        
        await query.message.edit_text(
            "✅ **تم تصدير الحسابات وإرسالها!**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await query.message.edit_text(f"❌ **خطأ:** `{str(e)}`", parse_mode="Markdown")

# =========================================================
# 🎯 معالج الأزرار
# =========================================================

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    
    if user_id not in ADMIN_IDS:
        await query.message.edit_text("⛔ عذراً، هذا البوت خاص بالأدمن فقط.")
        return
    
    data = query.data
    
    if data == "main_menu":
        user_steps.pop(user_id, None)
        await safe_show_main_menu(update, context)
    elif data == "menu_confirmed":
        await show_confirmed_menu(update, context)
    elif data.startswith("view_prime_"):
        await view_prime_accounts(update, context)
    elif data.startswith("view_acc_"):
        await view_account_details(update, context)
    elif data.startswith("delete_acc_"):
        await delete_single_account(update, context)
    elif data.startswith("confirm_delete_acc_"):
        await confirm_delete_single(update, context)
    elif data == "show_stats":
        await show_stats(update, context)
    elif data == "search_menu":
        await show_search_menu(update, context)
    elif data == "receive_backup":
        await receive_backup_start(update, context)
    elif data == "clear_all_start":
        await clear_all_start(update, context)
    elif data == "clear_all_step2":
        await clear_all_step2(update, context)
    elif data == "clear_all_final":
        await clear_all_final(update, context)
    elif data == "export_backup":
        await export_backup(update, context)

# =========================================================
# 📝 معالج الرسائل
# =========================================================

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    
    # ✅ التحقق من حالة انتظار الملف
    if user_id in user_steps:
        step = user_steps[user_id].get("step")
        if step == "waiting_backup":
            await update.message.reply_text(
                "📤 **يرجى إرسال ملف ZIP.**\n\n"
                "استخدم زر '📤 استقبال نسخة احتياطية' من القائمة، ثم أرسل الملف.",
                reply_markup=main_menu_keyboard()
            )
            return
        elif step == "searching":
            await handle_search(update, context)
            return
    
    # ✅ بحث تلقائي
    await auto_search(update, context)

# =========================================================
# 🚀 التشغيل
# =========================================================

async def post_init(application: Application):
    commands = [
        ("start", "القائمة الرئيسية 🏦"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("✅ تم تعيين الأوامر")
    
    # ✅ تنظيف الملفات المؤقتة عند بدء البوت
    cleanup_temp_files()
    logger.info("🧹 تم تنظيف الملفات المؤقتة عند بدء البوت")

def cleanup_temp_files():
    """حذف الملفات المؤقتة تلقائياً"""
    try:
        for f in os.listdir('.'):
            if f.startswith('temp_') and f.endswith('.zip'):
                os.remove(f)
                logger.info(f"🧹 تم حذف الملف المؤقت: {f}")
        
        for d in os.listdir('.'):
            if d.startswith('extract_') and os.path.isdir(d):
                shutil.rmtree(d)
                logger.info(f"🧹 تم حذف المجلد المؤقت: {d}")
        
        logger.info("🧹 تم تنظيف الملفات المؤقتة بنجاح")
    except Exception as e:
        logger.error(f"❌ خطأ في تنظيف الملفات: {e}")

def main():
    os.makedirs("data", exist_ok=True)
    init_db()
    
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_IDS), handle_text_messages))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.User(ADMIN_IDS), handle_backup_file))
    
    logger.info("🏦 بوت حفظ الحسابات المؤكدة يعمل بنجاح...")
    logger.info(f"👥 الأدمن المسموح لهم: {ADMIN_IDS}")
    logger.info("🔐 نظام رمز الأمان (6 أرقام) مفعل")
    logger.info("📤 استقبال النسخ الاحتياطية من البوت الإداري")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
