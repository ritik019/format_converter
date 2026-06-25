"""Standalone PUBLIC converter app (converter only — no ControlGrid access).

Exposes just the planning-sheet -> freebie CSV conversion, so it is safe to
deploy publicly. It deliberately does NOT import the deal-creation code
(auth / controlgrid / planner), so there is no way to create or edit real deals
through this app.

Run locally:  uvicorn app:app --port 8004
Deploy:       uvicorn app:app --host 0.0.0.0 --port $PORT
"""
import base64
import html
import os

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response

try:  # works as part of the freebie package and as a flat deploy repo
    from freebie import convert_split
    from freebie import dealupload
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    import convert_split
    import dealupload

app = FastAPI(title="Freebie Sheet Converter")

_STYLE = """
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:920px;margin:32px auto;padding:0 16px;color:#1a1a1a}
 h1{font-size:22px}
 label{display:block;font-weight:600;margin:16px 0 6px}
 input[type=text],input[type=file]{width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;font:inherit}
 button{margin-top:18px;background:#0a7d33;color:#fff;border:0;padding:10px 18px;border-radius:6px;font-size:15px;cursor:pointer}
 code{background:#f0f0f0;padding:1px 5px;border-radius:4px}
 .err{background:#fff3f3;border:1px solid #f0caca;color:#a40000;border-radius:6px;padding:12px}
 .ok{background:#f1faf2;border:1px solid #bfe3c6;border-radius:6px;padding:12px}
 .warn{background:#fff8e6;border:1px solid #f0e0a0;color:#7a5b00;border-radius:6px;padding:12px;margin:8px 0}
 pre{background:#f6f6f6;border:1px solid #e3e3e3;border-radius:6px;padding:14px;white-space:pre-wrap;font-size:13px}
 .muted{color:#666;font-size:13px}
 a{color:#0a58ca}
</style>
"""


def _page(body):
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Freebie Sheet Converter</title>{_STYLE}</head><body>{body}</body></html>")


def _form(message=""):
    blurb = (
        "Paste the planning sheet link (merged-header layout with "
        "<b>mov / warehouse / rule_name_and_description / fcn</b> columns), or upload a CSV. "
        "Each warehouse line carries a quantity (<code>FCHHYDTEL01&nbsp;&nbsp;16</code>): "
        "quantity &gt; 5 &rarr; PARTIAL with <b>split_number</b> = qty; quantity &le; 5 or "
        "none &rarr; FULL. <code>all</code> / <code>all ch</code> &rarr; every channel as "
        "PARTIAL 20. One filled row per warehouse.")
    return f"""
    <h1>Freebie sheet &rarr; CSV converter</h1>
    <p class="muted">{blurb}</p>
    {message}
    <form action="convert" method="post" enctype="multipart/form-data">
      <label>Google Sheet link <span class="muted">(must be 'Anyone with the link can view')</span></label>
      <input type="text" name="sheet_url" placeholder="https://docs.google.com/spreadsheets/d/.../edit?gid=...">
      <label>&hellip; or upload a CSV instead</label>
      <input type="file" name="file" accept=".csv">
      <button type="submit">Convert &amp; download</button>
    </form>
    """


def _upload_form(message=""):
    return f"""
    <h1>Upload converted CSV &rarr; create deals</h1>
    <div class="warn"><b>This creates real FREEBIE_DEALS in ControlGrid.</b> Preview first; nothing
    is created until you confirm.</div>
    {message}
    <form action="upload-preview" method="post" enctype="multipart/form-data">
      <label>Converted CSV <span class="muted">(the file downloaded from the converter)</span></label>
      <input type="file" name="file" accept=".csv" required>
      <p class="muted">By default the server's ControlGrid login is used. If it has expired,
      paste your own cookies (from your logged-in browser at portal.controlgrid.in) to run it as yourself.</p>
      <label>SESSION cookie <span class="muted">(optional)</span></label>
      <input type="text" name="session_cookie" placeholder="leave blank to use the server login">
      <label>XSRF-TOKEN cookie <span class="muted">(optional)</span></label>
      <input type="text" name="xsrf_token" placeholder="leave blank to use the server login">
      <button type="submit">Preview deals</button>
    </form>
    <p style="margin-top:14px"><a href=".">&larr; Back to converter</a></p>
    """


def _resolve_auth(form_session, form_xsrf):
    """Form-provided cookies win; otherwise fall back to the server env login."""
    session = (form_session or "").strip() or os.environ.get("FREEBIE_SESSION", "")
    xsrf = (form_xsrf or "").strip() or os.environ.get("FREEBIE_XSRF", "")
    return session, xsrf


@app.get("/", response_class=HTMLResponse)
def index():
    link = '<p><a href="upload">&rarr; Upload a converted CSV to create the deals in ControlGrid</a></p>'
    return _page(_form() + link)


@app.get("/upload", response_class=HTMLResponse)
def upload_form():
    return _page(_upload_form())


@app.post("/upload-preview", response_class=HTMLResponse)
async def upload_preview(file: UploadFile = File(...),
                         session_cookie: str = Form(""), xsrf_token: str = Form("")):
    data = await file.read()
    try:
        targets = dealupload.parse_split_csv(data)
    except dealupload.ValidationError as e:
        items = "".join(f"<li>{html.escape(m)}</li>" for m in e.messages)
        return _page(_upload_form(f'<div class="err"><b>Validation failed:</b><ul>{items}</ul></div>'))
    except Exception as ex:  # noqa: BLE001
        return _page(_upload_form(f'<div class="err">Could not read CSV: {html.escape(str(ex))}</div>'))

    session, xsrf = _resolve_auth(session_cookie, xsrf_token)
    if not session or not xsrf:
        return _page(_upload_form('<div class="err">No ControlGrid login available. '
                                  'Paste your SESSION and XSRF-TOKEN cookies above.</div>'))

    using = "your pasted cookies" if session_cookie.strip() else "the server login"
    try:
        plan, _ = dealupload.build_upload_plan(targets, session, xsrf)
    except Exception as ex:  # noqa: BLE001 - cookies likely expired
        return _page(_upload_form(f'<div class="err">Could not fetch existing rules '
                                  f'(login may have expired): {html.escape(str(ex))}</div>'))

    csv_b64 = base64.b64encode(data).decode()
    body = f"""
    <h1>Preview</h1>
    <p class="muted">Auth: {using}. Nothing has been written yet. Existing FSN&times;warehouse
    overlaps are removed from old rules (edits) so this sheet wins.</p>
    <pre>{html.escape(dealupload.plan_preview(plan))}</pre>
    <form action="upload-execute" method="post">
      <input type="hidden" name="csv_b64" value="{csv_b64}">
      <input type="hidden" name="session_cookie" value="{html.escape(session_cookie)}">
      <input type="hidden" name="xsrf_token" value="{html.escape(xsrf_token)}">
      <button type="submit">Confirm &amp; apply ({len(plan.edits)} edit(s), {len(plan.creates)} create(s))</button>
    </form>
    <p style="margin-top:14px"><a href="upload">&larr; Start over</a></p>
    """
    return _page(body)


@app.post("/upload-execute", response_class=HTMLResponse)
def upload_execute(csv_b64: str = Form(...),
                   session_cookie: str = Form(""), xsrf_token: str = Form("")):
    try:
        data = base64.b64decode(csv_b64)
        targets = dealupload.parse_split_csv(data)
    except Exception as ex:  # noqa: BLE001
        return _page(_upload_form(f'<div class="err">{html.escape(str(ex))}</div>'))

    session, xsrf = _resolve_auth(session_cookie, xsrf_token)
    if not session or not xsrf:
        return _page(_upload_form('<div class="err">No ControlGrid login available.</div>'))

    try:
        result = dealupload.execute_upload(targets, session, xsrf)
    except Exception as ex:  # noqa: BLE001 - cookies likely expired
        return _page(_upload_form(f'<div class="err">Could not apply (login may have expired): '
                                  f'{html.escape(str(ex))}</div>'))

    failed = "".join(f"<li>{html.escape(str(k))} {html.escape(str(i))}: {html.escape(e)}</li>"
                     for k, i, e in result["failed"])
    failed_block = f'<div class="err"><b>Failures:</b><ul>{failed}</ul></div>' if failed else ""
    body = f"""
    <h1>Done</h1>
    <div class="ok">Edited <b>{len(result['edited'])}</b>, created <b>{len(result['created'])}</b>,
    failed <b>{len(result['failed'])}</b>.</div>
    {failed_block}
    <p style="margin-top:14px"><a href=".">&larr; Convert another</a> &middot; <a href="upload">Upload another</a></p>
    """
    return _page(body)


@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}


@app.post("/convert")
async def convert(file: UploadFile = File(None), sheet_url: str = Form("")):
    sheet_url = (sheet_url or "").strip()
    try:
        if sheet_url:
            out_bytes, warnings = convert_split.convert_from_sheet(sheet_url)
            name = "sheet"
        elif file is not None:
            out_bytes, warnings = convert_split.convert_csv_bytes(await file.read())
            name = (file.filename or "sheet").rsplit(".", 1)[0]
        else:
            return _page(_form('<div class="err">Provide a Google Sheet link or upload a CSV.</div>'))
    except Exception as ex:  # noqa: BLE001
        return _page(_form(f'<div class="err">Could not convert: {html.escape(str(ex))}</div>'))

    headers = {"Content-Disposition": f'attachment; filename="{name}_freebie_split.csv"'}
    if warnings:
        headers["X-Convert-Warnings"] = str(len(warnings))
    return Response(content=out_bytes, media_type="text/csv", headers=headers)
