"""Guards freebie-deal creation against FSNs owned by the referrer reward rule.

Rule 5daa2776-68a1-44c2-96cf-f4fd82a7a375 ("Referrer Product Reward Rule",
ruleType=REFERRER) carries its own per-warehouse FSN list for referral rewards.
It is unrelated to FREEBIE_DEALS and must never be edited or have its FSNs
reused by a freebie deal. Its FSN list changes on its own schedule, so it is
always re-fetched live right before a check — never cached across runs.
"""
import json

REFERRAL_RULE_ID = "5daa2776-68a1-44c2-96cf-f4fd82a7a375"
REFERRAL_RULE_TYPE = "REFERRER"


def fetch_referral_fsns(post_fn, rule_id=REFERRAL_RULE_ID, rule_type=REFERRAL_RULE_TYPE):
    """Live-fetch the referrer rule and return the set of FSNs it currently covers."""
    rule = None
    page = 0
    while True:
        resp = post_fn("/fetch", {"page": page, "size": 100, "ruleType": rule_type})
        body = resp["body"]
        for r in body["content"]:
            if r["id"] == rule_id:
                rule = r
        if rule or body.get("last", True):
            break
        page += 1
    if rule is None:
        raise RuntimeError(f"referral rule {rule_id} not found — cannot safely check FSN conflicts")

    consequence = json.loads(rule["consequences"])
    fsns = set()
    for mapping in consequence.get("warehouseProductMappings", []):
        fsns.update(mapping.get("fsnIds", []))
    return fsns


def split_referral_conflicts(fsns, post_fn):
    """Split fsns (iterable) into (clean, conflicting) against the live referral FSN set."""
    referral_fsns = fetch_referral_fsns(post_fn)
    fsns = list(fsns)
    conflicting = [f for f in fsns if f in referral_fsns]
    clean = [f for f in fsns if f not in referral_fsns]
    return clean, conflicting
