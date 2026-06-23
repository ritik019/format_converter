import requests

BASE_URL = "https://portal.controlgrid.in/api/v1/reward/rules"


def build_headers(session_cookie, xsrf_token):
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://portal.controlgrid.in",
        "referer": "https://portal.controlgrid.in/rules-management/",
        "x-xsrf-token": xsrf_token,
        "Cookie": f"SESSION={session_cookie}; XSRF-TOKEN={xsrf_token}",
    }


def handle_response(resp):
    """Return parsed JSON on success; on error raise with controlgrid's own message."""
    if not resp.ok:
        detail = ""
        try:
            body = resp.json()
            detail = (body.get("error") or {}).get("message") or body.get("message") or resp.text
        except Exception:  # noqa: BLE001 - body wasn't JSON
            detail = resp.text
        raise RuntimeError(f"controlgrid {resp.status_code}: {detail}".strip())
    return resp.json()


def make_post_fn(session_cookie, xsrf_token):
    """Returns post_fn(path, json_body) -> parsed dict for ControlGrid."""
    http = requests.Session()
    headers = build_headers(session_cookie, xsrf_token)

    def post_fn(path, json_body):
        resp = http.post(BASE_URL + path, json=json_body, headers=headers, timeout=60)
        return handle_response(resp)

    return post_fn
