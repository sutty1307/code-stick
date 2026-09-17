"""Check configured provider access without submitting a transfer.

--config-only checks required deployment settings without a network request.
The normal check creates an OAuth token and reads the configured source bank.
It does not create customers, link banks, or initiate payments.
"""
import argparse
import json
import os

from app import create_app


def configuration_report():
    mode = os.environ.get("DEPOSITDESK_PROVIDER", "simulator")
    required = ["DEPOSITDESK_DATA_DIR", "DEPOSITDESK_ORIGIN"]
    if mode in {"dwolla_sandbox", "dwolla_production"}:
        prefix = "DWOLLA_PRODUCTION_" if mode == "dwolla_production" else "DWOLLA_SANDBOX_"
        required += [prefix + key for key in ("KEY", "SECRET", "SOURCE_ID")]
        if mode == "dwolla_production":
            required += [prefix + "ACCOUNT_ID", prefix + "WEBHOOK_SECRET"]
    missing = [name for name in required if not os.environ.get(name) and not os.environ.get(name + "_FILE")]
    return {"mode": mode, "missing_settings": missing,
        "network_contacted": False, "transfer_submitted": False,
        "message": "Presence check only; values, secret files, approval and bank connection are not verified."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args()
    if args.config_only:
        report = configuration_report()
        print(json.dumps(report, indent=2))
        return 2 if report["missing_settings"] else 0
    try:
        report = create_app().connection_report()
    except (ValueError, OSError) as exc:
        # Never print file contents, tokens, or process environment values.
        print(json.dumps({"connected": False, "transfer_submitted": False,
            "message": str(exc) if isinstance(exc, ValueError) else "A required configuration or secret file could not be read."}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["connected"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
