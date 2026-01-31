import logging
from datetime import datetime


class TenthSecondFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created)
        base = dt.strftime('%Y-%m-%d %H:%M:%S')
        tenths = int(record.msecs / 100)
        return f"{base},{tenths}"


handler = logging.StreamHandler()
handler.setFormatter(
    TenthSecondFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
)

logging.basicConfig(
    level=logging.WARNING,
    handlers=[handler]
)

logger = logging.getLogger(__name__)

