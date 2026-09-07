from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import re
import unicodedata
from .urls import canonical_url


def _sanitize_scalar(value: object) -> tuple[str, bool]:
    """Normalize text to Unicode scalars without invoking hostile ``__str__``."""

    if not isinstance(value, str):
        return "", True
    invalid = any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    return "".join("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value), invalid


def normalized_title(title: str, source: str = "") -> str:
    title, _ = _sanitize_scalar(title)
    source, _ = _sanitize_scalar(source)
    value = unicodedata.normalize("NFKC", title).casefold().strip()
    if source:
        suffix = f" - {source.casefold().strip()}"
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


@dataclass(slots=True)
class NewsItem:
    title: str
    url: str
    source: str
    source_url: str = ""
    published_at: datetime | None = None
    summary: str = ""
    collector: str = ""
    query_name: str = ""
    score: int = 0
    topic: str = ""
    topic_label: str = ""
    relevance_reason: str = ""
    matches: list[str] = field(default_factory=list)
    source_id: str = ""
    source_host: str = ""
    freshness_reason: str = ""
    quarantine_reason: str = ""

    def __post_init__(self) -> None:
        # C4.1: scalar validation at the model boundary.  Do not stringify
        # mappings/objects here: their representation can be attacker-sized
        # or raise while the model is being built.
        invalid = False
        for field_name in ("title", "url", "source", "source_url", "summary", "collector", "query_name", "topic", "topic_label", "relevance_reason", "source_id", "source_host"):
            clean, field_invalid = _sanitize_scalar(getattr(self, field_name))
            setattr(self, field_name, clean)
            invalid = invalid or field_invalid
        clean_matches: list[str] = []
        for match in self.matches if isinstance(self.matches, list) else []:
            clean_match, match_invalid = _sanitize_scalar(match)
            if not match_invalid:
                clean_matches.append(clean_match)
            invalid = invalid or match_invalid
        self.matches = clean_matches
        if invalid and not self.quarantine_reason:
            self.quarantine_reason = "invalid_unicode_scalar"

    @property
    def url_key(self) -> str:
        return sha256(canonical_url(self.url).encode("utf-8")).hexdigest()

    @property
    def title_key(self) -> str:
        # title-v2 is deliberately source-independent.  The optional source
        # argument on normalized_title remains only for compatibility with
        # callers that want to strip a publisher suffix explicitly.
        normalized = normalized_title(self.title)
        return sha256(b"meco-news:title-v2\0" + normalized.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        # A title key catches the same syndicated story arriving through two feeds.
        return self.title_key

    @property
    def is_quarantined(self) -> bool:
        return bool(self.quarantine_reason)
