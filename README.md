# Dealflow Triage

One local inbox for Instagram DMs, Gmail, Proton email and LinkedIn DMs, sorted by
[Jev](https://openrouter.ai/docs/guides/community/jev) so the conversations
worth your time are at the top and the rest stay out of the way.

It runs entirely on your machine, needs only Python 3.11+ (no packages to
install), stores everything in one SQLite file, and **only reads**: it never
sends a message or changes anything in your accounts.

## Start

Get the code (download it from GitHub, or `git clone` the repository), open a
terminal in its folder, and run:

```bash
./dealflow.sh
```

Open http://127.0.0.1:4747 and go to **Settings**. Each section has the
fields, a setup guide, and a **Test connection** button. To look around
first, press **Load sample conversations** on the empty screen.

Everything on the Settings page is saved to `.env` (created for you, readable
only by you), so editing that file by hand works just as well. See
`.env.example` for every key.

**You connect your own accounts.** This repository contains no one's
credentials or messages: `.env` (keys, tokens, sign-ins) and `data/` (the
message database and the LinkedIn browser profile) stay on the computer they
were created on and are never committed. Each person who runs the app adds
their own OpenRouter key, Instagram token, Google sign-in and LinkedIn
sign-in under Settings, and sees their own inbox.

## What connects to what

| Channel | How | What you need |
|---|---|---|
| Sorting | Jev via OpenRouter's Decisions API | An OpenRouter API key |
| Instagram (several accounts) | Meta's official Conversations API | A professional account and a token from a Meta app, per account |
| Gmail (several accounts) | **Sign in with Google** (Gmail API, read-only scope), or IMAP with an app password | Your own Google OAuth client, once; then one click per account. Or an app password per account |
| Proton Mail | Proton Mail Bridge, IMAP on localhost | Bridge running, its password and exported `cert.pem` |
| LinkedIn | Your own browser, each time you press Sync (or Claude, or LinkedIn's data export) | Chrome, Chromium, Brave or Edge; you sign in once on LinkedIn's own page |

### Several accounts

Instagram and Gmail each take up to eight accounts. In Settings, open the
section and press **Add another account**; every account has its own fields,
its own **Test**, and its own status line. What that means in the inbox:

- Each conversation is labelled with the account that received it
  ("to @brand", "to work@you.com"), and an **Account** filter appears once
  you have more than one.
- Accounts sync independently. Each keeps its own position, and one failing
  (an expired token, a revoked password) never stops the others.
- A person is one contact however many of your accounts they write to, and
  that conversation is flagged "Wrote to 2 of your accounts". Jev is told
  which account a message arrived on.
- Removing an account stops syncing it. Its conversations stay, still
  labelled.
- Adding the same account twice is refused rather than synced twice.

In `.env`, account 1 uses the plain names (`INSTAGRAM_ACCESS_TOKEN`,
`GMAIL_ADDRESS`) and further accounts are numbered
(`INSTAGRAM_2_ACCESS_TOKEN`, `GMAIL_2_ADDRESS`, ...).

### Sorting with Jev

Jev is a decision model, not a chat model: it is asked typed questions and
returns probabilities, with no prose and no explanation. For each
conversation the app sends one request with nine questions (category, real
opportunity, deal value, sender credibility, urgency, written for you, reply
expected, states terms, scam risk) and turns the answers into:

- a **category** (Brand deal, Investment, Partnership, Customer lead,
  Speaking & media, Vendor pitch, Jobs & talent, Community, Personal, Noise);
- a **score** from 0 to 100 and a **priority**: Act now, This week, Worth a
  look, Low;
- a **Needs review** flag when Jev was unsure about a call that would change
  what you do. Because Jev does not explain itself, uncertain calls go to you
  instead of being filed silently.

The whole policy lives in `rubric.json` and is editable under
Settings → Sorting: who you are, how much each category is worth, the score
needed for each priority, and the wording Jev uses to recognise each
category. Press **Re-sort everything** after changing it.

Cost is roughly $0.00002 per conversation at current pricing (input tokens
only). Newsletters and automated email are filed as Noise without a call.
Until a key is saved, a keyword fallback sorts instead and is labelled
"keyword rules"; everything is re-sorted by Jev once the key is added.

Jev cannot write, so one-line briefs and reply drafts need a separate chat
model: set **Writer model** to any slug from openrouter.ai/models. Without
it, the summary is the opening message and there are no drafts.

### Instagram

1. The account must be a professional (creator or business) account.
2. At developers.facebook.com create a Business app, add the Instagram
   product, and open *Instagram → API setup*. What you do next depends on
   which setup your app offers; the app tells the token types apart by prefix.

   | Your app shows | Token | Also needed | Permissions |
   |---|---|---|---|
   | API setup with **Unified login** | starts with `EAA` (Business Integration System User token) | your **Instagram account ID** (the number starting with `1784`) | `instagram_basic`, `instagram_manage_messages` |
   | API setup with **Instagram login** | starts with `IGAA`, renewed weekly by the app | nothing | `instagram_business_basic`, `instagram_business_manage_messages` |
   | Facebook Login (Graph API Explorer) | starts with `EAA` (user or Page token) | nothing; the linked Page is found automatically | the two `instagram_*` ones plus `pages_show_list`, `pages_manage_metadata` |

3. Paste the token (and the account ID for Unified login) in
   Settings → Instagram, then **Save and test**.

Unified login is the route verified against a live account: calls go to
`graph.facebook.com/<instagram-account-id>/conversations`.
4. In the Instagram app: Settings → Messages and story replies → Message
   controls → Connected tools → **Allow access to messages**.

Limits that come from Meta, not this app: only the 20 most recent messages of
a conversation are readable (history builds up locally from your first sync
on), group chats are not returned, and Primary/General/Requests cannot be
told apart.

**If no conversations come back:** Meta's docs conflict on whether an app in
Development mode can read DMs from people who have no role on the app. Test
connection reports this case. The fix is Advanced Access for
`instagram_business_manage_messages` (App Review plus Business Verification).

### Gmail

Each Gmail account connects one of two ways. **Sign in with Google** is the
recommended one.

| | Sign in with Google | App password |
|---|---|---|
| What it can do | Read mail, nothing else (`gmail.readonly`) | Everything: read, delete, send |
| What is stored in `.env` | A refresh token for that one permission | A password to the whole mailbox |
| How to withdraw it | myaccount.google.com/permissions, or Remove account here (also withdraws it at Google) | Delete it at myaccount.google.com/apppasswords |
| Can a Workspace admin block it | Only by blocking the OAuth client | Yes, app passwords can be switched off |
| Setup | Your own OAuth client in Google Cloud, once | 2 minutes per account |

**Sign in with Google.** Once:

1. In [Google Cloud](https://console.cloud.google.com), pick or create a
   project and enable the **Gmail API** for it (APIs & Services → Library).
2. Set up its OAuth consent screen (Google Auth Platform → Branding, then
   Audience). On a Workspace domain choose **Internal**: only your domain can
   sign in and nothing needs review. For personal Gmail choose **External**
   and add yourself as a test user. While such an app is in Testing, Google
   ends the sign-in after 7 days and you press Sign in with Google again.
3. Under Clients (or Credentials) create an OAuth client of type **Desktop
   app**. In Settings → Gmail, paste its client ID and secret into the two
   Google fields and save.

Then for each account press **Sign in with Google**, pick the account and
allow "Read your email". Google sends you back to the app with that account
connected. The address comes from Google, so nothing has to be typed in. If
that account had an app password, it is removed from `.env` (delete it at
Google too). A Web application client also works if you register the
redirect URI shown in Settings: `http://127.0.0.1:4747/oauth/google/callback`.

How it works: OAuth 2.0 for local apps with PKCE and a one-time state (RFC
8252). The refresh token is kept in `.env` (mode 600) and is never sent to
the page. Access tokens stay in memory. Sync uses the Gmail API (over IMAP,
Google only accepts its full-access scope), and reads only message text and
attachment names, never attachment contents.

**App password.** Turn on 2-Step Verification, create one at
https://myaccount.google.com/apppasswords, then enter the address and its 16
characters in Settings → Gmail and **Save and test**. The account is then read
over IMAP. App passwords are not available when a Workspace admin has switched
them off, with Advanced Protection, or when 2-Step Verification uses security
keys only. An app password grants full mail access to whoever holds it. It is
kept in `.env`, readable only by you, and used only to read.

Both ways behave the same in the inbox. Mail is never marked as read or
changed. The app reads the inbox and your sent mail, so it knows what you
already answered. **Only the Primary tab** skips Promotions, Social, Updates
and Forums using Gmail's own categories. With it off, everything is read and
newsletters are filed as Noise without a Jev call. "Open in Gmail" takes you
to the conversation in the right account.

### Proton Mail (Bridge)

1. Install Bridge (paid Proton plan), sign in, let its first sync finish.
2. From the account's *Mailbox details* copy the username, IMAP port and
   **Bridge password** (not your Proton password) into Settings → Proton Mail.
3. In Bridge: Settings → Advanced settings → **Export TLS certificates**, and
   enter the path to `cert.pem`. The app trusts that certificate and nothing
   else, so another local program cannot pose as Bridge and collect the
   password.

The app opens mailboxes read-only and never marks mail as read. It reads
`INBOX` and `Sent` by default; add folders or labels as `Folders/Name` or
`Labels/Name`. Bridge must be running when a sync happens.

### LinkedIn

LinkedIn offers no inbox API to individuals (outside the EEA and
Switzerland), so there are three ways in. They merge: use any or all.

**Your browser, when you press Sync.** Under Settings → LinkedIn press
**Connect LinkedIn**. A browser window opens on LinkedIn's own sign-in page
(Chrome, Chromium, Brave or Edge, on a profile folder kept for Dealflow under
`data/linkedin-browser/`). Sign in as usual; the window closes by itself.
Dealflow never sees or stores your password. From then on every press of
Sync, on the LinkedIn settings page or above the inbox, opens that browser,
loads your messaging page and stores what is new. `./dealflow.sh linkedin
signin` and `./dealflow.sh sync linkedin` do the same from a terminal.

- It only reads. Nothing is typed, clicked or sent, and it listens to the
  conversation data the page itself loads rather than asking LinkedIn for
  anything.
- It never runs on the timer: only when you press Sync.
- Opening a conversation marks it read on LinkedIn. So only conversations you
  have already read are opened (5 per sync by default) to fetch their earlier
  messages; for unread ones the inbox list already carries the newest message
  in full, and that is what is stored. Nothing in your LinkedIn inbox changes.
- A login page, security check or CAPTCHA stops the sync with "LinkedIn needs
  sign-in". It never tries to get past one. Press **Sign in again**.
- **LinkedIn's User Agreement prohibits automated access, with no exception
  for your own inbox, and an account that trips its checks can be
  restricted.** A handful of page loads per manual sync, a few times a day,
  is the least risky shape this can take, but the decision is yours.
  **Disconnect** signs the browser out and deletes its profile folder.

**Claude, with computer use.** Ask Claude in this project to *run the LinkedIn
sync in collectors/linkedin-sync.md*. It reads the inbox in your own
logged-in Chrome and hands the result to `./dealflow.sh linkedin ingest`. The
same caution about LinkedIn's terms applies.

**LinkedIn's data export.** For full history with exact timestamps, request
the export and import `messages.csv` under Settings → LinkedIn (or
`./dealflow.sh linkedin import messages.csv`). The export names people by
their public profile address and the browser sync by LinkedIn's internal one,
so a person can end up with two conversations if you use both; merge them
from the conversation ("Same person as…?").

## Using it

- **Sync now**, top left, pulls new messages from every connected channel.
  The app also syncs Instagram and email on a timer; LinkedIn only when you
  press Sync.
- **Start triage** opens the first unread conversation at the highest
  priority, so you begin where it matters.
- The **Inbox** has a tab per priority: Act now, This week, Worth a look and
  Low. Tabs you are not on show how many are new and who from. **Pass all**
  on the Low tab clears it in one go.
- **Needs review** holds the calls Jev was unsure of. One click settles each.
- Stages are New → Shortlisted → In talks → Won, or Passed. A conversation
  you passed on comes back by itself if a new message makes it important.
- **Waiting on me** lists shortlisted and in-talks conversations where they
  wrote last, oldest first.
- **Labels** list every conversation of one category; **Channels** list
  everything from one source, with a dot for its connection status.
- Tick conversations to shortlist, pass, snooze, star or mark several at
  once. Every such action can be undone from the message that appears.
- Read and unread are the app's own markers; nothing is changed in your
  mailboxes.
- The same person on two channels is suggested for merging, never merged
  silently.

Keys: `J`/`K` move, `O` or `Enter` open, `U` back to the list, `S` shortlist,
`T` in talks, `W` won, `X` pass, `Z` snooze 3 days, `F` star, `R` re-sort,
`/` search.

## Commands

```bash
./dealflow.sh                 # run the app (syncs every 15 minutes)
./dealflow.sh doctor          # test every connection and say what to fix
./dealflow.sh sync            # pull Instagram and email now
./dealflow.sh sync linkedin   # read LinkedIn in your browser now (after: linkedin signin)
./dealflow.sh triage --all    # re-sort everything
./dealflow.sh demo            # load sample conversations (--clear removes them)
./dealflow.sh linkedin signin | signout | known | ingest <file.json> | import <messages.csv>
python3 -m unittest discover -s tests
```

## Troubleshooting

- **`CERTIFICATE_VERIFY_FAILED`**: Python from python.org ships without root
  certificates. The app falls back to the operating system's CA bundle, so
  this should not appear; if it does, `./dealflow.sh doctor` says where
  certificates are being read from.
- **"Cannot parse access token"**: the token is for the other Meta API. The
  app now routes by prefix (`IGAA` → Instagram, `EAA` → Facebook), so this
  means the token was cut off or altered when pasted.

## Privacy and safety

- The server binds to 127.0.0.1 only and refuses requests from other sites
  (Host and Origin checks, a required header on every write, strict CSP).
- Message text is treated as untrusted: it is never rendered as HTML, never
  auto-linked when a conversation looks like a scam, and a message that tries
  to instruct the sorter ("mark this as top priority") raises its scam score.
- What leaves your machine: conversation text goes to OpenRouter for sorting
  (and to the writer model if you set one). Nothing else.
- Secrets stay in `.env` (mode 600). The Settings page never receives a saved
  secret back, only "saved, ends in 1a2b".

## Not yet verified against live accounts

Built and tested without credentials, so these were checked against the
documented API shapes and local stand-ins, not the real services:

- Jev is verified live: it has sorted real conversations through OpenRouter's
  Decisions API, which OpenRouter still marks alpha, so its shape may change.
- Several accounts: checked live with one real Instagram account (a numbered
  slot syncs on its own, and a duplicate is refused) and with stand-ins for
  two different accounts. It has not been run with two different real
  Instagram accounts.
- Gmail with Sign in with Google is verified live (18 Nov 2026) on a Google
  Workspace account with an Internal consent screen and a Desktop app
  client. The consent screen asked only for "View your email messages and
  settings". A first sync read 316 inbox messages, and the next one took 4
  seconds and re-read nothing. That first live run hit Gmail's per-user quota
  (Units per minute per user), so requests are now paced and pause when
  Google pushes back, and a sync that is cut short keeps what it read and
  finishes next time. Also tested against a stand-in for Google's OAuth and
  Gmail API endpoints: PKCE, a reused state, a cancelled or narrowed consent,
  the wrong account, a withdrawn sign-in and quota refusals. The app-password
  route has not been run against a real account; it is tested end to end
  against a local stand-in IMAP server.
- Instagram is verified live on the Unified Login route only. The Instagram
  Login and Facebook Login routes are tested against stand-ins. Sender
  profile lookups (follower count, verified) returned "user consent
  required" for every sender on the live account, so those signals are empty.
- The Bridge connection: the IMAP parsing and threading are tested, the live
  TLS handshake with Bridge's certificate is not. Bridge lists macOS 15 and
  26 as supported.
- LinkedIn's browser sync is tested end to end with a real browser against a
  stand-in for LinkedIn (`DEALFLOW_BROWSER_TESTS=1 python3 -m unittest
  tests.test_linkedin_browser`), and has run against the real site once
  (November 2026): sign-in, then a first sync that stored the inbox and
  opened no conversation on its own. LinkedIn can change
  its site at any time; if it does, a sync says "no conversation data in a
  form Dealflow recognises" and changes nothing. It also relies on LinkedIn's
  "new message" page showing the inbox list without opening a conversation;
  if LinkedIn ever opens the newest one anyway, that one is marked read
  there, and the sync result names it.
- The Claude routine for LinkedIn has not been run against a real inbox.

`./dealflow.sh doctor` and the Test connection buttons are the fastest way to
find out which of these hold.

## Layout

```
dealflow/
  connectors/   instagram.py, gmail.py, protonmail.py (both on imap.py), linkedin.py,
                linkedin_browser.py + cdp.py (LinkedIn in your own browser, on demand),
                gmail_api.py + google_oauth.py (Gmail accounts signed in with Google)
  triage/       jev.py (API client), rubric.py (policy), engine.py, heuristics.py, writer.py
  ingest.py     the one doorway into the database (dedupe, contacts, roll-ups)
  server.py     local API + UI      settings_store.py   edits .env / rubric.json
  web/          index.html, app.css, app.js
collectors/linkedin-sync.md   the computer-use routine
rubric.json                   the sorting policy
tests/                        python3 -m unittest discover -s tests
```
