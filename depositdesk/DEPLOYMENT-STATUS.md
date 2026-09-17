# DepositDesk status — September 17, 2026

## Completed

- Recovered the saved September 13 source archive and verified its SHA-256:
  `c99f8fa6c2fe63dc18dac4e667a6ee20506f29086b9379a1959a1f5f07877e03`.
- Re-ran all 77 Python tests successfully; both JavaScript syntax checks passed.
- Published the source, tests, offline preview and deployment templates under
  `depositdesk/` on the separate `depositdesk-launch` branch in
  `sutty1307/code-stick`. The default branch is not changed.
- Implemented the Google Cloud installer and Cloud Shell tutorial. It creates
  a dedicated project, persistent VM, IP-based HTTPS, certificate renewal and
  private owner bootstrap after the user's Google sign-in.
- Added local tests for cloud decisions, HTTPS verification order, archive
  isolation, installation recovery and preservation of rotated owner credentials.
  The updated suite passes all 100 tests (77 existing and 23 new).

## Not yet completed

- Hosting deployment, HTTPS endpoint and deployment-specific database backups.
- Actual Dwolla sandbox acceptance, production approval and banking credentials.
- Live bank transfer and receiving-bank statement verification.

The application defaults to simulation. A published GitHub repository does not
activate payments. The production adapter and explicit live-release controls are
implemented but have only been exercised with mocked provider responses.

## Hosting requirements

The current backend uses SQLite with local transactions. Deploy it on one host
with persistent local storage, using the supplied systemd/nginx or Docker
configuration. Independently replicated serverless instances cannot safely
share this local database. The connected Vercel account alone does not provide
the durable database this backend requires.

No Google Cloud project was created and no Google sign-in was performed here.
The automated installer is now implemented with local tests. Cloud responses
are simulated in those tests; actual billing, VM provisioning, dependency
installation, HTTPS issuance and public hosting are still unverified.
Open `deploy/CLOUD-SHELL.md` for the shortest sign-in and installation path.

Follow `LIVE-SETUP.md` for the remaining provider activation steps. The earlier
entries in `AUDIT.md` and `VALIDATION.md` are dated historical records.
