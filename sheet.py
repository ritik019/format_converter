import csv
import io
try:  # works inside the freebie package and as a flat deploy repo (files at root)
    from freebie.models import WarehouseConfig, TargetRule, ValidationError
    from freebie.timeutil import parse_ist_to_epoch_ms
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from models import WarehouseConfig, TargetRule, ValidationError
    from timeutil import parse_ist_to_epoch_ms


def rows_from_csv_bytes(data):
    """Parse uploaded CSV bytes into a list-of-lists, same shape as a sheet read."""
    text = data.decode("utf-8-sig")
    return [row for row in csv.reader(io.StringIO(text))]


def parse_cohort_ids(text):
    """Split a free-text cohort-id field on commas / whitespace / newlines."""
    import re
    if not text:
        return []
    return [p for p in re.split(r"[\s,]+", text.strip()) if p]


def coerce_bool(value):
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0", ""):
        return False
    raise ValueError(f"Not a boolean: {value!r}")


def coerce_number(value):
    s = str(value).strip().replace(",", "")
    return float(s)


def find_header(rows):
    for i, row in enumerate(rows):
        cells = [str(c).strip() for c in row]
        if "rule_name" in cells:
            col = {name: idx for idx, name in enumerate(cells) if name}
            return i, col
    raise ValueError("Header row containing 'rule_name' not found")


# Rule-level columns (carried per rule, as opposed to per-warehouse columns).
_RULE_FIELDS = [
    "rule_name", "rule_type", "activation", "rule_description", "start_time",
    "end-time", "replenishment_type", "child_fcn", "title",
    "cart_value_threshold", "cart_description",
]
_REQUIRED_RULE_FIELDS = [
    "rule_name", "activation", "rule_description", "start_time",
    "end-time", "child_fcn", "title", "cart_value_threshold", "cart_description",
]


def _cell(row, col, name):
    idx = col.get(name)
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx]).strip()


def _collect_records(rows, header_idx, col):
    """Walk data rows into (rule_fields, warehouse_row) records.

    A row with `rule_name` filled (re)sets the current rule context; rows with
    `rule_name` blank inherit it (supports both the repeated-fields layout and the
    blank-continuation layout). Only rows that carry a `warehouse_id` emit a record.
    Rows before the first rule context, or with neither field, are skipped (preamble).
    """
    records = []
    carried = None
    for r in range(header_idx + 1, len(rows)):
        row = rows[r]
        if _cell(row, col, "rule_name"):
            carried = {f: _cell(row, col, f) for f in _RULE_FIELDS}
            carried["_line"] = r + 1
        if not _cell(row, col, "warehouse_id") or carried is None:
            continue
        records.append({"rule": carried, "row": row, "line": r + 1})
    return records


def _build_warehouse(row, col, cap_name, line, errors):
    wid = _cell(row, col, "warehouse_id")
    try:
        is_full = coerce_bool(_cell(row, col, "full"))
        is_partial = coerce_bool(_cell(row, col, "partial"))
    except ValueError as e:
        errors.append(f"Line {line}: {e}")
        return None
    if is_full == is_partial:
        errors.append(f"Line {line} ({wid}): exactly one of full/partial must be TRUE")
        return None
    consumption = "FULL" if is_full else "PARTIAL"
    cap = None
    if consumption == "PARTIAL":
        raw = _cell(row, col, cap_name)
        if not raw:
            errors.append(f"Line {line} ({wid}): inventory_cap required when PARTIAL")
            return None
        try:
            cap = int(coerce_number(raw))
        except ValueError:
            errors.append(f"Line {line} ({wid}): inventory_cap must be a number")
            return None
    return WarehouseConfig(wid, consumption, cap)


def parse_and_validate(rows):
    header_idx, col = find_header(rows)
    cap_name = "inventory_cap" if "inventory_cap" in col else (
        "split_number" if "split_number" in col else "inventory_cap")
    records = _collect_records(rows, header_idx, col)
    errors = []
    targets = []
    if not records:
        raise ValidationError(["No rule rows found below the header"])

    # Group rows into one rule per (FSN, threshold); warehouses are merged.
    groups = {}
    order = []
    for rec in records:
        rule = rec["rule"]
        key = (rule.get("child_fcn", ""), rule.get("cart_value_threshold", ""))
        if key not in groups:
            groups[key] = {"rule": rule, "line": rule.get("_line", rec["line"]), "rows": []}
            order.append(key)
        groups[key]["rows"].append((rec["row"], rec["line"]))

    seen_fsn_wh = {}
    for key in order:
        grp = groups[key]
        rule, line = grp["rule"], grp["line"]

        for name in _REQUIRED_RULE_FIELDS:
            if not rule.get(name):
                errors.append(f"Line {line}: missing required '{name}'")
        if rule.get("rule_type") != "FREEBIE DEAL":
            errors.append(f"Line {line}: rule_type must be 'FREEBIE DEAL'")

        warehouses = []
        seen_wh = set()
        for wh_row, wh_line in grp["rows"]:
            w = _build_warehouse(wh_row, col, cap_name, wh_line, errors)
            if w is None:
                continue
            if w.warehouse_id in seen_wh:
                errors.append(f"Line {wh_line}: duplicate warehouse '{w.warehouse_id}' "
                              f"for FSN {key[0]} @ threshold {key[1]}")
                continue
            seen_wh.add(w.warehouse_id)
            warehouses.append(w)

        try:
            start_ms = parse_ist_to_epoch_ms(rule.get("start_time", ""))
            end_ms = parse_ist_to_epoch_ms(rule.get("end-time", ""))
            if end_ms <= start_ms:
                errors.append(f"Line {line}: end-time must be after start_time")
        except ValueError as e:
            errors.append(f"Line {line}: {e}")
            start_ms = end_ms = 0

        try:
            is_active = coerce_bool(rule.get("activation", ""))
        except ValueError as e:
            errors.append(f"Line {line}: activation {e}")
            is_active = False

        try:
            threshold = coerce_number(rule.get("cart_value_threshold", ""))
        except ValueError:
            errors.append(f"Line {line}: cart_value_threshold must be a number")
            threshold = 0.0

        replenishment = rule.get("replenishment_type") or "DAILY"

        targets.append(TargetRule(
            rule_name=rule.get("rule_name", ""),
            rule_description=rule.get("rule_description", ""),
            is_active=is_active,
            start_ms=start_ms,
            end_ms=end_ms,
            replenishment_type=replenishment,
            fsn=rule.get("child_fcn", ""),
            title=rule.get("title", ""),
            cart_value_threshold=threshold,
            cart_description=rule.get("cart_description", ""),
            warehouses=warehouses,
        ))

        # same FSN x warehouse must not appear under two different rules/thresholds
        for w in warehouses:
            fw = (key[0], w.warehouse_id)
            if fw in seen_fsn_wh:
                errors.append(f"Duplicate FSN x warehouse {key[0]} x {w.warehouse_id} across sheet")
            seen_fsn_wh[fw] = True

    if errors:
        raise ValidationError(errors)
    return targets


def read_sheet_values(spreadsheet_id, tab):
    """Read a tab's cells as formatted strings via the service account."""
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import Request
    import requests as _requests
    try:
        from freebie.config import SERVICE_ACCOUNT_PATH
    except ImportError:  # pragma: no cover - deploy layout (files at repo root)
        from config import SERVICE_ACCOUNT_PATH

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    creds.refresh(Request())
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{tab}"
    resp = _requests.get(url, headers={"Authorization": f"Bearer {creds.token}"},
                         params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=60)
    resp.raise_for_status()
    return resp.json().get("values", [])
