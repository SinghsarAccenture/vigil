"""SEC Rulemaking Activity scraper.

Source: https://www.sec.gov/rules-regulations/rulemaking-activity
The page is the only up-to-date source (no API; the RSS feeds stopped in 2023).
It is an HTML table paginated with ?page=0..N; page sizes vary, so pagination
follows the "last page" link rather than assuming 50 rows per page.

Each table row is one release on one rulemaking. Its link looks like
  /rules-regulations/2026/08/s7-22-22#IA-6992final
where "s7-22-22" names the rulemaking (shared by all its releases) and
"IA-6992final" names the release. The year/month part moves whenever the SEC
adds a release, so it is dropped for change detection (link_keys) but the real
link is kept.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Any, Iterator, Mapping

from bs4 import BeautifulSoup

from vigil.ingestion.base import Page, ScrapeError, Source, make_fallback_id

SEC_HOST = "https://www.sec.gov"
EXPECTED_HEADERS = ["Issue Date", "File Number", "Rulemaking", "Status"]

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_DATE_RE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})")
_MOVING_PART_RE = re.compile(r"/rules-regulations/\d{4}/\d{2}/")


def parse_sec_date(text: str) -> str:
    """Convert the SEC's display date (e.g. "Sept. 30, 2026") to "2026-09-30".

    Uses the visible text, not the hidden UTC timestamp, which can be a day
    later than the date the SEC shows.
    """
    match = _DATE_RE.fullmatch(" ".join(text.split()))
    if not match or match.group(1).lower() not in MONTHS:
        raise ValueError(f"unrecognised SEC date {text!r}")
    month = MONTHS[match.group(1).lower()]
    return dt.date(int(match.group(3)), month, int(match.group(2))).isoformat()


def absolute_url(href: str) -> str:
    return href if href.startswith("http") else SEC_HOST + href


def link_key(url: str) -> str:
    """Stable form of an SEC link: the moving /YYYY/MM/ part removed."""
    return _MOVING_PART_RE.sub("/rules-regulations/", url)


class SecRulemaking(Source):
    source_id = "sec_rulemaking"

    def fetch(self) -> Iterator[Page]:
        first_text = self.http.get(self.base_url)
        total = self._total_pages(first_text)
        if self.max_pages is not None:
            total = min(total, self.max_pages)
        yield Page(0, self.base_url, first_text)
        for number in range(1, total):
            yield Page(number, f"{self.base_url}?page={number}",
                       self.http.get(self.base_url, params={"page": number}))

    @staticmethod
    def _total_pages(html: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        last = soup.find("a", title=re.compile(r"last page", re.I))
        if last is not None:
            match = re.search(r"[?&]page=(\d+)", last.get("href", ""))
            if not match:
                raise ScrapeError(f"SEC: cannot read the last-page link {last.get('href')!r}")
            return int(match.group(1)) + 1
        if soup.find("a", rel="next") is not None:
            raise ScrapeError("SEC: page has a 'next' link but no 'last page' link; pagination layout changed")
        return 1

    def parse(self, page: Page) -> list[dict[str, Any]]:
        soup = BeautifulSoup(page.text, "html.parser")
        table = soup.find("table")
        if table is None:
            raise ScrapeError(f"SEC page {page.number}: no table found; the page layout may have changed")
        headers = [" ".join(th.get_text().split()) for th in table.find_all("th")]
        if headers != EXPECTED_HEADERS:
            raise ScrapeError(f"SEC page {page.number}: table columns are {headers}, expected {EXPECTED_HEADERS}")

        rows: list[dict[str, Any]] = []
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            if len(cells) != 4:
                raise ScrapeError(f"SEC page {page.number}: a row has {len(cells)} cells, expected 4")
            rows.extend(self._parse_row(cells))
        if not rows:
            raise ScrapeError(f"SEC page {page.number}: table has no rows")
        return rows

    @staticmethod
    def _parse_row(cells) -> list[dict[str, Any]]:
        date_cell, file_cell, rulemaking_cell, status_cell = cells
        time_el = date_cell.find("time")
        title_cell = copy.copy(rulemaking_cell)
        division_div = title_cell.find("div", class_="division")
        division = division_div.get_text(" ", strip=True) if division_div else ""
        if division_div is not None:
            division_div.extract()
        common = {
            "date_text": date_cell.get_text(" ", strip=True),
            "datetime_attr": time_el.get("datetime") if time_el else None,
            "file_number_cell": file_cell.get_text(),
            "title": title_cell.get_text(" ", strip=True),
            "division": division,
        }

        buttons = status_cell.find_all("a", class_="info-button")
        if not buttons:
            return [{**common, "status": "", "href": "", "release_numbers": []}]
        rows = []
        for btn in buttons:
            top = btn.find("span", class_="info-button__top")
            rows.append({
                **common,
                "status": " ".join(top.get_text().split()) if top else "",
                "href": btn.get("href", ""),
                "release_numbers": [s.get_text(strip=True).rstrip(",").strip()
                                    for s in btn.select("span.regulation-node-release-number > span")
                                    if s.get_text(strip=True)],
            })
        return rows

    def normalise(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        issue_date = parse_sec_date(raw["date_text"])
        title = " ".join(raw["title"].split())
        status = " ".join(raw["status"].split())
        href = raw["href"].strip()
        if "#" in href:
            path, fragment = href.split("#", 1)
            group_key = path.rstrip("/").rsplit("/", 1)[-1]
            external_id = f"{group_key}#{fragment}"
        else:
            group_key = None
            external_id = make_fallback_id(issue_date, title, status)
        url = absolute_url(href) if href else None
        return {
            "external_id": external_id,
            "group_key": group_key,
            "issue_date": issue_date,
            "title": title,
            "division": ", ".join(sorted(p.strip() for p in raw["division"].split(",") if p.strip())),
            "status": status,
            "links": [url] if url else [],
            "link_keys": [link_key(url)] if url else [],
            "file_numbers": [p.strip() for p in raw["file_number_cell"].split(",") if p.strip()],
            "file_numbers_raw": raw["file_number_cell"],
            "reference_numbers": list(raw["release_numbers"]),
        }
