"""Upload a converted freebie CSV into ControlGrid (create the deals).

Parses the split-format CSV produced by convert_split and creates one
FREEBIE_DEALS rule per (FSN, cart_value_threshold), merging warehouses.
``split_number`` is used as the PARTIAL ``inventoryCap``; ``deal_price`` and
``tag`` are ignored (the live API does not take them).

Auth is resolved by the caller: ControlGrid SESSION + XSRF cookies, either from
the server environment or supplied per-request.
"""
import csv
import io
import os
from datetime import datetime, timezone, timedelta

try:  # works inside the freebie package and as a flat deploy repo
    from freebie.models import WarehouseConfig, TargetRule, ValidationError
    from freebie.payloads import build_create_payload
    from freebie.controlgrid import ControlGrid
    from freebie.auth import make_post_fn
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from models import WarehouseConfig, TargetRule, ValidationError
    from payloads import build_create_payload
    from controlgrid import ControlGrid
    from auth import make_post_fn

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_EMAIL = os.environ.get("FREEBIE_EMAIL", "madhavsingal@firstclub.co.in")

_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
]

# Columns the uploader needs from the converted CSV.
_REQUIRED_COLS = [
    "rule_name", "activation", "start_time", "end-time", "child_fcn", "title",
    "warehouse_id", "full", "partial", "cart_value_threshold", "cart_description",
]


def _epoch_ms(value):
    s = str(value).strip()
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=IST).timestamp() * 1000)
    raise ValueError(f"unrecognized date-time {value!r}")


def _bool(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def _number(value):
    return float(str(value).strip().replace(",", ""))


def parse_split_csv(data):
    """CSV bytes (convert_split output) -> list[TargetRule]. Raises ValidationError."""
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValidationError(["The CSV is empty."])

    header = [c.strip() for c in rows[0]]
    col = {name: i for i, name in enumerate(header) if name}
    missing = [c for c in _REQUIRED_COLS if c not in col]
    if missing:
        raise ValidationError([f"Missing required column(s): {', '.join(missing)}"])

    def cell(row, name):
        i = col.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    errors = []
    groups = {}   # (fsn, threshold) -> {"rule": {...}, "line": int, "whs": [(row, line)]}
    order = []
    carried = None

    for r in range(1, len(rows)):
        row = rows[r]
        line = r + 1
        if cell(row, "rule_name"):
            carried = {f: cell(row, f) for f in (
                "rule_name", "rule_description", "activation", "start_time", "end-time",
                "replenishment_type", "child_fcn", "title", "cart_value_threshold",
                "cart_description")}
            carried["_line"] = line
        if not cell(row, "warehouse_id") or carried is None:
            continue
        key = (carried.get("child_fcn", ""), carried.get("cart_value_threshold", ""))
        if key not in groups:
            groups[key] = {"rule": carried, "line": carried["_line"], "whs": []}
            order.append(key)
        groups[key]["whs"].append((row, line))

    if not order:
        raise ValidationError(["No data rows with a warehouse_id were found."])

    targets = []
    for key in order:
        grp = groups[key]
        rule, line = grp["rule"], grp["line"]

        warehouses, seen = [], set()
        for wh_row, wh_line in grp["whs"]:
            wid = cell(wh_row, "warehouse_id")
            is_full = _bool(cell(wh_row, "full"))
            is_partial = _bool(cell(wh_row, "partial"))
            if is_full == is_partial:
                errors.append(f"Line {wh_line} ({wid}): exactly one of full/partial must be TRUE")
                continue
            cap = None
            if is_partial:
                raw = cell(wh_row, "split_number")
                if not raw:
                    errors.append(f"Line {wh_line} ({wid}): split_number required for PARTIAL")
                    continue
                try:
                    cap = int(_number(raw))
                except ValueError:
                    errors.append(f"Line {wh_line} ({wid}): split_number must be a number")
                    continue
            if wid in seen:
                errors.append(f"Line {wh_line}: duplicate warehouse {wid} for FSN {key[0]}")
                continue
            seen.add(wid)
            warehouses.append(WarehouseConfig(wid, "FULL" if is_full else "PARTIAL", cap))

        try:
            start_ms = _epoch_ms(rule.get("start_time", ""))
            end_ms = _epoch_ms(rule.get("end-time", ""))
            if end_ms <= start_ms:
                errors.append(f"Line {line}: end-time must be after start_time")
        except ValueError as e:
            errors.append(f"Line {line}: {e}")
            start_ms = end_ms = 0

        try:
            threshold = _number(rule.get("cart_value_threshold", ""))
        except ValueError:
            errors.append(f"Line {line}: cart_value_threshold must be a number")
            threshold = 0.0

        targets.append(TargetRule(
            rule_name=rule.get("rule_name", ""),
            rule_description=rule.get("rule_description") or rule.get("rule_name", ""),
            is_active=_bool(rule.get("activation", "")),
            start_ms=start_ms,
            end_ms=end_ms,
            replenishment_type=rule.get("replenishment_type") or "DAILY",
            fsn=rule.get("child_fcn", ""),
            title=rule.get("title", ""),
            cart_value_threshold=threshold,
            cart_description=rule.get("cart_description", ""),
            warehouses=warehouses,
        ))

    if errors:
        raise ValidationError(errors)
    return targets


def preview_text(targets):
    lines = [f"{len(targets)} rule(s) will be CREATED:"]
    for t in targets:
        full = sum(1 for w in t.warehouses if w.consumption_type == "FULL")
        partial = sum(1 for w in t.warehouses if w.consumption_type == "PARTIAL")
        lines.append(
            f"  {t.rule_name} | fsn={t.fsn} | MOV={t.cart_value_threshold} "
            f"| active={t.is_active} | {len(t.warehouses)} channels "
            f"(full={full}, partial={partial})")
    return "\n".join(lines)


def create_deals(targets, session_cookie, xsrf_token, email=None):
    """Create each target via ControlGrid. Returns {'created': [...], 'failed': [...]}."""
    email = email or DEFAULT_EMAIL
    client = ControlGrid(make_post_fn(session_cookie, xsrf_token))
    result = {"created": [], "failed": []}
    for t in targets:
        try:
            client.create_rule(build_create_payload(t, email))
            result["created"].append(t.rule_name)
        except Exception as ex:  # noqa: BLE001 - report and continue
            result["failed"].append((t.rule_name, str(ex)))
    return result
