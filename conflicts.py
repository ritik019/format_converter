"""Parse controlgrid 'FSN x Warehouse conflict' error messages into structured
records so the UI can list the offending rules and let the user pick which to delete.

A conflict failure looks like:
    controlgrid 400: FSN x Warehouse conflict with rule 'NAME' (id: UUID): [FSN x WAREHOUSE]
"""
import re

# Rule name may contain apostrophes ("Let's Try...", "Bagrry'S..."), so anchor on
# the literal " (id: " that always follows the closing quote.
_CONFLICT_RE = re.compile(
    r"with rule '(?P<name>.+)' \(id: (?P<id>[0-9a-fA-F-]+)\)\s*:\s*"
    r"\[(?P<fsn>\S+)\s*x\s*(?P<wh>\S+)\]"
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


def collect_conflicts(failures, type_index=None):
    """Group failures by the conflicting rule's id.

    failures:   list of (kind, ident, err) as returned by execute_plan.
    type_index: optional {rule_id: {"ruleType": ...}} for labelling rule type.

    Returns a list of dicts: {id, name, rule_type, info, pairs:[(fsn, warehouse), ...]},
    one per distinct conflicting rule, sorted by name. ``info`` is the matching
    type_index entry (status / times / hurdles / raw rule) for rendering a summary,
    or {} when the rule was not found in the index.
    """
    type_index = type_index or {}
    by_id = {}
    for _kind, _ident, err in failures:
        c = parse_conflict(err)
        if not c:
            continue
        info = type_index.get(c["id"]) or {}
        entry = by_id.setdefault(c["id"], {
            "id": c["id"],
            "name": c["name"],
            "rule_type": info.get("ruleType", ""),
            "info": info,
            "pairs": [],
        })
        pair = (c["fsn"], c["warehouse"])
        if pair not in entry["pairs"]:
            entry["pairs"].append(pair)
    return sorted(by_id.values(), key=lambda e: (e["name"] or "").lower())
