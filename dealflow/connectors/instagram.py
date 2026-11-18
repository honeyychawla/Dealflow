"""Instagram DMs through Meta's official Conversations API. Read-only: this never sends.

Two kinds of token work, and the connector tells them apart by prefix:

  * "IGAA..." from "Instagram API with Instagram Login" (graph.instagram.com).
    No Facebook Page needed; the app dashboard issues a 60-day token that
    this connector keeps refreshed. The simplest route.
  * "EAA..." on graph.facebook.com, in either of two setups:
      - Unified Login: a Business Integration System User token, used with the
        Instagram account id as the node (/<ig-account-id>/conversations).
        Needs instagram_basic and instagram_manage_messages, plus the account
        id in INSTAGRAM_USER_ID. No Facebook Page involved.
      - Facebook Login: DMs are read through the Facebook Page linked to the
        Instagram account, which the connector finds by itself. Also needs
        pages_show_list and pages_manage_metadata.

Facts this code is built around (Meta docs, Graph API v27.0, Nov 2026):
  * GET /me/conversations?platform=instagram lists threads, newest first.
  * GET /<conversation>?fields=messages lists message ids; details are only
    readable for the 20 most recent messages of a conversation. So we store
    everything we see, and history accumulates locally from the first sync on.
  * The Conversations API allows about 2 calls per second per account.
  * Reading through the API does not mark anything as seen.
  * Group chats are not returned, and folders (Primary/General/Requests)
    cannot be told apart.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any, Callable

from ..config import InstagramAccount, Settings
from ..db import DB
from ..ingest import Message, Participant, Thread, remember_account, upsert_thread
from ..net import explain_tls_failure, ssl_context
from ..util import loads, norm_iso, now, now_iso, parse_iso, sha, to_iso
from . import ConnectorError, NotConfigured

NAME = "instagram"
MIN_INTERVAL = 0.55  # seconds between calls: stays under 2 calls/second
THROTTLE_CODES = {4, 17, 32, 613, 80002}
RICH_FIELDS = "id,created_time,from,to,message,attachments,shares,story,reactions,is_unsupported"
BASIC_FIELDS = "id,created_time,from,to,message"
PROFILE_FIELDS = (
    "name,username,follower_count,is_verified_user,is_user_follow_business,is_business_follow_user"
)
REFRESH_EVERY_DAYS = 7
PROFILE_TTL_DAYS = 14

Progress = Callable[[str], None]


class GraphError(ConnectorError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


# Facebook-side permission names. (Instagram Login uses instagram_business_* instead.)
UNIFIED_PERMISSIONS = ("instagram_basic", "instagram_manage_messages")
PAGE_PERMISSIONS = ("pages_show_list", "pages_manage_metadata")
PAGE_FIELDS = "id,name,access_token,instagram_business_account{id,username}"
# fb_page_id holds the node the conversations hang off: an Instagram account id
# (Unified Login) or a Facebook Page id (Facebook Login).
FB_STATE_KEYS = ("fb_page_id", "fb_page_token", "fb_page_name", "fb_ig_id", "fb_ig_username", "fb_route", "fb_wanted")


def token_kind(token: str) -> str:
    """Which Meta API a token belongs to, read from its prefix.

    Instagram Login tokens start with "IG" and are used on graph.instagram.com.
    Facebook-side tokens start with "EAA" and are used on graph.facebook.com:
    Business Integration System User tokens from Unified Login, and user or
    Page tokens from Facebook Login. Sending one kind to the other host fails
    with "Cannot parse access token".
    """
    if token.startswith("IG"):
        return "instagram"
    if token.startswith("EAA"):
        return "facebook"
    return "unknown"


class Instagram:
    def __init__(self, settings: Settings, db: DB, account: InstagramAccount | None = None):
        self.settings = settings
        self.db = db
        if account is None:  # single-account use: the first configured account
            configured = settings.instagram_accounts
            account = configured[0] if configured else InstagramAccount(1, "", settings.instagram_user_id)
        self.account = account
        self.ns = account.namespace  # sync-state namespace: one per account, so accounts never share a cursor
        self.account_id = ""  # the Instagram account's own id, known after identify()
        self._prepared = False
        self._last_call = 0.0
        self._token = ""
        self._host = settings.instagram_api_host
        self._node_id = ""  # Instagram account id (Unified Login) or Page id (Facebook Login)
        self._me = {"id": "", "username": ""}
        self.page_name = ""
        self.route = "instagram"  # instagram | unified | facebook
        self.calls = 0

    # -- HTTP -------------------------------------------------------------

    @property
    def base(self) -> str:
        return f"https://{self._host}/{self.settings.instagram_api_version}"

    @property
    def on_facebook(self) -> bool:
        return self._host != "graph.instagram.com"

    def _get(self, path_or_url: str, params: dict[str, Any] | None = None, attempts: int = 4) -> Any:
        if path_or_url.startswith("https://"):
            url = path_or_url  # a `paging.next` link, which already carries the token
        else:
            query = dict(params or {})
            query["access_token"] = self._token
            url = f"{self.base}/{path_or_url.lstrip('/')}?{urllib.parse.urlencode(query)}"
        error: GraphError | None = None
        for attempt in range(attempts):
            wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                with urllib.request.urlopen(url, timeout=30, context=ssl_context()) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as http_error:
                error = _graph_error(http_error)
                if error.code in THROTTLE_CODES or (error.status or 0) >= 500:
                    time.sleep(min(60, 5 * (2**attempt)))
                    continue
                raise error from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as net_error:
                tls = explain_tls_failure(net_error)
                if tls:  # retrying cannot fix a certificate problem
                    raise GraphError(tls) from None
                # str(net_error) never contains the URL, so the token cannot leak into this message
                error = GraphError(f"network error talking to Instagram: {net_error}")
                time.sleep(2 * (attempt + 1))
        assert error is not None
        raise error

    def _try(self, path: str, params: dict[str, Any]) -> Any:
        """A lookup that is allowed to fail (missing permission, wrong node type). Dead tokens still raise."""
        try:
            return self._get(path, params, attempts=2)
        except GraphError as error:
            if error.code == 190:
                raise
            return {}

    # -- token ------------------------------------------------------------

    def _load_token(self) -> None:
        """Use the saved token (or the refreshed copy of it) and pick the API host that matches it."""
        configured = self.account.token
        if not configured:
            raise NotConfigured("No access token is saved for this Instagram account")
        kind = token_kind(configured)
        if kind == "instagram":
            self._host = "graph.instagram.com"
        elif kind == "facebook":
            self._host = "graph.facebook.com"
        else:
            self._host = self.settings.instagram_api_host
        fingerprint = sha(configured)
        if self.db.get_state(self.ns, "token_source") == fingerprint:
            self._token = self.db.get_state(self.ns, "token") or configured
        else:  # a new token was saved: forget everything derived from the old one
            self._token = configured
            self.db.set_state(self.ns, "token_source", fingerprint)
            self.db.set_state(self.ns, "token", configured)
            self.db.set_state(self.ns, "token_refreshed_at", now_iso())
            self.db.set_state(self.ns, "token_expires_at", "")
            self.db.set_state(self.ns, "fields_mode", "rich")
            for key in FB_STATE_KEYS:
                self.db.set_state(self.ns, key, "")

    def _maybe_refresh_token(self) -> None:
        """Long-lived Instagram Login tokens last 60 days and can be renewed once a day old."""
        if self.on_facebook:
            return  # Facebook tokens are not renewed this way
        refreshed = parse_iso(self.db.get_state(self.ns, "token_refreshed_at"))
        if refreshed and (now() - refreshed).days < REFRESH_EVERY_DAYS:
            return
        url = "https://graph.instagram.com/refresh_access_token?" + urllib.parse.urlencode(
            {"grant_type": "ig_refresh_token", "access_token": self._token}
        )
        try:
            payload = self._get(url)
        except GraphError:
            return  # too young to refresh, or already expired: the sync itself will report it
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if token:
            self._token = token
            self.db.set_state(self.ns, "token", token)
            self.db.set_state(self.ns, "token_refreshed_at", now_iso())
            expires_in = payload.get("expires_in")
            if isinstance(expires_in, (int, float)):
                self.db.set_state(self.ns, "token_expires_at", to_iso(now() + timedelta(seconds=float(expires_in)))
                )

    # -- who am I ---------------------------------------------------------

    def _prepare(self) -> dict[str, str]:
        """Load the token, route to the API it belongs to, and identify the Instagram account."""
        self._load_token()
        if self.on_facebook:
            me = self._resolve_facebook()
        else:
            self.route = "instagram"
            self._maybe_refresh_token()
            data = self._get("me", {"fields": "user_id,username"})
            data = data if isinstance(data, dict) else {}
            me = {"id": str(data.get("user_id") or data.get("id") or ""), "username": str(data.get("username") or "")}
        previous = self.db.get_state(self.ns, "username") or ""
        if me["username"]:
            if previous and previous.lower() != me["username"].lower():
                self.db.set_state(self.ns, "updated_cursor", "")  # a different account: sync it from the start
            self.db.set_state(self.ns, "username", me["username"])
        account_id = me["id"] or me["username"]
        if not account_id:
            raise ConnectorError("Meta did not say which Instagram account this token belongs to.")
        self._me = me
        self.account_id = account_id
        self.db.set_state(self.ns, "account_id", account_id)
        remember_account(self.db, NAME, account_id, me["username"], self.account.label)
        self._adopt_orphans(me)
        self._prepared = True
        return me

    def identify(self) -> dict[str, str]:
        """Work out which Instagram account this is (id and username). Cached for the life of this object."""
        return self._me if self._prepared else self._prepare()

    @property
    def display(self) -> str:
        """What to call this account in messages to the owner."""
        if self.account.label:
            return self.account.label
        return f"@{self._me['username']}" if self._me["username"] else f"account {self.account.slot}"

    def _adopt_orphans(self, me: dict[str, str]) -> None:
        """Finish the one-time labelling of conversations synced before several accounts existed.

        db.py does this itself when it knows the old account's id. For an Instagram
        Login setup it only knew the username, so it left a note here; the
        conversations are adopted only if this token is still that same account.
        """
        pending = self.db.get_state(self.ns, "adopt_orphans_for")
        if not pending:
            return
        if pending.lower() == (me["username"] or "").lower():
            self.db.run(
                "UPDATE threads SET account=? WHERE channel='instagram' AND account='' AND is_demo=0",
                (self.account_id,),
            )
        self.db.set_state(self.ns, "adopt_orphans_for", "")

    def _resolve_facebook(self) -> dict[str, str]:
        """graph.facebook.com: work out which node the DMs hang off, and which token reads them.

        Two setups use this host, and both are handled:
          * Unified Login. The token is a Business Integration System User token
            and the node is the Instagram account itself: /<ig-account-id>/conversations.
            The account id has to be given (Settings > Instagram), because a
            system-user token has no "me" to discover it from.
          * Facebook Login. The node is the Facebook Page linked to the Instagram
            account, read with that Page's own token, which is found automatically.
        """
        saved_token = self.account.token
        wanted = self.account.user_id.strip()
        cached_node = self.db.get_state(self.ns, "fb_page_id")
        if cached_node and (not wanted or wanted == cached_node or self.db.get_state(self.ns, "fb_wanted") == wanted):
            self._node_id = cached_node
            self._token = self.db.get_state(self.ns, "fb_page_token") or saved_token
            self.page_name = self.db.get_state(self.ns, "fb_page_name") or ""
            self.route = self.db.get_state(self.ns, "fb_route") or "facebook"
            try:
                self._get(cached_node, {"fields": "id"}, attempts=2)
                return {
                    "id": self.db.get_state(self.ns, "fb_ig_id") or "",
                    "username": self.db.get_state(self.ns, "fb_ig_username") or "",
                }
            except GraphError as error:
                if error.code != 190:
                    raise
                # the derived Page token expired: fall through and derive it again
        for key in FB_STATE_KEYS:
            self.db.set_state(self.ns, key, "")

        self._token = saved_token
        pages_seen = 0
        chosen: dict[str, Any] | None = None

        # 1. Unified Login: the id in Settings is the Instagram account. media_count
        #    exists only on Instagram accounts, so it tells an account from a Page.
        if wanted:
            account = self._try(wanted, {"fields": "id,username,media_count"})
            if isinstance(account, dict) and account.get("username") and "media_count" in account:
                self.route = "unified"
                chosen = {"id": wanted, "name": "", "access_token": saved_token,
                          "instagram_business_account": {"id": str(account.get("id") or wanted), "username": account["username"]}}

        if chosen is None:
            self.route = "facebook"
            candidates: list[dict[str, Any]] = []
            # 2. The token may itself be a Page token, in which case "me" is the Page.
            me = self._try("me", {"fields": "id,name,instagram_business_account{id,username}"})
            if isinstance(me, dict) and me.get("instagram_business_account"):
                candidates = [{**me, "access_token": saved_token}]
            # 3. Otherwise it may be a user token: list the Pages it can act for.
            if not candidates:
                listing = self._try("me/accounts", {"fields": PAGE_FIELDS, "limit": 100})
                pages = (listing.get("data") or []) if isinstance(listing, dict) else []
                pages_seen = len(pages)
                candidates = [page for page in pages if page.get("instagram_business_account")]
            # 4. An id in Settings that is a Page picks one of several, or reaches an unlisted Page.
            if wanted:
                matching = [page for page in candidates if str(page.get("id")) == wanted]
                if not matching:
                    page = self._try(wanted, {"fields": PAGE_FIELDS})
                    if isinstance(page, dict) and page.get("instagram_business_account"):
                        matching = [page]
                candidates = matching
            if not candidates:
                raise ConnectorError(self._explain_unreachable(pages_seen, wanted))
            chosen = candidates[0]

        account = chosen.get("instagram_business_account") or {}
        self._node_id = str(chosen.get("id") or "")
        self._token = str(chosen.get("access_token") or saved_token)
        self.page_name = str(chosen.get("name") or "")
        me = {"id": str(account.get("id") or ""), "username": str(account.get("username") or "")}
        for key, value in (("fb_page_id", self._node_id), ("fb_page_token", self._token), ("fb_page_name", self.page_name),
                           ("fb_ig_id", me["id"]), ("fb_ig_username", me["username"]), ("fb_route", self.route),
                           ("fb_wanted", wanted)):
            self.db.set_state(self.ns, key, value)
        return me

    def _explain_unreachable(self, pages_seen: int, wanted: str) -> str:
        """Say exactly why a Facebook-side token cannot reach the DMs, and the quickest way out."""
        listing = self._try("me/permissions", {})
        granted = {
            item.get("permission")
            for item in ((listing.get("data") or []) if isinstance(listing, dict) else [])
            if item.get("status") == "granted"
        }
        missing_messaging = [name for name in UNIFIED_PERMISSIONS if name not in granted]
        missing_pages = [name for name in PAGE_PERMISSIONS if name not in granted]
        parts = ["This is a Facebook-side access token (it starts with EAA), so it is used on graph.facebook.com."]
        if granted and missing_messaging:
            parts.append("It is missing the permission(s) needed to read Instagram DMs: " + ", ".join(missing_messaging) + ".")
        if wanted:
            parts.append(
                f"The ID {wanted} could not be read with it, either as an Instagram account or as a Facebook Page. "
                "Check the ID, and that the token was generated for that Instagram account."
            )
        else:
            parts.append(
                "No Instagram account ID is set. If your Meta app uses “API setup with Unified login”, enter your "
                "Instagram account ID above (the number starting with 1784 shown next to your account in the app "
                "dashboard), then test again."
            )
            if pages_seen:
                parts.append(f"The token can see {pages_seen} Facebook Page(s), but none has an Instagram professional account linked.")
            elif missing_pages:
                parts.append(
                    "Without that ID the app looks for a linked Facebook Page instead, which this token cannot list "
                    "(it lacks " + ", ".join(missing_pages) + ")."
                )
        return " ".join(parts)

    def whoami(self) -> dict[str, str]:
        return self.identify()

    def _conversations_path(self) -> str:
        return f"{self._node_id}/conversations" if self.on_facebook else "me/conversations"

    def _fetch_messages(self, conversation_id: str, external_id: str | None = None) -> list[dict[str, Any]]:
        """The readable (most recent 20) messages of a conversation, with details.

        `external_id` is the id the thread is stored under, when that differs from
        Meta's conversation id (see _thread_external_id).
        """
        mode = self.db.get_state(self.ns, "fields_mode", "rich")
        if mode in ("rich", "basic"):
            fields = RICH_FIELDS if mode == "rich" else BASIC_FIELDS
            try:
                payload = self._get(conversation_id, {"fields": f"messages.limit(20){{{fields}}}"})
                return list((payload.get("messages") or {}).get("data") or [])
            except GraphError as error:
                if error.code != 100:  # 100 = invalid parameter / unknown field
                    raise
                # Step down: rich fields -> basic fields -> one call per message.
                self.db.set_state(self.ns, "fields_mode", "basic" if mode == "rich" else "single")
                return self._fetch_messages(conversation_id, external_id)

        payload = self._get(conversation_id, {"fields": "messages"})
        listed = list((payload.get("messages") or {}).get("data") or [])[:20]
        thread = self.db.one(
            "SELECT id FROM threads WHERE channel='instagram' AND external_id=?", (external_id or conversation_id,)
        )
        known = set()
        if thread is not None:
            known = {
                r["external_id"]
                for r in self.db.all("SELECT external_id FROM messages WHERE thread_id=?", (thread["id"],))
            }
        out = []
        for item in listed:
            if item.get("id") in known:
                continue
            try:
                out.append(self._get(item["id"], {"fields": BASIC_FIELDS}))
            except GraphError:
                continue  # older than the 20-message window, or deleted
        return out

    def _profile(self, igsid: str) -> dict[str, Any]:
        try:
            return self._get(igsid, {"fields": PROFILE_FIELDS})
        except GraphError:
            return {}  # no consent yet, blocked, or field not available: not fatal

    # -- sync -------------------------------------------------------------

    def sync(self, progress: Progress | None = None) -> dict[str, Any]:
        say = progress or (lambda _message: None)
        me = self.identify()
        cursor = parse_iso(self.db.get_state(self.ns, "updated_cursor"))
        newest = cursor
        stats = {"conversations_seen": 0, "conversations_changed": 0, "new_messages": 0, "profiles": 0}

        url: str | None = None
        params = {"platform": "instagram", "fields": "id,updated_time,participants", "limit": 25}
        done = False
        stale_streak = 0
        while not done:
            page = self._get(url, None) if url else self._get(self._conversations_path(), params)
            for conversation in page.get("data") or []:
                stats["conversations_seen"] += 1
                updated = parse_iso(conversation.get("updated_time"))
                # The list comes newest first, so a run of threads untouched since the
                # last sync means we have caught up. A run rather than the first one,
                # because the ordering is not something Meta documents as guaranteed.
                if cursor and updated and updated <= cursor:
                    stale_streak += 1
                    if stale_streak >= 5:
                        done = True
                        break
                    continue
                stale_streak = 0
                if stats["conversations_seen"] > self.settings.instagram_max_conversations:
                    done = True
                    break
                added = self._sync_conversation(conversation, me, stats)
                if added:
                    stats["conversations_changed"] += 1
                    stats["new_messages"] += added
                if updated and (newest is None or updated > newest):
                    newest = updated
                if stats["conversations_seen"] % 10 == 0:
                    say(f"Instagram {self.display}: {stats['conversations_seen']} conversations checked")
            url = (page.get("paging") or {}).get("next")
            if not url:
                done = True

        if newest is not None:
            self.db.set_state(self.ns, "updated_cursor", to_iso(newest))
        stats["api_calls"] = self.calls
        return stats

    def _sync_conversation(self, conversation: dict[str, Any], me: dict[str, str], stats: dict[str, Any]) -> int:
        conversation_id = str(conversation["id"])
        external_id = self._thread_external_id(conversation_id)
        people = (conversation.get("participants") or {}).get("data") or []
        other = next((p for p in people if not _is_me(p, me)), None)
        raw_messages = self._fetch_messages(conversation_id, external_id)
        if other is None:
            # participants was not returned: recover the other side from the messages
            for raw in raw_messages:
                sender = raw.get("from") or {}
                if sender and not _is_me(sender, me):
                    other = sender
                    break
        if other is None:
            return 0  # only my own messages are visible; nothing to triage yet

        igsid = str(other.get("id") or "")
        username = str(other.get("username") or "")
        meta: dict[str, Any] = {}
        name = ""
        if self.settings.instagram_enrich_profiles and igsid and self._profile_is_stale(igsid):
            profile = self._profile(igsid)
            if profile:
                stats["profiles"] += 1
                name = str(profile.get("name") or "")
                username = str(profile.get("username") or username)
                meta = {
                    "followers": profile.get("follower_count"),
                    "verified": profile.get("is_verified_user"),
                    "follows_me": profile.get("is_user_follow_business"),
                    "i_follow": profile.get("is_business_follow_user"),
                }
            meta["profile_checked_at"] = now_iso()

        messages = [self._to_message(raw, me) for raw in raw_messages if raw.get("id")]
        thread = Thread(
            channel=NAME,
            external_id=external_id,
            account=self.account_id,
            participant=Participant(
                key=igsid or username,
                name=name,
                handle=f"@{username}" if username else "",
                url=f"https://www.instagram.com/{username}/" if username else "",
                meta=meta,
            ),
            messages=messages,
            url=f"https://ig.me/m/{username}" if username else "https://www.instagram.com/direct/inbox/",
        )
        return upsert_thread(self.db, thread).new_messages

    def _thread_external_id(self, conversation_id: str) -> str:
        """The id a conversation is stored under.

        Normally Meta's conversation id. If another of the owner's accounts already
        holds a thread with that id (two of their own accounts messaging each other
        is the one way that can happen), this account's copy gets its own row, so
        the two sides are never merged into one thread.
        """
        row = self.db.one(
            "SELECT account FROM threads WHERE channel='instagram' AND external_id=?", (conversation_id,)
        )
        if row is not None and row["account"] and row["account"] != self.account_id:
            return f"{conversation_id}@{self.account_id}"
        return conversation_id

    def _profile_is_stale(self, igsid: str) -> bool:
        row = self.db.one("SELECT meta FROM identities WHERE channel='instagram' AND key=?", (igsid,))
        if row is None:
            return True
        checked = parse_iso((loads(row["meta"], {}) or {}).get("profile_checked_at"))
        return checked is None or (now() - checked).days >= PROFILE_TTL_DAYS

    def _to_message(self, raw: dict[str, Any], me: dict[str, str]) -> Message:
        sender = raw.get("from") or {}
        attachments = _attachments(raw)
        text = str(raw.get("message") or "")
        if not text and raw.get("is_unsupported"):
            text = "[unsupported message type]"
        elif not text and not attachments:
            text = "[no text: media, reaction or story not exposed by the API]"
        meta: dict[str, Any] = {}
        reactions = (raw.get("reactions") or {}).get("data") or []
        if reactions:
            meta["reactions"] = [r.get("reaction") for r in reactions if r.get("reaction")]
        return Message(
            external_id=str(raw["id"]),
            direction="out" if _is_me(sender, me) else "in",
            sent_at=norm_iso(raw.get("created_time")),
            text=text,
            sender_name="",
            sender_handle=f"@{sender.get('username')}" if sender.get("username") else "",
            attachments=attachments,
            meta=meta,
        )

    # -- diagnostics ------------------------------------------------------

    def check(self) -> dict[str, Any]:
        """A cheap live test used by `./dealflow.sh doctor`."""
        me = self.identify()
        page = self._get(self._conversations_path(), {"platform": "instagram", "fields": "id,updated_time", "limit": 5})
        return {
            "username": me["username"],
            "account_id": self.account_id,
            "sample_conversations": len((page.get("data") or []) if isinstance(page, dict) else []),
            "route": self.route,
            "page": self.page_name,
        }


def _is_me(person: dict[str, Any], me: dict[str, str]) -> bool:
    if me.get("username") and person.get("username"):
        return str(person["username"]).lower() == me["username"].lower()
    return bool(me.get("id")) and str(person.get("id")) == me["id"]


def _attachments(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the several shapes Meta uses into [{type, url?}]. Media is never downloaded."""
    out: list[dict[str, Any]] = []
    for item in (raw.get("attachments") or {}).get("data") or []:
        if item.get("image_data"):
            out.append({"type": "image", "url": item["image_data"].get("url", "")})
        elif item.get("video_data"):
            out.append({"type": "video", "url": item["video_data"].get("url", "")})
        elif item.get("file_url"):
            out.append({"type": "file", "url": item["file_url"]})
        else:
            out.append({"type": item.get("mime_type") or "attachment"})
    for item in (raw.get("shares") or {}).get("data") or []:
        kind = {"ig_reel": "reel", "ig_post": "post"}.get(str(item.get("type")), "share")
        out.append({"type": kind, "url": item.get("link", "")})
    story = raw.get("story") or {}
    if story.get("mention"):
        out.append({"type": "story mention"})  # CDN link dies with the story; not stored
    if story.get("reply_to"):
        out.append({"type": "story reply"})
    return out


def _graph_error(http_error: urllib.error.HTTPError) -> GraphError:
    try:
        body = json.loads(http_error.read().decode("utf-8")).get("error", {})
    except Exception:  # noqa: BLE001
        body = {}
    finally:
        http_error.close()
    code = body.get("code")
    message = str(body.get("message") or http_error.reason)
    if code == 190:
        message = (
            "Meta says this token is invalid or expired. Generate a new one in the Meta app dashboard "
            "(Instagram > API setup with Instagram login) and paste it under Settings > Instagram. "
            f"[{message}]"
        )
    elif code in (10, 200):
        message = (
            "Instagram refused access to messages. Check the token has "
            "instagram_business_manage_messages, and that 'Allow access to messages' is on in the "
            f"Instagram app (Settings > Messages and story replies > Message controls). [{message}]"
        )
    return GraphError(message, code=code if isinstance(code, int) else None, status=http_error.code)
