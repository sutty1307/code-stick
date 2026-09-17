# Google Cloud deployment

[Open the DepositDesk installer in Google Cloud Shell](https://shell.cloud.google.com/?cloudshell_git_repo=https://github.com/sutty1307/code-stick&cloudshell_git_branch=depositdesk-launch&cloudshell_workspace=.&cloudshell_tutorial=depositdesk/deploy/CLOUD-SHELL.md)

Complete Google's sign-in yourself, then run the command shown in the tutorial.
The installer never signs in to Google on your behalf. It creates a project ID
automatically; it does not require a pre-created project or a domain.

**Status:** implemented and locally tested with fake Google responses. This is
not a completed hosting deployment. Cloud IAM, billing, package installation,
certificate issuance and service startup still require an actual account run.

## What runs

`google_cloud.py` runs in Cloud Shell and uploads only the application allowlist
and `vm_setup.py` through Google's Identity-Aware Proxy (IAP). Local `.env`
files, payment databases, owner files and test fixtures are not uploaded. The
archive is reproducible across checkouts, and its checksum is verified before
any VM installation begins.

| Resource | Configuration |
|---|---|
| Project | Automatically named `depositdesk-…`, labeled `depositdesk=managed` |
| Compute | One Debian 12 `e2-micro` in `us-central1-a` |
| Storage | 30 GB standard persistent boot disk; retained when VM is deleted |
| Address | Reserved external IPv4 address, with HTTPS certificate |
| Network | Dedicated custom VPC/subnet; public TCP 80/443; TCP 22 only from IAP |
| Cloud identity | No service account attached to the VM |
| Application | Two Gunicorn workers, SQLite at `/var/lib/depositdesk`, nginx proxy |
| Owner | Random password generated on the VM, stored in a root-only file |
| HTTPS | Certbot 5.8.0, Let's Encrypt IP certificate, hourly renewal timer |
| Payments | Simulator initially; live release disabled |

The IP certificate uses Let's Encrypt's short-lived profile and the webroot
challenge. The installer checks renewal against staging and then checks the
public `/readyz` endpoint with normal certificate verification. It reports
success only after that HTTPS check. See [Let's Encrypt's IP-certificate
guide](https://letsencrypt.org/2026/03/11/shorter-certs-certbot) and the
[Certbot documentation](https://eff-certbot.readthedocs.io/en/stable/using.html).

The VM has no public application port other than nginx. App code is root-owned;
the service runs as `depositdesk` with a private state directory. Bank secrets
are absent from source, metadata and the initial service environment. The
bootstrap password is sent to initialization on stdin, never in command-line
arguments. The root-only initial-password file is printed only in your Cloud
Shell terminal after HTTPS verification.

## Run or resume

From the repository root, this prints a plan without making cloud calls:

```sh
python3 depositdesk/deploy/google_cloud.py
```

This performs the installation after you sign in:

```sh
python3 depositdesk/deploy/google_cloud.py --deploy
```

One open billing account is selected automatically. When there is more than
one, choose the account with `--billing-account ACCOUNT_ID`. No new project is
created when there is no usable billing account. An organization policy, quota
or permission error stops installation with Google's error text; it does not
change organization policies or switch to another account.

The installer saves project/account details in a private Cloud Shell state
file. [Google's Open in Cloud Shell documentation](https://docs.cloud.google.com/shell/docs/open-in-cloud-shell)
explains why this non-Google repository opens in a temporary environment. If
that environment disappears, the next run finds your one labeled DepositDesk
project. Multiple matches require `--project EXISTING_PROJECT_ID`.

Installation is resumable. A matching finished install is retained. It refuses
unmarked existing application data, a changed IP or application version, a
different saved Google account, changed VM access settings, or a retained disk
whose VM is missing. It does not delete records, rotate the owner password or
rebuild a missing database. A source update is a separate operation: follow
the backup-and-upgrade procedure in the main README.

Errors leave created resources in place so they can be resumed or recovered.
They may continue to incur charges. A single host is not high availability;
retaining a disk is not an offsite backup. No automatic offsite backup or cloud
monitoring alert has been provisioned.

## Server access and operations

Use your printed project ID to open an IAP-protected shell:

```sh
gcloud compute ssh depositdesk --project=YOUR_PROJECT_ID --zone=us-central1-a --tunnel-through-iap
```

On the VM, inspect service status or certificate renewal:

```sh
sudo systemctl status depositdesk nginx depositdesk-cert-renew.timer
sudo journalctl -u depositdesk -n 50 --no-pager
sudo /opt/depositdesk-certbot/bin/certbot renew --cert-name depositdesk --dry-run
```

To rotate the owner password from that interactive VM shell, this starts the
existing app's password command with the service configuration and user:

```sh
sudo systemd-run --wait --pty --collect \
  --property=User=depositdesk --property=Group=depositdesk \
  --property=WorkingDirectory=/opt/depositdesk \
  --property=EnvironmentFile=/etc/depositdesk/depositdesk.env \
  /opt/depositdesk/.venv/bin/python app.py passwd
```

After saving and verifying your new password, remove the obsolete bootstrap
password file with `sudo rm /etc/depositdesk/initial-password`. This does not
delete the current owner credential, which is in SQLite. Rerunning deployment
does not recover or reset a rotated password.

Use `backup.py` from the main README to create and verify a private backup,
then copy it to separately protected storage. Its destination must be writable
by the `depositdesk` service user, outside the data directory, and must not
already exist. A local copy alone does not survive disk loss. Never delete the
database as a password-reset or retry mechanism.

For live activation, keep a separate persistent data directory for the provider
environment, as described in `LIVE-SETUP.md`. Initialize it once with the new
provider configuration and make the service and maintenance commands use that
same directory. Keep it under `/var/lib/depositdesk` so the service's filesystem
protection permits writes. Do not enable live release before provider
verification is complete.

Google charges depend on account credits and actual usage. Review
[Compute pricing](https://cloud.google.com/products/compute/pricing/general-purpose)
and [network/IP pricing](https://cloud.google.com/vpc/network-pricing). Stopping
the VM does not release its disk or reserved IP. Deleting the dedicated project
is destructive and is not part of this installer; preserve and verify backups
before any eventual teardown.
