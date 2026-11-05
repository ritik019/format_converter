"""Deals CSV -> Pricing and Offers (pno) plan, for the web app's preview/execute.

Since 5 Nov 2026 deals live on portal.controlgrid.in/pricing-and-offers. The old
rules API refuses a parent FSN ("must be a cloned product"); the new one takes the
parent FSN plus a fixed deal price. This module turns the converter's CSV rows
(already parsed into ``TargetRule``) into pno create/update actions:

  * child_fcn column        -> the product's parent FSN
  * deal_price              -> FIXED_PRICE consequence (required)
  * cart_value_threshold    -> CART_VALUE hurdle (sheet already has MOV - 12)
  * PARTIAL n / FULL        -> per-CH daily cap n / uncapped
  * excluded cohort ids     -> USER_COHORT NOT_IN

A CH already held by another open offer on the same FSN is skipped and reported
(one offer per product per CH). An open rule with the same FSN + MOV is updated in
place; otherwise a new rule is created. A start time in the past is moved to two
minutes from now, and an updated rule that has already started keeps its start.
"""
import dataclasses

try:
    from freebie.pno import OPEN_STATUSES
    from freebie.pno_payloads import validate
    from freebie.pno_payloads import from_target_rule
    from freebie.pno_planner import plan_upserts
except ImportError:  # pragma: no cover - deploy layout (files at repo root)
    from pno import OPEN_STATUSES
    from pno_payloads import validate
    from pno_payloads import from_target_rule
    from pno_planner import plan_upserts

START_LEAD_MS = 2 * 60 * 1000


def to_pno_targets(targets, cohorts, now_ms):
    """TargetRule list -> (PnoTarget list, error strings). Nothing is sent."""
    out, errors = [], []
    for tr in targets:
        label = f"{tr.rule_name} ({tr.fsn})"
        if not tr.is_active:
            errors.append(f"{label}: inactive rows can't be created on the new portal - "
                          f"remove the row or set activation TRUE")
            continue
        if tr.deal_price is None or tr.deal_price <= 0:
            errors.append(f"{label}: deal_price is missing - fill the sheet's Price column")
            continue
        try:
            t = from_target_rule(tr, price_mode="FIXED_PRICE", price_value=tr.deal_price)
        except ValueError as e:
            errors.append(str(e))
            continue
        start = t.start_ms if t.start_ms >= now_ms + START_LEAD_MS else now_ms + START_LEAD_MS
        if t.end_ms <= start:
            errors.append(f"{label}: end time is already past")
            continue
        t = dataclasses.replace(t, start_ms=start, cohort_out=list(cohorts or []))
        try:
            validate(t)
        except ValueError as e:
            errors.append(f"{label}: {e}")
            continue
        out.append(t)
    return out, errors


def open_rules_for(client, fsns):
    """Full views of every open pno rule on these FSNs (GET only)."""
    found = {}
    fsns = sorted({f.strip() for f in fsns if f and f.strip()})
    for i in range(0, len(fsns), 50):
        for r in client.search(fsn=fsns[i:i + 50], status=list(OPEN_STATUSES)):
            found[r["id"]] = r
    views = []
    for rid, r in found.items():
        views.append(r if r.get("hurdles") and r.get("version") is not None else client.get_rule(rid))
    return views


def build_pno_plan(client, targets, cohorts, now_ms):
    """-> (actions, errors, n_existing). Reads only."""
    pts, errors = to_pno_targets(targets, cohorts, now_ms)
    if not pts:
        return [], errors, 0
    views = open_rules_for(client, [t.fsn for t in pts])
    actions = plan_upserts(pts, views)
    by_id = {v["id"]: v for v in views}
    for a in actions:
        if a.kind == "update" and a.body:
            v = by_id.get(a.rule_id) or {}
            if v.get("startTime") and v["startTime"] <= now_ms:
                a.body["startTime"] = v["startTime"]   # already running: keep its start
    return actions, errors, len(views)
