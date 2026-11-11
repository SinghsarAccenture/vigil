from pathlib import Path

import pytest
import requests

from vigil.ingestion.base import FALLBACK_PREFIX, RECORD_KEYS, HttpClient, Page, ScrapeError
from vigil.ingestion.sec_rulemaking import SecRulemaking, link_key, parse_sec_date
from vigil.storage import db

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
BASE = "https://www.sec.gov/rules-regulations/rulemaking-activity"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    """Serves saved pages by page number and records what was requested."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, params=None):
        number = (params or {}).get("page", 0)
        self.requested.append(number)
        return self.pages[number]


def three_page_site():
    # The saved first page says the list has 29 pages; pretend it has 3.
    first = fixture("page0.html").replace("?page=28", "?page=2")
    return {0: first, 1: fixture("page1.html"), 2: fixture("page_last.html")}


def sec(pages, max_pages=None):
    http = FakeHttp(pages)
    return SecRulemaking(http, BASE, max_pages=max_pages), http


# --- dates ---------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Sept. 30, 2026", "2026-09-30"),
    ("Sep. 3, 2026", "2026-09-03"),
    ("Oct. 9, 2026", "2026-10-09"),
    ("Dec. 31, 1999", "1999-12-31"),
    ("June 11, 2025", "2025-06-11"),
    ("July 4, 2024", "2024-07-04"),
    ("March 18, 2025", "2025-03-18"),
    ("May 28, 2026", "2026-05-28"),
    ("Jan. 29, 2025", "2025-01-29"),
    ("Feb. 8, 2024", "2024-02-08"),
    ("Aug. 31, 2026", "2026-08-31"),
    ("Nov. 18, 1960", "1960-11-18"),
    ("  Sept.\n 30,   2026 ", "2026-09-30"),
])
def test_parse_sec_date_handles_abbreviations(text, expected):
    assert parse_sec_date(text) == expected


@pytest.mark.parametrize("text", ["", "2026-09-30", "Sept 30 2026", "Mayhem 1, 2026", "Feb. 30, 2026", "30 Sept. 2026"])
def test_parse_sec_date_rejects_unknown_formats(text):
    with pytest.raises(ValueError):
        parse_sec_date(text)


# --- parsing the saved pages -------------------------------------------------------

def test_parse_first_page_fixture():
    source, _ = sec({})
    rows = source.parse(Page(0, BASE, fixture("page0.html")))

    assert len(rows) == 50
    assert rows[0] == {
        "date_text": "Oct. 9, 2026",
        "datetime_attr": "2026-10-09T14:26:34Z",
        "file_number_cell": rows[0]["file_number_cell"],
        "title": "Investment Company Cross Trading",
        "division": "Investment Management",
        "status": "Proposed Rule",
        "href": "/rules-regulations/2026/10/s7-2026-36#IC-36358proposed",
        "release_numbers": ["IC-36358"],
    }
    assert rows[0]["file_number_cell"].strip() == "S7-2026-36"


def test_normalise_produces_the_standard_shape():
    source, _ = sec({})
    rows = source.parse(Page(0, BASE, fixture("page0.html")))
    records = [source.normalise(r) for r in rows]

    assert all(set(r) == RECORD_KEYS for r in records)
    first = records[0]
    assert first["external_id"] == "s7-2026-36#IC-36358proposed"
    assert first["group_key"] == "s7-2026-36"
    assert first["issue_date"] == "2026-10-09"
    assert first["links"] == ["https://www.sec.gov/rules-regulations/2026/10/s7-2026-36#IC-36358proposed"]
    assert first["link_keys"] == ["https://www.sec.gov/rules-regulations/s7-2026-36#IC-36358proposed"]
    assert first["file_numbers"] == ["S7-2026-36"]
    assert first["reference_numbers"] == ["IC-36358"]

    multi = next(r for r in records if r["title"].startswith("Investment Adviser Performance-Based"))
    assert multi["reference_numbers"] == ["33-11443", "34-106533", "IA-7022", "IC-36350"]
    no_file = next(r for r in records if r["title"] == "Commission Quorum Requirement")
    assert no_file["file_numbers"] == [] and no_file["external_id"] == "34-106537#34-106537final"


def test_multiple_file_numbers_are_split_and_raw_text_kept():
    source, _ = sec({})
    rows = source.parse(Page(28, BASE, fixture("page_last.html")))
    records = [source.normalise(r) for r in rows]

    multi = next(r for r in records if len(r["file_numbers"]) > 1)
    assert "," in multi["file_numbers_raw"]
    assert multi["file_numbers"] == [p.strip() for p in multi["file_numbers_raw"].split(",")]


def test_division_order_and_whitespace_are_normalised():
    source, _ = sec({})
    raw = {"date_text": "Oct. 9, 2026", "datetime_attr": None, "file_number_cell": " S7-1 ",
           "title": "  A   title ", "division": "Trading and Markets, Investment Management",
           "status": "Final\n      Rule", "href": "/rules-regulations/2026/10/x#1final", "release_numbers": []}
    rec = source.normalise(raw)
    assert rec["division"] == "Investment Management, Trading and Markets"
    assert (rec["title"], rec["status"]) == ("A title", "Final Rule")


def test_moved_page_gives_the_same_fingerprint():
    source, _ = sec({})
    raw = {"date_text": "Aug. 10, 2022", "datetime_attr": None, "file_number_cell": "S7-22-22",
           "title": "Form PF", "division": "", "status": "Proposed Rule",
           "href": "/rules-regulations/2025/09/s7-22-22#IA-6083proposed", "release_numbers": ["IA-6083"]}
    before = source.normalise(raw)
    after = source.normalise({**raw, "href": "/rules-regulations/2026/08/s7-22-22#IA-6083proposed"})

    assert before["links"] != after["links"]
    assert link_key(before["links"][0]) == link_key(after["links"][0])
    assert db.content_hash(before) == db.content_hash(after)


def test_row_without_link_gets_a_prefixed_fallback_id():
    source, _ = sec({})
    raw = {"date_text": "Oct. 9, 2026", "datetime_attr": None, "file_number_cell": "",
           "title": "Mystery", "division": "", "status": "", "href": "", "release_numbers": []}
    rec = source.normalise(raw)
    assert rec["external_id"].startswith(FALLBACK_PREFIX)
    assert rec["group_key"] is None and rec["links"] == [] and rec["link_keys"] == []
    assert source.normalise(raw)["external_id"] == rec["external_id"]


# --- pagination and the full scrape -------------------------------------------------

def test_saved_first_page_reports_29_pages():
    assert SecRulemaking._total_pages(fixture("page0.html")) == 29
    assert SecRulemaking._total_pages(fixture("page_last.html")) == 1


def test_scrape_follows_every_page():
    source, http = sec(three_page_site())
    result = source.scrape()

    assert http.requested == [0, 1, 2]
    assert result.pages == 3
    assert result.raw_rows == 50 + 41 + 55
    assert len(result.records) == result.raw_rows - result.duplicates
    assert result.fallback_ids == 0


def test_max_pages_limits_the_scrape():
    source, http = sec(three_page_site(), max_pages=2)
    result = source.scrape()
    assert http.requested == [0, 1]
    assert result.pages == 2


def test_repeated_rows_are_dropped_and_counted():
    pages = three_page_site()
    pages[2] = pages[1]
    result = sec(pages)[0].scrape()

    assert result.duplicates == 41
    assert len(result.records) == 50 + 41 - _repeats_within(pages[0], pages[1])
    assert result.warnings == []


def _repeats_within(*page_texts):
    source, _ = sec({})
    ids = [source.normalise(r)["external_id"] for t in page_texts for r in source.parse(Page(0, BASE, t))]
    return len(ids) - len(set(ids))


def test_scrape_fails_loudly_on_an_empty_page():
    pages = three_page_site()
    pages[1] = pages[1].split("<tbody")[0] + "</table></body></html>"
    with pytest.raises(ScrapeError, match="page 1: table has no rows"):
        sec(pages)[0].scrape()


def test_scrape_fails_loudly_when_table_missing():
    with pytest.raises(ScrapeError, match="no table found"):
        sec({0: "<html><body><p>Maintenance</p></body></html>"})[0].scrape()


def test_scrape_fails_loudly_when_columns_change():
    pages = {0: fixture("page_last.html").replace(">Status<", ">Outcome<", 1)}
    with pytest.raises(ScrapeError, match="table columns"):
        sec(pages)[0].scrape()


# --- polite HTTP client ------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, text="ok", headers=None):
        self.status_code, self.text, self.headers, self.url = status, text, headers or {}, "https://x"


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client(outcomes, **kw):
    sleeps = []
    clock = iter(range(0, 10_000, 5))
    http = HttpClient("Vigil test (ops@example.test)", session=FakeSession(outcomes),
                      sleep=sleeps.append, clock=lambda: next(clock), **kw)
    return http, sleeps


def test_http_sends_user_agent_and_timeout():
    http, _ = client([FakeResponse(200, "hello")])
    assert http.get("https://x") == "hello"
    call = http.session.calls[0]
    assert call["headers"]["User-Agent"] == "Vigil test (ops@example.test)"
    assert call["timeout"] == 30.0


def test_http_retries_with_growing_waits_then_succeeds():
    http, sleeps = client([FakeResponse(503), requests.ConnectionError("reset"), FakeResponse(200, "done")])
    assert http.get("https://x") == "done"
    assert sleeps == [2.0, 4.0]


def test_http_honours_retry_after():
    http, sleeps = client([FakeResponse(429, headers={"Retry-After": "30"}), FakeResponse(200)])
    http.get("https://x")
    assert sleeps == [30.0]


def test_http_gives_up_loudly_after_max_attempts():
    http, sleeps = client([FakeResponse(503)] * 4)
    with pytest.raises(ScrapeError, match="after 4 attempts: HTTP 503"):
        http.get("https://x", params={"page": 7})
    assert sleeps == [2.0, 4.0, 8.0]


def test_http_does_not_retry_a_404():
    http, sleeps = client([FakeResponse(404)])
    with pytest.raises(ScrapeError, match="HTTP 404"):
        http.get("https://x")
    assert sleeps == []


def test_http_waits_between_requests():
    sleeps = []
    times = iter([0.0, 0.25, 1.0])
    http = HttpClient("Vigil test (ops@example.test)", session=FakeSession([FakeResponse(200)] * 2),
                      sleep=sleeps.append, clock=lambda: next(times))
    http.get("https://x")
    http.get("https://x")
    assert sleeps == [0.75]


def test_http_requires_a_user_agent():
    with pytest.raises(ScrapeError, match="SEC_USER_AGENT"):
        HttpClient("  ")
