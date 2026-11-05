"""Read-only client for the new Pricing and Offers (pno) API.

Since 5 Nov 2026 deals and freebies live on portal.controlgrid.in/pricing-and-offers
(rule ids start with ``pno_``). The old ``/api/v1/reward/rules`` API (see auth.py /
controlgrid.py) now only matters for banners and wallet offers.

Writes (create / PUT / pause / resume / end) exist but are LOCKED unless the client is
built with ``allow_writes=True``; ops scripts only do that after a dry-run preview and a
typed "yes". See PNO_API_NOTES.md for the full endpoint list and the rule body shape.

Auth: the same Cognito ID token the old package uses (FREEBIE_AUTH_TOKEN), sent as
``Authorization: Bearer``.
"""
import requests

PORTAL = "https://portal.controlgrid.in"
RULES = PORTAL + "/api/v1/pno/rules"

# Every status the API knows; ENDED is excluded from the default "open" set.
OPEN_STATUSES = ("LIVE", "PAUSED", "SCHEDULED")
ALL_STATUSES = OPEN_STATUSES + ("ENDED",)

# Hurdle that carries per-warehouse unit caps. A warehouse missing from its
# limitsByWarehouse (or a rule with no such hurdle) is FULL (uncapped).
CAP_HURDLE = "TOTAL_REDEMPTION_PER_WAREHOUSE"


def build_headers(auth_token):
    return {
        "accept": "application/json",
        "origin": PORTAL,
        "referer": PORTAL + "/pricing-and-offers/",
        "authorization": f"Bearer {auth_token}",
    }


def _csv(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple, set)):
        return ",".join(str(x) for x in v)
    return str(v)


class PnoError(RuntimeError):
    def __init__(self, status, message, details=None):
        super().__init__(f"pno {status}: {message}".strip())
        self.status = status
        self.details = details


STATUS_VERBS = ("pause", "resume", "end")
CSRF_URL = PORTAL + "/api/v1/security/csrf"


class WritesLocked(RuntimeError):
    pass


class Pno:
    """Wrapper around /api/v1/pno/*. Reads always work; writes need allow_writes=True."""

    def __init__(self, auth_token, session=None, timeout=60, allow_writes=False):
        self._http = session or requests.Session()
        self._headers = build_headers(auth_token)
        self._timeout = timeout
        self._allow_writes = allow_writes
        self._xsrf = None

    # -- transport -------------------------------------------------------
    def _get(self, url, params=None):
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        resp = self._http.get(url, params=params, headers=self._headers, timeout=self._timeout)
        return self._unwrap(resp)

    def _fetch_xsrf(self):
        """The portal sends X-XSRF-TOKEN on writes. Untested whether Bearer calls need it;
        we only fetch it after a 403. Token comes from the body or the XSRF-TOKEN cookie."""
        resp = self._http.get(CSRF_URL, headers=self._headers, timeout=self._timeout)
        tok = None
        try:
            body = resp.json()
            body = body.get("body", body) if isinstance(body, dict) else {}
            tok = body.get("token") or body.get("csrfToken") if isinstance(body, dict) else None
        except ValueError:
            pass
        cookies = getattr(self._http, "cookies", None)
        if not tok and cookies is not None:
            tok = cookies.get("XSRF-TOKEN")
        self._xsrf = tok
        return tok

    def _send(self, method, url, body):
        if not self._allow_writes:
            raise WritesLocked(f"{method} {url} blocked: client built without allow_writes=True")
        for attempt in (1, 2):
            headers = dict(self._headers, **{"content-type": "application/json"})
            if self._xsrf:
                headers["X-XSRF-TOKEN"] = self._xsrf
            resp = self._http.request(method, url, json=body, headers=headers, timeout=self._timeout)
            if resp.status_code == 403 and attempt == 1 and not self._xsrf and self._fetch_xsrf():
                continue
            return self._unwrap(resp)

    def _unwrap(self, resp):
        try:
            payload = resp.json() if resp.text else None
        except ValueError:
            payload = None
        if not resp.ok or (isinstance(payload, dict) and payload.get("success") is False):
            body = payload if isinstance(payload, dict) else {}
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            msg = err.get("message") or body.get("message") or resp.text[:300]
            raise PnoError(resp.status_code, msg, err.get("details") or body.get("details"))
        # FlowResponse {success, body}
        if isinstance(payload, dict) and "body" in payload:
            return payload["body"]
        return payload

    # -- reads -----------------------------------------------------------
    def search_page(self, status=OPEN_STATUSES, rule_type=None, warehouse_id=None,
                    fsn=None, sort=None, origin=None, cursor=None, size=100):
        """One page: {content, size, first, last, nextCursor}."""
        return self._get(RULES, {
            "status": _csv(status), "ruleType": _csv(rule_type),
            "warehouseId": _csv(warehouse_id), "fsn": _csv(fsn), "sort": sort,
            "origin": origin, "cursor": cursor, "size": size,
        })

    def search(self, max_pages=50, **filters):
        """All pages of search_page(**filters), following nextCursor."""
        out, cursor = [], None
        for _ in range(max_pages):
            page = self.search_page(cursor=cursor, **filters) or {}
            out.extend(page.get("content") or [])
            if page.get("last") is not False or not page.get("nextCursor"):
                return out
            cursor = page["nextCursor"]
        raise PnoError(0, f"search stopped after {max_pages} pages; narrow the filters")

    def get_rule(self, rule_id):
        return self._get(f"{RULES}/{rule_id}")

    def activity(self, rule_id, cursor=None, size=20):
        return self._get(f"{RULES}/{rule_id}/activity", {"cursor": cursor, "size": size})

    def templates(self):
        return self._get(PORTAL + "/api/v1/pno/templates")

    def warehouse_sets(self):
        return self._get(PORTAL + "/api/v1/pno/warehouse-sets")

    def warehouses(self):
        return self._get(PORTAL + "/api/v1/pno/warehouses")

    # -- writes (locked unless allow_writes=True) --------------------------
    def create_rule(self, body):
        return self._send("POST", RULES, body)

    def update_rule(self, rule_id, body):
        """PUT; body must carry the rule's current `version` (stale → 409)."""
        if body.get("version") is None:
            raise ValueError("update body needs the rule's current version")
        return self._send("PUT", f"{RULES}/{rule_id}", body)

    def change_status(self, rule_id, verb, version, reason=None):
        if verb not in STATUS_VERBS:
            raise ValueError(f"verb must be one of {STATUS_VERBS}")
        return self._send("POST", f"{RULES}/{rule_id}/{verb}", {"version": version, "reason": reason})


# -- helpers that read a rule view ----------------------------------------
def hurdle(rule, htype):
    for h in rule.get("hurdles") or []:
        if h.get("type") == htype:
            return h
    return None


def rule_fsns(rule):
    """Parent FSNs a rule is about: the deal product, or the gift item(s)."""
    fsns = []
    cons = rule.get("consequence") or {}
    if cons.get("type") == "PRICING_DEAL":
        for h in rule.get("hurdles") or []:
            if h.get("type") == "DIMENSION_QUANTITY" and h.get("dimension") == "FSN" and h.get("dimensionId"):
                fsns.append(h["dimensionId"])
    for it in cons.get("items") or []:
        if it.get("fsn"):
            fsns.append(it["fsn"])
    return list(dict.fromkeys(fsns))


def cart_value(rule):
    h = hurdle(rule, "CART_VALUE")
    return h.get("value") if h else None


def warehouse_limits(rule):
    """{warehouseId: int cap or 'FULL'} for every warehouse on the rule."""
    cap = hurdle(rule, CAP_HURDLE) or {}
    limits = cap.get("limitsByWarehouse") or {}
    out = {}
    for w in rule.get("warehouses") or []:
        wid = w.get("warehouseId")
        lim = w.get("limit")
        if lim in (None, "FULL"):
            lim = limits.get(wid, "FULL")
        out[wid] = lim
    for wid, lim in limits.items():
        out.setdefault(wid, lim)
    return out


def units_reset(rule):
    h = hurdle(rule, CAP_HURDLE)
    return (h or {}).get("unitsReset") or ("" if h is None else "NEVER")


def deal_price(rule):
    cons = rule.get("consequence") or {}
    if cons.get("type") != "PRICING_DEAL":
        return None
    return f"{cons.get('mode')} {cons.get('value')}"
