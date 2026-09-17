"""Installer for the dedicated Debian VM. Invoked by google_cloud.py over IAP."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

APP = Path("/opt/depositdesk")
DATA = Path("/var/lib/depositdesk")
ETC = Path("/etc/depositdesk")
CERTBOT = Path("/opt/depositdesk-certbot/bin/certbot")
INSTALLER = "depositdesk-v1"
ALLOWED = {
    "app.py", "providers.py", "wsgi.py", "gunicorn.conf.py", "backup.py",
    "connection_check.py", "requirements.txt", "nginx.conf", "depositdesk.service",
    "static/index.html", "static/app.js", "static/styles.css", "static/favicon.svg",
}


class InstallError(RuntimeError):
    pass


def run(*command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


def atomic_write(path, content, mode=0o600):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".install-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        raise InstallError("Invalid public IP.") from exc
    if ip.version != 4 or not ip.is_global:
        raise InstallError("A public IPv4 address is required.")
    return str(ip)


def unpack(archive_path, expected_hash, destination):
    """Validate the entire bundle before writing; never use tar.extractall."""
    if archive_path.stat().st_size > 5_000_000:
        raise InstallError("Application bundle exceeds the size limit.")
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != expected_hash:
        raise InstallError("Application bundle checksum mismatch.")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if set(names) != ALLOWED or len(names) != len(ALLOWED):
            raise InstallError("Application bundle has missing, duplicate or unexpected files.")
        if any(not member.isfile() or member.size > 1_000_000 for member in members):
            raise InstallError("Linked or oversized application file refused.")
        for member in members:
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            # Destination is a newly-created, private staging directory.
            with archive.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o644)


def claim_install(ip, digest, etc=ETC, app=APP, data=DATA):
    marker = etc / "deployment.json"
    if marker.exists():
        state = json.loads(marker.read_text())
        if state.get("installer") != INSTALLER or state.get("ip") != ip:
            raise InstallError("Existing installation has a different identity or IP; no configuration was replaced.")
        if state.get("sha256") != digest:
            raise InstallError("This is a different application version. Use the documented backup-and-upgrade procedure.")
        return state
    if app.exists() or data.exists() or (etc.exists() and any(etc.iterdir())):
        raise InstallError("Existing application files found without this installer's marker. No data was overwritten.")
    etc.mkdir(parents=True, exist_ok=True, mode=0o750)
    state = {"installer": INSTALLER, "ip": ip, "sha256": digest, "status": "installing"}
    atomic_write(marker, json.dumps(state))
    return state


def environment(ip):
    return {
        "DEPOSITDESK_ORIGIN": "https://" + validate_ip(ip),
        "DEPOSITDESK_PROVIDER": "simulator",
        "DEPOSITDESK_ENABLE_LIVE_PAYMENTS": "false",
        "DEPOSITDESK_BEHIND_PROXY": "true",
        "DEPOSITDESK_TRUSTED_PROXIES": "127.0.0.1/32,::1/128",
        "DEPOSITDESK_DATA_DIR": str(DATA),
    }


def initial_password(etc=ETC, data=DATA):
    path = etc / "initial-password"
    if path.exists():
        return path.read_text().strip()
    if (data / "owner.json").exists() or (data / "depositdesk.sqlite3").exists():
        raise InstallError("Owner records already exist. Their password was not replaced.")
    password = secrets.token_urlsafe(24)
    # Atomic installation marker/lock ensures only one bootstrap can run.
    atomic_write(path, password + "\n")
    return password


# Runs as the service user. The generated secret travels on stdin, never argv,
# metadata, an environment file, or service logs. An interrupted initialization
# resumes without rotating the owner credential or replacing an existing DB.
OWNER_BOOTSTRAP = r'''
import json
import sys
from pathlib import Path
import app

directory = app.data_directory()
owner = directory / "owner.json"
database = directory / "depositdesk.sqlite3"
if not owner.exists():
    if database.exists():
        raise RuntimeError("Database found without owner configuration. Restore; do not initialize again.")
    record = {"business_name": "My business", "password": app.password_record(sys.stdin.read().strip())}
    app.write_config(owner, record)
elif not database.exists() and "password" not in json.loads(owner.read_text()):
    raise RuntimeError("Owner exists but its database is missing. Restore the database.")
desk = app.create_app(directory)
app.write_config(owner, {"business_name": desk.config["business_name"]})
'''


def nginx_config(template, ip):
    validate_ip(ip)
    return template.replace("/live/pay.example.com/", "/live/depositdesk/").replace("pay.example.com", ip)


RENEW_SERVICE = """[Unit]
Description=Renew DepositDesk HTTPS certificate
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/depositdesk-certbot/bin/certbot renew --quiet --deploy-hook "/usr/bin/systemctl reload nginx"
TimeoutStartSec=1800
PrivateTmp=true
UMask=0022
"""
RENEW_TIMER = """[Unit]
Description=Check DepositDesk short-lived certificate every hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
"""


def wait_ready():
    for attempt in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/readyz", timeout=3) as response:
                if response.status == 200 and json.load(response).get("status") == "ready":
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise InstallError("The application did not become ready. Inspect: sudo journalctl -u depositdesk")


def install(archive, digest, ip):
    ip = validate_ip(ip)
    with tempfile.TemporaryDirectory(prefix="depositdesk-stage-") as stage_name:
        stage = Path(stage_name)
        unpack(archive, digest, stage)
        state = claim_install(ip, digest)
        if state.get("status") == "complete":
            wait_ready()
            print("Existing installation retained, including its settings, password and database.")
            return

        print("Installing the application and HTTPS dependencies...", flush=True)
        package_env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        # Avoid unreadable Python packages: all application source/dependencies
        # are public code; only state and owner credentials use private modes.
        os.umask(0o022)
        run("apt-get", "update", env=package_env)
        run("apt-get", "install", "-y", "python3-venv", "nginx", "ca-certificates", env=package_env)
        try:
            user = pwd.getpwnam("depositdesk")
        except KeyError:
            run("useradd", "--system", "--user-group", "--home-dir", str(DATA), "--shell", "/usr/sbin/nologin", "depositdesk")
            user = pwd.getpwnam("depositdesk")
        if user.pw_dir != str(DATA) or user.pw_shell != "/usr/sbin/nologin":
            raise InstallError("An unrelated depositdesk system user already exists.")
        APP.mkdir(parents=True, exist_ok=True, mode=0o755)
        for name in sorted(ALLOWED):
            target = APP / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.copyfile(stage / name, target)
            target.chmod(0o644)
        run("python3", "-m", "venv", str(APP / ".venv"))
        run(str(APP / ".venv/bin/python"), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(APP / "requirements.txt"))
        run("python3", "-m", "venv", str(CERTBOT.parents[1]))
        run(str(CERTBOT.parent / "python"), "-m", "pip", "install", "--disable-pip-version-check", "certbot==5.8.0")
        os.umask(0o077)

        DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        DATA.chmod(0o700)
        os.chown(DATA, user.pw_uid, user.pw_gid)
        os.chown(ETC, 0, user.pw_gid)
        ETC.chmod(0o750)
        env = environment(ip)
        env_path = ETC / "depositdesk.env"
        env_content = "".join(key + "=" + value + "\n" for key, value in env.items())
        if env_path.exists() and env_path.read_text() != env_content:
            raise InstallError("Settings changed during installation. Existing configuration was preserved.")
        atomic_write(env_path, env_content, 0o640)
        os.chown(env_path, 0, user.pw_gid)
        password = initial_password()
        run("runuser", "-u", "depositdesk", "--", str(APP / ".venv/bin/python"), "-c", OWNER_BOOTSTRAP,
            cwd=APP, text=True, input=password + "\n", env={"PATH": os.defpath, "LANG": "C.UTF-8", **env})
        del password

        webroot = Path("/var/www/certbot")
        webroot.mkdir(parents=True, exist_ok=True, mode=0o755)
        webroot.chmod(0o755)
        nginx = Path("/etc/nginx/conf.d/depositdesk.conf")
        # Only ACME is reachable over HTTP during installation; no owner login.
        atomic_write(nginx, "server { listen 80; server_name " + ip + ";\n"
            "location /.well-known/acme-challenge/ { root /var/www/certbot; }\n"
            "location / { return 503; } }\n", 0o644)
        default = Path("/etc/nginx/sites-enabled/default")
        if default.is_symlink() and default.resolve() == Path("/etc/nginx/sites-available/default"):
            default.unlink()
        run("nginx", "-t")
        run("systemctl", "enable", "--now", "nginx")
        run("systemctl", "reload", "nginx")
        print("Obtaining a publicly trusted IP certificate...", flush=True)
        run(str(CERTBOT), "certonly", "--webroot", "-w", str(webroot), "--ip-address", ip,
            "--cert-name", "depositdesk", "--required-profile", "shortlived", "--keep-until-expiring",
            "--non-interactive", "--agree-tos", "--register-unsafely-without-email", umask=0o022)
        atomic_write(nginx, nginx_config((APP / "nginx.conf").read_text(), ip), 0o644)
        units = Path("/etc/systemd/system")
        atomic_write(units / "depositdesk.service", (APP / "depositdesk.service").read_text(), 0o644)
        atomic_write(units / "depositdesk-cert-renew.service", RENEW_SERVICE, 0o644)
        atomic_write(units / "depositdesk-cert-renew.timer", RENEW_TIMER, 0o644)
        run("nginx", "-t")
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", "depositdesk")
        run("systemctl", "reload", "nginx")
        wait_ready()
        # Short-lived IP certificates depend on unattended renewal. Verify its
        # challenge against Let's Encrypt staging before declaring completion.
        run(str(CERTBOT), "renew", "--cert-name", "depositdesk", "--dry-run", "--non-interactive", umask=0o022)
        run("systemctl", "enable", "--now", "depositdesk-cert-renew.timer")
        run("systemctl", "is-active", "depositdesk", "nginx", "depositdesk-cert-renew.timer")
        state["status"] = "complete"
        atomic_write(ETC / "deployment.json", json.dumps(state))
        print("Server installation and certificate renewal rehearsal completed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--ip", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("This VM installer must run with sudo.")
    os.umask(0o077)
    try:
        with open("/run/lock/depositdesk-install.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError("Another DepositDesk installer is running. Let it finish.") from exc
            install(args.archive, args.sha256, args.ip)
    except (InstallError, ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print("Server setup stopped:", exc, file=sys.stderr)
        print("Existing records and settings were preserved. Resolve the error, then resume the same installer.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
