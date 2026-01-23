import os
# Set environment variables to disable proxy before importing supabase
# This prevents httpx.Client from receiving 'proxy' argument which is not supported in newer httpx versions
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("HTTPX_NO_PROXY", "1")

# Configure logging - only errors and warnings in production
import logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING
)
logger = logging.getLogger(__name__)

# Try to monkeypatch httpx.Client to intercept proxy argument (silent in production)
try:
    import httpx
    
    # Store original Client.__init__
    original_httpx_client_init = httpx.Client.__init__
    
    def patched_httpx_client_init(self, *args, **kwargs):
        """Patched httpx.Client.__init__ to remove proxy argument"""
        if 'proxy' in kwargs:
            kwargs.pop('proxy')
        return original_httpx_client_init(self, *args, **kwargs)
    
    # Apply monkeypatch
    httpx.Client.__init__ = patched_httpx_client_init
except Exception:
    pass

from datetime import datetime
import time
import signal
import sys
import threading
import asyncio
import uuid
from functools import wraps
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from telegram.error import TimedOut, NetworkError, RetryAfter
from telegram.request import HTTPXRequest
import io

from supabase import create_client, Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Initialize Flask app
app = Flask(__name__)

# Environment variables - all required except PORT (set by Koyeb)
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
# Service role key for Storage operations (bypasses RLS)
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
TABLE_NAME = os.environ.get('TABLE_NAME', 'facebook_leads')  # Default table name
PORT = int(os.environ.get('PORT', 8000))  # Default port, usually set by Koyeb

# Photo upload configuration
SUPABASE_LEADS_BUCKET = os.environ.get('SUPABASE_LEADS_BUCKET', 'Leads')  # Supabase Storage bucket name
ENABLE_LEAD_PHOTOS = os.environ.get('ENABLE_LEAD_PHOTOS', 'true').lower() == 'true'  # Enable/disable photo uploads

# Supabase client - thread-safe, can be used concurrently by multiple users
supabase: Client = None
# Separate client for Storage operations (uses service_role key to bypass RLS)
supabase_storage: Client = None

# Keep-alive scheduler
scheduler = None

# Cache for uniqueness checks (TTL: 5 minutes)
uniqueness_cache = {}
CACHE_TTL = 300  # 5 minutes in seconds

# Graceful shutdown flag
shutdown_requested = False

def retry_supabase_query(max_retries=3, delay=1, backoff=2):
    """Decorator for retrying Supabase queries with exponential backoff"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    error_msg = str(e).lower()
                    
                    # Only retry on network/temporary errors
                    if any(keyword in error_msg for keyword in ['timeout', 'connection', 'network', 'temporary', '503', '502', '504']):
                        if attempt < max_retries - 1:
                            logger.warning(f"Supabase query failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {current_delay}s...")
                            time.sleep(current_delay)
                            current_delay *= backoff
                        else:
                            logger.error(f"Supabase query failed after {max_retries} attempts: {e}")
                    else:
                        # Non-retryable error, raise immediately
                        raise
            
            # If all retries failed, raise the last exception
            raise last_exception
        return wrapper
    return decorator

async def retry_telegram_api(func, max_retries=3, delay=1, backoff=2, *args, **kwargs):
    """Retry Telegram API calls with exponential backoff"""
    last_exception = None
    current_delay = delay
    
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except (TimedOut, NetworkError) as e:
            last_exception = e
            if attempt < max_retries - 1:
                logger.warning(f"Telegram API call failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {current_delay}s...")
                await asyncio.sleep(current_delay)
                current_delay *= backoff
            else:
                logger.error(f"Telegram API call failed after {max_retries} attempts: {e}")
        except RetryAfter as e:
            # Telegram rate limit - wait for the specified time
            wait_time = e.retry_after
            logger.warning(f"Rate limited by Telegram. Waiting {wait_time}s...")
            await asyncio.sleep(wait_time)
            # Retry once more after rate limit
            if attempt < max_retries - 1:
                return await func(*args, **kwargs)
            raise
    
    raise last_exception

def get_supabase_client():
    """Initialize and return Supabase client (uses anon key)"""
    global supabase
    if supabase is None:
        try:
            if not SUPABASE_KEY:
                logger.error("SUPABASE_KEY not found in environment variables")
                return None
            
            if not SUPABASE_URL:
                logger.error("SUPABASE_URL not found in environment variables")
                return None
            
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as e:
            logger.error(f"Error initializing Supabase client: {e}", exc_info=True)
            return None
    return supabase

def get_supabase_storage_client():
    """Initialize and return Supabase client for Storage operations (uses service_role key to bypass RLS)"""
    global supabase_storage
    if supabase_storage is None:
        try:
            if not SUPABASE_SERVICE_ROLE_KEY:
                logger.warning("SUPABASE_SERVICE_ROLE_KEY not found, falling back to SUPABASE_KEY for Storage operations")
                # Fallback to regular key if service_role not provided
                return get_supabase_client()
            
            if not SUPABASE_URL:
                logger.error("SUPABASE_URL not found in environment variables")
                return None
            
            supabase_storage = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
            logger.info("[STORAGE] Initialized Supabase Storage client with service_role key")
        except Exception as e:
            logger.error(f"Error initializing Supabase Storage client: {e}", exc_info=True)
            # Fallback to regular client
            return get_supabase_client()
    return supabase_storage

def normalize_telegram_id(tg_id: str) -> str:
    """Normalize Telegram ID: extract only digits (similar to phone)"""
    if not tg_id:
        return ""
    # Remove all non-digit characters
    return ''.join(filter(str.isdigit, tg_id))

def normalize_tag(tag: str) -> str:
    """Normalize tag: handle three formats and return format 3 (username without @ and without https://t.me/)
    
    Accepts:
    1. Full URL: https://t.me/marklindt -> marklindt
    2. With @: @marklindt -> marklindt
    3. Without @: marklindt -> marklindt
    
    Returns: username without @ and without https://t.me/ prefix
    """
    if not tag:
        return ""
    
    # Trim spaces
    normalized = tag.strip()
    
    # Handle full Telegram URL format: https://t.me/username
    if normalized.startswith('https://t.me/'):
        # Extract username after https://t.me/
        normalized = normalized.replace('https://t.me/', '').strip()
    elif normalized.startswith('http://t.me/'):
        # Handle http:// variant
        normalized = normalized.replace('http://t.me/', '').strip()
    elif normalized.startswith('t.me/'):
        # Handle t.me/ variant
        normalized = normalized.replace('t.me/', '').strip()
    
    # Remove @ symbol if present
    normalized = normalized.replace('@', '').strip()
    
    # Remove any trailing slashes or query parameters
    if '/' in normalized:
        normalized = normalized.split('/')[0]
    if '?' in normalized:
        normalized = normalized.split('?')[0]
    
    return normalized

def normalize_text_field(text: str) -> str:
    """Normalize text field (fullname, manager_name): trim spaces, collapse multiple spaces, limit length"""
    if not text:
        return ""
    # Trim leading/trailing whitespace
    normalized = text.strip()
    # Collapse multiple spaces into single space
    normalized = ' '.join(normalized.split())
    # Remove any control characters (keep only printable characters)
    normalized = ''.join(char for char in normalized if char.isprintable() or char.isspace())
    # Final trim after cleaning
    normalized = normalized.strip()
    # Limit to 500 characters to prevent database issues
    if len(normalized) > 500:
        normalized = normalized[:500]
    return normalized

def escape_html(text: str) -> str:
    """Escape HTML special characters"""
    if not text:
        return text
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_facebook_link_for_display(value: str) -> str:
    """Format Facebook link value to full URL for display
    
    Args:
        value: Facebook link value from database (can be ID or username)
    
    Returns:
        Formatted Facebook URL with https://
    """
    if not value:
        return value
    
    value = str(value).strip()
    
    # If already a full URL, return as is
    if value.startswith('http://') or value.startswith('https://'):
        return value
    
    # Check if value is only digits (Facebook ID)
    if value.isdigit():
        # Format as profile.php?id=...
        return f"https://www.facebook.com/profile.php?id={value}"
    else:
        # Format as username link
        return f"https://www.facebook.com/{value}"

def get_user_friendly_error(error: Exception, operation: str = "операция") -> str:
    """Convert technical errors to user-friendly messages"""
    error_str = str(error).lower()
    
    # Database connection errors
    if 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
        return (
            f"⚠️ Проблема с подключением к базе данных.\n\n"
            f"ℹ️ Что можно сделать:\n"
            f"• Проверьте интернет-соединение\n"
            f"• Попробуйте через несколько секунд\n"
            f"• Если проблема сохраняется, обратитесь к администратору"
        )
    
    # Database query errors
    if 'postgres' in error_str or 'database' in error_str or 'query' in error_str:
        return (
            f"⚠️ Ошибка при выполнении запроса к базе данных.\n\n"
            f"ℹ️ Попробуйте:\n"
            f"• Повторить операцию\n"
            f"• Проверить правильность введенных данных"
        )
    
    # Validation errors (already user-friendly)
    if 'не может быть пустым' in error_str or 'неверный формат' in error_str:
        return str(error)
    
    # Unknown errors
    return (
        f"❌ Произошла ошибка при {operation}.\n\n"
        f"ℹ️ Попробуйте:\n"
        f"• Повторить операцию\n"
        f"• Проверить введенные данные\n"
        f"• Обратиться к администратору, если проблема сохраняется"
    )

import re
from urllib.parse import urlparse, parse_qs

def validate_facebook_link(link: str) -> tuple[bool, str, str]:
    """
    Validate Facebook link and extract username or ID.
    Supports various Facebook URL formats:
    - https://www.facebook.com/profile.php?id=123456 → 123456 (only ID)
    - https://www.facebook.com/profile.php?id=123456&ref=... → 123456 (only ID)
    - https://www.facebook.com/markl1n → markl1n
    - www.facebook.com/markl1n → markl1n
    - facebook.com/markl1n → markl1n
    - https://www.facebook.com/profile/username → username
    - https://m.facebook.com/username → username
    - m.facebook.com/username → username
    """
    if not link:
        return False, "Facebook ссылка не может быть пустой", ""
    
    link_clean = link.strip()
    
    # Remove @ if present at the beginning
    if link_clean.startswith('@'):
        link_clean = link_clean[1:]
    
    # Check if it's a valid Facebook URL - support more formats
    # Accept: https://www.facebook.com/..., http://www.facebook.com/..., 
    # www.facebook.com/..., facebook.com/..., m.facebook.com/...
    facebook_patterns = [
        r'https?://(www\.)?(m\.)?facebook\.com/',
        r'^(www\.)?facebook\.com/',
        r'^m\.facebook\.com/'
    ]
    
    is_facebook_url = False
    for pattern in facebook_patterns:
        if re.search(pattern, link_clean, re.IGNORECASE):
            is_facebook_url = True
            break
    
    # Also check for formats without protocol explicitly
    if not is_facebook_url:
        link_lower = link_clean.lower()
        if (link_lower.startswith('www.facebook.com/') or 
            link_lower.startswith('facebook.com/') or 
            link_lower.startswith('m.facebook.com/')):
            is_facebook_url = True
    
    if not is_facebook_url:
        error_msg = (
            "❌ Неверный формат Facebook ссылки.\n\n"
            "📋 Примеры допустимых вариантов:\n"
            "• <code>https://www.facebook.com/username</code>\n"
            "• <code>www.facebook.com/username</code>\n"
            "• <code>facebook.com/username</code>\n"
            "• <code>https://m.facebook.com/profile.php?id=123456789012345</code>\n\n"
            "💡 Можно вставлять ссылку целиком, бот сам извлечёт username или ID."
        )
        return False, error_msg, ""
    
    # Remove http:// or https:// if present
    if link_clean.startswith('http://'):
        link_clean = link_clean[7:]
    elif link_clean.startswith('https://'):
        link_clean = link_clean[8:]
    
    # Remove www. if present
    if link_clean.startswith('www.'):
        link_clean = link_clean[4:]
    
    # Remove facebook.com/ or m.facebook.com/ if present
    if link_clean.lower().startswith('facebook.com/'):
        link_clean = link_clean[13:]
    elif link_clean.lower().startswith('m.facebook.com/'):
        link_clean = link_clean[15:]
    
    # Handle profile.php?id= format or any link with id= parameter - extract ONLY the ID number
    if 'id=' in link_clean:
        # Extract ID from query string - look for id= parameter
        # Handle cases like:
        # - profile.php?id=123456
        # - profile.php?id=123456&ref=...
        # - profile.php?id=123456] (with trailing characters)
        # - profile.php?id=123456/extra/path (with additional paths)
        # - ?id=123456 (without profile.php)
        # - /people/Name/123456 (alternative format)
        
        # Extract everything after id=
        id_part_raw = link_clean.split('id=')[-1]
        
        # Remove query parameters (&), hash fragments (#), and any trailing characters
        # Extract only the numeric ID part, ignoring any non-digit characters after it
        id_part = ""
        for char in id_part_raw:
            if char.isdigit():
                id_part += char
            elif char in ['&', '#', '?', '/', '\\', ']', '[', ')', '(', '}', '{', ' ', '\t', '\n']:
                # Stop at first non-digit separator character
                break
            else:
                # If we encounter a non-digit, non-separator character, it might be part of the ID
                # But typically Facebook IDs are only digits, so we stop here
                break
        
        # Also check for alternative format: /people/Name/123456
        if not id_part or not id_part.isdigit():
            # Try to extract ID from path like /people/Name/123456
            path_parts = link_clean.split('/')
            for part in reversed(path_parts):
                # Extract only digits from the part
                digits_only = ''.join(filter(str.isdigit, part))
                if digits_only and len(digits_only) > 5:  # Facebook IDs are usually long numbers
                    id_part = digits_only
                    break
        
        # Validate that ID contains only digits and has reasonable length
        if id_part and id_part.isdigit() and len(id_part) >= 5:
            return True, "", id_part
        else:
            return False, "Не удалось извлечь Facebook ID из ссылки. Убедитесь, что ссылка содержит корректный ID.", ""
    
    # For username format: extract just the username (last part after /)
    # Remove query parameters if present
    if '?' in link_clean:
        link_clean = link_clean.split('?')[0]
    
    # Remove hash fragments if present
    if '#' in link_clean:
        link_clean = link_clean.split('#')[0]
    
    # Remove trailing slash and any trailing non-alphanumeric characters
    link_clean = link_clean.rstrip('/')
    
    # Remove any trailing brackets, parentheses, or other special characters
    # Keep only alphanumeric, dots, underscores, and hyphens for username
    while link_clean and not link_clean[-1].isalnum() and link_clean[-1] not in ['.', '_', '-']:
        link_clean = link_clean[:-1]
    
    # Extract username (last part after /)
    parts = link_clean.split('/')
    if len(parts) > 0:
        # Get the last non-empty part (username)
        extracted = parts[-1] if parts[-1] else (parts[-2] if len(parts) > 1 else "")
        
        # Clean extracted username from any trailing special characters
        if extracted:
            # Remove any trailing non-alphanumeric characters (except dots, underscores, hyphens)
            cleaned_username = extracted
            while cleaned_username and not cleaned_username[-1].isalnum() and cleaned_username[-1] not in ['.', '_', '-']:
                cleaned_username = cleaned_username[:-1]
            
            if cleaned_username:
                return True, "", cleaned_username
    
    error_msg = (
        "❌ Неверный формат Facebook ссылки.\n\n"
        "📋 Примеры допустимых вариантов:\n"
        "• <code>https://www.facebook.com/username</code>\n"
        "• <code>www.facebook.com/username</code>\n"
        "• <code>facebook.com/username</code>\n"
        "• <code>https://m.facebook.com/profile.php?id=123456789012345</code>\n\n"
        "💡 Можно вставлять ссылку целиком, бот сам извлечёт username или ID."
    )
    return False, error_msg, ""

def validate_telegram_name(tg_name: str) -> tuple[bool, str, str]:
    """Validate Telegram name: remove @ if present, remove all spaces, check not empty"""
    if not tg_name:
        return False, "Имя пользователя Telegram не может быть пустым", ""
    # Remove all spaces (not just trim)
    normalized = tg_name.replace(' ', '').replace('\t', '').replace('\n', '')
    # Remove all @ symbols (handle multiple @)
    normalized = normalized.replace('@', '')
    # Trim any remaining whitespace
    normalized = normalized.strip()
    if not normalized:
        return False, "Имя пользователя Telegram не может быть пустым", ""
    return True, "", normalized

def validate_telegram_id(tg_id: str) -> tuple[bool, str, str]:
    """Validate Telegram ID: must contain only digits"""
    if not tg_id:
        return False, "Telegram ID не может быть пустым", ""
    # Check that input contains only digits
    if not tg_id.isdigit():
        return False, "Telegram ID должен содержать только цифры", ""
    normalized = normalize_telegram_id(tg_id)
    if not normalized:
        return False, "Telegram ID не может быть пустым", ""
    return True, "", normalized

def detect_search_type(value: str) -> tuple[str, str]:
    """
    Automatically detect the type of search value.
    Returns: (field_type, normalized_value)
    field_type can be: 'telegram_id', 'telegram_user', 'fullname', 'unknown'
    """
    if not value:
        return 'unknown', ''
    
    value_stripped = value.strip()
    
    # 1. Check for Telegram ID (only digits, minimum 5 digits for reliability)
    if value_stripped.isdigit() and len(value_stripped) >= 5:
        normalized = normalize_telegram_id(value_stripped)
        if normalized:
            return 'telegram_id', normalized
    
    # 2. Check for Telegram username (letters, digits, underscores, no spaces, may start with @)
    # Remove @ if present
    username_candidate = value_stripped.replace('@', '').strip()
    # Check if it contains only allowed characters for Telegram username
    if username_candidate and not ' ' in username_candidate:
        # Check if it's a valid Telegram username format (alphanumeric, underscores, dots, hyphens)
        if all(c.isalnum() or c in ['_', '.', '-'] for c in username_candidate):
            # Normalize it
            is_valid_tg, _, tg_normalized = validate_telegram_name(username_candidate)
            if is_valid_tg:
                return 'telegram_user', tg_normalized
    
    # 3. Check for fullname (contains spaces or letters, not just digits)
    # If it contains spaces or has letters (not just digits), it's likely a name
    if ' ' in value_stripped or any(c.isalpha() for c in value_stripped):
        # Normalize text field
        normalized = normalize_text_field(value_stripped)
        if normalized and len(normalized) >= 3:  # Minimum 3 characters for name search
            return 'fullname', normalized
    
    # 4. Unknown - cannot determine type
    return 'unknown', value_stripped

def get_field_format_requirements(field_name: str) -> str:
    """Get format requirements description for a field"""
    requirements = {
        'fullname': (
            "📋 <b>Требования к формату:</b>\n"
            "• Введите имя и фамилию клиента\n"
            "• Можно использовать любые буквы (русские, латинские)\n"
            "• Пробелы между словами разрешены\n\n"
            "💡 <b>Примеры:</b>\n"
            "<code>Иван Иванов</code>\n"
            "<code>John Smith</code>\n"
            "<code>Мария Петрова-Сидорова</code>"
        ),
        'manager_name': (
            "📋 <b>Требования к формату:</b>\n"
            "• Введите стейдж менеджера (так менеджер записан в отчётности)\n"
            "• Можно использовать любые буквы (русские, латинские)\n"
            "• Пробелы между словами разрешены\n\n"
            "💡 <b>Примеры:</b>\n"
            "<code>Анна</code>\n"
            "<code>Петр Сидоров</code>\n"
            "<code>Maria</code>"
        ),
        'facebook_link': (
            "Примеры:\n"
            "<code>https://www.facebook.com/username</code>\n"
            "<code>www.facebook.com/username</code>\n"
            "<code>facebook.com/username</code>\n"
            "<code>https://www.facebook.com/profile.php?id=123456789012345</code>\n"
            "<code>https://m.facebook.com/username</code>\n\n"
            "‼️ Важно: добавляйте только прямую ссылку на профиль (без фото, информации и прочих вкладок).\n\n"
        ),
        'telegram_name': (
            "📋 <b>Требования к формату:</b>\n"
            "• Пробелы не допускаются\n\n"
            "💡 <b>Примеры:</b>\n"
            "<code>username</code>\n"
            "<code>Ivan_123</code>\n"
            "<code>john_doe</code>\n\n"
            "⚠️ <b>Важно:</b> Не указывайте символ @ в начале"
        ),
        'telegram_id': (
            "⚠️ Требования к формату:\n"
            "• Только цифры (без букв и символов)\n"
            "• Без пробелов\n"
            "• Минимум 1 цифра\n"
            "Примеры: 12345, 789, 999888777"
        )
    }
    return requirements.get(field_name, "")

def get_field_label(field_name: str) -> str:
    """Get Russian label for field"""
    labels = {
        'fullname': 'имя клиента',
        'manager_name': 'имя агента',
        'facebook_link': 'ссылку клиента',
        'telegram_name': 'username клиента',
        'telegram_id': 'ID клиента'
    }
    return labels.get(field_name, field_name)

def is_field_filled(user_data: dict, field_name: str) -> bool:
    """Check if field is filled (exists and has non-empty value)"""
    value = user_data.get(field_name)
    return value is not None and value != '' and str(value).strip() != ''

def get_next_add_field(current_field: str) -> tuple[str, int, int, int]:
    """Get next field in the add flow. Returns (field_name, state, current_step, total_steps)"""
    field_sequence = [
        ('fullname', ADD_FULLNAME),
        ('facebook_link', ADD_FB_LINK),
        ('telegram_name', ADD_TELEGRAM_NAME),
        ('telegram_id', ADD_TELEGRAM_ID),
    ]
    total_steps = len(field_sequence) + 1  # +1 for review step
    
    if not current_field:
        return field_sequence[0][0], field_sequence[0][1], 1, total_steps
    
    for i, (field, state) in enumerate(field_sequence):
        if field == current_field:
            if i + 1 < len(field_sequence):
                return field_sequence[i + 1][0], field_sequence[i + 1][1], i + 2, total_steps
            else:
                return ('review', ADD_REVIEW, total_steps, total_steps)
    
    return field_sequence[0][0], field_sequence[0][1], 1, total_steps

def get_navigation_keyboard(is_optional: bool = False, show_back: bool = True) -> InlineKeyboardMarkup:
    """Get navigation keyboard for field input"""
    keyboard = []
    
    # Кнопка "Пропустить" сверху на 100% ширины (если поле опциональное)
    if is_optional:
        keyboard.append([InlineKeyboardButton("⏭️ Пропустить", callback_data="add_skip")])
    
    # Кнопки "Назад" и "Главное меню" в один ряд (по 50% каждая)
    if show_back:
        keyboard.append([
            InlineKeyboardButton("◀️ Назад", callback_data="add_back"),
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
        ])
    else:
        # Если кнопка "Назад" не нужна, только "Главное меню" на 100%
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(keyboard)


# Conversation states
(
    # Check states
    CHECK_BY_TELEGRAM,
    CHECK_BY_FB_LINK,
    CHECK_BY_TELEGRAM_ID,
    # CHECK_BY_PHONE,  # Removed - phone field no longer used
    CHECK_BY_FULLNAME,
    SMART_CHECK_INPUT,  # Smart check with auto-detection
    # Add states (sequential flow)
    ADD_FULLNAME,
    ADD_MANAGER_NAME,
    # ADD_PHONE,  # Removed - phone field no longer used
    ADD_FB_LINK,
    ADD_TELEGRAM_NAME,
    ADD_TELEGRAM_ID,
    ADD_REVIEW,  # Review before saving
    # Edit states
    EDIT_MENU,
    EDIT_FULLNAME,
    # EDIT_PHONE,  # Removed - phone field no longer used
    EDIT_FB_LINK,
    EDIT_TELEGRAM_NAME,
    EDIT_TELEGRAM_ID,
    EDIT_MANAGER_NAME,
    EDIT_PIN,
    # Tag states
    TAG_PIN,  # PIN verification for tag command
    TAG_SELECT_MANAGER,  # Selection of manager from list
    TAG_ENTER_NEW  # Enter new tag
) = range(21)

# Store user data during conversation - isolated per user_id for concurrent access
# Each user's data is stored separately, allowing 10+ managers to work simultaneously
user_data_store = {}
user_data_store_access_time = {}
USER_DATA_STORE_TTL = 3600  # 1 hour in seconds
USER_DATA_STORE_MAX_SIZE = 1000  # Maximum number of entries

# Main menu keyboard
def get_main_menu_keyboard():
    """Create main menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("✅ Проверить", callback_data="check_menu")],
        [InlineKeyboardButton("➕ Добавить", callback_data="add_new")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Check menu keyboard
def get_check_menu_keyboard():
    """Create check menu keyboard with all search options"""
    keyboard = [
        [InlineKeyboardButton("📱 Имя пользователя Telegram", callback_data="check_telegram")],
        [InlineKeyboardButton("🆔 Telegram ID", callback_data="check_telegram_id")],
        [InlineKeyboardButton("👤 Клиент", callback_data="check_fullname")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_check_back_keyboard():
    """Create keyboard with only 'Back' button for check input prompts"""
    keyboard = [
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Add menu keyboard
def get_add_menu_keyboard():
    """Create add menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("➕ Добавить новый лид", callback_data="add_new")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Photo handling functions
def build_lead_photo_path(lead_id: int, extension: str = "jpg") -> str:
    """Generate unique storage path for lead photo"""
    unique = uuid.uuid4().hex[:8]
    return f"photos/lead_{lead_id}_{unique}.{extension}"

async def download_photo_from_supabase(photo_url: str) -> bytes | None:
    """Download photo from Supabase Storage using storage client and return as bytes"""
    try:
        # Extract storage path from URL
        # URL format: https://{project}.supabase.co/storage/v1/object/public/{bucket}/{path}
        # Example: https://mwovubdpxkeqpyxsadgg.supabase.co/storage/v1/object/public/Leads/photos/lead_2051_f475f8b3.jpg
        if not photo_url or not photo_url.strip():
            logger.error("[PHOTO] Empty photo_url provided")
            return None
        
        # Remove query parameters if any
        photo_url = photo_url.split('?')[0]
        
        # Extract path from URL
        # Find the bucket name and path after /object/public/
        if '/object/public/' not in photo_url:
            logger.error(f"[PHOTO] Invalid Supabase Storage URL format: {photo_url}")
            return None
        
        # Extract path after /object/public/{bucket}/
        parts = photo_url.split('/object/public/')
        if len(parts) < 2:
            logger.error(f"[PHOTO] Could not parse URL: {photo_url}")
            return None
        
        path_with_bucket = parts[1]
        # Split bucket and path
        path_parts = path_with_bucket.split('/', 1)
        if len(path_parts) < 2:
            logger.error(f"[PHOTO] Could not extract path from URL: {photo_url}")
            return None
        
        bucket_name = path_parts[0]
        storage_path = path_parts[1]
        
        logger.info(f"[PHOTO] Extracted bucket='{bucket_name}', path='{storage_path}' from URL")
        
        # Use storage client to download file
        client = get_supabase_storage_client()
        if not client:
            logger.error("[PHOTO] Supabase Storage client is None, cannot download photo")
            return None
        
        # Download file from Supabase Storage
        file_data = client.storage.from_(bucket_name).download(storage_path)
        
        if file_data:
            logger.info(f"[PHOTO] Successfully downloaded photo from Supabase Storage: {len(file_data)} bytes")
            return file_data
        else:
            logger.error(f"[PHOTO] download() returned None for path: {storage_path}")
            return None
            
    except Exception as e:
        logger.error(f"[PHOTO] Error downloading photo from Supabase Storage: {e}", exc_info=True)
        return None

async def upload_lead_photo_to_supabase(bot, file_id: str, lead_id: int) -> str | None:
    """
    Download photo from Telegram and upload to Supabase Storage.
    Returns public URL of uploaded photo or None if failed.
    """
    if not ENABLE_LEAD_PHOTOS:
        logger.info(f"[PHOTO] Photo upload disabled by ENABLE_LEAD_PHOTOS for lead {lead_id}")
        return None
    
    # Use storage client with service_role key to bypass RLS
    client = get_supabase_storage_client()
    if not client:
        logger.error(f"[PHOTO] Supabase Storage client is None, cannot upload photo for lead {lead_id}")
        return None
    
    try:
        # 1) Get file from Telegram
        tg_file = await bot.get_file(file_id)
        logger.info(f"[PHOTO] Got Telegram file for lead {lead_id}: {tg_file.file_path if tg_file.file_path else 'no path'}")
        
        # 2) Determine file extension
        ext = "jpg"  # default
        if tg_file.file_path:
            file_path_lower = tg_file.file_path.lower()
            if file_path_lower.endswith(".png"):
                ext = "png"
            elif file_path_lower.endswith(".webp"):
                ext = "webp"
            elif file_path_lower.endswith(".jpeg") or file_path_lower.endswith(".jpg"):
                ext = "jpg"
        
        # 3) Download file content as bytes
        file_bytes = await tg_file.download_as_bytearray()
        # Convert bytearray to bytes for Supabase Storage API compatibility
        file_bytes = bytes(file_bytes)
        file_size = len(file_bytes)
        logger.info(f"[PHOTO] Downloaded photo for lead {lead_id}: {file_size} bytes, extension: {ext}")
        
        # 4) Build storage path
        storage_path = build_lead_photo_path(lead_id, ext)
        logger.info(f"[PHOTO] Storage path for lead {lead_id}: {storage_path}")
        
        # 5) Upload to Supabase Storage
        # Note: Supabase Python client uses from_() method (with underscore) to avoid conflict with 'from' keyword
        response = client.storage.from_(SUPABASE_LEADS_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": f"image/{ext}"}
        )
        
        logger.info(f"[PHOTO] Upload response for lead {lead_id}: {response}")
        
        # 6) Get public URL
        public_url = client.storage.from_(SUPABASE_LEADS_BUCKET).get_public_url(storage_path)
        logger.info(f"[PHOTO] Successfully uploaded photo for lead {lead_id}: {public_url}")
        return public_url
        
    except Exception as e:
        logger.error(f"[PHOTO] Error uploading photo for lead {lead_id}: {e}", exc_info=True)
        return None

# Helper function to clear all conversation state including internal ConversationHandler keys
def clear_all_conversation_state(context: ContextTypes.DEFAULT_TYPE, user_id: int = None):
    """Clear all conversation state including internal ConversationHandler keys (_conversation_*)"""
    if context.user_data:
        # Remove all conversation-related keys
        keys_to_remove = [
            'current_field', 'current_state', 'add_step', 'editing_lead_id',
            'last_check_messages', 'add_message_ids', 'check_by', 'check_value',
            'check_results', 'selected_lead_id', 'original_lead_data'
        ]
        for key in keys_to_remove:
            if key in context.user_data:
                del context.user_data[key]
        
        # Remove all internal ConversationHandler keys (they start with _conversation_)
        conversation_keys = [key for key in context.user_data.keys() if key.startswith('_conversation_')]
        for key in conversation_keys:
            del context.user_data[key]
            logger.debug(f"Cleared ConversationHandler internal key: {key}")
        
        # Clear all remaining state
        context.user_data.clear()
        logger.info(f"Cleared all conversation state for user {user_id if user_id else 'unknown'}")
    
    # Clear user data store if user_id provided
    if user_id:
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]


def log_conversation_state(user_id: int, context: ContextTypes.DEFAULT_TYPE, prefix: str = "[STATE]") -> None:
    """Log current conversation-related state for diagnostics."""
    try:
        user_keys = list(context.user_data.keys()) if context.user_data else []
        conversation_keys = [key for key in user_keys if key.startswith("_conversation_")]
        in_user_store = user_id in user_data_store
        logger.info(
            f"{prefix} user_id={user_id}, "
            f"user_keys={user_keys}, "
            f"conversation_keys={conversation_keys}, "
            f"in_user_data_store={in_user_store}"
        )
    except Exception as e:
        logger.error(f"{prefix} Failed to log conversation state for user_id={user_id}: {e}", exc_info=True)

async def check_add_state_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for add flow when it was initialized by forwarded message.

    If user already has add state (set by handle_forwarded_message or add_field_input),
    we immediately delegate this *same* update to the proper handler so the user
    doesn't have to send the message twice.
    
    IMPORTANT: This should NOT intercept messages if user is in another active ConversationHandler
    (e.g., tag_conv, edit_conv, etc.)
    """
    if not update.message:
        return None
    
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')
    
    # ADD LOGGING
    logger.info(
        f"[CHECK_ADD_STATE_ENTRY] Called for user {user_id}, "
        f"message_text='{update.message.text if update.message and update.message.text else None}', "
        f"current_state={current_state}, "
        f"pin_attempts={context.user_data.get('pin_attempts')}, "
        f"conversation_keys={[k for k in (context.user_data.keys() if context.user_data else []) if k.startswith('_conversation_')]}"
    )
    
    # PRIORITY CHECK: Check for indicators of other active flows BEFORE checking add flow
    # These checks use context.user_data keys that are set during tag/edit flows
    
    # Check for tag flow indicators (even if current_state is not set correctly)
    tag_manager_name = context.user_data.get('tag_manager_name')
    tag_new_tag = context.user_data.get('tag_new_tag')
    pin_attempts = context.user_data.get('pin_attempts')
    # If user has pin_attempts, they're likely in tag flow (PIN input stage)
    if tag_manager_name or tag_new_tag or pin_attempts is not None:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in tag flow "
            f"(tag_manager_name={bool(tag_manager_name)}, tag_new_tag={bool(tag_new_tag)}, pin_attempts={pin_attempts}), "
            "not intercepting message"
        )
        return None
    
    # Check for edit flow indicators (even if current_state is not set correctly)
    editing_lead_id = context.user_data.get('editing_lead_id')
    if editing_lead_id:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in edit flow "
            f"(editing_lead_id={editing_lead_id}), not intercepting message"
        )
        return None
    
    # Check if user is in another active ConversationHandler by state
    # Tag flow states - INCLUDING TAG_PIN to prevent intercepting PIN input!
    tag_states = {TAG_PIN, TAG_SELECT_MANAGER, TAG_ENTER_NEW}
    if current_state in tag_states:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in tag flow (state={current_state}), "
            "not intercepting message"
        )
        return None
    
    # Edit flow states
    edit_states = {EDIT_PIN, EDIT_MENU, EDIT_FULLNAME, EDIT_FB_LINK, EDIT_TELEGRAM_NAME, EDIT_TELEGRAM_ID, EDIT_MANAGER_NAME}
    if current_state in edit_states:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} is in edit flow (state={current_state}), "
            "not intercepting message"
        )
        return None
    
    # Conversation states that belong to the add flow
    add_states = {ADD_FULLNAME, ADD_FB_LINK, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    
    # If user has add data and current_state points to add flow – handle this update there
    if user_id in user_data_store and current_state in add_states:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY] User {user_id} has existing add state {current_state}, "
            "delegating update to add flow handler"
        )
        # If we're already at review stage, just show review
        if current_state == ADD_REVIEW:
            return await show_add_review(update, context)
        # For all other add states, process input via add_field_input
        return await add_field_input(update, context)
    
    # No pre-initialized add state – let other handlers process update
    return None

async def check_add_state_entry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for add flow via callback query when state was initialized by forwarded message.
    
    This allows callback queries (like add_skip, add_back) to work even when
    the ConversationHandler wasn't explicitly activated via add_new button.
    """
    if not update.callback_query:
        return None
    
    user_id = update.effective_user.id
    current_state = context.user_data.get('current_state')
    callback_data = update.callback_query.data if update.callback_query.data else ""
    
    # Check for indicators of other active flows BEFORE checking add flow
    tag_manager_name = context.user_data.get('tag_manager_name')
    tag_new_tag = context.user_data.get('tag_new_tag')
    editing_lead_id = context.user_data.get('editing_lead_id')
    
    if tag_manager_name or tag_new_tag or editing_lead_id:
        # User is in tag/edit flow, don't activate add flow
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY_CALLBACK] User {user_id} is in tag/edit flow, "
            "not activating add flow"
        )
        return None
    
    # Conversation states that belong to the add flow
    add_states = {ADD_FULLNAME, ADD_FB_LINK, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    
    # If user has add state, activate ConversationHandler AND process the callback
    if user_id in user_data_store and current_state in add_states:
        logger.info(
            f"[CHECK_ADD_STATE_ENTRY_CALLBACK] User {user_id} has existing add state {current_state}, "
            f"activating ConversationHandler and processing callback: {callback_data}"
        )
        
        # Process the callback immediately based on its type
        if callback_data == "add_skip":
            # Delegate to add_skip_callback, which will return the next state
            result = await add_skip_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_back":
            # Delegate to add_back_callback
            result = await add_back_callback(update, context)
            return result if result is not None else current_state
        elif callback_data == "add_cancel":
            # Delegate to add_cancel_callback
            result = await add_cancel_callback(update, context)
            return result if result is not None else current_state
        
        # If callback doesn't match, just activate ConversationHandler
        return current_state
    
    # No pre-initialized add state – let other handlers process callback
    return None

async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle forwarded messages globally - extract data and start add flow if needed"""
    if not update.message:
        return None  # Not a message update
    
    user_id = update.effective_user.id
    
    # Log message attributes for debugging
    has_photo = bool(update.message.photo)
    has_text = bool(update.message.text)
    has_caption = bool(update.message.caption)
    forward_from = update.message.forward_from
    forward_from_chat = update.message.forward_from_chat
    forward_sender_name = update.message.forward_sender_name
    
    logger.info(
        f"[FORWARD_GLOBAL] Message from user {user_id}: "
        f"photo={has_photo}, text={has_text}, caption={has_caption}, "
        f"forward_from={bool(forward_from)}, forward_from_chat={bool(forward_from_chat)}, "
        f"forward_sender_name={bool(forward_sender_name)}"
    )
    
    # Check if message is forwarded
    # Include photo-only messages if they have forwarding indicators
    is_forwarded = (forward_from is not None or 
                    forward_from_chat is not None or 
                    forward_sender_name is not None)
    
    # Special case: if message has photo but no text/caption, and has forwarding indicators,
    # treat it as forwarded even if forward_from is None (privacy settings)
    if not is_forwarded and has_photo and not has_text and not has_caption:
        # Check if there are any forwarding indicators (even if forward_from is None)
        if forward_from_chat is not None or forward_sender_name is not None:
            is_forwarded = True
            logger.info(f"[FORWARD_GLOBAL] Photo-only forwarded message detected (privacy settings hide forward_from)")
    
    if not is_forwarded:
        logger.info(f"[FORWARD_GLOBAL] Not a forwarded message, skipping")
        return None  # Not a forwarded message, let other handlers process it
    
    logger.info(f"[FORWARD_GLOBAL] Forwarded message detected from user {user_id}")
    
    # Check if user is in another active ConversationHandler (edit, tag, or add flow)
    current_state = context.user_data.get('current_state')
    
    # Edit flow states
    edit_states = {EDIT_PIN, EDIT_MENU, EDIT_FULLNAME, EDIT_FB_LINK, EDIT_TELEGRAM_NAME, EDIT_TELEGRAM_ID, EDIT_MANAGER_NAME}
    # Tag flow states
    tag_states = {TAG_SELECT_MANAGER, TAG_ENTER_NEW}
    # Add flow states
    add_states = {ADD_FULLNAME, ADD_FB_LINK, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    
    # Check if user is in edit flow
    if current_state in edit_states or context.user_data.get('editing_lead_id'):
        logger.info(f"[FORWARD_GLOBAL] User {user_id} is in edit flow (state={current_state}), ignoring forwarded message")
        await update.message.reply_text(
            "⚠️ Вы находитесь в процессе редактирования лида.\n\n"
            "Завершите редактирование или отмените его, прежде чем добавлять новый лид.",
            reply_markup=get_main_menu_keyboard()
        )
        return None
    
    # Check if user is in tag flow
    if current_state in tag_states:
        logger.info(f"[FORWARD_GLOBAL] User {user_id} is in tag flow (state={current_state}), ignoring forwarded message")
        await update.message.reply_text(
            "⚠️ Вы находитесь в процессе изменения тега менеджера.\n\n"
            "Завершите операцию или отмените её, прежде чем добавлять новый лид.",
            reply_markup=get_main_menu_keyboard()
        )
        return None
    
    # Check if user is already in the process of adding a lead
    # If yes, let the existing add_field_input handle it
    if user_id in user_data_store:
        # Check if user is in add flow (has current_field or current_state in add_states)
        if context.user_data.get('current_field') or current_state in add_states:
            # User is in add flow, let add_field_input handle it
            logger.info(f"[FORWARD_GLOBAL] User {user_id} is already in add flow, letting add_field_input handle it")
            return None
    
    # User is not in any active flow - start new add flow from forwarded message
    # Check if forward_from is available (privacy settings may hide it)
    if update.message.forward_from is None:
        # Privacy settings hide the sender info - start normal flow
        clear_all_conversation_state(context, user_id)
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
        context.user_data['current_field'] = 'fullname'
        context.user_data['current_state'] = ADD_FULLNAME
        context.user_data['add_step'] = 0
        
        # Extract photo if available (even if forward_from is None)
        extracted_info = []
        if update.message.photo:
            # Get largest photo (last in the list)
            largest_photo = update.message.photo[-1]
            photo_file_id = largest_photo.file_id
            user_data_store[user_id]['photo_file_id'] = photo_file_id
            extracted_info.append("• Фото: обнаружено (будет загружено при сохранении)")
            logger.info(f"[FORWARD_GLOBAL] Extracted photo_file_id (privacy mode) for user {user_id}: {photo_file_id}")
        
        # Parse caption for Facebook link (if available)
        if update.message.caption:
            caption = update.message.caption.strip()
            is_valid_fb, _, fb_normalized = validate_facebook_link(caption)
            if is_valid_fb:
                user_data_store[user_id]['facebook_link'] = fb_normalized
                extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
                logger.info(f"[FORWARD_GLOBAL] Extracted facebook_link from caption (privacy mode): {fb_normalized}")
        
        # Show extracted info if any
        info_text = ""
        if extracted_info:
            info_text = "\n\n✅ Извлечено из сообщения:\n" + "\n".join(extracted_info) + "\n"
        
        field_label = get_field_label('fullname')
        _, _, current_step, total_steps = get_next_add_field('')
        message = f"<b>Шаг {current_step} из {total_steps}</b>\n\n📝 Введите {field_label}:"
        
        await update.message.reply_text(
            "⚠️ Данные отправителя недоступны из-за настроек приватности." + info_text + "\n" + message,
            reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
            parse_mode='HTML'
        )
        # Return None - the next message from user will be handled by ConversationHandler
        # The state is already set in context.user_data, so ConversationHandler will process it
        return None
    else:
        forward_from = update.message.forward_from
        
        # Check if it's a bot
        if forward_from.is_bot:
            await update.message.reply_text(
                "❌ Недоступно. Аккаунт является ботом.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Initialize add flow
        clear_all_conversation_state(context, user_id)
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
        context.user_data['current_field'] = 'fullname'
        context.user_data['current_state'] = ADD_FULLNAME
        context.user_data['add_step'] = 0
        
        # Extract data from forward_from
        extracted_data = {}
        extracted_info = []
        
        # Extract telegram_id (required if available)
        if forward_from.id:
            telegram_id = normalize_telegram_id(str(forward_from.id))
            if telegram_id:
                extracted_data['telegram_id'] = telegram_id
                extracted_info.append(f"• Telegram ID: {telegram_id}")
                logger.info(f"[FORWARD_GLOBAL] Extracted telegram_id: {telegram_id}")
        
        # Extract telegram_name (if available)
        if forward_from.username:
            is_valid, _, normalized = validate_telegram_name(forward_from.username)
            if is_valid:
                extracted_data['telegram_name'] = normalized
                extracted_info.append(f"• Username: @{normalized}")
                logger.info(f"[FORWARD_GLOBAL] Extracted telegram_name: {normalized}")
        
        # Extract fullname (Display Name: first_name + last_name)
        first_name = forward_from.first_name or ""
        last_name = forward_from.last_name or ""
        if first_name or last_name:
            if last_name:
                fullname = f"{first_name} {last_name}".strip()
            else:
                fullname = first_name
            normalized_fullname = normalize_text_field(fullname)
            if normalized_fullname:
                extracted_data['fullname'] = normalized_fullname
                extracted_info.append(f"• Имя: {normalized_fullname}")
                logger.info(f"[FORWARD_GLOBAL] Extracted fullname: {normalized_fullname}")
        
        # Parse text message for Facebook link (if available)
        if update.message.text:
            text = update.message.text.strip()
            is_valid_fb, _, fb_normalized = validate_facebook_link(text)
            if is_valid_fb:
                extracted_data['facebook_link'] = fb_normalized
                extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
                logger.info(f"[FORWARD_GLOBAL] Extracted facebook_link from message text: {fb_normalized}")
        
        # Parse caption for Facebook link (if available, and text was not processed)
        if update.message.caption and 'facebook_link' not in extracted_data:
            caption = update.message.caption.strip()
            is_valid_fb, _, fb_normalized = validate_facebook_link(caption)
            if is_valid_fb:
                extracted_data['facebook_link'] = fb_normalized
                extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
                logger.info(f"[FORWARD_GLOBAL] Extracted facebook_link from caption: {fb_normalized}")
        
        # Extract photo if available
        if update.message.photo:
            # Get largest photo (last in the list)
            largest_photo = update.message.photo[-1]
            photo_file_id = largest_photo.file_id
            extracted_data['photo_file_id'] = photo_file_id
            extracted_info.append("• Фото: обнаружено (будет загружено при сохранении)")
            logger.info(f"[FORWARD_GLOBAL] Extracted photo_file_id for user {user_id}: {photo_file_id}")
        
        # Save extracted data to user_data_store
        for key, value in extracted_data.items():
            user_data_store[user_id][key] = value
        
        # Update access time
        user_data_store_access_time[user_id] = time.time()
        
        logger.info(f"[FORWARD_GLOBAL] Saved extracted data to user_data_store for user {user_id}: {list(extracted_data.keys())}")
        
        # Show user what was extracted
        if extracted_info:
            info_text = "\n".join(extracted_info)
            await update.message.reply_text(
                f"✅ Данные извлечены из пересланного сообщения:\n\n{info_text}\n\n"
                f"Продолжайте заполнение остальных полей."
            )
        else:
            await update.message.reply_text(
                "⚠️ Не удалось извлечь данные из пересланного сообщения.\n\n"
                "Продолжайте заполнение полей вручную."
            )
        
        # Determine next field to fill - start from beginning and skip all filled fields
        next_field, next_state, current_step, total_steps = get_next_add_field('')
        
        # Skip already filled fields
        while next_field != 'review' and is_field_filled(user_data_store[user_id], next_field):
            logger.info(f"[FORWARD_GLOBAL] Skipping already filled field: {next_field}")
            next_field, next_state, current_step, total_steps = get_next_add_field(next_field)
        
        # Move to next field or review
        if next_field == 'review':
            logger.info(f"[FORWARD_GLOBAL] All fields filled, showing review for user {user_id}")
            await show_add_review(update, context)
            # Return None to let ConversationHandler handle the state transition
            return None
        else:
            logger.info(f"[FORWARD_GLOBAL] Starting add flow from field '{next_field}' (step {current_step}/{total_steps}) for user {user_id}")
            field_label = get_field_label(next_field)
            is_optional = next_field not in ['fullname']
            progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
            
            if next_field == 'fullname':
                message = f"{progress_text}📝 Введите {field_label}:"
            else:
                requirements = get_field_format_requirements(next_field)
                message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
            
            context.user_data['current_field'] = next_field
            context.user_data['current_state'] = next_state
            
            sent_message = await update.message.reply_text(
                message,
                reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
            # Return None - the next message from user will be handled by ConversationHandler
            # The state is already set in context.user_data, so ConversationHandler will process it
            return None

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular (not forwarded) photo messages - start add lead flow"""
    if not update.message:
        return None
    
    user_id = update.effective_user.id
    logger.info(f"[PHOTO_MESSAGE] Processing photo message from user {user_id}")
    
    # Check if message is forwarded - if yes, let handle_forwarded_message handle it
    is_forwarded = (update.message.forward_from is not None or 
                    update.message.forward_from_chat is not None or 
                    update.message.forward_sender_name is not None)
    if is_forwarded:
        logger.info(f"[PHOTO_MESSAGE] Message is forwarded, delegating to handle_forwarded_message")
        return None  # Let handle_forwarded_message handle forwarded messages
    
    # Check if message has photo
    if not update.message.photo:
        logger.info(f"[PHOTO_MESSAGE] Message has no photo, skipping")
        return None
    
    # Check if user is in another active ConversationHandler
    current_state = context.user_data.get('current_state')
    edit_states = {EDIT_PIN, EDIT_MENU, EDIT_FULLNAME, EDIT_FB_LINK, EDIT_TELEGRAM_NAME, EDIT_TELEGRAM_ID, EDIT_MANAGER_NAME}
    tag_states = {TAG_SELECT_MANAGER, TAG_ENTER_NEW}
    add_states = {ADD_FULLNAME, ADD_FB_LINK, ADD_TELEGRAM_NAME, ADD_TELEGRAM_ID, ADD_REVIEW}
    
    # Check if user is in edit flow
    if current_state in edit_states or context.user_data.get('editing_lead_id'):
        logger.info(f"[PHOTO_MESSAGE] User {user_id} is in edit flow, ignoring photo message")
        return None
    
    # Check if user is in tag flow
    if current_state in tag_states:
        logger.info(f"[PHOTO_MESSAGE] User {user_id} is in tag flow, ignoring photo message")
        return None
    
    # Check if user is already in add flow
    if user_id in user_data_store and current_state in add_states:
        logger.info(f"[PHOTO_MESSAGE] User {user_id} is already in add flow, ignoring photo message")
        return None
    
    # Extract photo
    largest_photo = update.message.photo[-1]
    photo_file_id = largest_photo.file_id
    logger.info(f"[PHOTO_MESSAGE] Extracted photo_file_id: {photo_file_id} for user {user_id}")
    
    # Check if message has text or caption
    has_text = bool(update.message.text and update.message.text.strip())
    has_caption = bool(update.message.caption and update.message.caption.strip())
    logger.info(f"[PHOTO_MESSAGE] Scenario: {'with text' if (has_text or has_caption) else 'without text'} for user {user_id}")
    
    # Initialize add flow
    clear_all_conversation_state(context, user_id)
    user_data_store[user_id] = {}
    user_data_store_access_time[user_id] = time.time()
    user_data_store[user_id]['photo_file_id'] = photo_file_id
    
    if has_text or has_caption:
        # Сценарий 2: Фото с текстом - использовать текст как fullname, начать с шага 2
        text = (update.message.text or update.message.caption).strip()
        normalized_fullname = normalize_text_field(text)
        if normalized_fullname:
            user_data_store[user_id]['fullname'] = normalized_fullname
            context.user_data['current_field'] = 'facebook_link'
            context.user_data['current_state'] = ADD_FB_LINK
            context.user_data['add_step'] = 1
            
            field_label = get_field_label('facebook_link')
            _, _, current_step, total_steps = get_next_add_field('fullname')
            requirements = get_field_format_requirements('facebook_link')
            
            await update.message.reply_text(
                f"✅ Фото получено. Имя извлечено из текста: <code>{escape_html(normalized_fullname)}</code>\n\n"
                f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
                f"📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=False),
                parse_mode='HTML'
            )
        else:
            # Если текст не удалось нормализовать, начать с шага 1
            context.user_data['current_field'] = 'fullname'
            context.user_data['current_state'] = ADD_FULLNAME
            context.user_data['add_step'] = 0
            
            field_label = get_field_label('fullname')
            _, _, current_step, total_steps = get_next_add_field('')
            
            await update.message.reply_text(
                f"✅ Фото получено.\n\n"
                f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
                f"📝 Введите {field_label}:",
                reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
                parse_mode='HTML'
            )
    else:
        # Сценарий 1: Фото без текста - начать с шага 1
        context.user_data['current_field'] = 'fullname'
        context.user_data['current_state'] = ADD_FULLNAME
        context.user_data['add_step'] = 0
        
        field_label = get_field_label('fullname')
        _, _, current_step, total_steps = get_next_add_field('')
        
        await update.message.reply_text(
            f"✅ Фото получено.\n\n"
            f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
            f"📝 Введите {field_label}:",
            reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
            parse_mode='HTML'
        )
    
    logger.info(f"[PHOTO_MESSAGE] Started add flow for user {user_id} with photo (scenario: {'with text' if (has_text or has_caption) else 'without text'})")
    return None

# Command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show main menu"""
    try:
        user_id = update.effective_user.id
        logger.info(f"[START] Clearing all conversation state for user {user_id}")
        
        # Clear all conversation state including internal ConversationHandler keys
        clear_all_conversation_state(context, user_id)
        
        # Clean up all intermediate messages before showing main menu
        await cleanup_all_messages_before_main_menu(update, context)
        
        welcome_message = (
            "👋 Добро пожаловать в ClientsBot!\n\n"
            "Выберите действие:"
        )
        await retry_telegram_api(
            update.message.reply_text,
            text=welcome_message,
            reply_markup=get_main_menu_keyboard()
        )
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in start_command: {e}", exc_info=True)
        try:
            await retry_telegram_api(
                update.message.reply_text,
                text="❌ Произошла ошибка при обработке команды. Попробуйте позже."
            )
        except:
            pass
        return ConversationHandler.END

async def tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tag command - request PIN code before showing manager list
    This command ALWAYS interrupts any current process and starts its own flow."""
    try:
        user_id = update.effective_user.id
        from_user = update.effective_user
        logger.info(
            f"[TAG] /tag command received from user_id={user_id}, "
            f"name='{from_user.first_name} {from_user.last_name}', "
            f"username='{from_user.username}', "
            f"context_keys_before_clear={list(context.user_data.keys()) if context.user_data else []}"
        )
        
        # ALWAYS clear all conversation states to interrupt any current process
        clear_all_conversation_state(context, user_id)
        # Also clear user_data_store to prevent check_add_state_entry from intercepting messages
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]
        # Explicitly clear ALL flow related keys from context.user_data
        for key in ['current_field', 'current_state', 'add_step', 'editing_lead_id', 
                    'tag_manager_name', 'tag_new_tag', 'pin_attempts']:
            context.user_data.pop(key, None)
        
        logger.info(f"[TAG] All states cleared for user {user_id}, starting tag flow, context_keys_after_clear={list(context.user_data.keys()) if context.user_data else []}")
        
        # Reset PIN attempt counter when starting new tag flow
        context.user_data['pin_attempts'] = 0
        # Explicitly set current_state to TAG_PIN to prevent check_add_state_entry from intercepting
        context.user_data['current_state'] = TAG_PIN
        
        # Request PIN code before allowing tag change
        message = "🔒 Для изменения тега менеджера требуется PIN-код.\n\nВведите PIN-код:"
        await update.message.reply_text(message)
        
        # ADD THIS LOGGING
        log_conversation_state(user_id, context, prefix="[TAG_AFTER_PIN_REQUEST]")
        
        logger.info(
            f"[TAG] Requested PIN code from user {user_id}, expecting TAG_PIN, current_state set to TAG_PIN. "
            f"Returning TAG_PIN state."
        )
        return TAG_PIN
    except Exception as e:
        logger.error(f"[TAG] Error in tag_command: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "❌ Произошла ошибка при обработке команды. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def tag_pin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PIN code input for tag command"""
    user_id = update.effective_user.id
    
    # ADD DETAILED LOGGING AT THE START
    logger.info(
        f"[TAG_PIN_INPUT] ⚡ Function CALLED for user {user_id}, "
        f"message_text='{update.message.text if update.message and update.message.text else None}', "
        f"current_state={context.user_data.get('current_state')}, "
        f"pin_attempts={context.user_data.get('pin_attempts')}, "
        f"conversation_keys={[k for k in (context.user_data.keys() if context.user_data else []) if k.startswith('_conversation_')]}"
    )
    
    # Check if message exists and has text
    if not update.message or not update.message.text:
        if update.message:
            await update.message.reply_text(
                "❌ Ошибка: Неверный формат сообщения. Пожалуйста, введите PIN-код текстом."
            )
        else:
            logger.error("tag_pin_input: update.message is None")
        return TAG_PIN
    
    text = update.message.text.strip()
    
    # PIN code is "2025"
    PIN_CODE = "2025"
    
    if text == PIN_CODE:
        # PIN is correct, reset attempt counter
        if 'pin_attempts' in context.user_data:
            del context.user_data['pin_attempts']
        
        # PIN is correct, load manager names and show selection
        # Clear any old field editing state to prevent automatic transitions
        if 'current_field' in context.user_data:
            del context.user_data['current_field']
        if 'current_state' in context.user_data:
            del context.user_data['current_state']
        
        # Get Supabase client
        client = get_supabase_client()
        if not client:
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            logger.error(f"[TAG] get_supabase_client returned None for user {user_id}")
            await update.message.reply_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
        
        # Get unique manager names
        manager_names = get_unique_manager_names(client)
        logger.info(f"[TAG] Loaded {len(manager_names)} unique manager_name values for user {user_id}: {manager_names}")
        
        if not manager_names:
            await update.message.reply_text(
                "❌ В базе данных нет записей с manager_name.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Store manager names in context for later retrieval (to avoid long callback_data)
        context.user_data['tag_manager_names'] = manager_names
        logger.info(f"[TAG] Stored tag_manager_names in context for user {user_id}")
        
        # Create keyboard with manager names
        keyboard = []
        # Add buttons in rows of 2
        # Use index in callback_data instead of full name to avoid exceeding 64 byte limit
        for i in range(0, len(manager_names), 2):
            row = []
            row.append(InlineKeyboardButton(
                manager_names[i],
                callback_data=f"tag_mgr_{i}"
            ))
            if i + 1 < len(manager_names):
                row.append(InlineKeyboardButton(
                    manager_names[i + 1],
                    callback_data=f"tag_mgr_{i + 1}"
                ))
            keyboard.append(row)
        
        # Add main menu button
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🏷️ <b>Выберите менеджера для смены тега:</b>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        
        # Explicitly set current_state to TAG_SELECT_MANAGER to prevent other handlers from intercepting
        context.user_data['current_state'] = TAG_SELECT_MANAGER
        
        logger.info(f"[TAG] Sent manager selection keyboard to user {user_id}, expecting TAG_SELECT_MANAGER, current_state set to TAG_SELECT_MANAGER")
        return TAG_SELECT_MANAGER
    else:
        # PIN is incorrect, increment attempt counter
        pin_attempts = context.user_data.get('pin_attempts', 0) + 1
        context.user_data['pin_attempts'] = pin_attempts
        
        if pin_attempts >= 3:
            # Too many failed attempts, return to main menu
            await update.message.reply_text(
                "❌ Превышено количество попыток ввода PIN-кода (3). Доступ к изменению тега заблокирован.",
                reply_markup=get_main_menu_keyboard()
            )
            # Clear tag state
            if 'pin_attempts' in context.user_data:
                del context.user_data['pin_attempts']
            return ConversationHandler.END
        else:
            # PIN is incorrect, ask again
            remaining_attempts = 3 - pin_attempts
            await update.message.reply_text(
                f"❌ Неверный PIN-код. Осталось попыток: {remaining_attempts}\n\nВведите PIN-код:"
            )
            return TAG_PIN

async def tag_manager_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle selection of manager from tag command"""
    query = update.callback_query
    await query.answer()
    
    try:
        user_id = update.effective_user.id
        callback_data = query.data
        logger.info(
            f"[TAG] tag_manager_callback called for user {user_id}, "
            f"callback_data={callback_data}, "
            f"context_keys={list(context.user_data.keys()) if context.user_data else []}"
        )
        
        # Clear user_data_store to prevent check_add_state_entry from intercepting messages
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]
        # Explicitly clear add flow related keys from context.user_data
        if 'current_field' in context.user_data:
            del context.user_data['current_field']
        if 'current_state' in context.user_data:
            del context.user_data['current_state']
        if 'add_step' in context.user_data:
            del context.user_data['add_step']
        
        # Extract index from callback_data
        # Format: "tag_mgr_{index}"
        if not callback_data.startswith("tag_mgr_"):
            logger.error(f"[TAG] Invalid callback_data: {callback_data}")
            await query.edit_message_text(
                "❌ Ошибка: неверные данные. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Extract index
        try:
            index_str = callback_data.replace("tag_mgr_", "", 1)
            index = int(index_str)
        except (ValueError, IndexError):
            logger.error(f"[TAG] Invalid index in callback_data: {callback_data}")
            await query.edit_message_text(
                "❌ Ошибка: неверный индекс. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Get manager name from stored list
        # If list is not in context (e.g., if called as entry point), reload it
        manager_names = context.user_data.get('tag_manager_names')
        logger.info(f"[TAG] tag_manager_names in context for user {user_id}: {manager_names}")
        if not manager_names:
            logger.info(f"[TAG] manager_names not in context, reloading from database for user {user_id}")
            client = get_supabase_client()
            if not client:
                error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
                await query.edit_message_text(
                    error_msg,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
                return ConversationHandler.END
            manager_names = get_unique_manager_names(client)
            if not manager_names:
                await query.edit_message_text(
                    "❌ В базе данных нет записей с manager_name.",
                    reply_markup=get_main_menu_keyboard(),
                )
                return ConversationHandler.END
            context.user_data['tag_manager_names'] = manager_names
            logger.info(f"[TAG] Reloaded tag_manager_names for user {user_id}: {manager_names}")
        
        if index < 0 or index >= len(manager_names):
            logger.error(f"[TAG] Invalid index {index} for manager_names list of length {len(manager_names)}")
            await query.edit_message_text(
                "❌ Ошибка: неверный индекс. Попробуйте снова с команды /tag",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        manager_name = manager_names[index]
        logger.info(f"[TAG] User {user_id} selected manager_name='{manager_name}' (index={index})")
        
        # Save manager_name to context
        context.user_data['tag_manager_name'] = manager_name
        logger.info(f"[TAG] Saved tag_manager_name in context for user {user_id}, context_keys_now={list(context.user_data.keys()) if context.user_data else []}")
        
        # Ask for new tag
        await query.edit_message_text(
            f"🏷️ <b>Менеджер:</b> {escape_html(manager_name)}\n\n"
            f"📝 Введите новый тег (с @ или без):",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отменить", callback_data="tag_cancel")]
            ])
        )
        
        logger.info(f"[TAG] Prompted user {user_id} to enter new tag, expecting TAG_ENTER_NEW")
        return TAG_ENTER_NEW
    except Exception as e:
        logger.error(f"Error in tag_manager_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def quit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /q command - return to main menu from any state"""
    try:
        user_id = update.effective_user.id
        update_type = "message" if update.message else "callback_query" if update.callback_query else "unknown"
        logger.info(f"[QUIT] /q command received from user {user_id} (update_type: {update_type})")
        logger.info(f"[QUIT] Context keys before clear: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        
        # Clear all conversation state including internal ConversationHandler keys
        clear_all_conversation_state(context, user_id)
        
        logger.info(f"[QUIT] Context keys after clear: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        
        # Show main menu FIRST (fast response)
        welcome_message = (
            "👋 Главное меню\n\n"
            "Выберите действие:"
        )
        
        # Handle both message and callback_query
        if update.message:
            sent_message = await retry_telegram_api(
                update.message.reply_text,
                text=welcome_message,
                reply_markup=get_main_menu_keyboard()
            )
        elif update.callback_query:
            query = update.callback_query
            await retry_telegram_api(query.answer)
            try:
                await retry_telegram_api(
                    query.edit_message_text,
                    text=welcome_message,
                    reply_markup=get_main_menu_keyboard()
                )
            except Exception as e:
                # If edit fails, send new message
                logger.warning(f"[QUIT] Could not edit message: {e}")
                if query.message:
                    await retry_telegram_api(
                        query.message.reply_text,
                        text=welcome_message,
                        reply_markup=get_main_menu_keyboard()
                    )
        else:
            logger.error(f"[QUIT] No message or callback_query in update")
        
        # Clean up messages AFTER showing menu (in background, don't wait)
        # This ensures fast response to user
        # Use application.create_task to ensure it runs in the correct event loop
        if update.message or update.callback_query:
            context.application.create_task(cleanup_all_messages_before_main_menu(update, context))
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"[QUIT] Error in quit_command: {e}", exc_info=True)
        try:
            if update.message:
                await retry_telegram_api(
                    update.message.reply_text,
                    text="❌ Произошла ошибка. Попробуйте позже.",
                    reply_markup=get_main_menu_keyboard()
                )
            elif update.callback_query:
                await retry_telegram_api(update.callback_query.answer, text="❌ Произошла ошибка. Попробуйте позже.", show_alert=True)
        except:
            pass
        return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command - send guide file"""
    try:
        user_id = update.effective_user.id
        logger.info(f"[HELP] Help command received from user {user_id}")
        
        # Path to the guide file (relative to the script location)
        guide_path = "BOT_GUIDE.html"
        
        # Check if file exists
        if not os.path.exists(guide_path):
            logger.error(f"[HELP] Guide file not found at {guide_path}")
            await update.message.reply_text(
                "❌ Файл руководства временно недоступен.\n\n"
                "Обратитесь к администратору.",
                reply_markup=get_main_menu_keyboard()
            )
            return
        
        # Send the file as document
        # Telegram will allow user to download and open it
        # Using file path directly - python-telegram-bot will handle file opening
        await retry_telegram_api(
            update.message.reply_document,
            document=guide_path,
            filename="BOT_GUIDE.html",
            caption="📖 Руководство пользователя ClientsBot\n\n"
                   "Скачайте файл и откройте его в браузере для просмотра.",
            reply_markup=get_main_menu_keyboard()
        )
        
        logger.info(f"[HELP] Guide file sent successfully to user {user_id}")
        
    except FileNotFoundError:
        logger.error(f"[HELP] Guide file not found")
        await update.message.reply_text(
            "❌ Файл руководства не найден.\n\n"
            "Обратитесь к администратору.",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"[HELP] Error sending guide file: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Произошла ошибка при отправке руководства.\n\n"
            "Попробуйте позже или обратитесь к администратору.",
            reply_markup=get_main_menu_keyboard()
        )

# Callback query handlers
async def check_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle check_menu callback - start smart check input"""
    query = update.callback_query
    await retry_telegram_api(query.answer)
    
    user_id = query.from_user.id
    logger.info(f"[SMART_CHECK] Starting smart check for user {user_id}")
    
    # ALWAYS clear all conversation state to ensure clean start
    clear_all_conversation_state(context, user_id)
    # Also clear any stale ConversationHandler internal keys
    if context.user_data:
        keys_to_remove = [key for key in context.user_data.keys() if key.startswith('_conversation_')]
        for key in keys_to_remove:
            del context.user_data[key]
    
    # Clean up old check messages
    await cleanup_check_messages(update, context)
    
    # Show input prompt immediately
    try:
        await retry_telegram_api(
            query.edit_message_text,
            text="✅ Введите данные для поиска:\n\n"
                 "Можно ввести:\n"
                 "• Telegram username\n"
                 "• Telegram ID\n"
                 "• Имя клиента",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        logger.warning(f"Could not edit message in check_menu_callback: {e}")
        if query.message:
            await retry_telegram_api(
                query.message.reply_text,
                text="✅ Введите данные для поиска:\n\n"
                     "Можно ввести:\n"
                     "• Telegram username\n"
                     "• Telegram ID\n"
                     "• Имя клиента",
                reply_markup=get_check_back_keyboard()
            )
        else:
            logger.error("check_menu_callback: query.message is None")
            return ConversationHandler.END
    
    return SMART_CHECK_INPUT

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks for menu navigation"""
    query = update.callback_query
    await retry_telegram_api(query.answer)
    
    data = query.data
    
    # Note: edit_lead_* callbacks are now handled by ConversationHandler (edit_lead_entry_callback)
    
    if data == "main_menu":
        # Get current message ID to exclude from cleanup
        current_message_id = query.message.message_id if query.message else None
        
        # IMPORTANT: Remove current message from cleanup lists BEFORE cleanup
        # This prevents it from being deleted even if cleanup runs quickly
        if current_message_id:
            if 'add_message_ids' in context.user_data and current_message_id in context.user_data['add_message_ids']:
                context.user_data['add_message_ids'].remove(current_message_id)
            if 'last_check_messages' in context.user_data and current_message_id in context.user_data['last_check_messages']:
                context.user_data['last_check_messages'].remove(current_message_id)
        
        try:
            # Show main menu FIRST (fast response)
            await retry_telegram_api(
                query.edit_message_text,
                text="👋 Главное меню\n\nВыберите действие:",
                reply_markup=get_main_menu_keyboard()
            )
            
            # Clean up messages AFTER showing menu (in background, don't wait)
            # This ensures fast response to user
            # Add small delay to ensure edit completes before cleanup starts
            async def delayed_cleanup():
                import asyncio
                await asyncio.sleep(0.2)  # Small delay to ensure edit completes
                await cleanup_all_messages_before_main_menu(update, context, exclude_message_id=current_message_id)
            
            # Use application.create_task to ensure it runs in the correct event loop
            context.application.create_task(delayed_cleanup())
        except Exception as e:
            # If edit fails (message was deleted), send new message
            if "not found" in str(e) or "BadRequest" in str(type(e).__name__):
                try:
                    await retry_telegram_api(
                        query.message.reply_text,
                        text="👋 Главное меню\n\nВыберите действие:",
                        reply_markup=get_main_menu_keyboard()
                    )
                    # Clean up in background with delay
                    async def delayed_cleanup_fallback():
                        import asyncio
                        await asyncio.sleep(0.2)
                        await cleanup_all_messages_before_main_menu(update, context, exclude_message_id=current_message_id)
                    context.application.create_task(delayed_cleanup_fallback())
                except Exception as reply_error:
                    logger.error(f"Error sending reply message: {reply_error}", exc_info=True)
                    # Last resort: try to send without cleanup
                    try:
                        await retry_telegram_api(
                            context.bot.send_message,
                            chat_id=query.message.chat_id,
                            text="👋 Главное меню\n\nВыберите действие:",
                            reply_markup=get_main_menu_keyboard()
                        )
                    except Exception as send_error:
                        logger.error(f"Error sending message directly: {send_error}", exc_info=True)
            else:
                logger.error(f"Error in main_menu callback: {e}", exc_info=True)
                raise
    
    # check_menu is now handled by smart_check_conv ConversationHandler
    # elif data == "check_menu":
    #     return await check_menu_callback(update, context)
    
    elif data == "add_menu":
        await retry_telegram_api(
            query.edit_message_text,
            text="➕ Добавить клиента\n\nВыберите способ добавления:",
            reply_markup=get_add_menu_keyboard()
        )
    
    elif data == "add_new":
        # Fallback: if ConversationHandler doesn't catch this, handle it here
        logger.warning(f"add_new callback received in button_callback (should be handled by ConversationHandler)")
        try:
            # Try to call add_new_callback directly as fallback
            result = await add_new_callback(update, context)
            if result:
                return result
        except Exception as e:
            logger.error(f"Error in add_new fallback handler: {e}", exc_info=True)
            await retry_telegram_api(query.answer, text="❌ Произошла ошибка. Попробуйте снова.")
            await retry_telegram_api(
                query.edit_message_text,
                text="❌ Произошла ошибка при запуске добавления лида.\n\n"
                "Попробуйте снова или обратитесь к администратору.",
                reply_markup=get_main_menu_keyboard()
            )
    else:
        # Unknown callback data - should not happen, but handle gracefully
        logger.warning(f"Unknown callback data received: {data}")
        await retry_telegram_api(query.answer, text="⚠️ Неизвестная команда. Используйте меню.", show_alert=True)
        try:
            if query.message:
                await retry_telegram_api(
                    query.edit_message_text,
                    text="❌ Неизвестная команда.\n\n"
                    "Используйте кнопки меню для навигации.",
                    reply_markup=get_main_menu_keyboard()
                )
        except Exception as e:
            logger.error(f"Error handling unknown callback: {e}", exc_info=True)

# Global fallback handlers
async def unknown_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries that don't match any pattern"""
    query = update.callback_query
    if query:
        callback_data = query.data if query.data else ""
        user_id = query.from_user.id if query.from_user else None
        
        # Special handling for check_menu - try to activate ConversationHandler
        if callback_data == "check_menu":
            logger.info(f"[UNKNOWN_CALLBACK] check_menu callback not handled by ConversationHandler, trying to activate for user {user_id}")
            # Clear stale state and let ConversationHandler handle it
            if user_id:
                clear_all_conversation_state(context, user_id)
                # Also clear any stale ConversationHandler internal keys
                if context.user_data:
                    keys_to_remove = [key for key in context.user_data.keys() if key.startswith('_conversation_')]
                    for key in keys_to_remove:
                        del context.user_data[key]
            # Answer callback and let ConversationHandler process it
            try:
                await retry_telegram_api(query.answer)
            except:
                pass
            # Return None to let ConversationHandler process it
            return None
        
        # Check if we're in a stale ConversationHandler state
        if context.user_data:
            has_conversation_keys = any(key.startswith('_conversation_') for key in context.user_data.keys())
            if has_conversation_keys:
                logger.warning(f"[UNKNOWN_CALLBACK] Stale ConversationHandler state detected for callback '{callback_data}'. Clearing state for user {user_id}")
                if user_id:
                    clear_all_conversation_state(context, user_id)
                # Don't show error for stale callbacks - just answer silently
                try:
                    await retry_telegram_api(query.answer)
                except:
                    pass
                return ConversationHandler.END
        
        # Log the unknown callback for debugging, with state
        log_conversation_state(user_id or -1, context, prefix="[UNKNOWN_CALLBACK_STATE]")
        logger.warning(f"[UNKNOWN_CALLBACK] Unknown callback query: '{callback_data}' from user {user_id}")
        
        try:
            await retry_telegram_api(query.answer, text="⚠️ Неизвестная команда. Используйте меню.", show_alert=True)
            if query.message:
                # Check if message already shows main menu to avoid "Message is not modified" error
                message_text = query.message.text or ""
                has_main_menu_buttons = "Проверить" in message_text or "Добавить" in message_text
                
                # Only edit if message doesn't already show main menu
                if not has_main_menu_buttons:
                    await retry_telegram_api(
                        query.edit_message_text,
                        text="❌ Неизвестная команда.\n\n"
                        "Используйте кнопки меню для навигации.",
                        reply_markup=get_main_menu_keyboard()
                    )
                else:
                    # Message already shows main menu, just answer callback
                    logger.info(f"[UNKNOWN_CALLBACK] Message already shows main menu, skipping edit for callback '{callback_data}'")
        except Exception as e:
            logger.error(f"Error in unknown_callback_handler: {e}", exc_info=True)
    return ConversationHandler.END

async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle commands sent during ConversationHandler (except /q, /start, /skip, and /tag)"""
    if not update.message or not update.message.text:
        return
    
    command = update.message.text.strip().split()[0] if update.message.text else ""
    
    # CRITICAL: Never intercept /start, /q, /skip, or /tag - they must be handled by their dedicated handlers
    # /tag should always interrupt current process and start its own flow
    # This prevents issues when ConversationHandler is in a stale state after deploy
    if command in ["/q", "/start", "/skip", "/tag"]:
        logger.warning(f"[UNKNOWN_CMD] Ignoring {command} - should be handled by dedicated handler. Context keys: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        return None  # Return None to let other handlers process it
    
    # Check if we're in a stale ConversationHandler state (has _conversation_ keys but shouldn't)
    has_conversation_keys = any(key.startswith('_conversation_') for key in (context.user_data.keys() if context.user_data else []))
    if has_conversation_keys:
        logger.warning(f"[UNKNOWN_CMD] Command {command} intercepted but ConversationHandler appears stale. Clearing state.")
        user_id = update.effective_user.id
        clear_all_conversation_state(context, user_id)
        # After clearing, let the command be processed by its handler
        return None
    
    # Show message that command is not available during conversation
    try:
        logger.info(f"[UNKNOWN_CMD] Command {command} not available during active conversation")
        await update.message.reply_text(
            f"⚠️ Команда {command} недоступна во время выполнения операции.\n\n"
            "Используйте /q для возврата в главное меню или завершите текущую операцию.",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Error in unknown_command_handler: {e}", exc_info=True)
    
    # Don't end conversation, let user continue or use /q
    return None

# Message cleanup functions
async def cleanup_check_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean up old check messages when starting a new check"""
    if 'last_check_messages' in context.user_data and context.user_data['last_check_messages']:
        chat_id = update.effective_chat.id
        bot = context.bot
        message_ids = context.user_data['last_check_messages']
        
        for msg_id in message_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                # Message might be too old or already deleted, ignore
                pass
        
        # Clear the list
        context.user_data['last_check_messages'] = []

async def save_check_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Save message ID for later cleanup"""
    if 'last_check_messages' not in context.user_data:
        context.user_data['last_check_messages'] = []
    context.user_data['last_check_messages'].append(message_id)

async def cleanup_add_messages(update: Update, context: ContextTypes.DEFAULT_TYPE, exclude_message_id: int = None):
    """Clean up all add flow messages except the final success message and optionally exclude a specific message"""
    user_id = update.effective_user.id
    if 'add_message_ids' in context.user_data and context.user_data['add_message_ids']:
        chat_id = update.effective_chat.id
        bot = context.bot
        message_ids = context.user_data['add_message_ids'].copy()
        
        for msg_id in message_ids:
            # Skip the message we want to exclude (e.g., review screen)
            if exclude_message_id and msg_id == exclude_message_id:
                continue
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                # Message might be too old or already deleted, ignore
                pass
        
        # Clear the list
        context.user_data['add_message_ids'] = []

async def save_add_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Save message ID for later cleanup after successful add"""
    if 'add_message_ids' not in context.user_data:
        context.user_data['add_message_ids'] = []
    context.user_data['add_message_ids'].append(message_id)

async def cleanup_all_messages_before_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, exclude_message_id: int = None):
    """Clean up all intermediate bot messages before showing main menu"""
    chat_id = update.effective_chat.id
    bot = context.bot
    
    # Collect all message IDs to delete
    message_ids_to_delete = []
    
    # Clean up add flow messages
    if 'add_message_ids' in context.user_data and context.user_data['add_message_ids']:
        message_ids_to_delete.extend(context.user_data['add_message_ids'])
        context.user_data['add_message_ids'] = []
    
    # Clean up check flow messages
    if 'last_check_messages' in context.user_data and context.user_data['last_check_messages']:
        message_ids_to_delete.extend(context.user_data['last_check_messages'])
        context.user_data['last_check_messages'] = []
    
    # Remove excluded message ID if provided (double-check to be safe)
    if exclude_message_id:
        # Remove from list if present (multiple times to be absolutely sure)
        while exclude_message_id in message_ids_to_delete:
            message_ids_to_delete.remove(exclude_message_id)
    
    # Delete messages in parallel for better performance
    if message_ids_to_delete:
        import asyncio
        delete_tasks = []
        for msg_id in message_ids_to_delete:
            delete_tasks.append(
                bot.delete_message(chat_id=chat_id, message_id=msg_id)
            )
        
        # Execute deletions in parallel, but don't wait for all to complete
        # This speeds up the response
        try:
            # Use asyncio.gather with return_exceptions=True to not fail on individual errors
            results = await asyncio.gather(*delete_tasks, return_exceptions=True)
        except Exception:
            pass

# Check callbacks
async def check_telegram_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for check by telegram conversation"""
    query = update.callback_query
    if not query:
        logger.error("check_telegram_callback: query is None")
        return ConversationHandler.END
    
    await query.answer()
    
    user_id = query.from_user.id
    logger.info(f"[CHECK_TELEGRAM] Clearing state before entry for user {user_id}")
    
    # Explicitly clear all conversation state including internal ConversationHandler keys
    # This prevents issues when re-entering after /q or stale states after deploy
    clear_all_conversation_state(context, user_id)
    
    # Clean up old check messages if any
    await cleanup_check_messages(update, context)
    
    try:
        await query.edit_message_text(
            "📱 Введите Имя пользователя Telegram для проверки:",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        # If message can't be edited (e.g., already deleted), send new message
        logger.warning(f"Could not edit message in check_telegram_callback: {e}")
        if query.message:
            await query.message.reply_text(
                "📱 Введите Имя пользователя Telegram для проверки:",
                reply_markup=get_check_back_keyboard()
            )
        else:
            logger.error("check_telegram_callback: query.message is None")
            return ConversationHandler.END
    
    return CHECK_BY_TELEGRAM

async def check_fb_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for check by facebook link conversation"""
    query = update.callback_query
    if not query:
        logger.error("check_fb_link_callback: query is None")
        return ConversationHandler.END
    
    await retry_telegram_api(query.answer)
    
    user_id = query.from_user.id
    logger.info(f"[CHECK_FB_LINK] Clearing state before entry for user {user_id}")
    
    # Explicitly clear all conversation state including internal ConversationHandler keys
    # This prevents issues when re-entering after /q or stale states after deploy
    clear_all_conversation_state(context, user_id)
    
    # Clean up old check messages if any
    await cleanup_check_messages(update, context)
    
    try:
        await retry_telegram_api(
            query.edit_message_text,
            text="🔗 Введите Facebook Ссылка для проверки:",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        # If message can't be edited (e.g., already deleted), send new message
        logger.warning(f"Could not edit message in check_fb_link_callback: {e}")
        if query.message:
            await retry_telegram_api(
                query.message.reply_text,
                text="🔗 Введите Facebook Ссылка для проверки:",
                reply_markup=get_check_back_keyboard()
            )
        else:
            logger.error("check_fb_link_callback: query.message is None")
            return ConversationHandler.END
    
    return CHECK_BY_FB_LINK

async def check_fullname_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for check by fullname conversation"""
    query = update.callback_query
    if not query:
        logger.error("check_fullname_callback: query is None")
        return ConversationHandler.END
    
    await retry_telegram_api(query.answer)
    
    user_id = query.from_user.id
    logger.info(f"[CHECK_FULLNAME] Clearing state before entry for user {user_id}")
    
    # Explicitly clear all conversation state including internal ConversationHandler keys
    # This prevents issues when re-entering after /q or stale states after deploy
    clear_all_conversation_state(context, user_id)
    
    # Clean up old check messages if any
    await cleanup_check_messages(update, context)
    
    try:
        await retry_telegram_api(
            query.edit_message_text,
            text="👤 Введите имя клиента (или фамилию):",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        # If message can't be edited (e.g., already deleted), send new message
        logger.warning(f"Could not edit message in check_fullname_callback: {e}")
        if query.message:
            await retry_telegram_api(
                query.message.reply_text,
                text="👤 Введите имя клиента (или фамилию):",
                reply_markup=get_check_back_keyboard()
            )
        else:
            logger.error("check_fullname_callback: query.message is None")
            return ConversationHandler.END
    
    return CHECK_BY_FULLNAME

# Add callback - new sequential flow
async def add_new_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start adding new lead - sequential flow"""
    query = update.callback_query
    try:
        await query.answer()
        user_id = query.from_user.id
        
        logger.info(f"[ADD_NEW] Clearing state before entry for user {user_id}")
        
        # Explicitly clear all conversation state including internal ConversationHandler keys
        # This prevents issues when re-entering after /q or stale states after deploy
        clear_all_conversation_state(context, user_id)
        
        # Initialize fresh state for new add flow
        user_data_store[user_id] = {}
        user_data_store_access_time[user_id] = time.time()
        context.user_data['current_field'] = 'fullname'
        context.user_data['current_state'] = ADD_FULLNAME  # ЯВНО устанавливаем состояние
        context.user_data['add_step'] = 0
        
        # Start with first field: Full Name
        field_label = get_field_label('fullname')
        _, _, current_step, total_steps = get_next_add_field('')
        
        # Убираем описание для первого шага
        message = f"<b>Шаг {current_step} из {total_steps}</b>\n\n📝 Введите {field_label}:"
        
        await retry_telegram_api(
            query.edit_message_text,
            text=message,
            reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
            parse_mode='HTML'
        )
        # Save message ID for cleanup
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        return ADD_FULLNAME
    except Exception as e:
        logger.error(f"Error in add_new_callback: {e}", exc_info=True)
        try:
            await query.answer("❌ Произошла ошибка. Попробуйте снова.")
            await query.edit_message_text(
                "❌ Произошла ошибка при запуске добавления лида.\n\n"
                "Попробуйте снова или обратитесь к администратору.",
                reply_markup=get_main_menu_keyboard()
            )
        except Exception as fallback_error:
            logger.error(f"Error in add_new_callback fallback: {fallback_error}", exc_info=True)
        return ConversationHandler.END

# Search by multiple fields function
async def check_by_multiple_fields(update: Update, context: ContextTypes.DEFAULT_TYPE, search_value: str):
    """
    Search across multiple fields simultaneously using OR conditions.
    Searches in: telegram_user, telegram_id, fullname
    """
    if not update.message:
        logger.error(f"[MULTI_FIELD_SEARCH] update.message is None")
        return ConversationHandler.END
    
    logger.info(f"[MULTI_FIELD_SEARCH] Starting multi-field search with value: '{search_value}'")
    
    # Get Supabase client
    client = get_supabase_client()
    if not client:
        error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        return ConversationHandler.END
    
    try:
        # Try to normalize values for different field types
        normalized_tg_user = None
        normalized_tg_id = None
        normalized_fullname = None
        
        # Try Telegram username normalization
        is_valid_tg_user, _, tg_user_normalized = validate_telegram_name(search_value)
        if is_valid_tg_user:
            normalized_tg_user = tg_user_normalized
        
        # Try Telegram ID normalization
        if search_value.isdigit() and len(search_value) >= 5:
            normalized_tg_id = normalize_telegram_id(search_value)
        
        # Normalize for fullname search (contains pattern)
        if len(search_value.strip()) >= 3:
            normalized_fullname = re.sub(r'\s+', ' ', search_value.strip())
            # Escape special characters for ILIKE
            normalized_fullname = normalized_fullname.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        
        # Build query with OR conditions
        # Supabase Python client doesn't support .or() directly, so we need to use multiple queries
        # or use a different approach. Let's use multiple queries and combine results.
        
        all_results = []
        seen_ids = set()
        
        # Search in telegram_user (exact match)
        if normalized_tg_user:
            try:
                response = client.table(TABLE_NAME).select("*").eq("telegram_user", normalized_tg_user).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching telegram_user: {e}")
        
        # Search in telegram_id (exact match)
        if normalized_tg_id:
            try:
                response = client.table(TABLE_NAME).select("*").eq("telegram_id", normalized_tg_id).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching telegram_id: {e}")
        
        # Search in fullname (contains pattern, case-insensitive)
        if normalized_fullname:
            try:
                pattern = f"%{normalized_fullname}%"
                response = client.table(TABLE_NAME).select("*").ilike("fullname", pattern).limit(50).execute()
                if response.data:
                    for item in response.data:
                        if item.get('id') not in seen_ids:
                            all_results.append(item)
                            seen_ids.add(item.get('id'))
            except Exception as e:
                logger.warning(f"[MULTI_FIELD_SEARCH] Error searching fullname: {e}")
        
        # Limit total results to 50
        all_results = all_results[:50]
        
        # Field labels mapping (Russian)
        field_labels = {
            'fullname': 'Клиент',
            'facebook_link': 'Facebook Ссылка',
            'telegram_user': 'Имя пользователя Telegram',
            'telegram_id': 'Telegram ID',
            'manager_name': 'Добавил',
            'manager_tag': 'Тег',
            'photo_url': 'Фото',
            'created_at': 'Дата'
        }
        
        if all_results:
            # Show results
            photo_url = None  # Initialize for multiple results case
            if len(all_results) > 1:
                message_parts = [f"✅ <b>Найдено клиентов: {len(all_results)}</b>\n"]
                
                for idx, result in enumerate(all_results, 1):
                    if idx > 1:
                        message_parts.append("")
                    message_parts.append(f"<b>━━━ Клиент {idx} ━━━</b>")
                    for field_name_key, field_label in field_labels.items():
                        value = result.get(field_name_key)
                        
                        if value is None or value == '' or value == 'Не указано':
                            continue
                        
                        # Format date field
                        if field_name_key == 'created_at':
                            try:
                                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                                value = dt.strftime('%d.%m.%Y %H:%M')
                            except:
                                pass
                        
                        # Format Facebook link to full URL
                        if field_name_key == 'facebook_link':
                            value = format_facebook_link_for_display(value)
                        
                        # Format manager_tag as clickable Telegram mention (Telegram auto-detects @username)
                        if field_name_key == 'manager_tag':
                            tag_value = str(value).strip()
                            message_parts.append(f"{field_label}: @{tag_value}")
                        elif field_name_key == 'photo_url':
                            # Format photo_url as clickable link
                            url = str(value).strip()
                            if url:
                                message_parts.append(f"{field_label}: <a href=\"{url}\">🔗 Открыть фото</a>")
                        else:
                            escaped_value = escape_html(str(value))
                            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
            else:
                # Single result
                result = all_results[0]
                message_parts = ["✅ <b>Лид найден</b>", ""]
                
                # Check if photo exists
                photo_url = result.get('photo_url')
                if photo_url:
                    photo_url = str(photo_url).strip()
                
                for field_name_key, field_label in field_labels.items():
                    value = result.get(field_name_key)
                    
                    if value is None or value == '' or value == 'Не указано':
                        continue
                    
                    # Skip photo_url field - we'll send it as attached image
                    if field_name_key == 'photo_url':
                        continue
                    
                    # Format date field
                    if field_name_key == 'created_at':
                        try:
                            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                            value = dt.strftime('%d.%m.%Y %H:%M')
                        except:
                            pass
                    
                    # Format Facebook link to full URL
                    if field_name_key == 'facebook_link':
                        value = format_facebook_link_for_display(value)
                    
                    # Format manager_tag as clickable Telegram mention (Telegram auto-detects @username)
                    if field_name_key == 'manager_tag':
                        tag_value = str(value).strip()
                        message_parts.append(f"{field_label}: @{tag_value}")
                    else:
                        escaped_value = escape_html(str(value))
                        message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
            
            message = "\n".join(message_parts)
            
            # Build inline keyboard for editing
            keyboard = []
            if len(all_results) == 1:
                lead_id = all_results[0].get('id')
                if lead_id is not None:
                    keyboard.append([InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_lead_{lead_id}")])
            else:
                for idx, result in enumerate(all_results, 1):
                    lead_id = result.get('id')
                    if lead_id is None:
                        continue
                    name = result.get('fullname') or "без имени"
                    label = f"✏️ Клиент {idx} ({name})"
                    if len(label) > 60:
                        label = label[:57] + "..."
                    keyboard.append([InlineKeyboardButton(label, callback_data=f"edit_lead_{lead_id}")])
            
            keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
        else:
            message = "❌ <b>Клиент не найден</b>."
            reply_markup = get_main_menu_keyboard()
            photo_url = None
        
        # Send message with photo if available (only for single result)
        if len(all_results) == 1 and photo_url:
            try:
                # Try to download and send as file
                photo_bytes = await download_photo_from_supabase(photo_url)
                if photo_bytes:
                    photo_file = io.BytesIO(photo_bytes)
                    sent_message = await update.message.reply_photo(
                        photo=photo_file,
                        caption=message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                else:
                    # If download fails, send text with link
                    sent_message = await update.message.reply_text(
                        message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"[MULTIPLE FIELDS SEARCH] Error sending photo: {e}", exc_info=True)
                # Fallback: send text with link
                sent_message = await update.message.reply_text(
                    message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
        else:
            sent_message = await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        await save_check_message(update, context, sent_message.message_id)
        
    except Exception as e:
        logger.error(f"[MULTI_FIELD_SEARCH] ❌ Error in multi-field search: {e}", exc_info=True)
        error_msg = get_user_friendly_error(e, "проверке")
        sent_message = await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        await save_check_message(update, context, sent_message.message_id)
    
    return ConversationHandler.END

# Universal check function
async def check_by_field(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str, field_label: str, current_state: int):
    """Universal function to check by any field"""
    # Validate that update has a message
    if not update.message:
        logger.error(f"[CHECK_BY_FIELD] update.message is None for field '{field_name}'. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        logger.error(f"[CHECK_BY_FIELD] Context keys: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        # Try to get user_id from callback_query if available
        user_id = update.effective_user.id if update.effective_user else None
        if user_id:
            logger.error(f"[CHECK_BY_FIELD] User ID: {user_id}")
        # Return END to exit conversation
        return ConversationHandler.END
    
    if not update.message.text:
        logger.error(f"[CHECK_BY_FIELD] update.message.text is None for field '{field_name}'")
        return ConversationHandler.END
    
    search_value = update.message.text.strip()
    
    if not search_value:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
        sent_message = await update.message.reply_text(
            f"❌ {field_label} не может быть пустым.",
            reply_markup=keyboard
        )
        await save_check_message(update, context, sent_message.message_id)
        return ConversationHandler.END
    
    # Map internal field names to database column names
    FIELD_NAME_MAPPING = {
        'telegram_name': 'telegram_user',  # Map telegram_name to telegram_user for database
    }
    
    # Get database column name
    db_field_name = FIELD_NAME_MAPPING.get(field_name, field_name)
    
    # Add detailed logging for all search types
    search_type_map = {
        'fullname': 'FULLNAME SEARCH',
        'facebook_link': 'FACEBOOK SEARCH',
        'telegram_user': 'TELEGRAM USER SEARCH',
        'telegram_id': 'TELEGRAM ID SEARCH'
    }
    search_type = search_type_map.get(field_name, f'{field_name.upper()} SEARCH')
    logger.info(f"[{search_type}] Starting search with value: '{search_value}' (length: {len(search_value)}, type: {type(search_value)})")
    
    # Normalize Facebook link if checking by facebook_link
    if field_name == "facebook_link":
        # Use validate_facebook_link to normalize the link (same logic as when adding)
        is_valid, error_msg, normalized = validate_facebook_link(search_value)
        if not is_valid:
            logger.warning(f"[{search_type}] ❌ Validation failed: {error_msg}")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            await save_check_message(update, context, sent_message.message_id)
            return ConversationHandler.END
        search_value = normalized
        logger.info(f"[{search_type}] Normalized Facebook link: '{normalized}'")
    
    # Normalize Telegram Name if checking by telegram_user
    elif field_name == "telegram_user":
        # Use same normalization as when adding (remove @, spaces)
        is_valid, error_msg, normalized = validate_telegram_name(search_value)
        if not is_valid:
            logger.warning(f"[{search_type}] ❌ Validation failed: {error_msg}")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}",
                reply_markup=keyboard
            )
            await save_check_message(update, context, sent_message.message_id)
            return ConversationHandler.END
        search_value = normalized
        logger.info(f"[{search_type}] Normalized Telegram username: '{normalized}'")
    
    # Get Supabase client (for all fields, not just phone)
    client = get_supabase_client()
    if not client:
        error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        return ConversationHandler.END
    
    try:
        # For all fields: exact match, limit to 50 results
        logger.info(f"[{search_type}] Executing query: SELECT * FROM {TABLE_NAME} WHERE {db_field_name} = '{search_value}' LIMIT 50")
        response = client.table(TABLE_NAME).select("*").eq(db_field_name, search_value).limit(50).execute()
        logger.info(f"[{search_type}] Query executed. Response type: {type(response)}, has data: {hasattr(response, 'data')}")
        logger.info(f"[{search_type}] Response.data length: {len(response.data) if hasattr(response, 'data') and response.data else 0}")
        
        # Field labels mapping (Russian) - use database column names
        field_labels = {
            'fullname': 'Клиент',
            'facebook_link': 'Facebook Ссылка',
            'telegram_user': 'Имя пользователя Telegram',  # Changed from telegram_name to telegram_user
            'telegram_id': 'Telegram ID',
            'manager_name': 'Добавил',
            'manager_tag': 'Тег',
            'photo_url': 'Фото',
            'created_at': 'Дата'
        }
        
        if response.data and len(response.data) > 0:
            results = response.data
            photo_url = None  # Initialize for multiple results case
            
            # If multiple results, show all
            if len(results) > 1:
                message_parts = [f"✅ <b>Найдено клиентов: {len(results)}</b>\n"]
                
                for idx, result in enumerate(results, 1):
                    if idx > 1:
                        message_parts.append("")  # Empty line between leads
                    message_parts.append(f"<b>━━━ Клиент {idx} ━━━</b>")
                    for field_name_key, field_label in field_labels.items():
                        value = result.get(field_name_key)
                        
                        # Skip if None, empty string, or 'Не указано'
                        if value is None or value == '' or value == 'Не указано':
                            continue
                        
                        # Format date field
                        if field_name_key == 'created_at':
                            try:
                                dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                                value = dt.strftime('%d.%m.%Y %H:%M')
                            except:
                                pass
                        
                        # Format Facebook link to full URL
                        if field_name_key == 'facebook_link':
                            value = format_facebook_link_for_display(value)
                        
                        # Format manager_tag as clickable Telegram mention (Telegram auto-detects @username)
                        if field_name_key == 'manager_tag':
                            tag_value = str(value).strip()
                            message_parts.append(f"{field_label}: @{tag_value}")
                        elif field_name_key == 'photo_url':
                            # Format photo_url as clickable link
                            url = str(value).strip()
                            if url:
                                message_parts.append(f"{field_label}: <a href=\"{url}\">🔗 Открыть фото</a>")
                        else:
                            # Format value in code tags for easy copying
                            escaped_value = escape_html(str(value))
                            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
            else:
                # Single result
                result = results[0]
                message_parts = ["✅ <b>Лид найден</b>", ""]  # Empty line after header
                
                # Check if photo exists
                photo_url = result.get('photo_url')
                if photo_url:
                    photo_url = str(photo_url).strip()
                
                for field_name_key, field_label in field_labels.items():
                    value = result.get(field_name_key)
                    
                    # Skip if None, empty string, or 'Не указано'
                    if value is None or value == '' or value == 'Не указано':
                        continue
                    
                    # Skip photo_url field - we'll send it as attached image
                    if field_name_key == 'photo_url':
                        continue
                    
                    # Format date field
                    if field_name_key == 'created_at':
                        try:
                            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                            value = dt.strftime('%d.%m.%Y %H:%M')
                        except:
                            pass
                    
                    # Format Facebook link to full URL
                    if field_name_key == 'facebook_link':
                        value = format_facebook_link_for_display(value)
                    
                    # Format manager_tag as clickable Telegram mention (Telegram auto-detects @username)
                    if field_name_key == 'manager_tag':
                        tag_value = str(value).strip()
                        message_parts.append(f"{field_label}: @{tag_value}")
                    else:
                        # Format value in code tags for easy copying
                        escaped_value = escape_html(str(value))
                        message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
            
            message = "\n".join(message_parts)

            # Build inline keyboard for editing
            keyboard = []
            if len(results) == 1:
                lead_id = results[0].get('id')
                if lead_id is not None:
                    keyboard.append([InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_lead_{lead_id}")])
            else:
                for idx, result in enumerate(results, 1):
                    lead_id = result.get('id')
                    if lead_id is None:
                        continue
                    name = result.get('fullname') or "без имени"
                    label = f"✏️ Клиент {idx} ({name})"
                    if len(label) > 60:
                        label = label[:57] + "..."
                    keyboard.append([InlineKeyboardButton(label, callback_data=f"edit_lead_{lead_id}")])
            
            # Add main menu button
            keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
        else:
            logger.warning(f"[{search_type}] ❌ No results found for {db_field_name} = '{search_value}'")
            message = "❌ <b>Клиент не найден</b>."
            reply_markup = get_main_menu_keyboard()
            photo_url = None
        
        # Send message with photo if available (only for single result)
        if len(results) == 1 and photo_url:
            try:
                # Try to download and send as file
                photo_bytes = await download_photo_from_supabase(photo_url)
                if photo_bytes:
                    photo_file = io.BytesIO(photo_bytes)
                    sent_message = await update.message.reply_photo(
                        photo=photo_file,
                        caption=message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                else:
                    # If download fails, send text with link
                    sent_message = await update.message.reply_text(
                        message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"[FIELD SEARCH] Error sending photo: {e}", exc_info=True)
                # Fallback: send text with link
                sent_message = await update.message.reply_text(
                    message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
        else:
            sent_message = await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        # Save message ID for cleanup
        await save_check_message(update, context, sent_message.message_id)
        
    except Exception as e:
        logger.error(f"[{search_type}] ❌ Error checking by {field_name}: {e}", exc_info=True)
        error_msg = get_user_friendly_error(e, "проверке")
        sent_message = await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        await save_check_message(update, context, sent_message.message_id)
    
    return ConversationHandler.END

async def check_by_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check by fullname using contains search with limit of 10 results"""
    # Validate that update has a message
    if not update.message:
        logger.error(f"[FULLNAME SEARCH] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        logger.error(f"[FULLNAME SEARCH] Context keys: {list(context.user_data.keys()) if context.user_data else 'empty'}")
        user_id = update.effective_user.id if update.effective_user else None
        if user_id:
            logger.error(f"[FULLNAME SEARCH] User ID: {user_id}")
        return ConversationHandler.END
    
    if not update.message.text:
        logger.error(f"[FULLNAME SEARCH] update.message.text is None")
        return ConversationHandler.END
    
    search_value = update.message.text.strip()
    
    logger.info(f"[FULLNAME SEARCH] Starting search with value: '{search_value}' (length: {len(search_value)}, type: {type(search_value)})")
    
    if not search_value:
        await update.message.reply_text("❌ Имя не может быть пустым. Попробуйте снова:")
        return CHECK_BY_FULLNAME
    
    # Validate minimum length for fullname search
    if len(search_value) < 3:
        await update.message.reply_text("❌ Для поиска по имени необходимо минимум 3 символа. Попробуйте снова:")
        return CHECK_BY_FULLNAME
    
    # Normalize search value: remove extra spaces, trim
    search_value = re.sub(r'\s+', ' ', search_value).strip()
    
    # Escape special characters for LIKE/ILIKE pattern matching
    # In SQL LIKE/ILIKE, % and _ are special characters that need to be escaped
    # Escape % and _ by replacing them with \% and \_
    escaped_search_value = search_value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    
    logger.info(f"[FULLNAME SEARCH] Normalized search value: '{search_value}' -> escaped: '{escaped_search_value}'")
    
    # Get Supabase client
    client = get_supabase_client()
    if not client:
        logger.error("[FULLNAME SEARCH] Failed to get Supabase client")
        error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        return ConversationHandler.END
    
    try:
        # Search using ilike with contains pattern (case-insensitive)
        # Limit to 10 results at DB level for better performance
        # Sort by created_at descending (newest first)
        # For ilike in Supabase Python client, use % as wildcard (SQL standard)
        # Pattern: %escaped_value% - finds records where fullname contains the search value
        # This works for both full matches and partial matches
        pattern = f"%{escaped_search_value}%"
        logger.info(f"[FULLNAME SEARCH] Using pattern: '{pattern}' for field 'fullname'")
        logger.info(f"[FULLNAME SEARCH] Executing query: SELECT * FROM {TABLE_NAME} WHERE fullname ILIKE '{pattern}' ORDER BY created_at DESC LIMIT 10")
        
        response = client.table(TABLE_NAME).select("*").ilike("fullname", pattern).order("created_at", desc=True).limit(10).execute()
        
        logger.info(f"[FULLNAME SEARCH] Query executed. Response type: {type(response)}, has data: {hasattr(response, 'data')}")
        logger.info(f"[FULLNAME SEARCH] Response.data type: {type(response.data) if hasattr(response, 'data') else 'N/A'}")
        logger.info(f"[FULLNAME SEARCH] Response.data length: {len(response.data) if hasattr(response, 'data') and response.data else 0}")
        
        if hasattr(response, 'data') and response.data:
            logger.info(f"[FULLNAME SEARCH] ✅ Found {len(response.data)} results for pattern '{pattern}'")
            for idx, result in enumerate(response.data[:5], 1):  # Log first 5 results
                fullname = result.get('fullname', 'N/A')
                logger.info(f"[FULLNAME SEARCH] Result {idx}: id={result.get('id')}, fullname='{fullname}' (matches: {escaped_search_value.lower() in str(fullname).lower() if fullname else False})")
        else:
            logger.warning(f"[FULLNAME SEARCH] ❌ No results found for pattern '{pattern}' (search_value: '{search_value}', escaped: '{escaped_search_value}')")
        
        # Field labels mapping (Russian)
        field_labels = {
            'fullname': 'Клиент',
            'facebook_link': 'Facebook Ссылка',
            'telegram_user': 'Имя пользователя Telegram',  # Changed from telegram_name to telegram_user
            'telegram_id': 'Telegram ID',
            'manager_name': 'Добавил',
            'manager_tag': 'Тег',
            'photo_url': 'Фото',
            'created_at': 'Дата'
        }
        
        if response.data and len(response.data) > 0:
            results = response.data
            photo_url = None  # Initialize for multiple results case
            
            # Check if more than 10 results
            if len(results) > 10:
                await update.message.reply_text(
                    "❌ Слишком много совпадений. Попробуйте другой фильтр поиска.",
                    reply_markup=get_main_menu_keyboard()
                )
                return ConversationHandler.END
            
            # If multiple results, show all
            if len(results) > 1:
                message_parts = [f"✅ <b>Найдено клиентов: {len(results)}</b>\n"]
                
                for idx, result in enumerate(results, 1):
                    if idx > 1:
                        message_parts.append("")  # Empty line between leads
                    message_parts.append(f"<b>━━━ Клиент {idx} ━━━</b>")
                    for field_name_key, field_label in field_labels.items():
                        value = result.get(field_name_key)
                        
                        # Skip if None, empty string, or 'Не указано'
                        if value is None or value == '' or value == 'Не указано':
                            continue
                    
                    # Format date field
                    if field_name_key == 'created_at':
                        try:
                            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                            value = dt.strftime('%d.%m.%Y %H:%M')
                        except:
                            pass
                    
                    # Format Facebook link to full URL
                    if field_name_key == 'facebook_link':
                        value = format_facebook_link_for_display(value)
                    
                    # Format manager_tag as clickable Telegram mention (Telegram auto-detects @username)
                    if field_name_key == 'manager_tag':
                        tag_value = str(value).strip()
                        message_parts.append(f"{field_label}: @{tag_value}")
                    elif field_name_key == 'photo_url':
                        # Format photo_url as clickable link
                        url = str(value).strip()
                        if url:
                            message_parts.append(f"{field_label}: <a href=\"{url}\">🔗 Открыть фото</a>")
                    else:
                        # Format value in code tags for easy copying
                        escaped_value = escape_html(str(value))
                        message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
            else:
                # Single result
                result = results[0]
                message_parts = ["✅ <b>Лид найден</b>", ""]  # Empty line after header
                
                # Check if photo exists
                photo_url = result.get('photo_url')
                if photo_url:
                    photo_url = str(photo_url).strip()
                
                for field_name_key, field_label in field_labels.items():
                    value = result.get(field_name_key)
                    
                    # Skip if None, empty string, or 'Не указано'
                    if value is None or value == '' or value == 'Не указано':
                        continue
                    
                    # Skip photo_url field - we'll send it as attached image
                    if field_name_key == 'photo_url':
                        continue
            
                    # Format date field
                    if field_name_key == 'created_at':
                        try:
                            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                            value = dt.strftime('%d.%m.%Y %H:%M')
                        except:
                            pass
                    
                    # Format Facebook link to full URL
                    if field_name_key == 'facebook_link':
                        value = format_facebook_link_for_display(value)
                    
                    # Format manager_tag as clickable Telegram mention (Telegram auto-detects @username)
                    if field_name_key == 'manager_tag':
                        tag_value = str(value).strip()
                        message_parts.append(f"{field_label}: @{tag_value}")
                    else:
                        # Format value in code tags for easy copying
                        escaped_value = escape_html(str(value))
                        message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
            
            message = "\n".join(message_parts)

            # Build inline keyboard for editing
            keyboard = []
            if len(results) == 1:
                lead_id = results[0].get('id')
                if lead_id is not None:
                    keyboard.append([InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_lead_{lead_id}")])
            else:
                for idx, result in enumerate(results, 1):
                    lead_id = result.get('id')
                    if lead_id is None:
                        continue
                    name = result.get('fullname') or "без имени"
                    label = f"✏️ Клиент {idx} ({name})"
                    if len(label) > 60:
                        label = label[:57] + "..."
                    keyboard.append([InlineKeyboardButton(label, callback_data=f"edit_lead_{lead_id}")])
            
            # Add main menu button
            keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
        else:
            logger.warning(f"[FULLNAME SEARCH] ❌ No results found for pattern '{pattern}' (search_value: '{search_value}', escaped: '{escaped_search_value}')")
            message = "❌ <b>Клиент не найден</b>."
            reply_markup = get_main_menu_keyboard()
            photo_url = None
        
        # Send message with photo if available (only for single result)
        if len(results) == 1 and photo_url:
            try:
                # Try to download and send as file
                photo_bytes = await download_photo_from_supabase(photo_url)
                if photo_bytes:
                    photo_file = io.BytesIO(photo_bytes)
                    await update.message.reply_photo(
                        photo=photo_file,
                        caption=message,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                else:
                    # If download fails, send text with link
                    await update.message.reply_text(
                        message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"[FULLNAME SEARCH] Error sending photo: {e}", exc_info=True)
                # Fallback: send text with link
                await update.message.reply_text(
                    message + f"\n\n📷 <a href=\"{photo_url}\">🔗 Открыть фото</a>",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
        else:
            await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        
    except Exception as e:
        logger.error(f"[FULLNAME SEARCH] ❌ Error checking by fullname: {e}", exc_info=True)
        logger.error(f"[FULLNAME SEARCH] Search value was: '{search_value}', escaped: '{escaped_search_value if 'escaped_search_value' in locals() else 'N/A'}', pattern was: '{pattern if 'pattern' in locals() else 'N/A'}'")
        error_msg = "❌ Произошла ошибка при проверке. Попробуйте позже или обратитесь к администратору."
        await update.message.reply_text(
            error_msg,
            reply_markup=get_main_menu_keyboard()
        )
    
    return ConversationHandler.END

# Check input handlers
async def check_telegram_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle telegram username input for checking"""
    if not update.message:
        logger.error(f"[CHECK_TELEGRAM_INPUT] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        return ConversationHandler.END
    return await check_by_field(update, context, "telegram_user", "Имя пользователя Telegram", CHECK_BY_TELEGRAM)

async def check_fb_link_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Facebook link input for checking"""
    if not update.message:
        logger.error(f"[CHECK_FB_LINK_INPUT] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        return ConversationHandler.END
    return await check_by_field(update, context, "facebook_link", "Facebook Ссылка", CHECK_BY_FB_LINK)

async def check_telegram_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for check by telegram ID conversation"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    logger.info(f"[CHECK_TELEGRAM_ID] Clearing state before entry for user {user_id}")
    
    # Explicitly clear all conversation state including internal ConversationHandler keys
    # This prevents issues when re-entering after /q or stale states after deploy
    clear_all_conversation_state(context, user_id)
    
    # Clean up old check messages if any
    await cleanup_check_messages(update, context)
    
    try:
        await query.edit_message_text(
            "🆔 Введите Telegram ID для проверки:",
            reply_markup=get_check_back_keyboard()
        )
    except Exception as e:
        # If message can't be edited (e.g., already deleted), send new message
        logger.warning(f"Could not edit message in check_telegram_id_callback: {e}")
        await query.message.reply_text(
            "🆔 Введите Telegram ID для проверки:",
            reply_markup=get_check_back_keyboard()
        )
    
    return CHECK_BY_TELEGRAM_ID

async def check_telegram_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram ID input for checking"""
    if not update.message:
        logger.error(f"[CHECK_TELEGRAM_ID_INPUT] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        return ConversationHandler.END
    return await check_by_field(update, context, "telegram_id", "Telegram ID", CHECK_BY_TELEGRAM_ID)

async def check_fullname_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle fullname input for checking"""
    if not update.message:
        logger.error(f"[CHECK_FULLNAME_INPUT] update.message is None. Update type: {type(update)}, has callback_query: {update.callback_query is not None}")
        return ConversationHandler.END
    return await check_by_fullname(update, context)

async def smart_check_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart check input handler - automatically detects type and searches accordingly"""
    if not update.message or not update.message.text:
        logger.error(f"[SMART_CHECK] update.message or update.message.text is None")
        return ConversationHandler.END
    
    search_value = update.message.text.strip()
    
    if not search_value:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
        sent_message = await update.message.reply_text(
            "❌ Значение для поиска не может быть пустым.",
            reply_markup=keyboard
        )
        await save_check_message(update, context, sent_message.message_id)
        return ConversationHandler.END
    
    # Detect the type of search value
    field_type, normalized_value = detect_search_type(search_value)
    
    logger.info(f"[SMART_CHECK] Detected type: '{field_type}' for value: '{search_value}' (normalized: '{normalized_value}')")
    
    # Route to appropriate search function based on detected type
    if field_type == 'telegram_id':
        # Use existing check_by_field for Telegram ID
        return await check_by_field(update, context, "telegram_id", "Telegram ID", SMART_CHECK_INPUT)
    
    elif field_type == 'telegram_user':
        # Use existing check_by_field for Telegram username
        return await check_by_field(update, context, "telegram_user", "Имя пользователя Telegram", SMART_CHECK_INPUT)
    
    elif field_type == 'fullname':
        # Use existing check_by_fullname for name search
        return await check_by_fullname(update, context)
    
    else:
        # Unknown type - search across multiple fields
        logger.info(f"[SMART_CHECK] Type unknown, searching across multiple fields")
        return await check_by_multiple_fields(update, context, search_value)

# Old add_field_callback removed - using sequential flow now

def cleanup_user_data_store(exclude_user_id: int = None):
    """Clean up old entries from user_data_store to optimize memory
    
    Args:
        exclude_user_id: User ID to exclude from cleanup (active user to protect from race conditions)
    """
    global user_data_store, user_data_store_access_time
    
    current_time = time.time()
    users_to_remove = []
    
    # Remove entries older than TTL (but exclude active user)
    for user_id, access_time in user_data_store_access_time.items():
        if user_id == exclude_user_id:
            continue  # Never remove active user's data
        if current_time - access_time > USER_DATA_STORE_TTL:
            users_to_remove.append(user_id)
    
    # Remove old entries
    for user_id in users_to_remove:
        if user_id in user_data_store:
            del user_data_store[user_id]
        if user_id in user_data_store_access_time:
            del user_data_store_access_time[user_id]
    
    # If still too large, remove oldest entries (but exclude active user)
    if len(user_data_store) > USER_DATA_STORE_MAX_SIZE:
        sorted_users = sorted(user_data_store_access_time.items(), key=lambda x: x[1])
        users_to_remove = []
        for user_id, _ in sorted_users:
            if user_id == exclude_user_id:
                continue  # Never remove active user's data
            users_to_remove.append(user_id)
            if len(user_data_store) - len(users_to_remove) <= USER_DATA_STORE_MAX_SIZE:
                break
        
        for user_id in users_to_remove:
            if user_id in user_data_store:
                del user_data_store[user_id]
            if user_id in user_data_store_access_time:
                del user_data_store_access_time[user_id]
    

async def check_duplicate_realtime(client, field_name: str, field_value: str) -> tuple[bool, str]:
    """Check if a field value already exists in the database (for real-time validation)"""
    if not field_value or field_value.strip() == '':
        return True, ""  # Empty values are considered unique
    
    # Map internal field names to database column names
    FIELD_NAME_MAPPING = {
        'telegram_name': 'telegram_user',  # Map telegram_name to telegram_user for database
    }
    
    try:
        # Map field name to database column name
        db_field_name = FIELD_NAME_MAPPING.get(field_name, field_name)
        response = client.table(TABLE_NAME).select("id, fullname").eq(db_field_name, field_value).limit(1).execute()
        if response.data and len(response.data) > 0:
            existing_lead = response.data[0]
            fullname = existing_lead.get('fullname', 'Неизвестно')
            return False, fullname
        return True, ""
    except Exception as e:
        logger.error(f"Error checking real-time duplicate for {field_name}: {e}", exc_info=True)
        return True, ""  # On error, allow to continue (will be checked again on save)

async def tag_enter_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new tag input"""
    if not update.message or not update.message.text:
        logger.error("[TAG] tag_enter_new: update.message or update.message.text is None")
        return ConversationHandler.END
    
    try:
        user_id = update.effective_user.id
        raw_text = update.message.text
        text = raw_text.strip()
        logger.info(
            f"[TAG] tag_enter_new called for user {user_id}, "
            f"raw_text='{raw_text}', normalized_text='{text}', "
            f"context_keys={list(context.user_data.keys()) if context.user_data else []}"
        )
        
        # Normalize tag
        normalized_tag = normalize_tag(text)
        logger.info(f"[TAG] tag_enter_new: normalized_tag='{normalized_tag}' for user {user_id}")
        
        if not normalized_tag:
            logger.warning(f"[TAG] tag_enter_new: empty normalized_tag for user {user_id}")
            await update.message.reply_text(
                "❌ Тег не может быть пустым. Попробуйте снова:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отменить", callback_data="tag_cancel")]
                ])
            )
            return TAG_ENTER_NEW
        
        # Get manager_name from context
        manager_name = context.user_data.get('tag_manager_name')
        if not manager_name:
            logger.error(f"[TAG] tag_enter_new: manager_name not found in context for user {user_id}")
            await update.message.reply_text(
                "❌ Ошибка: не удалось определить менеджера. Попробуйте снова с команды /tag",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        logger.info(f"[TAG] tag_enter_new: manager_name='{manager_name}' for user {user_id}")
        
        # Get Supabase client
        client = get_supabase_client()
        if not client:
            logger.error(f"[TAG] tag_enter_new: get_supabase_client returned None for user {user_id}")
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            await update.message.reply_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
        
        # Count records that will be updated
        record_count = count_records_by_manager_name(client, manager_name)
        logger.info(f"[TAG] tag_enter_new: record_count={record_count} for manager_name='{manager_name}' and user {user_id}")
        
        # Save new tag to context
        context.user_data['tag_new_tag'] = normalized_tag
        logger.info(f"[TAG] tag_enter_new: saved tag_new_tag='{normalized_tag}' in context for user {user_id}, context_keys_now={list(context.user_data.keys()) if context.user_data else []}")
        
        # Show confirmation
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="tag_confirm")],
            [InlineKeyboardButton("❌ Отменить", callback_data="tag_cancel")]
        ])
        
        await update.message.reply_text(
            f"📋 <b>Подтверждение обновления тега</b>\n\n"
            f"<b>Менеджер:</b> {escape_html(manager_name)}\n"
            f"<b>Новый тег:</b> <code>{escape_html(normalized_tag)}</code>\n"
            f"<b>Будет обновлено записей:</b> {record_count}\n\n"
            f"Подтвердите обновление:",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        
        logger.info(f"[TAG] tag_enter_new: sent confirmation message to user {user_id}, staying in TAG_ENTER_NEW")
        return TAG_ENTER_NEW
    except Exception as e:
        logger.error(f"[TAG] Error in tag_enter_new for user {update.effective_user.id}: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def add_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Universal handler for field input - sequential flow"""
    if not update.message:
        logger.error("add_field_input: update.message is None")
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    # Diagnostic: log state when entering add_field_input
    log_conversation_state(user_id, context, prefix="[ADD_FIELD_STATE]")
    
    # PRIORITY 1: Check if message is forwarded (before checking text)
    # Check for forwarded message (either forward_from or forward_from_chat or forward_sender_name)
    is_forwarded = (update.message.forward_from is not None or 
                    update.message.forward_from_chat is not None or 
                    update.message.forward_sender_name is not None)
    
    if is_forwarded:
        logger.info(f"[ADD_FIELD] Forwarded message detected from user {user_id}")
        
        # Check if user is in the process of adding a lead
        if user_id in user_data_store:
            # Check if forward_from is available (privacy settings may hide it)
            if update.message.forward_from is None:
                # Privacy settings hide the sender info
                await update.message.reply_text(
                    "⚠️ Данные отправителя недоступны из-за настроек приватности.\n\n"
                    "Продолжайте заполнение полей вручную."
                )
                # Continue with normal flow
                if not update.message.text:
                    logger.error("add_field_input: update.message.text is None (and forwarded with privacy)")
                    return ConversationHandler.END
                # Fall through to normal text processing below
            else:
                forward_from = update.message.forward_from
                
                # Check if it's a bot
                if forward_from.is_bot:
                    await update.message.reply_text(
                        "❌ Недоступно. Аккаунт является ботом.",
                        reply_markup=get_main_menu_keyboard()
                    )
                    return ConversationHandler.END
                
                # Extract data from forward_from
                extracted_data = {}
                extracted_info = []
                
                # Extract telegram_id (required if available)
                if forward_from.id:
                    telegram_id = normalize_telegram_id(str(forward_from.id))
                    if telegram_id:
                        extracted_data['telegram_id'] = telegram_id
                        extracted_info.append(f"• Telegram ID: {telegram_id}")
                        logger.info(f"[ADD_FIELD] Extracted telegram_id: {telegram_id}")
                
                # Extract telegram_name (if available)
                if forward_from.username:
                    is_valid, _, normalized = validate_telegram_name(forward_from.username)
                    if is_valid:
                        extracted_data['telegram_name'] = normalized
                        extracted_info.append(f"• Username: @{normalized}")
                        logger.info(f"[ADD_FIELD] Extracted telegram_name: {normalized}")
                
                # Extract fullname (Display Name: first_name + last_name)
                first_name = forward_from.first_name or ""
                last_name = forward_from.last_name or ""
                if first_name or last_name:
                    if last_name:
                        fullname = f"{first_name} {last_name}".strip()
                    else:
                        fullname = first_name
                    normalized_fullname = normalize_text_field(fullname)
                    if normalized_fullname:
                        extracted_data['fullname'] = normalized_fullname
                        extracted_info.append(f"• Имя: {normalized_fullname}")
                        logger.info(f"[ADD_FIELD] Extracted fullname: {normalized_fullname}")
                
                # Parse text message for Facebook link (if available)
                if update.message.text:
                    text = update.message.text.strip()
                    is_valid_fb, _, fb_normalized = validate_facebook_link(text)
                    if is_valid_fb:
                        extracted_data['facebook_link'] = fb_normalized
                        extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
                        logger.info(f"[ADD_FIELD] Extracted facebook_link from message text: {fb_normalized}")
                
                # Extract photo if available
                if update.message.photo:
                    # Get largest photo (last in the list)
                    largest_photo = update.message.photo[-1]
                    photo_file_id = largest_photo.file_id
                    extracted_data['photo_file_id'] = photo_file_id
                    extracted_info.append("• Фото: обнаружено (будет загружено при сохранении)")
                    logger.info(f"[ADD_FIELD] Extracted photo_file_id for user {user_id}: {photo_file_id}")
                
                # Save extracted data to user_data_store
                for key, value in extracted_data.items():
                    user_data_store[user_id][key] = value
                
                # Update access time
                user_data_store_access_time[user_id] = time.time()
                
                # Show user what was extracted
                if extracted_info:
                    info_text = "\n".join(extracted_info)
                    await update.message.reply_text(
                        f"✅ Данные извлечены из пересланного сообщения:\n\n{info_text}\n\n"
                        f"Продолжайте заполнение остальных полей."
                    )
                else:
                    await update.message.reply_text(
                        "⚠️ Не удалось извлечь данные из пересланного сообщения.\n\n"
                        "Продолжайте заполнение полей вручную."
                    )
                
                # Determine next field to fill - start from beginning and skip all filled fields
                # Start from first field and skip all already filled fields
                next_field, next_state, current_step, total_steps = get_next_add_field('')
                
                # Skip already filled fields (check both key existence and non-empty value)
                while next_field != 'review' and is_field_filled(user_data_store[user_id], next_field):
                    logger.info(f"[ADD_FIELD] Skipping already filled field: {next_field}")
                    next_field, next_state, current_step, total_steps = get_next_add_field(next_field)
                
                # Move to next field or review
                if next_field == 'review':
                    await show_add_review(update, context)
                    return ADD_REVIEW
                else:
                    field_label = get_field_label(next_field)
                    is_optional = next_field not in ['fullname']
                    progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
                    
                    if next_field == 'manager_name':
                        message = f"{progress_text}📝 Введите стейдж менеджера:\n\n ⚠️ Так менеджер записан в отчётности"
                    elif next_field == 'fullname':
                        message = f"{progress_text}📝 Введите {field_label}:"
                    else:
                        requirements = get_field_format_requirements(next_field)
                        message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
                    
                    context.user_data['current_field'] = next_field
                    context.user_data['current_state'] = next_state
                    
                    sent_message = await update.message.reply_text(
                        message,
                        reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                        parse_mode='HTML'
                    )
                    await save_add_message(update, context, sent_message.message_id)
                    return next_state
        else:
            # User is not in the process of adding a lead - start adding immediately
            user_id = update.effective_user.id
            logger.info(f"[ADD_FIELD] Starting add flow from forwarded message for user {user_id}")
            
            # Check if forward_from is available (privacy settings may hide it)
            if update.message.forward_from is None:
                # Privacy settings hide the sender info - start normal flow
                clear_all_conversation_state(context, user_id)
                user_data_store[user_id] = {}
                user_data_store_access_time[user_id] = time.time()
                context.user_data['current_field'] = 'fullname'
                context.user_data['current_state'] = ADD_FULLNAME
                context.user_data['add_step'] = 0
                
                field_label = get_field_label('fullname')
                _, _, current_step, total_steps = get_next_add_field('')
                message = f"<b>Шаг {current_step} из {total_steps}</b>\n\n📝 Введите {field_label}:"
                
                await update.message.reply_text(
                    "⚠️ Данные отправителя недоступны из-за настроек приватности.\n\n" + message,
                    reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
                    parse_mode='HTML'
                )
                return ADD_FULLNAME
            else:
                forward_from = update.message.forward_from
                
                # Check if it's a bot
                if forward_from.is_bot:
                    await update.message.reply_text(
                        "❌ Недоступно. Аккаунт является ботом.",
                        reply_markup=get_main_menu_keyboard()
                    )
                    return ConversationHandler.END
                
                # Initialize add flow
                clear_all_conversation_state(context, user_id)
                user_data_store[user_id] = {}
                user_data_store_access_time[user_id] = time.time()
                context.user_data['current_field'] = 'fullname'
                context.user_data['current_state'] = ADD_FULLNAME
                context.user_data['add_step'] = 0
                
                # Extract data from forward_from
                extracted_data = {}
                extracted_info = []
                
                # Extract telegram_id (required if available)
                if forward_from.id:
                    telegram_id = normalize_telegram_id(str(forward_from.id))
                    if telegram_id:
                        extracted_data['telegram_id'] = telegram_id
                        extracted_info.append(f"• Telegram ID: {telegram_id}")
                        logger.info(f"[ADD_FIELD] Extracted telegram_id: {telegram_id}")
                
                # Extract telegram_name (if available)
                if forward_from.username:
                    is_valid, _, normalized = validate_telegram_name(forward_from.username)
                    if is_valid:
                        extracted_data['telegram_name'] = normalized
                        extracted_info.append(f"• Username: @{normalized}")
                        logger.info(f"[ADD_FIELD] Extracted telegram_name: {normalized}")
                
                # Extract fullname (Display Name: first_name + last_name)
                first_name = forward_from.first_name or ""
                last_name = forward_from.last_name or ""
                if first_name or last_name:
                    if last_name:
                        fullname = f"{first_name} {last_name}".strip()
                    else:
                        fullname = first_name
                    normalized_fullname = normalize_text_field(fullname)
                    if normalized_fullname:
                        extracted_data['fullname'] = normalized_fullname
                        extracted_info.append(f"• Имя: {normalized_fullname}")
                        logger.info(f"[ADD_FIELD] Extracted fullname: {normalized_fullname}")
                
                # Parse text message for Facebook link (if available)
                if update.message.text:
                    text = update.message.text.strip()
                    is_valid_fb, _, fb_normalized = validate_facebook_link(text)
                    if is_valid_fb:
                        extracted_data['facebook_link'] = fb_normalized
                        extracted_info.append(f"• Facebook ссылка: {format_facebook_link_for_display(fb_normalized)}")
                        logger.info(f"[ADD_FIELD] Extracted facebook_link from message text: {fb_normalized}")
                
                # Save extracted data to user_data_store
                for key, value in extracted_data.items():
                    user_data_store[user_id][key] = value
                
                # Show user what was extracted
                if extracted_info:
                    info_text = "\n".join(extracted_info)
                    await update.message.reply_text(
                        f"✅ Данные извлечены из пересланного сообщения:\n\n{info_text}\n\n"
                        f"Продолжайте заполнение остальных полей."
                    )
                
                # Determine next field to fill - start from beginning and skip all filled fields
                # Start from first field and skip all already filled fields
                next_field, next_state, current_step, total_steps = get_next_add_field('')
                
                # Skip already filled fields (check both key existence and non-empty value)
                while next_field != 'review' and is_field_filled(user_data_store[user_id], next_field):
                    logger.info(f"[ADD_FIELD] Skipping already filled field: {next_field}")
                    next_field, next_state, current_step, total_steps = get_next_add_field(next_field)
                
                # Move to next field or review
                if next_field == 'review':
                    await show_add_review(update, context)
                    return ADD_REVIEW
                else:
                    field_label = get_field_label(next_field)
                    is_optional = next_field not in ['fullname']
                    progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
                    
                    if next_field == 'manager_name':
                        message = f"{progress_text}📝 Введите стейдж менеджера:\n\n ⚠️ Так менеджер записан в отчётности"
                    elif next_field == 'fullname':
                        message = f"{progress_text}📝 Введите {field_label}:"
                    else:
                        requirements = get_field_format_requirements(next_field)
                        message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
                    
                    context.user_data['current_field'] = next_field
                    context.user_data['current_state'] = next_state
                    
                    sent_message = await update.message.reply_text(
                        message,
                        reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                        parse_mode='HTML'
                    )
                    await save_add_message(update, context, sent_message.message_id)
                    return next_state
    
    # PRIORITY 2: Handle regular text message (existing logic)
    if not update.message.text:
        logger.error("add_field_input: update.message.text is None (and not forwarded)")
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    # Determine field_name from current_state FIRST, then fallback to context.user_data
    # This ensures we use the correct field based on ConversationHandler state
    current_state = context.user_data.get('current_state', ADD_FULLNAME)
    
    # Map ConversationHandler states to field names
    state_to_field = {
        ADD_FULLNAME: 'fullname',
        ADD_FB_LINK: 'facebook_link',
        ADD_TELEGRAM_NAME: 'telegram_name',
        ADD_TELEGRAM_ID: 'telegram_id',
    }
    
    # Get field_name from state first (more reliable)
    field_name = state_to_field.get(current_state)
    
    # Fallback to context.user_data if state mapping doesn't work
    if not field_name:
        field_name = context.user_data.get('current_field')
    
    # Log for debugging
    logger.info(f"[ADD_FIELD] Processing input - current_state: {current_state}, field_name from state: {state_to_field.get(current_state)}, field_name from user_data: {context.user_data.get('current_field')}, field_name determined: {field_name}, text: '{text[:50]}...'")
    
    # Update access time BEFORE cleanup to protect from race conditions
    user_data_store_access_time[user_id] = time.time()
    
    # Ensure user_data_store entry exists before cleanup
    if user_id not in user_data_store:
        user_data_store[user_id] = {}
    
    # Log before cleanup
    if user_id in user_data_store:
        logger.info(f"[ADD_FIELD] Before cleanup - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        if 'telegram_name' in user_data_store[user_id]:
            logger.info(f"[ADD_FIELD] Before cleanup - telegram_name: '{user_data_store[user_id].get('telegram_name')}'")
    
    # Cleanup with exclusion of current user to prevent race conditions
    cleanup_user_data_store(exclude_user_id=user_id)
    
    # Log after cleanup
    if user_id in user_data_store:
        logger.info(f"[ADD_FIELD] After cleanup - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        if 'telegram_name' in user_data_store[user_id]:
            logger.info(f"[ADD_FIELD] After cleanup - telegram_name: '{user_data_store[user_id].get('telegram_name')}'")
    else:
        logger.error(f"[ADD_FIELD] After cleanup - user_data_store[{user_id}] was DELETED!")
    
    if not field_name:
        # Fallback: start from beginning
        await update.message.reply_text(
            "❌ Неизвестная команда. Выберите действие:",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Validate and normalize based on field type
    validation_passed = False
    normalized_value = text
    
    if field_name == 'facebook_link':
        is_valid, error_msg, extracted = validate_facebook_link(text)
        if is_valid:
            validation_passed = True
            normalized_value = extracted
        else:
            field_label = get_field_label('facebook_link')
            requirements = get_field_format_requirements('facebook_link')
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}\n\n📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
    
    elif field_name == 'telegram_name':
        is_valid, error_msg, normalized = validate_telegram_name(text)
        if is_valid:
            validation_passed = True
            normalized_value = normalized
        else:
            field_label = get_field_label('telegram_name')
            requirements = get_field_format_requirements('telegram_name')
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}\n\n📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
    
    elif field_name == 'telegram_id':
        is_valid, error_msg, normalized = validate_telegram_id(text)
        if is_valid:
            validation_passed = True
            normalized_value = normalized
        else:
            field_label = get_field_label('telegram_id')
            requirements = get_field_format_requirements('telegram_id')
            sent_message = await update.message.reply_text(
                f"❌ {error_msg}\n\n📝 Введите {field_label}:\n\n{requirements}",
                reply_markup=get_navigation_keyboard(is_optional=True, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
    
    else:
        # For other fields (fullname, manager_name), normalize text and check not empty
        if text:
            # Check length before normalization
            if len(text) > 500:
                field_label = get_field_label(field_name)
                await update.message.reply_text(
                    f"❌ {field_label} слишком длинное (максимум 500 символов).\n\n"
                    f"📝 Введите {field_label}:",
                    reply_markup=get_navigation_keyboard(is_optional=(field_name not in ['fullname']), show_back=True),
                    parse_mode='HTML'
                )
                await save_add_message(update, context, update.message.message_id)
                return current_state
            
            # Normalize text fields (fullname, manager_name)
            normalized_value = normalize_text_field(text)
            if normalized_value:
                validation_passed = True
            else:
                # Text was only whitespace
                validation_passed = False
        else:
            field_label = get_field_label(field_name)
            is_optional = field_name not in ['fullname']
            
            # Для обязательных полей (fullname) не показываем требования к формату
            if field_name == 'fullname':
                message = f"❌ Поле не может быть пустым.\n\n📝 Введите {field_label}:"
                use_html = False
            else:
                requirements = get_field_format_requirements(field_name)
                message = f"❌ Поле не может быть пустым.\n\n📝 Введите {field_label}:\n\n{requirements}"
                use_html = True
            
            sent_message = await update.message.reply_text(
                message,
                reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=(field_name != 'fullname')),
                parse_mode='HTML' if use_html else None
            )
            await save_add_message(update, context, sent_message.message_id)
            return current_state
    
    # Real-time duplicate check for critical fields (removed phone check)
    # Note: Real-time duplicate checking can be added for other fields if needed
    if False:  # Placeholder for future real-time checks
        client = get_supabase_client()
        if client:
            is_unique, existing_fullname = await check_duplicate_realtime(client, field_name, normalized_value)
            if not is_unique:
                field_label = UNIQUENESS_FIELD_LABELS.get(field_name, field_name)
                # If duplicate found, stop adding and return to main menu
                await update.message.reply_text(
                    f"⚠️ Внимание: {field_label} уже существует в базе.\n"
                    f"Существующий лид: {existing_fullname}\n\n"
                    f"Добавление нового лида отменено.",
                    reply_markup=get_main_menu_keyboard()
                )
                # Clean up user data for this user
                if user_id in user_data_store:
                    del user_data_store[user_id]
                if user_id in user_data_store_access_time:
                    del user_data_store_access_time[user_id]
                return ConversationHandler.END
    
    # Save value only if validation passed
    if validation_passed and normalized_value:
        # Ensure user_data_store entry exists (protection against race conditions)
        if user_id not in user_data_store:
            user_data_store[user_id] = {}
        user_data_store[user_id][field_name] = normalized_value
        logger.info(f"[ADD_FIELD] Saved {field_name} = '{normalized_value}' for user {user_id}")
        logger.info(f"[ADD_FIELD] user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        
        # If Facebook link is valid, automatically skip Telegram fields and go to review
        if field_name == 'facebook_link' and validation_passed:
            logger.info(f"[ADD_FIELD] Facebook link is valid, auto-skipping Telegram fields (telegram_name, telegram_id)")
            # Skip telegram_name and telegram_id, go directly to review
            next_field, next_state, current_step, total_steps = get_next_add_field('telegram_id')
        else:
            # Normal flow - move to next field
            next_field, next_state, current_step, total_steps = get_next_add_field(field_name)
    else:
        logger.warning(f"[ADD_FIELD] Not saving {field_name}: validation_passed={validation_passed}, normalized_value='{normalized_value}'")
        # If validation failed, stay on current field
        next_field, next_state, current_step, total_steps = get_next_add_field(field_name)
    
    # Skip already filled fields (e.g., if they were filled from forwarded message)
    while next_field != 'review' and is_field_filled(user_data_store.get(user_id, {}), next_field):
        logger.info(f"[ADD_FIELD] Skipping already filled field: {next_field}")
        next_field, next_state, current_step, total_steps = get_next_add_field(next_field)
    
    # Log current state before moving to next field
    if user_id in user_data_store:
        logger.info(f"[ADD_FIELD] Before moving to {next_field} - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys())}")
        if 'telegram_name' in user_data_store[user_id]:
            logger.info(f"[ADD_FIELD] Before moving to {next_field} - telegram_name: '{user_data_store[user_id].get('telegram_name')}'")
    
    if next_field == 'review':
        # Show review and save option
        logger.info(f"[ADD_FIELD] Moving to review - user_data_store[{user_id}] keys: {list(user_data_store[user_id].keys()) if user_id in user_data_store else 'N/A'}")
        
        # Check if we're returning to review after editing fullname
        if context.user_data.get('return_to_review'):
            # Clear the flag
            del context.user_data['return_to_review']
            # Show review screen
            await show_add_review(update, context)
            return ADD_REVIEW
        else:
            # Normal flow to review
            await show_add_review(update, context)
            return ADD_REVIEW
    else:
        # Show next field with progress indicator
        field_label = get_field_label(next_field)
        is_optional = next_field not in ['fullname', 'manager_name']
        
        # Add progress indicator
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        
        # Для обязательных полей (fullname, manager_name) не показываем требования к формату
        if next_field == 'manager_name':
            message = f"{progress_text}📝 Введите стейдж менеджера:\n\n ⚠️ Так менеджер записан в отчётности"
        elif next_field == 'fullname':
            message = f"{progress_text}📝 Введите {field_label}:"
        else:
            requirements = get_field_format_requirements(next_field)
            message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
        
        context.user_data['current_field'] = next_field
        context.user_data['current_state'] = next_state
        
        # Работаем как с message, так и с callback_query
        # Always use HTML when progress indicator is present
        if update.callback_query:
            await update.callback_query.edit_message_text(
                message,
                reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                parse_mode='HTML'
            )
            # Save message ID for cleanup (edit_message_text doesn't return new message, use existing)
            if update.callback_query.message:
                await save_add_message(update, context, update.callback_query.message.message_id)
        elif update.message:
            sent_message = await retry_telegram_api(
                update.message.reply_text,
                text=message,
                reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
                parse_mode='HTML'
            )
            await save_add_message(update, context, sent_message.message_id)
        return next_state

async def show_add_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show review of entered data before saving"""
    user_id = update.effective_user.id
    user_data = user_data_store.get(user_id, {})
    
    logger.info(f"[SHOW_REVIEW] user_data keys: {list(user_data.keys())}")
    
    message_parts = ["✅ <b>Проверьте введенные данные:</b>\n"]
    
    field_labels = {
        'fullname': 'Имя Фамилия',
        'facebook_link': 'Facebook Ссылка',
        'telegram_name': 'Имя пользователя Telegram',
        'telegram_id': 'Telegram ID'
    }
    
    for field_name, field_label in field_labels.items():
        value = user_data.get(field_name)
        if value:
            # Format Facebook link to full URL for display
            if field_name == 'facebook_link':
                formatted_value = format_facebook_link_for_display(str(value))
                escaped_value = escape_html(formatted_value)
            else:
                escaped_value = escape_html(str(value))
            message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
    
    message = "\n".join(message_parts)
    
    keyboard = [
        [InlineKeyboardButton("💾 Сохранить", callback_data="add_save")],
    ]
    
    # Add edit fullname button if fullname exists
    if user_data.get('fullname'):
        keyboard.append([InlineKeyboardButton("✏️ Редактировать", callback_data="edit_fullname_from_review")])
    
    keyboard.extend([
        [InlineKeyboardButton("◀️ Назад", callback_data="add_back")],
        [InlineKeyboardButton("❌ Отменить", callback_data="add_cancel")]
    ])
    
    # Работаем как с message, так и с callback_query
    if update.callback_query:
        await update.callback_query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        # Save message ID for cleanup
        if update.callback_query.message:
            await save_add_message(update, context, update.callback_query.message.message_id)
    elif update.message:
        sent_message = await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        await save_add_message(update, context, sent_message.message_id)

# Field labels for uniqueness check messages (Russian)
UNIQUENESS_FIELD_LABELS = {
    'facebook_link': 'Facebook Ссылка',
    'telegram_name': 'Имя пользователя Telegram',
    'telegram_id': 'Telegram ID'
}

def check_fields_uniqueness_batch(client, fields_to_check: dict) -> tuple[bool, str]:
    """
    Check uniqueness of multiple fields in a single query using OR conditions.
    Returns (is_unique, conflicting_field) where conflicting_field is empty if all unique.
    """
    if not fields_to_check:
        return True, ""
    
    # Map internal field names to database column names
    FIELD_NAME_MAPPING = {
        'telegram_name': 'telegram_user',  # Map telegram_name to telegram_user for database
    }
    
    # Check cache first
    cache_key = tuple(sorted(fields_to_check.items()))
    if cache_key in uniqueness_cache:
        cached_result, cached_time = uniqueness_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            return cached_result
    
    # Check each field but use caching and limit queries
    # Supabase Python client doesn't support OR conditions directly,
    # so we check each field but optimize with caching
    try:
        for field_name, field_value in fields_to_check.items():
            if field_value and field_value.strip():
                # Map field name to database column name
                db_field_name = FIELD_NAME_MAPPING.get(field_name, field_name)
                # Check individual field with retry
                response = client.table(TABLE_NAME).select("id").eq(db_field_name, field_value).limit(1).execute()
                if response.data and len(response.data) > 0:
                    # Found duplicate - return original field name for error message
                    result = (False, field_name)
                    uniqueness_cache[cache_key] = (result, time.time())
                    return result
        
        # All fields are unique
        result = (True, "")
        uniqueness_cache[cache_key] = (result, time.time())
        return result
        
    except Exception as e:
        logger.error(f"Error checking batch uniqueness: {e}", exc_info=True)
        # On error, assume not unique to prevent duplicate inserts
        return False, "unknown"

def get_unique_manager_names(client) -> list[str]:
    """Get list of unique manager_name values from database"""
    try:
        # Get all records and extract unique manager_name values
        # Note: Supabase Python client doesn't support DISTINCT directly in select,
        # so we fetch all and extract unique values in Python
        response = client.table(TABLE_NAME).select("manager_name").execute()
        
        if not response.data:
            return []
        
        # Extract unique manager_name values (excluding None and empty strings)
        unique_names = set()
        for record in response.data:
            manager_name = record.get('manager_name')
            if manager_name and manager_name.strip():
                unique_names.add(manager_name.strip())
        
        # Sort alphabetically and return as list
        return sorted(list(unique_names))
    except Exception as e:
        logger.error(f"Error getting unique manager names: {e}", exc_info=True)
        return []

@retry_supabase_query(max_retries=3, delay=1, backoff=2)
def update_manager_tag_by_name(client, manager_name: str, new_tag: str) -> int:
    """Update manager_tag for all records with given manager_name"""
    try:
        # Normalize the tag
        normalized_tag = normalize_tag(new_tag)
        
        # Update all records with the given manager_name
        response = client.table(TABLE_NAME).update({"manager_tag": normalized_tag}).eq("manager_name", manager_name).execute()
        
        # Count updated records
        updated_count = len(response.data) if response.data else 0
        
        logger.info(f"[UPDATE_TAG] Updated manager_tag for manager_name '{manager_name}' to '{normalized_tag}'. Updated {updated_count} records.")
        
        return updated_count
    except Exception as e:
        logger.error(f"Error updating manager_tag for {manager_name}: {e}", exc_info=True)
        raise

def count_records_by_manager_name(client, manager_name: str) -> int:
    """Count records with given manager_name"""
    try:
        # Get all records with this manager_name and count them
        # Using limit to avoid loading all data, but we need to count
        response = client.table(TABLE_NAME).select("id").eq("manager_name", manager_name).execute()
        return len(response.data) if response.data else 0
    except Exception as e:
        logger.error(f"Error counting records for manager_name {manager_name}: {e}", exc_info=True)
        return 0

def check_field_uniqueness(client, field_name: str, field_value: str) -> bool:
    """Check if a field value already exists in the database (with retry and cache)"""
    if not field_value or field_value.strip() == '':
        return True  # Empty values are considered unique
    
    # Check cache
    cache_key = (field_name, field_value)
    if cache_key in uniqueness_cache:
        cached_result, cached_time = uniqueness_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            return cached_result
    
    # Use retry wrapper for the actual query
    @retry_supabase_query(max_retries=3, delay=1, backoff=2)
    def _execute_query():
        return client.table(TABLE_NAME).select("id").eq(field_name, field_value).limit(1).execute()
    
    try:
        response = _execute_query()
        # If any records found, field is not unique
        is_unique = not (response.data and len(response.data) > 0)
        
        # Cache result
        uniqueness_cache[cache_key] = (is_unique, time.time())
        return is_unique
    except Exception as e:
        logger.error(f"Error checking uniqueness for {field_name}: {e}", exc_info=True)
        # On error, assume not unique to prevent duplicate inserts
        return False

async def add_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip current optional field"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    field_name = context.user_data.get('current_field')
    current_state = context.user_data.get('current_state', ADD_FULLNAME)
    
    # Move to next field
    next_field, next_state, current_step, total_steps = get_next_add_field(field_name)
    
    # Skip already filled fields (e.g., if they were filled from forwarded message)
    while next_field != 'review' and is_field_filled(user_data_store.get(user_id, {}), next_field):
        logger.info(f"[ADD_SKIP] Skipping already filled field: {next_field}")
        next_field, next_state, current_step, total_steps = get_next_add_field(next_field)
    
    if next_field == 'review':
        # Show review and save option
        await show_add_review(update, context)
        return ADD_REVIEW
    else:
        # Show next field with progress indicator
        field_label = get_field_label(next_field)
        is_optional = next_field not in ['fullname', 'manager_name']
        
        # Add progress indicator
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        
        # Для обязательных полей (fullname, manager_name) не показываем требования к формату
        if next_field == 'manager_name':
            message = f"{progress_text}📝 Введите стейдж менеджера:\n\n ⚠️ Так менеджер записан в отчётности"
        elif next_field == 'fullname':
            message = f"{progress_text}📝 Введите {field_label}:"
        else:
            requirements = get_field_format_requirements(next_field)
            message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
        
        context.user_data['current_field'] = next_field
        context.user_data['current_state'] = next_state
        
        await query.edit_message_text(
            message,
            reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=True),
            parse_mode='HTML'
        )
        # Save message ID for cleanup
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        return next_state

async def edit_fullname_from_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle edit fullname button from review screen"""
    query = update.callback_query
    await retry_telegram_api(query.answer)
    
    user_id = query.from_user.id
    
    # Ensure user_data_store exists
    if user_id not in user_data_store:
        await query.edit_message_text(
            "❌ Ошибка: данные не найдены. Начните добавление заново.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Save current review state to return after editing
    context.user_data['return_to_review'] = True
    
    # Set state to ADD_FULLNAME for editing
    context.user_data['current_field'] = 'fullname'
    context.user_data['current_state'] = ADD_FULLNAME
    
    # Get current fullname value (if exists) for reference
    current_fullname = user_data_store[user_id].get('fullname', '')
    
    field_label = get_field_label('fullname')
    _, _, current_step, total_steps = get_next_add_field('')
    
    if current_fullname:
        message = f"<b>Шаг {current_step} из {total_steps}</b>\n\n📝 Введите {field_label}:\n\n💡 Текущее значение: <code>{escape_html(current_fullname)}</code>"
    else:
        message = f"<b>Шаг {current_step} из {total_steps}</b>\n\n📝 Введите {field_label}:"
    
    await retry_telegram_api(
        query.edit_message_text,
        text=message,
        reply_markup=get_navigation_keyboard(is_optional=False, show_back=True),
        parse_mode='HTML'
    )
    
    # Save message ID for cleanup
    if query.message:
        await save_add_message(update, context, query.message.message_id)
    
    return ADD_FULLNAME

async def add_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to previous field"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    field_name = context.user_data.get('current_field')
    
    # Get previous field
    field_sequence = [
        ('fullname', ADD_FULLNAME),
        ('facebook_link', ADD_FB_LINK),
        ('telegram_name', ADD_TELEGRAM_NAME),
        ('telegram_id', ADD_TELEGRAM_ID),
    ]
    
    prev_field = None
    prev_state = ADD_FULLNAME
    
    for i, (field, state) in enumerate(field_sequence):
        if field == field_name:
            if i > 0:
                prev_field, prev_state = field_sequence[i - 1]
            break
    
    if prev_field:
        field_label = get_field_label(prev_field)
        is_optional = prev_field not in ['fullname']
        
        # Calculate step number for previous field
        _, _, current_step, total_steps = get_next_add_field(prev_field)
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        
        # Для обязательных полей (fullname) не показываем требования к формату
        if prev_field == 'fullname':
            message = f"{progress_text}📝 Введите {field_label}:"
        else:
            requirements = get_field_format_requirements(prev_field)
            message = f"{progress_text}📝 Введите {field_label}:\n\n{requirements}"
        
        context.user_data['current_field'] = prev_field
        context.user_data['current_state'] = prev_state
        
        await query.edit_message_text(
            message,
            reply_markup=get_navigation_keyboard(is_optional=is_optional, show_back=(prev_field != 'fullname')),
            parse_mode='HTML'
        )
        # Save message ID for cleanup
        if query.message:
            await save_add_message(update, context, query.message.message_id)
        return prev_state
    else:
        # Already at first field, go to main menu
        await query.edit_message_text(
            "👋 Главное меню\n\nВыберите действие:",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END

async def tag_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation of tag update"""
    query = update.callback_query
    await query.answer()
    
    try:
        # Get manager_name and new_tag from context
        manager_name = context.user_data.get('tag_manager_name')
        new_tag = context.user_data.get('tag_new_tag')
        
        if not manager_name or not new_tag:
            logger.error(f"[TAG] Missing data in context: manager_name={manager_name}, new_tag={new_tag}")
            await query.edit_message_text(
                "❌ Ошибка: не удалось получить данные. Попробуйте снова с команды /tag",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        # Get Supabase client
        client = get_supabase_client()
        if not client:
            error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
            await query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
            return ConversationHandler.END
        
        # Update manager_tag
        updated_count = update_manager_tag_by_name(client, manager_name, new_tag)
        
        # Clear context
        clear_all_conversation_state(context, update.effective_user.id)
        
        # Show success message
        await query.edit_message_text(
            f"✅ <b>Тег успешно обновлен!</b>\n\n"
            f"<b>Менеджер:</b> {escape_html(manager_name)}\n"
            f"<b>Новый тег:</b> <code>{escape_html(new_tag)}</code>\n"
            f"<b>Обновлено записей:</b> {updated_count}",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in tag_confirm_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка при обновлении тега. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def tag_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancellation of tag update"""
    query = update.callback_query
    await query.answer()
    
    try:
        # Clear context
        clear_all_conversation_state(context, update.effective_user.id)
        
        # Show cancellation message
        await query.edit_message_text(
            "❌ Обновление тега отменено.",
            reply_markup=get_main_menu_keyboard()
        )
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in tag_cancel_callback: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

async def add_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate and save the lead"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = user_data_store.get(user_id, {})
    
    # Validation
    if not user_data.get('fullname'):
        field_label = get_field_label('fullname')
        _, _, current_step, total_steps = get_next_add_field('')
        progress_text = f"<b>Шаг {current_step} из {total_steps}</b>\n\n"
        await query.edit_message_text(
            f"{progress_text}❌ <b>Ошибка:</b> {field_label} обязателен для заполнения!\n\n"
            f"📝 Введите {field_label}:",
            reply_markup=get_navigation_keyboard(is_optional=False, show_back=False),
            parse_mode='HTML'
        )
        context.user_data['current_field'] = 'fullname'
        context.user_data['current_state'] = ADD_FULLNAME
        return ADD_FULLNAME
    
    # Check if at least one identifier is present
    required_fields = ['facebook_link', 'telegram_name', 'telegram_id']
    has_identifier = any(user_data.get(field) for field in required_fields)
    
    if not has_identifier:
        await query.edit_message_text(
            "❌ <b>Ошибка:</b> Необходимо указать минимум одно из полей:\n\n"
            "• Facebook Ссылка\n"
            "• Имя пользователя Telegram\n"
            "• Telegram ID\n\n"
            "ℹ️ Начнем с первого опционального поля:",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        return ConversationHandler.END
    
    # Get Supabase client for uniqueness check
    client = get_supabase_client()
    if not client:
        error_msg = get_user_friendly_error(Exception("Database connection failed"), "подключении к базе данных")
        await query.edit_message_text(
            error_msg,
            reply_markup=get_main_menu_keyboard(),
            parse_mode='HTML'
        )
        if user_id in user_data_store:
            del user_data_store[user_id]
        return ConversationHandler.END
    
    # Check uniqueness of fields - optimized batch check
    # Only check optional identifier fields (facebook_link, telegram_name, telegram_id)
    # Do NOT check fullname and manager_name - they can be duplicated
    logger.info(f"[ADD_SAVE] Before uniqueness check - user_data keys: {list(user_data.keys())}")
    if 'telegram_name' in user_data:
        logger.info(f"[ADD_SAVE] Before uniqueness check - telegram_name: '{user_data.get('telegram_name')}'")
    
    fields_to_check = {}
    # Only check optional identifier fields for uniqueness
    for field_name in ['facebook_link', 'telegram_name', 'telegram_id']:
        field_value = user_data.get(field_name)
        if field_value and field_value.strip():  # Only check non-empty fields
            check_value = field_value
            fields_to_check[field_name] = check_value
            logger.info(f"[ADD_SAVE] Adding {field_name} to uniqueness check: '{check_value}'")
        else:
            logger.info(f"[ADD_SAVE] Skipping {field_name} in uniqueness check (empty or None)")
    
    logger.info(f"[ADD_SAVE] Fields to check for uniqueness: {list(fields_to_check.keys())}")
    
    # Batch check uniqueness
    if fields_to_check:
        is_unique, conflicting_field = check_fields_uniqueness_batch(client, fields_to_check)
        logger.info(f"[ADD_SAVE] After uniqueness check - is_unique: {is_unique}, conflicting_field: {conflicting_field}")
        logger.info(f"[ADD_SAVE] After uniqueness check - user_data keys: {list(user_data.keys())}")
        if 'telegram_name' in user_data:
            logger.info(f"[ADD_SAVE] After uniqueness check - telegram_name: '{user_data.get('telegram_name')}'")
        if not is_unique:
            field_label = UNIQUENESS_FIELD_LABELS.get(conflicting_field, conflicting_field)
            
            # Get review screen message ID to exclude from cleanup
            review_message_id = None
            if query.message:
                review_message_id = query.message.message_id
            
            # Clean up all add flow messages BEFORE showing error message
            # Exclude review screen message so we can edit it
            await cleanup_add_messages(update, context, exclude_message_id=review_message_id)
            
            try:
                await query.edit_message_text(
                    f"❌ <b>Ошибка:</b> {field_label} уже существует в базе.\n\n"
                    "ℹ️ Попробуйте добавить лид заново с другими данными.",
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            except Exception as e:
                # If edit fails (message was deleted), send new message
                if "not found" in str(e) or "BadRequest" in str(type(e).__name__):
                    await query.message.reply_text(
                        f"❌ <b>Ошибка:</b> {field_label} уже существует в базе.\n\n"
                        "ℹ️ Попробуйте добавить лид заново с другими данными.",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    raise
            if user_id in user_data_store:
                del user_data_store[user_id]
            if user_id in user_data_store_access_time:
                del user_data_store_access_time[user_id]
            return ConversationHandler.END
    
    # All fields are unique, proceed with saving
    try:
        # Prepare data for saving - map telegram_name to telegram_user for database compatibility
        save_data = user_data.copy()
        
        # Remove photo_file_id - it's a temporary value used only for uploading to Supabase Storage
        # The photo_url will be set later after successful upload
        if 'photo_file_id' in save_data:
            save_data.pop('photo_file_id')
        
        # Map telegram_name to telegram_user for database (backward compatibility)
        if 'telegram_name' in save_data:
            telegram_name_value = save_data.pop('telegram_name')
            # Only set telegram_user if telegram_name has a value (not empty string or None)
            if telegram_name_value:
                save_data['telegram_user'] = telegram_name_value
        
        # Automatically extract manager_name and manager_tag from user who is saving
        from_user = query.from_user
        first_name = from_user.first_name or ""
        last_name = from_user.last_name or ""
        
        # Build manager_name from first_name + last_name
        if last_name:
            manager_name = f"{first_name} {last_name}".strip()
        else:
            manager_name = first_name.strip()
        
        # Normalize manager_name (trim spaces, collapse multiple spaces)
        manager_name = normalize_text_field(manager_name)
        save_data['manager_name'] = manager_name
        
        # Extract manager_tag from username (without @)
        manager_tag = ""
        if from_user.username:
            # Remove @ if present and normalize
            username = from_user.username.replace('@', '').strip()
            if username:
                manager_tag = username
        
        save_data['manager_tag'] = manager_tag
        
        logger.info(f"[ADD_SAVE] Inserting data to database: {save_data}")
        response = client.table(TABLE_NAME).insert(save_data).execute()
        
        # Log successful save with all fields
        if response.data and len(response.data) > 0:
            saved_lead = response.data[0]
            lead_id = saved_lead.get('id')
            logger.info(f"[NEW_LEAD_SAVED] ✅ New lead successfully saved to database")
            logger.info(f"[NEW_LEAD_SAVED] Lead ID: {lead_id}")
            
            # Try to upload photo if we extracted it earlier
            user_photo_file_id = user_data.get('photo_file_id')
            if lead_id:
                if user_photo_file_id:
                    logger.info(f"[PHOTO] Starting photo upload for lead {lead_id} from file_id={user_photo_file_id}")
                    photo_url = await upload_lead_photo_to_supabase(context.bot, user_photo_file_id, lead_id)
                    if photo_url:
                        try:
                            # Update the lead with photo_url
                            client.table(TABLE_NAME).update({"photo_url": photo_url}).eq("id", lead_id).execute()
                            logger.info(f"[PHOTO] Successfully saved photo_url for lead {lead_id}: {photo_url}")
                        except Exception as e:
                            logger.error(f"[PHOTO] Failed to save photo_url to database for lead {lead_id}: {e}", exc_info=True)
                    else:
                        logger.warning(f"[PHOTO] Photo upload failed (returned None) for lead {lead_id}, file_id={user_photo_file_id}")
                else:
                    logger.info(f"[PHOTO] No photo_file_id in user_data for lead {lead_id}, skipping photo upload")
            
            # Log all fields with their values
            field_labels = {
                'fullname': 'Клиент (fullname)',
                'manager_name': 'Стейдж менеджера (manager_name)',
                'manager_tag': 'Тег менеджера (manager_tag)',
                'facebook_link': 'Facebook Ссылка (facebook_link)',
                'telegram_user': 'Имя пользователя Telegram (telegram_user)',
                'telegram_id': 'Telegram ID (telegram_id)',
                'photo_url': 'Фото (photo_url)',
                'created_at': 'Дата создания (created_at)'
            }
            
            logged_fields = []
            for field_name, field_label in field_labels.items():
                value = saved_lead.get(field_name)
                if value is not None and value != '':
                    logged_fields.append(f"  {field_label}: '{value}'")
                else:
                    logged_fields.append(f"  {field_label}: (не указано)")
            
            logger.info(f"[NEW_LEAD_SAVED] Fields:\n" + "\n".join(logged_fields))
        
        if response.data:
            # Show success message with entered data
            message_parts = ["✅ <b>Клиент успешно добавлен!</b>\n"]
            field_labels = {
                'fullname': 'Имя Фамилия',
                'facebook_link': 'Facebook Ссылка',
                'telegram_name': 'Имя пользователя Telegram',
                'telegram_id': 'Telegram ID'
            }
            
            for field_name, field_label in field_labels.items():
                # Check telegram_user for display (it was mapped from telegram_name)
                display_field = 'telegram_user' if field_name == 'telegram_name' else field_name
                value = save_data.get(display_field) or user_data.get(field_name)
                if value:
                    # Format Facebook link to full URL for display
                    if field_name == 'facebook_link':
                        formatted_value = format_facebook_link_for_display(str(value))
                        escaped_value = escape_html(formatted_value)
                    else:
                        escaped_value = escape_html(str(value))
                    message_parts.append(f"{field_label}: <code>{escaped_value}</code>")
            
            # Add date
            from datetime import datetime
            message_parts.append(f"Дата добавления: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            
            message = "\n".join(message_parts)
            
            # Get review screen message ID to exclude from cleanup
            review_message_id = None
            if query.message:
                review_message_id = query.message.message_id
            
            # Clean up all add flow messages BEFORE showing final success message
            # Exclude review screen message so we can edit it
            await cleanup_add_messages(update, context, exclude_message_id=review_message_id)
            
            try:
                await query.edit_message_text(
                    message,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            except Exception as e:
                # If edit fails (message was deleted), send new message
                if "not found" in str(e) or "BadRequest" in str(type(e).__name__):
                    await query.message.reply_text(
                        message,
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    raise
        else:
            # Get review screen message ID to exclude from cleanup
            review_message_id = None
            if query.message:
                review_message_id = query.message.message_id
            
            # Clean up all add flow messages BEFORE showing error message
            # Exclude review screen message so we can edit it
            await cleanup_add_messages(update, context, exclude_message_id=review_message_id)
            
            try:
                await query.edit_message_text(
                    "❌ <b>Ошибка:</b> Данные не были сохранены.\n\n"
                    "ℹ️ Попробуйте снова или обратитесь к администратору.",
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            except Exception as e:
                # If edit fails (message was deleted), send new message
                if "not found" in str(e) or "BadRequest" in str(type(e).__name__):
                    await query.message.reply_text(
                        "❌ <b>Ошибка:</b> Данные не были сохранены.\n\n"
                        "ℹ️ Попробуйте снова или обратитесь к администратору.",
                        reply_markup=get_main_menu_keyboard(),
                        parse_mode='HTML'
                    )
                else:
                    raise
    
    except Exception as e:
        logger.error(f"Error adding client: {e}", exc_info=True)
        error_msg = get_user_friendly_error(e, "сохранении данных")
        
        # Get review screen message ID to exclude from cleanup
        review_message_id = None
        if query.message:
            review_message_id = query.message.message_id
        
        # Clean up all add flow messages BEFORE showing error message
        # Exclude review screen message so we can edit it
        await cleanup_add_messages(update, context, exclude_message_id=review_message_id)
        
        try:
            await query.edit_message_text(
                error_msg,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
        except Exception as edit_error:
            # If edit fails (message was deleted), send new message
            if "not found" in str(edit_error) or "BadRequest" in str(type(edit_error).__name__):
                await query.message.reply_text(
                    error_msg,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode='HTML'
                )
            else:
                raise
    
    # Clean up
    if user_id in user_data_store:
        del user_data_store[user_id]
    if user_id in user_data_store_access_time:
        del user_data_store_access_time[user_id]
    
    return ConversationHandler.END

async def add_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel adding new lead"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id in user_data_store:
        del user_data_store[user_id]
    if user_id in user_data_store_access_time:
        del user_data_store_access_time[user_id]
    
    await query.edit_message_text(
        "❌ Добавление отменено.",
        reply_markup=get_main_menu_keyboard()
    )
    return ConversationHandler.END

# Edit lead functionality
async def edit_lead_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, lead_id: int):
    """Start editing a lead"""
    query = update.callback_query
    await query.answer()
    
    # Get lead from database
    client = get_supabase_client()
    if not client:
        await query.edit_message_text(
            "❌ Ошибка: Не удалось подключиться к базе данных.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    try:
        response = client.table(TABLE_NAME).select("*").eq("id", lead_id).execute()
        if not response.data or len(response.data) == 0:
            await query.edit_message_text(
                "❌ Ошибка: Лид не найден.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        lead = response.data[0]
        user_id = query.from_user.id
        
        # Store lead data for editing
        # Map telegram_user to telegram_name for consistency
        lead_data = lead.copy()
        if 'telegram_user' in lead_data and lead_data.get('telegram_user'):
            # Map telegram_user to telegram_name for consistency in UI
            if 'telegram_name' not in lead_data or not lead_data.get('telegram_name'):
                lead_data['telegram_name'] = lead_data.get('telegram_user')
        
        # Ensure all fields are present (even if None/empty) for proper display
        # This ensures indicators work correctly
        for field in ['fullname', 'manager_name', 'facebook_link', 'telegram_name', 'telegram_id']:
            if field not in lead_data:
                lead_data[field] = None
        
        user_data_store[user_id] = lead_data
        user_data_store_access_time[user_id] = time.time()
        context.user_data['editing_lead_id'] = lead_id
        
        # Reset PIN attempt counter when starting new edit
        context.user_data['pin_attempts'] = 0
        
        # Request PIN code before allowing editing
        message = f"🔒 Для редактирования лида (ID: {lead_id}) требуется PIN-код.\n\nВведите PIN-код:"
        # Use reply_text instead of edit_message_text to ensure ConversationHandler works correctly
        await query.message.reply_text(message)
        return EDIT_PIN
        
    except Exception as e:
        logger.error(f"Error loading lead for editing: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке лида. Попробуйте позже.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END

async def edit_pin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PIN code input for editing"""
    user_id = update.effective_user.id
    
    # Check if message exists and has text
    if not update.message or not update.message.text:
        if update.message:
            await update.message.reply_text(
                "❌ Ошибка: Неверный формат сообщения. Пожалуйста, введите PIN-код текстом."
            )
        else:
            logger.error("edit_pin_input: update.message is None")
        return EDIT_PIN
    
    text = update.message.text.strip()
    lead_id = context.user_data.get('editing_lead_id')
    
    if not lead_id:
        await update.message.reply_text(
            "❌ Ошибка: ID лида не найден. Пожалуйста, начните редактирование заново.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # PIN code is "2025"
    PIN_CODE = "2025"
    
    if text == PIN_CODE:
        # PIN is correct, reset attempt counter
        if 'pin_attempts' in context.user_data:
            del context.user_data['pin_attempts']
        
        # PIN is correct, show edit menu
        # Clear any old field editing state to prevent automatic transitions
        if 'current_field' in context.user_data:
            del context.user_data['current_field']
        if 'current_state' in context.user_data:
            del context.user_data['current_state']
        
        # Always reload lead data to ensure we have the latest from DB
        client = get_supabase_client()
        if client:
            try:
                response = client.table(TABLE_NAME).select("*").eq("id", lead_id).execute()
                if response.data and len(response.data) > 0:
                    lead = response.data[0]
                    lead_data = lead.copy()
                    if 'telegram_user' in lead_data and lead_data.get('telegram_user'):
                        if 'telegram_name' not in lead_data or not lead_data.get('telegram_name'):
                            lead_data['telegram_name'] = lead_data.get('telegram_user')
                    # Ensure all fields are present (even if None)
                    for field in ['fullname', 'manager_name', 'facebook_link', 'telegram_name', 'telegram_id']:
                        if field not in lead_data:
                            lead_data[field] = None
                    
                    # Save original data for comparison (deep copy)
                    context.user_data['original_lead_data'] = lead_data.copy()
                    
                    # Initialize user_data_store with current data
                    user_data_store[user_id] = lead_data.copy()
                    user_data_store_access_time[user_id] = time.time()
                else:
                    await update.message.reply_text(
                        "❌ Ошибка: Лид не найден в базе данных.",
                        reply_markup=get_main_menu_keyboard()
                    )
                    return ConversationHandler.END
            except Exception as e:
                logger.error(f"Error reloading lead data in PIN handler: {e}", exc_info=True)
                await update.message.reply_text(
                    "❌ Ошибка: Не удалось загрузить данные лида.",
                    reply_markup=get_main_menu_keyboard()
                )
                return ConversationHandler.END
        else:
            await update.message.reply_text(
                "❌ Ошибка: Не удалось подключиться к базе данных.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        message = f"✏️ Редактирование лида (ID: {lead_id})\n\nВыберите поле для редактирования:"
        await update.message.reply_text(
            message,
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}))
        )
        return EDIT_MENU
    else:
        # PIN is incorrect, increment attempt counter
        pin_attempts = context.user_data.get('pin_attempts', 0) + 1
        context.user_data['pin_attempts'] = pin_attempts
        
        if pin_attempts >= 3:
            # Too many failed attempts, return to main menu
            await update.message.reply_text(
                "❌ Превышено количество попыток ввода PIN-кода (3). Доступ к редактированию заблокирован.",
                reply_markup=get_main_menu_keyboard()
            )
            # Clear editing state
            if 'editing_lead_id' in context.user_data:
                del context.user_data['editing_lead_id']
            if 'pin_attempts' in context.user_data:
                del context.user_data['pin_attempts']
            return ConversationHandler.END
        else:
            # PIN is incorrect, ask again
            remaining_attempts = 3 - pin_attempts
            await update.message.reply_text(
                f"❌ Неверный PIN-код. Осталось попыток: {remaining_attempts}\n\nВведите PIN-код:"
            )
            return EDIT_PIN

def get_edit_field_keyboard(user_id: int, original_data: dict = None):
    """Create keyboard for editing lead fields with change indicators"""
    user_data = user_data_store.get(user_id, {})
    keyboard = []
    
    # Helper function to check if field has a value
    def has_value(field_name):
        value = user_data.get(field_name)
        return value is not None and value != '' and (not isinstance(value, str) or value.strip() != '')
    
    # Helper function to check if field was changed
    def is_changed(field_name):
        if not original_data:
            return False
        current_value = user_data.get(field_name)
        original_value = original_data.get(field_name)
        # Handle telegram_user/telegram_name mapping
        if field_name == 'telegram_name':
            original_value = original_data.get('telegram_name') or original_data.get('telegram_user')
        # Compare values (handle None and empty strings)
        if current_value is None and (original_value is None or original_value == ''):
            return False
        if current_value == '' and (original_value is None or original_value == ''):
            return False
        return str(current_value).strip() != str(original_value).strip() if original_value else current_value is not None
    
    # Helper function to get status indicator
    def get_status(field_name):
        if is_changed(field_name):
            return "🟡"  # Changed
        elif has_value(field_name):
            return "🟢"  # Filled, not changed
        else:
            return "⚪"  # Empty
    
    # Mandatory fields
    fullname_status = get_status('fullname')
    manager_status = get_status('manager_name')
    
    keyboard.append([InlineKeyboardButton(f"{fullname_status} Имя Фамилия *", callback_data="edit_field_fullname")])
    keyboard.append([InlineKeyboardButton(f"{manager_status} Агент *", callback_data="edit_field_manager")])
    
    # Identifier fields
    fb_link_status = get_status('facebook_link')
    telegram_name_status = get_status('telegram_name')
    telegram_id_status = get_status('telegram_id')
    
    keyboard.append([InlineKeyboardButton(f"{fb_link_status} Facebook Ссылка", callback_data="edit_field_fb_link")])
    keyboard.append([InlineKeyboardButton(f"{telegram_name_status} Имя пользователя Telegram", callback_data="edit_field_telegram_name")])
    keyboard.append([InlineKeyboardButton(f"{telegram_id_status} Telegram ID", callback_data="edit_field_telegram_id")])
    
    # Action buttons
    keyboard.append([InlineKeyboardButton("💾 Сохранить изменения", callback_data="edit_save")])
    keyboard.append([InlineKeyboardButton("❌ Отменить", callback_data="edit_cancel")])
    
    return InlineKeyboardMarkup(keyboard)

# Edit field callbacks (must be defined before create_telegram_app)
async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str, field_label: str, next_state: int):
    """Universal callback for editing field selection"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    # If field is already filled, ask if user wants to change it
    if user_data_store.get(user_id, {}).get(field_name):
        await query.edit_message_text(
            f"📝 {field_label} текущее значение: {user_data_store[user_id][field_name]}\n"
            f"Введите новое значение или отправьте /skip чтобы оставить текущее:"
        )
    else:
        await query.edit_message_text(f"📝 Введите {field_label}:")
    
    context.user_data['current_field'] = field_name
    context.user_data['current_state'] = next_state
    return next_state

async def edit_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Universal handler for edit field input"""
    if not update.message or not update.message.text:
        logger.error("edit_field_input: update.message or update.message.text is None")
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    
    # Check for /skip command first
    if update.message.text.strip() == "/skip":
        # User wants to skip changing this field, return to edit menu
        await update.message.reply_text(
            "✏️ Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}))
        )
        # Clear current_field to prevent issues
        if 'current_field' in context.user_data:
            del context.user_data['current_field']
        if 'current_state' in context.user_data:
            del context.user_data['current_state']
        return EDIT_MENU
    
    text = update.message.text.strip()
    
    # Check length for text fields (fullname, manager_name)
    field_name = context.user_data.get('current_field')
    if field_name in ['fullname', 'manager_name'] and len(text) > 500:
        field_label = get_field_label(field_name)
        await update.message.reply_text(
            f"❌ {field_label} слишком длинное (максимум 500 символов).\n\n"
            f"Попробуйте снова:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}))
        )
        return context.user_data.get('current_state', EDIT_MENU)
    
    # Update access time BEFORE cleanup to prevent deletion
    # Update access time BEFORE cleanup to protect from race conditions
    user_data_store_access_time[user_id] = time.time()
    
    # Ensure user_data_store entry exists before cleanup
    if user_id not in user_data_store:
        # Re-initialize from database if missing
        lead_id = context.user_data.get('editing_lead_id')
        if lead_id:
            client = get_supabase_client()
            if client:
                try:
                    response = client.table(TABLE_NAME).select("*").eq("id", lead_id).execute()
                    if response.data and len(response.data) > 0:
                        lead = response.data[0]
                        lead_data = lead.copy()
                        if 'telegram_user' in lead_data and lead_data.get('telegram_user'):
                            if 'telegram_name' not in lead_data or not lead_data.get('telegram_name'):
                                lead_data['telegram_name'] = lead_data.get('telegram_user')
                        for field in ['fullname', 'manager_name', 'facebook_link', 'telegram_name', 'telegram_id']:
                            if field not in lead_data:
                                lead_data[field] = None
                        # Save original data if not already saved
                        if 'original_lead_data' not in context.user_data:
                            context.user_data['original_lead_data'] = lead_data.copy()
                        user_data_store[user_id] = lead_data
                        user_data_store_access_time[user_id] = time.time()
                except Exception as e:
                    logger.error(f"Error reloading lead data: {e}", exc_info=True)
        else:
            # If no lead_id, create empty entry
            user_data_store[user_id] = {}
    
    # Cleanup with exclusion of current user to prevent race conditions
    cleanup_user_data_store(exclude_user_id=user_id)
    
    if not field_name:
        await update.message.reply_text(
            "❌ Ошибка: поле не определено.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Ensure user_data_store[user_id] exists
    if user_id not in user_data_store:
        user_data_store[user_id] = {}
    
    # Validate and normalize based on field type (same logic as add_field_input)
    validation_passed = False
    normalized_value = text
    
    if field_name == 'facebook_link':
        is_valid, error_msg, extracted = validate_facebook_link(text)
        if is_valid:
            validation_passed = True
            normalized_value = extracted
        else:
            await update.message.reply_text(f"❌ {error_msg}\n\nПопробуйте снова:")
            return context.user_data.get('current_state', EDIT_MENU)
    
    elif field_name == 'telegram_name':
        is_valid, error_msg, normalized = validate_telegram_name(text)
        if is_valid:
            validation_passed = True
            normalized_value = normalized
        else:
            await update.message.reply_text(f"❌ {error_msg}\n\nПопробуйте снова:")
            return context.user_data.get('current_state', EDIT_MENU)
    
    elif field_name == 'telegram_id':
        is_valid, error_msg, normalized = validate_telegram_id(text)
        if is_valid:
            validation_passed = True
            normalized_value = normalized
        else:
            await update.message.reply_text(f"❌ {error_msg}\n\nПопробуйте снова:")
            return context.user_data.get('current_state', EDIT_MENU)
    
    else:
        # For other fields (fullname, manager_name), normalize text and check not empty
        if text:
            # Normalize text fields (fullname, manager_name)
            normalized_value = normalize_text_field(text)
            if normalized_value:
                validation_passed = True
            else:
                # Text was only whitespace
                validation_passed = False
        else:
            await update.message.reply_text(f"❌ Поле не может быть пустым.\n\nПопробуйте снова:")
            return context.user_data.get('current_state', EDIT_MENU)
    
    # Save value only if validation passed
    if validation_passed and normalized_value:
        # Ensure user_data_store[user_id] exists before assignment (protection against race conditions)
        if user_id not in user_data_store:
            user_data_store[user_id] = {}
        user_data_store[user_id][field_name] = normalized_value
        # Update access time after saving to protect from cleanup
        user_data_store_access_time[user_id] = time.time()
        
        # Show confirmation message
        field_label = get_field_label(field_name)
        await update.message.reply_text(
            f"✅ <b>{field_label}</b> успешно изменено!\n\n"
            f"Новое значение: <code>{normalized_value}</code>\n\n"
            f"Выберите следующее поле для редактирования:",
            parse_mode='HTML',
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}))
        )
    else:
        # Show edit menu again (validation failed or value is empty)
        await update.message.reply_text(
            "✏️ Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}))
        )
    return EDIT_MENU

async def edit_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save edited lead"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lead_id = context.user_data.get('editing_lead_id')
    
    if not lead_id:
        await query.edit_message_text(
            "❌ Ошибка: ID лида не найден.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Get Supabase client first
    client = get_supabase_client()
    if not client:
        await query.edit_message_text(
            "❌ Ошибка: Не удалось подключиться к базе данных.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Load current data from database for merge logic
    try:
        response = client.table(TABLE_NAME).select("*").eq("id", lead_id).execute()
        if not response.data or len(response.data) == 0:
            await query.edit_message_text(
                "❌ Ошибка: Лид не найден в базе данных.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END
        
        current_db_data = response.data[0].copy()
        # Map telegram_user to telegram_name for comparison
        if 'telegram_user' in current_db_data and current_db_data.get('telegram_user'):
            if 'telegram_name' not in current_db_data or not current_db_data.get('telegram_name'):
                current_db_data['telegram_name'] = current_db_data.get('telegram_user')
    except Exception as e:
        logger.error(f"Error loading current lead data in save: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Ошибка: Не удалось загрузить данные лида из базы.",
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END
    
    # Get user_data (changes made by user)
    user_data = user_data_store.get(user_id, {})
    
    # Validation (same as add_save_callback)
    # Check if fullname is empty or None
    fullname_value = user_data.get('fullname')
    if not fullname_value or (isinstance(fullname_value, str) and not fullname_value.strip()):
        await query.edit_message_text(
            "❌ <b>Ошибка:</b> Имя Фамилия обязателен для заполнения!\n\n"
            "⚠️ Пожалуйста, заполните это поле перед сохранением.\n\n"
            "Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {})),
            parse_mode='HTML'
        )
        return EDIT_MENU
    
    # Check if manager_name is empty or None
    manager_value = user_data.get('manager_name')
    if not manager_value or (isinstance(manager_value, str) and not manager_value.strip()):
        await query.edit_message_text(
            "❌ <b>Ошибка:</b> Агент обязателен для заполнения!\n\n"
            "⚠️ Пожалуйста, заполните это поле перед сохранением.\n\n"
            "Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {})),
            parse_mode='HTML'
        )
        return EDIT_MENU
    
    # Check if at least one identifier is present
    required_fields = ['facebook_link', 'telegram_name', 'telegram_id']
    # Also check telegram_user for backward compatibility
    has_identifier = any(user_data.get(field) for field in required_fields) or user_data.get('telegram_user')
    
    if not has_identifier:
        await query.edit_message_text(
            "❌ Ошибка: Необходимо указать минимум одно из полей:\n"
            "Facebook Ссылка, Имя пользователя Telegram или Telegram ID!\n\n"
            "Выберите поле для редактирования:",
            reply_markup=get_edit_field_keyboard(user_id, context.user_data.get('original_lead_data', {}))
        )
        return EDIT_MENU
    
    # Merge logic: Start with current DB data, apply only changes from user_data_store
    # This ensures unchanged fields are not lost
    update_data = current_db_data.copy()
    
    # Remove fields that shouldn't be updated
    for field in ['id', 'created_at']:
        update_data.pop(field, None)
    
    # Apply only fields that were changed by the user
    # Compare with original data to determine what was actually changed
    original_data = context.user_data.get('original_lead_data', {})
    
    # Fields that can be edited
    editable_fields = ['fullname', 'manager_name', 'facebook_link', 'telegram_name', 'telegram_id']
    
    for field in editable_fields:
        if field in user_data:
            # Get current value from user_data (what user entered/changed)
            current_value = user_data.get(field)
            # Get original value (when editing started)
            original_value = original_data.get(field)
            # Handle telegram_user/telegram_name mapping for original
            if field == 'telegram_name':
                original_value = original_data.get('telegram_name') or original_data.get('telegram_user')
            
            # Normalize values for comparison (handle None, empty strings, whitespace)
            def normalize_for_compare(val):
                if val is None:
                    return ''
                if isinstance(val, str):
                    return val.strip()
                return str(val).strip()
            
            current_normalized = normalize_for_compare(current_value)
            original_normalized = normalize_for_compare(original_value)
            
            # If value changed, update it
            if current_normalized != original_normalized:
                update_data[field] = current_value
    
    # Map telegram_name to telegram_user for database (backward compatibility)
    if 'telegram_name' in update_data:
        telegram_name_value = update_data.pop('telegram_name')
        # Only set telegram_user if telegram_name has a value
        if telegram_name_value:
            update_data['telegram_user'] = telegram_name_value
        # If telegram_name is empty, also clear telegram_user
        elif 'telegram_user' in update_data:
            update_data['telegram_user'] = None
    
    try:

        # This ensures we update all fields that were in user_data
        clean_update_data = {}
        for k, v in update_data.items():
            # Keep all values except None (None means field wasn't set)
            # Empty strings are valid and should be saved
            if v is not None:
                clean_update_data[k] = v
        
        response = client.table(TABLE_NAME).update(clean_update_data).eq("id", lead_id).execute()
        
        if response.data:
            await query.edit_message_text(
                "✅ <b>Лид успешно обновлен!</b>",
                reply_markup=get_main_menu_keyboard(),
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(
                "❌ Ошибка: Данные не были обновлены. Попробуйте снова.",
                reply_markup=get_main_menu_keyboard()
            )
    
    except Exception as e:
        logger.error(f"Error updating lead: {e}", exc_info=True)
        error_msg = "❌ Произошла ошибка при обновлении данных. Попробуйте позже или обратитесь к администратору."
        await query.edit_message_text(
            error_msg,
            reply_markup=get_main_menu_keyboard()
        )
    
    # Clean up
    if user_id in user_data_store:
        del user_data_store[user_id]
    if user_id in user_data_store_access_time:
        del user_data_store_access_time[user_id]
    if 'editing_lead_id' in context.user_data:
        del context.user_data['editing_lead_id']
    if 'original_lead_data' in context.user_data:
        del context.user_data['original_lead_data']
    
    return ConversationHandler.END

async def edit_lead_entry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for editing a lead - parses lead_id from callback_data"""
    query = update.callback_query
    if not query or not query.data:
        logger.error("edit_lead_entry_callback: query or query.data is None")
        return ConversationHandler.END
    
    data = query.data
    
    # Parse lead_id from callback_data (format: "edit_lead_123")
    try:
        lead_id = int(data.split("_")[-1])
        return await edit_lead_callback(update, context, lead_id)
    except (ValueError, IndexError) as e:
        logger.error(f"Error parsing edit_lead callback: {e}")
        await query.answer("❌ Ошибка: Неверный формат запроса.", show_alert=True)
        return ConversationHandler.END

async def edit_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel editing lead"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id in user_data_store:
        del user_data_store[user_id]
    if user_id in user_data_store_access_time:
        del user_data_store_access_time[user_id]
    if 'editing_lead_id' in context.user_data:
        del context.user_data['editing_lead_id']
    if 'original_lead_data' in context.user_data:
        del context.user_data['original_lead_data']
    
    await query.edit_message_text(
        "❌ Редактирование отменено.",
        reply_markup=get_main_menu_keyboard()
    )
    return ConversationHandler.END

# Edit field callbacks
async def edit_field_fullname_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'fullname', 'Клиент', EDIT_FULLNAME)

async def edit_field_fb_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'facebook_link', 'Facebook Ссылка', EDIT_FB_LINK)

async def edit_field_telegram_name_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'telegram_name', 'Имя пользователя Telegram', EDIT_TELEGRAM_NAME)

async def edit_field_telegram_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'telegram_id', 'Telegram ID', EDIT_TELEGRAM_ID)

async def edit_field_manager_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await edit_field_callback(update, context, 'manager_name', 'Manager Name', EDIT_MANAGER_NAME)

# Flask routes
@app.route('/')
def index():
    """Health check endpoint for Koyeb"""
    return jsonify({
        "status": "ok",
        "service": "telegram-bot",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

@app.route('/health')
def health():
    """Health check endpoint (alias for compatibility)"""
    return jsonify({
        "status": "ok",
        "service": "telegram-bot",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

@app.route('/ready')
def ready():
    """Readiness probe endpoint for Koyeb"""
    checks = {
        "telegram_app": telegram_app is not None,
        "supabase_client": supabase is not None,
        "telegram_event_loop": telegram_event_loop is not None and telegram_event_loop.is_running() if telegram_event_loop else False
    }
    
    all_ready = all(checks.values())
    status_code = 200 if all_ready else 503
    
    return jsonify({
        "status": "ready" if all_ready else "not ready",
        "checks": checks,
        "timestamp": datetime.utcnow().isoformat()
    }), status_code

def cleanup_on_shutdown():
    """Cleanup resources on shutdown"""
    global shutdown_requested, telegram_app, telegram_event_loop, supabase
    
    shutdown_requested = True
    
    try:
        # Stop Telegram app
        if telegram_app:
            import asyncio
            if telegram_event_loop and telegram_event_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    telegram_app.stop(),
                    telegram_event_loop
                )
                asyncio.run_coroutine_threadsafe(
                    telegram_app.shutdown(),
                    telegram_event_loop
                )
    except Exception as e:
        logger.error(f"Error stopping Telegram app: {e}", exc_info=True)
    
    # Clear cache
    uniqueness_cache.clear()
    

def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown"""
    def signal_handler(signum, frame):
        cleanup_on_shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming Telegram updates via webhook"""
    try:
        json_data = request.get_json()
        if json_data:
            
            # Check if telegram_app is initialized
            if telegram_app is None:
                logger.error("Telegram app is not initialized yet")
                return "Service not ready", 503
            
            update = Update.de_json(json_data, telegram_app.bot)
            
            # Process update asynchronously using the telegram event loop
            import asyncio
            if telegram_event_loop and telegram_event_loop.is_running():
                # Schedule update processing in the telegram event loop
                asyncio.run_coroutine_threadsafe(
                    telegram_app.process_update(update),
                    telegram_event_loop
                )
            else:
                # Fallback: process synchronously if loop not ready
                logger.warning("Telegram event loop not ready, processing update synchronously")
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(telegram_app.process_update(update))
                loop.close()
        else:
            logger.warning("Received empty webhook request")
        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return "Error", 500

async def setup_webhook():
    """Set up the webhook for Telegram bot"""
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        # Drop pending updates to clear old states on deploy
        await telegram_app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True  # Сбрасывает все ожидающие обновления
        )
        logger.info("Webhook set successfully with pending updates dropped")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")

# Initialize Telegram application
telegram_app = None
telegram_event_loop = None

def initialize_telegram_app():
    """Initialize Telegram app - called on module import (needed for gunicorn)"""
    global telegram_app, telegram_event_loop, user_data_store, user_data_store_access_time
    
    # Clear all user data stores on startup (fresh start after deploy)
    user_data_store.clear()
    user_data_store_access_time.clear()
    logger.info("Cleared user_data_store and user_data_store_access_time on startup")
    
    # Validate environment variables first
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found - Telegram app will not be initialized")
        return
    
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL not found - Telegram app will not be initialized")
        return
    
    
    try:
        # Create Telegram app
        create_telegram_app()
    except Exception as e:
        logger.error(f"Failed to create Telegram app: {e}", exc_info=True)
        return
    
    # Initialize Telegram bot in a separate thread
    import asyncio
    import threading
    
    def run_telegram_setup():
        """Run async webhook setup and start update processing in a separate thread"""
        global telegram_event_loop
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            telegram_event_loop = loop  # Save reference for webhook
            
            loop.run_until_complete(telegram_app.initialize())
            
            loop.run_until_complete(setup_webhook())
            
            # Start processing updates
            loop.run_until_complete(telegram_app.start())
            
            # Setup keep-alive scheduler after bot is started
            setup_keep_alive_scheduler()
            
            # Keep the loop running to process updates
            loop.run_forever()
        except Exception as e:
            logger.error(f"Error in Telegram setup thread: {e}", exc_info=True)
    
    # Start webhook setup in background
    setup_thread = threading.Thread(target=run_telegram_setup)
    setup_thread.daemon = True
    setup_thread.start()
    

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors in Telegram handlers"""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    
    # Extra diagnostics for tag-related flows
    try:
        from telegram import Update as TgUpdate  # type: ignore
        if isinstance(update, TgUpdate) and update.effective_user:
            user_id = update.effective_user.id
            log_conversation_state(user_id, context, prefix="[ERROR_STATE]")
    except Exception as state_err:
        logger.error(f"[ERROR_STATE] Failed to log state in error_handler: {state_err}", exc_info=True)
    
    # Try to notify user if update is available
    # Use direct calls without retry to avoid infinite recursion
    if update and isinstance(update, Update):
        try:
            if update.message:
                try:
                    await update.message.reply_text(
                        "⚠️ Произошла временная ошибка. Попробуйте снова через несколько секунд."
                    )
                except Exception:
                    pass  # Silently fail if we can't send error message
            elif update.callback_query:
                try:
                    await update.callback_query.answer(
                        text="⚠️ Временная ошибка. Попробуйте снова.",
                        show_alert=True
                    )
                except Exception:
                    pass  # Silently fail if we can't send error message
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

def create_telegram_app():
    """Create and configure Telegram application"""
    global telegram_app
    
    # Create application with timeout settings
    request = HTTPXRequest(connection_pool_size=8, connect_timeout=10.0, read_timeout=10.0)
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).build()
    
    # Add error handler FIRST (before other handlers)
    telegram_app.add_error_handler(error_handler)
    
    # Add command handlers
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("q", quit_command))
    telegram_app.add_handler(CommandHandler("help", help_command))
    # Note: /q command has high priority and will work from any state
    # /tag is handled via tag_conv ConversationHandler entry_points

    async def debug_log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Global debug logger for all updates (does not interfere with handlers)."""
        try:
            user_id = update.effective_user.id if getattr(update, "effective_user", None) else None
            if getattr(update, "message", None):
                msg = update.message
                is_forwarded = bool(
                    msg.forward_from or msg.forward_from_chat or msg.forward_sender_name
                )
                # ADD DETAILED LOGGING FOR TEXT MESSAGES (especially for tag PIN input)
                if msg.text and not msg.text.startswith('/'):
                    logger.info(
                        f"[UPDATE] type=message user_id={user_id} "
                        f"text='{msg.text}' is_forwarded={is_forwarded} "
                        f"is_command=False"
                    )
                    # Log conversation state for text messages to track tag flow
                    if user_id is not None:
                        log_conversation_state(user_id, context, prefix="[UPDATE_TEXT_MSG]")
                else:
                    logger.info(
                        f"[UPDATE] type=message user_id={user_id} "
                        f"text='{msg.text or ''}' is_forwarded={is_forwarded}"
                    )
            elif getattr(update, "callback_query", None):
                q = update.callback_query
                logger.info(
                    f"[UPDATE] type=callback user_id={user_id} data='{q.data}'"
                )
            else:
                logger.info(f"[UPDATE] type=other raw_update={update}")

            if user_id is not None and not (getattr(update, "message", None) and update.message.text and not update.message.text.startswith('/')):
                # Log state for non-text messages (already logged above for text messages)
                log_conversation_state(user_id, context, prefix="[UPDATE_STATE]")
        except Exception as e:
            logger.error(f"[UPDATE] Failed to log update: {e}", exc_info=True)

    # Global debug logger - register early, but it never returns states so it won't affect flows
    telegram_app.add_handler(MessageHandler(filters.ALL, debug_log_update), group=99)
    
    # Global handler for forwarded messages (register BEFORE ConversationHandlers)
    # This allows forwarding messages to work from any state
    # The handler returns None if user is already in add flow, allowing add_field_input to handle it
    forwarded_message_handler = MessageHandler(
        filters.FORWARDED,
        handle_forwarded_message
    )
    telegram_app.add_handler(forwarded_message_handler)
    
    # Global handler for regular photo messages (register AFTER forwarded messages handler)
    # This handles regular (not forwarded) photo messages to start add lead flow
    photo_message_handler = MessageHandler(
        filters.PHOTO & ~filters.FORWARDED,
        handle_photo_message
    )
    telegram_app.add_handler(photo_message_handler)
    
    # Smart check conversation handler (register FIRST to have priority)
    smart_check_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(check_menu_callback, pattern="^check_menu$")],
        states={
            SMART_CHECK_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, smart_check_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ]
        },
        fallbacks=[
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
        ],
        per_message=False,
    )
    
    # Old conversation handlers for checking (kept for backward compatibility, but not registered)
    # These are no longer used but kept in case we need them in the future
    # check_telegram_conv = ConversationHandler(
    #     entry_points=[CallbackQueryHandler(check_telegram_callback, pattern="^check_telegram$")],
    #     states={
    #         CHECK_BY_TELEGRAM: [
    #             MessageHandler(filters.TEXT & ~filters.COMMAND, check_telegram_input),
    #             CommandHandler("q", quit_command),
    #             CommandHandler("start", start_command),
    #         ]
    #     },
    #     fallbacks=[
    #         CommandHandler("q", quit_command), 
    #         CommandHandler("start", start_command),
    #         CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
    #     ],
    #     per_message=False,
    # )
    # 
    # check_fb_link_conv = ConversationHandler(
    #     entry_points=[CallbackQueryHandler(check_fb_link_callback, pattern="^check_fb_link$")],
    #     states={
    #         CHECK_BY_FB_LINK: [
    #             MessageHandler(filters.TEXT & ~filters.COMMAND, check_fb_link_input),
    #             CommandHandler("q", quit_command),
    #             CommandHandler("start", start_command),
    #         ]
    #     },
    #     fallbacks=[
    #         CommandHandler("q", quit_command), 
    #         CommandHandler("start", start_command),
    #         CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
    #     ],
    #     per_message=False,
    # )
    # 
    # check_telegram_id_conv = ConversationHandler(
    #     entry_points=[CallbackQueryHandler(check_telegram_id_callback, pattern="^check_telegram_id$")],
    #     states={
    #         CHECK_BY_TELEGRAM_ID: [
    #             MessageHandler(filters.TEXT & ~filters.COMMAND, check_telegram_id_input),
    #             CommandHandler("q", quit_command),
    #             CommandHandler("start", start_command),
    #         ]
    #     },
    #     fallbacks=[
    #         CommandHandler("q", quit_command), 
    #         CommandHandler("start", start_command),
    #         CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
    #     ],
    #     per_message=False,
    # )
    # 
    # check_fullname_conv = ConversationHandler(
    #     entry_points=[CallbackQueryHandler(check_fullname_callback, pattern="^check_fullname$")],
    #     states={
    #         CHECK_BY_FULLNAME: [
    #             MessageHandler(filters.TEXT & ~filters.COMMAND, check_fullname_input),
    #             CommandHandler("q", quit_command),
    #             CommandHandler("start", start_command),
    #         ]
    #     },
    #     fallbacks=[
    #         CommandHandler("q", quit_command), 
    #         CommandHandler("start", start_command),
    #         CallbackQueryHandler(check_menu_callback, pattern="^check_menu$"),
    #     ],
    #     per_message=False,
    # )
    
    # Conversation handler for adding - sequential flow
    add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_new_callback, pattern="^add_new$"),
            # Allow MessageHandler to enter if user has add state initialized (from forwarded message)
            MessageHandler(filters.TEXT & ~filters.COMMAND, check_add_state_entry),
            # Allow CallbackQueryHandler to enter if user has add state initialized (from forwarded message)
            # This handles callbacks like add_skip, add_back when flow was started via forwarded message
            CallbackQueryHandler(check_add_state_entry_callback, pattern="^(add_skip|add_back|add_cancel)$")
        ],
        states={
            ADD_FULLNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_field_input),
                CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
                CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            ADD_FB_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_field_input),
                CallbackQueryHandler(add_skip_callback, pattern="^add_skip$"),
                CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
                CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            ADD_TELEGRAM_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_field_input),
                CallbackQueryHandler(add_skip_callback, pattern="^add_skip$"),
                CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
                CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            ADD_TELEGRAM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_field_input),
                CallbackQueryHandler(add_skip_callback, pattern="^add_skip$"),
                CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
                CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            ADD_REVIEW: [
                CallbackQueryHandler(add_save_callback, pattern="^add_save$"),
                CallbackQueryHandler(edit_fullname_from_review_callback, pattern="^edit_fullname_from_review$"),
                CallbackQueryHandler(add_back_callback, pattern="^add_back$"),
                CallbackQueryHandler(add_cancel_callback, pattern="^add_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
        },
        fallbacks=[CommandHandler("q", quit_command), CommandHandler("start", start_command)],
        per_message=False,
    )
    
    # Register ConversationHandlers FIRST (before button_callback) to have priority.
    # IMPORTANT: Order matters. `tag_conv` must be registered BEFORE `add_conv`,
    # otherwise `add_conv` entry_point `check_add_state_entry` can intercept PIN input.
    telegram_app.add_handler(smart_check_conv)  # Smart check with auto-detection
    
    # Edit conversation handler - register with other ConversationHandlers for priority
    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_lead_entry_callback, pattern="^edit_lead_\\d+$"),
            CallbackQueryHandler(edit_field_fullname_callback, pattern="^edit_field_fullname$"),
            CallbackQueryHandler(edit_field_fb_link_callback, pattern="^edit_field_fb_link$"),
            CallbackQueryHandler(edit_field_telegram_name_callback, pattern="^edit_field_telegram_name$"),
            CallbackQueryHandler(edit_field_telegram_id_callback, pattern="^edit_field_telegram_id$"),
            CallbackQueryHandler(edit_field_manager_callback, pattern="^edit_field_manager$"),
            CallbackQueryHandler(edit_save_callback, pattern="^edit_save$"),
            CallbackQueryHandler(edit_cancel_callback, pattern="^edit_cancel$"),
        ],
        states={
            EDIT_PIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_pin_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_MENU: [
                CallbackQueryHandler(edit_field_fullname_callback, pattern="^edit_field_fullname$"),
                CallbackQueryHandler(edit_field_fb_link_callback, pattern="^edit_field_fb_link$"),
                CallbackQueryHandler(edit_field_telegram_name_callback, pattern="^edit_field_telegram_name$"),
                CallbackQueryHandler(edit_field_telegram_id_callback, pattern="^edit_field_telegram_id$"),
                CallbackQueryHandler(edit_field_manager_callback, pattern="^edit_field_manager$"),
                CallbackQueryHandler(edit_save_callback, pattern="^edit_save$"),
                CallbackQueryHandler(edit_cancel_callback, pattern="^edit_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_FULLNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_FB_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_TELEGRAM_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_TELEGRAM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            EDIT_MANAGER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
        },
        fallbacks=[
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
            CommandHandler("skip", lambda u, c: edit_field_input(u, c)),
        ],
        per_message=False,
    )
    telegram_app.add_handler(edit_conv)
    
    # Tag conversation handler - for changing manager_tag
    tag_conv = ConversationHandler(
        entry_points=[
            CommandHandler("tag", tag_command),
            CallbackQueryHandler(tag_manager_callback, pattern="^tag_mgr_\\d+$")
        ],
        states={
            TAG_PIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tag_pin_input),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            TAG_SELECT_MANAGER: [
                CallbackQueryHandler(tag_manager_callback, pattern="^tag_mgr_\\d+$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
            TAG_ENTER_NEW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tag_enter_new),
                CallbackQueryHandler(tag_confirm_callback, pattern="^tag_confirm$"),
                CallbackQueryHandler(tag_cancel_callback, pattern="^tag_cancel$"),
                CommandHandler("q", quit_command),
                CommandHandler("start", start_command),
            ],
        },
        fallbacks=[
            CommandHandler("q", quit_command),
            CommandHandler("start", start_command),
        ],
        per_message=False,
    )
    telegram_app.add_handler(tag_conv)

    # Old check handlers are no longer registered (commented out above)
    # Register AFTER tag_conv so /tag PIN flow has priority over add flow entry points.
    telegram_app.add_handler(add_conv)
    
    # Add callback query handler for menu navigation buttons
    # Registered AFTER ConversationHandlers so they have priority
    # Note: check_menu is now handled by smart_check_conv ConversationHandler
    # Note: add_new is included here as fallback, but ConversationHandler should catch it first
    telegram_app.add_handler(CallbackQueryHandler(button_callback, pattern="^(main_menu|add_menu|add_new)$"))
    
    # Add handler for unknown commands during conversations (must be after command handlers)
    # Exclude /start, /q, /skip, and /tag (skip is handled by ConversationHandlers, tag should interrupt any process)
    telegram_app.add_handler(MessageHandler(filters.COMMAND & ~filters.Regex("^(/start|/q|/skip|/tag)$"), unknown_command_handler))
    
    # Add global fallback for unknown callback queries (must be last, after all ConversationHandlers)
    telegram_app.add_handler(CallbackQueryHandler(unknown_callback_handler))
    
    return telegram_app

# Setup signal handlers for graceful shutdown
setup_signal_handlers()

async def single_keep_alive():
    """Keep bot alive by calling bot.get_me() - works even in Sleeping state"""
    global telegram_app
    if telegram_app is None or telegram_app.bot is None:
        logger.warning("Keep-alive: Telegram app not initialized")
        return
    
    try:
        await telegram_app.bot.get_me()
        logger.debug("Keep-alive OK: bot.get_me() successful")
    except Exception as e:
        logger.warning(f"Keep-alive failed: {e}")

def setup_keep_alive_scheduler():
    """Setup APScheduler to keep bot alive"""
    global scheduler, telegram_event_loop
    
    if telegram_event_loop is None:
        logger.warning("Keep-alive scheduler: Telegram event loop not ready, will retry later")
        return
    
    try:
        # Create scheduler with the telegram event loop
        scheduler = AsyncIOScheduler(event_loop=telegram_event_loop)
        
        # Add job to call bot.get_me() every 5 minutes
        scheduler.add_job(
            single_keep_alive,
            trigger=IntervalTrigger(minutes=5),
            id='keep_alive',
            name='Keep bot alive',
            replace_existing=True
        )
        
        scheduler.start()
        logger.info("Keep-alive scheduler started: bot.get_me() will be called every 5 minutes")
    except Exception as e:
        logger.error(f"Failed to setup keep-alive scheduler: {e}", exc_info=True)

# Initialize Telegram app when module is imported (needed for gunicorn)
# This ensures telegram_app is initialized even when running with gunicorn
try:
    initialize_telegram_app()
except Exception as e:
    logger.error(f"Failed to initialize Telegram app on module import: {e}", exc_info=True)

if __name__ == '__main__':
    # Validate environment variables
    missing_vars = []
    
    if not TELEGRAM_BOT_TOKEN:
        missing_vars.append("TELEGRAM_BOT_TOKEN")
    
    if not WEBHOOK_URL:
        missing_vars.append("WEBHOOK_URL")
    
    if not SUPABASE_URL:
        missing_vars.append("SUPABASE_URL")
    
    if not SUPABASE_KEY:
        missing_vars.append("SUPABASE_KEY")
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.error("Please set all required environment variables in Koyeb dashboard")
        exit(1)
    
    # Note: Supabase client will be initialized lazily on first use
    
    # Telegram app is already initialized by initialize_telegram_app() above
    # Give it a moment to initialize
    import time
    time.sleep(2)
    
    # For production, gunicorn will be used (see Procfile)
    # This code is kept for local development
    logger.warning("For production, use: gunicorn -w 1 -b 0.0.0.0:$PORT main:app")
    app.run(host='0.0.0.0', port=PORT, debug=False)
