# Deploy DepositDesk

## Sign in to Google

This installs the existing app on your Google Cloud account. You do not need to
choose or create a project ID: the installer creates a dedicated project.

Google bills the VM, persistent disk, static IPv4 address and network usage.
Account-specific free-tier credits may apply; this is not a promise of free
hosting. Stopping the VM leaves disk and reserved-IP charges running.

Google opens this repository in a temporary Cloud Shell without your account
credentials. Run this command and complete Google's sign-in yourself:

```sh
gcloud auth login
```

Do not paste a password, sign-in code or bank credential into chat.

## Install

The terminal starts in the repository root. Run:

```sh
python3 depositdesk/deploy/google_cloud.py --deploy
```

The installer handles the project, persistent VM, network, application, HTTPS,
certificate renewal and initial owner password. Keep this terminal open until
it prints your HTTPS address and password. Save the password privately.

If Google reports no billing account, [open Google billing](https://console.cloud.google.com/billing),
complete that account step, then run the same install command again. If several
billing accounts exist, the installer lists them and explains how to select one.

## Open the app

Open the HTTPS address printed after successful verification, then sign in with
the generated owner password. No domain purchase or DNS changes are required.

The initial installation uses the simulator. Connect and verify the payment
provider using the app's [activation guide](https://github.com/sutty1307/code-stick/blob/depositdesk-launch/depositdesk/LIVE-SETUP.md)
before sending actual deposits.

An interrupted installation can be resumed by rerunning the same command. It
keeps the existing project, disk, database and password. If it stops with an
error, share the error text and project ID; keep credentials private.

Deployment code and local tests are complete. An actual Google Cloud deployment
has not yet been verified. [Deployment details and recovery](https://github.com/sutty1307/code-stick/blob/depositdesk-launch/depositdesk/deploy/README.md).
