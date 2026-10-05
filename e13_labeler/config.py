"""
Runtime configuration, read from environment variables.

Grown out of the configuration block at the top of the old ``app.py``; the
settings for features that no longer exist (tokenizer, consensus pools,
calibration routing, training mode) are gone.

Environment variables:
    E13_DB=path                  SQLite database (default: outputs/e13_labeler/e13.db)
    SINGLE_USER=1                One owner account, auto-login, loopback only (FR-54)
    LOCK_TIMEOUT_MINUTES=20      How long an item lock lasts (FR-32)
    SESSION_EXPIRY_DAYS=30       Session lifetime
    COOKIE_SECURE=1              Mark the session cookie Secure (set behind HTTPS)
    TRUSTED_PROXIES=ip,ip        Proxies whose X-Forwarded-For / X-Real-IP are believed

Rate limiting:
    RATE_LIMIT_ENABLED=1         Enable/disable rate limiting (default: enabled)
    RATE_LIMIT_AUTH=5/minute     Limit for login
    RATE_LIMIT_DEFAULT=120/minute  Limit for everything else

CORS & logging:
    CORS_ORIGINS=https://a,https://b  Allowed origins (default: same-origin only)
    CORS_ALLOW_CREDENTIALS=1     Allow credentials in CORS requests (default: 1)
    REQUEST_LOG_LEVEL=INFO       Logging level for request logs
    REQUEST_LOG_FILE=path        Optional file for request logs (default: stdout only)
"""

import logging
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent.resolve()
REPO_DIR = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"
OUTPUTS_DIR = REPO_DIR / "outputs" / "e13_labeler"


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) == "1"


def db_path() -> Path:
    """Database path. Read on every call so tests can point E13_DB elsewhere."""
    return Path(os.environ.get("E13_DB", OUTPUTS_DIR / "e13.db"))


def single_user() -> bool:
    return _flag("SINGLE_USER")


SESSION_EXPIRY_DAYS = int(os.environ.get("SESSION_EXPIRY_DAYS", "30"))
LOCK_TIMEOUT_MINUTES = int(os.environ.get("LOCK_TIMEOUT_MINUTES", "20"))
COOKIE_SECURE = _flag("COOKIE_SECURE")
SESSION_COOKIE = "e13_session"
TRUSTED_PROXIES = {
    ip.strip() for ip in os.environ.get("TRUSTED_PROXIES", "").split(",") if ip.strip()
}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Rate limiting configuration
RATE_LIMIT_ENABLED = _flag("RATE_LIMIT_ENABLED", "1")
RATE_LIMIT_AUTH = os.environ.get("RATE_LIMIT_AUTH", "5/minute")
RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "120/minute")

# CORS configuration - comma-separated list of allowed origins (default: same-origin only)
CORS_ORIGINS = [
    origin.strip() for origin in os.environ.get("CORS_ORIGINS", "").split(",") if origin.strip()
]
CORS_ALLOW_CREDENTIALS = _flag("CORS_ALLOW_CREDENTIALS", "1")

# Request logging configuration
REQUEST_LOG_LEVEL = os.environ.get("REQUEST_LOG_LEVEL", "INFO").upper()
REQUEST_LOG_FILE = os.environ.get("REQUEST_LOG_FILE", "")

request_logger = logging.getLogger("e13.requests")
request_logger.setLevel(getattr(logging, REQUEST_LOG_LEVEL, logging.INFO))
if not request_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(handler)
    if REQUEST_LOG_FILE:
        file_handler = logging.FileHandler(REQUEST_LOG_FILE)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        request_logger.addHandler(file_handler)
