import time
from functools import wraps

from bot.config import TABLE_NAME
from bot.logging import logger
from bot.utils import normalize_tag

FIELD_NAME_MAPPING = {
    'telegram_name': 'telegram_user',
}

UNIQUENESS_FIELD_LABELS = {
    'facebook_link': 'Facebook Ссылка',
    'telegram_name': 'Тег Telegram',
    'telegram_id': 'Telegram ID',
}

uniqueness_cache = {}
CACHE_TTL = 300  # 5 minutes in seconds


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

                    if any(keyword in error_msg for keyword in ['timeout', 'connection', 'network', 'temporary', '503', '502', '504']):
                        if attempt < max_retries - 1:
                            logger.warning(
                                f"Supabase query failed (attempt {attempt + 1}/{max_retries}): {e}. "
                                f"Retrying in {current_delay}s..."
                            )
                            time.sleep(current_delay)
                            current_delay *= backoff
                        else:
                            logger.error(f"Supabase query failed after {max_retries} attempts: {e}")
                    else:
                        raise

            raise last_exception
        return wrapper
    return decorator


async def check_duplicate_realtime(client, field_name: str, field_value: str) -> tuple[bool, str]:
    """Check if a field value already exists in the database (for real-time validation)"""
    if not field_value or field_value.strip() == '':
        return True, ""

    try:
        db_field_name = FIELD_NAME_MAPPING.get(field_name, field_name)
        response = client.table(TABLE_NAME).select("id, fullname").eq(db_field_name, field_value).limit(1).execute()
        if response.data and len(response.data) > 0:
            existing_lead = response.data[0]
            fullname = existing_lead.get('fullname', 'Неизвестно')
            return False, fullname
        return True, ""
    except Exception as e:
        logger.error(f"Error checking real-time duplicate for {field_name}: {e}", exc_info=True)
        return True, ""


def check_fields_uniqueness_batch(client, fields_to_check: dict) -> tuple[bool, str]:
    """
    Check uniqueness of multiple fields in a single query using OR conditions.
    Returns (is_unique, conflicting_field) where conflicting_field is empty if all unique.
    """
    if not fields_to_check:
        return True, ""

    cache_key = tuple(sorted(fields_to_check.items()))
    if cache_key in uniqueness_cache:
        cached_result, cached_time = uniqueness_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            return cached_result

    try:
        for field_name, field_value in fields_to_check.items():
            if field_value and field_value.strip():
                db_field_name = FIELD_NAME_MAPPING.get(field_name, field_name)
                response = client.table(TABLE_NAME).select("id").eq(db_field_name, field_value).limit(1).execute()
                if response.data and len(response.data) > 0:
                    result = (False, field_name)
                    uniqueness_cache[cache_key] = (result, time.time())
                    return result

        result = (True, "")
        uniqueness_cache[cache_key] = (result, time.time())
        return result

    except Exception as e:
        logger.error(f"Error checking batch uniqueness: {e}", exc_info=True)
        return False, "unknown"


def ensure_lead_identifiers_unique(
    client,
    fields_to_check: dict,
    current_lead_id: int | None = None,
) -> tuple[bool, str]:
    """
    Ensure that identifier fields (telegram/facebook) are unique.
    """
    if not fields_to_check:
        return True, ""

    if current_lead_id is None:
        return check_fields_uniqueness_batch(client, fields_to_check)

    try:
        for field_name, field_value in fields_to_check.items():
            if not field_value or not str(field_value).strip():
                continue

            db_field_name = FIELD_NAME_MAPPING.get(field_name, field_name)
            response = (
                client
                .table(TABLE_NAME)
                .select("id")
                .eq(db_field_name, field_value)
                .neq("id", current_lead_id)
                .limit(1)
                .execute()
            )
            if response.data and len(response.data) > 0:
                return False, field_name

        return True, ""
    except Exception as e:
        logger.error(f"Error ensuring identifiers uniqueness (edit flow): {e}", exc_info=True)
        return False, "unknown"


def get_unique_manager_names(client) -> list[str]:
    """Get list of unique manager_name values from database"""
    try:
        response = client.table(TABLE_NAME).select("manager_name").execute()
        if not response.data:
            return []
        unique_names = set()
        for record in response.data:
            manager_name = record.get('manager_name')
            if manager_name and manager_name.strip():
                unique_names.add(manager_name.strip())
        return sorted(list(unique_names))
    except Exception as e:
        logger.error(f"Error getting unique manager names: {e}", exc_info=True)
        return []


@retry_supabase_query(max_retries=3, delay=1, backoff=2)
def update_manager_tag_by_name(client, manager_name: str, new_tag: str) -> int:
    """Update manager_tag for all records with given manager_name"""
    try:
        normalized_tag = normalize_tag(new_tag)
        response = client.table(TABLE_NAME).update({"manager_tag": normalized_tag}).eq("manager_name", manager_name).execute()
        updated_count = len(response.data) if response.data else 0
        logger.info(
            f"[UPDATE_TAG] Updated manager_tag for manager_name '{manager_name}' to '{normalized_tag}'. "
            f"Updated {updated_count} records."
        )
        return updated_count
    except Exception as e:
        logger.error(f"Error updating manager_tag for {manager_name}: {e}", exc_info=True)
        raise


def count_records_by_manager_name(client, manager_name: str) -> int:
    """Count records with given manager_name"""
    try:
        response = client.table(TABLE_NAME).select("id").eq("manager_name", manager_name).execute()
        return len(response.data) if response.data else 0
    except Exception as e:
        logger.error(f"Error counting records for manager_name {manager_name}: {e}", exc_info=True)
        return 0


def get_manager_tag_by_name(client, manager_name: str) -> str:
    """Get manager_tag for given manager_name (first non-empty)"""
    try:
        response = (
            client.table(TABLE_NAME)
            .select("manager_tag")
            .eq("manager_name", manager_name)
            .limit(1)
            .execute()
        )
        if response.data and response.data[0].get("manager_tag"):
            return response.data[0].get("manager_tag")
        return ""
    except Exception as e:
        logger.error(f"Error getting manager_tag for {manager_name}: {e}", exc_info=True)
        return ""


@retry_supabase_query(max_retries=3, delay=1, backoff=2)
def transfer_manager_leads(client, from_manager: str, to_manager: str, to_tag: str) -> int:
    """Transfer all leads from one manager to another (update manager_name + manager_tag)"""
    normalized_tag = normalize_tag(to_tag) if to_tag else ""
    response = (
        client
        .table(TABLE_NAME)
        .update({"manager_name": to_manager, "manager_tag": normalized_tag})
        .eq("manager_name", from_manager)
        .execute()
    )
    updated_count = len(response.data) if response.data else 0
    logger.info(
        f"[TRANSFER] Updated manager_name from '{from_manager}' to '{to_manager}', "
        f"manager_tag='{normalized_tag}'. Updated {updated_count} records."
    )
    return updated_count


def check_field_uniqueness(client, field_name: str, field_value: str) -> bool:
    """Check if a field value already exists in the database (with retry and cache)"""
    if not field_value or field_value.strip() == '':
        return True

    cache_key = (field_name, field_value)
    if cache_key in uniqueness_cache:
        cached_result, cached_time = uniqueness_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            return cached_result

    @retry_supabase_query(max_retries=3, delay=1, backoff=2)
    def _execute_query():
        return client.table(TABLE_NAME).select("id").eq(field_name, field_value).limit(1).execute()

    try:
        response = _execute_query()
        is_unique = not (response.data and len(response.data) > 0)
        uniqueness_cache[cache_key] = (is_unique, time.time())
        return is_unique
    except Exception as e:
        logger.error(f"Error checking uniqueness for {field_name}: {e}", exc_info=True)
        return False


def clear_uniqueness_cache():
    uniqueness_cache.clear()

