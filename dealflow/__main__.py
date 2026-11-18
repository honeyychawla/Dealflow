"""Command line entry point:  ./dealflow.sh <command>   (or: python3 -m dealflow <command>)"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .app import App, ON_DEMAND, PULLABLE
from .connectors import ConnectorError, linkedin
from .connectors.instagram import Instagram
from .connectors.protonmail import ProtonMail
from .triage import jev
from .util import now, parse_iso


def _say(message: str) -> None:
    print(f"  {message}", flush=True)


def cmd_serve(app: App, args: argparse.Namespace) -> int:
    from .server import serve

    if args.no_sync:
        app.runtime_overrides["sync_interval_minutes"] = 0
    if args.port:
        app.runtime_overrides["port"] = args.port
    for name, value in app.runtime_overrides.items():
        setattr(app.settings, name, value)
    serve(app)
    return 0


def cmd_sync(app: App, args: argparse.Namespace) -> int:
    targets = PULLABLE if args.connector == "all" else (args.connector,)
    failed = False
    for name in targets:
        if not app.configured(name):
            print(f"{name}: not configured, skipping (see .env.example)")
            continue
        print(f"{name}: syncing…")
        try:
            stats = app.sync(name, _say)
            print(f"{name}: {json.dumps(stats)}")
            if stats.get("error"):  # Instagram reports per-account failures without stopping the rest
                failed = True
                print(f"{name}: FAILED: {stats['error']}")
        except ConnectorError as error:
            failed = True
            print(f"{name}: FAILED: {error}")
    if not args.no_triage:
        print("triage: " + json.dumps(app.run_triage(progress=_say)))
    return 1 if failed else 0


def cmd_triage(app: App, args: argparse.Namespace) -> int:
    engine_name = f"Jev ({app.settings.jev_model})" if app.settings.jev_ready else "keyword rules (no OPENROUTER_API_KEY)"
    print(f"Sorting with {engine_name}…")
    stats = app.run_triage(force=args.all, progress=_say)
    print(json.dumps(stats, indent=2))
    return 1 if stats.get("error") else 0


def cmd_demo(app: App, args: argparse.Namespace) -> int:
    from . import demo

    if args.clear:
        print(f"Removed {demo.clear(app.db)} demo conversations.")
        return 0
    stats = demo.seed(app.db)
    print(f"Added {stats['threads']} sample conversations ({stats['messages']} messages).")
    print("triage: " + json.dumps(app.run_triage(progress=_say)))
    return 0


def cmd_linkedin(app: App, args: argparse.Namespace) -> int:
    if args.action == "signin":
        # Opens LinkedIn's own sign-in page in your browser, on a profile kept for Dealflow.
        try:
            app.linkedin_sign_in(_say)
        except ConnectorError as error:
            print(f"linkedin: {error}")
            return 1
        print("linkedin: connected. Sync it with: ./dealflow.sh sync linkedin")
        return 0
    if args.action == "signout":
        try:
            app.linkedin_disconnect()
        except ConnectorError as error:
            print(f"linkedin: {error}")
            return 1
        print("linkedin: signed out here; the browser profile was removed. Stored conversations are kept.")
        return 0
    if args.action == "known":
        print(json.dumps({"now": now().isoformat(timespec="seconds"), "threads": linkedin.known_threads(app.db, args.limit)}, indent=1))
        return 0
    if args.action == "ingest":
        source = sys.stdin.read() if args.path in (None, "-") else Path(args.path).read_text(encoding="utf-8")
        try:
            payload = json.loads(source)
        except json.JSONDecodeError as error:
            print(f"Not valid JSON: {error}")
            return 1
        stats = linkedin.ingest_payload(app.db, payload)
        app.record_push("linkedin", stats)
        print("linkedin: " + json.dumps(stats))
    elif args.action == "import":
        if not args.path:
            print("Usage: ./dealflow.sh linkedin import <path/to/messages.csv> [--since-days 365]")
            return 1
        try:
            stats = linkedin.import_export(app.db, app.settings, Path(args.path).expanduser(), args.since_days)
        except (OSError, ValueError) as error:
            print(f"Import failed: {error}")
            return 1
        app.record_push("linkedin", stats)
        print("linkedin: " + json.dumps(stats))
    if not args.no_triage:
        print("triage: " + json.dumps(app.run_triage(progress=_say)))
    return 0


def cmd_doctor(app: App, _args: argparse.Namespace) -> int:
    s = app.settings
    ok = True
    print("Dealflow doctor\n")

    print("Sorting (Jev via OpenRouter)")
    if not s.jev_ready:
        print("  -  OPENROUTER_API_KEY is not set. Sorting falls back to keyword rules until it is.")
    else:
        try:
            result = jev.self_test(s.openrouter_api_key, s.jev_model, s.openrouter_base_url)
            print(f"  ok {result['model']} answered a test question (p={result['probability']}, cost ${result['cost']})")
            if app.rubric.questions():
                print(f"     rubric: {len(app.rubric.questions())} questions, {len(app.rubric.categories)} categories")
        except jev.JevError as error:
            ok = False
            print(f"  FAIL {error}")
    print(f"  {'ok' if s.writer_model and s.jev_ready else '- '} writer model: {s.writer_model or 'not set (summaries use the first message; no reply drafts)'}")

    print("\nInstagram")
    if not s.instagram_ready:
        print("  -  No Instagram account is set up (INSTAGRAM_ACCESS_TOKEN is not set).")
    seen_accounts: dict[str, int] = {}
    for account in s.instagram_accounts:
        tag = f"account {account.slot}" + (f" ({account.label})" if account.label else "")
        try:
            info = Instagram(s, app.db, account).check()
            if info["account_id"] in seen_accounts:
                ok = False
                print(f"  FAIL {tag}: same Instagram account as account {seen_accounts[info['account_id']]} (@{info['username']})")
                continue
            seen_accounts[info["account_id"]] = account.slot
            print(f"  ok {tag}: signed in as @{info['username']} via {info['route']} login; "
                  f"{info['sample_conversations']} conversations visible in a sample of 5")
            if info["sample_conversations"] == 0:
                print("     0 conversations is the known symptom of an app without access to messages from people\n"
                      "     who have no role on it. See README > Instagram > 'If no conversations come back'.")
        except ConnectorError as error:
            ok = False
            print(f"  FAIL {tag}: {error}")

    print("\nGmail")
    if not s.gmail_ready:
        print("  -  No Gmail account is set up (Sign in with Google under Settings > Gmail, or set GMAIL_ADDRESS and GMAIL_APP_PASSWORD).")
    seen_addresses: set[str] = set()
    for gmail_account in s.gmail_accounts:
        tag = gmail_account.label or gmail_account.address
        if gmail_account.address in seen_addresses:
            ok = False
            print(f"  FAIL {tag}: {gmail_account.address} is set up twice")
            continue
        seen_addresses.add(gmail_account.address)
        try:
            info = app.gmail_connector(gmail_account).check()
            via = "with Google, read-only" if info.get("via") == "google" else "over IMAP with an app password"
            print(f"  ok {tag}: signed in to {info['address']} {via}; inbox holds {info['inbox_messages']} messages; "
                  f"sent mailbox: {info['sent_mailbox'] or 'not found'}")
            if info["missing_mailboxes"]:
                ok = False
                print(f"  FAIL {tag}: mailboxes not found: {', '.join(info['missing_mailboxes'])}")
        except ConnectorError as error:
            ok = False
            print(f"  FAIL {tag}: {error}")

    print("\nEmail (Proton Mail Bridge)")
    if not s.proton_ready:
        print("  -  PROTON_USERNAME / PROTON_BRIDGE_PASSWORD are not set.")
    else:
        try:
            info = ProtonMail(s, app.db).check()
            pinned = "certificate pinned" if info["pinned_cert"] else "UNVERIFIED TLS (set PROTON_CERT_PATH)"
            print(f"  ok Bridge reachable at {s.proton_imap_host}:{s.proton_imap_port}, {pinned}")
            print(f"     mailboxes: {', '.join(info['mailboxes'][:14])}")
            missing = [m for m in [*s.proton_mailboxes, s.proton_sent_mailbox] if m and m not in info["mailboxes"]]
            if missing:
                ok = False
                print(f"  FAIL configured mailboxes not found: {', '.join(missing)}")
        except ConnectorError as error:
            ok = False
            print(f"  FAIL {error}")

    print("\nLinkedIn (your browser, on demand)")
    from .connectors import linkedin_browser

    browser = linkedin_browser.browser_status(s, app.db)
    if not browser["available"]:
        print("  -  no Chrome, Chromium, Brave or Edge found, so browser sync is unavailable")
    elif not browser["connected"]:
        print(f"  -  not connected. {browser['browser']} is available: ./dealflow.sh linkedin signin")
    elif browser["needs_sign_in"]:
        ok = False
        print("  FAIL LinkedIn needs sign-in again: ./dealflow.sh linkedin signin")
    else:
        print(f"  ok connected in {browser['browser']}; syncs when you press Sync or run: ./dealflow.sh sync linkedin")
    collected = parse_iso(app.db.get_state("linkedin", "last_collected_at"))
    threads = app.db.scalar("SELECT COUNT(*) FROM threads WHERE channel='linkedin' AND is_demo=0", (), 0)
    if collected is None:
        print(f"  -  never collected ({threads} conversations stored)")
    else:
        hours = (now() - collected).total_seconds() / 3600
        print(f"  ok last collected {hours:.1f}h ago; {threads} conversations stored")

    from .net import trust_source

    source = trust_source()
    print("\nHTTPS root certificates")
    if source == "none":
        ok = False
        print("  FAIL none found, so Instagram and OpenRouter cannot be reached. On macOS run\n"
              "       \"/Applications/Python 3.x/Install Certificates.command\" or set SSL_CERT_FILE.")
    else:
        print(f"  ok {source}")
    print(f"\nData: {s.db_path}")
    return 0 if ok else 1


def cmd_stats(app: App, _args: argparse.Namespace) -> int:
    status = app.status()
    for connector in status["connectors"]:
        print(f"{connector['name']:<10} {connector['threads']:>5} conversations   last ok: {connector['last_ok_at'] or 'never'}")
    print(f"sorting    engine={status['triage']['engine']} pending={status['triage']['pending']}")
    print(f"spend      ${status['spend']['total']:.4f} total, ${status['spend']['month']:.4f} this month, {status['spend']['calls']} calls")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dealflow", description="One inbox for Instagram, LinkedIn and email, sorted by Jev.")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the web app (default)")
    p.add_argument("--port", type=int)
    p.add_argument("--no-sync", action="store_true", help="do not sync on a timer")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("sync", help="pull new messages now")
    p.add_argument("connector", nargs="?", default="all", choices=["all", *PULLABLE, *ON_DEMAND],
                   help="'all' is everything the timer syncs; LinkedIn's browser sync runs only when named")
    p.add_argument("--no-triage", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("triage", help="sort conversations that are new or changed")
    p.add_argument("--all", action="store_true", help="re-sort everything (after editing rubric.json)")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("linkedin", help="LinkedIn: signin | signout | known | ingest <file.json> | import <messages.csv>")
    p.add_argument("action", choices=["signin", "signout", "known", "ingest", "import"])
    p.add_argument("path", nargs="?")
    p.add_argument("--since-days", type=int, default=365)
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--no-triage", action="store_true")
    p.set_defaults(func=cmd_linkedin)

    p = sub.add_parser("doctor", help="check every connection and say what to fix")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("demo", help="load sample conversations (or --clear them)")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("stats", help="counts and spend")
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve", *(argv or [])])
    try:
        app = App()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Could not start: {error}")
        return 1
    return int(args.func(app, args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
