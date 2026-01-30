from supabase import create_client, Client

from bot.config import SUPABASE_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
from bot.logging import logger


supabase: Client | None = None
supabase_storage: Client | None = None


def get_supabase_client() -> Client | None:
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


def get_supabase_storage_client() -> Client | None:
    """Initialize and return Supabase Storage client (uses service_role key)"""
    global supabase_storage
    if supabase_storage is None:
        try:
            if not SUPABASE_SERVICE_ROLE_KEY:
                logger.error("SUPABASE_SERVICE_ROLE_KEY not found in environment variables")
                return get_supabase_client()
            if not SUPABASE_URL:
                logger.error("SUPABASE_URL not found in environment variables")
                return None
            supabase_storage = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
            logger.info("[STORAGE] Initialized Supabase Storage client with service_role key")
        except Exception as e:
            logger.error(f"Error initializing Supabase Storage client: {e}", exc_info=True)
            return get_supabase_client()
    return supabase_storage

