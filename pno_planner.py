"""Plan, preview and execute pno writes. Planning is pure; execute() is the only writer.

Rules of the new portal that shape the plan (SOP + form.js):
  * One offer per product per warehouse is shown. A deal AND a freebie on the same parent
    FSN x warehouse clash too, and paused rules count. So a target's warehouse that is
    already held by ANOTHER open rule is SKIPPED (and reported), never stripped from that
    rule — the same "skip conflicting CHs" approach the user asked for on the old portal.
  * A target whose FSN + template + MOV matches exactly one open rule updates that rule in
    place (PUT with its version). Otherwise it is created.
  * No delete: retiring is POST /end.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

try:
    from freebie.pno import rule_fsns
    from freebie.pno_payloads import FULL, build_create_body, build_edit_body, view_limits
    from freebie.timeutil import IST
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from pno import rule_fsns
    from pno_payloads import FULL, build_create_body, build_edit_body, view_limits
    from timeutil import IST

OPEN = ("LIVE", "PAUSED", "SCHEDULED")


@dataclass
class Action:
    kind: str                     # create | update | end | pause | resume | skip
    label: str
    body: Optional[dict] = None
    rule_id: Optional[str] = None
    version: Optional[int] = None
    skipped: dict = field(default_factory=dict)   # {warehouse: "rule name (id)"}
    note: str = ""


def _cv(rule):
    h = next((h for h in rule.get("hurdles") or [] if h.get("type") == "CART_VALUE"), None)
    return float(h["value"]) if h and h.get("value") is not None else None


def _rule_whs(rule):
    return set(view_limits(rule)) or {w.get("warehouseId") for w in rule.get("warehouses") or []}


def plan_upserts(targets, open_rules):
    by_fsn = {}
    for r in open_rules:
        if r.get("status") in OPEN:
            for f in rule_fsns(r):
                by_fsn.setdefault(f, []).append(r)

    actions, claimed = [], {}   # claimed: (fsn, wh) -> label, to catch clashes inside the batch
    for t in targets:
        fsn = t.fsn.strip()
        rules = by_fsn.get(fsn, [])
        same = [r for r in rules if r.get("subType") == t.template
                and _cv(r) == (float(t.cart_value) if t.cart_value is not None else None)]
        primary = same[0] if len(same) == 1 else None

        want = dict(t.warehouses)
        skipped = {}
        for wh in list(want):
            for r in rules:
                if primary is not None and r["id"] == primary["id"]:
                    continue
                if wh in _rule_whs(r):
                    skipped[wh] = f"{r.get('name')} ({r['id']})"
                    break
            if wh not in skipped and (fsn, wh) in claimed:
                skipped[wh] = f"earlier row in this batch: {claimed[(fsn, wh)]}"
        for wh in skipped:
            want.pop(wh)

        note = f"{len(same)} open {t.template} rules share FSN+MOV; created new" if len(same) > 1 else ""
        if not want:
            actions.append(Action("skip", t.name, skipped=skipped,
                                  note="every warehouse is held by another open offer"))
            continue
        for wh in want:
            claimed[(fsn, wh)] = t.name

        t2 = _with_warehouses(t, want)
        if primary is not None:
            body = build_edit_body(primary, start_ms=t.start_ms, end_ms=t.end_ms, warehouses=want,
                                   units_reset=t.units_reset, title=t.title, locked=t.locked,
                                   unlocked=t.unlocked, name=t.name,
                                   price_value=t.price_value if t.is_deal else None)
            actions.append(Action("update", t.name, body=body, rule_id=primary["id"],
                                  version=primary.get("version"), skipped=skipped))
        else:
            actions.append(Action("create", t.name, body=build_create_body(t2),
                                  skipped=skipped, note=note))
    return actions


def _with_warehouses(t, whs):
    import dataclasses
    return dataclasses.replace(t, warehouses=dict(whs))


def plan_edit(views, **changes):
    """Same field change on many rules, e.g. plan_edit(views, end_ms=...) to extend."""
    out = []
    for v in views:
        try:
            body = build_edit_body(v, **changes)
        except ValueError as e:
            out.append(Action("skip", v.get("name") or v.get("id"), rule_id=v.get("id"), note=str(e)))
            continue
        out.append(Action("update", v.get("name"), body=body, rule_id=v["id"], version=v.get("version")))
    return out


def plan_status(views, verb, reason=None):
    allowed = {"pause": ("LIVE",), "resume": ("PAUSED",), "end": ("LIVE", "PAUSED", "SCHEDULED")}[verb]
    out = []
    for v in views:
        if v.get("status") not in allowed:
            out.append(Action("skip", v.get("name"), rule_id=v.get("id"),
                              note=f"status {v.get('status')} cannot {verb}"))
        else:
            out.append(Action(verb, v.get("name"), rule_id=v["id"], version=v.get("version"),
                              body={"reason": reason}))
    return out


def _fmt(ms):
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%d %b %H:%M") if ms else "?"


def _caps(body):
    cap = next((h for h in body["hurdles"] if h.get("type") == "TOTAL_REDEMPTION_PER_WAREHOUSE"), None)
    limits = (cap or {}).get("limitsByWarehouse") or {}
    full = [w for w in body["warehouseIds"] if w not in limits]
    parts = [f"{w}={n}" for w, n in sorted(limits.items())]
    if full:
        parts.append(f"FULL x{len(full)}")
    reset = (cap or {}).get("unitsReset")
    return ", ".join(parts) + (f" [{reset}]" if reset else "")


def render_preview(actions):
    counts = {}
    for a in actions:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    lines = ["PLAN: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())), ""]
    for a in actions:
        head = f"[{a.kind.upper()}] {a.label}" + (f"  ({a.rule_id} v{a.version})" if a.rule_id else "")
        lines.append(head)
        b = a.body or {}
        if a.kind in ("create", "update"):
            cv = next((h.get("value") for h in b["hurdles"] if h.get("type") == "CART_VALUE"), None)
            cons = b["consequence"]
            out = (f"{cons.get('mode')} {cons.get('value')}" if cons.get("type") == "PRICING_DEAL"
                   else "free " + ",".join(i["fsn"] for i in cons.get("items", [])))
            lines.append(f"    {b['subType']} | {_fmt(b['startTime'])} -> {_fmt(b['endTime'])} | "
                         f"MOV={cv} | {out} | {len(b['warehouseIds'])} CH: {_caps(b)}")
            lines.append(f"    title={b['metaInfo']['userMessage'].get('offerTitle')!r}")
        for wh, why in sorted(a.skipped.items()):
            lines.append(f"    SKIP {wh}: held by {why}")
        if a.note:
            lines.append(f"    note: {a.note}")
    return "\n".join(lines)


def execute(actions, client):
    """Run the plan with a client built with allow_writes=True. Reports and continues."""
    result = {"done": [], "failed": []}
    for a in actions:
        try:
            if a.kind == "create":
                res = client.create_rule(a.body)
                result["done"].append(("create", a.label, (res or {}).get("id")))
            elif a.kind == "update":
                client.update_rule(a.rule_id, a.body)
                result["done"].append(("update", a.label, a.rule_id))
            elif a.kind in ("pause", "resume", "end"):
                client.change_status(a.rule_id, a.kind, a.version, (a.body or {}).get("reason"))
                result["done"].append((a.kind, a.label, a.rule_id))
        except Exception as ex:  # noqa: BLE001 - report and continue
            details = getattr(ex, "details", None)
            result["failed"].append((a.kind, a.label, f"{ex}" + (f" | {details}" if details else "")))
    return result
