"""Standalone PUBLIC converter app (converter only — no ControlGrid access).

Exposes just the planning-sheet -> freebie CSV conversion, so it is safe to
deploy publicly. It deliberately does NOT import the deal-creation code
(auth / controlgrid / planner), so there is no way to create or edit real deals
through this app.

Run locally:  uvicorn app:app --port 8004
Deploy:       uvicorn app:app --host 0.0.0.0 --port $PORT
"""
import html

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response

try:  # works as part of the freebie package and as a flat deploy repo
    from freebie import convert_split
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    import convert_split

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
 .muted{color:#666;font-size:13px}
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


@app.get("/", response_class=HTMLResponse)
def index():
    return _page(_form())


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
