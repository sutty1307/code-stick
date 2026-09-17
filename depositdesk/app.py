"""DepositDesk: a single-business outgoing ACH payment application.

Run `python app.py init`, then `python app.py`. Standard library only, Python 3.11+.
The bundled WSGI server is for local testing, not public deployment.
"""
import argparse
import csv
import getpass
import hashlib
import hmac
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from providers import DwollaSandbox, DwollaProduction, ProviderError, Simulator, identifier, resource_id, link, SANDBOX, PRODUCTION

ROOT = Path(__file__).resolve().parent
LIMIT_CENTS = 2_500_000  # Application ceiling, not a bank/provider-approved limit.
SESSION_SECONDS = 8 * 3600
MAX_BODY = 16_384
RECOVERY_DELAY_SECONDS = 120
REFRESH_TRANSITIONS = {
    "pending": {"pending", "processed", "failed", "cancelled"},
    "processed": {"processed", "failed"},
    "failed": {"failed"}, "cancelled": {"cancelled"}, "returned": {"returned"},
}


class Problem(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def text_value(value, name, maximum=140, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum or any(ord(c) < 32 for c in value):
        raise Problem(400, f"{name} must be {minimum}–{maximum} characters, without control characters.")
    return value.strip()


def cents(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?", value):
        raise Problem(400, "Enter a dollar amount with at most two decimal places, such as 125.50.")
    whole, _, fraction = value.partition(".")
    result = int(whole) * 100 + int(fraction.ljust(2, "0") or "0")
    if not 0 < result <= LIMIT_CENTS:
        raise Problem(400, "The payment must be between $0.01 and $25,000.00; provider limits also apply.")
    return result


def password_record(password):
    if len(password) < 14 or len(password) > 256:
        raise ValueError("Choose a password of 14–256 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return {"salt": salt.hex(), "digest": digest.hex()}


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipients(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,account_label TEXT NOT NULL,
 destination_ref TEXT NOT NULL UNIQUE,customer_id TEXT NOT NULL,authorization_ref TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments(
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,payload_hash TEXT NOT NULL,
 recipient_id TEXT NOT NULL REFERENCES recipients(id),recipient_name TEXT NOT NULL,
 account_label TEXT NOT NULL,authorization_ref TEXT NOT NULL,source_ref TEXT NOT NULL,
 destination_ref TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>0 AND amount_cents<=2500000),
 reference TEXT NOT NULL,memo TEXT NOT NULL,state TEXT NOT NULL,provider_ref TEXT,
 message TEXT NOT NULL DEFAULT '',refresh_due INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,approved_at TEXT,
 UNIQUE(recipient_id,reference));
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT,payment_id TEXT,action TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT NOT NULL,created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS webhook_events(id TEXT PRIMARY KEY,resource_ref TEXT NOT NULL,topic TEXT NOT NULL,created_at TEXT NOT NULL);
"""


class Desk:
    def __init__(self, directory, config, provider=None):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = self.directory / "depositdesk.sqlite3"
        self.config = config
        self.mode = config.get("provider", "simulator")
        if self.mode not in {"simulator", "dwolla_sandbox", "dwolla_production"}:
            raise ValueError("Use simulator, dwolla_sandbox or dwolla_production.")
        self.live = self.mode == "dwolla_production"
        self.live_enabled = self.live and truthy(config.get("enable_live_payments", ""))
        self.api_base = PRODUCTION if self.live else SANDBOX
        self.source = identifier(config.get("source_id", "")) if self.mode != "simulator" else "simulated:business"
        self.origin = config.get("origin", "http://127.0.0.1:8765").rstrip("/")
        parsed = urlsplit(self.origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Configure a single absolute HTTP(S) origin.")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Non-local origins require HTTPS.")
        self.host = parsed.netloc
        self.secure = parsed.scheme == "https"
        if self.live:
            if not self.secure:
                raise ValueError("Live mode requires an HTTPS origin, including during initialization.")
            identifier(config.get("account_id", ""))
            if not isinstance(config.get("webhook_secret"), str) or len(config["webhook_secret"]) < 16:
                raise ValueError("Configure the production webhook subscription secret (at least 16 characters).")
        # Set only when a reverse proxy you control terminates TLS in front of the app.
        # Without it, X-Forwarded-For is ignored, because anyone could send one.
        self.behind_proxy = truthy(config.get("behind_proxy", ""))
        self.trusted_proxies = tuple(ipaddress.ip_network(value.strip()) for value in
            config.get("trusted_proxies", "127.0.0.1/32,::1/128").split(",") if value.strip())
        self.provider = provider or (Simulator() if self.mode == "simulator" else
            DwollaProduction(config.get("key"), config.get("secret"), self.source,
                config.get("account_id"), enabled=self.live_enabled) if self.live else
            DwollaSandbox(config.get("key"), config.get("secret"), self.source))
        with self.connection() as db:
            db.executescript(SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")
            binding = self.mode + ":" + self.source
            if self.live:
                binding += ":" + identifier(config["account_id"])
            existing = db.execute("SELECT value FROM metadata WHERE key='environment'").fetchone()
            if existing and existing[0] != binding:
                raise ValueError("Use a separate data directory when changing provider or source account.")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('environment',?)", (binding,))
            # All workers read this credential. Rotating it and deleting sessions
            # is one transaction, including when a login is already in flight.
            if not db.execute("SELECT 1 FROM metadata WHERE key='owner_auth'").fetchone():
                if not isinstance(config.get("password"), dict):
                    raise ValueError("Owner credentials are missing. Restore the database from backup.")
                db.execute("INSERT INTO metadata VALUES('owner_auth',?)", (json.dumps(config["password"], sort_keys=True),))
            self.migrate(db)
        os.chmod(self.db_path, 0o600)

    def migrate(self, db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(payments)")}
        if "reference_key" not in columns:
            db.execute("ALTER TABLE payments ADD COLUMN reference_key TEXT")
        if "refresh_version" not in columns:
            db.execute("ALTER TABLE payments ADD COLUMN refresh_version INTEGER NOT NULL DEFAULT 0")
        for row in db.execute("SELECT id,reference FROM payments WHERE reference_key IS NULL").fetchall():
            db.execute("UPDATE payments SET reference_key=? WHERE id=?", (row["reference"].casefold(), row["id"]))
        # The attached adapter stored full funding-source URLs; the original
        # adapter used UUIDs. Canonicalize only exact sandbox URLs, preserving IDs.
        if self.mode == "dwolla_sandbox":
            for table in ("recipients", "payments"):
                for row in db.execute(f"SELECT id,destination_ref FROM {table}").fetchall():
                    if row["destination_ref"].startswith("https://"):
                        db.execute(f"UPDATE {table} SET destination_ref=? WHERE id=?",
                            (resource_id(row["destination_ref"], "funding-sources"), row["id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS payment_reference_key ON payments(recipient_id,reference_key)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS payment_provider_ref ON payments(provider_ref) WHERE provider_ref IS NOT NULL")
        db.execute("CREATE INDEX IF NOT EXISTS audit_payment ON audit(payment_id,id)")
        db.execute("PRAGMA user_version=2")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, db, payment_id, action, detail):
        db.execute("INSERT INTO audit(payment_id,action,detail,created_at) VALUES(?,?,?,?)", (payment_id, action, detail, stamp()))

    def public_payment(self, row):
        result = {k: row[k] for k in ("id", "recipient_id", "recipient_name", "account_label", "authorization_ref", "amount_cents", "reference", "memo", "state", "provider_ref", "message", "refresh_due", "created_at", "updated_at", "approved_at")}
        result["can_reconcile"] = self.mode != "simulator" and not row["provider_ref"] and (
            row["state"] == "needs_review" or (row["state"] == "submitting" and
            (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds() >= RECOVERY_DELAY_SECONDS))
        return result

    def payment(self, db, payment_id):
        row = db.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not row:
            raise Problem(404, "Payment not found.")
        return row

    def add_recipient(self, data):
        name = text_value(data.get("name"), "Recipient name", 80)
        kind = data.get("kind")
        if kind not in {"contractor", "employee", "vendor"}:
            raise Problem(400, "Choose contractor, employee or vendor.")
        label = text_value(data.get("account_label"), "Account nickname", 60)
        authorization = text_value(data.get("authorization_ref"), "Authorization record reference", 100)
        if data.get("acknowledged") is not True:
            raise Problem(400, "Confirm the recipient and that the authorization record exists.")
        customer = identifier(data.get("customer_id")) if self.mode != "simulator" else "simulated"
        destination = self.provider.validate_recipient(customer, data.get("funding_id"))
        if destination == self.source:
            raise Problem(400, "Source and destination must be different.")
        rid = str(uuid.uuid4())
        with self.connection() as db:
            db.execute("INSERT INTO recipients VALUES(?,?,?,?,?,?,?,?)", (rid, name, kind, label, destination, customer, authorization, stamp()))
            self.audit(db, None, "recipient_added", "Recipient record created: " + rid)
        return {"id": rid}

    def draft(self, data):
        request_id = identifier(data.get("request_id"))
        recipient_id = identifier(data.get("recipient_id"))
        amount = cents(data.get("amount"))
        reference = text_value(data.get("reference"), "Payment reference", 80)
        reference_key = reference.casefold()
        memo = text_value(data.get("memo", ""), "Memo", 140, 0)
        canonical = json.dumps([recipient_id, amount, reference_key, memo], separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM payments WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise Problem(409, "This request identifier already belongs to a different payment.")
                return self.public_payment(existing)
            recipient = db.execute("SELECT * FROM recipients WHERE id=?", (recipient_id,)).fetchone()
            if not recipient:
                raise Problem(404, "Choose an existing recipient.")
            if db.execute("SELECT 1 FROM payments WHERE recipient_id=? AND reference_key=?", (recipient_id, reference_key)).fetchone():
                raise Problem(409, "This recipient already has a payment with that reference. Review the existing record.")
            payment_id, now = str(uuid.uuid4()), stamp()
            db.execute("""INSERT INTO payments(id,request_id,payload_hash,recipient_id,recipient_name,account_label,authorization_ref,
                source_ref,destination_ref,amount_cents,reference,memo,state,created_at,updated_at,reference_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (payment_id, request_id, digest, recipient_id, recipient["name"], recipient["account_label"],
                recipient["authorization_ref"], self.source, recipient["destination_ref"], amount, reference, memo, "draft", now, now, reference_key))
            self.audit(db, payment_id, "draft_created", "Payment details saved for review. No submission.")
            return self.public_payment(self.payment(db, payment_id))

    def submit(self, payment_id, data):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            payment = dict(self.payment(db, payment_id))
            if data.get("authorized") is not True or cents(data.get("amount")) != payment["amount_cents"]:
                raise Problem(400, "Review and confirm the exact amount before submitting.")
            if payment["state"] != "draft":
                # Never resubmit, even after a crash or an ambiguous timeout.
                return self.public_payment(payment)
            if self.live and not self.live_enabled:
                raise Problem(403, "Live payment release is disabled on this server. Connection checks and drafts remain available.")
            if self.live and data.get("live_confirmed") is not True:
                raise Problem(400, "Confirm that this instruction will move real money before releasing it.")
            now = stamp()
            db.execute("UPDATE payments SET state='submitting',approved_at=?,updated_at=? WHERE id=?", (now, now, payment_id))
            self.audit(db, payment_id, "submission_started", "Owner confirmed the immutable recipient and amount; " + self.mode + " transfer submission started.")
        try:
            self.provider.check_source(payment["source_ref"])
            if self.mode != "simulator":
                with self.connection() as db:
                    customer = db.execute("SELECT customer_id FROM recipients WHERE id=?", (payment["recipient_id"],)).fetchone()[0]
                if self.provider.validate_recipient(customer, payment["destination_ref"]) != payment["destination_ref"]:
                    raise ProviderError("The destination has changed.")
        except Exception:
            # No transfer request was attempted. Distinguish this from an uncertain POST outcome.
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self.payment(db, payment_id)
                if current["state"] != "submitting" or current["provider_ref"]:
                    return self.public_payment(current)
                db.execute("UPDATE payments SET state='draft',approved_at=NULL,message=?,updated_at=? WHERE id=?", (
                    "The source or recipient account could not be validated. No transfer was submitted. Use Check connection and verify the recipient in the provider dashboard.", stamp(), payment_id))
                self.audit(db, payment_id, "source_check_failed", "Source validation did not complete. No transfer request was attempted.")
                return self.public_payment(self.payment(db, payment_id))
        try:
            provider_ref = self.provider.submit(payment)
        except Exception:
            # A transport failure can occur after a transfer was accepted. Never call it failed or retry it.
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self.payment(db, payment_id)
                if current["state"] != "submitting" or current["provider_ref"]:
                    return self.public_payment(current)
                db.execute("UPDATE payments SET state='needs_review',message=?,updated_at=? WHERE id=?", (
                    "Submission outcome is unconfirmed. Reconcile the payment ID in the provider dashboard before any replacement payment.", stamp(), payment_id))
                self.audit(db, payment_id, "submission_unconfirmed", "No automatic retry. Reconciliation is required.")
                return self.public_payment(self.payment(db, payment_id))
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = self.payment(db, payment_id)
            if saved["state"] != "submitting" or saved["provider_ref"]:
                if saved["provider_ref"] != provider_ref:
                    self.audit(db, payment_id, "late_response_conflict", "A late submission response differs from the reconciled transfer. Investigate in the provider dashboard.")
                    db.execute("UPDATE payments SET message=?,refresh_due=1 WHERE id=?", ("A late provider response conflicts with this record. Manual reconciliation is required.", payment_id))
                return self.public_payment(self.payment(db, payment_id))
            due = bool(db.execute("SELECT 1 FROM webhook_events WHERE resource_ref=?", (provider_ref,)).fetchone())
            db.execute("UPDATE payments SET state='pending',provider_ref=?,message=?,updated_at=? WHERE id=?", (provider_ref,
                "Submission accepted. This is not proof of delivery or bank classification.", stamp(), payment_id))
            if due:
                db.execute("UPDATE payments SET refresh_due=1,refresh_version=refresh_version+1 WHERE id=?", (payment_id,))
            self.audit(db, payment_id, "submission_accepted", "Provider transfer reference saved; awaiting a status update.")
            return self.public_payment(self.payment(db, payment_id))

    def action(self, payment_id, action, data):
        if action == "submit":
            return self.submit(payment_id, data)
        with self.connection() as db:
            payment = dict(self.payment(db, payment_id))
        if action == "reconcile":
            if not self.public_payment(payment)["can_reconcile"]:
                raise Problem(409, "Only an unconfirmed submission can be reconciled. Allow two minutes for an active submission to finish.")
            transfer_ref = identifier(data.get("provider_ref"))
            state = self.provider.verify_transfer(payment, transfer_ref)
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self.payment(db, payment_id)
                if current["state"] != payment["state"] or current["updated_at"] != payment["updated_at"] or current["provider_ref"]:
                    raise Problem(409, "The payment changed. Reload before reconciling.")
                db.execute("UPDATE payments SET provider_ref=?,state=?,message=?,updated_at=?,refresh_due=1 WHERE id=?",
                    (transfer_ref, state, "Existing provider transfer verified and linked. No new transfer was submitted.", stamp(), payment_id))
                self.audit(db, payment_id, "submission_reconciled", "Provider identity, USD amount, source, destination and local payment identity verified against the saved instruction. Transfer: " + transfer_ref)
                return self.public_payment(self.payment(db, payment_id))
        if action == "refresh":
            if self.mode == "simulator" or not payment["provider_ref"]:
                raise Problem(409, "There is no external transfer to refresh.")
            state = self.provider.refresh(payment)
            message = "Latest status of the provider transfer resource. End-to-end delivery is not independently verified."
        elif action == "simulate":
            if self.mode != "simulator":
                raise Problem(403, "Simulation controls are available only in the local simulator.")
            state = data.get("state")
            allowed = {"pending": {"processed", "failed"}, "processed": {"returned"}}
            if state not in allowed.get(payment["state"], set()):
                raise Problem(409, "This simulated status transition is not allowed.")
            message = "Simulated test outcome only. No bank transaction occurred."
        elif action == "cancel":
            if payment["state"] != "draft":
                raise Problem(409, "Only unsubmitted drafts can be cancelled here.")
            state, message = "cancelled", "Draft cancelled before submission."
        else:
            raise Problem(404, "Action not found.")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.payment(db, payment_id)["updated_at"] != payment["updated_at"] or self.payment(db, payment_id)["state"] != payment["state"]:
                raise Problem(409, "The payment changed. Reload its current status.")
            # A stale provider read must not revert a terminal status to pending.
            if action == "refresh" and state not in REFRESH_TRANSITIONS.get(payment["state"], set()):
                raise Problem(409, "Provider status conflicts with the saved status. Reconcile the transfer.")
            db.execute("""UPDATE payments SET state=?,message=?,updated_at=?,
                refresh_due=CASE WHEN refresh_version=? THEN 0 ELSE refresh_due END WHERE id=?""",
                (state, message, stamp(), payment["refresh_version"], payment_id))
            self.audit(db, payment_id, action + "_" + state, message)
            return self.public_payment(self.payment(db, payment_id))

    def webhook(self, raw, signature):
        secret = self.config.get("webhook_secret", "")
        if self.mode == "simulator" or not secret:
            raise Problem(503, "Provider webhooks are not configured.")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature) or not hmac.compare_digest(signature, expected):
            raise Problem(401, "Invalid webhook signature.")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise Problem(400, "Invalid webhook event.")
        event_id = identifier(event.get("id"))
        topic = text_value(event.get("topic"), "Event topic", 100)
        url = link(event, "resource")
        # Ignore unrelated customer/account events. Never trust an event's claimed payment status.
        if not isinstance(url, str):
            raise Problem(400, "Webhook resource link is missing or invalid.")
        if "/transfers/" not in url:
            return {"received": True}
        transfer_ref = resource_id(url, "transfers", self.api_base)
        with self.connection() as db:
            inserted = db.execute("INSERT OR IGNORE INTO webhook_events VALUES(?,?,?,?)", (event_id, transfer_ref, topic, stamp())).rowcount
            if inserted:
                db.execute("UPDATE payments SET refresh_due=1,refresh_version=refresh_version+1 WHERE provider_ref=?", (transfer_ref,))
        return {"received": True}

    def connection_report(self):
        """Provider reads and token authentication only; no payment instruction."""
        report = {"mode": self.mode, "live_enabled": self.live_enabled,
            "connected": False, "checked_at": stamp(), "transfer_submitted": False,
            "webhook_configured": bool(self.config.get("webhook_secret")),
            "bank_classification": "Not verified; receiving-bank evidence is required."}
        if self.mode == "simulator":
            report["message"] = "Simulator only. No bank is connected."
            return report
        try:
            bank = self.provider.check_source(self.source)
        except ProviderError as exc:
            report["message"] = str(exc)
            return report
        report["connected"] = True
        report["message"] = "Source verified with the provider. No payment was submitted; funds availability and recipient-bank receipt were not checked."
        if isinstance(bank, dict):
            # Do not forward the provider object (which can include personal data).
            report["source_name"] = bank.get("name", "Configured business bank")
        return report

    def login(self, password, ip):
        now = int(time.time())
        window = 1800 if self.live else 900
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE created<?", (now - window,))
            count = db.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=?", (ip,)).fetchone()[0]
            if self.live and db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] >= 10:
                raise Problem(429, "Owner sign-in is temporarily locked. Try again in 30 minutes.")
            if count >= 5:
                raise Problem(429, f"Too many sign-in attempts. Try again in {window // 60} minutes.")
            db.execute("INSERT INTO login_attempts VALUES(?,?)", (ip, now))
            credential = db.execute("SELECT value FROM metadata WHERE key='owner_auth'").fetchone()[0]
        valid = False
        if isinstance(password, str) and len(password) <= 256:
            record = json.loads(credential)
            computed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record["salt"]), n=16384, r=8, p=1).hex()
            valid = hmac.compare_digest(computed, record["digest"])
        if not valid:
            raise Problem(401, "Password not recognized.")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT value FROM metadata WHERE key='owner_auth'").fetchone()[0] != credential:
                raise Problem(401, "The owner password changed. Sign in again.")
            db.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), csrf, now + SESSION_SECONDS))
        return token, csrf

    def client_ip(self, environ):
        direct = environ.get("REMOTE_ADDR", "unknown")
        try:
            peer = ipaddress.ip_address(direct)
        except ValueError:
            return "unknown"
        if not self.behind_proxy or not any(peer in network for network in self.trusted_proxies):
            return direct
        # The proxy appends the peer it saw, so the last entry is the only one it
        # wrote. Earlier entries are client-supplied and must not be trusted.
        forwarded = environ.get("HTTP_X_FORWARDED_FOR", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        try:
            return str(ipaddress.ip_address(hops[-1])) if hops else direct
        except ValueError:
            return direct

    def session(self, environ):
        cookies = SimpleCookie()
        try:
            cookies.load(environ.get("HTTP_COOKIE", ""))
            token = cookies["desk_session"].value
        except (KeyError, ValueError):
            raise Problem(401, "Sign in to continue.") from None
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
        if not row:
            raise Problem(401, "Your session expired. Sign in again.")
        return row

    def handle(self, environ):
        path, method = environ.get("PATH_INFO", "/"), environ.get("REQUEST_METHOD", "GET")
        # Answered before the host check so a load balancer can probe by address.
        if path == "/healthz" and method == "GET":
            return 200, b'{"status":"ok"}', [("Content-Type", "application/json")]
        if path == "/readyz" and method == "GET":
            try:
                with self.connection() as db:
                    if not db.execute("SELECT value FROM metadata WHERE key='owner_auth'").fetchone():
                        raise sqlite3.DatabaseError("Owner configuration is unavailable.")
                return 200, b'{"status":"ready"}', [("Content-Type", "application/json")]
            except sqlite3.Error:
                return 503, b'{"status":"unavailable"}', [("Content-Type", "application/json")]
        if environ.get("HTTP_HOST") != self.host:
            raise Problem(403, "Open the configured application address.")
        if method not in {"GET", "POST"}:
            raise Problem(405, "Method not supported.")
        if method == "GET" and path in {"/", "/app.js", "/styles.css", "/favicon.svg"}:
            filename = "index.html" if path == "/" else path[1:]
            content_type = {"index.html": "text/html", "app.js": "text/javascript",
                "styles.css": "text/css", "favicon.svg": "image/svg+xml"}[filename]
            return 200, (ROOT / "static" / filename).read_bytes(), [("Content-Type", content_type + "; charset=utf-8")]
        raw, data = b"", {}
        if method == "POST":
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                raise Problem(400, "Invalid request length.") from None
            if not 0 < length <= MAX_BODY:
                raise Problem(413, "Request body is empty or too large.")
            raw = environ["wsgi.input"].read(length)
            if path == "/api/webhooks/dwolla":
                return self.webhook(raw, environ.get("HTTP_X_REQUEST_SIGNATURE_SHA_256"))
            if environ.get("HTTP_ORIGIN") != self.origin:
                raise Problem(403, "Request origin was not accepted.")
            if environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/json":
                raise Problem(415, "Send a JSON request.")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise Problem(400, "Request must be a JSON object.")
        if path == "/api/login" and method == "POST":
            token, csrf = self.login(data.get("password"), self.client_ip(environ))
            cookie = f"desk_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}" + ("; Secure" if self.secure else "")
            return 200, json.dumps({"csrf": csrf}).encode(), [("Set-Cookie", cookie), ("Content-Type", "application/json")]
        session = self.session(environ)
        if method == "POST" and not hmac.compare_digest(environ.get("HTTP_X_CSRF_TOKEN", "").encode(), session["csrf"].encode()):
            raise Problem(403, "Request verification failed. Reload and try again.")
        if path == "/api/logout" and method == "POST":
            with self.connection() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))
            return 200, b'{}', [("Content-Type", "application/json"), ("Set-Cookie", "desk_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")]
        if path == "/api/state" and method == "GET":
            with self.connection() as db:
                recipients = [dict(r) for r in db.execute("SELECT id,name,kind,account_label,authorization_ref,created_at FROM recipients ORDER BY created_at DESC")]
                payments = [self.public_payment(r) for r in db.execute("SELECT * FROM payments ORDER BY created_at DESC,rowid DESC")]
            return {"csrf": session["csrf"], "mode": self.mode, "live_enabled": self.live_enabled,
                "business_name": self.config.get("business_name", "My business"), "recipients": recipients, "payments": payments,
                "source_label": "Simulated business account" if self.mode == "simulator" else
                    "Configured live business bank" if self.live else "Configured Dwolla sandbox account"}
        if path == "/api/connection" and method == "GET":
            return self.connection_report()
        if path == "/api/recipients" and method == "POST":
            return self.add_recipient(data)
        if path == "/api/payments" and method == "POST":
            return self.draft(data)
        match = re.fullmatch(r"/api/payments/([0-9a-f-]{36})(?:/(submit|cancel|simulate|refresh|reconcile))?", path)
        if match:
            payment_id, action = match.groups()
            if method == "POST" and action:
                return self.action(payment_id, action, data)
            if method == "GET" and not action:
                with self.connection() as db:
                    payment = self.public_payment(self.payment(db, payment_id))
                    audit = [dict(r) for r in db.execute("SELECT action,detail,created_at FROM audit WHERE payment_id=? ORDER BY id", (payment_id,))]
                return {"payment": payment, "audit": audit}
        if path == "/api/export.csv" and method == "GET":
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["environment", "payment_id", "recipient", "reference", "amount_usd", "status", "created_utc"])
            def safe(value):
                value = str(value)
                return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
            with self.connection() as db:
                for row in db.execute("SELECT * FROM payments ORDER BY created_at,id"):
                    writer.writerow([self.mode, row["id"], safe(row["recipient_name"]), safe(row["reference"]),
                        f'{row["amount_cents"] // 100}.{row["amount_cents"] % 100:02d}', row["state"], row["created_at"]])
            return 200, output.getvalue().encode(), [("Content-Type", "text/csv; charset=utf-8"), ("Content-Disposition", 'attachment; filename="depositdesk-payments.csv"')]
        raise Problem(404, "Page not found.")

    def __call__(self, environ, start_response):
        try:
            result = self.handle(environ)
            if isinstance(result, tuple):
                status, body, headers = result
            else:
                status, body, headers = 200, json.dumps(result).encode(), [("Content-Type", "application/json")]
        except (Problem, ValueError, ProviderError, sqlite3.IntegrityError) as exc:
            status = exc.status if isinstance(exc, Problem) else (409 if isinstance(exc, sqlite3.IntegrityError) else 400)
            message = exc.message if isinstance(exc, Problem) else ("This recipient or payment already exists." if isinstance(exc, sqlite3.IntegrityError) else str(exc))
            body, headers = json.dumps({"error": message}).encode(), [("Content-Type", "application/json")]
        except Exception:
            status, body, headers = 500, b'{"error":"The request could not be completed. Reload before trying again."}', [("Content-Type", "application/json")]
        if self.secure:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        headers += [("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    """Local development server only. Deploy with the included gunicorn config."""
    daemon_threads = True
    request_queue_size = 32


def data_directory():
    return Path(os.environ.get("DEPOSITDESK_DATA_DIR", ROOT / "data"))


def load_config(directory):
    config_path = Path(directory) / "owner.json"
    if not config_path.exists():
        raise ValueError("No owner account yet. Run: python app.py init")
    config = json.loads(config_path.read_text())
    mode = os.environ.get("DEPOSITDESK_PROVIDER", "simulator")
    prefix = "DWOLLA_PRODUCTION_" if mode == "dwolla_production" else "DWOLLA_SANDBOX_"
    config.update({"provider": mode,
        "origin": os.environ.get("DEPOSITDESK_ORIGIN", "http://127.0.0.1:8765"),
        "behind_proxy": os.environ.get("DEPOSITDESK_BEHIND_PROXY", ""),
        "trusted_proxies": os.environ.get("DEPOSITDESK_TRUSTED_PROXIES", "127.0.0.1/32,::1/128"),
        "key": environment_secret(prefix + "KEY"), "secret": environment_secret(prefix + "SECRET"),
        "source_id": os.environ.get(prefix + "SOURCE_ID", ""),
        "account_id": os.environ.get(prefix + "ACCOUNT_ID", ""),
        "enable_live_payments": os.environ.get("DEPOSITDESK_ENABLE_LIVE_PAYMENTS", "false"),
        "webhook_secret": environment_secret(prefix + "WEBHOOK_SECRET")})
    return config


def environment_secret(name):
    """Prefer a mounted secret file. Never log credentials or copy them to JSON."""
    filename = os.environ.get(name + "_FILE")
    return Path(filename).read_text().rstrip("\r\n") if filename else os.environ.get(name, "")


def create_app(directory=None):
    os.umask(0o077)
    directory = Path(directory) if directory is not None else data_directory()
    return Desk(directory, load_config(directory))


def rotate_password(desk, password):
    record = json.dumps(password_record(password), sort_keys=True)
    with desk.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE metadata SET value=? WHERE key='owner_auth'", (record,))
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM login_attempts")
        desk.audit(db, None, "password_rotated", "Owner credential replaced; all sessions invalidated.")


def owner_password(confirm=True):
    secret_file = os.environ.get("DEPOSITDESK_OWNER_PASSWORD_FILE")
    if secret_file:
        return Path(secret_file).read_text().rstrip("\r\n")
    value = os.environ.get("DEPOSITDESK_OWNER_PASSWORD")
    if value:
        return value
    value = getpass.getpass("Owner password (14–256 characters): ")
    if confirm and value != getpass.getpass("Repeat password: "):
        raise ValueError("Passwords do not match.")
    return value


def write_config(path, config):
    fd, temporary = tempfile.mkstemp(prefix=".owner-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description="DepositDesk outgoing ACH payment register")
    parser.add_argument("command", nargs="?", choices=["run", "serve", "init", "passwd"], default="run")
    parser.add_argument("--bind", default="127.0.0.1:8765", help="Loopback address for local development")
    args = parser.parse_args()
    directory = data_directory()
    os.umask(0o077)
    try:
        if args.command == "init":
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / "owner.json"
            name = os.environ.get("DEPOSITDESK_BUSINESS_NAME") or (input("Business display name: ").strip() or "My business")
            config = {"business_name": text_value(name, "Business name", 80), "password": password_record(owner_password())}
            with path.open("x") as stream:
                json.dump(config, stream)
            create_app(directory)
            # SQLite owns the credential after bootstrap. A lost database must
            # be restored, never silently recreated from an obsolete password.
            write_config(path, {"business_name": config["business_name"]})
            print("Owner configured. Start locally with: python app.py")
            return
        desk = create_app(directory)
        if args.command == "passwd":
            rotate_password(desk, owner_password())
            if "password" in json.loads((directory / "owner.json").read_text()):
                write_config(directory / "owner.json", {"business_name": desk.config["business_name"]})
            print("Password changed immediately for all workers. All sessions signed out.")
            return
        host, separator, port = args.bind.rpartition(":")
        if not separator or host not in {"127.0.0.1", "localhost"} or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("The development server requires --bind 127.0.0.1:PORT. Use gunicorn for deployment.")
        if desk.secure:
            raise ValueError("Use gunicorn behind HTTPS for a secure origin; the bundled server is for local testing.")
        if (urlsplit(desk.origin).port or 80) != int(port):
            raise ValueError("DEPOSITDESK_ORIGIN must match the development server port.")
        print(f"DepositDesk [{desk.mode}]: {desk.origin} — local development, no live funds.", flush=True)
        with make_server(host, int(port), desk, server_class=ThreadedWSGIServer) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (ValueError, Problem, FileExistsError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
