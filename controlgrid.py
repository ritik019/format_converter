import json


class ControlGrid:
    def __init__(self, post_fn):
        # post_fn(path, json_body) -> parsed dict
        self._post = post_fn

    def fetch_all(self):
        rules = []
        page = 0
        while True:
            resp = self._post("/fetch", {"page": page, "size": 100, "ruleType": "FREEBIE_DEALS"})
            body = resp["body"]
            for r in body["content"]:
                r["consequence"] = json.loads(r["consequences"])
                r["hurdleList"] = json.loads(r["hurdles"]) if r.get("hurdles") else []
                rules.append(r)
            if body.get("last", True):
                break
            page += 1
        return rules

    def edit_rule(self, payload):
        return self._post("/edit", payload)

    def create_rule(self, payload):
        return self._post("/create", payload)

    def delete_rule(self, rule_id, user):
        return self._post("/delete", {"ruleId": rule_id, "user": user})
