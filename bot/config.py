import os


# Set environment variables to disable proxy before importing supabase/httpx
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("HTTPX_NO_PROXY", "1")


# Environment variables - all required except PORT (set by Koyeb)
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
# Service role key for Storage operations (bypasses RLS)
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
TABLE_NAME = os.environ.get('TABLE_NAME', 'facebook_leads')
PORT = int(os.environ.get('PORT', 8000))

# Photo upload configuration
SUPABASE_LEADS_BUCKET = os.environ.get('SUPABASE_LEADS_BUCKET', 'Leads')
ENABLE_LEAD_PHOTOS = os.environ.get('ENABLE_LEAD_PHOTOS', 'true').lower() == 'true'

# Facebook flow configuration
FACEBOOK_FLOW_ENABLED = os.environ.get('FACEBOOK_FLOW', 'OFF').upper() == 'ON'

# Minimal add mode configuration
MINIMAL_ADD_MODE_ENABLED = os.environ.get('MINIMAL_ADD_MODE', 'OFF').upper() == 'ON'

# PIN code configuration - REQUIRED environment variable (no default for security)
PIN_CODE = os.environ.get('PIN_CODE')

# Cleanup configuration
CLEANUP_INTERVAL_MINUTES = int(os.environ.get('CLEANUP_INTERVAL_MINUTES', 10))

# Rate limiting configuration
RATE_LIMIT_ENABLED = os.environ.get('RATE_LIMIT_ENABLED', 'true').lower() == 'true'
RATE_LIMIT_REQUESTS = int(os.environ.get('RATE_LIMIT_REQUESTS', 30))
RATE_LIMIT_WINDOW = int(os.environ.get('RATE_LIMIT_WINDOW', 60))


def is_facebook_flow_enabled() -> bool:
    """Check if Facebook flow is enabled via environment variable."""
    return FACEBOOK_FLOW_ENABLED


def is_minimal_add_mode_enabled() -> bool:
    """Check if minimal add mode is enabled via environment variable."""
    return MINIMAL_ADD_MODE_ENABLED

