import os

# Path to the Google service-account JSON used to read the sheet.
# Override with the FREEBIE_SERVICE_ACCOUNT env var; defaults to a file
# named service_account.json sitting next to this package.
SERVICE_ACCOUNT_PATH = os.environ.get(
    "FREEBIE_SERVICE_ACCOUNT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_account.json"),
)
DEFAULT_EMAIL = "madhavsingal@firstclub.co.in"
SHEET_TAB = "FREEBIE_DEALS"


class Config:
    def __init__(self):
        self.auth_token = os.environ.get("FREEBIE_AUTH_TOKEN", "")
        self.email = os.environ.get("FREEBIE_EMAIL", DEFAULT_EMAIL)

    def require_auth(self):
        if not self.auth_token:
            raise SystemExit("Set the FREEBIE_AUTH_TOKEN environment variable "
                             "(the Cognito ID token ControlGrid's UI sends as "
                             "Authorization: Bearer).")
