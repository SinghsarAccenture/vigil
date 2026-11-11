"""Scraper framework shared by every source.

A Source subclass implements three steps:
  fetch()      download the raw pages (all of them, following pagination)
  parse()      turn one page into raw row dicts, exactly as seen
  normalise()  turn one raw row into a record of the standard shape

Source.scrape() runs the three steps, removes duplicates and counts fallback
IDs. It only returns data; saving is the caller's job, so a run can be checked
(e.g. for a sharp row-count drop) before anything is written.
"""

from __future__ import annotations

import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

import requests

FALLBACK_PREFIX = "fallback:"

# Keys every normalise() must return. link_keys (stable forms of the links,
# used only for change detection) and group_key may be None.
RECORD_KEYS = frozenset({
    "external_id", "group_key", "issue_date", "title", "division", "status",
    "links", "link_keys", "file_numbers", "file_numbers_raw", "reference_numbers",
})

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class ScrapeError(RuntimeError):
    """A scrape could not be completed; the run must be marked failed."""


def make_fallback_id(*parts: Any) -> str:
    """Stable ID for a row with no natural identifier, clearly prefixed."""
    key = "|".join(" ".join(str(p).split()) for p in parts)
    return FALLBACK_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class HttpClient:
    """Polite HTTP GET: fixed User-Agent, timeouts, rate limit, retries with backoff.

    Retries connection errors, timeouts and HTTP 429/5xx, waiting
    backoff_base ** attempt seconds (2, 4, 8 ... by default), or the server's
    Retry-After if it gives one. Any other error status fails immediately.
    """

    user_agent: str
    min_interval: float = 1.0
    timeout: float = 30.0
    max_attempts: int = 4
    backoff_base: float = 2.0
    session: Any = field(default_factory=requests.Session)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    requests_made: int = 0
    _last_request_at: float | None = None

    def __post_init__(self) -> None:
        if not self.user_agent or not self.user_agent.strip():
            raise ScrapeError("User-Agent is empty; set SEC_USER_AGENT in .env")

    def get(self, url: str, params: Mapping[str, Any] | None = None) -> str:
        last_problem = ""
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_turn()
            wait = self.backoff_base ** attempt
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout,
                                        headers={"User-Agent": self.user_agent,
                                                 "Accept-Language": "en-US,en;q=0.9"})
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_problem = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code not in RETRY_STATUSES:
                    raise ScrapeError(f"HTTP {resp.status_code} from {resp.url} (not retryable)")
                last_problem = f"HTTP {resp.status_code} from {resp.url}"
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = max(wait, float(retry_after))
            if attempt < self.max_attempts:
                self.sleep(wait)
        raise ScrapeError(f"gave up on {url} {dict(params or {})} after {self.max_attempts} attempts: {last_problem}")

    def _wait_for_turn(self) -> None:
        now = self.clock()
        if self._last_request_at is not None:
            gap = self.min_interval - (now - self._last_request_at)
            if gap > 0:
                self.sleep(gap)
                now = self.clock()
        self._last_request_at = now
        self.requests_made += 1


@dataclass
class Page:
    """One downloaded page."""
    number: int
    url: str
    text: str


@dataclass
class ScrapeResult:
    """Everything one scrape produced, before anything is saved."""
    source_id: str
    pages: int
    raw_rows: int
    records: list[tuple[dict[str, Any], dict[str, Any]]]  # (normalised record, raw row)
    duplicates: int
    fallback_ids: int
    warnings: list[str]


class Source(ABC):
    """Base class for one regulator feed."""

    source_id: str

    def __init__(self, http: HttpClient, base_url: str, max_pages: int | None = None):
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        self.http = http
        self.base_url = base_url
        self.max_pages = max_pages

    @abstractmethod
    def fetch(self) -> Iterator[Page]:
        """Yield every page of the listing, honouring max_pages."""

    @abstractmethod
    def parse(self, page: Page) -> list[dict[str, Any]]:
        """Return the raw rows on one page, exactly as seen."""

    @abstractmethod
    def normalise(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Return one record with exactly the RECORD_KEYS."""

    def scrape(self) -> ScrapeResult:
        """Fetch, parse and normalise everything; drop repeated rows.

        A repeated external_id with identical content is a harmless repeat
        (the SEC shows a few rows twice across page breaks). A repeat with
        different content is kept as the first sighting and reported.
        """
        records: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        pages = raw_rows = duplicates = 0
        warnings: list[str] = []
        for page in self.fetch():
            pages += 1
            for raw in self.parse(page):
                raw_rows += 1
                record = self.normalise(raw)
                if set(record) != RECORD_KEYS:
                    raise ScrapeError(f"{self.source_id}: normalise() returned keys {sorted(record)}, "
                                      f"expected {sorted(RECORD_KEYS)}")
                key = record["external_id"]
                if key in records:
                    duplicates += 1
                    if records[key][0] != record:
                        warnings.append(f"{key}: appears more than once with different details; kept the first")
                    continue
                records[key] = (record, raw)
        fallback_ids = sum(1 for k in records if k.startswith(FALLBACK_PREFIX))
        return ScrapeResult(self.source_id, pages, raw_rows, list(records.values()),
                            duplicates, fallback_ids, warnings)
