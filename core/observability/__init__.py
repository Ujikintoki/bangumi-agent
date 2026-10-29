"""Neutral local observation interface. Importing this module performs no I/O."""
from .runtime import Runtime, current, span

__all__ = ['Runtime', 'current', 'span']
