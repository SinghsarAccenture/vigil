"""Static HTML dashboard: a summary plus every filing with its relevance and a link.

Rebuilt from the database after each run. Relevance comes from the newest real
AI answer when there is one; until then a keyword guess on the title is shown and
labelled as such. Each title links to the filing's own page when one is stored,
otherwise to the regulator's list page for that source.
"""

from __future__ import annotations

import html
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
COLORS = {"High": "#c0392b", "Medium": "#e6a100", "Low": "#aab3bc"}


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


def collect(conn: sqlite3.Connection) -> dict:
    """Everything the page shows, as plain data."""
    sources = conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY source_id").fetchall()
    filings, runs = [], []
    for source in sources:
        run = conn.execute(
            "SELECT * FROM scrape_runs WHERE source_id = ? AND status = 'success' ORDER BY run_id DESC LIMIT 1",
            (source["source_id"],)).fetchone()
        runs.append({"name": source["name"], "url": source["base_url"],
                     "run": dict(run) if run else None})
        rows = conn.execute("SELECT * FROM v_current WHERE source_id = ? ORDER BY issue_date DESC, version_id",
                            (source["source_id"],)).fetchall()
        for row in rows:
            level, why, basis = _relevance(row)
            filings.append({
                "source": source["name"], "date": row["issue_date"] or "", "status": row["status"] or "",
                "title": row["title"], "level": level, "why": why, "basis": basis,
                "link": _safe_url(db.current_link(conn, row["document_id"])) or _safe_url(source["base_url"]),
                "list_url": _safe_url(source["base_url"]),
                "new": bool(run) and row["change_type"] == "new" and row["observed_at"] >= run["started_at"],
            })
    return {"filings": filings, "runs": runs}


def render(data: dict, generated_at: str, live: bool = False) -> str:
    """The page. live=True adds the 'get filings' button (only meaningful when served by serve.py)."""
    e = html.escape
    filings = data["filings"]
    counts = {k: sum(1 for f in filings if f["level"] == k) for k in LEVELS}
    total = max(len(filings), 1)
    bar = "".join(f'<div title="{k}: {counts[k]}" style="width:{100 * counts[k] / total:.2f}%;'
                  f'background:{COLORS[k]}"></div>' for k in LEVELS)
    run_lines = []
    for r in data["runs"]:
        run = r["run"]
        if run:
            run_lines.append(
                f'<a href="{e(r["url"])}" rel="noopener">{e(r["name"])}</a>: run #{run["run_id"]} finished '
                f'{e(run["finished_at"] or "")} &middot; {run["rows_seen"]:,} entries, {run["rows_new"]:,} new, '
                f'{run["rows_changed"]:,} changed')
        else:
            run_lines.append(f'<a href="{e(r["url"])}" rel="noopener">{e(r["name"])}</a>: no successful run yet')
    new_count = sum(1 for f in filings if f["new"])
    body = []
    for f in filings:
        title = e(f["title"])
        if f["link"]:
            title = f'<a href="{e(f["link"])}" target="_blank" rel="noopener">{title}</a>'
        new = ' <span class="new">NEW</span>' if f["new"] else ""
        body.append(
            f'<tr data-l="{f["level"]}"><td>{e(f["date"])}</td><td>{e(f["status"])}</td>'
            f'<td>{title}{new}</td><td><span class="b {f["level"]}">{f["level"]}</span></td>'
            f'<td>{e(f["why"])}<br><span class="basis">{e(f["basis"])}</span></td></tr>')
    return (_PAGE.replace("@@LIVE@@", _LIVE if live else "").replace("@@LIVEJS@@", _LIVE_JS if live else "")
            .replace("@@GENERATED@@", e(generated_at)).replace("@@N@@", f"{len(filings):,}")
            .replace("@@NEW@@", f"{new_count:,}").replace("@@BAR@@", bar)
            .replace("@@H@@", str(counts["High"])).replace("@@M@@", str(counts["Medium"]))
            .replace("@@L@@", str(counts["Low"]))
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

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Vigil dashboard</title>
<style>
body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#1b1f24;background:#fff}
h1{font-size:20px;margin:0 0 4px}.sub{color:#59636e;margin-bottom:10px}
.bar{display:flex;height:14px;border-radius:7px;overflow:hidden;margin:10px 0 6px;max-width:640px}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
button,input{font:inherit;padding:6px 10px;border:1px solid #c5ccd3;border-radius:6px;background:#fff;cursor:pointer}
button.on{background:#1b1f24;color:#fff;border-color:#1b1f24}input{cursor:text;min-width:260px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #e3e7eb;vertical-align:top}
th{position:sticky;top:0;background:#f5f7f9;cursor:pointer;white-space:nowrap}
.b{display:inline-block;padding:2px 8px;border-radius:10px;font-weight:600;font-size:12px}
.High{background:#fbdcdc;color:#8a1111}.Medium{background:#fdecc8;color:#7a4b00}.Low{background:#e6eaee;color:#3d4650}
.new{background:#dbeafe;color:#1e40af;border-radius:10px;padding:1px 6px;font-size:11px;font-weight:700}
.basis{color:#59636e;font-size:12px}a{color:#1a56b0}
#run{background:#1a56b0;color:#fff;border-color:#1a56b0;font-weight:600}#run:disabled{background:#8fa8cf;border-color:#8fa8cf;cursor:wait}
#msg{margin-left:10px}.live{margin:10px 0 16px}pre{background:#f5f7f9;padding:10px;border-radius:6px;overflow:auto;max-height:320px;font-size:12px}
</style></head><body>
<h1>Vigil: regulatory filings and relevance</h1>
@@LIVE@@
<div id="loading" class="sub" hidden><b>Loading today's filings...</b> Checking the SEC. This can take a minute or two, and the list will appear here when it's done.</div>
<div id="list">
<div class="sub">Updated @@GENERATED@@ &middot; @@N@@ filings &middot; @@NEW@@ new in the latest run</div>
<div class="sub">@@RUNS@@</div>
<div class="bar">@@BAR@@</div>
<div class="sub"><b>@@H@@ High</b> &middot; <b>@@M@@ Medium</b> &middot; <b>@@L@@ Low</b></div>
<div class="tools" id="filters"><button class="on" data-f="All">All</button><button data-f="High">High</button><button data-f="Medium">Medium</button><button data-f="Low">Low</button>
<input id="q" placeholder="Search titles..." autocomplete="off"><span id="cnt"></span></div>
<table id="t"><thead><tr><th data-k="0">Date</th><th data-k="1">Status</th><th data-k="2">Filing (click to open)</th><th data-k="3">Relevance</th><th data-k="4">Why</th></tr></thead><tbody>@@BODY@@</tbody></table>
</div>
<script>
var f="All",rows=[].slice.call(document.querySelectorAll("#t tbody tr"));
function go(){var q=document.getElementById("q").value.toLowerCase(),n=0;rows.forEach(function(r){var ok=(f=="All"||r.dataset.l==f)&&r.textContent.toLowerCase().indexOf(q)>-1;r.style.display=ok?"":"none";if(ok)n++});document.getElementById("cnt").textContent=n+" shown"}
document.querySelectorAll("#filters button").forEach(function(b){b.onclick=function(){f=b.dataset.f;document.querySelectorAll("#filters button").forEach(function(x){x.classList.toggle("on",x==b)});go()}});
document.getElementById("q").oninput=go;
var asc={};document.querySelectorAll("th").forEach(function(h){h.onclick=function(){var k=+h.dataset.k;asc[k]=!asc[k];var order={High:0,Medium:1,Low:2};rows.sort(function(a,b){var x=a.cells[k].textContent,y=b.cells[k].textContent;if(k==3){x=order[x];y=order[y]}return (x>y?1:x<y?-1:0)*(asc[k]?1:-1)});var tb=document.querySelector("#t tbody");rows.forEach(function(r){tb.appendChild(r)})}});
go();
</script>
@@LIVEJS@@
</body></html>"""
