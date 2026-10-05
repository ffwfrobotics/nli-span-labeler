"""The shared rate limiter (slowapi), keyed by the proxy-aware client address."""

from slowapi import Limiter

from . import config
from .auth import client_ip

limiter = Limiter(
    key_func=client_ip,
    enabled=config.RATE_LIMIT_ENABLED,
    default_limits=[config.RATE_LIMIT_DEFAULT],
)
