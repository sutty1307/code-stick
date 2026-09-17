"""Outbound ACH adapters with separate sandbox and production configurations."""
import base64
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SANDBOX = "https://api-sandbox.dwolla.com"
PRODUCTION = "https://api.dwolla.com"
MEDIA = "application/vnd.dwolla.v1.hal+json"


class ProviderError(Exception):
    pass


def identifier(value):
    if not isinstance(value, str):
        raise ValueError("A valid provider UUID is required.")
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("A valid provider UUID is required.") from None
    if parsed != value.lower():
        raise ValueError("Use a hyphenated provider UUID.")
    return parsed


def resource_url(kind, value, base=SANDBOX):
    if base not in {SANDBOX, PRODUCTION} or kind not in {"funding-sources", "customers", "accounts", "transfers"}:
        raise ValueError("Unsupported resource.")
    return f"{base}/{kind}/{identifier(value)}"


def resource_id(url, kind, base=SANDBOX):
    if base not in {SANDBOX, PRODUCTION}:
        raise ProviderError("Unsupported provider environment.")
    prefix = f"{base}/{kind}/"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise ProviderError("Provider returned an unexpected resource address.")
    try:
        return identifier(url[len(prefix):])
    except ValueError:
        raise ProviderError("Provider returned an invalid resource identifier.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("Provider redirects are refused.")


class Simulator:
    name = "simulator"

    def validate_recipient(self, customer_id, funding_id):
        if isinstance(funding_id, str) and funding_id.startswith("simulated:"):
            return funding_id
        return "simulated:" + str(uuid.uuid4())

    def check_source(self, source_id):
        if source_id != "simulated:business":
            raise ProviderError("The simulated business account is not configured.")

    def submit(self, payment):
        # Deterministic within a payment. No network calls exist in this adapter.
        return "simulated:" + payment["id"]


class DwollaSandbox:
    name = "dwolla_sandbox"
    api_base = SANDBOX

    def __init__(self, key, secret, source_id, base=SANDBOX):
        if base != self.api_base:
            raise ValueError(f"This adapter requires exactly {self.api_base}.")
        if not key or not secret:
            raise ValueError("Set the matching Dwolla environment's key and secret on the server.")
        self.source_id = identifier(source_id)
        self.key, self.secret = key, secret
        self.token = None
        self.expires = 0
        self.lock = threading.Lock()
        self.http = urllib.request.build_opener(NoRedirect())

    def _exchange(self, path, method="GET", data=None, headers=None):
        # Origins are fixed by adapter class. Provider links never choose a host.
        if not isinstance(path, str) or not re.fullmatch(r"/(?:token|transfers|(?:transfers|funding-sources)/[0-9a-f-]{36}|(?:accounts|customers)/[0-9a-f-]{36}/funding-sources(?:\?removed=false)?)?", path):
            raise ProviderError("Unsupported provider API path.")
        if (path == "/token" and method != "POST") or (path == "/transfers" and method != "POST") or (path not in {"/token", "/transfers"} and method != "GET"):
            raise ProviderError("Unsupported provider API method.")
        request = urllib.request.Request(self.api_base + path, data=data, headers=headers or {}, method=method)
        try:
            with self.http.open(request, timeout=15) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise ProviderError("Provider response exceeded the limit.")
                parsed = json.loads(body) if body else {}
                if not isinstance(parsed, dict):
                    raise ProviderError("Provider response was not an object.")
                return response.status, response.headers, parsed
        except urllib.error.HTTPError as exc:
            # Never expose raw responses, bank details, bearer tokens or personal data.
            raise ProviderError(f"Dwolla provider returned HTTP {exc.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ProviderError("Dwolla provider response could not be verified.") from None

    def _token(self):
        with self.lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            basic = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
            status, _, body = self._exchange("/token", "POST", b"grant_type=client_credentials", {
                "Authorization": "Basic " + basic,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": MEDIA,
            })
            token = body.get("access_token")
            expiry = body.get("expires_in")
            if (status != 200 or not isinstance(token, str) or not token or
                any(ord(c) < 33 or ord(c) > 126 for c in token) or
                type(expiry) not in {int, float} or not math.isfinite(expiry) or expiry <= 0):
                raise ProviderError("Dwolla authentication response was incomplete.")
            self.token, self.expires = token, time.monotonic() + max(0, expiry - 60)
            return self.token

    def request(self, path, method="GET", payload=None, idempotency=None):
        headers = {"Accept": MEDIA, "Authorization": "Bearer " + self._token()}
        data = None
        if payload is not None:
            headers["Content-Type"] = MEDIA
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if idempotency:
            headers["Idempotency-Key"] = identifier(idempotency)
        return self._exchange(path, method, data, headers)

    def validate_recipient(self, customer_id, funding_id):
        customer_url = resource_url("customers", customer_id, self.api_base)
        funding_id = identifier(funding_id)
        status, _, body = self.request("/funding-sources/" + funding_id)
        owner = link(body, "customer")
        if status != 200 or body.get("id") != funding_id or body.get("type") != "bank" or body.get("removed") is not False:
            raise ProviderError("The bank does not belong to this customer, or is unavailable.")
        if owner is None:
            # Funding-source responses may expose only a self link. Verify
            # ownership via the customer's own listing; never assume it.
            status, _, listing = self.request(f"/customers/{identifier(customer_id)}/funding-sources?removed=false")
            embedded = listing.get("_embedded")
            banks = embedded.get("funding-sources") if isinstance(embedded, dict) else None
            matches = [bank for bank in banks if isinstance(bank, dict) and bank.get("id") == funding_id] if isinstance(banks, list) else []
            if status != 200 or len(matches) != 1 or matches[0].get("removed") is not False or matches[0].get("type") != "bank":
                raise ProviderError("The recipient bank's ownership could not be verified.")
        elif owner != customer_url:
            raise ProviderError("The bank does not belong to this customer.")
        return funding_id

    def check_source(self, source_id):
        if identifier(source_id) != self.source_id:
            raise ProviderError("The source differs from this environment's configured bank.")
        status, _, body = self.request("/funding-sources/" + identifier(source_id))
        if status != 200 or body.get("id") != source_id or body.get("removed") is not False or body.get("status") != "verified" or body.get("type") != "bank":
            raise ProviderError("The source must be a verified, active bank funding source.")
        return body

    def submit(self, payment):
        if payment["source_ref"] != self.source_id or payment["source_ref"] == payment["destination_ref"]:
            raise ProviderError("Transfer source or destination is invalid.")
        # A stable local payment UUID is reused as the provider key and correlation ID.
        payload = {
            "_links": {
                "source": {"href": resource_url("funding-sources", payment["source_ref"], self.api_base)},
                "destination": {"href": resource_url("funding-sources", payment["destination_ref"], self.api_base)},
            },
            "amount": {"currency": "USD", "value": f'{payment["amount_cents"] // 100}.{payment["amount_cents"] % 100:02d}'},
            "correlationId": payment["id"],
        }
        status, headers, _ = self.request("/transfers", "POST", payload, payment["id"])
        if status != 201:
            raise ProviderError("Transfer creation was not confirmed.")
        return resource_id(headers.get("Location"), "transfers", self.api_base)

    def refresh(self, payment):
        return self.verify_transfer(payment, payment["provider_ref"])

    def verify_transfer(self, payment, transfer_ref):
        """Read an existing transfer; never retry or create a transfer during recovery.

        Ambiguous submissions additionally require the local payment identity
        sent as correlationId or by the attached adapter in metadata.payment_id.
        Strict endpoint matching may leave a multi-leg transfer unresolved; it
        must never silently attach an unrelated transfer merely matching dollars.
        """
        transfer_ref = identifier(transfer_ref)
        status, _, body = self.request("/transfers/" + transfer_ref)
        amount = body.get("amount")
        expected = f'{payment["amount_cents"] // 100}.{payment["amount_cents"] % 100:02d}'
        if (status != 200 or body.get("id") != transfer_ref or not isinstance(amount, dict) or
            amount.get("currency") != "USD" or amount.get("value") != expected):
            raise ProviderError("Transfer identity or amount does not match the saved payment.")
        for name, ref in (("source", payment["source_ref"]), ("destination", payment["destination_ref"])):
            # Current Dwolla responses identify parties at source/destination,
            # and banks at source-funding-source/destination-funding-source.
            # If an explicit bank relation exists but is malformed, do not fall
            # back to a different link that happens to match.
            relation = name + "-funding-source"
            links = body.get("_links")
            address = link(body, relation) if isinstance(links, dict) and relation in links else link(body, name)
            if address != resource_url("funding-sources", ref, self.api_base):
                raise ProviderError("Transfer accounts do not match the saved payment. Reconcile its transfer legs in the provider dashboard.")
        correlation = body.get("correlationId")
        metadata = body.get("metadata")
        legacy_id = metadata.get("payment_id") if isinstance(metadata, dict) else None
        if (not payment.get("provider_ref") and correlation != payment["id"] and legacy_id != payment["id"]) or (correlation is not None and correlation != payment["id"]):
            raise ProviderError("Transfer correlation ID does not match this payment.")
        state = body.get("status")
        if state not in {"pending", "processed", "failed", "cancelled"}:
            raise ProviderError("Unrecognized provider transfer status; reconcile in the provider dashboard.")
        return state


class DwollaProduction(DwollaSandbox):
    """A live ACH connection; transfer POSTs require an explicit release switch.

    This first deployment pays from one approved business's Main Account bank.
    It does not accept arbitrary third-party funding-source IDs as payers.
    """
    name = "dwolla_production"
    api_base = PRODUCTION

    def __init__(self, key, secret, source_id, account_id, enabled=False, base=PRODUCTION):
        super().__init__(key, secret, source_id, base=base)
        self.account_id = identifier(account_id)
        self.enabled = enabled is True

    def _exchange(self, path, method="GET", data=None, headers=None):
        if path == "/transfers" and method == "POST" and not self.enabled:
            raise ProviderError("Live payment release is disabled on this server.")
        return super()._exchange(path, method, data, headers)

    def _owned_bank(self, kind, owner_id, funding_id, verified):
        status, _, listing = self.request(f"/{kind}/{identifier(owner_id)}/funding-sources?removed=false")
        embedded = listing.get("_embedded")
        banks = embedded.get("funding-sources") if isinstance(embedded, dict) else None
        if status != 200 or not isinstance(banks, list):
            raise ProviderError("The provider bank ownership listing could not be verified.")
        matches = [bank for bank in banks if isinstance(bank, dict) and bank.get("id") == funding_id]
        if len(matches) != 1:
            raise ProviderError("The selected bank was not found under the specified owner.")
        bank = matches[0]
        if (bank.get("removed") is not False or bank.get("type") != "bank" or
            not isinstance(bank.get("channels"), list) or "ach" not in bank["channels"] or
            (verified and bank.get("status") != "verified")):
            raise ProviderError("The selected bank is not active and ACH-capable with the required verification.")
        return bank

    def check_source(self, source_id):
        if identifier(source_id) != self.source_id:
            raise ProviderError("The source differs from the configured live bank.")
        status, _, root = self.request("/")
        if status != 200 or link(root, "account") != resource_url("accounts", self.account_id, PRODUCTION):
            raise ProviderError("The credentials do not identify the configured business account.")
        return self._owned_bank("accounts", self.account_id, self.source_id, verified=True)

    def validate_recipient(self, customer_id, funding_id):
        funding_id = identifier(funding_id)
        self._owned_bank("customers", identifier(customer_id), funding_id, verified=False)
        return funding_id

    def submit(self, payment):
        if not self.enabled:
            raise ProviderError("Live payment release is disabled on this server.")
        return super().submit(payment)


def link(body, relation):
    links = body.get("_links")
    entry = links.get(relation) if isinstance(links, dict) else None
    return entry.get("href") if isinstance(entry, dict) else None
