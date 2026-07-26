# Freebie sheet converter

A small web app that converts an ops planning sheet (merged-header layout) into
the freebie-deals CSV format. Upload a CSV or paste a link to a link-viewable
Google Sheet, and download the converted file.

## Conversion rules
- Each warehouse line carries a quantity, e.g. `FCHHYDTEL01   16`:
  - quantity **> 5** → PARTIAL, `split_number` = quantity
  - quantity **≤ 5** or none → FULL, `split_number` blank
  - if the `split` column says **"Full"**, every warehouse on that row is forced
    to FULL (`split_number` blank), regardless of quantity
  - if the `split` column says **"Partial"**, every warehouse on that row is
    forced to PARTIAL (`split_number` = quantity), even if quantity ≤ 5 or missing
- `all` / `all ch` → every channel as PARTIAL with `split_number` 20
- `cart_value_threshold` = `mov − 12`
- One filled row per warehouse; `title` and `cart_description` use fixed defaults;
  `tag` / `split_percentage` / `deal_price` are left empty.

## Run locally
```
pip install -r requirements.txt
uvicorn app:app --port 8004
```
Then open http://127.0.0.1:8004/

## Deploy (Render)
Connect this repo on Render as a Blueprint (uses `render.yaml`), or as a Web
Service with:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
