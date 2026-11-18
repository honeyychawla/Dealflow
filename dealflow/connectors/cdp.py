"""A small Chrome DevTools Protocol client. Standard library only.

Drives a Chromium-family browser the owner already has installed, on a profile
folder of its own, through --remote-debugging-pipe: commands and events travel
over two inherited pipes (file descriptors 3 and 4 inside the browser) as JSON
messages, each ended by a NUL byte. No TCP port is opened, so no other program
on the machine can attach to the browser while it runs, and closing our end of
the pipe makes the browser quit.

Only what the LinkedIn collector needs is here: start, navigate, read the
address, list cookie names, and read the bodies of responses the page loaded.
There is deliberately no way to run script in a page or to click or type.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

try:
    import fcntl  # not on Windows, where this way of talking to the browser is not available either
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

MAC_APPS = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Chromium.app/Contents/MacOS/Chromium",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
LINUX_NAMES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser", "microsoft-edge",
)
MAX_BODY = 8 * 1024 * 1024  # a response larger than this is not conversation data


class BrowserError(Exception):
    """The browser could not be started or did not answer."""


class BrowserClosed(BrowserError):
    """The browser went away: its window was closed, or it quit."""


def _executable(path: Path) -> str:
    return str(path) if path.is_file() and os.access(path, os.X_OK) else ""


def find_browser(preferred: str = "") -> str:
    """Path of a Chromium-family browser on this machine, or "" if there is none.

    `preferred` may be the program itself or, on a Mac, its .app bundle.
    """
    if preferred.strip():
        path = Path(preferred.strip()).expanduser()
        if path.is_dir() and path.suffix == ".app":
            inner = path / "Contents" / "MacOS" / path.stem
            return _executable(inner)
        return _executable(path)
    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            for app in MAC_APPS:
                found = _executable(root / app)
                if found:
                    return found
    elif sys.platform.startswith("linux"):
        for name in LINUX_NAMES:
            found = shutil.which(name)
            if found:
                return found
    return ""


def browser_name(path: str) -> str:
    """"Google Chrome" for .../Google Chrome.app/Contents/MacOS/Google Chrome."""
    return Path(path).name if path else ""


class Browser:
    """One running browser, talked to over a pipe. Use as a context manager."""

    def __init__(self, executable: str, profile_dir: Path, *, headless: bool = False,
                 window: tuple[int, int] = (1180, 860)):
        self.executable = executable
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.window = window
        self.proc: subprocess.Popen[bytes] | None = None
        self._write_fd = -1
        self._read_fd = -1
        self._write_lock = threading.Lock()
        self._cond = threading.Condition()
        self._replies: dict[int, dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=50_000)
        self._next_id = 0
        self._closed = False

    def __enter__(self) -> "Browser":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> "Browser":
        if not self.executable:
            raise BrowserError("No browser to start")
        if fcntl is None or sys.platform == "win32":
            raise BrowserError("Browser sync is not available on Windows yet")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile_dir, 0o700)  # it will hold a signed-in session

        to_browser_read, to_browser_write = os.pipe()
        from_browser_read, from_browser_write = os.pipe()
        # The browser wants its ends on descriptors 3 and 4 exactly. Park them on high
        # numbers first so the shell's two redirections cannot overwrite each other.
        child_in = fcntl.fcntl(to_browser_read, fcntl.F_DUPFD, 20)
        child_out = fcntl.fcntl(from_browser_write, fcntl.F_DUPFD, 20)
        os.close(to_browser_read)
        os.close(from_browser_write)

        args = [
            self.executable,
            f"--user-data-dir={self.profile_dir}",
            "--remote-debugging-pipe",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={self.window[0]},{self.window[1]}",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append("about:blank")
        try:
            self.proc = subprocess.Popen(
                ["/bin/sh", "-c", f'exec "$@" 3<&{child_in} 4>&{child_out}', "sh", *args],
                pass_fds=(child_in, child_out),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            for fd in (child_in, child_out, to_browser_write, from_browser_read):
                os.close(fd)
            raise BrowserError(f"Could not start {browser_name(self.executable)}: {error}") from None
        os.close(child_in)
        os.close(child_out)
        self._write_fd = to_browser_write
        self._read_fd = from_browser_read
        threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True).start()
        try:
            self.call("Browser.getVersion", timeout=30)
        except BrowserError:
            self.close()
            raise BrowserError(
                f"{browser_name(self.executable)} did not start for Dealflow. If a Dealflow LinkedIn window is "
                "still open, close it and try again."
            ) from None
        return self

    def _read_loop(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = os.read(self._read_fd, 1 << 16)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buffer += chunk
            while True:
                cut = buffer.find(b"\0")
                if cut == -1:
                    break
                raw, buffer = buffer[:cut], buffer[cut + 1:]
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                with self._cond:
                    if "id" in message:
                        self._replies[message["id"]] = message
                    else:
                        self._events.append(message)
                    self._cond.notify_all()
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def call(self, method: str, params: dict[str, Any] | None = None, *, session: str = "",
             timeout: float = 30.0) -> dict[str, Any]:
        """Send one command and wait for its answer."""
        with self._cond:
            if self._closed:
                raise BrowserClosed("The browser is no longer running")
            self._next_id += 1
            call_id = self._next_id
        message: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        data = json.dumps(message).encode("utf-8") + b"\0"
        with self._write_lock:
            try:
                while data:
                    data = data[os.write(self._write_fd, data):]
            except OSError:
                raise BrowserClosed("The browser is no longer running") from None
        deadline = time.monotonic() + timeout
        with self._cond:
            while call_id not in self._replies:
                if self._closed:
                    raise BrowserClosed("The browser was closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserError(f"The browser did not answer in time ({method})")
                self._cond.wait(remaining)
            reply = self._replies.pop(call_id)
        if "error" in reply:
            raise BrowserError(f"{method}: {(reply['error'] or {}).get('message', 'failed')}")
        return reply.get("result") or {}

    def take_events(self, wait: float = 0.0) -> list[dict[str, Any]]:
        """Everything the browser reported since the last call; waits up to `wait` seconds if nothing has."""
        with self._cond:
            if not self._events and wait > 0 and not self._closed:
                self._cond.wait(wait)
            events = list(self._events)
            self._events.clear()
        return events

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if not self._closed:
                self.call("Browser.close", timeout=5)
        except BrowserError:
            pass
        if self._write_fd != -1:
            try:
                os.close(self._write_fd)  # the browser quits when its command pipe closes
            except OSError:
                pass
            self._write_fd = -1
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self._read_fd != -1:
            try:
                os.close(self._read_fd)
            except OSError:
                pass
            self._read_fd = -1


class Page:
    """The browser's tab. Reads only: where it is, which cookies exist, what it loaded."""

    def __init__(self, browser: Browser, target_id: str, session: str):
        self.browser = browser
        self.target_id = target_id
        self.session = session

    @classmethod
    def first(cls, browser: Browser) -> "Page":
        targets = browser.call("Target.getTargets").get("targetInfos") or []
        page = next((t for t in targets if t.get("type") == "page"), None)
        target_id = page["targetId"] if page else browser.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = browser.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        return cls(browser, target_id, session)

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        return self.browser.call(method, params, session=self.session, timeout=timeout)

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def url(self) -> str:
        info = self.browser.call("Target.getTargetInfo", {"targetId": self.target_id}).get("targetInfo") or {}
        return str(info.get("url") or "")

    def cookie_names(self, url: str) -> set[str]:
        """Names only. The values are the owner's session and are never read out."""
        cookies = self.call("Network.getCookies", {"urls": [url]}).get("cookies") or []
        return {str(cookie.get("name") or "") for cookie in cookies}

    def response_body(self, request_id: str) -> str:
        result = self.call("Network.getResponseBody", {"requestId": request_id})
        body = result.get("body") or ""
        if result.get("base64Encoded"):
            if len(body) > MAX_BODY * 2:
                return ""
            return base64.b64decode(body).decode("utf-8", "replace")
        return body if len(body) <= MAX_BODY else ""
