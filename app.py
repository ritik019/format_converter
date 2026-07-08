"""Single freebie web app: a public sheet -> CSV converter plus the ControlGrid
deal-ingestion flow (preview -> execute -> resolve conflicts), inherited from the
freebie-product webapp.

Two independent sections share one app:
  * Converter ( / , /convert )       -- transform a planning sheet / CSV into the
                                        freebie split CSV. No ControlGrid access.
  * Deal ingestion ( /deals, /preview,
    /execute, /resolve )             -- upload a FREEBIE_DEALS CSV, preview the
                                        plan, confirm to create/edit rules, and
                                        resolve FSN x warehouse conflicts.

The converter logic lives in ``convert_split`` and is deliberately left untouched.

Run locally:  uvicorn app:app --port 8004
Deploy:       uvicorn app:app --host 0.0.0.0 --port $PORT
Subpath-safe: every form action is relative, so it works under an nginx subpath.
"""
import base64
import html
import json
from datetime import datetime
from typing import List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response, RedirectResponse

try:  # works inside the freebie package and as a flat deploy repo (files at root)
    from freebie.config import Config
    from freebie.auth import make_post_fn
    from freebie import convert_split
    from freebie.sheet import rows_from_csv_bytes, parse_and_validate, parse_cohort_ids
    from freebie.controlgrid import ControlGrid
    from freebie.planner import build_plan, render_preview, execute_plan, filter_plan
    from freebie.conflicts import collect_conflicts
    from freebie.models import ValidationError
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from config import Config
    from auth import make_post_fn
    import convert_split
    from sheet import rows_from_csv_bytes, parse_and_validate, parse_cohort_ids
    from controlgrid import ControlGrid
    from planner import build_plan, render_preview, execute_plan, filter_plan
    from conflicts import collect_conflicts
    from models import ValidationError

try:
    from freebie.timeutil import IST
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from timeutil import IST

app = FastAPI(title="Freebie Deals Automation")

_STYLE = """
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
      margin:0;background:#f5f6f8;color:#1f2328;line-height:1.45;font-size:14px}
 .topbar{position:sticky;top:0;z-index:30;background:#fff;border-bottom:1px solid #e6e8eb}
 .topbar .wrap{max-width:960px;margin:0 auto;padding:0 20px;height:56px;display:flex;align-items:center;gap:16px}
 .brand{font-weight:700;font-size:15px;color:#0a7d33;text-decoration:none;display:flex;align-items:center;gap:9px}
 .brand .dot{width:11px;height:11px;border-radius:4px;background:#0a7d33;display:inline-block}
 .nav{display:flex;gap:4px;margin-left:auto}
 .nav a{padding:7px 13px;border-radius:8px;color:#374151;text-decoration:none;font-size:14px;font-weight:500}
 .nav a:hover{background:#f0f1f3}
 .nav a.active{background:#eaf5ec;color:#0a7d33}
 .container{max-width:960px;margin:26px auto 60px;padding:0 20px}
 .card{background:#fff;border:1px solid #e6e8eb;border-radius:12px;padding:22px 24px;
       box-shadow:0 1px 2px rgba(16,24,40,.04);margin-bottom:18px}
 h1{font-size:20px;margin:0 0 6px} h2{font-size:16px;margin:0 0 8px}
 .sub{color:#6b7280;font-size:13px;margin:0 0 8px}
 label{display:block;font-weight:600;font-size:13px;margin:16px 0 6px;color:#374151}
 input[type=text],input[type=file],input[type=datetime-local],textarea,select{width:100%;padding:9px 11px;
       border:1px solid #d4d7dc;border-radius:8px;font:inherit;background:#fff;color:#1f2328}
 input:focus,textarea:focus,select:focus{outline:none;border-color:#0a7d33;box-shadow:0 0 0 3px rgba(10,125,51,.12)}
 textarea{min-height:60px;resize:vertical}
 .field-row{display:flex;gap:14px;flex-wrap:wrap}
 .field-row .field-col{flex:1;min-width:200px}
 .btn{display:inline-flex;align-items:center;gap:7px;margin-top:18px;background:#111827;color:#fff;border:0;
      padding:10px 16px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
 .btn:hover{opacity:.93}
 .btn.go{background:#0a7d33} .btn.danger{background:#b42318}
 code{background:#f1f2f4;padding:1.5px 6px;border-radius:5px;font-size:12.5px}
 pre{background:#0f172a;color:#e2e8f0;border-radius:10px;padding:16px;white-space:pre-wrap;
     font-size:12.5px;overflow:auto;max-height:430px;margin:14px 0 0}
 .err{background:#fef3f2;border:1px solid #fecdc9;color:#b42318;border-radius:10px;padding:12px 14px;margin-bottom:10px}
 .ok{background:#ecfdf3;border:1px solid #bbf2cd;color:#067647;border-radius:10px;padding:12px 14px;margin-bottom:10px}
 .warn{background:#fffaeb;border:1px solid #fde68a;color:#92400e;border-radius:10px;padding:12px 14px;margin-bottom:10px}
 .muted{color:#6b7280;font-size:13px}
 a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
 .backlink{display:inline-block;font-size:13px}
 ul{margin:8px 0 0;padding-left:20px}
 details.adv{margin-top:16px;border-top:1px dashed #e6e8eb;padding-top:10px}
 details.adv>summary{cursor:pointer;font-size:13px;color:#374151;font-weight:600;list-style:none}
 details.adv>summary::-webkit-details-marker{display:none}
 details.adv>summary:before{content:'\\25B8\\00a0';color:#9aa1ab}
 details.adv[open]>summary:before{content:'\\25BE\\00a0'}
 .stats{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 2px}
 .stat{background:#f6f7f9;border:1px solid #e6e8eb;border-radius:9px;padding:8px 14px;font-size:12px;color:#6b7280;text-align:center}
 .stat b{font-size:18px;display:block;color:#1f2328;font-weight:700}
 .stat.bad b{color:#b42318}
 .toolbar{position:sticky;top:56px;z-index:10;background:#fff;display:flex;gap:10px;align-items:center;
          flex-wrap:wrap;padding:12px 0;border-bottom:1px solid #eceef1}
 .toolbar input[type=text]{flex:1;min-width:200px}
 .toolbar select{width:auto;min-width:130px}
 .toolbar .selall{display:flex;align-items:center;gap:6px;font-size:13px;color:#374151;white-space:nowrap;font-weight:600}
 .toolbar .selall input{width:auto;margin:0}
 .toolbar .btn{margin-top:0;margin-left:auto}
 .toolbar .count{font-size:12px;color:#6b7280;white-space:nowrap}
 .tablewrap{max-height:62vh;overflow:auto;border:1px solid #e6e8eb;border-radius:10px;margin-top:14px}
 table.grid{width:100%;border-collapse:separate;border-spacing:0;font-size:13px}
 table.grid th,table.grid td{border-bottom:1px solid #eceef1;padding:10px 12px;text-align:left;vertical-align:top}
 table.grid tbody tr:last-child td{border-bottom:0}
 table.grid thead th{position:sticky;top:0;background:#f6f7f9;z-index:1;font-size:11px;
       text-transform:uppercase;letter-spacing:.04em;color:#6b7280}
 table.grid tbody tr:hover{background:#fafbfc}
 table.grid td:first-child,table.grid th:first-child{text-align:center;width:46px}
 table.grid input[type=checkbox]{width:auto;margin:0}
 .rname{font-weight:600;color:#1f2328}
 .rid{font-size:11.5px}
 .meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
 .chip{display:inline-flex;align-items:center;background:#f1f2f4;color:#374151;border-radius:999px;
       padding:2px 9px;font-size:11.5px;line-height:1.7;white-space:nowrap}
 .chip.active{background:#ecfdf3;color:#067647}
 .chip.paused{background:#fffaeb;color:#92400e}
 .chip.cohort{background:#eef2ff;color:#3730a3;cursor:default}
 .chip.none{background:#f4f5f6;color:#9aa1ab}
 .pairs{font-size:12.5px;color:#374151;white-space:nowrap}
</style>
"""

RULE_TYPE_LABELS = {
    "FREEBIE_DEALS": "Freebie Deal",
    "FREEBIE_CART_VALUE": "Freebie Cart Value",
}


def _nav(active):
    def link(href, label, key):
        cls = ' class="active"' if active == key else ""
        return f'<a href="{href}"{cls}>{label}</a>'
    return link(".", "Converter", "convert") + link("deals", "Deals", "deals")


def _page(body, active=""):
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Freebie Console</title>{_STYLE}</head><body>"
        "<div class='topbar'><div class='wrap'>"
        "<a class='brand' href='.'><span class='dot'></span>Freebie Console</a>"
        f"<nav class='nav'>{_nav(active)}</nav></div></div>"
        f"<div class='container'>{body}</div></body></html>")


# --------------------------------------------------------------------------- #
# Converter section (transform a planning sheet / CSV -> freebie split CSV)
# --------------------------------------------------------------------------- #

def _convert_form(message=""):
    blurb = (
        "Paste a link to the planning sheet (merged-header layout with "
        "<b>mov / warehouse / rule_name_and_description / fcn</b> columns), or upload a CSV. "
        "Each warehouse line carries a quantity (<code>FCHHYDTEL01&nbsp;&nbsp;16</code>): "
        "quantity &gt; 5 &rarr; PARTIAL with <b>split_number</b> = qty; quantity &le; 5 or "
        "none &rarr; FULL. <code>all</code> / <code>all ch</code> &rarr; every channel as PARTIAL 20.")
    return f"""
    <div class="card">
      <h1>Sheet &rarr; CSV converter</h1>
      <p class="sub">{blurb}</p>
      {message}
      <form action="convert" method="post" enctype="multipart/form-data">
        <label>Google Sheet link <span class="muted">(must be 'Anyone with the link can view')</span></label>
        <input type="text" name="sheet_url" placeholder="https://docs.google.com/spreadsheets/d/.../edit?gid=...">
        <label>&hellip; or upload a CSV instead</label>
        <input type="file" name="file" accept=".csv">
        <label>When the sheet says <code>all</code>, apply to</label>
        <select name="all_region">
          <option value="all">All channels (BLR + HYD)</option>
          <option value="blr">BLR only</option>
          <option value="hyd">HYD only</option>
        </select>
        <div class="field-row">
          <div class="field-col">
            <label>Start date &amp; time <span class="muted">(optional &mdash; defaults to today, midnight)</span></label>
            <input type="datetime-local" name="start_date">
          </div>
          <div class="field-col">
            <label>End date &amp; time <span class="muted">(optional &mdash; defaults to start + 1 day)</span></label>
            <input type="datetime-local" name="end_date">
          </div>
        </div>
        <button class="btn go" type="submit">Convert &amp; download</button>
      </form>
    </div>
    """


@app.get("/", response_class=HTMLResponse)
def index():
    return _page(_convert_form(), active="convert")


@app.get("/convert")
def convert_get():
    # The converter form POSTs here; a browser GET just bounces to the form.
    return RedirectResponse(url=".")


@app.post("/convert")
async def convert(file: UploadFile = File(None), sheet_url: str = Form(""),
                  all_region: str = Form("all"), start_date: str = Form(""),
                  end_date: str = Form("")):
    sheet_url = (sheet_url or "").strip()
    try:
        if sheet_url:
            out_bytes, warnings = convert_split.convert_from_sheet(
                sheet_url, all_region=all_region, start_date=start_date, end_date=end_date)
            name = "sheet"
        elif file is not None:
            out_bytes, warnings = convert_split.convert_csv_bytes(
                await file.read(), all_region=all_region, start_date=start_date, end_date=end_date)
            name = (file.filename or "sheet").rsplit(".", 1)[0]
        else:
            return _page(_convert_form('<div class="err">Provide a Google Sheet link or upload a CSV.</div>'),
                         active="convert")
    except Exception as ex:  # noqa: BLE001
        return _page(_convert_form(f'<div class="err">Could not convert: {html.escape(str(ex))}</div>'),
                     active="convert")

    headers = {"Content-Disposition": f'attachment; filename="{name}_freebie_split.csv"'}
    if warnings:
        headers["X-Convert-Warnings"] = str(len(warnings))
    return Response(content=out_bytes, media_type="text/csv", headers=headers)


# --------------------------------------------------------------------------- #
# Deal ingestion section (inherited from freebie-product webapp)
# --------------------------------------------------------------------------- #

def _deals_form(message=""):
    return f"""
    <div class="card">
      <h1>Create / edit deals</h1>
      <p class="sub">Upload the FREEBIE_DEALS CSV (same columns as the template). Optionally list
      cohort IDs to exclude from <b>every</b> rule (a NOT_IN hurdle on new rules).</p>
      {message}
      <form action="preview" method="post" enctype="multipart/form-data">
        <label>Rules CSV</label>
        <input type="file" name="file" accept=".csv" required>
        <label>Excluded cohort IDs <span class="muted">(comma / space / newline separated — optional)</span></label>
        <textarea name="cohort_ids" placeholder="cohort_123, cohort_456"></textarea>
        <label>Created / edited by <span class="muted">(your name or email — stamped on every rule you create, edit or delete; defaults to the server account if blank)</span></label>
        <input type="text" name="actor" placeholder="e.g. sakshyat.pradhan@firstclub.co.in">
        <details class="adv">
          <summary>Advanced &mdash; run with your own ControlGrid login</summary>
          <p class="muted" style="margin-top:8px">The server's environment login is used by default.
          If it has expired, paste your own Cognito ID token &mdash; from the Network tab at
          portal.controlgrid.in, the <code>Authorization: Bearer …</code> header on any
          rules-management request &mdash; to run it as yourself.</p>
          <label>Login token <span class="muted">(optional)</span></label>
          <textarea name="auth_token" placeholder="leave blank to use the server login"></textarea>
        </details>
        <button class="btn go" type="submit">Preview plan</button>
      </form>
    </div>
    """


def _failed_block(result):
    if not result["failed"]:
        return ""
    items = "".join(f"<li>{html.escape(k)} {html.escape(str(i))}: {html.escape(e)}</li>"
                    for k, i, e in result["failed"])
    return f'<div class="err"><b>Failures:</b><ul>{items}</ul></div>'


def _retry_set(result):
    """Failed item identifiers, split by kind, for a targeted re-run."""
    return {
        "edits": [str(i) for k, i, _e in result["failed"] if k == "edit"],
        "creates": [i for k, i, _e in result["failed"] if k == "create"],
    }


# Audit fields aren't referenced elsewhere in the codebase and ControlGrid's exact
# key names aren't documented here, so probe a list of likely names and use the
# first present value. Missing fields degrade to "N/A" rather than erroring.
_CREATED_BY = ["createdBy", "created_by", "createdByEmail", "createdByName",
               "creator", "createdUser"]
_CREATED_AT = ["createdAt", "created_at", "createdDate", "createdOn", "createdTime",
               "creationTime", "createdDateTime"]
_UPDATED_BY = ["updatedBy", "updated_by", "lastModifiedBy", "modifiedBy",
               "lastUpdatedBy", "updatedByEmail", "editedBy"]
_UPDATED_AT = ["updatedAt", "updated_at", "lastModifiedDate", "modifiedAt",
               "lastModifiedAt", "lastUpdatedAt", "updatedDate", "modifiedDate",
               "updatedOn", "lastModifiedDateTime"]


def _fmt_ms(value):
    """Epoch-millis -> 'YYYY-MM-DD HH:MM IST', or '' if not a usable timestamp."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%Y-%m-%d %H:%M") + " IST"


def _fmt_when(value):
    """Audit timestamp that may be epoch-millis (int/numeric) or an ISO-ish string."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return _fmt_ms(value)
    s = str(value).strip()
    return _fmt_ms(s) if s.isdigit() else s


def _first(raw, keys):
    for k in keys:
        v = raw.get(k)
        if v not in (None, ""):
            return v
    return ""


def _cohort_ids_of(hurdle_list):
    """All cohort IDs excluded via NOT_IN USER_COHORT hurdles on the rule."""
    ids = []
    for h in (hurdle_list or []):
        if h.get("type") == "USER_COHORT" and h.get("operation") == "NOT_IN":
            ids.extend(h.get("value") or h.get("requiredCohortIds") or [])
    return ids


def _chip(text, cls=""):
    c = f" {cls}" if cls else ""
    return f'<span class="chip{c}">{text}</span>'


def _conflict_summary(info):
    """Build the compact chip row + searchable text for one conflicting rule."""
    raw = info.get("raw") or {}
    chips, search = [], []

    status = (info.get("ruleStatus") or "").strip()
    if status:
        cls = "active" if status.upper() == "ACTIVE" else "paused"
        chips.append(_chip(html.escape(status), cls))
        search.append(status)

    st, en = _fmt_ms(info.get("startTime")), _fmt_ms(info.get("endTime"))
    if st or en:
        chips.append(_chip(f"{html.escape(st or '?')} &rarr; {html.escape(en or '?')}"))

    c_by, c_at = _first(raw, _CREATED_BY), _fmt_when(_first(raw, _CREATED_AT))
    u_by, u_at = _first(raw, _UPDATED_BY), _fmt_when(_first(raw, _UPDATED_AT))
    if c_by or c_at:
        t = f"Created {html.escape(str(c_by))}" if c_by else "Created"
        if c_at:
            t += f" &middot; {html.escape(c_at)}"
        chips.append(_chip(t))
        search.append(str(c_by))
    if u_by or u_at:
        t = f"Edited {html.escape(str(u_by))}" if u_by else "Edited"
        if u_at:
            t += f" &middot; {html.escape(u_at)}"
        chips.append(_chip(t))
        search.append(str(u_by))
    if not (c_by or c_at or u_by or u_at):
        chips.append(_chip("created / edited info N/A", "none"))

    cohorts = _cohort_ids_of(info.get("hurdleList"))
    if cohorts:
        joined = ", ".join(map(str, cohorts))
        chips.append(f'<span class="chip cohort" title="{html.escape(joined)}">'
                     f'Cohorts ({len(cohorts)}): {html.escape(joined)}</span>')
        search.extend(map(str, cohorts))
    else:
        chips.append(_chip("no cohorts", "none"))

    return f'<div class="meta">{"".join(chips)}</div>', " ".join(search)


_CONFLICT_JS = """
<script>
(function(){
  var box = document.getElementById('cf-box');
  if(!box) return;
  var search = box.querySelector('#cf-search');
  var typeSel = box.querySelector('#cf-type');
  var selAll = box.querySelector('#cf-selall');
  var count = box.querySelector('#cf-count');
  function rows(){ return Array.prototype.slice.call(box.querySelectorAll('tbody tr')); }
  function isVisible(tr){ return tr.style.display !== 'none'; }
  function cbOf(tr){ return tr.querySelector('input[type=checkbox]'); }
  function applyFilter(){
    var q = (search ? search.value : '').toLowerCase();
    var t = typeSel ? typeSel.value : '';
    rows().forEach(function(tr){
      var txt = (tr.getAttribute('data-text')||'');
      var ty = (tr.getAttribute('data-type')||'');
      var ok = (!q || txt.indexOf(q) !== -1) && (!t || ty === t);
      tr.style.display = ok ? '' : 'none';
    });
    syncSelAll();
  }
  function syncSelAll(){
    var vis = rows().filter(isVisible);
    var checked = vis.filter(function(tr){ return cbOf(tr).checked; });
    if(selAll){
      selAll.checked = vis.length > 0 && checked.length === vis.length;
      selAll.indeterminate = checked.length > 0 && checked.length < vis.length;
    }
    if(count) count.textContent = checked.length + ' selected / ' + vis.length + ' shown';
  }
  if(selAll) selAll.addEventListener('change', function(){
    rows().filter(isVisible).forEach(function(tr){ cbOf(tr).checked = selAll.checked; });
    syncSelAll();
  });
  box.addEventListener('change', function(e){
    if(e.target && e.target.matches('tbody input[type=checkbox]')) syncSelAll();
  });
  if(search) search.addEventListener('input', applyFilter);
  if(typeSel) typeSel.addEventListener('change', applyFilter);
  syncSelAll();
})();
</script>
"""


def _conflict_section(conflicts, csv_b64, cohort_ids, retry, auth_token="", actor=""):
    """Render the checkbox table of conflicting rules + the re-run form. The
    failed items to retry travel along (base64 JSON) so /resolve re-runs only those.
    Any pasted cookies ride along too, so the re-run uses the same login. Each row
    carries a compact summary (status / window / created / edited / cohorts); a sticky
    toolbar holds the text + type filters, a select-all toggle, and the action button."""
    if not conflicts:
        return ""
    seen_types = []
    rows = []
    for c in conflicts:
        rule_type = RULE_TYPE_LABELS.get(c["rule_type"], c["rule_type"] or "—")
        if rule_type not in seen_types:
            seen_types.append(rule_type)
        pairs = "<br>".join(f"{html.escape(f)} &times; {html.escape(w)}" for f, w in c["pairs"])
        summary_html, summary_text = _conflict_summary(c.get("info") or {})
        data_text = " ".join([
            c.get("name") or "", c["id"], rule_type, summary_text,
            " ".join(f"{f} {w}" for f, w in c["pairs"]),
        ]).lower()
        rows.append(
            f"<tr data-type=\"{html.escape(rule_type)}\" data-text=\"{html.escape(data_text)}\">"
            f"<td><input type='checkbox' name='delete_ids' value='{html.escape(c['id'])}'></td>"
            f"<td><div class='rname'>{html.escape(c['name'] or '')}</div>"
            f"<div class='rid'><code>{html.escape(c['id'])}</code></div>"
            f"{summary_html}</td>"
            f"<td>{_chip(html.escape(rule_type))}</td>"
            f"<td class='pairs'>{pairs}</td>"
            "</tr>")
    type_options = "".join(f'<option value="{html.escape(t)}">{html.escape(t)}</option>'
                           for t in seen_types)
    retry_b64 = base64.b64encode(json.dumps(retry).encode()).decode()
    table = f"""
    <div id="cf-box" class="card">
    <h2>Resolve conflicts</h2>
    <p class="sub">These existing rules already own the FSN &times; warehouse pairs your rules
    need, so the create/edit was rejected. Tick the rules that are <b>safe to delete</b>
    &mdash; anything left unticked is not touched &mdash; then re-run only the failed items.</p>
    <form action="resolve" method="post">
      <input type="hidden" name="csv_b64" value="{csv_b64}">
      <input type="hidden" name="cohort_ids" value="{html.escape(cohort_ids)}">
      <input type="hidden" name="retry_b64" value="{retry_b64}">
      <input type="hidden" name="actor" value="{html.escape(actor)}">
      <input type="hidden" name="auth_token" value="{html.escape(auth_token)}">
      <div class="toolbar">
        <input type="text" id="cf-search" placeholder="Filter by name / id / FSN / warehouse / cohort / editor…">
        <select id="cf-type">
          <option value="">All types</option>
          {type_options}
        </select>
        <label class="selall"><input type="checkbox" id="cf-selall"> Select all shown</label>
        <span class="count" id="cf-count"></span>
        <button class="btn danger" type="submit">Delete selected &amp; re-run</button>
      </div>
      <div class="tablewrap">
        <table class="grid">
          <thead><tr>
            <th>Del</th><th>Rule &amp; summary</th><th>Type</th><th>FSN &times; Warehouse</th>
          </tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </form>
    </div>
    """
    return table + _CONFLICT_JS


def _resolve_auth(form_token):
    """Form-provided token wins; otherwise fall back to the server env login."""
    cfg = Config()
    token = (form_token or "").strip() or cfg.auth_token
    return cfg, token


def _get_client(form_token=""):
    """Returns (cfg, client) on success, or (None, error_html). A pasted token
    takes precedence over the server's FREEBIE_AUTH_TOKEN env login."""
    cfg, token = _resolve_auth(form_token)
    if not token:
        return None, ('<div class="err">No ControlGrid login available. Set '
                      'FREEBIE_AUTH_TOKEN in the service environment, or paste '
                      'your login token under <b>Advanced</b>.</div>')
    return cfg, ControlGrid(make_post_fn(token))


@app.get("/deals", response_class=HTMLResponse)
def deals_form():
    return _page(_deals_form(), active="deals")


@app.post("/preview", response_class=HTMLResponse)
async def preview(file: UploadFile = File(...), cohort_ids: str = Form(""),
                  actor: str = Form(""),
                  auth_token: str = Form("")):
    data = await file.read()
    cohorts = parse_cohort_ids(cohort_ids)
    try:
        rows = rows_from_csv_bytes(data)
        targets = parse_and_validate(rows)
    except ValidationError as e:
        items = "".join(f"<li>{html.escape(m)}</li>" for m in e.messages)
        return _page(_deals_form(f'<div class="err"><b>Validation failed:</b><ul>{items}</ul></div>'),
                     active="deals")
    except Exception as ex:  # noqa: BLE001
        return _page(_deals_form(f'<div class="err">Could not read CSV: {html.escape(str(ex))}</div>'),
                     active="deals")

    cfg, client = _get_client(auth_token)
    if cfg is None:
        return _page(_deals_form(client), active="deals")

    try:
        existing = client.fetch_all()
    except Exception as ex:  # noqa: BLE001
        return _page(_deals_form(f'<div class="err">Failed to fetch existing rules '
                                 f'(login token may have expired): {html.escape(str(ex))}</div>'),
                     active="deals")

    email = (actor or "").strip() or cfg.email
    plan = build_plan(targets, existing, email, cohorts)
    csv_b64 = base64.b64encode(data).decode()
    body = f"""
    <div class="card">
      <h1>Preview</h1>
      <p class="sub">Nothing has been written yet &mdash; review the plan, then confirm.</p>
      <div class="stats">
        <div class="stat"><b>{len(targets)}</b>parsed</div>
        <div class="stat"><b>{len(existing)}</b>existing</div>
        <div class="stat"><b>{len(plan.edits)}</b>edits</div>
        <div class="stat"><b>{len(plan.creates)}</b>creates</div>
      </div>
      <pre>{html.escape(render_preview(plan))}</pre>
      <form action="execute" method="post">
        <input type="hidden" name="csv_b64" value="{csv_b64}">
        <input type="hidden" name="cohort_ids" value="{html.escape(cohort_ids)}">
        <input type="hidden" name="actor" value="{html.escape(actor)}">
        <input type="hidden" name="auth_token" value="{html.escape(auth_token)}">
        <button class="btn go" type="submit">Confirm &amp; execute ({len(plan.edits)} edits, {len(plan.creates)} creates)</button>
      </form>
    </div>
    <a class="backlink" href="deals">&larr; Start over</a>
    """
    return _page(body, active="deals")


@app.post("/execute", response_class=HTMLResponse)
def execute(csv_b64: str = Form(...), cohort_ids: str = Form(""), actor: str = Form(""),
            auth_token: str = Form("")):
    data = base64.b64decode(csv_b64)
    cohorts = parse_cohort_ids(cohort_ids)
    try:
        targets = parse_and_validate(rows_from_csv_bytes(data))
    except ValidationError as e:
        items = "".join(f"<li>{html.escape(m)}</li>" for m in e.messages)
        return _page(_deals_form(f'<div class="err"><b>Validation failed:</b><ul>{items}</ul></div>'),
                     active="deals")
    except Exception as ex:  # noqa: BLE001
        return _page(_deals_form(f'<div class="err">Could not read CSV: {html.escape(str(ex))}</div>'),
                     active="deals")

    cfg, client = _get_client(auth_token)
    if cfg is None:
        return _page(_deals_form(client), active="deals")

    try:
        existing = client.fetch_all()
    except Exception as ex:  # noqa: BLE001
        return _page(_deals_form(f'<div class="err">Failed to fetch existing rules '
                                 f'(login token may have expired): {html.escape(str(ex))}</div>'),
                     active="deals")

    email = (actor or "").strip() or cfg.email
    plan = build_plan(targets, existing, email, cohorts)
    try:
        result = execute_plan(plan, client)
    except Exception as ex:  # noqa: BLE001
        return _page(_deals_form(f'<div class="err">Execution failed '
                                 f'(login may have expired): {html.escape(str(ex))}</div>'),
                     active="deals")

    try:
        conflicts = collect_conflicts(result["failed"], client.fetch_index())
    except Exception:  # noqa: BLE001 - conflict lookup is best-effort
        conflicts = []
    conflict_section = _conflict_section(conflicts, csv_b64, cohort_ids, _retry_set(result),
                                         auth_token, actor)
    failed_cls = " bad" if result["failed"] else ""
    body = f"""
    <div class="card">
      <h1>Done</h1>
      <div class="stats">
        <div class="stat"><b>{len(result['edited'])}</b>edited</div>
        <div class="stat"><b>{len(result['created'])}</b>created</div>
        <div class="stat{failed_cls}"><b>{len(result['failed'])}</b>failed</div>
      </div>
      {_failed_block(result)}
    </div>
    {conflict_section}
    <a class="backlink" href="deals">&larr; Run another</a>
    """
    return _page(body, active="deals")


@app.post("/resolve", response_class=HTMLResponse)
def resolve(csv_b64: str = Form(...), cohort_ids: str = Form(""), actor: str = Form(""),
            retry_b64: str = Form(...), delete_ids: List[str] = Form(default=[]),
            auth_token: str = Form("")):
    """Delete the user-selected conflicting rules, then re-run only the items that
    previously failed. Loops back to the resolve UI if conflicts remain."""
    data = base64.b64decode(csv_b64)
    cohorts = parse_cohort_ids(cohort_ids)
    try:
        retry = json.loads(base64.b64decode(retry_b64).decode())
    except Exception:  # noqa: BLE001
        retry = {"edits": [], "creates": []}
    try:
        targets = parse_and_validate(rows_from_csv_bytes(data))
    except ValidationError as e:
        items = "".join(f"<li>{html.escape(m)}</li>" for m in e.messages)
        return _page(_deals_form(f'<div class="err"><b>Validation failed:</b><ul>{items}</ul></div>'),
                     active="deals")
    except Exception as ex:  # noqa: BLE001
        return _page(_deals_form(f'<div class="err">Could not read CSV: {html.escape(str(ex))}</div>'),
                     active="deals")

    cfg, client = _get_client(auth_token)
    if cfg is None:
        return _page(_deals_form(client), active="deals")

    email = (actor or "").strip() or cfg.email
    deleted, delete_failed = [], []
    for rid in delete_ids:
        try:
            client.delete_rule(rid, email)
            deleted.append(rid)
        except Exception as ex:  # noqa: BLE001 - report and continue
            delete_failed.append((rid, str(ex)))

    try:
        existing = client.fetch_all()
    except Exception as ex:  # noqa: BLE001
        return _page(_deals_form(f'<div class="err">Failed to fetch existing rules '
                                 f'(login token may have expired): {html.escape(str(ex))}</div>'),
                     active="deals")

    plan = build_plan(targets, existing, email, cohorts)
    plan = filter_plan(plan, set(retry.get("edits", [])), set(retry.get("creates", [])))
    try:
        result = execute_plan(plan, client)
    except Exception as ex:  # noqa: BLE001
        return _page(_deals_form(f'<div class="err">Re-run failed '
                                 f'(login may have expired): {html.escape(str(ex))}</div>'),
                     active="deals")

    try:
        conflicts = collect_conflicts(result["failed"], client.fetch_index())
    except Exception:  # noqa: BLE001 - conflict lookup is best-effort
        conflicts = []
    conflict_section = _conflict_section(conflicts, csv_b64, cohort_ids, _retry_set(result),
                                         auth_token, actor)

    del_block = ""
    if deleted:
        ids = ", ".join(f"<code>{html.escape(d)}</code>" for d in deleted)
        del_block += f'<div class="ok">Deleted {len(deleted)} conflicting rule(s): {ids}</div>'
    if delete_failed:
        items = "".join(f"<li><code>{html.escape(i)}</code>: {html.escape(e)}</li>"
                        for i, e in delete_failed)
        del_block += f'<div class="err"><b>Could not delete:</b><ul>{items}</ul></div>'

    failed_cls = " bad" if result["failed"] else ""
    body = f"""
    <div class="card">
      <h1>Re-run complete</h1>
      {del_block}
      <div class="stats">
        <div class="stat"><b>{len(result['edited'])}</b>edited</div>
        <div class="stat"><b>{len(result['created'])}</b>created</div>
        <div class="stat{failed_cls}"><b>{len(result['failed'])}</b>still failing</div>
      </div>
      {_failed_block(result)}
    </div>
    {conflict_section}
    <a class="backlink" href="deals">&larr; Run another</a>
    """
    return _page(body, active="deals")


# Browsers that GET a POST-only ingestion route bounce back to the deals form.
@app.get("/preview")
@app.get("/execute")
@app.get("/resolve")
def deals_get():
    return RedirectResponse(url="deals")


@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}
