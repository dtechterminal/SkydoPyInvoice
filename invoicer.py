import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone
import argparse
import requests
from urllib.parse import quote
import time
import threading
import contextlib
from itertools import cycle
import shutil

# ----------------------------------
# Logging setup
# ----------------------------------
LOGGER_NAME = "skydo_invoicer"
logger = logging.getLogger(LOGGER_NAME)


def setup_logging(level: str = "INFO") -> None:
    """Configure a verbose console logger.

    Args:
        level: Logging level name (e.g., DEBUG, INFO, WARNING).
    """
    # Normalize and map to logging level
    level = level.upper().strip()
    if level not in {
        "CRITICAL",
        "ERROR",
        "WARNING",
        "INFO",
        "DEBUG",
        "NOTSET",
    }:
        level = "INFO"

    logger.setLevel(level)

    # Clear existing handlers (for re-runs)
    for h in list(logger.handlers):
        logger.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    fmt = (
        "%(asctime)s | %(levelname)-8s | %(name)s | %(module)s:%(lineno)d | "
        "%(message)s"
    )
    handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)


# ----------------------------------
# CLI UI spinner/progress helper
# ----------------------------------
class CLIUI:
    """Very light-weight keyboard-only CLI UI with step spinners.

    Usage:
        ui = CLIUI(enabled=True)
        with ui.task("Creating invoice"):
            do_something()
    """
    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled) and sys.stdout.isatty()
        self.total_steps = 0
        self.completed_steps = 0
        self.detail_text = ""
        self._orig_log_handlers = None

    def set_total_steps(self, n: int) -> None:
        self.total_steps = max(0, int(n))

    def inc_total_steps(self, n: int = 1) -> None:
        self.total_steps = max(0, int(self.total_steps) + int(n))

    def set_detail(self, text: str | None) -> None:
        self.detail_text = (text or "")

    def hijack_logger(self, log: logging.Logger) -> None:
        """Route logger output away from stdout to keep a single-screen UI."""
        if not self.enabled:
            return
        self._orig_log_handlers = list(log.handlers)
        class _UILogHandler(logging.Handler):
            def __init__(self, ui: "CLIUI"):
                super().__init__()
                self.ui = ui
                # Mirror parent formatter if present
                if ui and ui._orig_log_handlers:
                    fmt = ui._orig_log_handlers[0].formatter
                    if fmt:
                        self.setFormatter(fmt)
            def emit(self, record: logging.LogRecord) -> None:
                # Swallow logs to avoid newlines; optionally surface a short detail
                try:
                    msg = self.format(record)
                    if self.ui and self.ui.enabled and record.levelno >= logging.INFO:
                        # Show only the tail part after the last ' | ' to reduce noise
                        tail = msg.split(" | ", maxsplit=5)[-1]
                        # Trim to a sane width
                        self.ui.set_detail(tail[:120])
                except Exception:
                    pass
        # Replace all existing handlers with our UI handler
        ui_handler = _UILogHandler(self)
        ui_handler.setLevel(log.level)
        log.handlers = [ui_handler]

    @contextlib.contextmanager
    def task(self, label: str):
        display_label = label
        if not self.enabled:
            # Fall back to log line if UI disabled or not a TTY
            logger.info(display_label + "…")
            yield
            return

        stop_event = threading.Event()

        def spinner():
            p = 0.0
            cleared = False
            while not stop_event.is_set():
                try:
                    # Clear and move cursor home once when spinner starts; hide cursor
                    if not cleared:
                        sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")
                        cleared = True
                    total = max(self.total_steps, 1)
                    p = 0.85 if p >= 0.85 else (p + 0.02)
                    frac = (self.completed_steps + (p if self.total_steps else 0)) / total
                    cols = shutil.get_terminal_size(fallback=(80, 20)).columns
                    bar_width = max(10, min(40, cols - len(display_label) - 30))
                    filled = int(bar_width * max(0.0, min(1.0, frac)))
                    bar = "#" * filled + "-" * (bar_width - filled)
                    percent = int(frac * 100)
                    detail = f" | {self.detail_text}" if self.detail_text else ""
                    sys.stdout.write(f"\x1b[H[{bar}] {percent:3d}% ({self.completed_steps}/{self.total_steps}) {display_label}…{detail}")
                    sys.stdout.flush()
                    time.sleep(0.08)
                except Exception:
                    break

        t = threading.Thread(target=spinner, daemon=True)
        t.start()
        ok = True
        try:
            yield
        except Exception:
            ok = False
            raise
        finally:
            stop_event.set()
            t.join(timeout=0.2)
            # Mark this step complete and render a final single-screen state
            self.completed_steps += 1
            try:
                total = max(self.total_steps, 1)
                frac = self.completed_steps / total
                cols = shutil.get_terminal_size(fallback=(80, 20)).columns
                bar_width = max(10, min(40, cols - len(display_label) - 30))
                filled = int(bar_width * max(0.0, min(1.0, frac)))
                bar = "#" * filled + "-" * (bar_width - filled)
                percent = int(frac * 100)
                detail = f" | {self.detail_text}" if self.detail_text else ""
                # Clear and draw the final state without adding new lines
                sys.stdout.write(f"\x1b[2J\x1b[H[{bar}] {percent:3d}% ({self.completed_steps}/{self.total_steps}) {display_label}{detail}")
                sys.stdout.flush()
            finally:
                # Always show the cursor again at the end of the step
                sys.stdout.write("\x1b[?25h")
                sys.stdout.flush()


# ----------------------------------
# Token cache helpers
# ----------------------------------

DEFAULT_SESSION_CACHE = os.environ.get(
    "SKYDO_SESSION_CACHE",
    os.path.expanduser("~/.cache/skydo-invoicer/session.json"),
)


def _parse_iso8601_z(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def is_token_expired(expiry_iso: str | None) -> bool:
    dt = _parse_iso8601_z(expiry_iso) if expiry_iso else None
    if not dt:
        # No expiry info; let server validator decide
        return False
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # safety window of 60s to avoid edge expiries
    return dt <= (now + timedelta(seconds=60))


def load_cached_token(path: str = DEFAULT_SESSION_CACHE) -> dict | None:
    target = os.path.expanduser(path)
    try:
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Failed to read token cache at %s: %s", target, e)
        return None


def save_cached_token(*, token: str, expiry_iso: str | None, merchant_secret: str | None, email: str | None, path: str = DEFAULT_SESSION_CACHE) -> None:
    target = os.path.expanduser(path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        data = {
            "token": token,
            "expiryDate": expiry_iso,
            "merchant_secret": merchant_secret,
            "email": email,
            "savedAt": datetime.now(timezone.utc).isoformat(),
        }
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, target)
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass
        logger.info("Saved session token cache to %s", target)
    except Exception as e:
        logger.warning("Failed to write token cache at %s: %s", target, e)


# ----------------------------------
# Helpers for invoice item generation
# ----------------------------------

def get_invoice_items(year, month, rate_per_hour, name):
    """Generate weekly invoice items for the given month.

    Only counts Monday–Friday, 8 hours/day. Splits the month into week-long
    blocks starting from the first weekday.
    """

    def get_first_weekday(year, month):
        first_day = datetime(year, month, 1)
        # Adjust if the first day is Saturday (5) or Sunday (6)
        if first_day.weekday() == 5:
            first_day += timedelta(days=2)
        elif first_day.weekday() == 6:
            first_day += timedelta(days=1)
        return first_day

    def create_week_description(start_date, end_date):
        return f"{start_date.strftime('%m-%d-%Y')} - {end_date.strftime('%m-%d-%Y')}"

    def generate_weekly_schedule(year, month, rate_per_hour):
        schedule_list = []
        first_weekday = get_first_weekday(year, month)
        current_date = first_weekday

        while current_date.month == month:
            days_in_week = 0
            week_end_date = current_date

            # Count only weekdays (Monday to Friday)
            while week_end_date.weekday() < 5 and week_end_date.month == month:
                days_in_week += 1
                week_end_date += timedelta(days=1)

            week_end_date -= timedelta(days=1)  # Adjust end_date to the last weekday

            description = create_week_description(current_date, week_end_date)
            quantity = 8 * days_in_week
            total = quantity * rate_per_hour

            schedule_dict = {
                "quantity": quantity,
                "igstApplicable": True,
                "description": description,
                "sac": None,
                "rate": rate_per_hour,
                "igst": None,
                "cgst": None,
                "sgst": None,
                "name": name,
                "unit": "HOUR",
                "total": total,
            }
            schedule_list.append(schedule_dict)
            current_date = week_end_date + timedelta(days=3)  # Move to the next Monday

        return schedule_list

    weekly_schedule = generate_weekly_schedule(year, month, rate_per_hour)
    return [schedule for schedule in weekly_schedule]


# ----------------------------------
# Auth (email OTP) client
# ----------------------------------

class SkydoAuth:
    """Handles Skydo email OTP authentication and session setup.

    Flow:
      1) request_otp(email) -> correlationId
      2) verify_otp_login(otp, correlationId) -> token, secretKey
      3) complete_login(token) -> finalize session on server
      4) validate_session() -> optional sanity check

    After login, `self.session` includes the `token` cookie and can be
    passed into SkydoAPI so it doesn't need a cookie string.
    """

    BASE_URL = "https://dashboard.skydo.com/api"

    def __init__(self, *, secret_key: str | None = None, timeout: int = 30, ui: "CLIUI | None" = None):
        self.timeout = timeout
        self.session = requests.Session()
        # Default to AUTH for auth endpoints
        self.session.headers.update({"x-server": "AUTH"})
        if secret_key:
            # If provided, include merchant/app secret for auth calls
            self.session.headers.update({"x-secret-key": secret_key})
        self.ui = ui

    # Internal helpers
    def _post(self, path: str, json_payload: dict | None = None, *, server_header: str | None = None) -> dict:
        url = f"{self.BASE_URL}/{path.lstrip('/')}"
        if server_header:
            # Temporarily set x-server for this call
            prev = self.session.headers.get("x-server")
            self.session.headers.update({"x-server": server_header})
        else:
            prev = None
        try:
            if getattr(self, "ui", None):
                self.ui.set_detail(f"POST {url}")
            resp = self.session.post(url, json=json_payload, timeout=self.timeout)
            resp.raise_for_status()
            if getattr(self, "ui", None):
                self.ui.set_detail(f"{resp.status_code} {url}")
            return resp.json()
        finally:
            if prev is not None:
                self.session.headers.update({"x-server": prev})

    def request_otp(self, email: str, *, application_name: str = "SKYDO_WEBSITE", resend: bool = False) -> str:
        logger.info("Requesting OTP for %s", email)
        # Matches: route?path=auth/email/request_otp&manageToken=true
        path = "route?path=auth%2Femail%2Frequest_otp&manageToken=true"
        payload = {
            "email": email,
            "applicationName": application_name,
            "resendFlag": bool(resend),
        }
        data = self._post(path, json_payload=payload, server_header="AUTH")
        correlation_id = (data or {}).get("data")
        if not correlation_id:
            logger.error("OTP request did not return correlationId")
            raise SystemExit(2)
        logger.info("OTP requested | correlationId=%s", correlation_id)
        return correlation_id

    def verify_otp_login(self, otp: str, correlation_id: str) -> dict:
        logger.info("Verifying OTP…")
        # Matches: route?path=auth/email/login&manageToken=true&isUserDetailsRequired=true
        path = "route?path=auth%2Femail%2Flogin&manageToken=true&isUserDetailsRequired=true"
        payload = {"otp": otp, "correlationId": correlation_id}
        resp = self._post(path, json_payload=payload, server_header="AUTH")
        data = (resp or {}).get("data", {})
        token = data.get("token")
        merchant_secret = data.get("merchantDetails", {}).get("secretKey")
        if not token:
            logger.error("OTP verification failed: no token returned")
            raise SystemExit(2)
        # Attach token as cookie for subsequent calls
        self.session.cookies.set("token", token)
        # If a merchant secret is returned, keep it for downstream calls
        if merchant_secret:
            self.session.headers.update({"x-secret-key": merchant_secret})
        # Do not log the raw token at INFO level
        logger.debug("OTP verified; token and merchant secret captured")
        return {"token": token, "merchant_secret": merchant_secret, "raw": data}

    def complete_login(self, token: str) -> dict:
        logger.info("Completing login with token…")
        # Matches: POST /api/login with x-server: https://api.skydo.com
        payload = {"token": token, "utmAttributes": {}, "referralData": {}}
        return self._post("login", json_payload=payload, server_header="https://api.skydo.com")

    def validate_session(self) -> dict:
        logger.info("Validating session…")
        # Matches: POST route/session_validator with x-server: https://api.skydo.com
        return self._post("route/session_validator", json_payload={}, server_header="https://api.skydo.com")


# ----------------------------------
# Skydo API client
# ----------------------------------

class SkydoAPI:
    BASE_URL = "https://dashboard.skydo.com/api"
    invoice_id = 0
    clients = []
    invoice_items = []
    skydo_bank_accounts = []

    def __init__(
        self,
        cookie_str,
        client_name,
        item_name,
        year=None,
        month=None,
        timeout=30,
        session=None,
        ui: "CLIUI | None" = None,
        include_lut: bool = True,
        include_signature: bool = True,
        lut: str | None = None,
        notes: str | None = None,
    ):
        logger.info(
            "Initializing SkydoAPI | client=%s | item=%s | year=%s | month=%s",
            client_name,
            item_name,
            year,
            month,
        )

        self.ui = ui

        current_date = datetime.now()
        self.year = current_date.year if not year else int(year)
        self.month = current_date.month if not month else int(month)
        self.timeout = timeout

        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()
        # Ensure CHALLAN server header for invoicing endpoints
        self.session.headers.update({"x-server": "CHALLAN"})
        if cookie_str:
            self.load_cookies(cookie_str)

        if self.ui:
            self.ui.inc_total_steps(1)
            with self.ui.task("Creating invoice draft"):
                self.create_invoice()
            self.ui.inc_total_steps(1)
            with self.ui.task("Fetching invoice details"):
                self.get_invoice_details()
        else:
            self.create_invoice()
            self.get_invoice_details()

        chosen_client = None
        for client in self.clients:
            if client_name.lower() == client.get("name", "").lower():
                chosen_client = client
                break
        if not chosen_client:
            logger.error("Client '%s' not found among %d clients", client_name, len(self.clients))
            raise SystemExit(2)
        if self.ui:
            self.ui.inc_total_steps(1)
            with self.ui.task(f"Selecting client '{client_name}'"):
                self.choose_client(chosen_client)
        else:
            self.choose_client(chosen_client)

        chosen_item = None
        for item in self.invoice_items:
            if item_name.lower() == item.get("name", "").lower():
                chosen_item = item
                break
        if not chosen_item:
            logger.error("Item '%s' not found among %d items", item_name, len(self.invoice_items))
            raise SystemExit(2)
        if self.ui:
            self.ui.inc_total_steps(1)
            with self.ui.task(f"Adding items '{item_name}'"):
                self.choose_items(chosen_item)
            self.ui.inc_total_steps(1)
            with self.ui.task("Selecting bank account"):
                self.choose_bank_account(currency=chosen_item.get("currency", "USD"))
        else:
            self.choose_items(chosen_item)
            self.choose_bank_account(currency=chosen_item.get("currency", "USD"))

        if self.ui:
            self.ui.inc_total_steps(1)
            with self.ui.task("Updating invoice 'Other Details'"):
                self.update_other_details(
                    include_lut=include_lut,
                    include_signature=include_signature,
                    lut=lut,
                    notes=notes,
                    others={}
                )
        else:
            self.update_other_details(
                include_lut=include_lut,
                include_signature=include_signature,
                lut=lut,
                notes=notes,
                others={}
            )
    def update_other_details(self, *, include_lut: bool = True, include_signature: bool = True, lut: str | None = None, notes: str | None = None, others: dict | None = None) -> None:
        """Update 'Other Details' on the invoice (include LUT/signature, notes, etc.).

        Matches:
          POST /api/create-invoice/update-invoice?path=%2Fchallan%2Fupdate%2Fother%2Fdetails&amp;invoiceId={id}
          Headers: x-server: CHALLAN (already set on session)
        """
        logger.info("Updating 'Other Details' | includeLut=%s | includeSignature=%s | lut=%s", include_lut, include_signature, (lut or ""))
        encoded_path = quote("/challan/update/other/details", safe="")
        endpoint = f"create-invoice/update-invoice?path={encoded_path}&invoiceId={self.invoice_id}"
        payload = {
            "invoiceId": self.invoice_id,
            "includeLut": bool(include_lut),
            "includeSignature": bool(include_signature),
            "notes": notes,
            "lut": lut,
            "others": others or {},
        }
        self._post(endpoint, json_payload=payload)
        logger.info("Other details updated.")

    # ------------- HTTP helpers -------------
    def _post(self, path: str, *, json_payload: dict | None = None) -> dict:
        url = f"{self.BASE_URL}/{path.lstrip('/')}"
        logger.debug("POST %s | payload_keys=%s", url, list((json_payload or {}).keys()))
        try:
            if getattr(self, "ui", None):
                self.ui.set_detail(f"POST {url}")
            resp = self.session.post(url, json=json_payload, timeout=self.timeout)
            resp.raise_for_status()
            if getattr(self, "ui", None):
                self.ui.set_detail(f"{resp.status_code} {url}")
            logger.debug("Response %s | status=%s", url, resp.status_code)
            return resp.json()
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "<no body>")
            logger.error("HTTPError on POST %s | status=%s | body=%s", url, getattr(e.response, "status_code", "?"), body[:500])
            raise
        except requests.RequestException as e:
            logger.error("RequestException on POST %s | err=%s", url, e)
            raise
        except json.JSONDecodeError:
            logger.error("Invalid JSON in response from %s", url)
            raise

    def _get(self, path: str) -> dict:
        url = f"{self.BASE_URL}/{path.lstrip('/')}"
        logger.debug("GET %s", url)
        try:
            if getattr(self, "ui", None):
                self.ui.set_detail(f"GET  {url}")
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            if getattr(self, "ui", None):
                self.ui.set_detail(f"{resp.status_code} {url}")
            logger.debug("Response %s | status=%s", url, resp.status_code)
            return resp.json()
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "<no body>")
            logger.error("HTTPError on GET %s | status=%s | body=%s", url, getattr(e.response, "status_code", "?"), body[:500])
            raise
        except requests.RequestException as e:
            logger.error("RequestException on GET %s | err=%s", url, e)
            raise
        except json.JSONDecodeError:
            logger.error("Invalid JSON in response from %s", url)
            raise

    # ------------- Cookie & API ops -------------
    def load_cookies(self, cookie_str):
        logger.info("Loading cookies into session")
        cookies = {}
        for part in cookie_str.split("; "):
            if not part:
                continue
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            cookies[k] = v
        for name, value in cookies.items():
            self.session.cookies.set(name, value)
        logger.debug("Loaded %d cookie(s) into session", len(cookies))

    def create_invoice(self):
        logger.info("Creating invoice draft…")
        endpoint = "route?path=challan/create/invoice"
        response = self._post(endpoint)
        self.invoice_id = response.get("data")
        logger.info("Invoice created | id=%s", self.invoice_id)

    def get_invoice_details(self):
        logger.info("Fetching invoice details | id=%s", self.invoice_id)
        endpoint = f"create-invoice/get-invoice-details?invoiceId={self.invoice_id}"
        response = self._get(endpoint)
        cache = response.get("data", {}).get("cacheDetails", {})
        self.clients = cache.get("challanClients", [])
        self.invoice_items = cache.get("invoiceItems", [])
        self.skydo_bank_accounts = cache.get("skydoBankAccounts", [])
        logger.debug("Loaded %d client(s) and %d item(s)", len(self.clients), len(self.invoice_items))

    def choose_client(self, client):
        logger.info("Selecting client | %s", client.get("name"))
        endpoint = f"create-invoice/update-invoice?path=challan/update/bill/to&invoiceId={self.invoice_id}"
        payload = {
            "invoiceId": self.invoice_id,
            "name": client["name"],
            "address": client.get("address", "").replace("\n", " "),
            "pincode": "",
            "country": client.get("country"),
            "gstin": "",
            "placeOfSupply": "Other country(96)",
            "gstinNotAvailable": False,
            "gstinVerified": False,
        }
        self._post(endpoint, json_payload=payload)

    def choose_items(self, item):
        logger.info(
            "Adding invoice items | item=%s | month=%02d/%04d | rate=%s %s/hr",
            item.get("name"),
            self.month,
            self.year,
            item.get("currency"),
            item.get("rate"),
        )

        # Generate weekly items (old behavior preserved)
        weekly = get_invoice_items(self.year, self.month, item["rate"], item["name"])
        hours_total = sum(w["quantity"] for w in weekly)
        sub_total = sum(w["total"] for w in weekly)

        # Map weekly items to the new API's expected shape but keep one line per week
        weekly_new_shape = []
        for w in weekly:
            weekly_new_shape.append({
                "quantity": str(w["quantity"]),  # API expects string
                "igstApplicable": True,
                "isMaxAmountBreach": False,
                "description": w.get("description"),
                "sac": None,
                "rate": w["rate"],
                "igst": None,
                "cgst": None,
                "sgst": None,
                "name": w["name"],
                "unit": "QUANTITY",  # quantity represents hours per week
                "total": w["total"],
            })

        # New encoded endpoint (no fallbacks)
        encoded_path = quote("/challan/update/items", safe="")
        endpoint = f"create-invoice/update-invoice?path={encoded_path}&invoiceId={self.invoice_id}"
        payload = {
            "invoiceId": self.invoice_id,
            "invoiceFinancial": {
                "currency": item["currency"],
                "subTotal": sub_total,
                "discountPercentage": 0,
                "discountValue": None,
                "total": sub_total,
                "totalCgst": 0,
                "totalSgst": 0,
                "totalIgst": 0,
                "igstApplicable": True,
                "itemTotal": sub_total,
            },
            "invoiceItems": weekly_new_shape,
        }

        self._post(endpoint, json_payload=payload)
        logger.info(
            "Invoice items updated (weekly) | weeks=%d | hours=%s | subtotal=%s %s",
            len(weekly_new_shape),
            hours_total,
            item.get("currency"),
            sub_total,
        )


    def choose_bank_account(self, *, currency: str = "USD", skydo_bank_type: str = "LOCAL_ACCOUNT") -> None:
        """Select and set the Skydo bank account for the invoice.

        Prefers a matching-currency account with paymentType == 'regular'.
        """
        if not self.skydo_bank_accounts:
            # refresh cache just in case
            try:
                self.get_invoice_details()
            except Exception:
                logger.exception("Could not refresh invoice details before selecting bank account")

        candidates = [acc for acc in (self.skydo_bank_accounts or []) if acc.get("currency") == currency]
        if not candidates:
            logger.error("No Skydo bank accounts found for currency %s", currency)
            raise SystemExit(2)

        # Prefer 'regular' paymentType if available
        regular = [acc for acc in candidates if acc.get("paymentType") == "regular"]
        chosen = (regular or candidates)[0]
        bank_account_id = chosen.get("id")
        logger.info("Selecting bank account | id=%s | currency=%s | paymentType=%s", bank_account_id, currency, chosen.get("paymentType"))

        encoded_path = quote("/challan/update/bankaccount", safe="")
        endpoint = f"create-invoice/update-invoice?path={encoded_path}&invoiceId={self.invoice_id}"
        payload = {
            "invoiceId": self.invoice_id,
            "bankType": "SKYDO",
            "bankAccountId": bank_account_id,
            "currency": currency,
            "skydoBankType": skydo_bank_type,
            "paymentLinkMethod": None,
            "passOnBankFee": None,
            "passOnCardsFee": None,
            "passOnNetBankingFee": None,
        }
        self._post(endpoint, json_payload=payload)
        logger.info("Bank account updated | id=%s | currency=%s | type=%s", bank_account_id, currency, skydo_bank_type)


# ----------------------------------
# CLI & interactive month/year prompt
# ----------------------------------

MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def prompt_for_month_year(default_year: int, default_month: int) -> tuple[int, int]:
    """Interactive prompt to select month and year.

    Accepts numeric input (1–12) or words like "current" / "previous".
    Returns (year, month).
    """
    print("\n=== Invoice Period Selection ===")
    print("Enter the month to generate the invoice for.")
    print("You can type a number (1-12), the month name, 'current', or 'previous'.")
    print(f"Press ENTER for default [{MONTH_NAMES[default_month-1]} {default_year}].")

    # Month
    while True:
        raw = input("Month: ").strip()
        if not raw:
            month = default_month
            break
        low = raw.lower()
        if low in {"current", "this"}:
            month = default_month
            break
        if low in {"previous", "prev", "last"}:
            if default_month == 1:
                return (default_year - 1, 12)
            return (default_year, default_month - 1)
        # Try number
        if raw.isdigit() and 1 <= int(raw) <= 12:
            month = int(raw)
            break
        # Try name
        try:
            month = [m.lower() for m in MONTH_NAMES].index(low) + 1
            break
        except ValueError:
            print("Invalid month. Try again.")

    # Year
    while True:
        raw_y = input(f"Year (ENTER for {default_year}): ").strip()
        if not raw_y:
            year = default_year
            break
        if raw_y.isdigit() and 1900 <= int(raw_y) <= 2999:
            year = int(raw_y)
            break
        print("Invalid year. Try again.")

    return (year, month)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create a Skydo invoice for a client and item, with interactive month selection.",
    )
    parser.add_argument("--client-name", required=True, help="Client name as it appears in Skydo")
    parser.add_argument("--item-name", required=True, help="Invoice Item name as it appears in Skydo")
    parser.add_argument(
        "--cookie",
        default=os.environ.get("SKYDO_COOKIE", ""),
        help="Cookie string for Skydo (or set SKYDO_COOKIE env var)",
    )
    parser.add_argument("--year", type=int, help="Invoice year (skip to be prompted)")
    parser.add_argument("--month", type=int, help="Invoice month 1-12 (skip to be prompted)")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("SKYDO_LOG_LEVEL", "INFO"),
        help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("SKYDO_HTTP_TIMEOUT", "30")),
        help="HTTP timeout in seconds (default 30)",
    )
    parser.add_argument("--email", help="If set, perform email OTP login to obtain a session token")
    parser.add_argument("--otp", help="OTP code for login (if omitted, you will be prompted)")
    parser.add_argument(
        "--secret-key",
        default=os.environ.get("SKYDO_SECRET_KEY", ""),
        help="Optional merchant/app secret key header for auth calls",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="Skip session validation after login",
    )
    parser.add_argument(
        "--session-cache",
        default=os.environ.get("SKYDO_SESSION_CACHE", os.path.expanduser("~/.cache/skydo-invoicer/session.json")),
        help="Path to read/write cached auth token (default: ~/.cache/skydo-invoicer/session.json)",
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Ignore any cached token and perform fresh login",
    )
    parser.add_argument(
        "--lut",
        help="LUT number to include in invoice 'Other Details' (e.g., AD090625118874O)",
    )
    parser.add_argument(
        "--notes",
        help="Optional notes to include in invoice 'Other Details'",
    )
    parser.add_argument(
        "--include-lut",
        dest="include_lut",
        action="store_true",
        default=True,
        help="Include LUT in invoice 'Other Details' (default: True)",
    )
    parser.add_argument(
        "--no-include-lut",
        dest="include_lut",
        action="store_false",
        help="Do not include LUT in invoice 'Other Details'",
    )
    parser.add_argument(
        "--include-signature",
        dest="include_signature",
        action="store_true",
        default=True,
        help="Include signature on the invoice (default: True)",
    )
    parser.add_argument(
        "--no-include-signature",
        dest="include_signature",
        action="store_false",
        help="Do not include signature on the invoice",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable the interactive CLI UI/progress spinners (use logs only)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(args.log_level)

    # Enable the keyboard-only CLI UI when not in DEBUG and not explicitly disabled
    ui = CLIUI(enabled=(not args.no_ui and logger.getEffectiveLevel() != logging.DEBUG))
    # Prevent logger from printing lines while the UI is active
    ui.hijack_logger(logger)

    now = datetime.now()
    default_year, default_month = now.year, now.month

    year = args.year
    month = args.month

    if not (year and month):
        year, month = prompt_for_month_year(default_year, default_month)

    # Optional login flow with token cache
    session_for_use = None
    cache_used = False

    # Use cached token if no cookie and not forcing fresh login
    if not args.cookie and not args.force_login:
        cached = load_cached_token(args.session_cache)
        if cached and cached.get("token"):
            logger.info("Found cached token at %s", args.session_cache)
            auth = SkydoAuth(secret_key=cached.get("merchant_secret") or args.secret_key or None, timeout=args.timeout, ui=ui)
            # Attach cached token cookie and any merchant secret
            auth.session.cookies.set("token", cached["token"])
            if cached.get("merchant_secret"):
                auth.session.headers.update({"x-secret-key": cached["merchant_secret"]})
            # Validate + expiry check
            expired = is_token_expired(cached.get("expiryDate"))
            valid = True
            if not args.skip_validate:
                try:
                    ui.inc_total_steps(1)
                    with ui.task("Validating cached session"):
                        val = auth.validate_session()
                    valid = bool((val or {}).get("data", {}).get("isSessionValid"))
                except Exception:
                    logger.exception("Session validation request failed")
                    valid = False
            if not expired and valid:
                session_for_use = auth.session
                cache_used = True
                logger.info("Using cached session token (valid).")
            else:
                logger.info("Cached token expired/invalid; will perform fresh login.")

    # Fresh login if no cookie and cache wasn't used
    if not args.cookie and session_for_use is None:
        email = args.email
        if not email:
            email = input("Email for Skydo login: ").strip()
            if not email:
                logger.error("No email provided for login and no valid cached token/cookie. Aborting.")
                raise SystemExit(2)
        auth = SkydoAuth(secret_key=args.secret_key or None, timeout=args.timeout, ui=ui)
        ui.inc_total_steps(1)
        with ui.task(f"Requesting OTP for {email}"):
            correlation_id = auth.request_otp(email)
        otp = args.otp
        if not otp:
            otp = input(f"Enter OTP sent to {email}: ").strip()
        ui.inc_total_steps(1)
        with ui.task("Verifying OTP"):
            token_info = auth.verify_otp_login(otp, correlation_id)
        ui.inc_total_steps(1)
        with ui.task("Completing login"):
            auth.complete_login(token_info["token"])  # finalize server session
        if not args.skip_validate:
            try:
                ui.inc_total_steps(1)
                with ui.task("Validating session"):
                    val = auth.validate_session()
                ok = (val or {}).get("data", {}).get("isSessionValid")
                if not ok:
                    logger.warning("Session validator returned not-ok: %s", val)
            except Exception:
                logger.exception("Session validation failed (continuing)")
        # Persist token to cache
        try:
            ui.inc_total_steps(1)
            with ui.task("Saving session token to cache"):
                save_cached_token(
                    token=token_info["token"],
                    expiry_iso=(token_info.get("raw") or {}).get("expiryDate"),
                    merchant_secret=token_info.get("merchant_secret"),
                    email=email,
                    path=args.session_cache,
                )
        except Exception:
            logger.exception("Could not persist session token cache")
        session_for_use = auth.session

    logger.info(
        "Starting invoice generation | client=%s | item=%s | period=%02d/%04d",
        args.client_name,
        args.item_name,
        month,
        year,
    )

    try:
        SkydoAPI(
            cookie_str=args.cookie,
            client_name=args.client_name,
            item_name=args.item_name,
            year=year,
            month=month,
            timeout=args.timeout,
            session=session_for_use,
            ui=ui,
            include_lut=args.include_lut,
            include_signature=args.include_signature,
            lut=args.lut,
            notes=args.notes,
        )
        logger.info("Invoice draft prepared successfully.")
    except Exception:
        logger.exception("Failed to prepare invoice draft.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
