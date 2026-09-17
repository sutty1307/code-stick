# DepositDesk — outgoing ACH payment register

A single-owner payment register for outgoing deposits. Python 3.11+; the local application and its tests need only the standard library. It includes the backend, matching interface, separate simulator/sandbox/production adapters, migrations, recovery tools and deployment templates.

**A production adapter is now implemented, but has not been tested with an actual provider account or deployed.** The default simulator contacts no bank. The sandbox adapter remains sandbox-only. Production requires separate credentials, an HTTPS origin, a production webhook secret, a verified business source, and an explicit server-side live-release switch. See **[LIVE-SETUP.md](LIVE-SETUP.md)** for activation. This is payment workflow software, not a payroll calculation or tax filing service.

## Install on Google Cloud

[Open the Google Cloud installer](https://shell.cloud.google.com/?cloudshell_git_repo=https://github.com/sutty1307/code-stick&cloudshell_git_branch=depositdesk-launch&cloudshell_workspace=.&cloudshell_tutorial=depositdesk/deploy/CLOUD-SHELL.md).
Complete your Google sign-in, then run the command in the tutorial. The installer
creates the project ID, persistent VM, HTTPS endpoint and initial owner login.
Google hosting charges apply. New installations use the simulator.

The installer is implemented and locally tested. It has not yet been executed
against a real Google Cloud account. See [deployment details](deploy/README.md).

## Run locally

Extract the source archive and open its `DepositDesk` directory:

```sh
python3 app.py init
python3 app.py
```

Open `http://127.0.0.1:8765`. Create an owner password when prompted. Add a fictional recipient and authorization reference, save a draft, then release it by confirming the exact amount and authorization. Simulator controls let you test processing, failure and return. Records survive restarts.

The standalone `DepositDesk-Upgraded-Preview.html` is a separate, in-memory demo. Reloading it resets its fictional records. It does not authenticate an owner or talk to the Python backend.

## What changed

September 13 update adds a separate live ACH adapter, authenticated read-only connection check, live recipient/release controls, mounted secrets, production bank ownership checks, live/test database separation and environment-specific webhooks. Transfer validation now recognizes the provider's documented `source-funding-source` and `destination-funding-source` relationships; malformed explicit links do not fall back to weaker matching. The original protections below remain.

- Exact sandbox URL restriction and refused redirects; strict transfer identity, amount and account verification.
- Source and recipient checks immediately before submission; consistent UUIDs prevent a URL/UUID mismatch from evading the self-payment check.
- Case-preserving references, with a separate casefolded duplicate-prevention key and a migration for old records.
- Password rotation takes effect in already-running workers and invalidates every session. A login overlapping rotation cannot create a session with the old credential.
- An event arriving during a status check stays marked for another check. Events received before the submission response are matched when its reference is saved.
- An owner can reconcile an unconfirmed sandbox submission by verifying an **existing** provider transfer. Recovery never creates or retries a transfer.
- Search, status filters, totals for drafts/pending/attention, clearer review details, native modal focus containment, and a layout for narrow screens.
- Private SQLite backups, checksums, integrity verification, and restoration tests. No separate `sqlite3` executable is needed.

See `AUDIT.md` for the specific findings and `VALIDATION.md` for what was and was not verified.

## Verify

```sh
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check preview-adapter.js
python3 build_preview.py
```

Node is optional and is used only for JavaScript syntax checks. The Python suite uses temporary databases and fictional data; it makes no external payment requests. The optional `python3 tests/check_gunicorn.py` verifies whichever Gunicorn version is installed. `tests/browser_fixture.py` offers a disposable loopback-only simulator with a seeded session for manual interface inspection; it is excluded from the container image.

## Updating an existing copy

1. Stop the old application so no submission can be in progress during the upgrade.
2. Point `DEPOSITDESK_DATA_DIR` to its data directory and use the new `backup.py` to create a backup outside that directory. It supports both the old credential format and the upgraded one.
3. Replace the application source with this package and start it with the same provider, source and data-directory settings. The database migrates on startup. Keep the original source and backup together for rollback.

Existing IDs, payment instructions and history are preserved. Legacy destination URLs from the supplied adapter become their exact sandbox UUIDs. If legacy rows collide or contain foreign provider URLs, startup stops rather than deleting records. The new build preserves the casing of **new** references; capitalization already discarded by an older build cannot be reconstructed.

The owner credential now lives in SQLite. `owner.json` supplies the business display name and, for older installations, the initial credential used during migration. Editing that file is not a password reset. Deleting the database is not a recovery procedure.

## Passwords and backups

```sh
python3 app.py passwd
python3 backup.py /private/backups/desk-backup.zip
python3 backup.py --verify /private/backups/desk-backup.zip
```

`passwd` asks for the new password twice and updates running workers immediately. For unattended `init` or `passwd`, set `DEPOSITDESK_BUSINESS_NAME` and a private `DEPOSITDESK_OWNER_PASSWORD_FILE`. An environment password is supported for local test setup, but should not be committed or included in a deployment configuration.

Backup creates a new file with mode `0600` and refuses to overwrite an existing file. It uses SQLite's online backup API. Saved sessions and login attempts are removed from the snapshot; payments, audit history, environment binding and the current credential are retained. Checksums detect accidental corruption; they are not a signature or protection against an administrator rewriting the archive.

To restore, stop the application, verify the archive, and extract its `depositdesk.sqlite3` and `owner.json` into a **new private directory**. Set that directory as `DEPOSITDESK_DATA_DIR`, keep the original provider/source configuration, and start the app. Users must sign in again. Preserve the previous directory until restoration is confirmed. Restoring an older backup can omit instructions submitted after it was created; reconcile those against provider records before permitting any replacement payment.

## Dwolla sandbox acceptance

The sandbox adapter has mocked-contract tests. **It has not completed an actual Dwolla sandbox run.** Configure existing sandbox credentials and existing funding sources through your process environment, not through the browser or source files. Use a separate persistent data directory for the sandbox.

Required: `DEPOSITDESK_PROVIDER=dwolla_sandbox`, `DWOLLA_SANDBOX_KEY`, `DWOLLA_SANDBOX_SECRET`, and `DWOLLA_SANDBOX_SOURCE_ID`. The harness also uses `DWOLLA_SANDBOX_CUSTOMER_ID` and `DWOLLA_SANDBOX_DESTINATION_ID`. Initialize that directory once with `python3 app.py init`.

```sh
# Source and recipient verification only; no transfer request.
python3 sandbox_check.py

# Explicitly authorize one $0.01 sandbox instruction, then verify its transfer.
python3 sandbox_check.py --submit --confirm 0.01
```

The acceptance instruction and request ID are stable across reruns in the same retained data directory. An unconfirmed or already-submitted payment is never resubmitted. A failed verification exits with status 2 and prints the saved local/provider references for investigation. Do not delete the data directory or change the reference to force a retry. The script does not create customer accounts, link real banks, or enroll the business with a provider.

To recover an unconfirmed submission, locate its existing transfer in the provider dashboard, open the payment, and choose **Reconcile submission**. A recently started request must have at least two minutes to finish. The server verifies the transfer UUID, exact USD amount, source and destination funding sources, and local identity via correlation ID or the supplied adapter's legacy `metadata.payment_id`. Missing or inconsistent evidence leaves the submission unresolved. A linked provider reference is unique in the local database.

Transfer legs can expose different source/destination accounts. This build does not automatically traverse or reconcile those legs. Strict matching may therefore leave a legitimate multi-leg transfer unresolved. Do not weaken the matching checks merely to make it pass. A resource marked processed is not independent proof that the recipient received the money.

Optional webhook: `POST /api/webhooks/dwolla`, with `DWOLLA_SANDBOX_WEBHOOK_SECRET` set to the subscription secret. Signatures are verified over the raw body; event IDs are deduplicated. Webhooks only mark work for a **manual** status check. There is no background reconciliation worker or payment scheduler.

## Deployment templates

The bundled threaded server is for local development. `wsgi.py`, `gunicorn.conf.py`, `Dockerfile`, `nginx.conf`, and `depositdesk.service` provide the deployment entry points.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
gunicorn --config gunicorn.conf.py wsgi:application
```

The requirements pin the upstream Gunicorn 26.2.2 patch. The accessible package index in the build environment did not provide that release; the WSGI smoke test was run with 26.2.0. **The pinned deployment dependency, Docker image and nginx/systemd deployment have not been verified here.** Do not describe these templates as a completed production deployment. The standard-library local run is independent of that dependency.

For HTTPS, configure the exact public `DEPOSITDESK_ORIGIN`. The nginx file is a complete `http{}`-context snippet with its rate-limit definitions and headers included; replace its example domain and certificate paths. Bind the app privately. Enable `DEPOSITDESK_BEHIND_PROXY` only with `DEPOSITDESK_TRUSTED_PROXIES` listing the actual connecting proxy addresses. For host nginx and host Gunicorn, the loopback defaults apply. Containers require configuring their actual proxy connection address; do not assume that it is loopback.

The service uses `/var/lib/depositdesk`, a private system user and `/etc/depositdesk/depositdesk.env`; initialize the owner as that service user with the same data directory. A container similarly needs its owner initialized once in a persistent `/data` volume. Keep any published container port bound to host loopback behind the proxy. The image excludes test fixtures, offline demos and local data. `/healthz` reports liveness; `/readyz` reads the database's owner configuration. Neither checks whether a bank is reachable.

## Remaining limits

Single owner and business; no MFA, multi-user approvals, public third-party payer onboarding, incoming customer collections, bulk payroll, bank balances, fees, tax calculations, automatic settlement reconciliation or in-app provider onboarding. Live bank verification and submission use the configured business account and provider-onboarded recipients. The audit trail and unencrypted SQLite files can be modified by someone with filesystem access. Complete the acceptance and activation steps before live release.

The source is published in the `depositdesk/` folder on the `depositdesk-launch` branch of [sutty1307/code-stick](https://github.com/sutty1307/code-stick/tree/depositdesk-launch/depositdesk). This separate branch holds the payment app; the main branch remains the USB project. No hosting deployment or financial-account connection has been completed, and no real funds have moved. See `DEPLOYMENT-STATUS.md` for the current status.
