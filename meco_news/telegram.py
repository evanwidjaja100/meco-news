"""Telegram API client, error classification, and bounded HTML rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import html
import json
import socket
from typing import Any
from collections.abc import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import NewsItem
from .timezones import get_timezone


TELEGRAM_MAX_UNITS = 4_096
DEFAULT_MESSAGE_UNITS = 3_900
DEFAULT_MESSAGE_BYTES = 15_600
_CONTROL_TRANSLATION = dict.fromkeys(range(0, 32), " ")
_CONTROL_TRANSLATION.update(
    {
        127: " ",
        0x2028: " ",
        0x2029: " ",
        0x200E: "",
        0x200F: "",
        0x202A: "",
        0x202B: "",
        0x202C: "",
        0x202D: "",
        0x202E: "",
        0x2066: "",
        0x2067: "",
        0x2068: "",
        0x2069: "",
    }
)


class TelegramSendError(RuntimeError):
    """A Telegram request has a stable, durable delivery outcome."""

    def __init__(self, reason_code: str, message: str, *, retry_after: int = 0) -> None:
        self.reason_code = reason_code
        self.retry_after = max(0, int(retry_after))
        super().__init__(message)

    @property
    def outcome(self) -> str:
        if self.reason_code in {"telegram_ambiguous", "telegram_malformed_response"}:
            return "ambiguous"
        if self.reason_code in {"telegram_rate_limited", "telegram_retryable"}:
            return "rejected_retryable"
        return "rejected_terminal"


@dataclass(frozen=True, slots=True)
class DigestBuildResult:
    messages: list[str]
    included_items: list[NewsItem]
    omitted_items: list[tuple[NewsItem, str]] = field(default_factory=list)
    item_chunk_indexes: dict[str, int] = field(default_factory=dict)


class TelegramClient:
    def __init__(self, token: str, chat_id: str = "", timeout: int = 25):
        if not token or _looks_placeholder(token):
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.timeout = max(1, min(int(timeout), 120))

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "MecoMarketWatch/2.0"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read(128 * 1024)
        except HTTPError as exc:
            retry_after = 0
            try:
                parsed = json.loads(exc.read(64 * 1024).decode("utf-8", errors="replace"))
                candidate_parameters = parsed.get("parameters") if isinstance(parsed, dict) else None
                error_parameters = candidate_parameters if isinstance(candidate_parameters, dict) else {}
                retry_after = int(error_parameters.get("retry_after", 0) or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            if exc.code == 429:
                raise TelegramSendError("telegram_rate_limited", "Telegram rate limited the request", retry_after=retry_after) from exc
            if 500 <= exc.code <= 599:
                # C3.1: raw HTTP 5xx after possible transmission is ambiguous — must not auto-retry
                raise TelegramSendError("telegram_ambiguous", f"Telegram acceptance is unknown after HTTP {exc.code}") from exc
            raise TelegramSendError("telegram_terminal", f"Telegram rejected the request with HTTP {exc.code}") from exc
        except (TimeoutError, ConnectionResetError) as exc:
            raise TelegramSendError("telegram_ambiguous", "Telegram acceptance is unknown after a transport timeout/reset") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, socket.timeout | TimeoutError | ConnectionResetError):
                raise TelegramSendError("telegram_ambiguous", "Telegram acceptance is unknown after a transport failure") from exc
            # C3.1: broad transport errors without proven pre-transmission are ambiguous
            raise TelegramSendError("telegram_ambiguous", "Telegram acceptance is unknown after a transport failure") from exc
        except OSError as exc:
            raise TelegramSendError("telegram_ambiguous", "Telegram acceptance is unknown after an OS transport failure") from exc
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramSendError("telegram_malformed_response", "Telegram response was not valid JSON") from exc
        if not isinstance(result, dict):
            raise TelegramSendError("telegram_malformed_response", "Telegram response shape was invalid")
        if not result.get("ok"):
            code = result.get("error_code")
            candidate_response_parameters = result.get("parameters")
            response_parameters = candidate_response_parameters if isinstance(candidate_response_parameters, dict) else {}
            retry_after = int(response_parameters.get("retry_after", 0) or 0)
            if code == 429:
                raise TelegramSendError("telegram_rate_limited", "Telegram rate limited the request", retry_after=retry_after)
            if isinstance(code, int) and code >= 500:
                # C3.1: explicit Telegram envelope 5xx is still ambiguous without proof of non-acceptance
                raise TelegramSendError("telegram_ambiguous", "Telegram acceptance is unknown after a retryable error")
            raise TelegramSendError("telegram_terminal", "Telegram rejected the request")
        return result

    def get_me(self) -> dict[str, Any]:
        result = self._call("getMe").get("result")
        if not isinstance(result, dict):
            raise TelegramSendError("telegram_malformed_response", "Telegram getMe response was invalid")
        return result

    def discover_chats(self) -> list[dict[str, Any]]:
        updates = self._call("getUpdates", {"timeout": 0, "allowed_updates": ["message", "channel_post"]}).get("result", [])
        if not isinstance(updates, list):
            raise TelegramSendError("telegram_malformed_response", "Telegram updates response was invalid")
        chats: dict[str, dict[str, Any]] = {}
        for update in updates:
            if not isinstance(update, dict):
                continue
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") if isinstance(message, dict) else None
            if isinstance(chat, dict) and "id" in chat:
                chats[str(chat["id"])] = {key: chat[key] for key in ("id", "type", "title", "username", "first_name") if key in chat}
        return list(chats.values())

    def send_html(self, text: str) -> str:
        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not configured")
        validate_message(text)
        result = self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )
        message = result.get("result")
        if not isinstance(message, dict) or "message_id" not in message:
            raise TelegramSendError("telegram_malformed_response", "Telegram did not return a message id")
        return str(message["message_id"])


def _looks_placeholder(value: str) -> bool:
    lowered = value.casefold()
    return lowered.startswith("replace_with_") or "your_token" in lowered or lowered in {"changeme", "token"}


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _clean_display(value: object, maximum: int) -> str:
    text = str(value or "").translate(_CONTROL_TRANSLATION)
    text = " ".join(text.split())
    if len(text) <= maximum:
        return text
    return text[: max(0, maximum - 3)].rstrip() + "..."


def _truncate(value: str, length: int) -> str:
    return _clean_display(value, length)


def _timezone_label(timezone_name: str, tz: Any) -> str:
    label = tz.tzname(datetime.now(tz)) or timezone_name.rsplit("/", 1)[-1]
    return _clean_display(label, 32)


def _article_block(
    index: int,
    item: NewsItem,
    tz: Any,
    *,
    include_summary: bool = True,
    title_length: int = 260,
    source_length: int = 160,
) -> str:
    label = _timezone_label(getattr(tz, "key", "UTC"), tz)
    published = "Publication time unavailable"
    if item.published_at:
        published = item.published_at.astimezone(tz).strftime(f"%d %b %Y, %H:%M {label}")
    title = _clean_display(item.title, title_length)
    source = _clean_display(item.source, source_length)
    summary = _clean_display(item.summary, 220)
    if include_summary and summary and summary.casefold() not in title.casefold() and title.casefold() not in summary.casefold():
        summary_line = f"\n{html.escape(summary)}"
    else:
        summary_line = ""
    return (
        f"<b>{index}. {html.escape(title)}</b>\n"
        f"{html.escape(source)} &middot; {html.escape(published)}\n"
        f"<b>Watch lane:</b> {html.escape(_clean_display(item.topic_label, 256))}\n"
        f"<b>Why it matters:</b> {html.escape(_clean_display(item.relevance_reason, 512))}"
        f"{summary_line}\n"
        f'<a href="{html.escape(_clean_display(item.url, 2048), quote=True)}">Read at source</a>'
    )


def _fit_block(index: int, item: NewsItem, tz: Any, max_units: int, max_bytes: int) -> tuple[str | None, str]:
    variants = [
        (True, 260, 160, ""),
        (False, 260, 160, "summary_omitted"),
        (False, 220, 96, "source_shortened"),
        (False, 160, 72, "title_shortened"),
        (False, 100, 48, "title_compacted"),
    ]
    last_reason = "message_block_too_large"
    for include_summary, title_length, source_length, reason in variants:
        block = _article_block(index, item, tz, include_summary=include_summary, title_length=title_length, source_length=source_length)
        if utf16_units(block) <= max_units and len(block.encode("utf-8")) <= max_bytes:
            return block, reason
        last_reason = reason or last_reason
    return None, last_reason


def validate_message(text: str, *, max_units: int = DEFAULT_MESSAGE_UNITS, max_bytes: int = DEFAULT_MESSAGE_BYTES) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Telegram message must be nonempty")
    if utf16_units(text) > min(max_units, TELEGRAM_MAX_UNITS - 1):
        raise ValueError("Telegram message exceeds UTF-16 unit limit")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("Telegram message exceeds raw HTML byte limit")


def build_digest(
    items: Iterable[NewsItem],
    company: str,
    timezone_name: str,
    issues: list[str] | None = None,
    *,
    max_length: int = DEFAULT_MESSAGE_UNITS,
    max_bytes: int = DEFAULT_MESSAGE_BYTES,
    minimum_count: int = 5,
    coverage_notice: str = "",
    delivery_id: int | None = None,
    delivery_date: str | None = None,
) -> DigestBuildResult:
    item_list = list(items)
    tz = get_timezone(timezone_name)
    if delivery_date:
        try:
            date_label = datetime.fromisoformat(delivery_date).date().strftime("%d %B %Y")
        except ValueError:
            date_label = datetime.now(tz).strftime("%d %B %Y")
    else:
        date_label = datetime.now(tz).strftime("%d %B %Y")
    safe_company = _clean_display(company, 160)
    delivery_line = f"\nDelivery {delivery_id}" if delivery_id is not None else ""
    header = f"<b>{html.escape(safe_company)} - Daily Market Watch</b>\n{date_label} - {len(item_list)} selected stories{delivery_line}\n\n"
    if len(item_list) < minimum_count:
        header += f"Warning: fewer than {minimum_count} unsent relevant stories passed the quality floor today.\n\n"
    if coverage_notice:
        header += f"Coverage: {html.escape(_clean_display(coverage_notice, 500))}\n\n"
    if utf16_units(header) > max_length or len(header.encode("utf-8")) > max_bytes:
        header = _clean_display(header, max(32, max_length - 8))
    validate_message(header, max_units=max_length, max_bytes=max_bytes)

    messages: list[str] = []
    current = header
    current_chunk = 0
    included: list[NewsItem] = []
    omitted: list[tuple[NewsItem, str]] = []
    item_chunks: dict[str, int] = {}
    for index, item in enumerate(item_list, 1):
        block, reason = _fit_block(index, item, tz, max_length, max_bytes)
        if block is None:
            omitted.append((item, reason))
            continue
        separator = "\n\n"
        if utf16_units(current + separator + block) > max_length or len((current + separator + block).encode("utf-8")) > max_bytes:
            validate_message(current, max_units=max_length, max_bytes=max_bytes)
            messages.append(current)
            current_chunk += 1
            continuation_id = f" #{delivery_id}/{current_chunk + 1}" if delivery_id is not None else ""
            current = f"<b>Daily Market Watch (continued){continuation_id}</b>\n\n" + block
        else:
            current += separator + block
        included.append(item)
        item_chunks[item.fingerprint] = current_chunk
    messages.append(current)

    notes: list[str] = []
    if issues:
        notes.append(f"{len(issues)} source(s) were temporarily unavailable.")
    if omitted:
        notes.append(f"{len(omitted)} oversized item(s) were omitted from delivery.")
    if notes:
        note = "\n\n<i>Coverage note: " + html.escape(" ".join(notes)) + "</i>"
        if utf16_units(messages[-1] + note) <= max_length and len((messages[-1] + note).encode("utf-8")) <= max_bytes:
            messages[-1] += note
        else:
            messages.append("<i>Coverage note: " + html.escape(" ".join(notes)) + "</i>")
            current_chunk = len(messages) - 1
    for message in messages:
        validate_message(message, max_units=max_length, max_bytes=max_bytes)
    return DigestBuildResult(messages, included, omitted, item_chunks)


def build_digest_messages(
    items: Iterable[NewsItem],
    company: str,
    timezone_name: str,
    issues: list[str] | None = None,
    max_length: int = DEFAULT_MESSAGE_UNITS,
    minimum_count: int = 5,
) -> list[str]:
    return build_digest(items, company, timezone_name, issues, max_length=max_length, minimum_count=minimum_count).messages
