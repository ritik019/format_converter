import copy

DEFAULT_WAREHOUSE = "FCHBLRJMH01"


def _wh_config_dict(w):
    d = {"warehouseId": w.warehouse_id, "consumptionType": w.consumption_type}
    if w.consumption_type == "PARTIAL":
        d["inventoryCap"] = w.inventory_cap
    return d


def _cohort_exclusion_hurdle(cohort_ids, description):
    # ControlGrid stores the cohort IDs under "value" (confirmed from a live
    # rule read-back), so that is the canonical field we write and read.
    return {
        "type": "USER_COHORT",
        "name": "cohort_exclusion",
        "key": "cohort_exclusion",
        "operation": "NOT_IN",
        "value": list(cohort_ids),
        "description": description,
        "failureMessage": description,
        "successMessage": description,
    }


def build_create_payload(target, email, excluded_cohort_ids=None):
    mapping = {
        "fsn": target.fsn,
        "title": target.title,
        "warehouseIds": [w.warehouse_id for w in target.warehouses],
        "warehouseInventoryConfigs": [_wh_config_dict(w) for w in target.warehouses],
        "cartValueThreshold": target.cart_value_threshold,
        "description": target.cart_description,
    }
    hurdle_list = []
    if excluded_cohort_ids:
        hurdle_list.append(_cohort_exclusion_hurdle(excluded_cohort_ids, target.rule_description))
    rule = {
        "id": None,
        "name": target.rule_name,
        "campaignId": None,
        "hurdleList": hurdle_list,
        "successMessage": target.rule_description,
        "failureMessage": target.rule_description,
        "isActive": target.is_active,
        "consequence": {
            "type": "FREEBIE_DEALS",
            "replenishmentType": target.replenishment_type,
            "freebieDealsMapping": [mapping],
        },
        "description": target.rule_description,
        "ruleType": "FREEBIE_DEALS",
        "startTime": target.start_ms,
        "endTime": target.end_ms,
    }
    return {"rule": rule, "campaignId": None, "createdBy": email, "user": email}


def merge_cohort_exclusion(hurdle_list, cohort_ids, description):
    """Return a hurdleList with the given cohort IDs excluded via a NOT_IN
    USER_COHORT hurdle. If one already exists, merge IDs into it (no dupes,
    order preserved); otherwise append a fresh one."""
    hurdles = copy.deepcopy(hurdle_list or [])
    for h in hurdles:
        if h.get("type") == "USER_COHORT" and h.get("operation") == "NOT_IN":
            # accept either key when reading back an existing hurdle
            existing_ids = h.get("value") or h.get("requiredCohortIds") or []
            merged = list(existing_ids)
            for cid in cohort_ids:
                if cid not in merged:
                    merged.append(cid)
            h.pop("requiredCohortIds", None)
            h["value"] = merged
            return hurdles
    hurdles.append(_cohort_exclusion_hurdle(cohort_ids, description))
    return hurdles


def build_cohort_exclusion_edit_payload(existing, cohort_ids, email):
    """Edit payload that leaves the consequence untouched and only adds/merges
    a cohort-exclusion (NOT_IN) hurdle onto an existing rule."""
    description = existing.get("description") or "cohort exclusion"
    hurdle_list = merge_cohort_exclusion(
        existing.get("hurdleList", []), cohort_ids, description)
    rule = {
        "id": existing["id"],
        "name": existing["ruleName"],
        "campaignId": None,
        "hurdleList": hurdle_list,
        "successMessage": existing.get("successMessage"),
        "failureMessage": existing.get("failureMessage"),
        "isActive": existing.get("ruleStatus") == "ACTIVE",
        "consequence": existing["consequence"],
        "description": existing.get("description"),
        "ruleType": "FREEBIE_DEALS",
        "startTime": existing["startTime"],
        "endTime": existing["endTime"],
    }
    return {"ruleId": existing["id"], "updatedBy": email, "user": email,
            "campaignId": None, "rule": rule}


def build_update_payload(existing, target, email, excluded_cohort_ids=None):
    """Edit payload that updates an existing rule IN PLACE to match the target:
    new start/end time, warehouses + quantities, title/threshold/description and
    active flag, while keeping the rule's id. Existing cohort hurdles are preserved
    and any excluded cohort IDs are merged in. Intended for single-FSN rules (the
    planner only routes single-mapping rules here, so other FSNs are never dropped)."""
    mapping = {
        "fsn": target.fsn,
        "title": target.title,
        "warehouseIds": [w.warehouse_id for w in target.warehouses],
        "warehouseInventoryConfigs": [_wh_config_dict(w) for w in target.warehouses],
        "cartValueThreshold": target.cart_value_threshold,
        "description": target.cart_description,
    }
    hurdle_list = existing.get("hurdleList", []) or []
    if excluded_cohort_ids:
        hurdle_list = merge_cohort_exclusion(hurdle_list, excluded_cohort_ids, target.rule_description)
    rule = {
        "id": existing["id"],
        "name": target.rule_name,
        "campaignId": None,
        "hurdleList": hurdle_list,
        "successMessage": target.rule_description,
        "failureMessage": target.rule_description,
        "isActive": target.is_active,
        "consequence": {
            "type": "FREEBIE_DEALS",
            "replenishmentType": target.replenishment_type,
            "freebieDealsMapping": [mapping],
        },
        "description": target.rule_description,
        "ruleType": "FREEBIE_DEALS",
        "startTime": target.start_ms,
        "endTime": target.end_ms,
    }
    return {"ruleId": existing["id"], "updatedBy": email, "user": email,
            "campaignId": None, "rule": rule}


def build_edit_payload(existing, removals_by_fsn, email, default_warehouse=DEFAULT_WAREHOUSE):
    consequence = copy.deepcopy(existing["consequence"])
    for mapping in consequence["freebieDealsMapping"]:
        remove = removals_by_fsn.get(mapping["fsn"])
        if not remove:
            continue
        mapping["warehouseIds"] = [w for w in mapping["warehouseIds"] if w not in remove]
        mapping["warehouseInventoryConfigs"] = [
            c for c in mapping["warehouseInventoryConfigs"] if c["warehouseId"] not in remove
        ]

    total = sum(len(m["warehouseIds"]) for m in consequence["freebieDealsMapping"])
    # When the cleanup strips a rule down to nothing, it no longer serves any
    # FSN x warehouse. Inject a placeholder warehouse only to keep the payload
    # schema valid, but DEACTIVATE the rule so it does not stay active owning an
    # unrelated warehouse (which would create fresh conflicts / "ghost" rules).
    emptied = total == 0
    if emptied:
        first = consequence["freebieDealsMapping"][0]
        first["warehouseIds"] = [default_warehouse]
        first["warehouseInventoryConfigs"] = [
            {"warehouseId": default_warehouse, "consumptionType": "FULL"}
        ]

    rule = {
        "id": existing["id"],
        "name": existing["ruleName"],
        "campaignId": None,
        "hurdleList": existing.get("hurdleList", []),
        "successMessage": existing.get("successMessage"),
        "failureMessage": existing.get("failureMessage"),
        "isActive": (existing.get("ruleStatus") == "ACTIVE") and not emptied,
        "consequence": consequence,
        "description": existing.get("description"),
        "ruleType": "FREEBIE_DEALS",
        "startTime": existing["startTime"],
        "endTime": existing["endTime"],
    }
    return {"ruleId": existing["id"], "updatedBy": email, "user": email,
            "campaignId": None, "rule": rule}
