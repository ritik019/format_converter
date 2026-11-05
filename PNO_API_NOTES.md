# Pricing and Offers (pno) API — notes for the freebie migration

Captured 2026-11-05, read-only, from the new portal's own JS
(`/pricing-and-offers/api.js`, `form.js`, `shared.js`) and its network calls.
Nothing was written to ControlGrid while collecting this.

## Endpoints (all under https://portal.controlgrid.in)
| Call | Method + path |
|---|---|
| Search rules | `GET /api/v1/pno/rules?status=&ruleType=&warehouseId=&fsn=&sort=&origin=&cursor=&size=` → `{content:[...], cursor...}` |
| Counts | `GET /api/v1/pno/rules/counts?ruleType=&warehouseId=` |
| One rule | `GET /api/v1/pno/rules/{pno_id}` |
| Activity (audit) | `GET /api/v1/pno/rules/{id}/activity?cursor=&size=` |
| Create | `POST /api/v1/pno/rules` body = rule (below) |
| Update | `PUT /api/v1/pno/rules/{id}` body = rule incl. current `version` |
| Pause / resume / end | `POST /api/v1/pno/rules/{id}/{pause|resume|end}` body `{version, reason}` |
| Templates | `GET /api/v1/pno/templates` → `{families, hurdleKinds, pricingModes, surfaces, templates}` |
| Warehouse sets | `GET /api/v1/pno/warehouse-sets` |
| Warehouses | `GET /api/v1/pno/warehouses` (portal list view also uses `/api/v1/wms/warehouses`) |

- Responses are wrapped `{success, body}`; `success:false` is an error even on 2xx.
- Status values: `SCHEDULED, LIVE, PAUSED, ENDED`. Editable: SCHEDULED/LIVE/PAUSED. No delete — use `end`.
- Optimistic locking: the proxy drops If-Match, so every mutation sends the rule's `version`;
  a stale version → **409**. A 409 *with details* = cap breach ("a product can be in only so
  many offers of this kind at one warehouse, paused ones included").
- Auth: portal sends `AuthUtils.getAuthHeaders()` + `X-XSRF-TOKEN` (from `GET /api/v1/security/csrf`).
  The old freebie package used Bearer only — **untested whether pno writes need the XSRF header.**
- 401 → portal redirects to Cognito login (a bad call can log the browser session out).

## Templates (subType → ruleType)
| subType | ruleType | Portal label | Old equivalent |
|---|---|---|---|
| FSN_DEALS | FSN_DEALS | Cart value deal | FREEBIE_DEALS |
| FREEBIE_CART_VALUE | FREEBIE | Freebie on cart value | FREEBIE_CART_VALUE |
| FREEBIE_CATEGORY_CART | FREEBIE | Category freebie | FREEBIE_CATEGORY_CART |
| FREEBIE_SAMPLE_SELECTOR | FREEBIE | Sample selector | FREEBIE_CATEGORY_CART_MULTI-ish |
| FREEBIE_GENERIC | FREEBIE | Freebie generic | — |
| PARTNER_FREEBIE | FREEBIE | Partner freebie | — |
| REFERRAL | REFERRAL | Referral | REFERRER rule |

Pricing modes: `FIXED_PRICE, FLAT_OFF, PERCENT_OFF`. Surfaces: `HOME, CART, PDP, PLP, REFERRAL`.

## Rule body (what form.js buildRequest sends)
```json
{
  "version": 3,                       // edits only
  "name": "...", "description": "...",
  "ruleType": "FSN_DEALS", "subType": "FSN_DEALS",
  "startTime": 1793875475951, "endTime": 1793903400000,
  "warehouseIds": ["FCHBLRHAR01", "..."],
  "discoverySurfaces": ["CART"],
  "hurdles": [
    {"key": "DIMENSION_QUANTITY-1", "type": "DIMENSION_QUANTITY", "operation": "GE",
     "dimension": "FSN", "dimensionId": "FC2508...", "skus": [], "count": 1},   // deals: the product
    {"key": "CART_VALUE-1", "type": "CART_VALUE", "operation": "GE", "value": 187},
    {"key": "USER_COHORT-1", "type": "USER_COHORT", "operation": "IN", "...": "cohortIds"},
    {"key": "TOTAL_REDEMPTION_PER_WAREHOUSE-1", "type": "TOTAL_REDEMPTION_PER_WAREHOUSE",
     "operation": "LT", "unitsReset": "DAILY|null", "limitsByWarehouse": {"FCHBLRHAR01": 501}}
  ],
  "consequence": {"type": "PRICING_DEAL", "mode": "FIXED_PRICE", "value": 99, "maxQuantity": 1},
  // freebies instead: {"type": "FREEBIE_ITEM", "items": [{"fsn": "...", "skus": [], "price": 0, "maxQuantity": null}]}
  "metaInfo": {
    "pinned": false, "priority": null,          // priority only when HOME is a surface
    "autoAddApplicable": true, "removeEligible": true,
    "userMessage": {"offerTitle": "...", "offerDescription": null,
                    "unlockedMessage": "...", "lockedMessage": "Add items worth Rs.$$$ more to unlock"},
    "bannerInfo": null, "cta": null              // banners stay on the OLD portal per SOP
  },
  "offerGroupId": null,
  "origin": "GAMIFICATION"                      // create only, prizes only
}
```
- Hurdle `key` = `TYPE-n`, kept stable across edits.
- Full (no cap) = no TOTAL_REDEMPTION_PER_WAREHOUSE entry for that warehouse.
- Hurdle kinds: CART_VALUE, CATEGORY_COUNT/BRAND_COUNT/PRODUCT_COUNT (DIMENSION_QUANTITY),
  CATEGORY_SPEND/BRAND_SPEND (DIMENSION_VALUE), USER_COHORT, USER_TYPE, SOCIETY_ID,
  USER_REDEMPTION(_PERIOD), TOTAL_REDEMPTION, TIME_RANGE, VERTICAL, PURCHASE_TYPE,
  REFEREE_ORDER_COUNT. **No ORDER_COUNT** (use New User / OC 2+ cohorts).
- Offers sit on the **parent FSN**; no child/deal FSN, no SKU mapping.
