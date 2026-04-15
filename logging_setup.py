"""
logging_setup.py
----------------
Configures the application-wide logger.  Import ``log`` from here in every
other module so that all components share the same handler configuration.
"""

import logging
from pathlib import Path

from config import DEBUG_LOGGING

_log_path    = Path(__file__).parent / "claude_usage_widget.log"
_file_level  = logging.DEBUG if DEBUG_LOGGING else logging.INFO
_cons_level  = logging.DEBUG if DEBUG_LOGGING else logging.WARNING

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(funcName)s: %(message)s",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger().handlers[0].setLevel(_file_level)   # file: DEBUG or INFO
logging.getLogger().handlers[1].setLevel(_cons_level)   # console: DEBUG or WARNING

log = logging.getLogger("claude_usage_widget")
