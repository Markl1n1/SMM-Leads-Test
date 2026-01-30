import asyncio
import re
from urllib.parse import urlparse, parse_qs

from telegram.error import TimedOut, NetworkError, RetryAfter

from bot.logging import logger


def normalize_telegram_id(tg_id: str) -> str:
    """Normalize Telegram ID: extract only digits (similar to phone)"""
    if not tg_id:
        return ""
    return ''.join(filter(str.isdigit, tg_id))


def normalize_tag(tag: str) -> str:
    """Normalize tag: handle three formats and return format 3 (username without @ and without https://t.me/)"""
    if not tag:
        return ""
    normalized = tag.strip()
    if normalized.startswith('https://t.me/'):
        normalized = normalized.replace('https://t.me/', '').strip()
    elif normalized.startswith('http://t.me/'):
        normalized = normalized.replace('http://t.me/', '').strip()
    elif normalized.startswith('t.me/'):
        normalized = normalized.replace('t.me/', '').strip()
    normalized = normalized.replace('@', '').strip()
    if '/' in normalized:
        normalized = normalized.split('/')[0]
    if '?' in normalized:
        normalized = normalized.split('?')[0]
    return normalized


def normalize_text_field(text: str) -> str:
    """Normalize text field (fullname, manager_name): trim spaces, collapse multiple spaces, limit length"""
    if not text:
        return ""
    normalized = text.strip()
    normalized = ' '.join(normalized.split())
    normalized = ''.join(char for char in normalized if char.isprintable() or char.isspace())
    normalized = normalized.strip()
    if len(normalized) > 500:
        normalized = normalized[:500]
    return normalized


def escape_html(text: str) -> str:
    """Escape HTML special characters"""
    if not text:
        return text
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def format_facebook_link_for_display(value: str) -> str:
    """Format Facebook link value to full URL for display"""
    if not value:
        return value
    value = str(value).strip()
    if value.startswith('http://') or value.startswith('https://'):
        return value
    if value.isdigit():
        return f"https://www.facebook.com/profile.php?id={value}"
    return f"https://www.facebook.com/{value}"


def get_user_friendly_error(error: Exception, operation: str = "операция") -> str:
    """Convert technical errors to user-friendly messages"""
    error_str = str(error).lower()
    if 'connection' in error_str or 'timeout' in error_str or 'network' in error_str:
        return (
            f"⚠️ Проблема с подключением к базе данных.\n\n"
            f"ℹ️ Что можно сделать:\n"
            f"• Проверьте интернет-соединение\n"
            f"• Попробуйте через несколько секунд\n"
            f"• Если проблема сохраняется, обратитесь к администратору"
        )
    if 'postgres' in error_str or 'database' in error_str or 'query' in error_str:
        return (
            f"⚠️ Ошибка при выполнении запроса к базе данных.\n\n"
            f"ℹ️ Попробуйте:\n"
            f"• Повторить операцию\n"
            f"• Проверить правильность введенных данных"
        )
    if 'не может быть пустым' in error_str or 'неверный формат' in error_str:
        return str(error)
    return (
        f"❌ Произошла ошибка при {operation}.\n\n"
        f"ℹ️ Попробуйте:\n"
        f"• Повторить операцию\n"
        f"• Проверить введенные данные\n"
        f"• Обратиться к администратору, если проблема сохраняется"
    )


def validate_facebook_link(link: str) -> tuple[bool, str, str]:
    """Validate Facebook link and extract username or ID."""
    if not link:
        return False, "Facebook ссылка не может быть пустой", ""
    link_clean = link.strip()
    if link_clean.startswith('@'):
        link_clean = link_clean[1:]
    if link_clean.isdigit() and len(link_clean) >= 14:
        return True, "", link_clean
    link_lower = link_clean.lower()
    has_url_patterns = (
        'facebook.com' in link_lower or
        'http://' in link_lower or
        'https://' in link_lower or
        'www.' in link_lower
    )
    if not has_url_patterns:
        if link_clean and not ' ' in link_clean:
            has_letters = any(c.isalpha() for c in link_clean)
            is_valid_username_format = all(c.isalnum() or c in ['.', '_', '-'] for c in link_clean)
            if has_letters and is_valid_username_format and len(link_clean) >= 3:
                return True, "", link_clean
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
    if not is_facebook_url:
        if (link_lower.startswith('www.facebook.com/') or 
            link_lower.startswith('facebook.com/') or 
            link_lower.startswith('m.facebook.com/')):
            is_facebook_url = True
    if not is_facebook_url:
        return False, "Неверный формат Facebook ссылки.", ""
    try:
        url_to_parse = link_clean if link_clean.startswith('http') else f'https://{link_clean}'
        parsed = urlparse(url_to_parse)
        path = parsed.path.strip('/')
        query = parsed.query
        if 'id=' in query or 'id=' in link_clean:
            id_value = None
            if 'id=' in query:
                id_value = parse_qs(query).get('id', [None])[0]
            elif 'id=' in link_clean:
                id_part_raw = link_clean.split('id=')[-1]
                id_part = ""
                for char in id_part_raw:
                    if char.isdigit():
                        id_part += char
                    elif char in ['&', '#', '?', '/', '\\', ']', '[', ')', '(', '}', '{', ' ', '\t', '\n']:
                        break
                    else:
                        break
                if id_part and id_part.isdigit() and len(id_part) >= 5:
                    return True, "", id_part
            if id_value:
                id_digits = ''.join(filter(str.isdigit, str(id_value)))
                if id_digits and len(id_digits) >= 5:
                    return True, "", id_digits
        if path:
            path_parts = [p for p in path.split('/') if p]
            if path_parts:
                username = path_parts[-1]
                if '?' in username:
                    username = username.split('?')[0]
                if '#' in username:
                    username = username.split('#')[0]
                cleaned_username = username
                while cleaned_username and not cleaned_username[-1].isalnum() and cleaned_username[-1] not in ['.', '_', '-']:
                    cleaned_username = cleaned_username[:-1]
                if cleaned_username:
                    return True, "", cleaned_username
        link_clean_old = link_clean
        if link_clean_old.startswith('http://'):
            link_clean_old = link_clean_old[7:]
        elif link_clean_old.startswith('https://'):
            link_clean_old = link_clean_old[8:]
        if link_clean_old.startswith('www.'):
            link_clean_old = link_clean_old[4:]
        if link_clean_old.lower().startswith('facebook.com/'):
            link_clean_old = link_clean_old[13:]
        elif link_clean_old.lower().startswith('m.facebook.com/'):
            link_clean_old = link_clean_old[15:]
        if '?' in link_clean_old:
            link_clean_old = link_clean_old.split('?')[0]
        if '#' in link_clean_old:
            link_clean_old = link_clean_old.split('#')[0]
        link_clean_old = link_clean_old.rstrip('/')
        while link_clean_old and not link_clean_old[-1].isalnum() and link_clean_old[-1] not in ['.', '_', '-']:
            link_clean_old = link_clean_old[:-1]
        parts = link_clean_old.split('/')
        if parts:
            extracted = parts[-1] if parts[-1] else (parts[-2] if len(parts) > 1 else "")
            if extracted:
                cleaned_username = extracted
                while cleaned_username and not cleaned_username[-1].isalnum() and cleaned_username[-1] not in ['.', '_', '-']:
                    cleaned_username = cleaned_username[:-1]
                if cleaned_username:
                    return True, "", cleaned_username
    except Exception as e:
        from bot.logging import logger as _logger
        _logger.error(f"[VALIDATE_FB_LINK] Error parsing URL: {e}, link: {link_clean}")
    return False, "Неверный формат Facebook ссылки.", ""


def validate_telegram_name(tg_name: str) -> tuple[bool, str, str]:
    """Validate Telegram name: remove @ if present, remove all spaces, check not empty"""
    if not tg_name:
        return False, "Тег Telegram не может быть пустым", ""
    normalized = tg_name.replace(' ', '').replace('\t', '').replace('\n', '')
    normalized = normalized.replace('@', '')
    normalized = normalized.strip()
    if not normalized:
        return False, "Тег Telegram не может быть пустым", ""
    return True, "", normalized


def validate_telegram_id(tg_id: str) -> tuple[bool, str, str]:
    """Validate Telegram ID: must contain only digits"""
    if not tg_id:
        return False, "Telegram ID не может быть пустым", ""
    if not tg_id.isdigit():
        return False, "Telegram ID должен содержать только цифры", ""
    normalized = normalize_telegram_id(tg_id)
    if not normalized:
        return False, "Telegram ID не может быть пустым", ""
    return True, "", normalized


def get_field_format_requirements(field_name: str) -> str:
    requirements = {
        'fullname': (
            "📋 <b>Требования к формату:</b>\n"
            "• Введите имя и фамилию клиента\n"
            "• Можно использовать любые буквы (русские, латинские)\n"
            "• Пробелы между словами разрешены\n"
            "• Минимум 3 символа (для поиска)\n"
            "• Максимум 500 символов\n\n"
            "💡 <b>Примеры:</b>\n"
            "<code>Иван Иванов</code>\n"
            "<code>John Smith</code>\n"
            "<code>Мария Петрова-Сидорова</code>"
        ),
        'manager_name': (
            "📋 <b>Требования к формату:</b>\n"
            "• Введите стейдж менеджера (так менеджер записан в отчётности)\n"
            "• Можно использовать любые буквы (русские, латинские)\n"
            "• Пробелы между словами разрешены\n"
            "• Максимум 500 символов\n\n"
            "💡 <b>Примеры:</b>\n"
            "<code>Анна</code>\n"
            "<code>Петр Сидоров</code>\n"
            "<code>Maria</code>"
        ),
        'facebook_link': (
            "📋 <b>Примеры допустимых вариантов:</b>\n"
            "• <code>https://www.facebook.com/username</code>\n"
            "• <code>www.facebook.com/username</code>\n"
            "• <code>facebook.com/username</code>\n"
            "• <code>https://m.facebook.com/profile.php?id=123456789012345</code>\n"
            "• <code>https://m.facebook.com/username</code>\n\n"
            "💡 Можно вставлять ссылку целиком, бот сам извлечёт username или ID.\n\n"
            "‼️ <b>Важно:</b> добавляйте только прямую ссылку на профиль (без фото, информации и прочих вкладок)."
        ),
        'telegram_name': (
            "📋 <b>Требования к формату:</b>\n"
            "• Пробелы не допускаются\n"
            "• Минимум 5 символов (для надежного поиска)\n"
            "• Разрешены: буквы, цифры, точки, подчеркивания, дефисы\n\n"
            "💡 <b>Примеры:</b>\n"
            "<code>username</code>\n"
            "<code>Ivan_123</code>\n"
            "<code>john_doe</code>\n\n"
            "⚠️ <b>Важно:</b> Не указывайте символ @ в начале"
        ),
        'telegram_id': (
            "📋 <b>Требования к формату:</b>\n"
            "• Только цифры (без букв и символов)\n"
            "• Без пробелов\n"
            "• Для поиска требуется минимум 5 цифр\n\n"
            "💡 <b>Примеры:</b>\n"
            "<code>123456789</code>\n"
            "<code>987654321</code>\n"
            "<code>12345</code>"
        )
    }
    return requirements.get(field_name, "")


def get_field_label(field_name: str) -> str:
    labels = {
        'fullname': 'имя клиента',
        'manager_name': 'имя агента',
        'facebook_link': 'ссылку клиента',
        'telegram_name': 'username клиента',
        'telegram_id': 'ID клиента'
    }
    return labels.get(field_name, field_name)


def detect_search_type(value: str) -> tuple[str, str]:
    """
    Automatically detect the type of search value.
    Returns: (field_type, normalized_value)
    field_type can be: 'facebook_link', 'telegram_id', 'telegram_user', 'fullname', 'unknown'
    """
    if not value:
        return 'unknown', ''

    value_stripped = value.strip()

    # 1. Check for pure numeric IDs FIRST (before Facebook URL validation)
    # Telegram ID = 10 digits, Facebook ID = 14+ digits
    if value_stripped.isdigit():
        digit_length = len(value_stripped)
        if digit_length == 10:
            normalized = normalize_telegram_id(value_stripped)
            if normalized:
                return 'telegram_id', normalized
        elif digit_length >= 14:
            is_valid_fb, _, fb_normalized = validate_facebook_link(value_stripped)
            if is_valid_fb:
                return 'facebook_link', fb_normalized
        elif 11 <= digit_length <= 13:
            is_valid_fb, _, fb_normalized = validate_facebook_link(value_stripped)
            if is_valid_fb:
                return 'facebook_link', fb_normalized
            normalized = normalize_telegram_id(value_stripped)
            if normalized:
                return 'telegram_id', normalized
        elif 5 <= digit_length <= 9:
            normalized = normalize_telegram_id(value_stripped)
            if normalized:
                return 'telegram_id', normalized

    # 2. Check for Facebook URL (with facebook.com) - check BEFORE Telegram username
    value_lower = value_stripped.lower()
    has_url_patterns = (
        'facebook.com' in value_lower or
        'http://' in value_lower or
        'https://' in value_lower or
        'www.' in value_lower
    )

    if has_url_patterns:
        is_valid_fb, _, fb_normalized = validate_facebook_link(value_stripped)
        if is_valid_fb:
            return 'facebook_link', fb_normalized

    # 3. Check if value contains Cyrillic characters - if yes, prioritize as fullname
    has_cyrillic = any('\u0400' <= c <= '\u04FF' for c in value_stripped)

    # 4. Check for Telegram username (letters, digits, underscores, no spaces, may start with @)
    if not has_cyrillic:
        username_candidate = value_stripped.replace('@', '').strip()
        if username_candidate and not ' ' in username_candidate:
            if len(username_candidate) >= 5 and all(c.isalnum() or c in ['_', '.', '-'] for c in username_candidate):
                is_valid_tg, _, tg_normalized = validate_telegram_name(username_candidate)
                if is_valid_tg:
                    return 'telegram_user', tg_normalized

    # 5. Check for Facebook username without URL (only if not Telegram username)
    if not has_url_patterns:
        is_valid_fb, _, fb_normalized = validate_facebook_link(value_stripped)
        if is_valid_fb:
            return 'facebook_link', fb_normalized

    # 6. Check for fullname (contains spaces or letters, not just digits)
    if ' ' in value_stripped or any(c.isalpha() for c in value_stripped):
        normalized = normalize_text_field(value_stripped)
        if normalized and len(normalized) >= 3:
            return 'fullname', normalized

    return 'unknown', value_stripped


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
                logger.warning(
                    f"Telegram API call failed (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {current_delay}s..."
                )
                await asyncio.sleep(current_delay)
                current_delay *= backoff
            else:
                logger.error(f"Telegram API call failed after {max_retries} attempts: {e}")
        except RetryAfter as e:
            wait_time = e.retry_after
            logger.warning(f"Rate limited by Telegram. Waiting {wait_time}s...")
            await asyncio.sleep(wait_time)
            if attempt < max_retries - 1:
                return await func(*args, **kwargs)
            raise

    raise last_exception

