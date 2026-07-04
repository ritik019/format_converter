"""Directly create FREEBIE_DEALS rules from inline inputs (no sheet).

Builds the same create payloads as the sheet pipeline (freebie.payloads.
build_create_payload), previews them, and only POSTs to ControlGrid after you
type 'yes'.

Run:
  FREEBIE_SESSION=... FREEBIE_XSRF=... [FREEBIE_EMAIL=...] python -m freebie.create_deals

Each deal is applied to every channel in CHANNELS except GTM_CHANNEL, with
PARTIAL consumption and inventory_cap = PARTIAL_CAP.
"""
from datetime import datetime, timedelta

from freebie.config import Config
from freebie.auth import make_post_fn
from freebie.controlgrid import ControlGrid
from freebie.models import WarehouseConfig, TargetRule
from freebie.payloads import build_create_payload
from freebie.planner import Plan, render_preview, execute_plan
from freebie.referral_guard import split_referral_conflicts
from freebie.timeutil import IST

# ---------------------------------------------------------------------------
# INPUTS  — fill these in
# ---------------------------------------------------------------------------

# All channels (warehouse IDs). The GTM channel below is excluded automatically.
CHANNELS = [
    "FCHBLRDOD01", "FCHBLRELC01", "FCHBLRHEB01", "FCHBLRHOO01", "FCHBLRHSAF01",
    "FCHBLRHSR01", "FCHBLRIND01", "FCHBLRKNP01", "FCHBLRRAJ01", "FCHBLRVAR01",
    "FCHBLRWHF01", "FCHBLRAECS01", "FCHBLRBEN01", "FCHBLRBHO01", "FCHBLRHAR01",
    "FCHBLRHOR01", "FCHBLRJMH01", "FCHBLRJPN01", "FCHBLRKOR01", "FCHBLRMDP01",
    "FCHBLRNAG01", "FCHBLRSJR01", "FCHBLRTHA01", "FCHHYDKND01", "FCHHYDMDL02",
    "FCHHYDNAR01", "FCHHYDNIZ01", "FCHHYDTEL01",
]

# Include all channels — nothing excluded.
GTM_CHANNEL = ""

PARTIAL_CAP = 10          # inventory_cap on every channel (PARTIAL)
CART_VALUE_THRESHOLD = 599  # MOV
REPLENISHMENT_TYPE = "DAILY"
IS_ACTIVE = True

# One entry per deal. Metadata fields are required by the create payload.
DEALS = [
    {
        "fsn": "FC260618021929",
        "rule_name": "TWT Double Cocoa Protein Bar",
        "title": "TWT Double Cocoa Protein Bar",
        "rule_description": "TWT Double Cocoa Protein Bar",
        "cart_description": "TWT Double Cocoa Protein Bar",
    },
    {
        "fsn": "FC260618021930",
        "rule_name": "TWT Coffee Cocoa Protein Bar",
        "title": "TWT Coffee Cocoa Protein Bar",
        "rule_description": "TWT Coffee Cocoa Protein Bar",
        "cart_description": "TWT Coffee Cocoa Protein Bar",
    },
]


def _start_end_ms():
    """Start = now (IST); End = today's EOD = next midnight (IST)."""
    now = datetime.now(IST)
    start_ms = int(now.timestamp() * 1000)
    eod = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_ms = int(eod.timestamp() * 1000)
    return start_ms, end_ms


def build_targets(deals):
    included = [c for c in CHANNELS if c != GTM_CHANNEL]
    if not included:
        raise SystemExit("No channels to apply after excluding GTM. Fill CHANNELS.")
    start_ms, end_ms = _start_end_ms()
    warehouses = [WarehouseConfig(c, "PARTIAL", PARTIAL_CAP) for c in included]
    targets = []
    for d in deals:
        missing = [k for k in ("rule_name", "title", "rule_description", "cart_description")
                   if not d.get(k)]
        if missing:
            raise SystemExit(f"Deal {d['fsn']} missing: {', '.join(missing)}")
        targets.append(TargetRule(
            rule_name=d["rule_name"],
            rule_description=d["rule_description"],
            is_active=IS_ACTIVE,
            start_ms=start_ms,
            end_ms=end_ms,
            replenishment_type=REPLENISHMENT_TYPE,
            fsn=d["fsn"],
            title=d["title"],
            cart_value_threshold=float(CART_VALUE_THRESHOLD),
            cart_description=d["cart_description"],
            warehouses=list(warehouses),
        ))
    return targets


def main():
    cfg = Config()
    cfg.require_auth()
    post_fn = make_post_fn(cfg.session, cfg.xsrf)

    # Always re-check live: the referrer rule's FSN list changes on its own
    # schedule, so a cached list would go stale. Any FSN it currently covers
    # is skipped here rather than attempted.
    fsns = [d["fsn"] for d in DEALS]
    _clean, conflicting = split_referral_conflicts(fsns, post_fn)
    if conflicting:
        print(f"Skipping {len(conflicting)} FSN(s) already in the referrer reward rule "
              f"(not creating freebie deals for these): {', '.join(conflicting)}")
    deals = [d for d in DEALS if d["fsn"] not in conflicting]
    if not deals:
        raise SystemExit("All FSNs in DEALS are referral-only. Nothing to create.")

    targets = build_targets(deals)
    # Channel exclusion here means "don't include the GTM warehouse" — not a
    # cohort exclusion, so no cohort hurdle is added.
    plan = Plan(edits=[], creates=[build_create_payload(t, cfg.email) for t in targets])

    excluded = [c for c in CHANNELS if c == GTM_CHANNEL]
    print(f"Channels: {len([c for c in CHANNELS if c != GTM_CHANNEL])} included, "
          f"excluded (GTM): {excluded or 'none'}")
    print(render_preview(plan))

    answer = input("\nProceed and create these rules? Type 'yes' to execute: ").strip()
    if answer != "yes":
        print("Aborted. No changes made.")
        return

    client = ControlGrid(post_fn)
    result = execute_plan(plan, client)
    print(f"\nDONE. created={len(result['created'])} failed={len(result['failed'])}")
    for kind, ident, err in result["failed"]:
        print(f"  FAILED {kind} {ident}: {err}")


if __name__ == "__main__":
    main()
