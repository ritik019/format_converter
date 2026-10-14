"""One CSV row per rule a run touched (channel lists left out to keep it short): what was asked, whether it landed, and why
not when it didn't — so a run can be shared or checked without reading the page.

Columns cover the questions that come up after a run: was the rule created (and
its id), does it carry a cohort hurdle, is the FSN on the referral rule, and if
it failed, the error and the rule that blocked it.
"""
import csv
import io
from datetime import datetime

try:  # works inside the freebie package and as a flat deploy repo
    from freebie.conflicts import RULE_TYPE_LABELS, rule_pairs
    from freebie.timeutil import IST
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from conflicts import RULE_TYPE_LABELS, rule_pairs
    from timeutil import IST

COLUMNS = [
    "action", "status", "rule_name", "rule_id", "fsn", "cart_value_threshold",
    "start_ist", "end_ist", "active", "cohorts", "on_referral", "error", "blocked_by",
]

_ACTION = {"create": "Create", "update": "Update in place",
           "strip": "Remove CHs (freed for a sheet row)"}

def _ist(ms):
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%Y-%m-%d %H:%M") if ms > 0 else ""


def _cohorts(hurdles):
    """'NOT_IN: id1, id2 | IN: id3' for the rule's USER_COHORT hurdles."""
    parts = []
    for h in hurdles or []:
        if h.get("type") != "USER_COHORT":
            continue
        ids = h.get("value") or h.get("requiredCohortIds") or []
        ids = ids if isinstance(ids, list) else [ids]
        parts.append(f"{h.get('operation') or '?'}: {', '.join(map(str, ids))}")
    return " | ".join(parts)


def referral_pairs(type_index):
    """(fsn, warehouse) pairs held by any referral rule in the index."""
    pairs = set()
    for info in (type_index or {}).values():
        if info.get("ruleType") == "REFERRER":
            pairs |= rule_pairs(info.get("raw") or {})
    return pairs


def _on_referral(fsns, warehouses, ref_pairs):
    hit = sorted({w for f, w in ref_pairs if f in fsns})
    if not hit:
        return "No"
    clash = [w for w in warehouses if w in hit]
    if clash:
        return f"Yes - at {len(clash)} of this rule's CHs"
    return "Yes - but not at this rule's CHs"


def _blocked_text(blockers):
    return "; ".join(
        f"{RULE_TYPE_LABELS.get(b['rule_type'], b['rule_type'] or 'unknown type')} "
        f"'{b.get('name') or ''}' ({b['id']})" for b in blockers)


def summary_rows(plan, result, explained=(), type_index=None, deleted=(), delete_failed=()):
    """Rows (dicts keyed by COLUMNS) for one run, in plan order, then deletions."""
    ref = referral_pairs(type_index)
    blockers = {(k, str(i), e): b for k, i, e, b in explained}
    kinds = plan.edit_kinds or {}
    rows = []
    for kind, payload, ok, rule_id, err in result.get("outcomes", []):
        rule = payload["rule"]
        mappings = rule["consequence"].get("freebieDealsMapping") or []
        fsns = [m.get("fsn") for m in mappings]
        configs = [c for m in mappings for c in m.get("warehouseInventoryConfigs") or []]
        whs = [c.get("warehouseId") for c in configs]
        thresholds = sorted({str(m.get("cartValueThreshold")) for m in mappings})
        action = "create" if kind == "create" else kinds.get(str(payload.get("ruleId")), "update")
        ident = rule["name"] if kind == "create" else str(payload.get("ruleId"))
        status = ("Created" if kind == "create" else "Updated") if ok else "Failed"
        rows.append({
            "action": _ACTION[action],
            "status": status,
            "rule_name": rule.get("name") or "",
            "rule_id": rule_id,
            "fsn": ", ".join(fsns),
            "cart_value_threshold": ", ".join(thresholds),
            "start_ist": _ist(rule.get("startTime")),
            "end_ist": _ist(rule.get("endTime")),
            "active": "Yes" if rule.get("isActive") else "No",
            "cohorts": _cohorts(rule.get("hurdleList")),
            "on_referral": _on_referral(set(fsns), whs, ref),
            "error": err,
            "blocked_by": _blocked_text(blockers.get((kind, ident, err), [])),
        })
    for rid in deleted:
        rows.append({**dict.fromkeys(COLUMNS, ""),
                     "action": "Delete conflicting rule", "status": "Deleted", "rule_id": rid})
    for rid, err in delete_failed:
        rows.append({**dict.fromkeys(COLUMNS, ""), "action": "Delete conflicting rule",
                     "status": "Failed", "rule_id": rid, "error": err})
    return rows


def summary_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
