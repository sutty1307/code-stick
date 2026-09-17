"""Production contract tests. All credentials and bank records are fictional.
Every transport is mocked; this suite never contacts a financial provider.
"""
import copy
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from app import Desk, Problem, load_config, password_record
from connection_check import configuration_report
from providers import DwollaProduction, DwollaSandbox, ProviderError, PRODUCTION, SANDBOX, resource_url


def uid():
    return str(uuid.uuid4())


class LiveAdapterTests(unittest.TestCase):
    def setUp(self):
        self.source, self.destination, self.account, self.customer, self.transfer = [uid() for _ in range(5)]
        self.adapter = DwollaProduction("fictional-key", "fictional-secret", self.source, self.account, enabled=True)
        self.bank = {"id": self.source, "removed": False, "type": "bank", "status": "verified", "channels": ["ach"], "name": "Fictional source"}
        self.root = {"_links": {"account": {"href": resource_url("accounts", self.account, PRODUCTION)}}}
        self.payment = {"id": uid(), "source_ref": self.source, "destination_ref": self.destination, "amount_cents": 12345, "provider_ref": None}
        self.body = {"id": self.transfer, "status": "pending", "amount": {"currency": "USD", "value": "123.45"},
            "correlationId": self.payment["id"], "_links": {
                "source": {"href": resource_url("accounts", self.account, PRODUCTION)},
                "destination": {"href": resource_url("customers", self.customer, PRODUCTION)},
                "source-funding-source": {"href": resource_url("funding-sources", self.source, PRODUCTION)},
                "destination-funding-source": {"href": resource_url("funding-sources", self.destination, PRODUCTION)}}}

    def source_responses(self, bank=None, root=None):
        self.adapter.request = Mock(side_effect=[(200, {}, root or self.root),
            (200, {}, {"_embedded": {"funding-sources": [bank or self.bank]}})])

    def test_live_and_sandbox_cannot_swap_hosts(self):
        for base in [SANDBOX, PRODUCTION + "/", PRODUCTION + ":443", "https://api.dwolla.com.evil.invalid"]:
            with self.subTest(base=base), self.assertRaises(ValueError):
                DwollaProduction("key", "secret", self.source, self.account, base=base)
        with self.assertRaises(ValueError):
            DwollaSandbox("key", "secret", self.source, base=PRODUCTION)

    def test_sandbox_recipient_self_only_link_requires_ownership_listing(self):
        adapter = DwollaSandbox("key", "secret", self.source)
        bank = {**self.bank, "id": self.destination, "_links": {"self": {"href": resource_url("funding-sources", self.destination)}}}
        adapter.request = Mock(side_effect=[(200, {}, bank), (200, {}, {"_embedded": {"funding-sources": [bank]}})])
        self.assertEqual(adapter.validate_recipient(self.customer, self.destination), self.destination)
        self.assertEqual(adapter.request.call_count, 2)
        adapter.request = Mock(side_effect=[(200, {}, bank), (200, {}, {"_embedded": {"funding-sources": []}})])
        with self.assertRaises(ProviderError): adapter.validate_recipient(self.customer, self.destination)

    def test_live_disabled_by_default_before_submission_network(self):
        adapter = DwollaProduction("key", "secret", self.source, self.account)
        adapter.request = Mock(side_effect=AssertionError("No network permitted"))
        with self.assertRaises(ProviderError): adapter.submit(self.payment)
        adapter.request.assert_not_called()

    def test_transport_also_blocks_disabled_transfer_post(self):
        self.adapter.enabled = False
        self.adapter.http.open = Mock(side_effect=AssertionError("No transport permitted"))
        with self.assertRaises(ProviderError): self.adapter._exchange("/transfers", "POST", b"{}")
        self.adapter.http.open.assert_not_called()

    def test_source_belongs_to_authenticated_main_account(self):
        self.source_responses()
        self.assertEqual(self.adapter.check_source(self.source)["id"], self.source)
        self.assertEqual(self.adapter.request.call_args_list[0].args, ("/",))
        self.assertEqual(self.adapter.request.call_args_list[1].args, (f"/accounts/{self.account}/funding-sources?removed=false",))

    def test_wrong_account_cannot_validate_source(self):
        self.source_responses(root={"_links": {"account": {"href": resource_url("accounts", uid(), PRODUCTION)}}})
        with self.assertRaises(ProviderError): self.adapter.check_source(self.source)
        self.assertEqual(self.adapter.request.call_count, 1)

    def test_unavailable_unverified_wrong_channel_or_other_bank_rejected(self):
        for key, value in [("removed", True), ("status", "unverified"), ("type", "balance"), ("channels", ["wire"]), ("id", uid())]:
            with self.subTest(key=key):
                self.source_responses(bank={**self.bank, key: value})
                with self.assertRaises(ProviderError): self.adapter.check_source(self.source)

    def test_recipient_ownership_is_verified_without_requiring_payer_verification(self):
        bank = {**self.bank, "id": self.destination, "status": "unverified"}
        self.adapter.request = Mock(return_value=(200, {}, {"_embedded": {"funding-sources": [bank]}}))
        self.assertEqual(self.adapter.validate_recipient(self.customer, self.destination), self.destination)
        self.adapter.request.assert_called_once_with(f"/customers/{self.customer}/funding-sources?removed=false")

    def test_missing_duplicate_or_malformed_owner_bank_rejected(self):
        for banks in [[], [self.bank, self.bank], "not-a-list", [None], [{**self.bank, "channels": "ach"}]]:
            self.adapter.request = Mock(return_value=(200, {}, {"_embedded": {"funding-sources": banks}}))
            with self.subTest(banks=banks), self.assertRaises(ProviderError):
                self.adapter._owned_bank("accounts", self.account, self.source, True)

    def test_submission_uses_live_urls_exact_cents_and_stable_id(self):
        self.adapter.request = Mock(return_value=(201, {"Location": resource_url("transfers", self.transfer, PRODUCTION)}, {}))
        self.assertEqual(self.adapter.submit(self.payment), self.transfer)
        path, method, payload, key = self.adapter.request.call_args.args
        self.assertEqual((path, method, key), ("/transfers", "POST", self.payment["id"]))
        self.assertEqual(payload["amount"], {"currency": "USD", "value": "123.45"})
        self.assertEqual(payload["_links"]["destination"]["href"], resource_url("funding-sources", self.destination, PRODUCTION))
        self.assertEqual(payload["correlationId"], self.payment["id"])
        self.assertNotIn("achDetails", payload)  # No misleading statement labels.

    def test_sandbox_location_rejected_by_live_adapter(self):
        self.adapter.request = Mock(return_value=(201, {"Location": resource_url("transfers", self.transfer)}, {}))
        with self.assertRaises(ProviderError): self.adapter.submit(self.payment)

    def test_real_documented_party_and_funding_links_match(self):
        self.adapter.request = Mock(return_value=(200, {}, self.body))
        self.assertEqual(self.adapter.verify_transfer(self.payment, self.transfer), "pending")

    def test_malformed_explicit_bank_link_never_falls_back(self):
        self.body["_links"]["source"] = {"href": resource_url("funding-sources", self.source, PRODUCTION)}
        for entry in [None, {}, {"href": resource_url("funding-sources", uid(), PRODUCTION)}]:
            self.body["_links"]["source-funding-source"] = entry
            self.adapter.request = Mock(return_value=(200, {}, self.body))
            with self.subTest(entry=entry), self.assertRaises(ProviderError):
                self.adapter.verify_transfer(self.payment, self.transfer)

    def test_wrong_identity_currency_amount_and_environment_rejected(self):
        variants = [("id", uid()), ("amount", {"currency": "EUR", "value": "123.45"}),
            ("amount", {"currency": "USD", "value": "123.46"}), ("correlationId", uid())]
        for key, value in variants:
            body = copy.deepcopy(self.body); body[key] = value
            self.adapter.request = Mock(return_value=(200, {}, body))
            with self.subTest(key=key), self.assertRaises(ProviderError): self.adapter.verify_transfer(self.payment, self.transfer)
        self.body["_links"]["destination-funding-source"]["href"] = resource_url("funding-sources", self.destination)
        self.adapter.request = Mock(return_value=(200, {}, self.body))
        with self.assertRaises(ProviderError): self.adapter.verify_transfer(self.payment, self.transfer)

    def test_transport_is_fixed_to_production_and_sends_no_secrets_to_other_hosts(self):
        response = Mock(status=200, headers={})
        response.read.return_value = b"{}"
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        self.adapter.http.open = Mock(return_value=response)
        self.adapter._exchange("/")
        self.assertEqual(self.adapter.http.open.call_args.args[0].full_url, PRODUCTION + "/")
        for path in ["//evil.invalid", "/transfers/../customers", "/token?redirect=evil", "/accounts/" + self.account + "/funding-sources?removed=false&evil=1"]:
            with self.subTest(path=path), self.assertRaises(ProviderError): self.adapter._exchange(path)
        self.assertEqual(self.adapter.http.open.call_count, 1)


class LiveApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth = password_record("Fictional test password 456!")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = {"password": self.auth, "business_name": "Fictional business", "provider": "dwolla_production",
            "origin": "https://payments.example.test", "source_id": uid(), "account_id": uid(),
            "key": "fictional-key", "secret": "fictional-secret", "webhook_secret": "fictional-hook-secret-123",
            "enable_live_payments": False}
        self.provider = Mock()
        self.provider.check_source.return_value = {"name": "Fictional bank", "private_field": "must not leak"}
        self.provider.validate_recipient.side_effect = lambda customer, funding: funding
        self.provider.submit.return_value = uid()
        self.app = Desk(self.temp.name, self.config, self.provider)

    def draft(self):
        recipient = self.app.add_recipient({"name": "Sample vendor", "kind": "vendor", "account_label": "Test bank",
            "authorization_ref": "AUTH-TEST-001", "acknowledged": True, "customer_id": uid(), "funding_id": uid()})
        return self.app.draft({"request_id": uid(), "recipient_id": recipient["id"], "amount": "123.45", "reference": "INV-001"})

    def activate(self):
        self.config["enable_live_payments"] = True
        self.app = Desk(self.temp.name, self.config, self.provider)

    def test_live_requires_https_and_subscription_secret(self):
        for changes in [{"origin": "http://localhost:8765"}, {"webhook_secret": ""}, {"account_id": "bad"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Desk(self.temp.name, {**self.config, **changes}, self.provider)

    def test_credentials_are_required_without_mock_adapter(self):
        with self.assertRaises(ValueError): Desk(self.temp.name, {**self.config, "key": ""})

    def test_live_owner_lock_is_global_across_ips(self):
        for number in range(10):
            with self.assertRaises(Problem): self.app.login("wrong", f"192.0.2.{number + 1}")
        with self.assertRaises(Problem) as failure:
            self.app.login("Fictional test password 456!", "198.51.100.1")
        self.assertEqual(failure.exception.status, 429)
        self.assertIn("30 minutes", str(failure.exception))

    def test_disabled_release_leaves_draft_and_no_provider_submission(self):
        payment = self.draft()
        with self.assertRaises(Problem): self.app.submit(payment["id"], {"authorized": True, "amount": "123.45", "live_confirmed": True})
        self.provider.submit.assert_not_called()
        with self.app.connection() as db: self.assertEqual(self.app.payment(db, payment["id"])["state"], "draft")

    def test_explicit_live_confirmation_required(self):
        self.activate(); payment = self.draft()
        with self.assertRaises(Problem): self.app.submit(payment["id"], {"authorized": True, "amount": "123.45"})
        self.provider.submit.assert_not_called()

    def test_repeated_live_release_submits_once(self):
        self.activate(); payment = self.draft()
        for _ in range(3):
            self.assertEqual(self.app.submit(payment["id"], {"authorized": True, "amount": "123.45", "live_confirmed": True})["state"], "pending")
        self.assertEqual(self.provider.submit.call_count, 1)

    def test_connection_check_works_with_release_disabled_and_redacts_provider_fields(self):
        report = self.app.connection_report()
        self.assertTrue(report["connected"]); self.assertFalse(report["live_enabled"])
        self.assertFalse(report["transfer_submitted"])
        self.assertNotIn("must not leak", json.dumps(report))
        self.provider.submit.assert_not_called()

    def test_connection_failure_is_not_mislabeled_connected(self):
        self.provider.check_source.side_effect = ProviderError("Not verified")
        report = self.app.connection_report()
        self.assertFalse(report["connected"])
        self.assertEqual(report["message"], "Not verified")

    def test_connection_endpoint_requires_owner_authentication(self):
        result = {}
        self.app({"PATH_INFO": "/api/connection", "REQUEST_METHOD": "GET", "HTTP_HOST": "payments.example.test"},
            lambda status, headers: result.update(status=status))
        self.assertTrue(result["status"].startswith("401"))
        self.provider.check_source.assert_not_called()

    def test_live_database_cannot_change_account_source_or_environment(self):
        self.draft()
        for changes in [{"account_id": uid()}, {"source_id": uid()}, {"provider": "dwolla_sandbox"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Desk(self.temp.name, {**self.config, **changes}, self.provider)

    def test_live_ambiguous_submission_cannot_be_resubmitted(self):
        self.activate(); payment = self.draft()
        self.provider.submit.side_effect = ProviderError("Unconfirmed outcome")
        for _ in range(2):
            self.assertEqual(self.app.submit(payment["id"], {"authorized": True, "amount": "123.45", "live_confirmed": True})["state"], "needs_review")
        self.assertEqual(self.provider.submit.call_count, 1)

    def test_live_webhooks_require_live_resource_and_valid_signature(self):
        self.activate(); payment = self.draft()
        payment = self.app.submit(payment["id"], {"authorized": True, "amount": "123.45", "live_confirmed": True})
        def event(base):
            raw = json.dumps({"id": uid(), "topic": "transfer_completed", "_links": {"resource": {"href": resource_url("transfers", payment["provider_ref"], base)}}}).encode()
            return raw, hmac.new(self.config["webhook_secret"].encode(), raw, hashlib.sha256).hexdigest()
        with self.assertRaises(ProviderError): self.app.webhook(*event(SANDBOX))
        self.app.webhook(*event(PRODUCTION))
        with self.app.connection() as db: self.assertEqual(self.app.payment(db, payment["id"])["refresh_due"], 1)

    def test_simulation_forbidden_in_production(self):
        payment = self.draft()
        with self.assertRaises(Problem): self.app.action(payment["id"], "simulate", {"state": "processed"})

    def test_config_never_falls_back_from_live_to_sandbox_credentials(self):
        Path(self.temp.name, "owner.json").write_text(json.dumps({"business_name": "Test"}))
        with patch.dict(os.environ, {"DEPOSITDESK_PROVIDER": "dwolla_production", "DWOLLA_SANDBOX_KEY": "sandbox-only-key", "DWOLLA_SANDBOX_SECRET": "sandbox-only-secret"}, clear=True):
            config = load_config(self.temp.name)
            self.assertEqual(config["key"], ""); self.assertEqual(config["secret"], "")
            self.assertEqual(config["enable_live_payments"], "false")

    def test_production_secrets_can_be_mounted_files(self):
        Path(self.temp.name, "owner.json").write_text('{"business_name":"Test"}')
        secret = Path(self.temp.name, "fake-key"); secret.write_text("fictional-mounted-key\n")
        with patch.dict(os.environ, {"DEPOSITDESK_PROVIDER": "dwolla_production", "DWOLLA_PRODUCTION_KEY_FILE": str(secret)}, clear=True):
            self.assertEqual(load_config(self.temp.name)["key"], "fictional-mounted-key")

    def test_config_report_lists_missing_names_without_exposing_values(self):
        with patch.dict(os.environ, {"DEPOSITDESK_PROVIDER": "dwolla_production", "DWOLLA_PRODUCTION_KEY": "never-echo-this"}, clear=True):
            report = configuration_report()
            self.assertIn("DWOLLA_PRODUCTION_SECRET", report["missing_settings"])
            self.assertNotIn("never-echo-this", json.dumps(report))
            self.assertFalse(report["network_contacted"])


if __name__ == "__main__":
    unittest.main()
