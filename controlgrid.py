import json

# Conflicts can be raised against either freebie rule family, so the type-index
# lookup sweeps both. fetch_all (the planner's source of truth) stays FREEBIE_DEALS.
FREEBIE_RULE_TYPES = ["FREEBIE_DEALS", "FREEBIE_CART_VALUE"]


class ControlGrid:
    def __init__(self, post_fn):
        # post_fn(path, json_body) -> parsed dict
        self._post = post_fn

    def _fetch_type(self, rule_type):
        rules = []
        page = 0
        while True:
            resp = self._post("/fetch", {"page": page, "size": 100, "ruleType": rule_type})
            body = resp["body"]
            rules.extend(body["content"])
            if body.get("last", True):
                break
            page += 1
        return rules

    def fetch_all(self):
        rules = []
        for r in self._fetch_type("FREEBIE_DEALS"):
            r["consequence"] = json.loads(r["consequences"])
            r["hurdleList"] = json.loads(r["hurdles"]) if r.get("hurdles") else []
            rules.append(r)
        return rules

    def fetch_index(self, rule_types=FREEBIE_RULE_TYPES):
        """Best-effort {rule_id: {...}} across rule types, used to label and
        summarise conflicting rules surfaced in error messages. Each entry carries
        name / ruleType / ruleStatus / start+end times / parsed hurdleList (for
        cohort detection) and the raw rule (for created/updated audit fields). A
        rule type the API rejects is skipped rather than failing the whole lookup."""
        index = {}
        for rt in rule_types:
            try:
                for r in self._fetch_type(rt):
                    try:
                        hurdles = json.loads(r["hurdles"]) if r.get("hurdles") else []
                    except Exception:  # noqa: BLE001 - malformed hurdle blob
                        hurdles = []
                    index[r["id"]] = {
                        "name": r.get("ruleName"),
                        "ruleType": rt,
                        "ruleStatus": r.get("ruleStatus"),
                        "startTime": r.get("startTime"),
                        "endTime": r.get("endTime"),
                        "hurdleList": hurdles,
                        "raw": r,
                    }
            except Exception:  # noqa: BLE001 - unsupported type / transient; skip
                continue
        return index

    def edit_rule(self, payload):
        return self._post("/edit", payload)

    def create_rule(self, payload):
        return self._post("/create", payload)

    def delete_rule(self, rule_id, user):
        return self._post("/delete", {"ruleId": rule_id, "user": user})
