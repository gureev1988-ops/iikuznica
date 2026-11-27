"""HTTP contract for DION Bot API v2.

Vendor reference: https://faq.dion.vc/ru/users/chat/chatbots
"""

from dataclasses import dataclass, field
from html import escape
from threading import Lock
from time import monotonic
from typing import Any
from uuid import UUID

import httpx

DEFAULT_BASE_URL = "https://bots-api.dion.vc"
MAX_UPDATE_ID = 2**63 - 1
MAX_TEXT_LENGTH = 4096
_REJECTED = frozenset({400, 401, 403, 404})


class DionAPIError(RuntimeError):
    """A provider or protocol error without credentials or message content."""

    def __init__(self, operation: str, status_code: int | None = None):
        self.status_code = status_code
        description = f"HTTP {status_code}" if status_code is not None else "invalid or missing response"
        super().__init__(f"DION {operation}: {description}")


class DionRejected(DionAPIError):
    """The provider rejected the request before accepting a message."""


@dataclass(frozen=True)
class DionConfig:
    base_url: str
    email: str
    password: str = field(repr=False)


def as_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def update_id(value: Any) -> int:
    if isinstance(value, str) and len(value) <= 19 and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_UPDATE_ID:
        raise DionAPIError("getUpdates update_id")
    return value


class DionClient:
    def __init__(self, config: DionConfig, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._http = httpx.Client(base_url=config.base_url, transport=transport, follow_redirects=False)
        self._token: str | None = None
        self._bot_id: UUID | None = None
        self._refresh_at = 0.0
        self._lock = Lock()

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.RequestError:
            raise DionAPIError(path) from None

    def _json(self, response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            value = response.json()
        except (ValueError, UnicodeError):
            raise DionAPIError(operation) from None
        if not isinstance(value, dict):
            raise DionAPIError(operation)
        return value

    def _authorize(self, *, expired_token: str | None = None) -> tuple[str, UUID]:
        with self._lock:
            if (
                self._token is not None
                and self._bot_id is not None
                and self._token != expired_token
                and monotonic() < self._refresh_at
            ):
                return self._token, self._bot_id
            try:
                response = self._request(
                    "POST",
                    "/platform/v1/token",
                    json={"email": self.config.email, "password": self.config.password},
                )
                if response.status_code != 200:
                    raise DionRejected("authorization", response.status_code)
                payload = self._json(response, "authorization")
            except DionAPIError as exc:
                raise DionRejected("authorization", exc.status_code) from None
            token = payload.get("access_token")
            bot_id = as_uuid(as_object(payload.get("user")).get("id"))
            if not isinstance(token, str) or not token or bot_id is None:
                raise DionRejected("authorization")
            if self._bot_id is not None and bot_id != self._bot_id:
                raise DionRejected("authorization account changed")
            self._token = token
            self._bot_id = bot_id
            # The documented lifetime is 12 hours. Leave margin for polling and clock skew.
            self._refresh_at = monotonic() + 11 * 60 * 60 + 55 * 60
            return token, bot_id

    @property
    def bot_id(self) -> UUID:
        return self._authorize()[1]

    def _bot_request(
        self, method: str, operation: str, *, timeout: float = 10, **kwargs: Any
    ) -> Any:
        path = f"/chats/v2/{operation}"
        token, _ = self._authorize()
        response = self._request(
            method, path, headers={"Authorization": f"Bearer {token}"}, timeout=timeout, **kwargs
        )
        if response.status_code == 401:
            token, _ = self._authorize(expired_token=token)
            response = self._request(
                method, path, headers={"Authorization": f"Bearer {token}"}, timeout=timeout, **kwargs
            )
        if response.status_code in _REJECTED:
            raise DionRejected(operation, response.status_code)
        if not response.is_success:
            raise DionAPIError(operation, response.status_code)

        payload = self._json(response, operation)
        error_code = payload.get("error_code")
        if payload.get("ok") is False and type(error_code) is int and error_code in _REJECTED:
            raise DionRejected(operation, error_code)
        if payload.get("ok") is not True or "result" not in payload:
            raise DionAPIError(operation)
        return payload["result"]

    def get_updates(
        self, *, offset: int | None = None, limit: int = 100, timeout: int = 30
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100 or not 0 <= timeout <= 50:
            raise ValueError("DION limit must be 1–100 and timeout 0–50 seconds")
        params = {"limit": str(limit), "timeout": str(timeout)}
        if offset is not None:
            params["offset"] = str(update_id(offset))
        result = self._bot_request("GET", "getUpdates", params=params, timeout=timeout + 10)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise DionAPIError("getUpdates")
        for item in result:
            update_id(item.get("update_id"))
        return result

    def send_message(
        self, *, chat_id: UUID, text: str, reply_to: UUID | None = None
    ) -> str:
        encoded = escape(text)
        if not 1 <= len(encoded) <= MAX_TEXT_LENGTH:
            raise DionRejected("sendMessage text length")
        body: dict[str, Any] = {"chat_id": str(chat_id), "text": encoded, "parse_mode": "HTML"}
        if reply_to is not None:
            body["reply_parameters"] = {"message_id": str(reply_to)}
        result = as_object(self._bot_request("POST", "sendMessage", json=body))
        intermediate_id = as_uuid(result.get("intermediate_id") or result.get("message_id"))
        if intermediate_id is None:
            raise DionAPIError("sendMessage message_id")
        return str(intermediate_id)
