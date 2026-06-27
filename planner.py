from dataclasses import dataclass
from typing import List

try:  # works inside the freebie package and as a flat deploy repo
    from freebie.payloads import build_create_payload, build_edit_payload
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from payloads import build_create_payload, build_edit_payload


@dataclass
class Plan:
    edits: List[dict]
    creates: List[dict]
    excluded_cohort_ids: List[str] = None


def build_plan(targets, existing_rules, email, excluded_cohort_ids=None):
    # index existing rules by fsn
    by_fsn = {}
    for rule in existing_rules:
        for mapping in rule["consequence"]["freebieDealsMapping"]:
            by_fsn.setdefault(mapping["fsn"], []).append(rule)

    # accumulate removals per existing rule id
    removals = {}        # rule_id -> {fsn -> set(warehouse_ids)}
    existing_by_id = {}  # rule_id -> rule
    for t in targets:
        target_whs = {w.warehouse_id for w in t.warehouses}
        for rule in by_fsn.get(t.fsn, []):
            for mapping in rule["consequence"]["freebieDealsMapping"]:
                if mapping["fsn"] != t.fsn:
                    continue
                overlap = target_whs & set(mapping["warehouseIds"])
                if overlap:
                    existing_by_id[rule["id"]] = rule
                    removals.setdefault(rule["id"], {}).setdefault(t.fsn, set()).update(overlap)

    edits = [build_edit_payload(existing_by_id[rid], rem, email)
             for rid, rem in removals.items()]
    creates = [build_create_payload(t, email, excluded_cohort_ids) for t in targets]
    return Plan(edits=edits, creates=creates, excluded_cohort_ids=list(excluded_cohort_ids or []))


def filter_plan(plan, edit_ids, create_names):
    """Narrow a freshly-built plan to only the items that previously failed, so a
    re-run (after deleting conflicting rules) doesn't re-touch items that already
    succeeded. edit_ids is a set of rule-id strings; create_names a set of names."""
    edits = [e for e in plan.edits if str(e["ruleId"]) in edit_ids]
    creates = [c for c in plan.creates if c["rule"]["name"] in create_names]
    return Plan(edits=edits, creates=creates, excluded_cohort_ids=plan.excluded_cohort_ids)


def render_preview(plan):
    lines = [f"PLAN: {len(plan.edits)} edit(s), {len(plan.creates)} create(s)"]
    if plan.excluded_cohort_ids:
        lines.append(f"Excluded cohorts (NOT_IN hurdle on every new rule): {plan.excluded_cohort_ids}")
    lines.append("")
    if plan.edits:
        lines.append("EDITS (cleanup of overlapping FSN x warehouse):")
        for e in plan.edits:
            m = e["rule"]["consequence"]["freebieDealsMapping"][0]
            lines.append(f"  rule {e['ruleId']} ({m['fsn']}) -> warehouses now {m['warehouseIds']}")
        lines.append("")
    lines.append("CREATES:")
    for c in plan.creates:
        rule = c["rule"]
        m = rule["consequence"]["freebieDealsMapping"][0]
        whs = ", ".join(
            f"{cfg['warehouseId']}:{cfg['consumptionType']}" +
            (f"({cfg['inventoryCap']})" if cfg.get('inventoryCap') is not None else "")
            for cfg in m["warehouseInventoryConfigs"])
        lines.append(f"  {rule['name']} | fsn={m['fsn']} | threshold={m['cartValueThreshold']} "
                     f"| active={rule['isActive']} | [{whs}]")
    return "\n".join(lines)


def execute_plan(plan, client):
    result = {"edited": [], "created": [], "failed": []}
    for e in plan.edits:
        try:
            client.edit_rule(e)
            result["edited"].append(e["ruleId"])
        except Exception as ex:  # noqa: BLE001 - report and continue
            result["failed"].append(("edit", e["ruleId"], str(ex)))
    for c in plan.creates:
        name = c["rule"]["name"]
        try:
            client.create_rule(c)
            result["created"].append(name)
        except Exception as ex:  # noqa: BLE001 - report and continue
            result["failed"].append(("create", name, str(ex)))
    return result
