"""
Composer package.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)
env: dict[str, str] = dict(os.environ)

__all__ = ["__version__", "env", "log"]

__version__ = "0.1.0"
