"""Build Pricing and Offers (pno) rule bodies. Pure functions, no network.

Shapes mirror the portal's own form.js `buildRequest` (captured 5 Nov 2026, see
PNO_API_NOTES.md):
  * deal product  -> DIMENSION_QUANTITY GE, dimension FSN, count 1 (deals only)
  * MOV           -> CART_VALUE GE value
  * cohorts       -> USER_COHORT IN / NOT_IN with `cohortIds`
  * societies     -> SOCIETY_ID IN / NOT_IN with `societyIds`
  * once per user -> USER_REDEMPTION LT limit 1
  * unit caps     -> TOTAL_REDEMPTION_PER_WAREHOUSE LT, unitsReset EVERY_DAY|NEVER,
                     limitsByWarehouse {wh: n}; a FULL warehouse has no entry
  * the server builds the WAREHOUSE hurdle itself from warehouseIds — never send it.
"""
import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

CAP = "TOTAL_REDEMPTION_PER_WAREHOUSE"
FULL = "FULL"
UNITS_RESET = ("EVERY_DAY", "NEVER")
PRICE_MODES = ("FIXED_PRICE", "FLAT_OFF", "PERCENT_OFF")

# subType -> ruleType (the template's family)
TEMPLATES = {
    "FSN_DEALS": "FSN_DEALS",
    "FREEBIE_CART_VALUE": "FREEBIE",
    "FREEBIE_CATEGORY_CART": "FREEBIE",
    "FREEBIE_SAMPLE_SELECTOR": "FREEBIE",
    "FREEBIE_GENERIC": "FREEBIE",
    "PARTNER_FREEBIE": "FREEBIE",
    "REFERRAL": "REFERRAL",
}
# Templates this module can build from scratch today.
BUILDABLE = ("FSN_DEALS", "FREEBIE_CART_VALUE")

DEFAULT_UNLOCKED = {"FSN_DEALS": "Unlocked: special price applied",
                    "FREEBIE_CART_VALUE": "Unlocked: free gift added"}
DEFAULT_LOCKED = "Add items worth Rs.$$$ more to unlock"

META_KEYS = ("pinned", "priority", "autoAddApplicable", "removeEligible",
             "userMessage", "bannerInfo", "cta")

Cap = Union[int, str]  # a unit number, or "FULL"


@dataclass
class PnoTarget:
    template: str                      # FSN_DEALS or FREEBIE_CART_VALUE
    name: str
    start_ms: int
    end_ms: int
    fsn: str                           # PARENT FSN (no child/deal FSN any more)
    warehouses: Dict[str, Cap]         # {warehouseId: int cap | "FULL"}
    title: str
    cart_value: Optional[float] = None # MOV hurdle; required for FREEBIE_CART_VALUE
    description: Optional[str] = None
    skus: List[str] = field(default_factory=list)  # empty = all SKUs
    units_reset: str = "EVERY_DAY"
    surfaces: List[str] = field(default_factory=lambda: ["CART"])
    price_mode: Optional[str] = None   # deals: FIXED_PRICE / FLAT_OFF / PERCENT_OFF
    price_value: Optional[float] = None
    locked: str = DEFAULT_LOCKED
    unlocked: Optional[str] = None
    item_description: Optional[str] = None
    pinned: bool = False               # deals only
    auto_add: bool = True              # freebies only (deals are always off)
    removable: bool = True
    cohort_in: List[str] = field(default_factory=list)
    cohort_out: List[str] = field(default_factory=list)
    society_in: List[str] = field(default_factory=list)
    once_per_user: bool = False

    @property
    def is_deal(self):
        return self.template == "FSN_DEALS"


def validate(t):
    errs = []
    if t.template not in BUILDABLE:
        errs.append(f"template {t.template!r} not supported yet (have {BUILDABLE})")
    if not t.name.strip():
        errs.append("name is empty")
    if not t.fsn.strip().startswith("FC"):
        errs.append(f"fsn {t.fsn!r} does not look like an FSN")
    if t.end_ms <= t.start_ms:
        errs.append("end must be after start")
    if not t.warehouses:
        errs.append("no warehouses")
    for wh, cap in t.warehouses.items():
        if cap != FULL and (not isinstance(cap, int) or cap < 1):
            errs.append(f"{wh}: cap must be FULL or a positive int, got {cap!r}")
    if t.units_reset not in UNITS_RESET:
        errs.append(f"units_reset must be one of {UNITS_RESET}")
    if t.is_deal:
        if t.price_mode not in PRICE_MODES:
            errs.append(f"deal needs price_mode in {PRICE_MODES}")
        if t.price_value is None or t.price_value <= 0:
            errs.append("deal needs a positive price_value")
    else:
        if t.cart_value is None:
            errs.append("FREEBIE_CART_VALUE needs cart_value")
        if t.pinned:
            errs.append("pinned is deals-only")
    if t.cart_value is not None and t.cart_value <= 0:
        errs.append("cart_value must be positive")
    if "$$$" not in (t.locked or "") and t.cart_value is not None:
        errs.append("locked message should contain $$$ (the app fills the amount left)")
    return errs


def _num(v):
    if v is None:
        return None
    f = float(v)
    return int(f) if f.is_integer() else f


def _set_hurdles(htype, field_name, ins, outs):
    out = []
    if ins:
        out.append({"type": htype, "operation": "IN", field_name: list(ins)})
    if outs:
        out.append({"type": htype, "operation": "NOT_IN", field_name: list(outs)})
    return out


def cap_hurdle(warehouses, units_reset, keep=None):
    """None when every warehouse is FULL (no cap at all)."""
    limits = {wh: cap for wh, cap in warehouses.items() if cap != FULL}
    if not limits:
        return None
    h = {k: keep[k] for k in ("key", "name", "description", "failureMessage", "successMessage")
         if keep and k in keep}
    h.update({"type": CAP, "operation": "LT", "unitsReset": units_reset, "limitsByWarehouse": limits})
    return h


def assign_keys(hurdles):
    """Give every hurdle without a key the next free TYPE-n (portal behaviour)."""
    used = {h["key"] for h in hurdles if h.get("key")}
    for h in hurdles:
        if h.get("key"):
            continue
        n = 1
        while f"{h['type']}-{n}" in used:
            n += 1
        h["key"] = f"{h['type']}-{n}"
        used.add(h["key"])
    return hurdles


def build_create_body(t):
    errs = validate(t)
    if errs:
        raise ValueError(f"{t.name}: " + "; ".join(errs))
    hurdles = []
    if t.is_deal:
        hurdles.append({"type": "DIMENSION_QUANTITY", "operation": "GE", "dimension": "FSN",
                        "dimensionId": t.fsn.strip(), "skus": list(t.skus), "count": 1})
    if t.cart_value is not None:
        hurdles.append({"type": "CART_VALUE", "operation": "GE", "value": _num(t.cart_value)})
    hurdles += _set_hurdles("USER_COHORT", "cohortIds", t.cohort_in, t.cohort_out)
    hurdles += _set_hurdles("SOCIETY_ID", "societyIds", t.society_in, [])
    if t.once_per_user:
        hurdles.append({"type": "USER_REDEMPTION", "operation": "LT", "limit": 1,
                        "durationValue": None, "durationUnit": None})
    cap = cap_hurdle(t.warehouses, t.units_reset)
    if cap:
        hurdles.append(cap)
    assign_keys(hurdles)

    if t.is_deal:
        consequence = {"type": "PRICING_DEAL", "mode": t.price_mode,
                       "value": _num(t.price_value), "maxQuantity": 1}
    else:
        consequence = {"type": "FREEBIE_ITEM",
                       "items": [{"fsn": t.fsn.strip(), "skus": list(t.skus), "price": 0, "maxQuantity": None}]}

    meta = {
        "pinned": bool(t.pinned) if t.is_deal else False,
        "priority": None,
        "autoAddApplicable": False if t.is_deal else bool(t.auto_add),
        "removeEligible": True if t.is_deal else bool(t.removable),
        "userMessage": {"offerTitle": t.title, "offerDescription": t.item_description,
                        "unlockedMessage": t.unlocked or DEFAULT_UNLOCKED[t.template],
                        "lockedMessage": t.locked},
        "bannerInfo": None,   # banners stay on the OLD portal (SOP section 16)
        "cta": None,
    }
    return {
        "name": t.name.strip(), "description": t.description,
        "ruleType": TEMPLATES[t.template], "subType": t.template,
        "startTime": int(t.start_ms), "endTime": int(t.end_ms),
        "warehouseIds": list(t.warehouses), "discoverySurfaces": list(t.surfaces),
        "hurdles": hurdles, "consequence": consequence, "metaInfo": meta,
        "offerGroupId": None,
    }


def view_limits(view):
    """{wh: int | FULL} from a rule view (cap hurdle first, then warehouses[].limit)."""
    cap = next((h for h in view.get("hurdles") or [] if h.get("type") == CAP), None)
    limits = dict((cap or {}).get("limitsByWarehouse") or {})
    out = {}
    for w in view.get("warehouses") or []:
        lim = w.get("limit")
        out[w["warehouseId"]] = limits.get(w["warehouseId"], lim if isinstance(lim, int) else FULL)
    return out


def build_edit_body(view, *, start_ms=None, end_ms=None, cart_value=None, warehouses=None,
                    units_reset=None, title=None, locked=None, unlocked=None, name=None,
                    price_value=None):
    """PUT body for an existing rule view with only the given fields changed.

    Everything else (other hurdles and their keys, consequence, messages, surfaces) is
    sent back as stored. `warehouses` replaces the warehouse list + caps ({wh: int|FULL}).
    """
    if view.get("version") is None:
        raise ValueError(f"{view.get('id')}: view has no version")
    if view.get("status") not in ("SCHEDULED", "LIVE", "PAUSED"):
        raise ValueError(f"{view.get('id')}: status {view.get('status')} is not editable")

    hurdles = [copy.deepcopy(h) for h in view.get("hurdles") or []
               if h.get("type") != "WAREHOUSE"]
    old_cap = next((h for h in hurdles if h.get("type") == CAP), None)
    hurdles = [h for h in hurdles if h.get("type") != CAP]

    if cart_value is not None:
        cv = next((h for h in hurdles if h.get("type") == "CART_VALUE"), None)
        if cv is None:
            hurdles.append({"type": "CART_VALUE", "operation": "GE", "value": _num(cart_value)})
        else:
            cv["value"] = _num(cart_value)

    whs = dict(warehouses) if warehouses is not None else view_limits(view)
    reset = units_reset or (old_cap or {}).get("unitsReset") or "NEVER"
    cap = cap_hurdle(whs, reset, keep=old_cap)
    if cap:
        hurdles.append(cap)
    assign_keys(hurdles)

    consequence = copy.deepcopy(view.get("consequence") or {})
    if price_value is not None:
        if consequence.get("type") != "PRICING_DEAL":
            raise ValueError(f"{view.get('id')}: price_value only applies to deals")
        consequence["value"] = _num(price_value)

    meta_in = view.get("metaInfo") or {}
    meta = {k: copy.deepcopy(meta_in.get(k)) for k in META_KEYS}
    msg = dict(meta.get("userMessage") or {})
    if title is not None:
        msg["offerTitle"] = title
    if locked is not None:
        msg["lockedMessage"] = locked
    if unlocked is not None:
        msg["unlockedMessage"] = unlocked
    meta["userMessage"] = msg

    return {
        "version": view["version"],
        "name": name if name is not None else view.get("name"),
        "description": view.get("description"),
        "ruleType": view.get("ruleType"), "subType": view.get("subType"),
        "startTime": int(start_ms if start_ms is not None else view["startTime"]),
        "endTime": int(end_ms if end_ms is not None else view["endTime"]),
        "warehouseIds": list(whs), "discoverySurfaces": list(view.get("discoverySurfaces") or []),
        "hurdles": hurdles, "consequence": consequence, "metaInfo": meta,
        "offerGroupId": view.get("offerGroupId"),
    }


def from_target_rule(tr, *, price_mode=None, price_value=None, template="FSN_DEALS",
                     units_reset=None):
    """Adapt an old-sheet TargetRule (freebie.models) to a PnoTarget.

    - cart_value_threshold -> cart_value (the sheet already has MOV minus the offset)
    - PARTIAL cap n -> n; FULL -> FULL
    - replenishment DAILY -> EVERY_DAY, anything else -> NEVER (unless units_reset given)
    - cart_description -> locked message
    The sheet's child_fcn must now be the PARENT FSN; deals also need a price, which the
    old sheets never carried (it sat on the child listing).
    """
    if not tr.is_active:
        raise ValueError(f"{tr.rule_name}: inactive rows have no pno equivalent at create time")
    whs = {w.warehouse_id: (w.inventory_cap if w.consumption_type == "PARTIAL" else FULL)
           for w in tr.warehouses}
    reset = units_reset or ("EVERY_DAY" if (tr.replenishment_type or "").upper() == "DAILY" else "NEVER")
    return PnoTarget(template=template, name=tr.rule_name, description=tr.rule_description,
                     start_ms=tr.start_ms, end_ms=tr.end_ms, fsn=tr.fsn, warehouses=whs,
                     title=tr.title, cart_value=tr.cart_value_threshold, units_reset=reset,
                     price_mode=price_mode, price_value=price_value,
                     locked=tr.cart_description or DEFAULT_LOCKED)
