"""Turn controlgrid conflict errors into the rules that caused them, so the UI can
name each blocking rule (id + type) and let the user pick which to delete.

Two error shapes block a create/edit:

    controlgrid 400: FSN x Warehouse conflict with rule 'NAME' (id: UUID): [FSN x WAREHOUSE]
    controlgrid 400: Timeline overlap for FSN 'FSN' in warehouse 'WH': the requested
        window overlaps or touches an existing offer's window.

The first names its rule. The second does not, so the blocking rule is looked up
in the type index: any rule that maps the same FSN at the same warehouse with a
window that overlaps or touches the requested one. ControlGrid's check ignores
isActive, so inactive rules count as blockers too.
"""
import json
import re

# Rule name may contain apostrophes ("Let's Try...", "Bagrry'S..."), so anchor on
# the literal " (id: " that always follows the closing quote.
_CONFLICT_RE = re.compile(
    r"with rule '(?P<name>.+)' \(id: (?P<id>[0-9a-fA-F-]+)\)\s*:\s*"
    r"\[(?P<fsn>\S+)\s*x\s*(?P<wh>\S+)\]"
)
RULE_TYPE_LABELS = {
    "FREEBIE_DEALS": "Freebie Deal",
    "FREEBIE_CART_VALUE": "Freebie Cart Value",
    "REFERRER": "Referral",
}

_OVERLAP_RE = re.compile(
    r"Timeline overlap for FSN '(?P<fsn>[^']+)' in warehouse '(?P<wh>[^']+)'", re.I
)


def parse_conflict(err_msg):
    """Return dict(name, id, fsn, warehouse) for a conflict error, else None."""
    if not err_msg or "conflict" not in err_msg.lower():
        return None
    m = _CONFLICT_RE.search(err_msg)
    if not m:
        return None
    return {
        "name": m.group("name"),
        "id": m.group("id"),
        "fsn": m.group("fsn"),
        "warehouse": m.group("wh"),
    }


def parse_overlap(err_msg):
    """Return dict(fsn, warehouse) for a 'Timeline overlap' error, else None."""
    m = _OVERLAP_RE.search(err_msg or "")
    if not m:
        return None
    return {"fsn": m.group("fsn"), "warehouse": m.group("wh")}


def _consequence(raw):
    c = raw.get("consequence")
    if isinstance(c, dict):
        return c
    try:
        return json.loads(raw.get("consequences") or "{}")
    except (TypeError, ValueError):
        return {}


def rule_pairs(raw):
    """Set of (fsn, warehouse) a rule holds, across the deal, cart-value and
    referral consequence shapes."""
    c = _consequence(raw)
    pairs = set()
    for key in ("freebieDealsMapping", "freebieCartValueMappings", "referralProductMappings"):
        for m in c.get(key) or []:
            whs = m.get("warehouseIds") or [w.get("warehouseId")
                                            for w in m.get("warehouseInventoryConfigs") or []]
            pairs.update((m.get("fsn"), w) for w in whs if w)
    for m in c.get("warehouseProductMappings") or []:
        pairs.update((f, m.get("warehouseId")) for f in m.get("fsnIds") or [])
    return pairs


def _touches(info, window):
    """True when the rule's window overlaps or touches window=(start_ms, end_ms).
    With no window to compare against, every holder is reported."""
    if not window:
        return True
    try:
        start, end = int(info.get("startTime")), int(info.get("endTime"))
        return start <= int(window[1]) and end >= int(window[0])
    except (TypeError, ValueError):
        return True


def find_overlap_owners(fsn, warehouse, type_index, window=None, exclude_id=None):
    """Rule ids in type_index holding (fsn, warehouse) with a window touching ours."""
    owners = []
    for rid, info in type_index.items():
        if rid == exclude_id:
            continue
        if (fsn, warehouse) in rule_pairs(info.get("raw") or {}) and _touches(info, window):
            owners.append(rid)
    return owners


def blockers_for(kind, ident, err, type_index=None, windows=None):
    """The rules blocking one failure: a list of {id, name, rule_type, info, fsn,
    warehouse}. Empty when the error is not a conflict or no owner was found.

    windows maps (kind, ident) -> (start_ms, end_ms) of the requested rule; for an
    edit, ident is the rule's own id, which is never reported as its own blocker."""
    type_index = type_index or {}

    def entry(rid, name, fsn, wh):
        info = type_index.get(rid) or {}
        return {"id": rid, "name": name or info.get("name") or "",
                "rule_type": info.get("ruleType", ""), "info": info,
                "fsn": fsn, "warehouse": wh}

    c = parse_conflict(err)
    if c:
        return [entry(c["id"], c["name"], c["fsn"], c["warehouse"])]
    o = parse_overlap(err)
    if o:
        window = (windows or {}).get((kind, str(ident)))
        self_id = str(ident) if kind == "edit" else None
        return [entry(rid, None, o["fsn"], o["warehouse"])
                for rid in find_overlap_owners(o["fsn"], o["warehouse"], type_index,
                                               window, self_id)]
    return []


def explain_failures(failures, type_index=None, windows=None):
    """[(kind, ident, err)] -> [(kind, ident, err, blockers)]."""
    return [(k, i, e, blockers_for(k, i, e, type_index, windows)) for k, i, e in failures]


def collect_conflicts(failures, type_index=None, windows=None):
    """Group failures by the conflicting rule's id.

    failures:   list of (kind, ident, err) as returned by execute_plan, or the
                4-tuples from explain_failures.
    type_index: optional {rule_id: {"ruleType": ..., "raw": ...}} for labelling rule
                type and for finding the owner of a 'Timeline overlap'.

    Returns a list of dicts: {id, name, rule_type, info, pairs:[(fsn, warehouse), ...]},
    one per distinct conflicting rule, sorted by name. ``info`` is the matching
    type_index entry (status / times / hurdles / raw rule) for rendering a summary,
    or {} when the rule was not found in the index.
    """
    explained = [f if len(f) == 4 else (*f, blockers_for(*f, type_index, windows))
                 for f in failures]
    by_id = {}
    for _kind, _ident, _err, blockers in explained:
        for b in blockers:
            entry = by_id.setdefault(b["id"], {
                "id": b["id"],
                "name": b["name"],
                "rule_type": b["rule_type"],
                "info": b["info"],
                "pairs": [],
            })
            pair = (b["fsn"], b["warehouse"])
            if pair not in entry["pairs"]:
                entry["pairs"].append(pair)
    return sorted(by_id.values(), key=lambda e: (e["name"] or "").lower())


# --------------------------------------------------------------------------- #
# Before execution: which existing rules will block the plan
# --------------------------------------------------------------------------- #

PLACEHOLDER_WAREHOUSE = "FCHBLRJMH01"

# Risk of deleting a blocker, most serious first. The level drives the chip colour.
RISK_REFERRAL = ("referral", "Can't delete here - remove this FSN from your sheet")
RISK_ORDER = {"referral": 0, "high": 1, "medium": 2, "low": 3}


def rule_state(info, now_ms):
    """'expired' / 'live' / 'upcoming' from the rule's window against now."""
    try:
        start, end = int(info.get("startTime")), int(info.get("endTime"))
    except (TypeError, ValueError):
        return "unknown"
    if end <= now_ms:
        return "expired"
    return "live" if start <= now_ms else "upcoming"


def deletion_risk(info, blocked_fsns, now_ms):
    """(level, why) for deleting this rule to unblock the plan."""
    if info.get("ruleType") == "REFERRER":
        return RISK_REFERRAL
    pairs = rule_pairs(info.get("raw") or {})
    others = {f for f, _w in pairs} - set(blocked_fsns)
    if others:
        return ("high", f"Also carries {len(others)} other FSN(s) - deleting removes those offers too")
    state = rule_state(info, now_ms)
    if state == "expired":
        return ("low", "Already ended - it blocks only because its window touches yours")
    if (info.get("ruleStatus") or "").upper() != "ACTIVE":
        return ("low", "Inactive - customers don't see it")
    if {w for _f, w in pairs} == {PLACEHOLDER_WAREHOUSE}:
        return ("low", "JMH-only placeholder of a retiring rule")
    if state == "upcoming":
        return ("medium", "Scheduled - not live yet")
    return ("high", "Live offer customers can see right now")


def predict_conflicts(plan, type_index, now_ms):
    """Existing rules that will block a create or in-place update in ``plan``.

    Each rule is judged as it will look after the plan's own edits run (a rule the
    plan strips no longer holds those CHs; one emptied out holds the JMH
    placeholder). A blocker holds the same FSN at the same CH with a window that
    overlaps or touches the requested one. Returns one dict per blocking rule:
    {id, name, rule_type, info, pairs, blocks, state, risk}, riskiest first."""
    type_index = type_index or {}
    after = {rid: (rule_pairs(info.get("raw") or {}), info.get("startTime"), info.get("endTime"))
             for rid, info in type_index.items()}
    requests = []
    for e in plan.edits:
        rid, rule = str(e["ruleId"]), e["rule"]
        after[rid] = (rule_pairs(rule), rule.get("startTime"), rule.get("endTime"))
        if (plan.edit_kinds or {}).get(rid, "update") == "update":
            requests.append((rule["name"], rule, rid))
    requests += [(c["rule"]["name"], c["rule"], None) for c in plan.creates]

    holders = {}
    for rid, (pairs, _s, _e) in after.items():
        for p in pairs:
            holders.setdefault(p, []).append(rid)

    found = {}
    for label, rule, self_id in requests:
        window = (rule.get("startTime"), rule.get("endTime"))
        for pair in sorted(rule_pairs(rule)):
            for rid in holders.get(pair, []):
                if rid == self_id or rid not in type_index:
                    continue
                _p, s, e = after[rid]
                if not _touches({"startTime": s, "endTime": e}, window):
                    continue
                info = type_index[rid]
                entry = found.setdefault(rid, {
                    "id": rid, "name": info.get("name") or "", "rule_type": info.get("ruleType", ""),
                    "info": info, "pairs": [], "blocks": []})
                if pair not in entry["pairs"]:
                    entry["pairs"].append(pair)
                if label not in entry["blocks"]:
                    entry["blocks"].append(label)
    for entry in found.values():
        entry["state"] = rule_state(entry["info"], now_ms)
        entry["risk"] = deletion_risk(entry["info"], {f for f, _w in entry["pairs"]}, now_ms)
    return sorted(found.values(), key=lambda c: (RISK_ORDER[c["risk"][0]], c["name"].lower()))
