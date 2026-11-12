"""Static HTML dashboard: a summary plus every filing with its relevance and a link.

Rebuilt from the database after each run. Relevance comes from the newest real
AI answer when there is one; until then a keyword guess on the title is shown and
labelled as such. Each title links to the filing's own page when one is stored,
otherwise to the regulator's list page for that source.

Colour follows the data-viz status scale (relevance is a state to act on, not a
series identity): critical / warning / neutral, each always paired with its word,
so the level never rides on colour alone.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from pathlib import Path

from vigil.storage import db

HIGH_TERMS = [r"crypto", r"digital asset", r"blockchain", r"token", r"stablecoin", r"distributed ledger",
              r"\bbitcoin\b", r"\bether(?:eum)?\b"]
MEDIUM_TERMS = [r"custody", r"safeguarding", r"client assets", r"customer identification", r"anti-money",
                r"money laundering", r"alternative trading", r"\bats\b", r"definition of .?exchange",
                r"systems compliance", r"cybersecurity", r"privacy", r"regulation s-p", r"form pf",
                r"\bdealer\b", r"clearing", r"settlement", r"customer protection", r"reserve requirements",
                r"beneficial ownership", r"best execution", r"order competition", r"market structure"]
_HIGH = re.compile("|".join(HIGH_TERMS), re.I)
_MEDIUM = re.compile("|".join(MEDIUM_TERMS), re.I)

LEVELS = ("High", "Medium", "Low")


def keyword_relevance(title: str) -> tuple[str, str]:
    """(level, why) from the title alone. A stand-in until real AI answers exist."""
    if _HIGH.search(title):
        return "High", "Title mentions crypto / digital assets"
    if _MEDIUM.search(title):
        return "Medium", "Title touches a topic that can affect crypto compliance"
    return "Low", "No crypto-related keywords in the title"


def _relevance(row: sqlite3.Row) -> tuple[str, str, str]:
    """(level, why, basis). Real AI answers win; placeholders and gaps fall back to keywords."""
    if row["relevant"] is not None:
        level = "High" if row["relevant"] else "Low"
        return level, row["reason"] or "", f"AI ({row['model']})"
    level, why = keyword_relevance(row["title"])
    return level, why, "Keyword guess"


def _safe_url(url: str | None) -> str | None:
    return url if url and url.startswith(("https://", "http://")) else None


def _list(value: str | None) -> list[str]:
    try:
        return [str(x) for x in json.loads(value or "[]")]
    except (ValueError, TypeError):
        return []


def _day(timestamp: str | None) -> str:
    """The date part of a stored UTC timestamp."""
    return (timestamp or "")[:10]


def _minute(timestamp: str | None) -> str:
    """A stored UTC timestamp trimmed to the minute, for reading."""
    return (timestamp or "").replace("T", " ")[:16]


def _compact(n: int) -> str:
    if n < 10_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def collect(conn: sqlite3.Connection) -> dict:
    """Everything the page shows, as plain data."""
    sources = conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY source_id").fetchall()
    filings, runs = [], []
    for source in sources:
        run = conn.execute(
            "SELECT * FROM scrape_runs WHERE source_id = ? AND status = 'success' ORDER BY run_id DESC LIMIT 1",
            (source["source_id"],)).fetchone()
        runs.append({"name": source["name"], "url": source["base_url"], "run": dict(run) if run else None})
        group_sizes = dict(conn.execute(
            "SELECT group_key, COUNT(*) FROM v_current WHERE source_id = ? AND group_key IS NOT NULL "
            "GROUP BY group_key", (source["source_id"],)).fetchall())
        rows = conn.execute("SELECT * FROM v_current WHERE source_id = ? ORDER BY issue_date DESC, version_id",
                            (source["source_id"],)).fetchall()
        for row in rows:
            level, why, basis = _relevance(row)
            started = run["started_at"] if run else None
            filings.append({
                "source": source["name"], "date": row["issue_date"] or "", "status": row["status"] or "",
                "title": row["title"], "level": level, "why": why, "basis": basis,
                "division": row["division"] or "", "file_numbers": _list(row["file_numbers_json"]),
                "releases": _list(row["reference_numbers_json"]),
                "group_key": row["group_key"] or "", "group_size": group_sizes.get(row["group_key"], 0),
                "first_seen": _day(row["first_seen_at"]), "version_no": row["version_no"],
                # Placeholders always ask for review, so the tag would be on every row and mean nothing.
                "needs_review": bool(row["requires_human_review"]) and row["relevant"] is not None,
                "link": _safe_url(db.current_link(conn, row["document_id"])) or _safe_url(source["base_url"]),
                # 'new to the database' = first ever seen during the latest successful run.
                "new": bool(started) and row["first_seen_at"] >= started,
                "changed": bool(started) and row["change_type"] == "changed" and row["observed_at"] >= started,
            })
    return {"filings": filings, "runs": runs}


def render(data: dict, generated_at: str, live: bool = False) -> str:
    """The page. live=True adds the 'get filings' button (only meaningful when served by serve.py)."""
    e = html.escape
    filings = data["filings"]
    counts = {k: sum(1 for f in filings if f["level"] == k) for k in LEVELS}
    new_count = sum(1 for f in filings if f["new"])
    changed_count = sum(1 for f in filings if f["changed"])
    total = max(len(filings), 1)

    segments = "".join(
        f'<div class="seg seg-{k.lower()}" style="flex:{counts[k]} 1 0" '
        f'title="{k} relevance: {counts[k]:,} of {len(filings):,} filings"></div>'
        for k in LEVELS if counts[k])
    legend = "".join(
        f'<span class="key"><i class="dot dot-{k.lower()}"></i>{k}<b>{counts[k]:,}</b></span>' for k in LEVELS)

    tiles = "".join(f'<div class="tile"><div class="tval">{_compact(v)}</div><div class="tlab">{lab}</div></div>'
                    for v, lab in ((new_count, "New in latest run"), (changed_count, "Changed in latest run"),
                                   (counts["High"], "High relevance")))

    run_lines = []
    for r in data["runs"]:
        run = r["run"]
        if run:
            run_lines.append(
                f'<a href="{e(r["url"])}" rel="noopener">{e(r["name"])}</a> &middot; run #{run["run_id"]} '
                f'({e(run["mode"])}) finished {e(_minute(run["finished_at"]))} UTC &middot; {run["rows_seen"]:,} entries '
                f'seen, {run["rows_new"]:,} new, {run["rows_changed"]:,} changed, {run["rows_unchanged"]:,} unchanged')
        else:
            run_lines.append(f'<a href="{e(r["url"])}" rel="noopener">{e(r["name"])}</a> &middot; no successful run yet')

    body = []
    for f in filings:
        title = e(f["title"])
        if f["link"]:
            title = f'<a href="{e(f["link"])}" target="_blank" rel="noopener">{title}</a>'
        tags = ""
        if f["new"]:
            tags += ' <span class="tag new">NEW</span>'
        if f["changed"]:
            tags += f' <span class="tag chg">CHANGED &middot; v{f["version_no"]}</span>'
        if f["needs_review"]:
            tags += ' <span class="tag rev">NEEDS REVIEW</span>'
        meta = [e(f["division"])] if f["division"] else []
        if f["file_numbers"]:
            meta.append("File " + e(", ".join(f["file_numbers"])))
        if f["releases"]:
            meta.append("Release " + e(", ".join(f["releases"])))
        if f["group_size"] > 1:
            meta.append(f'{f["group_size"]} filings in rulemaking {e(f["group_key"])}')
        if f["first_seen"]:
            meta.append("first seen " + e(f["first_seen"]))
        meta_html = '<div class="meta">' + " &middot; ".join(meta) + "</div>" if meta else ""
        body.append(
            f'<tr data-l="{f["level"]}" data-new="{int(f["new"])}">'
            f'<td class="num">{e(f["date"])}</td><td>{e(f["status"])}</td>'
            f'<td><span class="ttl">{title}</span>{tags}{meta_html}</td>'
            f'<td><span class="b b-{f["level"].lower()}">{f["level"]}</span></td>'
            f'<td>{e(f["why"])}<br><span class="basis">{e(f["basis"])}</span></td></tr>')

    return (_PAGE.replace("@@LIVE@@", _LIVE if live else "").replace("@@LIVEJS@@", _LIVE_JS if live else "")
            .replace("@@GENERATED@@", e(generated_at)).replace("@@HERO@@", _compact(len(filings)))
            .replace("@@HEROLAB@@", "filing" if len(filings) == 1 else "filings")
            .replace("@@TILES@@", tiles).replace("@@SEGMENTS@@", segments).replace("@@LEGEND@@", legend)
            .replace("@@PCT@@", f"{100 * counts['High'] / total:.0f}")
            .replace("@@RUNS@@", "<br>".join(run_lines)).replace("@@BODY@@", "".join(body)))


def write_dashboard(conn: sqlite3.Connection, path: str | Path, generated_at: str) -> Path:
    path = Path(path)
    path.write_text(render(collect(conn), generated_at), encoding="utf-8")
    return path


_LIVE = """<div class="live"><button id="run">Get today's filings</button><span id="msg"></span>
<details id="log" hidden><summary>Run report</summary><pre id="out"></pre></details></div>"""

# Runs after the page body exists. While a scrape is running the old list is hidden behind a
# loading message; a successful run reloads the page, which always opens with the 'All' filter.
_LIVE_JS = """<script>
(function(){var btn=document.getElementById("run"),msg=document.getElementById("msg"),log=document.getElementById("log"),out=document.getElementById("out"),list=document.getElementById("list"),loading=document.getElementById("loading"),t=null;
function show(s){btn.disabled=s.running;list.hidden=s.running;loading.hidden=!s.running;if(s.running){msg.textContent="";return}
if(s.started_at&&s.exit_code!==null){log.hidden=false;out.textContent=s.report;msg.textContent=s.exit_code===0?"Done.":"Finished, but needs attention. See the run report."}}
function poll(){fetch("/status").then(function(r){return r.json()}).then(function(s){show(s);if(!s.running&&t){clearInterval(t);t=null;if(s.exit_code===0)setTimeout(function(){location.reload()},800)}})}
btn.onclick=function(){btn.disabled=true;list.hidden=true;loading.hidden=false;fetch("/run",{method:"POST"}).then(function(r){return r.json()}).then(function(){if(!t)t=setInterval(poll,1500);poll()}).catch(function(){msg.textContent="Could not reach the Vigil server.";btn.disabled=false;list.hidden=false;loading.hidden=true})};
fetch("/status").then(function(r){return r.json()}).then(function(s){if(s.running){if(!t)t=setInterval(poll,1500);show(s)}})})();
</script>"""

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vigil dashboard</title>
<style>
:root{color-scheme:light;
  --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --rule:#c3c2b7; --accent:#256abf; --accent-ink:#ffffff; --link:#256abf;
  --high:#d03b3b; --medium:#fab219; --low:#898781;
  --on-high:#ffffff; --on-medium:#0b0b0b; --on-low:#ffffff;
  --new:#256abf; --chg:#6250d6; --rev:#ec835a; --on-rev:#0b0b0b;
  --ring:rgba(11,11,11,0.10);}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;
  --surface:#1a1a19; --plane:#0d0d0d; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --rule:#383835; --link:#9ec5f4;
  --ring:rgba(255,255,255,0.10);}}
:root[data-theme="dark"]{color-scheme:dark;
  --surface:#1a1a19; --plane:#0d0d0d; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --rule:#383835; --link:#9ec5f4;
  --ring:rgba(255,255,255,0.10);}

*{box-sizing:border-box}
body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:28px 24px 48px;
  color:var(--ink);background:var(--plane)}
h1{font-size:19px;margin:0 0 2px;letter-spacing:-0.01em}
.sub{color:var(--ink2);font-size:13px;margin-bottom:6px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:16px 18px;margin:16px 0}

/* Summary: one hero figure, three stat tiles, one stacked meter. */
.summary{display:flex;flex-wrap:wrap;gap:28px;align-items:flex-end}
.hero{font-size:48px;font-weight:600;line-height:1}
.hero span{font-size:14px;font-weight:400;color:var(--ink2);margin-left:6px}
.tiles{display:flex;gap:26px}
.tval{font-size:22px;font-weight:600;line-height:1.2}
.tlab{color:var(--ink2);font-size:12px}
.meter{display:flex;gap:2px;height:14px;margin:14px 0 8px;max-width:680px}
.seg{border-radius:0}.seg:first-child{border-radius:4px 0 0 4px}.seg:last-child{border-radius:0 4px 4px 0}
.seg-high{background:var(--high)}.seg-medium{background:var(--medium)}.seg-low{background:var(--low)}
.key{color:var(--ink2);font-size:12px;margin-right:16px;white-space:nowrap}
.key b{color:var(--ink);margin-left:5px;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:baseline}
.dot-high{background:var(--high)}.dot-medium{background:var(--medium)}.dot-low{background:var(--low)}

.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
button,input{font:inherit;padding:6px 12px;border:1px solid var(--rule);border-radius:7px;
  background:var(--surface);color:var(--ink);cursor:pointer}
button.on{background:var(--ink);color:var(--surface);border-color:var(--ink)}
input[type=search]{cursor:text;min-width:240px}
label.chk{display:inline-flex;align-items:center;gap:6px;color:var(--ink2);font-size:13px;cursor:pointer}
#cnt{color:var(--muted);font-size:12px}

table{border-collapse:collapse;width:100%;background:var(--surface)}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--grid);vertical-align:top}
th{position:sticky;top:0;background:var(--surface);cursor:pointer;white-space:nowrap;font-size:12px;
  color:var(--ink2);border-bottom:1px solid var(--rule)}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--ink2)}
.ttl{font-weight:500}
.meta{color:var(--ink2);font-size:12px;margin-top:3px}
.basis{color:var(--ink2);font-size:12px}
a{color:var(--link)}
.b{display:inline-block;padding:2px 9px;border-radius:11px;font-weight:600;font-size:12px;white-space:nowrap}
.b-high{background:var(--high);color:var(--on-high)}
.b-medium{background:var(--medium);color:var(--on-medium)}
.b-low{background:var(--low);color:var(--on-low)}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:700;
  letter-spacing:0.03em;vertical-align:1px;white-space:nowrap}
.tag.new{background:var(--new);color:#fff}
.tag.chg{background:var(--chg);color:#fff}
.tag.rev{background:var(--rev);color:var(--on-rev)}

#run{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
#run:disabled{opacity:.55;cursor:wait}
#msg{margin-left:10px;color:var(--ink2)}
.live{margin:14px 0 0}
pre{background:var(--plane);border:1px solid var(--ring);padding:10px;border-radius:7px;overflow:auto;
  max-height:320px;font-size:12px}
</style></head><body>
<h1>Vigil &middot; regulatory filings and relevance</h1>
<div class="sub">Updated @@GENERATED@@</div>
@@LIVE@@
<div id="loading" class="card" hidden><b>Loading today's filings...</b> Checking the SEC. This can take a minute
or two, and the list will appear here when it's done.</div>
<div id="list">
<div class="card">
  <div class="summary">
    <div class="hero">@@HERO@@<span>@@HEROLAB@@ tracked</span></div>
    <div class="tiles">@@TILES@@</div>
  </div>
  <div class="meter">@@SEGMENTS@@</div>
  <div>@@LEGEND@@</div>
  <div class="sub" style="margin:10px 0 0">@@PCT@@% of tracked filings are rated High relevance. Each level is
  shown as a word as well as a colour; hover a bar segment for its count.</div>
</div>
<div class="sub">@@RUNS@@</div>
<div class="tools" id="filters"><button class="on" data-f="All">All</button><button data-f="High">High</button><button data-f="Medium">Medium</button><button data-f="Low">Low</button>
<label class="chk"><input type="checkbox" id="newonly" autocomplete="off">New only</label>
<input type="search" id="q" placeholder="Search titles, divisions, file numbers..." autocomplete="off"><span id="cnt"></span></div>
<table id="t"><thead><tr><th data-k="0">Date</th><th data-k="1">Status</th><th data-k="2">Filing (click to open)</th><th data-k="3">Relevance</th><th data-k="4">Why</th></tr></thead><tbody>@@BODY@@</tbody></table>
</div>
<script>
var f="All",rows=[].slice.call(document.querySelectorAll("#t tbody tr"));
function go(){var q=document.getElementById("q").value.toLowerCase(),only=document.getElementById("newonly").checked,n=0;
rows.forEach(function(r){var ok=(f=="All"||r.dataset.l==f)&&(!only||r.dataset.new=="1")&&r.textContent.toLowerCase().indexOf(q)>-1;r.style.display=ok?"":"none";if(ok)n++});
document.getElementById("cnt").textContent=n+" of "+rows.length+" shown"}
document.querySelectorAll("#filters button").forEach(function(b){b.onclick=function(){f=b.dataset.f;document.querySelectorAll("#filters button").forEach(function(x){x.classList.toggle("on",x==b)});go()}});
document.getElementById("q").oninput=go;document.getElementById("newonly").onchange=go;
var asc={};document.querySelectorAll("th").forEach(function(h){h.onclick=function(){var k=+h.dataset.k;asc[k]=!asc[k];var order={High:0,Medium:1,Low:2};rows.sort(function(a,b){var x=a.cells[k].textContent,y=b.cells[k].textContent;if(k==3){x=order[x];y=order[y]}return (x>y?1:x<y?-1:0)*(asc[k]?1:-1)});var tb=document.querySelector("#t tbody");rows.forEach(function(r){tb.appendChild(r)})}});
go();
</script>
@@LIVEJS@@
</body></html>"""
