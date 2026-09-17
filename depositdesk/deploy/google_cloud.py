"""Run from an authenticated Google Cloud Shell. No Google login is automated.

Without --deploy this only prints the resource plan. --deploy creates billable
resources in a dedicated project; it never submits a payment.
"""
import argparse
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REGION, ZONE = "us-central1", "us-central1-a"
MARKER = "Managed by DepositDesk installer v1"
FILES = ("app.py", "providers.py", "wsgi.py", "gunicorn.conf.py", "backup.py",
         "connection_check.py", "requirements.txt", "nginx.conf", "depositdesk.service")
ASSETS = ("index.html", "app.js", "styles.css", "favicon.svg")
PLAN = """DepositDesk Google Cloud installation
  Dedicated project (created automatically if none is saved)
  One e2-micro VM in us-central1-a; 30 GB persistent standard boot disk
  One static public IPv4 address with an automatically renewed HTTPS certificate
  Web ports 80/443; SSH only through Google's Identity-Aware Proxy
  No service account attached to the VM; no bank credentials in VM metadata
  Owner password generated on the server; simulator mode until bank activation

Google bills the VM, disk, IP address and network use. Free-tier credits depend
on your account. Stopping the VM does not stop disk or reserved-IP charges.
This installer creates hosting. It does not enroll you with a payment processor.
"""


class SetupError(RuntimeError):
    pass


def project_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", value):
        raise SetupError("Invalid Google Cloud project ID.")
    return value


def public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise SetupError("Google did not return a valid public IPv4 address.") from exc
    if ip.version != 4 or not ip.is_global:
        raise SetupError("Google did not return a public IPv4 address.")
    return str(ip)


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".deployment-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def package_source(root, destination):
    # Allowlist only. Local databases, .env files, credentials and test fixtures
    # are never copied, even if present in the checkout.
    names = list(FILES) + ["static/" + n for n in ASSETS]
    # Reproducible bytes are necessary for interrupted-install recovery across
    # separate Cloud Shell sessions/checkouts (timestamps and UIDs vary).
    with destination.open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name in names:
                path = root / name
                if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
                    raise SetupError("Missing or linked application file: " + name)
                member = tarfile.TarInfo(name)
                member.size, member.mode, member.mtime = path.stat().st_size, 0o644, 0
                with path.open("rb") as stream:
                    archive.addfile(member, stream)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


class GCloud:
    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def call(self, *args, project=None, stream=False, timeout=600):
        command = ["gcloud", *args, "--quiet"]
        if project:
            command += ["--project=" + project_id(project)]
        if not stream:
            command += ["--format=json"]
        result = self.runner(command, text=True, capture_output=not stream, timeout=timeout)
        if result.returncode:
            detail = (getattr(result, "stderr", "") or "See the Google Cloud error above.").strip()
            raise SetupError(detail)
        if stream:
            return None
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise SetupError("Google returned an unexpected response; no retry was made.") from exc

    def ssh(self, project, command, stream=True, timeout=600):
        # SSH output is application output, not gcloud JSON.
        args = ["gcloud", "compute", "ssh", "depositdesk", "--project=" + project_id(project),
                "--zone=" + ZONE, "--tunnel-through-iap", "--quiet",
                "--ssh-flag=-o ConnectTimeout=10", "--command=" + command]
        result = self.runner(args, text=True, capture_output=not stream, timeout=timeout)
        if result.returncode:
            raise SetupError((getattr(result, "stderr", "") or "SSH command failed.").strip())
        return result.stdout if not stream else ""


def choose_project(cloud, state, requested, account):
    if state.get("account") and state["account"] != account:
        raise SetupError("The saved deployment belongs to another Google account. Use that account.")
    available = cloud.call("projects", "list", "--filter=lifecycleState:ACTIVE")
    chosen = requested or state.get("project")
    if chosen:
        chosen = project_id(chosen)
        matches = [p for p in available if p["projectId"] == chosen]
        if matches:
            if matches[0].get("labels", {}).get("depositdesk") != "managed":
                raise SetupError("That project is not a dedicated DepositDesk project. No resources changed.")
            return chosen, False
        if requested:
            raise SetupError("The requested project is not accessible. No replacement project was created.")
        if state.get("project_created") or state.get("ip"):
            raise SetupError("The saved project is no longer accessible. No replacement was created; recover access to the original project.")
        # A saved but not-yet-created project can be resumed after a failed first run.
        return chosen, True
    managed = [p["projectId"] for p in available if p.get("labels", {}).get("depositdesk") == "managed"]
    if len(managed) > 1:
        raise SetupError("Several DepositDesk projects exist. Re-run with --project and one of: " + ", ".join(managed))
    return (managed[0], False) if managed else ("depositdesk-" + secrets.token_hex(5), True)


def choose_billing(cloud, specified):
    accounts = cloud.call("billing", "accounts", "list", "--filter=open=true")
    if specified:
        matches = [a for a in accounts if a["name"].rsplit("/", 1)[-1] == specified]
        if len(matches) != 1:
            raise SetupError("That billing account is not accessible or open.")
        return specified
    if not accounts:
        raise SetupError("Google requires a billing account: https://console.cloud.google.com/billing")
    if len(accounts) != 1:
        labels = [a.get("displayName", "Billing account") + " (" + a["name"].rsplit("/", 1)[-1] + ")" for a in accounts]
        raise SetupError("Choose the billing account with --billing-account: " + "; ".join(labels))
    return accounts[0]["name"].rsplit("/", 1)[-1]


def ensure_resource(cloud, project, group, name, flags=(), region=False):
    listing = cloud.call("compute", *group, "list", "--filter=name=" + name, project=project)
    matches = [x for x in listing if x.get("name") == name]
    if matches:
        if len(matches) != 1 or matches[0].get("description") != MARKER:
            raise SetupError("Refusing to reuse an unrelated resource: " + name)
        return matches[0]
    args = ["compute", *group, "create", name, "--description=" + MARKER, *flags]
    if region:
        args.append("--region=" + REGION)
    created = cloud.call(*args, project=project)
    return created[0] if isinstance(created, list) else created


def provision(cloud, project):
    cloud.call("services", "enable", "compute.googleapis.com", "iap.googleapis.com",
               "oslogin.googleapis.com", project=project)
    network = ensure_resource(cloud, project, ("networks",), "depositdesk-net", ("--subnet-mode=custom",))
    if network.get("autoCreateSubnetworks") is not False:
        raise SetupError("The existing network is not the expected custom network.")
    subnet = ensure_resource(cloud, project, ("networks", "subnets"), "depositdesk-subnet",
                             ("--network=depositdesk-net", "--range=10.84.0.0/24"), region=True)
    if (subnet.get("region", "").rsplit("/", 1)[-1] != REGION
            or subnet.get("network") != network.get("selfLink") or subnet.get("ipCidrRange") != "10.84.0.0/24"):
        raise SetupError("The existing subnet differs from the intended network or region.")
    for name, rules, sources in (("depositdesk-web", "tcp:80,tcp:443", "0.0.0.0/0"),
                                  ("depositdesk-iap", "tcp:22", "35.235.240.0/20")):
        rule = ensure_resource(cloud, project, ("firewall-rules",), name,
            ("--network=depositdesk-net", "--direction=INGRESS", "--allow=" + rules,
             "--source-ranges=" + sources, "--target-tags=depositdesk"))
        wanted_ports = {"80", "443"} if name.endswith("web") else {"22"}
        allowed = rule.get("allowed", [])
        actual_ports = {p for a in allowed for p in a.get("ports", [])}
        if (rule.get("sourceRanges") != [sources] or rule.get("targetTags") != ["depositdesk"]
                or rule.get("network") != network.get("selfLink")
                or rule.get("direction") != "INGRESS" or rule.get("disabled", False)
                or any(a.get("IPProtocol") != "tcp" for a in allowed)
                or actual_ports != wanted_ports):
            raise SetupError("Firewall configuration differs from the intended setup: " + name)
    address = ensure_resource(cloud, project, ("addresses",), "depositdesk-ip", region=True)
    if (address.get("region", "").rsplit("/", 1)[-1] != REGION
            or address.get("addressType") != "EXTERNAL"):
        raise SetupError("The existing IP address has an unexpected region or type.")
    ip = public_ip(address["address"])
    instances = cloud.call("compute", "instances", "list", "--filter=name=depositdesk", project=project)
    if instances:
        instance = instances[0]
        if (len(instances) != 1 or instance.get("description") != MARKER
                or instance.get("zone", "").rsplit("/", 1)[-1] != ZONE):
            raise SetupError("An unrelated or differently located VM exists. It was not changed.")
        interfaces = instance.get("networkInterfaces", [])
        if (len(interfaces) != 1 or interfaces[0].get("network") != network.get("selfLink")
                or interfaces[0].get("subnetwork") != subnet.get("selfLink")
                or not any(c.get("natIP") == ip for c in interfaces[0].get("accessConfigs", []))):
            raise SetupError("The saved VM's IP differs. No bank origin or database was changed.")
        metadata = {x["key"]: x["value"] for x in instance.get("metadata", {}).get("items", [])}
        boot_disks = [d for d in instance.get("disks", []) if d.get("boot")]
        if (instance.get("serviceAccounts") or instance.get("tags", {}).get("items") != ["depositdesk"]
                or metadata.get("enable-oslogin", "").upper() != "TRUE"
                or metadata.get("block-project-ssh-keys", "").upper() != "TRUE"
                or len(boot_disks) != 1 or boot_disks[0].get("autoDelete") is not False):
            raise SetupError("The existing VM's access or disk-retention settings changed. They were not overwritten.")
        if instance.get("status") != "RUNNING":
            raise SetupError("The existing VM is stopped. Start it in Google Cloud before resuming.")
    else:
        # Never silently replace a deleted VM when its retained disk may contain payments.
        disks = cloud.call("compute", "disks", "list", "--filter=name=depositdesk", project=project)
        if disks:
            raise SetupError("A retained DepositDesk disk exists. Restore that VM instead of creating a fresh database.")
        cloud.call("compute", "instances", "create", "depositdesk", "--zone=" + ZONE,
            "--machine-type=e2-micro", "--image-project=debian-cloud", "--image-family=debian-12",
            "--boot-disk-size=30GB", "--boot-disk-type=pd-standard", "--no-boot-disk-auto-delete",
            "--network=depositdesk-net", "--subnet=depositdesk-subnet", "--address=" + ip,
            "--tags=depositdesk", "--labels=depositdesk=managed", "--description=" + MARKER,
            "--metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE", "--shielded-secure-boot",
            "--no-service-account", "--no-scopes", project=project)
    return ip


def deploy(args, cloud=None):
    cloud = cloud or GCloud()
    active = cloud.call("auth", "list", "--filter=status:ACTIVE")
    if len(active) != 1:
        raise SetupError("Sign in yourself in this Cloud Shell with: gcloud auth login\nThen run the installer again.")
    account = active[0]["account"]
    state_path = args.state
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    project, create = choose_project(cloud, state, args.project, account)
    if state.get("project") and state["project"] != project:
        raise SetupError("Use a separate --state file for a different deployment.")
    state.update({"project": project, "account": account, "zone": ZONE, "https_verified": False})
    save_state(state_path, state)
    if create:
        # Check billing first, so a missing account creates no empty cloud project.
        billing = choose_billing(cloud, args.billing_account)
        print("Creating dedicated project:", project, flush=True)
        cloud.call("projects", "create", project, "--name=DepositDesk", "--labels=depositdesk=managed")
        state["project_created"] = True
        save_state(state_path, state)
        cloud.call("billing", "projects", "link", project, "--billing-account=" + billing)
    else:
        billing_info = cloud.call("billing", "projects", "describe", project)
        if not billing_info.get("billingEnabled"):
            billing = choose_billing(cloud, args.billing_account)
            cloud.call("billing", "projects", "link", project, "--billing-account=" + billing)
    state["project_created"] = True
    save_state(state_path, state)
    print("Configuring persistent hosting and network rules...", flush=True)
    ip = provision(cloud, project)
    state["ip"] = ip
    save_state(state_path, state)
    print("Waiting for the VM's Google-managed SSH access...", flush=True)
    last_error = None
    for attempt in range(18):
        try:
            cloud.ssh(project, "true", stream=False, timeout=30)
            break
        except (SetupError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if attempt == 17:
                raise SetupError("VM exists, but IAP/OS Login is not ready: " + str(last_error))
            time.sleep(5)
    remote = cloud.ssh(project, "umask 077\nmktemp -d /tmp/depositdesk-upload.XXXXXXXX", stream=False).strip()
    if not re.fullmatch(r"/tmp/depositdesk-upload\.[A-Za-z0-9]{8}", remote):
        raise SetupError("Unexpected upload directory; upload stopped.")
    with tempfile.TemporaryDirectory(prefix="depositdesk-build-") as temporary:
        archive = Path(temporary) / "source.tgz"
        digest = package_source(ROOT, archive)
        cloud.call("compute", "scp", str(archive), str(ROOT / "deploy" / "vm_setup.py"),
            "depositdesk:" + remote + "/", "--zone=" + ZONE, "--tunnel-through-iap",
            project=project, stream=True)
        command = shlex.join(["sudo", "python3", remote + "/vm_setup.py", "--archive",
                              remote + "/source.tgz", "--sha256", digest, "--ip", ip])
        cloud.ssh(project, command, timeout=1800)
    # HTTPS verification uses the default trust store. Never bypass certificate validation.
    with urllib.request.urlopen("https://" + ip + "/readyz", timeout=30) as response:
        if response.status != 200 or json.load(response).get("status") != "ready":
            raise SetupError("Public HTTPS readiness verification failed.")
    state["https_verified"] = True
    save_state(state_path, state)
    print("\nHosting is responding at https://" + ip)
    print("New installations use SIMULATOR mode. This installer sends no payment instructions.")
    print("If retained, your initial owner password follows. Save it privately; do not paste it into chat.")
    cloud.ssh(project, "sudo sh -c 'if [ -f /etc/depositdesk/initial-password ]; then cat /etc/depositdesk/initial-password; else echo \"Initial password removed. Use your current password or the documented reset command.\"; fi'")
    print("\nProject:", project)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy", action="store_true")
    parser.add_argument("--project")
    parser.add_argument("--billing-account")
    parser.add_argument("--state", type=Path, default=Path.home() / ".config/depositdesk/cloud.json")
    args = parser.parse_args()
    print(PLAN)
    if not args.deploy:
        print("To install from your signed-in Cloud Shell: python3 deploy/google_cloud.py --deploy")
        return
    if not shutil.which("gcloud"):
        parser.error("Run this in Google Cloud Shell, which includes gcloud.")
    try:
        os.umask(0o077)
        deploy(args)
    except (SetupError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print("\nSetup stopped:", exc, file=sys.stderr)
        print("Saved resources and records were not deleted. Re-run after resolving the reported issue.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
