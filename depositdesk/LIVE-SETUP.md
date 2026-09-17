# Live ACH connection — September 13, 2026

The application now includes a production Dwolla adapter. It requests an ACH
debit from the configured business bank and an ACH credit to a recipient's
already-linked bank. It supports employee, contractor and vendor recipient
records. It is still a single-owner, single-business application: public
customer registration, multiple businesses funding their own payments, and
customer-bank debit onboarding are not implemented.

## Activation sequence

1. Obtain production access for this integration through Dwolla. Existing
   sandbox credentials are not production credentials. Complete the provider's
   application review and use-case approval, including any required disclosures
   and operational notifications. See [Dwolla's integration guide](https://www.dwolla.com/p/customer-integration-guide/).
2. In that approved provider account, obtain the production application key,
   secret, Main Account ID, and its verified ACH-capable bank funding-source ID.
   Onboard recipients and their bank accounts with the provider. This app imports
   their Customer and funding-source UUIDs; it does not collect raw bank numbers.
3. Deploy the Python application to a host you control, with HTTPS and persistent
   local storage. The existing nginx/systemd or Docker templates are suitable
   starting points for a VM, including a Google Cloud VM. They have not been
   deployed in this work. Do not run this SQLite design on disposable or
   independently replicated serverless filesystems.
4. Configure `.env.production.example` values through the process manager.
   Store secrets in private server files or the host's secret manager, never
   in GitHub or chat. Use a NEW production data directory. Keep
   `DEPOSITDESK_ENABLE_LIVE_PAYMENTS=false` while validating access.
5. Configure the provider webhook subscription for
   `https://YOUR-DOMAIN/api/webhooks/dwolla` and mount its matching production
   secret. The URL here is a template, not an existing deployment.
6. As the service user, with the same environment, initialize once and check:

   ```sh
   python3 connection_check.py --config-only
   python3 app.py init
   python3 connection_check.py
   ```

   The normal connection check authenticates and reads provider records. It
   verifies that the key belongs to the configured business and the source bank
   belongs to that business, is verified, active and ACH-capable. It sends no
   payment and does not check available balance. The app's **Check connection**
   button performs the same check after owner sign-in.
7. After the actual sandbox acceptance run, provider review, deployment checks,
   backup verification and webhook-delivery check succeed, enable live release
   in the server environment and restart ALL workers. No UI control changes
   this setting. Sign in, add a provider-backed recipient, save a draft, and
   personally confirm the exact amount and real-money authorization to release.
   There is no automatic live test transaction in this package.
8. Verify the first authorized transfer with the provider AND the recipient's
   bank. Keep the provider reference and actual bank statement evidence. The
   software cannot certify how a receiving bank labels the entry or treats it
   under a promotion. No payroll descriptor or other classification is fabricated.

## Operational controls

- To stop new releases, set `DEPOSITDESK_ENABLE_LIVE_PAYMENTS=false` and restart
  all workers. Already-submitted payments remain in progress with the provider;
  this is not a cancellation or recall operation. Status checks remain available.
- Webhooks authenticate the raw request body and mark the payment for **Check
  status**. No automated reconciliation worker is included.
- Unconfirmed submissions remain locked. **Reconcile submission** only reads an
  existing provider transfer and verifies its identity, amount, account links
  and correlation; it never sends another instruction.
- A database is bound to its environment, funding source and (in live mode)
  business account. Do not reuse test data or erase duplicate-prevention records.
- The application ceiling is $25,000, not permission to send that amount.
  Provider-approved limits and account availability govern actual acceptance.

## Status of this delivery

Production request construction and checks were tested using mocks, not an
actual Dwolla account. No bank was linked, no money was moved, no GitHub upload
or hosting deployment was performed, and bank-statement classification remains
unverified. Browser access to the local fixture was blocked. See `VALIDATION.md`.

Provider references: [environment and authentication](https://developers.dwolla.com/docs/api-reference/api-fundamentals/making-requests-and-authentication),
[outgoing funds flow](https://developers.dwolla.com/docs/send-money),
[account funding sources](https://developers.dwolla.com/docs/api-reference/accounts/list-funding-sources-for-an-account),
[transfer lifecycle and bank links](https://developers.dwolla.com/docs/transfer-lifecycle).
