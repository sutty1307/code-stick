# DepositDesk — findings and changes

## September 13, 2026 update

A separate production adapter has been added; the sandbox adapter is still
restricted to the sandbox. The key API-contract correction uses Dwolla's
documented `source-funding-source` and `destination-funding-source` links when
verifying bank identities. The older matcher compared party links with funding
source URLs and could reject legitimate documented responses. Explicit invalid
bank links are rejected, not replaced with fallback party links.

Live source ownership is verified through the authenticated API root and the
configured Main Account's bank listing. Recipient ownership uses that Customer's
bank listing. Live release requires HTTPS, separate credentials/data and explicit
owner confirmation, and is disabled by default. `connection_check.py` and the
authenticated connection endpoint never submit transfers. The implementation is
covered by mocked contracts; live integration and bank classification are not
verified. See `LIVE-SETUP.md` and the current `VALIDATION.md` section.

## September 12, 2026 findings — historical record

Reviewed September 12, 2026. Inputs: the recovered original project and the ten supplied attachment files. The supplied `app.py.diff` was compared with the recovered application; the completed upgrade includes a full `app.py`, not a patch-only dependency. The findings below describe code behavior, not an observed compromise or an actual bank transaction.

| Finding | Evidence in the supplied code | Implemented correction |
|---|---|---|
| Sandbox restriction accepted impostor hosts | Constructing the attached adapter with `https://api-sandbox.example.invalid` or `https://api-sandbox.dwolla.com@other.example.invalid` succeeded with dummy credentials. No network request was made. | Exact permitted base; strict sandbox resource URLs; no redirects. |
| HTTP redirects were not refused | The attachment uses the default `urllib.request.urlopen` redirect behavior. This contradicts a claim of transport restricted to the sandbox. | The recovered adapter's refusing redirect handler is retained and tested. |
| A status was accepted for the wrong transfer | A mocked attached response containing a different transfer ID, EUR and `999.99` still returned `processed` for a one-cent saved instruction. | Verify HTTP result, UUID, currency, exact amount and account endpoints before accepting status. |
| Self-payment check compared different representations | The attachment returns a full destination URL, while `Desk.source` is a UUID. Their string equality check cannot recognize the same bank. | Canonical UUIDs, migration of legacy URLs, self-payment rejection, destination revalidation at release. |
| Password change did not update running workers | In the supplied diff, `passwd` rewrites `owner.json`, but already-running `Desk` instances keep the previous `self.config['password']`. Deleting sessions alone does not revoke that cached password. This is a code-inspection finding. | Database-authoritative credential, atomic rotation/session revocation, and a second credential check before a concurrent login creates its session. Tested in two Desk instances and a running two-worker Gunicorn process. |
| A new webhook could be cleared by an older refresh | The previous webhook only changes `refresh_due`, while refresh checks `updated_at` and then clears the flag. A webhook can arrive between that read and write. | Incrementing event version; clear the flag only if no newer event arrived. |
| An early webhook could be left unmatched | A webhook received before `provider_ref` was saved could not mark that payment. Submission did not revisit the stored event. | Match stored events when committing the provider reference. |
| Unconfirmed payments had no recovery operation | The old workflow deliberately locked them but offered no verified way to recover their reference. | Read and validate an existing transfer, atomically attach its unique reference, and retain audit evidence without a new POST. |
| Reference capitalization was destroyed | `draft()` stored only the casefolded text. | Store original trimmed text separately from its casefolded uniqueness key. Existing lost capitalization is not invented. |
| Backup depended on an undeclared executable | `backup.sh` invokes the SQLite CLI; the supplied Dockerfile does not install it. The shell also places the destination into a SQLite command string. | Standard-library SQLite backup, no shell/SQL interpolation, private files from creation, checksum/integrity verification and restoration tests. |
| Matching frontend assets were absent | The attachment set includes the redesigned HTML and JS, but no matching `styles.css` or `favicon.svg`. The recovered project's CSS targets its earlier interface. | Complete matching stylesheet/icon, search/filter UI, native modal and keyboard controls. Visual browser verification remains blocked. |
| Proxy trust relied only on a boolean | The supplied `client_ip` accepts forwarded addresses whenever `behind_proxy` is true, without verifying the connecting peer. | Configured proxy address/network allowlist and parsed IPs. The nginx template overwrites the supplied forwarding header. |

## What the checks establish

The upgraded tests cover these protections against local fixtures and mocked provider responses. A real local HTTP lifecycle and a Gunicorn smoke test exercise the server entry points. They do not establish that the Dwolla contract works end to end for this business, that a bank settled a transfer, or that the deployment is production-ready.

The source package contains repeatable tests and a separate opt-in sandbox harness. The standalone preview is an in-memory demo and is not evidence of backend integration.

## Primary references checked

- [Dwolla: initiate a transfer](https://developers.dwolla.com/docs/api-reference/transfers/initiate-a-transfer) — request identity and returned transfer location.
- [Dwolla: retrieve a transfer](https://developers.dwolla.com/docs/api-reference/transfers/retrieve-a-transfer) — transfer identity, amount, correlation and account relationships. The upgrade deliberately leaves incomplete matches unresolved.
- [Dwolla: idempotency keys](https://developers.dwolla.com/docs/api-reference/api-fundamentals/idempotency-key) — the documented key/body pairing expires after 24 hours; permanent local duplicate prevention cannot rely on that alone.
- [Gunicorn: 2026 release notes](https://gunicorn.org/2026-news/) — upstream lists 26.2.2. The accessible package index supplied only up to 26.2.0, which is the version actually smoke-tested.
