"""Structured, source-addressable evidence used by research retrievers.

The core project historically passed loosely shaped search-result dictionaries
through the research pipeline.  ``EvidenceItem`` provides a small, dependency-
free interchange type without changing that public search-result interface.
Only HTTP(S) sources are admitted so source counts cannot be inflated by
synthetic URLs such as ``codex-search://local``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


def canonical_http_url(url: str) -> str | None:
    """Return a stable HTTP(S) URL, or ``None`` for non-web sources."""

    if not isinstance(url, str):
        return None
    # Search snippets and generated prose commonly leave sentence punctuation
    # attached to a URL. Treat it as prose punctuation so equivalent evidence
    # does not become a distinct (and usually broken) source.
    value = url.strip().rstrip(".,;")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None

    filtered_query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
        and not key.lower().startswith(_TRACKING_QUERY_PREFIXES)
    ]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            urlencode(sorted(filtered_query)),
            "",
        )
    )


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One claim tied to one real web source."""

    claim: str
    source_url: str
    source_title: str = ""
    retriever: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    summary: str = ""
    value: str | int | float | None = None
    unit: str | None = None
    as_of_date: str | None = None
    checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.claim, str) or not self.claim.strip():
            raise ValueError("EvidenceItem claim must be a non-empty string")
        if isinstance(self.value, bool) or not isinstance(
            self.value,
            (str, int, float, type(None)),
        ):
            raise ValueError("EvidenceItem value must be a string, number, or null")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("EvidenceItem numeric value must be finite")
        canonical_url = canonical_http_url(self.source_url)
        if canonical_url is None:
            raise ValueError("EvidenceItem source_url must be an absolute HTTP(S) URL")
        object.__setattr__(self, "source_url", canonical_url)
        payload = {
            "claim": self.claim.strip(),
            "source_url": canonical_url,
            "value": self.value,
            "unit": self.unit,
            "as_of_date": self.as_of_date,
            "summary": self.summary.strip(),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "checksum", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "value": self.value,
            "unit": self.unit,
            "as_of_date": self.as_of_date,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "retriever": self.retriever,
            "retrieved_at": self.retrieved_at,
            "summary": self.summary,
            "checksum": self.checksum,
        }

    @classmethod
    def from_search_result(
        cls,
        result: dict[str, Any],
        *,
        retriever: str = "",
        claim: str | None = None,
    ) -> "EvidenceItem | None":
        """Best-effort conversion from a legacy search result."""

        url = result.get("href") or result.get("url") or ""
        if canonical_http_url(url) is None:
            return None
        body = str(result.get("body") or result.get("raw_content") or "").strip()
        if not body:
            return None
        return cls(
            claim=(claim or body).strip(),
            source_url=url,
            source_title=str(result.get("title") or ""),
            retriever=retriever or str(result.get("retriever") or ""),
            summary=body,
        )


def deduplicate_evidence(items: Iterable[EvidenceItem]) -> list[EvidenceItem]:
    """Deduplicate identical claims while preserving input order."""

    deduplicated: list[EvidenceItem] = []
    seen: set[str] = set()
    for item in items:
        if item.checksum in seen:
            continue
        seen.add(item.checksum)
        deduplicated.append(item)
    return deduplicated


def unique_http_sources(items: Iterable[EvidenceItem]) -> list[str]:
    """Return canonical, unique source URLs in first-seen order."""

    urls: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item.source_url in seen:
            continue
        seen.add(item.source_url)
        urls.append(item.source_url)
    return urls
