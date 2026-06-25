"""Convert the split-quantity planning sheet -> the freebie split-deals CSV.

This is the single converter tool. Source rows use a merged-header layout; the
columns are located by the labels ``mov`` / ``warehouse`` /
``rule_name_and_description`` / ``fcn``, and the date / status columns are read
positionally relative to those anchors. The ``warehouse`` cell carries a
per-warehouse quantity on each line, e.g.::

    FCHHYDTEL01    16
    FCHHYDNIZ01    3
    FCHHYDNAR01

Quantity rules
--------------
  qty > 5      -> PARTIAL, split_number = qty
  qty <= 5     -> FULL,    split_number blank
  no quantity  -> FULL,    split_number blank

Output (one filled row per warehouse)::

  rule_name, rule_type, activation, rule_description, start_time, end-time, tag,
  replenishment_type, child_fcn, title, warehouse_id, full, partial,
  split_percentage, split_number, cart_value_threshold, cart_description, deal_price

Fixed defaults: title and cart_description are the same on every row;
tag / split_percentage / deal_price are left empty.

CLI
---
  python -m freebie.convert_split SOURCE.csv [OUTPUT.csv]
  python -m freebie.convert_split --sheet URL [OUTPUT.csv]
"""
import csv
import io
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

TARGET_HEADER = [
    "rule_name", "rule_type", "activation", "rule_description", "start_time",
    "end-time", "tag", "replenishment_type", "child_fcn", "title",
    "warehouse_id", "full", "partial", "split_percentage", "split_number",
    "cart_value_threshold", "cart_description", "deal_price",
]

RULE_TYPE = "FREEBIE DEAL"
REPLENISHMENT_TYPE = "DAILY"

# Fixed text applied to every output row.
TITLE_DEFAULT = "Unlock Special Price Deal : Limited stock"
CART_DESCRIPTION_DEFAULT = "Add items worth Rs.$$$ more to unlock"

FULL_QTY_THRESHOLD = 5  # qty <= this -> FULL (no split)

# Rules are ACTIVE by default; a row is only inactive if the status column
# explicitly says one of these.
_INACTIVE_STATUSES = {
    "inactive", "paused", "off", "false", "no", "0", "draft", "disabled", "expired",
}

# When the warehouse cell says "all" / "all ch", apply every channel as
# PARTIAL with this split_number.
ALL_CHANNELS = [
    "FCHBLRDOD01", "FCHBLRELC01", "FCHBLRHEB01", "FCHBLRHOO01", "FCHBLRHSAF01",
    "FCHBLRHSR01", "FCHBLRIND01", "FCHBLRKNP01", "FCHBLRRAJ01", "FCHBLRVAR01",
    "FCHBLRWHF01", "FCHBLRAECS01", "FCHBLRBEN01", "FCHBLRBHO01", "FCHBLRHAR01",
    "FCHBLRHOR01", "FCHBLRJMH01", "FCHBLRJPN01", "FCHBLRKOR01", "FCHBLRMDP01",
    "FCHBLRNAG01", "FCHBLRSJR01", "FCHBLRTHA01", "FCHHYDKND01", "FCHHYDMDL02",
    "FCHHYDNAR01", "FCHHYDNIZ01", "FCHHYDTEL01",
]
ALL_SPLIT = 20  # split_number used for every channel when "all" is given

# Region subsets of "all", keyed by warehouse-id prefix.
BLR_CHANNELS = [c for c in ALL_CHANNELS if c.startswith("FCHBLR")]
HYD_CHANNELS = [c for c in ALL_CHANNELS if c.startswith("FCHHYD")]


def channels_for(region):
    """region in {'all','blr','hyd'} -> the channel list that 'all' expands to."""
    r = (region or "all").strip().lower()
    if r == "blr":
        return BLR_CHANNELS
    if r == "hyd":
        return HYD_CHANNELS
    return ALL_CHANNELS


# Matches a line that is "all", "all ch", "all channels", etc.
_ALL_RE = re.compile(r"^all(\s+ch.*)?$", re.I)

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID_RE = re.compile(r"[?&#]gid=(\d+)")

# A warehouse line: 'FCHHYDTEL01<tab/spaces>16' or just 'FCHHYDTEL01'.
_WH_QTY_RE = re.compile(r"^(FCH[A-Z0-9]+)(?:\s+(\d+))?\s*$")

# Date cell formats seen in the source ('Sun, 21 Jun'); year is filled in later.
_DATE_FORMATS = [
    "%a, %d %b", "%a %d %b", "%d %b",
    "%a, %d %b %Y", "%d %b %Y",
    "%Y-%m-%d", "%d/%m/%Y",
]


def _today_start_end():
    """Fallback when a row has no usable date: today 00:00 -> tomorrow 00:00 IST."""
    now = datetime.now(IST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def _parse_start_end(cell, year):
    """('Sun, 21 Jun', 2026) -> ('2026-06-21 00:00:00', '2026-06-22 00:00:00')."""
    s = str(cell).strip()
    if not s:
        return "", ""
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.year == 1900:  # no year in the source string
            dt = dt.replace(year=year)
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        fmt_out = "%Y-%m-%d %H:%M:%S"
        return start.strftime(fmt_out), end.strftime(fmt_out)
    return "", ""  # unrecognized -> leave blank, caller warns


def _find_header(rows):
    """Return (index, {label: col}) for the row carrying 'mov' and 'warehouse'."""
    for i, row in enumerate(rows):
        cells = [str(c).strip().lower() for c in row]
        if "mov" in cells and "warehouse" in cells:
            labels = {name: j for j, name in enumerate(cells) if name}
            return i, labels
    raise ValueError("Header row with 'mov' and 'warehouse' not found")


def _warehouse_qty(cell, all_channels=ALL_CHANNELS):
    """Parse the multi-line warehouse cell -> ([(wid, qty_or_None)], notes).

    If any line says "all" / "all ch", expand to every channel in all_channels
    as PARTIAL (split_number = ALL_SPLIT), ignoring individually-listed warehouses.
    """
    if any(_ALL_RE.match(line.strip()) for line in str(cell).splitlines()):
        return ([(c, ALL_SPLIT) for c in all_channels],
                [f"'all' -> {len(all_channels)} channels @ split {ALL_SPLIT}"])

    pairs, seen, notes = [], set(), []
    for line in str(cell).splitlines():
        s = line.strip()
        if not s:
            continue
        m = _WH_QTY_RE.match(s)
        if not m:
            notes.append(s)  # non-warehouse text, e.g. a stray '70% INV'
            continue
        wid = m.group(1)
        qty = int(m.group(2)) if m.group(2) else None
        if wid in seen:
            notes.append(f"duplicate {wid} dropped")
            continue
        seen.add(wid)
        pairs.append((wid, qty))
    return pairs, notes


def _consumption(qty):
    """qty -> (full, partial, split_number) cell strings."""
    if qty is not None and qty > FULL_QTY_THRESHOLD:
        return "FALSE", "TRUE", str(qty)
    return "TRUE", "FALSE", ""  # qty<=5 or missing -> FULL, no split


MOV_OFFSET = 14  # cart_value_threshold = mov - 14 (e.g. 499 -> 485)


def _cart_threshold(mov):
    """mov string -> (cart_value_threshold string, error_or_None)."""
    s = str(mov).strip().replace(",", "")
    if not s:
        return "", None
    try:
        n = float(s) - MOV_OFFSET
    except ValueError:
        return "", f"mov {mov!r} is not a number - cart_value_threshold left blank"
    return (str(int(n)) if n == int(n) else str(n)), None


def convert_rows(rows, year=None, all_region="all"):
    """Convert source rows (list-of-lists) -> (target_rows, warnings).

    all_region ('all'|'blr'|'hyd') decides which channels a literal "all" in the
    warehouse cell expands to.
    """
    if year is None:
        year = datetime.now(IST).year
    all_channels = channels_for(all_region)

    header_idx, labels = _find_header(rows)
    try:
        mov_idx = labels["mov"]
        wh_idx = labels["warehouse"]
        name_idx = labels["rule_name_and_description"]
        fcn_idx = labels["fcn"]
    except KeyError as e:
        raise ValueError(f"Missing expected header column: {e}")

    date_idx = 0
    status_idx = mov_idx - 1

    def cell(row, idx):
        return str(row[idx]).strip() if 0 <= idx < len(row) else ""

    out = [list(TARGET_HEADER)]
    warnings = []
    last_date = ""  # carry the date down (handles merged cells / deleted rows)

    for r in range(header_idx + 1, len(rows)):
        row = rows[r]
        line = r + 1
        name = cell(row, name_idx)
        fcn = cell(row, fcn_idx)
        if not any(cell(row, i) for i in (name_idx, fcn_idx, wh_idx, mov_idx)):
            continue

        pairs, notes = _warehouse_qty(row[wh_idx] if wh_idx < len(row) else "", all_channels)
        for n in notes:
            warnings.append(f"Line {line} ({name}): {n}")
        if not pairs:
            warnings.append(f"Line {line} ({name or fcn or '?'}): no warehouse ids found - row skipped")
            continue

        # Active by default; only FALSE when the status column explicitly says so.
        status = cell(row, status_idx).strip().lower()
        activation = "FALSE" if status in _INACTIVE_STATUSES else "TRUE"
        raw_date = cell(row, date_idx)
        if raw_date:
            last_date = raw_date  # remember most recent date for blank rows below
        start_time, end_time = _parse_start_end(last_date, year)
        if not start_time:
            start_time, end_time = _today_start_end()
            warnings.append(f"Line {line} ({name}): no usable date {raw_date!r} - defaulted to today")
        threshold, mov_err = _cart_threshold(cell(row, mov_idx))
        if mov_err:
            warnings.append(f"Line {line} ({name}): {mov_err}")

        # cols 0-9 (rule-level), repeated on every warehouse row
        prefix = [
            name, RULE_TYPE, activation, name, start_time, end_time, "",
            REPLENISHMENT_TYPE, fcn, TITLE_DEFAULT,
        ]
        for wid, qty in pairs:
            full, partial, split_number = _consumption(qty)
            out.append(prefix + [
                wid, full, partial, "", split_number, threshold,
                CART_DESCRIPTION_DEFAULT, "",
            ])

    return out, warnings


def convert_csv_bytes(data, year=None, all_region="all"):
    """CSV bytes -> (converted CSV bytes, warnings)."""
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    out_rows, warnings = convert_rows(rows, year=year, all_region=all_region)
    buf = io.StringIO()
    csv.writer(buf).writerows(out_rows)
    return buf.getvalue().encode("utf-8"), warnings


def sheet_csv_url(url_or_id):
    """A Google Sheet URL (or bare id) -> its CSV-export URL."""
    s = str(url_or_id).strip()
    m = _SHEET_ID_RE.search(s)
    sheet_id = m.group(1) if m else s
    if not sheet_id:
        raise ValueError("Could not find a spreadsheet id in the input")
    g = _GID_RE.search(s)
    gid = g.group(1) if g else "0"
    return (f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
            f"?format=csv&gid={gid}")


def fetch_sheet_csv(url_or_id):
    """Download a link-accessible Google Sheet tab as CSV bytes."""
    export = sheet_csv_url(url_or_id)
    with urllib.request.urlopen(export, timeout=60) as resp:  # noqa: S310 - fixed host
        data = resp.read()
    if data.lstrip()[:1] in (b"<", b"<!"):  # got an HTML login/permission page
        raise ValueError("Sheet is not link-accessible (got an HTML page, not CSV). "
                         "Share it as 'Anyone with the link can view'.")
    return data


def convert_from_sheet(url_or_id, year=None, all_region="all"):
    """Fetch a Google Sheet by URL/id and convert it -> (CSV bytes, warnings)."""
    return convert_csv_bytes(fetch_sheet_csv(url_or_id), year=year, all_region=all_region)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit(
            "Usage: python -m freebie.convert_split SOURCE.csv [OUTPUT.csv]\n"
            "       python -m freebie.convert_split --sheet URL [OUTPUT.csv]")

    if argv[0] == "--sheet":
        if len(argv) < 2:
            raise SystemExit("--sheet needs a Google Sheet URL or id")
        out_bytes, warnings = convert_from_sheet(argv[1])
        dst = argv[2] if len(argv) > 2 else None
    else:
        src = argv[0]
        dst = argv[1] if len(argv) > 1 else None
        with open(src, "rb") as f:
            out_bytes, warnings = convert_csv_bytes(f.read())

    if dst:
        with open(dst, "wb") as f:
            f.write(out_bytes)
        print(f"Wrote {dst}")
    else:
        sys.stdout.write(out_bytes.decode("utf-8"))

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
