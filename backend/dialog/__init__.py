"""Shared dialogue commands and their HTTP representation."""

from .api import create_router
from .service import Actor, DialogueError, DialogueService

__all__ = ["Actor", "DialogueError", "DialogueService", "create_router"]
