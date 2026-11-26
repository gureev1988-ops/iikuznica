"""DION Bot API v2 adapter."""

from .client import DEFAULT_BASE_URL, DionAPIError, DionClient, DionConfig

__all__ = ["DEFAULT_BASE_URL", "DionAPIError", "DionAdapter", "DionClient", "DionConfig", "queue_reply"]


def __getattr__(name: str):
    if name in ("DionAdapter", "queue_reply"):
        from .adapter import DionAdapter, queue_reply

        return {"DionAdapter": DionAdapter, "queue_reply": queue_reply}[name]
    raise AttributeError(name)
