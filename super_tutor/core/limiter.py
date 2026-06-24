"""Shared rate limiter instance — avoids circular imports between main.py and routes.

Import this module from both ``main.py`` (to attach to app.state) and route
files (to decorate endpoints) without creating import cycles.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
