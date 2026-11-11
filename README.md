# Vigil

Vigil is an AI-powered regulatory monitor. Once a day it reads regulators' publication lists
(starting with the SEC), stores every entry with a full change history, and will ask Claude
whether each new item matters to a crypto/digital-asset compliance team.

Status: **database layer, SEC scraper, placeholder AI step and daily runner complete and tested;
history loaded into `vigil.db` on 2026-10-13. Real Claude classifier not yet built.**

## Project layout

```
CCO_AGENT/
├── config/
│   └── sources.yaml
├── fixtures/
│   ├── page0.html
│   ├── page1.html
│   └── page_last.html
├── legacy/
│   ├── README.md
│   ├── placesToScrape.txt
│   └── scraper.cpython-314.pyc.disabled
├── src/
│   └── vigil/
│       ├── __init__.py
│       ├── ingestion/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   └── sec_rulemaking.py
│       ├── llm/
│       │   ├── __init__.py
│       │   └── classifier.py
│       ├── run_daily.py
│       └── storage/
│           ├── __init__.py
│           ├── schema.sql
│           └── db.py
├── tests/
│   ├── conftest.py
│   ├── test_classifier.py
│   ├── test_db.py
│   ├── test_run_daily.py
│   ├── test_schema_version.py
│   └── test_sec_rulemaking.py
├── .env              (not in git)
├── .env.example
├── .gitignore
├── .venv/            (not in git)
├── prompt.txt
├── README.md
├── pytest.ini
├── requirements.txt
└── vigil.db          (not in git)
```

---

## Setup (new machine)

Needs Python 3.14 or later. From the project folder:

1. Create the project's own Python environment: `python -m venv .venv`
2. Install the pinned packages: `.venv\Scripts\python.exe -m pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill it in. At minimum, set `SEC_USER_AGENT` (see below).
   The API key is only needed once the real Claude classifier is enabled.
4. Check everything works: `.venv\Scripts\python.exe -m pytest` (all tests should pass; no internet needed).
5. Do the first-run sequence below.

## How to run it

From the project folder, using the project's Python (`.venv\Scripts\python.exe`):

| Command | When to use it |
|---|---|
| `python src/vigil/run_daily.py --baseline` | Once, on a brand-new database: loads the full history without sending it to the AI step. **Done on 2026-10-13.** |
| `python src/vigil/run_daily.py --classify-since 2026-09-01` | Once after the baseline: sends recent entries (from that date) to the AI step. **Done on 2026-10-13.** |
| `python src/vigil/run_daily.py` | Every day from then on. |
| `python src/vigil/run_daily.py --max-pages 3` | Quick test that only reads the first 3 pages (skips the row-count checks). |

`.env` must contain `SEC_USER_AGENT`, a name plus contact email, e.g.
`SEC_USER_AGENT='Vigil regulatory monitor (you@example.com)'`. The SEC requires it.

**Recommended first-run sequence** on a new, empty database: run `--baseline` once, then
`--classify-since` with a recent date once, then the plain daily command from then on. This
avoids sending decades of history to the AI step.

### Running it automatically every day (Windows)

Not set up yet. To do it with Windows Task Scheduler, create a daily task with:

| Setting | Value |
|---|---|
| Program | `C:\Code\CCO_AGENT\.venv\Scripts\python.exe` |
| Arguments | `src\vigil\run_daily.py` |
| Start in | `C:\Code\CCO_AGENT` |

The task's "last run result" shows `0x0` when the run was OK and `0x1` when it needs attention.
Task Scheduler doesn't keep the printed report, but every run is recorded in the database's run
logbook (`scrape_runs`), and the stale-source check flags any regulator that misses a day.

## Current state of `vigil.db` (2026-10-13)

| What | Count |
|---|---|
| Runs logged | 3, all successful: #1 baseline, #2 daily, #3 daily with `--classify-since 2026-09-01` |
| SEC entries (documents) | 1,611, each on version 1 (no changes seen yet) |
| Rulemakings (groups) | 854 |
| AI answers | 10 placeholders (relevance "unknown") for entries issued since 2026-09-01 |
| Warnings raised in real runs | None |

## Reading the daily report

Each line starts with a label. The last line is **RESULT: OK** (exit code 0) or
**RESULT: NEEDS ATTENTION** (exit code 1).

| Label | Example | How serious | What to do |
|---|---|---|---|
| **OK** | `Run #2: 29 page(s) in 28s` plus counts | Fine | Nothing. |
| **NOTE** | `Closed abandoned run #N` | Informational | Nothing, unless it happens often (something keeps interrupting the program). |
| **WARNING** | `row count decreased 1,611 -> 1,609 ... Run kept.` | Low–medium; run still counts as OK | Check the newly missing entries listed beneath it. |
| **WARNING** | `N entries not seen this run (M newly missing)` | Medium | A rule disappearing can matter (e.g. a withdrawn proposal); check the listed ones. Each is listed once, on the day it goes missing. |
| **WARNING** | `... appears more than once with different details` | Low | Usually an SEC glitch; check sec.gov if it repeats. |
| **WARNING** | `N AI attempt(s) failed and will be retried` | Low | Retried automatically. |
| **ABORTED** | `row count dropped 1,609 -> 50 ... more than the 10% limit` | High; nothing saved | Almost always a broken scrape, not deleted rules. Retry later; if it repeats, the scraper needs updating. |
| **FAILED** | `ScrapeError: SEC page 0: no table found` | High; nothing saved | Temporary outages fix themselves; "layout changed" messages mean the scraper needs updating. |
| **FAILED** | message followed by `Traceback` lines | High | Likely a bug in Vigil; send the message to a developer. |
| **FAILED** | `Not started: ... still running` | Low if one-off | Two runs overlapped. A stuck run is cleared automatically after 6 hours. |
| **FAILED** | `Not run: SEC_USER_AGENT is missing` | High, quick fix | Add the contact line to `.env`. |
| **FAILED** | `AI step stopped after 3 failures in a row` | Medium; scraping was still saved | Usually a bad or expired API key. Waiting entries are picked up once fixed. |
| **FAILED** | `Vigil could not start: ...` | High | Broken `sources.yaml`, database design mismatch, or the database file won't open; the message says which. |
| **STALE** | `no successful run within 26h` | Highest | That regulator isn't being monitored. Find out why recent runs failed or didn't happen. |

---

## Folders

### `config/`
Settings that describe *what* to monitor, kept out of the code so they can change without
touching Python.

| File | What it does |
|---|---|
| `sources.yaml` | The list of regulators Vigil knows about. Each entry has an ID, name, regulator, jurisdiction, scraper type (`html`, `api`, `rss`, `pdf_index`), home URL, how often it should be checked (`expected_every_hours`) and whether it is switched on. Only **SEC Rulemaking Activity** is enabled. FinCEN, OFAC, ESMA, EBA, FCA, HM Treasury, Bank of England, SFC, HKMA, FATF, FSB and IOSCO are listed as disabled placeholders. Copied into the database's `sources` table on every run. |

### `fixtures/`
Saved copies of real SEC web pages, downloaded on 2026-10-12. Tests read these instead of the
live website, so tests are fast, repeatable and never touch the network.

| File | What it does |
|---|---|
| `page0.html` | First page of the SEC rulemaking list (newest 50 entries). Also contains the "last page" link used to work out how many pages exist. |
| `page1.html` | Second page, for testing that pagination is followed correctly. |
| `page_last.html` | Final page (page 29), the oldest entries, for testing the end of pagination. |

### `legacy/`
Archive of the earlier prototype. **Nothing here runs or is imported.**

| File | What it does |
|---|---|
| `README.md` | Explains what the archived files are and why they are disabled. |
| `placesToScrape.txt` | The original hand-written list of regulators. Replaced by `config/sources.yaml`. |
| `scraper.cpython-314.pyc.disabled` | Compiled leftover of the deleted `scraper.py`. Renamed so Python cannot load it. It contained no hardcoded API key. |

### `src/vigil/`
The application code, as a Python package named `vigil`.

| File | What it does |
|---|---|
| `__init__.py` | Marks `vigil` as a Python package. Empty. |
| `run_daily.py` | **The program you run once a day.** It updates the regulator list, then for each enabled regulator: closes any abandoned runs, opens a logbook entry, scrapes, checks the row count against the last successful run, saves everything in one go, and closes the logbook entry. Then it runs the AI step (placeholders for now), checks for stale sources, and prints a report: new entries with links, what changed in changed entries (old -> new), Final-Rule-after-Proposal alerts, repeated rows and fallback IDs. On any drop in entries it prints a WARNING with the newly missing entries. On a drop of more than 10% it prints ABORTED, marks the run failed and saves nothing. Exit code 0 = all fine, 1 = needs attention. Options: `--baseline` (first-time history load, no AI step), `--classify-since YYYY-MM-DD` (send entries from that date to the AI step), `--max-pages N` (quick partial test), `--db PATH`. Run it with `python src/vigil/run_daily.py`. |

### `src/vigil/ingestion/`
Downloading and reading regulators' websites. It only collects data; saving happens separately,
so a bad download can be caught before anything is stored.

| File | What it does |
|---|---|
| `__init__.py` | Marks `ingestion` as a sub-package. Empty. |
| `base.py` | The shared toolkit for every regulator. A polite downloader that identifies itself (User-Agent), waits 1 second between requests, gives up on a request after 30 seconds, and retries temporary errors up to 4 times with growing waits (2, 4, 8 seconds, or whatever the site asks). Also the common three-step pattern (download, read, tidy up), removal of repeated rows, `fallback:` IDs for rows with no natural ID, and a check that every regulator's records come out in the same shape. |
| `sec_rulemaking.py` | The SEC scraper. Reads every page of the SEC list (29 today) by following the site's own "last page" link, with an optional page limit for testing. Turns dates like "Sept. 30, 2026" into `2026-09-30`, splits File Numbers, collects release numbers, sorts divisions, and keeps the real link while ignoring the part the SEC moves. Stops loudly if the table is missing, its columns change, or a page comes back empty. A full scrape takes about 30–75 seconds, depending on how fast the SEC responds. |

### `src/vigil/llm/`
The AI step: deciding whether each entry matters to the compliance team.

| File | What it does |
|---|---|
| `__init__.py` | Marks `llm` as a sub-package. Empty. |
| `classifier.py` | Works through the queue of entries waiting for an assessment and saves each answer or error. One failure never stops the step; 3 failures in a row (e.g. a dead API key) stop it early, and the rest wait for the next run. For now it uses the **placeholder**: a clearly labelled stand-in answer with relevance "unknown" and "needs human review". The real Claude classifier will plug in here later, and will re-assess every entry, replacing the placeholders. |

### `src/vigil/storage/`
Everything to do with saving and reading data. Uses SQLite, a single-file database built into
Python, with no extra database server needed.

| File | What it does |
|---|---|
| `__init__.py` | Marks `storage` as a sub-package. Empty. |
| `schema.sql` | The database blueprint: every table, index, safety rule and view, stamped with a design version number. Only used to build a brand-new database; existing ones are upgraded step by step (see `db.py`). Kept as plain SQL so it can be moved to Postgres later. See *Database tables* below. |
| `db.py` | The functions the rest of Vigil uses to talk to the database. See *What `db.py` does* below. |

### `tests/`
Automated checks, run with `pytest`. They use throwaway databases and never call the
internet or the AI. 115 tests in total, all passing.

| File | What it does |
|---|---|
| `conftest.py` | Shared test setup: creates a fresh empty database for each test and provides a sample SEC-style record (`make_record`). |
| `test_classifier.py` | 6 tests for the AI step: placeholders are filled in as "unknown" and labelled; real AI answers later replace them; a real model can never save "unknown"; one failure doesn't stop the step; repeated failures stop it early and are retried next run; history loaded by baseline is skipped unless asked for. |
| `test_schema_version.py` | 7 tests for the design version stamp: new databases are stamped; reopening changes nothing; older databases are upgraded in place without losing data; a failed upgrade leaves everything untouched; a missing upgrade step, a database newer than the code, or an unstamped old database all stop with a clear message. |
| `test_run_daily.py` | 29 tests for the daily program, using a pretend scraper: the 10% rule and its messages; first run, re-run with nothing new, changes shown old -> new, Final-after-Proposal alerts, repeated rows and fallback IDs reported; baseline loads without AI; `--classify-since` only assesses recent entries; a small drop warns and lists newly missing entries (once, not every day); a sharp drop aborts and saves nothing; partial runs skip the checks; scrape errors, unexpected errors, overlapping runs, abandoned runs, a missing scraper or SEC contact line, and the AI step stopping early are all reported loudly; reading `.env`; and a full end-to-end run. |
| `test_sec_rulemaking.py` | 39 tests for the scraper, using the saved pages in `fixtures/`: date formats such as "Sept.", "Sep." and "June" (and rejecting nonsense like "Feb. 30"); reading the first page exactly; the standard record shape; split File Numbers; tidying divisions and spacing; a moved SEC page not counting as a change; fallback IDs; following every page; the page limit; dropping repeated rows; stopping loudly on empty pages, missing tables or changed columns; and the polite downloader (User-Agent, timeout, waits between requests, retries, giving up, not retrying a "page not found"). |
| `test_db.py` | 34 tests for the storage layer. Covers new entries, unchanged re-sightings, Proposed → Final versioning, ignoring cosmetic noise, the locked fingerprint recipe, saving real links while ignoring page moves, finding an entry's current working link, all-or-nothing saves, the append-only protections, run logging, blocking overlapping runs, closing abandoned runs, refusing an empty regulator list, the classification retry queue, baseline exclusion, stale-source detection, "not seen this run", and the Final-after-Proposed signal. |

---

## Root files

| File | What it does |
|---|---|
| `.env` | Your real settings and secrets: API key, proxy URL, SEC contact line. **Ignored by git**; never committed. |
| `.env.example` | Template showing which settings `.env` needs: `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` (optional proxy), `CLAUDE_MODEL`, `SEC_USER_AGENT` (SEC requires contact details), `VIGIL_DB_PATH` (optional). Safe to commit. |
| `.gitignore` | Tells git what never to commit: `.env`, database files (`*.db`), raw downloads (`raw/`), the virtual environment, caches and local editor settings. |
| `.venv/` | Project-local Python environment with the pinned packages installed. Ignored by git; recreate with `pip install -r requirements.txt`. |
| `prompt.txt` | Your latest instructions for this build (approval plus nine extra requirements). |
| `README.md` | This file. |
| `pytest.ini` | Test configuration: tells pytest where the code (`src`) and tests (`tests`) live. |
| `requirements.txt` | Exact pinned versions of the only allowed packages: anthropic 1.13.0, beautifulsoup4 4.15.0, PyYAML 6.0.3, requests 2.34.2, pytest 9.1.1. |
| `vigil.db` | The real database, a single file. Created by the first run; holds the full SEC history since the baseline load on 2026-10-13. **Ignored by git.** |

---

## Database tables (`schema.sql`)

| Table / view | Plain-English purpose |
|---|---|
| `schema_info` | One number: the design version this database was built to (currently 1). |
| `sources` | One row per regulator from `sources.yaml`. |
| `scrape_runs` | A log entry for every scrape: when it started and finished, whether it was a `baseline` (history load) or `daily` run, success or failure, the error if any, and counts of rows seen, new, changed, unchanged, duplicates dropped and fallback IDs used. |
| `documents` | One row per published item (for the SEC: one release on one rulemaking, e.g. `s7-22-22#IA-6992final`). Holds its identity, its `group_key` (the rulemaking it belongs to, can be blank) and when it was first and last seen. |
| `document_versions` | Each distinct state a document has been seen in: date, title, division, status, File Numbers (as a list **and** the raw cell text), release numbers, links, the full raw scraped row, and a SHA-256 fingerprint. A real change adds version 2, linked back to version 1. |
| `classifications` | Each AI judgement attempt: model, prompt version, relevant yes/no, confidence, reason, document type, needs-human-review flag, the full AI answer, or the error if it failed. At most one success per version per prompt version. Relevance may be "unknown" only for placeholder answers. |
| `alerts` | Placeholder for the future alerts workflow. Not used yet. |
| `v_current` (view) | Ready-made query: the latest version of every document with its latest successful classification. |
| `v_group_history` (view) | Ready-made query: all documents grouped by rulemaking, for seeing a proposal followed by its final rule. |

**Safety rules built into the database:** documents, versions and classifications can never be
edited or deleted. The only thing that can change is a document's `last_seen_at`, and only
forwards in time.

## What `db.py` does

| Function | Purpose |
|---|---|
| `connect`, `init_db`, `schema_version` | Open the database and get it ready. A brand-new database is built from `schema.sql`. An older one is upgraded in place, one step at a time, keeping all data. If anything doesn't match (newer than the code, no stamp, a missing upgrade step), it stops with a clear message. |
| `sync_sources` | Copy `sources.yaml` into the database; regulators removed from the file get switched off. Refuses an empty list. |
| `close_abandoned_runs` | Finds runs stuck on "running" for over 6 hours (the program was killed or crashed) and marks them failed, "abandoned". |
| `start_run`, `finish_run`, `get_run`, `last_successful_run` | Log each scrape from start to finish. A run can only be closed once, and a new run won't start while another is still running for the same regulator. |
| `canonicalise`, `content_hash` | Clean up a record (trim spaces, sort lists, require `YYYY-MM-DD` dates) and fingerprint it, so cosmetic noise never looks like a change. Uses stable link forms (`link_keys`) for the fingerprint when given, so a regulator moving a page isn't a change. |
| `record_observation` | The core rule: new item → create it; same fingerprint → just update "last seen"; different fingerprint → add a new linked version. Saves links exactly as seen. |
| `get_document`, `get_versions` | Look up a document and its history. |
| `versions_in_run` | Everything a run added, with each changed entry's previous details alongside, so the report can show old -> new. |
| `current_link` | Gives a working link for an entry. The SEC moves a rulemaking's page when it adds something, which breaks old addresses, so this uses the newest address seen for that rulemaking. |
| `pending_classifications` | The AI work queue: newest versions with no successful classification for the current prompt. Failed attempts are retried next run. History loaded by `--baseline` is skipped unless `--classify-since DATE` asks for it. |
| `record_classification` | Save one AI result or one error, never both. |
| `stale_sources` | Dead man's switch: lists enabled regulators with no successful scrape within their expected window. |
| `documents_not_seen` | Lists items that weren't on the website during the latest run (possible removals). |
| `final_after_proposed` | Flags a new Final Rule in a rulemaking that already had a Proposed Rule, or an item whose status moved from Proposed to Final. |

---

## Still to be built (next steps)

| Planned file | Purpose |
|---|---|
| `prompts/relevance_v1.md` | The versioned AI prompt for judging crypto/compliance relevance. |
| Real Claude classifier (in `src/vigil/llm/classifier.py`) | Sends waiting items to Claude, checks the answer is in the right format, records results or errors. Waiting on a new API key; placeholders are used until then. |