"""
Define errors reported at the compiler command boundary.
"""

from __future__ import annotations


class CompilationError(ValueError):
    """
    Report an invalid or ambiguous compilation input.
    """
