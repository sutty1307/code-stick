# Validation record

## September 17, 2026 — Google Cloud installer

- Source was published to `sutty1307/code-stick`, branch `depositdesk-launch`,
  folder `depositdesk/`. Earlier statements about an unperformed GitHub upload
  are retained below as historical records.
- All 100 Python tests pass: the existing 77 and 23 new deployment tests.
  `verification/google-cloud-unittest.txt` records this run.
- Cloud tests use a fake `gcloud` and fake HTTPS responses. They cover missing
  sign-in/billing, project/account selection, restricted SSH, disk retention,
  resumption, configuration drift, upload orchestration and public readiness
  verification before success or password display. No Google login or cloud
  resource creation was performed.
- Owner bootstrap runs as a real Python subprocess against temporary SQLite
  databases. Tests verify preservation after password rotation, interrupted
  initialization and a missing database. Bundle checks exclude credentials
  and records, verify reproducibility and reject changed hashes, traversal,
  unexpected files and links before extraction.
- Both JavaScript syntax checks pass. The renewal systemd units pass local
  `systemd-analyze verify` syntax validation with their VM-only executable
  substituted by `/usr/bin/true`; this does not verify Certbot execution.
- Running the installer without `--deploy` prints its plan without `gcloud`
  or cloud calls. Google Cloud Shell tutorial and installer are included.
- Actual VM creation, IAP/OS Login access, billing, Debian package installation,
  Gunicorn 26.2.2 installation, nginx startup, certificate issuance/renewal and
  public HTTPS are **not runtime-verified here**. The installer checks these
  at execution time and stops if a required command or readiness check fails.
- No new browser verification, provider API acceptance, bank connection or
  actual transfer was performed. This update completes deployment automation,
  not a deployed site or activated banking service.

## September 13, 2026 — production adapter update

- Recovered baseline: all 46 existing tests reproduced before changes.
- Updated suite: 77 tests pass, including 31 new production-mode/contract and
  application checks. All provider responses are mocked. No external payment
  request is made by these tests.
- Production tests cover disabled release, explicit real-money confirmation,
  separate environment credentials and URLs, business ownership, verified ACH
  funding, destination ownership, exact USD amount and actual documented bank
  links, rejection of malformed explicit links, duplicate releases, unconfirmed
  submissions, production webhooks, secret files, authenticated connection
  checks, environment binding and global owner-login throttling.
- Original HTTP lifecycle and backup restoration tests still pass.
- JavaScript syntax checks pass. Browser navigation to the local fixture was
  blocked with `ERR_BLOCKED_BY_CLIENT`; no visual/interaction verification is
  claimed for this update.
- Installing the pinned Gunicorn 26.2.2 failed because the available index lists
  only through 26.2.0. The deployment pin remains unchanged. No current-run
  Gunicorn, Docker or nginx deployment is claimed.
- No Dwolla credentials or cloud-hosting configuration were present. No actual
  sandbox/production call, bank connection, real transfer, GitHub upload, or
  hosted deployment was performed. Receiving-bank classification is unverified.

See `LIVE-SETUP.md` and `.env.production.example` for the exact activation path.
The current test log is `verification/live-update-unittest.txt`. Other files in
`verification/` without the `live-update` prefix are retained September 12
records, including historical hashes; they do not describe the updated source.

## September 12, 2026 — retained historical validation

| Check | Result | Scope |
|---|---|---|
| Original project test suite | 20 passed before changes | Recovered baseline, temporary SQLite databases and mocks |
| Upgraded Python suite | 46 passed; `verification/unittest.txt` | Transaction lifecycle, authentication, concurrency, recovery, legacy migrations, backup/restore, provider guards and real local HTTP |
| Fresh CLI setup and WSGI import | Passed in suite | New owner, import, credential storage and password change |
| Gunicorn process | Passed, version 26.2.0, 2 workers × 4 threads | Readiness, login, draft/release/process, rotation while serving, invalidated session and old password, new password, retained record |
| JavaScript syntax | Passed | `static/app.js` and `preview-adapter.js`; this is not a browser execution test |
| Asset contract and offline packaging | See `verification/assets.json` | Static HTML/JS IDs, asset presence, inline event absence, and self-contained demo resources |
| Browser visual/interaction verification | **Not performed** | Localhost was blocked; the standalone file was then explicitly rejected by browser URL policy. No alternate browser or workaround was used. No screenshots are claimed. |
| Actual Dwolla sandbox transfer | **Not performed** | No configured sandbox credentials were supplied. Mock tests are not an actual API acceptance test. |
| Gunicorn 26.2.2 dependency | **Not installed/tested here** | The upstream changelog lists it; the accessible package index did not supply it. Requirements retain the upstream patch pin. |
| Docker, nginx, systemd, HTTPS hosting | **Not deployed or runtime-tested** | Templates supplied. Docker/nginx executables were unavailable. |
| Live payments or financial account connections | **None** | No live adapter existed in that September 12 version; no real funds moved. |

## Reproduce

```sh
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check preview-adapter.js
python3 build_preview.py
# With an installed Gunicorn, reports the version it actually exercises:
python3 tests/check_gunicorn.py
```

The harness `sandbox_check.py` requires deliberate sandbox configuration. Its default performs account verification only. `--submit --confirm 0.01` explicitly authorizes one test instruction, persistently reuses its identity, and exits nonzero if the transfer cannot be verified. Keep the same test data directory across reruns.

A successful `processed` response refers to a provider resource. No test here establishes final bank receipt or reconciles all related transfer legs. No background polling, scheduled task, public deployment or GitHub upload was performed.
