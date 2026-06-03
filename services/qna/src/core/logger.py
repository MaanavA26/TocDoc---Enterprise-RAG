import logging
import os

# ---------------------------------------------------------------------------
# Global logging configuration
# ---------------------------------------------------------------------------
# Stdout always; file logging only if LOG_FILE env var is set (local dev)
_log_handlers = [logging.StreamHandler()]
_log_file = os.getenv("LOG_FILE")  # Not set in containers; set locally if desired
if _log_file:
    _log_handlers.append(logging.FileHandler(_log_file))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=_log_handlers,
)

# Module-level logger (use `logger` throughout your code for consistency)
logger = logging.getLogger(__name__)
