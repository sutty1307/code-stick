"""Deployment decisions use a fake gcloud. No cloud resources or payments created.

Owner-bootstrap tests execute the actual bootstrap program against temporary
SQLite files, including after password rotation and interrupted initialization.
"""
import argparse
import copy
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import app
from deploy import google_cloud as cloud
from deploy import vm_setup as vm

ROOT = Path(__file__).resolve().parents[1]
IP = "34.123.45.67"
PROJECT = "depositdesk-test123"
BASE = "https://www.googleapis.com/compute/v1/projects/" + PROJECT
NETWORK = BASE + "/global/networks/depositdesk-net"
SUBNET = BASE + "/regions/us-central1/subnetworks/depositdesk-subnet"


class FakeCloud:
    def __init__(self):
        self.calls = []
        self.active = [{"account": "owner@example.com", "status": "ACTIVE"}]
        self.projects = []
        self.billing = [{"name": "billingAccounts/000000-AAAAAA-000000", "open": True}]
        self.billing_enabled = True
        self.instances = []
        self.disks = []
        common = {"description": cloud.MARKER}
        self.resources = {
            "depositdesk-net": {**common, "name": "depositdesk-net", "autoCreateSubnetworks": False, "selfLink": NETWORK},
            "depositdesk-subnet": {**common, "name": "depositdesk-subnet", "network": NETWORK, "selfLink": SUBNET,
                                  "region": "regions/us-central1", "ipCidrRange": "10.84.0.0/24"},
            "depositdesk-ip": {**common, "name": "depositdesk-ip", "address": IP, "addressType": "EXTERNAL", "region": "regions/us-central1"},
        }
        for name, ports, sources in (("depositdesk-web", ["80", "443"], "0.0.0.0/0"), ("depositdesk-iap", ["22"], "35.235.240.0/20")):
            self.resources[name] = {**common, "name": name, "network": NETWORK,
                "sourceRanges": [sources], "targetTags": ["depositdesk"], "direction": "INGRESS",
                "allowed": [{"IPProtocol": "tcp", "ports": ports}]}
        self.existing = set()

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ("auth", "list"):
            return self.active
        if args[:2] == ("projects", "list"):
            return self.projects
        if args[:3] == ("billing", "accounts", "list"):
            return self.billing
        if args[:3] == ("billing", "projects", "describe"):
            return {"billingEnabled": self.billing_enabled}
        if args[:3] == ("compute", "instances", "list"):
            return self.instances
        if args[:3] == ("compute", "disks", "list"):
            return self.disks
        if args[0] == "compute" and "list" in args:
            name = next(a.split("=", 2)[-1] for a in args if a.startswith("--filter=name="))
            return [copy.deepcopy(self.resources[name])] if name in self.existing else []
        if args[0] == "compute" and "create" in args and args[1] != "instances":
            name = args[args.index("create") + 1]
            self.existing.add(name)
            return [copy.deepcopy(self.resources[name])]
        return {}

    def ssh(self, *args, **kwargs):
        raise AssertionError("Unexpected SSH call")

    def mutations(self):
        return [args for args, _ in self.calls if any(word in args for word in ("create", "enable", "link", "scp", "delete", "update"))]


class CloudDecisionTests(unittest.TestCase):
    def exercise_install(self, ready):
        fake = FakeCloud()
        fake.projects = [{"projectId": PROJECT, "labels": {"depositdesk": "managed"}}]
        response = io.BytesIO(json.dumps({"status": "ready" if ready else "unavailable"}).encode())
        response.status = 200 if ready else 503
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(state=Path(folder) / "state.json", project=None, billing_account=None)
            cloud.save_state(args.state, {"project": PROJECT, "account": "owner@example.com", "https_verified": True})
            with patch.object(fake, "ssh", side_effect=lambda project, command, **kw: "/tmp/depositdesk-upload.ABC123xy\n" if command.startswith("umask") else "") as ssh, \
                    patch.object(cloud.urllib.request, "urlopen", return_value=response) as https, contextlib.redirect_stdout(io.StringIO()):
                if ready:
                    cloud.deploy(args, fake)
                else:
                    with self.assertRaisesRegex(cloud.SetupError, "HTTPS readiness"):
                        cloud.deploy(args, fake)
                state = json.loads(args.state.read_text())
                https.assert_called_once_with("https://" + IP + "/readyz", timeout=30)
                password_reads = [call for call in ssh.call_args_list if "initial-password" in call.args[1]]
                self.assertEqual(bool(password_reads), ready)
                self.assertEqual(bool(state.get("https_verified")), ready)
                self.assertEqual(state["project"], PROJECT)
                self.assertEqual(state["ip"], IP)
                scp = next(call for call, _ in fake.calls if call[:2] == ("compute", "scp"))
                self.assertIn("--tunnel-through-iap", scp)
        self.assertFalse(any(call[:2] == ("auth", "login") for call, _ in fake.calls))

    def test_install_coordinates_upload_and_https_before_showing_password(self):
        self.exercise_install(True)

    def test_failed_https_does_not_claim_success_or_discard_resource_state(self):
        self.exercise_install(False)

    def test_absent_signin_never_starts_login_or_changes_cloud(self):
        fake = FakeCloud()
        fake.active = []
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(state=Path(folder) / "state.json", project=None, billing_account=None)
            with self.assertRaisesRegex(cloud.SetupError, "Sign in yourself"):
                cloud.deploy(args, fake)
        self.assertEqual(fake.calls, [(("auth", "list", "--filter=status:ACTIVE"), {})])

    def test_missing_billing_does_not_create_an_empty_project(self):
        fake = FakeCloud()
        fake.billing = []
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(state=Path(folder) / "state.json", project=None, billing_account=None)
            with self.assertRaisesRegex(cloud.SetupError, "billing account"):
                cloud.deploy(args, fake)
            self.assertEqual(args.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(fake.mutations(), [])

    def test_unrelated_projects_are_never_reused(self):
        fake = FakeCloud()
        fake.projects = [{"projectId": PROJECT}]
        with self.assertRaisesRegex(cloud.SetupError, "not a dedicated"):
            cloud.choose_project(fake, {}, PROJECT, "owner@example.com")
        self.assertEqual(fake.mutations(), [])

    def test_account_change_or_missing_existing_project_is_not_recreated(self):
        fake = FakeCloud()
        for state in ({"account": "someoneelse@example.com"}, {"project": PROJECT, "project_created": True}, {"project": PROJECT, "ip": IP}):
            with self.subTest(state=state), self.assertRaises(cloud.SetupError):
                cloud.choose_project(fake, state, None, "owner@example.com")
        self.assertEqual(fake.mutations(), [])

    def test_recovers_managed_project_in_fresh_cloud_shell(self):
        fake = FakeCloud()
        fake.projects = [{"projectId": PROJECT, "labels": {"depositdesk": "managed"}}]
        self.assertEqual(cloud.choose_project(fake, {}, None, "owner@example.com"), (PROJECT, False))

    def test_multiple_billing_accounts_are_not_chosen_arbitrarily(self):
        fake = FakeCloud()
        fake.billing += [{"name": "billingAccounts/111111-BBBBBB-111111", "open": True}]
        with self.assertRaisesRegex(cloud.SetupError, "--billing-account"):
            cloud.choose_billing(fake, None)
        self.assertEqual(cloud.choose_billing(fake, "111111-BBBBBB-111111"), "111111-BBBBBB-111111")

    def test_new_host_retains_disk_and_has_no_google_identity_or_public_ssh(self):
        fake = FakeCloud()
        self.assertEqual(cloud.provision(fake, PROJECT), IP)
        command = next(args for args, _ in fake.calls if args[:3] == ("compute", "instances", "create"))
        for flag in ("--no-boot-disk-auto-delete", "--no-service-account", "--no-scopes", "--address=" + IP):
            self.assertIn(flag, command)
        ssh_rule = next(args for args, _ in fake.calls if args[:4] == ("compute", "firewall-rules", "create", "depositdesk-iap"))
        self.assertIn("--source-ranges=35.235.240.0/20", ssh_rule)
        self.assertIn("--allow=tcp:22", ssh_rule)
        self.assertTrue(all(kwargs.get("project") == PROJECT for args, kwargs in fake.calls))

    def test_existing_host_is_reused_without_recreation(self):
        fake = FakeCloud()
        fake.existing = set(fake.resources)
        fake.instances = [{"description": cloud.MARKER, "zone": "zones/us-central1-a", "status": "RUNNING",
            "networkInterfaces": [{"network": NETWORK, "subnetwork": SUBNET, "accessConfigs": [{"natIP": IP}]}],
            "tags": {"items": ["depositdesk"]}, "disks": [{"boot": True, "autoDelete": False}],
            "metadata": {"items": [{"key": "enable-oslogin", "value": "TRUE"}, {"key": "block-project-ssh-keys", "value": "TRUE"}]}}]
        self.assertEqual(cloud.provision(fake, PROJECT), IP)
        self.assertFalse(any("create" in args for args, _ in fake.calls))
        fake.instances[0]["disks"][0]["autoDelete"] = True
        with self.assertRaisesRegex(cloud.SetupError, "disk-retention"):
            cloud.provision(fake, PROJECT)

    def test_retained_database_disk_blocks_replacement_vm(self):
        fake = FakeCloud()
        fake.disks = [{"name": "depositdesk"}]
        with self.assertRaisesRegex(cloud.SetupError, "retained DepositDesk disk"):
            cloud.provision(fake, PROJECT)
        self.assertFalse(any(args[:3] == ("compute", "instances", "create") for args, _ in fake.calls))

    def test_changed_firewall_is_not_silently_accepted(self):
        fake = FakeCloud()
        fake.existing.add("depositdesk-iap")
        fake.resources["depositdesk-iap"]["sourceRanges"] = ["0.0.0.0/0"]
        with self.assertRaisesRegex(cloud.SetupError, "Firewall"):
            cloud.provision(fake, PROJECT)
        self.assertFalse(any(args[:3] == ("compute", "instances", "create") for args, _ in fake.calls))


class BundleTests(unittest.TestCase):
    def test_bundle_is_reproducible_and_excludes_credentials_and_databases(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__", ".venv"))
            (root / ".env").write_text("SECRET=do-not-upload")
            (root / "owner.json").write_text("do-not-upload")
            (root / "depositdesk.sqlite3").write_text("do-not-upload")
            first, second = Path(folder) / "one.tgz", Path(folder) / "two.tgz"
            first_hash = cloud.package_source(root, first)
            os.utime(root / "app.py", (1700000000, 1700000000))
            self.assertEqual(first_hash, cloud.package_source(root, second))
            with tarfile.open(first) as archive:
                self.assertEqual(set(archive.getnames()), vm.ALLOWED)
            stage = Path(folder) / "stage"
            stage.mkdir()
            vm.unpack(first, first_hash, stage)
            self.assertEqual((stage / "app.py").read_bytes(), (root / "app.py").read_bytes())

    def test_source_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "app.py").symlink_to(ROOT / "app.py")
            with self.assertRaisesRegex(cloud.SetupError, "linked"):
                cloud.package_source(root, root / "source.tgz")

    def test_tampered_and_traversing_bundles_write_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root / "source.tgz"
            digest = cloud.package_source(ROOT, bundle)
            stage = root / "stage"
            stage.mkdir()
            with self.assertRaisesRegex(vm.InstallError, "checksum"):
                vm.unpack(bundle, "0" * 64, stage)
            with tarfile.open(bundle, "w:gz") as archive:
                member = tarfile.TarInfo("../escape")
                member.size = 3
                archive.addfile(member, io.BytesIO(b"bad"))
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            with self.assertRaisesRegex(vm.InstallError, "unexpected"):
                vm.unpack(bundle, digest, stage)
            self.assertEqual(list(stage.iterdir()), [])
            self.assertFalse((root / "escape").exists())

    def test_expected_filename_cannot_hide_a_link(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root / "source.tgz"
            with tarfile.open(bundle, "w:gz") as archive:
                for name in vm.ALLOWED:
                    member = tarfile.TarInfo(name)
                    if name == "app.py":
                        member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    archive.addfile(member, io.BytesIO(b""))
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            stage = root / "stage"
            stage.mkdir()
            with self.assertRaisesRegex(vm.InstallError, "Linked"):
                vm.unpack(bundle, digest, stage)
            self.assertEqual(list(stage.iterdir()), [])


class OwnerInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data, self.etc, self.source = [self.root / name for name in ("data", "etc", "source")]

    def bootstrap(self, password="a fictional bootstrap password"):
        self.data.mkdir(exist_ok=True)
        env = {"PATH": os.defpath, "DEPOSITDESK_DATA_DIR": str(self.data),
               "DEPOSITDESK_PROVIDER": "simulator", "DEPOSITDESK_ORIGIN": "https://" + IP}
        return subprocess.run([sys.executable, "-c", vm.OWNER_BOOTSTRAP], input=password,
                              text=True, capture_output=True, cwd=ROOT, env=env)

    def test_interrupted_install_reuses_marker_and_password(self):
        initial = vm.claim_install(IP, "digest", self.etc, self.source, self.data)
        password = vm.initial_password(self.etc, self.data)
        self.assertEqual(vm.claim_install(IP, "digest", self.etc, self.source, self.data), initial)
        self.assertEqual(vm.initial_password(self.etc, self.data), password)
        self.assertEqual((self.etc / "initial-password").stat().st_mode & 0o777, 0o600)

    def test_existing_unmarked_data_is_preserved(self):
        self.data.mkdir()
        evidence = self.data / "depositdesk.sqlite3"
        evidence.write_bytes(b"saved payment records")
        with self.assertRaisesRegex(vm.InstallError, "No data was overwritten"):
            vm.claim_install(IP, "digest", self.etc, self.source, self.data)
        self.assertEqual(evidence.read_bytes(), b"saved payment records")
        self.assertFalse(self.etc.exists())

    def test_new_version_or_new_ip_cannot_overwrite_old_install(self):
        vm.claim_install(IP, "digest", self.etc, self.source, self.data)
        before = (self.etc / "deployment.json").read_bytes()
        for ip, digest in (("34.123.45.68", "digest"), (IP, "new-version")):
            with self.subTest(ip=ip, digest=digest), self.assertRaises(vm.InstallError):
                vm.claim_install(ip, digest, self.etc, self.source, self.data)
        self.assertEqual((self.etc / "deployment.json").read_bytes(), before)

    def test_real_bootstrap_survives_rerun_and_password_rotation(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        with patch.dict(os.environ, {"DEPOSITDESK_PROVIDER": "simulator", "DEPOSITDESK_ORIGIN": "https://" + IP}):
            desk = app.create_app(self.data)
            app.rotate_password(desk, "a new fictional owner password")
            with desk.connection() as db:
                before = db.execute("SELECT value FROM metadata WHERE key='owner_auth'").fetchone()[0]
                db.execute("INSERT INTO metadata VALUES('retained-evidence','keep me')")
        result = self.bootstrap("this must not replace the rotated credential")
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.data / "depositdesk.sqlite3") as db:
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='owner_auth'").fetchone()[0], before)
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='retained-evidence'").fetchone()[0], "keep me")
        self.assertNotIn("password", json.loads((self.data / "owner.json").read_text()))

    def test_lost_database_is_not_reinitialized_with_an_old_password(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        for path in self.data.glob("depositdesk.sqlite3*"):
            path.unlink()
        result = self.bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restore the database", result.stderr)
        self.assertFalse((self.data / "depositdesk.sqlite3").exists())

    def test_initialization_resumes_between_owner_file_and_database(self):
        self.data.mkdir()
        original = {"business_name": "Saved business", "password": app.password_record("original fictional owner secret")}
        (self.data / "owner.json").write_text(json.dumps(original))
        result = self.bootstrap("must not become the password")
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect(self.data / "depositdesk.sqlite3") as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM metadata WHERE key='owner_auth'").fetchone()[0]), original["password"])

    def test_generated_https_configuration_and_simulator_mode(self):
        env = vm.environment(IP)
        self.assertEqual(env["DEPOSITDESK_PROVIDER"], "simulator")
        self.assertEqual(env["DEPOSITDESK_ENABLE_LIVE_PAYMENTS"], "false")
        result = vm.nginx_config((ROOT / "nginx.conf").read_text(), IP)
        self.assertNotIn("pay.example.com", result)
        self.assertIn("server_name " + IP, result)
        self.assertIn("/live/depositdesk/fullchain.pem", result)
        self.assertIn("X-Forwarded-For $remote_addr", result)
        with self.assertRaises(vm.InstallError):
            vm.nginx_config("template", "34.123.45.67; malicious;")


if __name__ == "__main__":
    unittest.main()
