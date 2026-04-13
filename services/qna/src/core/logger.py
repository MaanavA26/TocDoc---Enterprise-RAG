import logging

# ---------------------------------------------------------------------------
# Global logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("qna.log"),  # Logs persisted to local file
        logging.StreamHandler(),        # Logs also emitted to console/stdout
    ],
)

# Module-level logger (use `logger` throughout your code for consistency)
logger = logging.getLogger(__name__)