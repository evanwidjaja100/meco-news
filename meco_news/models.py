from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}


def canonical_url(url: str) -> str:
    """Remove common tracking noise while preserving an article's identity."""
    try:
        parts = urlsplit(url.strip())
        hostname = (parts.hostname or parts.netloc).casefold().rstrip(".")
        try:
            hostname = unicodedata.normalize("NFKC", hostname).encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            hostname = hostname.casefold()
        port = parts.port
        netloc = hostname
        if ":" in hostname and not hostname.startswith("["):
            netloc = f"[{hostname}]"
        if port and not ((parts.scheme.casefold() == "https" and port == 443) or (parts.scheme.casefold() == "http" and port == 80)):
            netloc = f"{netloc}:{port}"
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
        ]
        query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))
    except ValueError:
        return url.strip()


def normalized_title(title: str, source: str = "") -> str:
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

    @property
    def url_key(self) -> str:
        return sha256(canonical_url(self.url).encode("utf-8")).hexdigest()

    @property
    def title_key(self) -> str:
        return sha256(normalized_title(self.title, self.source).encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        # A title key catches the same syndicated story arriving through two feeds.
        return self.title_key

    @property
    def is_quarantined(self) -> bool:
        return bool(self.quarantine_reason)
