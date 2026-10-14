from dataclasses import dataclass
from datetime import datetime
from typing import List

try:  # works inside the freebie package and as a flat deploy repo
    from freebie.payloads import build_create_payload, build_edit_payload, build_update_payload
    from freebie.timeutil import IST
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from payloads import build_create_payload, build_edit_payload, build_update_payload
    from timeutil import IST


@dataclass
class Plan:
    edits: List[dict]
    creates: List[dict]
    excluded_cohort_ids: List[str] = None
    # rule id -> "update" (rule rewritten to match a sheet row) or "strip" (an
    # overlapping rule losing warehouses to a sheet row); used by the run summary.
    edit_kinds: dict = None


def _mapping_for(rule, fsn):
    for mapping in rule["consequence"]["freebieDealsMapping"]:
        if mapping["fsn"] == fsn:
            return mapping
    return None


def build_plan(targets, existing_rules, email, excluded_cohort_ids=None):
    """Upsert plan. For each target, find existing rules with the SAME FSN that
    share at least one warehouse. If a single-FSN one matches, UPDATE it in place
    (new time + warehouses/quantities); otherwise CREATE a new rule. Either way,
    the same FSN x warehouse is freed from any OTHER overlapping rules so the
    target's rule wins. This is why a re-run with a new time now actually updates
    that rule's time instead of spawning a duplicate."""
    by_fsn = {}
    for rule in existing_rules:
        for mapping in rule["consequence"]["freebieDealsMapping"]:
            by_fsn.setdefault(mapping["fsn"], []).append(rule)

    update_edits = []
    creates = []
    removals = {}         # rule_id -> {fsn -> set(warehouse_ids)} to strip
    existing_by_id = {}   # rule_id -> rule
    updated_ids = set()   # rules updated in place (never also stripped)

    for t in targets:
        target_whs = {w.warehouse_id for w in t.warehouses}
        # candidates: same FSN, overlapping warehouse, most overlap first
        candidates = []
        for rule in by_fsn.get(t.fsn, []):
            mapping = _mapping_for(rule, t.fsn)
            if mapping is None:
                continue
            overlap = target_whs & set(mapping["warehouseIds"])
            if overlap:
                candidates.append((len(overlap), rule))
        candidates.sort(key=lambda x: x[0], reverse=True)

        # primary = the best single-FSN candidate we can safely update in place
        primary = None
        for _ov, rule in candidates:
            if len(rule["consequence"]["freebieDealsMapping"]) == 1 and rule["id"] not in updated_ids:
                primary = rule
                break

        if primary is not None:
            updated_ids.add(primary["id"])
            update_edits.append(build_update_payload(primary, t, email, excluded_cohort_ids))
        else:
            creates.append(build_create_payload(t, email, excluded_cohort_ids))

        # free the target's warehouses from every OTHER overlapping rule
        for _ov, rule in candidates:
            if primary is not None and rule["id"] == primary["id"]:
                continue
            mapping = _mapping_for(rule, t.fsn)
            overlap = target_whs & set(mapping["warehouseIds"])
            if overlap:
                existing_by_id[rule["id"]] = rule
                removals.setdefault(rule["id"], {}).setdefault(t.fsn, set()).update(overlap)

    # strip-edits first so freed warehouses are available, then in-place updates
    strip_edits = [build_edit_payload(existing_by_id[rid], rem, email)
                   for rid, rem in removals.items() if rid not in updated_ids]
    edits = strip_edits + update_edits
    edit_kinds = {str(e["ruleId"]): "strip" for e in strip_edits}
    edit_kinds.update({str(e["ruleId"]): "update" for e in update_edits})
    return Plan(edits=edits, creates=creates, excluded_cohort_ids=list(excluded_cohort_ids or []),
                edit_kinds=edit_kinds)


def filter_plan(plan, edit_ids, create_names):
    """Narrow a freshly-built plan to only the items that previously failed, so a
    re-run (after deleting conflicting rules) doesn't re-touch items that already
    succeeded. edit_ids is a set of rule-id strings; create_names a set of names."""
    edits = [e for e in plan.edits if str(e["ruleId"]) in edit_ids]
    creates = [c for c in plan.creates if c["rule"]["name"] in create_names]
    return Plan(edits=edits, creates=creates, excluded_cohort_ids=plan.excluded_cohort_ids,
                edit_kinds=plan.edit_kinds)


def _fmt_window(s_ms, e_ms):
    def f(ms):
        try:
            ms = int(ms)
        except (TypeError, ValueError):
            return "?"
        return datetime.fromtimestamp(ms / 1000, IST).strftime("%d %b %H:%M") if ms > 0 else "?"
    return f"{f(s_ms)} -> {f(e_ms)}"


def render_preview(plan):
    lines = [f"PLAN: {len(plan.edits)} edit(s), {len(plan.creates)} create(s)"]
    if plan.excluded_cohort_ids:
        lines.append(f"Excluded cohorts (NOT_IN hurdle on every new rule): {plan.excluded_cohort_ids}")
    lines.append("")
    if plan.edits:
        lines.append("EDITS (update existing rules in place / free overlapping warehouses):")
        for e in plan.edits:
            rule = e["rule"]
            m = rule["consequence"]["freebieDealsMapping"][0]
            win = _fmt_window(rule.get("startTime"), rule.get("endTime"))
            act = "" if rule.get("isActive", True) else " [DEACTIVATED]"
            lines.append(f"  rule {e['ruleId']} ({m['fsn']}) | {win}{act} "
                         f"-> warehouses {m['warehouseIds']}")
        lines.append("")
    lines.append("CREATES:")
    for c in plan.creates:
        rule = c["rule"]
        m = rule["consequence"]["freebieDealsMapping"][0]
        win = _fmt_window(rule.get("startTime"), rule.get("endTime"))
        whs = ", ".join(
            f"{cfg['warehouseId']}:{cfg['consumptionType']}" +
            (f"({cfg['inventoryCap']})" if cfg.get('inventoryCap') is not None else "")
            for cfg in m["warehouseInventoryConfigs"])
        lines.append(f"  {rule['name']} | fsn={m['fsn']} | {win} | threshold={m['cartValueThreshold']} "
                     f"| active={rule['isActive']} | [{whs}]")
    return "\n".join(lines)


def _new_rule_id(resp):
    try:
        return resp["body"]["rule"]["id"]
    except (KeyError, TypeError):
        return ""


def execute_plan(plan, client):
    """Run every edit, then every create. ``outcomes`` keeps one entry per item in
    plan order (payload, ok, new/edited rule id, error) for the run summary."""
    result = {"edited": [], "created": [], "failed": [], "outcomes": []}
    for e in plan.edits:
        try:
            client.edit_rule(e)
            result["edited"].append(e["ruleId"])
            result["outcomes"].append(("edit", e, True, str(e["ruleId"]), ""))
        except Exception as ex:  # noqa: BLE001 - report and continue
            result["failed"].append(("edit", e["ruleId"], str(ex)))
            result["outcomes"].append(("edit", e, False, str(e["ruleId"]), str(ex)))
    for c in plan.creates:
        name = c["rule"]["name"]
        try:
            resp = client.create_rule(c)
            result["created"].append(name)
            result["outcomes"].append(("create", c, True, _new_rule_id(resp), ""))
        except Exception as ex:  # noqa: BLE001 - report and continue
            result["failed"].append(("create", name, str(ex)))
            result["outcomes"].append(("create", c, False, "", str(ex)))
    return result
