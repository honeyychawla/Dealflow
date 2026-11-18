"""The application service: owns settings, storage and the rubric, and runs sync and triage jobs."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .config import Settings, load_settings
from .connectors import linkedin as linkedin_connector
from .connectors import ConnectorError, NotConfigured, linkedin_browser
from .connectors.gmail import Gmail
from .connectors.gmail_api import GmailApi
from .connectors.instagram import Instagram
from .connectors.protonmail import ProtonMail
from .db import DB
from .triage import engine
from .triage.rubric import Rubric
from .util import dumps, loads, now, now_iso, parse_iso

CONNECTORS = ("instagram", "gmail", "email", "linkedin")
PULLABLE = ("instagram", "gmail", "email")  # what the timer syncs
# Synced only when the owner presses Sync, never on the timer: it drives their browser
# on LinkedIn, which LinkedIn's terms do not allow (see connectors/linkedin_browser.py).
ON_DEMAND = ("linkedin",)
MULTI_ACCOUNT = ("instagram", "gmail")  # connectors that hold several of the owner's accounts
# The channel a connector's conversations are filed under ("email" is Proton Mail's connector name).
CHANNEL_OF = {"instagram": "instagram", "gmail": "email", "email": "email", "linkedin": "linkedin"}


class App:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self.db = DB(self.settings.db_path)
        self.rubric = Rubric.load(self.settings.rubric_path)
        self._locks = {name: threading.Lock() for name in CONNECTORS}
        self._job_lock = threading.Lock()
        self.job: dict[str, Any] = {"running": False, "label": "", "detail": "", "started_at": "", "result": None}
        self._scheduler: threading.Thread | None = None
        self._stop = threading.Event()
        # Set from the command line (--port, --no-sync); these outlive a settings reload.
        self.runtime_overrides: dict[str, Any] = {}

    def reload_rubric(self) -> None:
        self.rubric = Rubric.load(self.settings.rubric_path)

    def reload_settings(self) -> bool:
        """Re-read .env after the Settings page saved it. Returns True if a restart is needed."""
        current = self.settings
        fresh = load_settings(current.env_path)
        restart_needed = fresh.port != current.port and "port" not in self.runtime_overrides
        # Things a running process cannot change underneath itself.
        fresh.data_dir, fresh.rubric_file = current.data_dir, current.rubric_file
        fresh.host, fresh.port = current.host, current.port
        for name, value in self.runtime_overrides.items():
            setattr(fresh, name, value)
        self.settings = fresh
        return restart_needed

    def import_linkedin_export(self, csv_text: str, since_days: int) -> dict[str, Any]:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "messages.csv"
            path.write_text(csv_text, encoding="utf-8")
            stats = linkedin_connector.import_export(self.db, self.settings, path, since_days)
        self.record_push("linkedin", stats)
        return stats

    # -- connectors ---------------------------------------------------------

    def configured(self, name: str) -> bool:
        if name == "instagram":
            return self.settings.instagram_ready
        if name == "gmail":
            return self.settings.gmail_ready
        if name == "email":
            return self.settings.proton_ready
        return True  # linkedin needs no credentials here

    def sync(self, name: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Run one connector with bookkeeping. Raises ConnectorError on failure."""
        if name not in PULLABLE + ON_DEMAND:
            raise ConnectorError(f"There is nothing called {name} to sync")
        if not self._locks[name].acquire(blocking=False):
            raise ConnectorError(f"{name} sync is already running")
        if name in MULTI_ACCOUNT:
            try:
                return self._sync_accounts(name, progress)
            finally:
                self._locks[name].release()
        run_id = int(
            self.db.run("INSERT INTO sync_runs(connector, started_at) VALUES(?,?)", (name, now_iso())).lastrowid
        )
        try:
            if name == "linkedin":
                stats = linkedin_browser.collect(self.settings, self.db, progress)
            else:
                stats = ProtonMail(self.settings, self.db, self.rubric.email_prefilter).sync(progress)
            self.db.run(
                "UPDATE sync_runs SET finished_at=?, status='ok', stats=? WHERE id=?", (now_iso(), dumps(stats), run_id)
            )
            return stats
        except Exception as error:  # noqa: BLE001 - recorded, then re-raised in a uniform type
            status = "unconfigured" if isinstance(error, NotConfigured) else "error"
            self.db.run(
                "UPDATE sync_runs SET finished_at=?, status=?, error=? WHERE id=?",
                (now_iso(), status, str(error)[:1200], run_id),
            )
            if isinstance(error, ConnectorError):
                raise
            raise ConnectorError(f"{type(error).__name__}: {error}") from error
        finally:
            self._locks[name].release()

    def gmail_connector(self, account: Any) -> Gmail:
        """Signed in with Google: the Gmail API, read-only scope. Otherwise IMAP with the app password."""
        kind = GmailApi if account.signed_in_with_google else Gmail
        return kind(self.settings, self.db, account, self.rubric.email_prefilter)

    def _accounts(self, kind: str) -> list[Any]:
        return self.settings.instagram_accounts if kind == "instagram" else self.settings.gmail_accounts

    def _sync_accounts(self, kind: str, progress: Callable[[str], None] | None) -> dict[str, Any]:
        """Sync every account of one connector in turn. Each has its own cursor and its own run record.

        Never raises for a problem with one account: what the others pulled is kept,
        and the failures are reported together under "error".
        """
        accounts = self._accounts(kind)
        if not accounts:
            raise NotConfigured(f"No {kind.capitalize()} account is set up")
        totals: dict[str, int] = {}
        report: list[dict[str, Any]] = []
        failures: list[str] = []
        seen: dict[str, str] = {}  # the account's own id -> the name of the slot that already synced it
        for account in accounts:
            run_id = int(
                self.db.run(
                    "INSERT INTO sync_runs(connector, started_at) VALUES(?,?)", (account.namespace, now_iso())
                ).lastrowid
            )
            name = account.label or f"account {account.slot}"
            try:
                if kind == "instagram":
                    connector: Any = Instagram(self.settings, self.db, account)
                    connector.identify()  # asks Meta which account the token belongs to
                    key = connector.account_id
                else:
                    connector = self.gmail_connector(account)
                    key = connector.account
                name = connector.display
                if key in seen:
                    raise ConnectorError(f"This is the same account as {seen[key]}. Remove one of the two.")
                seen[key] = name
                stats = connector.sync(progress)
                self.db.run(
                    "UPDATE sync_runs SET finished_at=?, status='ok', stats=? WHERE id=?",
                    (now_iso(), dumps(stats), run_id),
                )
                for stat, value in stats.items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        totals[stat] = totals.get(stat, 0) + value
                report.append({"slot": account.slot, "name": name, **stats})
            except Exception as error:  # noqa: BLE001 - recorded per account; the loop carries on
                status = "unconfigured" if isinstance(error, NotConfigured) else "error"
                message = str(error) if isinstance(error, ConnectorError) else f"{type(error).__name__}: {error}"
                self.db.run(
                    "UPDATE sync_runs SET finished_at=?, status=?, error=? WHERE id=?",
                    (now_iso(), status, message[:1200], run_id),
                )
                failures.append(message if len(accounts) == 1 else f"{name}: {message}")
                report.append({"slot": account.slot, "name": name, "error": message})
        totals.setdefault("new_messages", 0)
        result: dict[str, Any] = {**totals, "accounts": report}
        if failures:
            result["error"] = " | ".join(failures)
        return result

    def accounts_status(self, kind: str) -> list[dict[str, Any]]:
        """One entry per configured account of a connector: who it is and how its last sync went."""
        channel = CHANNEL_OF[kind]
        out = []
        for account in self._accounts(kind):
            ns = account.namespace
            last = self.db.one(
                "SELECT * FROM sync_runs WHERE connector=? AND status<>'running' ORDER BY id DESC LIMIT 1", (ns,)
            )
            last_ok = self.db.one(
                "SELECT finished_at FROM sync_runs WHERE connector=? AND status='ok' ORDER BY id DESC LIMIT 1", (ns,)
            )
            if kind == "instagram":
                account_id = self.db.get_state(ns, "account_id") or ""
                username = self.db.get_state(ns, "username") or ""
                fallback = f"@{username}" if username else f"Account {account.slot}"
            else:
                account_id = username = account.address
                fallback = account.address
            out.append({
                "slot": account.slot,
                "account_id": account_id,
                "username": username,
                "label": account.label,
                "name": account.label or fallback,
                "threads": self.db.scalar(
                    "SELECT COUNT(*) FROM threads WHERE channel=? AND account=? AND is_demo=0", (channel, account_id), 0
                ) if account_id else 0,
                "last_run_at": last["finished_at"] if last else None,
                "last_ok_at": last_ok["finished_at"] if last_ok else None,
                "last_status": last["status"] if last else "never",
                "last_error": last["error"] if last and last["status"] == "error" else "",
                "last_stats": loads(last["stats"], {}) if last else {},
                "token_expires_at": (self.db.get_state(ns, "token_expires_at") or "") if kind == "instagram" else "",
                "via": ("google" if account.signed_in_with_google else "app_password") if kind == "gmail" else "",
            })
        return out

    def known_accounts(self) -> list[dict[str, Any]]:
        """Every account conversations are labelled with, connected or not, with its current name."""
        connected: dict[tuple[str, str], dict[str, Any]] = {}
        for kind in MULTI_ACCOUNT:
            for entry in self.accounts_status(kind):
                if entry["account_id"]:
                    # If one account was added twice, the first slot is the real one.
                    connected.setdefault((CHANNEL_OF[kind], entry["account_id"]), {**entry, "kind": kind})
        out = []
        for row in self.db.all("SELECT channel, key, username, label FROM accounts ORDER BY channel, username, key"):
            live = connected.get((row["channel"], row["key"]))
            label = (live["label"] if live else row["label"]) or ""
            username = (live["username"] if live else "") or row["username"]
            plain = (f"@{username}" if row["channel"] == "instagram" else username) if username else row["key"]
            out.append({
                "channel": row["channel"],
                "id": row["key"],
                "username": username,
                "label": label,
                "name": label or plain,
                "connected": live is not None,
            })
        return out

    def _note_run(self, name: str, status: str) -> None:
        """A marker in the run history that is neither a sync nor a failure: a later one hides an older error."""
        stamp = now_iso()
        self.db.run(
            "INSERT INTO sync_runs(connector, started_at, finished_at, status) VALUES(?,?,?,?)", (name, stamp, stamp, status)
        )

    def linkedin_sign_in(self, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Open LinkedIn's sign-in page in the owner's browser and wait for them to finish there."""
        if not self._locks["linkedin"].acquire(blocking=False):
            raise ConnectorError("LinkedIn is busy. Try again in a moment.")
        try:
            result = linkedin_browser.sign_in(self.settings, self.db, progress)
            self._note_run("linkedin", "signed_in")  # an earlier "needs sign-in" no longer applies
            return result
        finally:
            self._locks["linkedin"].release()

    def linkedin_disconnect(self) -> None:
        """Sign out of LinkedIn here by deleting Dealflow's browser profile. Conversations stay."""
        if not self._locks["linkedin"].acquire(blocking=False):
            raise ConnectorError("LinkedIn is syncing right now. Try again when it has finished.")
        try:
            linkedin_browser.disconnect(self.settings, self.db)
            self._note_run("linkedin", "disconnected")
        finally:
            self._locks["linkedin"].release()

    def record_push(self, name: str, stats: dict[str, Any]) -> None:
        """Bookkeeping for data pushed in from outside (the LinkedIn agent, a CSV import)."""
        stamp = now_iso()
        self.db.run(
            "INSERT INTO sync_runs(connector, started_at, finished_at, status, stats) VALUES(?,?,?,'ok',?)",
            (name, stamp, stamp, dumps(stats)),
        )

    def sync_all(self, progress: Callable[[str], None] | None = None, triage: bool = True,
                 on_demand: bool = False) -> dict[str, Any]:
        """Sync every connected source. `on_demand` is True only when the owner pressed Sync:
        that is the one case in which LinkedIn's browser sync joins in, and only once they connected it."""
        result: dict[str, Any] = {}
        for name in PULLABLE + (ON_DEMAND if on_demand else ()):
            if name == "linkedin":
                if not linkedin_browser.ready(self.settings, self.db):
                    continue
            elif not self.configured(name):
                continue
            try:
                result[name] = self.sync(name, progress)
            except ConnectorError as error:
                result[name] = {"error": str(error)}
        if triage and self.settings.auto_triage:
            result["triage"] = engine.run_pending(self.db, self.settings, self.rubric, progress=progress)
        return result

    def run_triage(self, force: bool = False, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        self.reload_rubric()
        return engine.run_pending(self.db, self.settings, self.rubric, force=force, progress=progress)

    # -- background jobs (for the web UI) -----------------------------------

    def _thread_count(self, name: str) -> int:
        """Real conversations a connector has brought in. Gmail and Proton share the email channel."""
        gmail = [account.address for account in self.settings.gmail_accounts]
        marks = ",".join("?" for _ in gmail)
        if name == "gmail":
            if not gmail:
                return 0
            return self.db.scalar(
                f"SELECT COUNT(*) FROM threads WHERE channel='email' AND is_demo=0 AND account IN ({marks})", gmail, 0
            )
        if name == "email" and gmail:
            return self.db.scalar(
                f"SELECT COUNT(*) FROM threads WHERE channel='email' AND is_demo=0 AND account NOT IN ({marks})", gmail, 0
            )
        return self.db.scalar("SELECT COUNT(*) FROM threads WHERE channel=? AND is_demo=0", (CHANNEL_OF[name],), 0)

    def start_job(self, label: str, work: Callable[[Callable[[str], None]], Any]) -> bool:
        """Run `work` on a thread. One job at a time; returns False if one is already running."""
        with self._job_lock:
            if self.job["running"]:
                return False
            self.job = {"running": True, "label": label, "detail": "", "started_at": now_iso(), "result": None}

        def progress(message: str) -> None:
            self.job["detail"] = message

        def runner() -> None:
            try:
                result = work(progress)
            except Exception as error:  # noqa: BLE001
                result = {"error": str(error)}
            finally:
                self.db.close()
            self.job = {**self.job, "running": False, "detail": "", "result": result, "finished_at": now_iso()}

        threading.Thread(target=runner, name=f"job-{label}", daemon=True).start()
        return True

    def start_scheduler(self) -> None:
        if self._scheduler is not None:
            return

        def loop() -> None:
            # First pass shortly after boot, then on the interval. The interval is
            # re-read every cycle so a change on the Settings page applies at once.
            delay = 5.0
            while not self._stop.wait(delay):
                minutes = self.settings.sync_interval_minutes
                if minutes <= 0:
                    delay = 30.0  # timer off: look again soon in case it is switched on
                    continue
                delay = minutes * 60
                if any(self.configured(name) for name in PULLABLE) or engine.pending_ids(self.db, self.settings, limit=1):
                    self.start_job("Scheduled sync", lambda progress: self.sync_all(progress))
                self.db.close()

        self._scheduler = threading.Thread(target=loop, name="scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()

    # -- status ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        connectors = []
        for name in CONNECTORS:
            last = self.db.one(
                "SELECT * FROM sync_runs WHERE connector=? AND status<>'running' ORDER BY id DESC LIMIT 1", (name,)
            )
            last_ok = self.db.one(
                "SELECT finished_at FROM sync_runs WHERE connector=? AND status='ok' ORDER BY id DESC LIMIT 1", (name,)
            )
            entry: dict[str, Any] = {
                "name": name,
                "configured": self.configured(name),
                "threads": self._thread_count(name),
                "last_run_at": last["finished_at"] if last else None,
                "last_ok_at": last_ok["finished_at"] if last_ok else None,
                "last_status": last["status"] if last else "never",
                "last_error": last["error"] if last and last["status"] == "error" else "",
                "last_stats": loads(last["stats"], {}) if last else {},
                "syncing": self._locks[name].locked(),
            }
            if name == "linkedin":
                collected = parse_iso(self.db.get_state("linkedin", "last_collected_at"))
                entry["last_ok_at"] = self.db.get_state("linkedin", "last_collected_at") or entry["last_ok_at"]
                entry["stale"] = collected is None or (now() - collected).total_seconds() > self.settings.linkedin_stale_hours * 3600
                entry["request_count"] = self.db.get_state("linkedin", "request_count")
                entry["browser"] = linkedin_browser.browser_status(self.settings, self.db)
            if name in MULTI_ACCOUNT:
                # Roll the per-account runs up into the one line the sidebar shows.
                accounts = self.accounts_status(name)
                entry["accounts"] = accounts
                failing = [a for a in accounts if a["last_error"]]
                oks = [a["last_ok_at"] for a in accounts if a["last_ok_at"]]
                runs = [a["last_run_at"] for a in accounts if a["last_run_at"]]
                entry["last_ok_at"] = max(oks) if oks else None
                entry["last_run_at"] = max(runs) if runs else None
                entry["last_status"] = "error" if failing else ("ok" if oks else "never")
                entry["last_error"] = " | ".join(
                    a["last_error"] if len(accounts) == 1 else f"{a['name']}: {a['last_error']}" for a in failing
                )
                entry["last_stats"] = {"new_messages": sum(int(a["last_stats"].get("new_messages") or 0) for a in accounts)}
                entry["username"] = accounts[0]["username"] if accounts else ""
                entry["token_expires_at"] = accounts[0]["token_expires_at"] if accounts else ""
            connectors.append(entry)

        spend = self.db.one(
            "SELECT COALESCE(SUM(cost),0) AS total, COUNT(*) AS calls, "
            "COALESCE(SUM(CASE WHEN created_at >= ? THEN cost END),0) AS month FROM spend",
            (now_iso()[:7] + "-01T00:00:00Z",),
        )
        return {
            "connectors": connectors,
            "job": self.job,
            "triage": {
                "engine": "jev" if self.settings.jev_ready else "rules",
                "model": self.settings.jev_model if self.settings.jev_ready else "",
                "writer": self.settings.writer_model if (self.settings.writer_model and self.settings.jev_ready) else "",
                "pending": len(engine.pending_ids(self.db, self.settings)),
                "last_error": self.db.get_state("triage", "last_error") or "",
                "last_run_at": self.db.get_state("triage", "last_run_at") or "",
            },
            "spend": {"total": spend["total"], "month": spend["month"], "calls": spend["calls"]},
            "demo_threads": self.db.scalar("SELECT COUNT(*) FROM threads WHERE is_demo=1", (), 0),
            "now": now_iso(),
            "uptime_started": _STARTED,
        }


_STARTED = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
