"""Per-warehouse banner rows on ControlGrid (rules-management "Warehouse Widget").

Banners are NOT part of the rule body. They live on their own API, one row per
(ruleId, warehouseId):

  GET    /api/v1/rule-banner/config/{ruleId}                 rows for one rule
  GET    /api/v1/rule-banner/config/warehouse/{warehouseId}  rows on one CH (all rules)
  PUT    /api/v1/rule-banner/config                          upsert one row

Since the 5-Nov-2026 move to Pricing and Offers, live banners point at ``pno_...`` rule
ids. Rank must be unique among ACTIVE banners on a CH: the portal deactivates the other
banner when one is switched on at a taken rank, so the plan flags those clashes.

Input: a CSV / .xlsx (columns rule_id, rank, banner_id, banner_url, chs) or pasted lines
(comma or tab separated). ``chs`` is optional: comma separated CHs for that row only.
Blank rank = keep each row's current rank. Target CHs = the rule's warehouses on
Pricing and Offers plus any CH that already has a banner row for the rule.
"""
import csv
import io
import re
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

PORTAL = "https://portal.controlgrid.in"
BANNER_API = PORTAL + "/api/v1/rule-banner/config"
PNO_RULE_API = PORTAL + "/api/v1/pno/rules/"
ROW_KEYS = ("ruleId", "warehouseId", "bannerId", "bannerAssetUrl", "ctaId", "ctaType", "rank", "pageType")
DEFAULT_CTA = {"ctaType": "CATEGORY_TREE", "ctaId": "NA", "pageType": "NA"}


class BannerClient:
    def __init__(self, auth_token, session=None, timeout=60):
        self._http = session or requests.Session()
        self._headers = {"accept": "application/json", "content-type": "application/json",
                         "origin": PORTAL, "referer": PORTAL + "/rules-management/",
                         "authorization": f"Bearer {auth_token}"}
        self._timeout = timeout

    def _call(self, method, url, body=None):
        resp = self._http.request(method, url, json=body, headers=self._headers, timeout=self._timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 401:
            raise RuntimeError("401 Unauthorized - the login token has expired, paste a fresh one")
        if resp.status_code >= 400 or data.get("success") is False:
            err = data.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"{resp.status_code}: {msg or resp.text[:200]}")
        return data.get("body")

    def rule_rows(self, rule_id):
        return self._call("GET", f"{BANNER_API}/{rule_id}") or []

    def warehouse_rows(self, warehouse_id):
        return self._call("GET", f"{BANNER_API}/warehouse/{warehouse_id}") or []

    def pno_rule(self, rule_id):
        return self._call("GET", PNO_RULE_API + rule_id) or {}

    def put_row(self, row):
        return self._call("PUT", BANNER_API, {k: row.get(k) for k in ROW_KEYS})


TEMPLATE_HEADER = ["rule_id", "rank", "banner_id", "banner_url", "chs"]
TEMPLATE_CSV = (
    ",".join(TEMPLATE_HEADER) + "\n"
    "pno_01M6KXJ7DMDQN59F3PXH9M3SEZ,,482722,https://d1cuexdfcmg0zk.cloudfront.net/assets/image/<uuid>/original.webp,\n"
    "pno_01M6PPVT0PBGQDZH6BZ9WS2NB8,200,461945,https://d1cuexdfcmg0zk.cloudfront.net/assets/image/<uuid>/original.webp,"
    "\"FCHHYDTEL01,FCHHYDNAR01\"\n")


def rows_from_upload(filename, data):
    """CSV or .xlsx bytes -> list of rows (lists of str). First sheet only for xlsx."""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or data[:2] == b"PK":
        return _xlsx_rows(data)
    text = data.decode("utf-8-sig", errors="replace")
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]


def _xlsx_rows(data):
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        raise ValueError("no worksheet found in the .xlsx")
    rows = []
    for r in ET.fromstring(z.read(sheets[0])).iter("{%s}row" % ns["m"]):
        cells = {}
        for c in r.findall("m:c", ns):
            col = re.match(r"[A-Z]+", c.get("r", "A")).group(0)
            idx = 0
            for ch in col:
                idx = idx * 26 + ord(ch) - 64
            v = c.find("m:v", ns)
            if c.get("t") == "s" and v is not None:
                val = shared[int(v.text)]
            elif c.get("t") == "inlineStr":
                val = "".join(t.text or "" for t in c.iter("{%s}t" % ns["m"]))
            else:
                val = v.text if v is not None else ""
                if val and re.fullmatch(r"-?\d+\.0+", val):
                    val = val.split(".")[0]  # Excel stores 200 as 200.0
            cells[idx - 1] = (val or "").strip()
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


def rows_to_lines(rows):
    """Normalise uploaded rows to the tab-separated text the form and parse_lines use."""
    return "\n".join("\t".join((c or "").replace("\t", " ") for c in row) for row in rows if any(row))


def parse_lines(text):
    """-> (items, errors). Each item: {rule_id, rank (int|None), banner_id, banner_url, chs (set)}."""
    items, errors = [], []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in (raw.split("\t") if "\t" in raw else raw.split(","))]
        if parts and parts[0].lower().replace(" ", "_") in ("rule_id", "ruleid"):
            continue  # header row
        parts += [""] * (5 - len(parts))
        rule_id, rank, banner_id, url = parts[:4]
        chs = parse_ch_filter(" ".join(parts[4:]))
        if not rule_id:
            errors.append(f"line {n}: rule id missing")
            continue
        if not banner_id or not url:
            errors.append(f"line {n}: banner id and banner url are both required")
            continue
        if not url.startswith("http"):
            errors.append(f"line {n}: banner url should start with https:// ({url})")
            continue
        if rank and not rank.lstrip("-").isdigit():
            errors.append(f"line {n}: rank '{rank}' is not a number")
            continue
        items.append({"rule_id": rule_id, "rank": int(rank) if rank else None,
                      "banner_id": banner_id, "banner_url": url, "chs": chs})
    return items, errors


def parse_ch_filter(text):
    return {c.strip().upper() for c in re.split(r"[\s,;|]+", text or "") if c.strip()}


def build_plan(client, items, ch_filter=None):
    """Read-only. -> list of rule plans:
    {rule_id, name, status, rows:[{row, old, clash}], problems:[str]}"""
    plans, wh_cache = [], {}
    for it in items:
        rid = it["rule_id"]
        plan = {"rule_id": rid, "name": "", "status": "", "rows": [], "problems": []}
        plans.append(plan)
        try:
            existing = {r["warehouseId"]: r for r in client.rule_rows(rid)}
        except Exception as ex:  # noqa: BLE001
            plan["problems"].append(f"could not read banner rows: {ex}")
            continue
        rule_whs = []
        if rid.startswith("pno_"):
            try:
                rule = client.pno_rule(rid)
                plan["name"], plan["status"] = rule.get("name", ""), rule.get("status", "")
                rule_whs = [w["warehouseId"] for w in rule.get("warehouses") or []
                            if w.get("mappingStatus", "ACTIVE") == "ACTIVE"]
            except Exception as ex:  # noqa: BLE001
                plan["problems"].append(f"could not read the rule on Pricing and Offers: {ex}")
        whs = sorted(set(rule_whs) | set(existing))
        only = it.get("chs") or ch_filter
        if only:
            whs = [w for w in whs if w in only]
        if not whs:
            plan["problems"].append("no CHs to write (rule has no warehouses / no CH matched the filter)")
            continue
        for wh in whs:
            old = existing.get(wh)
            rank = it["rank"] if it["rank"] is not None else (old or {}).get("rank")
            if rank is None:
                plan["problems"].append(f"{wh}: no rank given and no existing row to keep it from")
                continue
            base = dict(DEFAULT_CTA)
            if old:
                base.update({k: old[k] for k in ("ctaType", "ctaId", "pageType") if old.get(k)})
            row = dict(base, ruleId=rid, warehouseId=wh, bannerId=it["banner_id"],
                       bannerAssetUrl=it["banner_url"], rank=rank)
            if wh not in wh_cache:
                try:
                    wh_cache[wh] = client.warehouse_rows(wh)
                except Exception:  # noqa: BLE001
                    wh_cache[wh] = []
            clash = [r for r in wh_cache[wh] if r.get("ruleId") != rid and r.get("rank") == rank
                     and r.get("isActive") and r.get("bannerId")]
            plan["rows"].append({"row": row, "old": old, "clash": clash})
    return plans


def execute(client, plans, pause=0.15):
    """PUT every planned row, then re-read each rule and compare. -> {ok, failed, mismatched}"""
    ok, failed = [], []
    for p in plans:
        for r in p["rows"]:
            row = r["row"]
            try:
                client.put_row(row)
                ok.append(row)
            except Exception as ex:  # noqa: BLE001
                failed.append((row, str(ex)))
            time.sleep(pause)
    mismatched = []
    for rid in sorted({row["ruleId"] for row in ok}):
        try:
            live = {r["warehouseId"]: r for r in client.rule_rows(rid)}
        except Exception as ex:  # noqa: BLE001
            mismatched.append((rid, "*", f"could not re-read: {ex}"))
            continue
        for row in (x for x in ok if x["ruleId"] == rid):
            got = live.get(row["warehouseId"])
            diff = [k for k in ("bannerId", "bannerAssetUrl", "rank") if not got or got.get(k) != row[k]]
            if diff:
                mismatched.append((rid, row["warehouseId"], "differs on " + ", ".join(diff)))
    return {"ok": ok, "failed": failed, "mismatched": mismatched}
