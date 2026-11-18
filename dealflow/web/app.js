// Dealflow UI: a mail-client layout over the local API. No framework, no build step.
// Every piece of message content reaches the page through textContent, never innerHTML.
// The app only reads: nothing here sends a message or changes anything in your accounts.

// -- tiny DOM helpers ---------------------------------------------------------

const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value == null) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false || kid === "") continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const ICONS = {
  instagram: '<rect x="3.5" y="3.5" width="17" height="17" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17" cy="7" r=".7" fill="currentColor" stroke="none"/>',
  linkedin: '<rect x="3.5" y="3.5" width="17" height="17" rx="3.5"/><path d="M8 10.8v5.7M8 7.6v.1M11.6 16.5v-3.2a2.3 2.3 0 0 1 4.6 0v3.2M11.6 10.8v5.7"/>',
  email: '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="m3.6 7.6 8.4 6 8.4-6"/>',
  gmail: '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M6.5 16V9.5l5.5 4 5.5-4V16"/>',
  mailOpen: '<path d="M3.5 10 12 4.5l8.5 5.5v8A1.5 1.5 0 0 1 19 19.5H5A1.5 1.5 0 0 1 3.5 18z"/><path d="m3.8 10.3 8.2 5.2 8.2-5.2"/>',
  star: '<path d="m12 3.8 2.5 5.2 5.7.8-4.1 4 1 5.7-5.1-2.7-5.1 2.7 1-5.7-4.1-4 5.7-.8z"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  dash: '<path d="M6 12h12"/>',
  x: '<path d="M6.5 6.5 17.5 17.5M17.5 6.5 6.5 17.5"/>',
  external: '<path d="M14 4.5h5.5V10M19.5 4.5l-8 8M10 6H6.5a2 2 0 0 0-2 2v9.5a2 2 0 0 0 2 2H16a2 2 0 0 0 2-2V14"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4"/>',
  alert: '<path d="M12 4.5 20.5 19h-17zM12 10v4.2M12 16.9v.1"/>',
  inbox: '<path d="m4 13.5 2.4-7.2A2 2 0 0 1 8.3 5h7.4a2 2 0 0 1 1.9 1.3L20 13.5M4 13.5V17a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3.5M4 13.5h4.5l1 2.5h5l1-2.5H20"/>',
  eye: '<path d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12s-3.5 6.5-9.5 6.5S2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  reply: '<path d="m9.5 7-5 5 5 5M4.5 12h9a6 6 0 0 1 6 6v.5"/>',
  bookmark: '<path d="M7 4.5h10a1 1 0 0 1 1 1V20l-6-4-6 4V5.5a1 1 0 0 1 1-1z"/>',
  chat: '<path d="M5 6.5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-5l-4.5 3.5v-3.5H7a2 2 0 0 1-2-2z"/>',
  flag: '<path d="M6 20V4.5M6 5h10.5l-2 3.5 2 3.5H6"/>',
  archive: '<path d="M5.5 8.5V18A1.5 1.5 0 0 0 7 19.5h10a1.5 1.5 0 0 0 1.5-1.5V8.5M9.5 12h5M4.5 4.5h15v4h-15z"/>',
  down: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  stack: '<path d="m12 4 8.5 4.5L12 13 3.5 8.5zM3.5 12.5 12 17l8.5-4.5M3.5 16.5 12 21l8.5-4.5"/>',
  sun: '<circle cx="12" cy="12" r="3.8"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/>',
  moon: '<path d="M19.5 14.2A8 8 0 1 1 9.8 4.5a6.5 6.5 0 0 0 9.7 9.7z"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  back: '<path d="m14.5 6-6 6 6 6"/>',
  chevR: '<path d="m9.5 6 6 6-6 6"/>',
  chevD: '<path d="m6 9.5 6 6 6-6"/>',
  caret: '<path d="m7.5 10 4.5 5 4.5-5z" fill="currentColor" stroke="none"/>',
  arrowBack: '<path d="M19.5 12h-15M10.5 6l-6 6 6 6"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M5.5 15.5v-9a2 2 0 0 1 2-2h8"/>',
  merge: '<path d="M7 20v-5a5 5 0 0 1 5-5 5 5 0 0 0 5-5V4M17 4l-2.5 2.5M17 4l2.5 2.5M7 4v6"/>',
  bolt: '<path d="M13 3.5 5.5 13.5H11l-1 7 7.5-10H12z"/>',
  calendar: '<rect x="4" y="5.5" width="16" height="14" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
  spark: '<path d="m12 3.5 2 6.5 6.5 2-6.5 2-2 6.5-2-6.5-6.5-2 6.5-2z"/>',
  file: '<path d="M7 3.5h7l4.5 4.5v11a1.5 1.5 0 0 1-1.5 1.5H7A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5z"/><path d="M14 3.5V8h4.5"/>',
  label: '<path d="M4.5 7a2 2 0 0 1 2-2h7.6a2 2 0 0 1 1.6.8l3.8 5a2 2 0 0 1 0 2.4l-3.8 5a2 2 0 0 1-1.6.8H6.5a2 2 0 0 1-2-2z"/>',
  imp: '<path d="M4.5 6h9.5l5.5 6-5.5 6H4.5l5-6z"/>',
  people: '<circle cx="9" cy="9" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0M15.5 6.2a3 3 0 0 1 0 5.6M17 14.3a5 5 0 0 1 3.5 4.7"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  plus: '<path d="M12 5.5v13M5.5 12h13"/>',
  trash: '<path d="M5 7.5h14M9.5 7.5V5.5h5v2M7 7.5l.8 11a1.5 1.5 0 0 0 1.5 1.4h5.4a1.5 1.5 0 0 0 1.5-1.4l.8-11"/>',
};

function icon(name, size = 20) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  svg.innerHTML = ICONS[name] || ""; // static strings defined above, never user data
  return svg;
}

function safeUrl(url) {
  return typeof url === "string" && /^(https?:\/\/|mailto:)/i.test(url) ? url : null;
}

// -- formatting -----------------------------------------------------------------

function ago(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  const minutes = Math.round((Date.now() - then.getTime()) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d`;
  const sameYear = then.getFullYear() === new Date().getFullYear();
  return then.toLocaleDateString(undefined, sameYear ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", year: "2-digit" });
}

// "3m ago", or "just now" within the first minute.
function since(iso) {
  const short = ago(iso);
  return short === "now" ? "just now" : `${short} ago`;
}

function stamp(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function compact(number) {
  if (number == null || number === "") return "";
  const n = Number(number);
  if (!Number.isFinite(n)) return String(number);
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "k";
  return String(n);
}

const money = (value) => "$" + (value < 0.01 && value > 0 ? value.toFixed(4) : value.toFixed(2));
const plural = (count, noun) => `${count} ${noun}${count === 1 ? "" : "s"}`;
const CHANNEL_LABEL = { instagram: "Instagram", linkedin: "LinkedIn", email: "Email" };
// Connectors are what you set up; two of them (Gmail, Proton Mail) feed the Email channel.
const CONNECTOR_LABEL = { instagram: "Instagram", gmail: "Gmail", email: "Proton Mail", linkedin: "LinkedIn" };
const MULTI_ACCOUNT = ["instagram", "gmail"];

// The inbox tabs are Jev's priority tiers. Everything still to triage lives in one of them.
const TIER_IDS = ["P0", "P1", "P2", "P3"];
const TIER_ICON = { P0: "bolt", P1: "calendar", P2: "spark", P3: "down" };
const QUEUES = [
  ["inbox", "Inbox", "inbox"], ["review", "Needs review", "eye"], ["waiting", "Waiting on me", "reply"],
  ["starred", "Starred", "star"], ["snoozed", "Snoozed", "clock"], ["shortlist", "Shortlisted", "bookmark"],
  ["talking", "In talks", "chat"], ["won", "Won", "flag"], ["passed", "Passed", "archive"], ["all", "All", "stack"],
];
const QUEUE_LABEL = Object.fromEntries(QUEUES.map(([id, label]) => [id, label]));
const CAT_COLOR = {
  brand_deal: "purple", investment: "green", partnership: "blue", customer: "yellow", speaking: "pink",
  vendor: "gray", talent: "brown", community: "orange", personal: "default", noise: "red",
};
const PALETTE = ["purple", "green", "blue", "yellow", "pink", "gray", "brown", "orange", "red", "default"];
const STAGE_COLOR = { new: "default", shortlist: "purple", talking: "blue", won: "green", passed: "brown" };

// A category added to rubric.json later still gets a steady colour of its own.
function catColor(id) {
  if (!id) return "default";
  if (CAT_COLOR[id]) return CAT_COLOR[id];
  let sum = 0;
  for (const char of String(id)) sum = (sum * 31 + char.charCodeAt(0)) >>> 0;
  return PALETTE[sum % PALETTE.length];
}

const tierLabel = (id) => ((state.boot.tiers.find((tier) => tier.id === id) || {}).label || id);
const stageLabel = (id) => ((state.boot.stages.find((stage) => stage.id === id) || {}).label || id);
const categoryLabel = (id) => ((state.boot.categories.find((category) => category.id === id) || {}).label || id);
const isPhone = () => window.matchMedia("(max-width: 720px)").matches;

function firstName(name) {
  const text = String(name || "").trim();
  return text.startsWith("@") ? text : text.split(/\s+/)[0] || text;
}

function initial(name) {
  const match = /[\p{L}\p{N}]/u.exec(String(name || ""));
  return match ? match[0].toUpperCase() : "?";
}

const isSnoozed = (t) => Boolean(t.snoozed_until && new Date(t.snoozed_until) > new Date());
const waitingOnMe = (t) => (t.stage === "shortlist" || t.stage === "talking") && t.last_direction === "in" && !isSnoozed(t);

function knownAccounts(channel) {
  const all = (state.status && state.status.accounts) || [];
  return channel ? all.filter((a) => a.channel === channel) : all;
}

// The owner's own account a conversation arrived on, by name. Empty when that
// channel has only one account, where saying so would be noise.
function accountName(channel, id) {
  if (!id || knownAccounts(channel).length < 2) return "";
  const account = knownAccounts(channel).find((a) => a.id === id);
  return account ? account.name : "";
}

// -- state ----------------------------------------------------------------------

const state = {
  boot: null,
  status: null,
  view: "inbox",        // inbox, review, waiting, starred, snoozed, shortlist, talking, won, passed, all, label, channel, settings
  arg: "",              // the category of a label view, the connector of a channel view
  tier: "P0",           // the inbox tab
  q: "",                // what the search box asks for
  shownQ: "",           // the search the rows on screen answer
  mode: "inbox",        // "inbox": rows are split into tier tabs; "flat": one list
  filters: { channel: "", category: "", account: "" },
  rows: [],
  inbox: { P0: [], P1: [], P2: [], P3: [] },
  unsorted: [],
  total: 0,
  fetched: 0,
  counts: {},
  nav: null,            // unread counts for the navigation; null on a server that does not send them
  facets: { channel: {}, category: {}, priority: {}, account: {} },
  cursorId: null,
  sel: new Set(),
  openId: null,
  detail: null,
  loadingList: true,
  loadingDetail: false,
  draftBusy: false,
  listError: "",
  listScroll: 0,
  expanded: new Set(),  // message ids shown in full in the open conversation
  foldOpen: false,
  foldFor: null,
  foldCount: 0,
  jevOpen: false,
  noteText: "",
  focusConvo: false,
  lastList: { view: "inbox", arg: "" },
  settingsSection: "sorting",
};

async function api(method, path, body) {
  const options = { method, headers: { "X-Dealflow": "1" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error("Can't reach the Dealflow server. Is ./dealflow.sh still running?");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

// This server keeps read/unread state and can tell Gmail from Proton Mail. An
// older server process does neither, so those controls stay hidden until it is restarted.
const canMarkRead = () => Boolean(state.boot && state.boot.read_state);

// -- toasts ---------------------------------------------------------------------

function toast(message, { action, onAction, tone } = {}) {
  const node = h("div", { class: "toast" + (tone ? ` toast-${tone}` : "") }, h("span", { text: message }));
  const dismiss = () => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 180);
  };
  if (action) {
    node.append(h("button", { class: "toast-action", type: "button", onclick: () => { dismiss(); onAction(); } }, action));
  }
  $("#toasts").append(node);
  setTimeout(dismiss, action ? 7000 : 3600);
}

const fail = (error) => toast(error.message || String(error), { tone: "error" });

// -- routing --------------------------------------------------------------------
// #/inbox/<tier>[/<id>]   #/<queue>[/<id>]   #/label/<category>[/<id>]
// #/channel/<connector>[/<id>]   #/settings/<section>

function parseHash() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map((part) => {
    try { return decodeURIComponent(part); } catch (error) { return part; }
  });
  const [first, second, third] = parts;
  const number = (value) => (/^\d+$/.test(value || "") ? Number(value) : null);
  const route = { view: "inbox", arg: "", tier: null, openId: null, section: null };
  if (first === "settings" || first === "connections") {
    route.view = "settings";
    route.section = second || null;
  } else if (!first || first === "inbox" || first === "queue") {
    if (TIER_IDS.includes(second)) { route.tier = second; route.openId = number(third); } else route.openId = number(second);
  } else if (first === "low") {
    route.tier = "P3";
    route.openId = number(second);
  } else if (first === "label" && state.boot.categories.some((category) => category.id === second)) {
    route.view = "label"; route.arg = second; route.openId = number(third);
  } else if (first === "channel" && CONNECTOR_LABEL[second]) {
    route.view = "channel"; route.arg = second; route.openId = number(third);
  } else if (QUEUE_LABEL[first]) {
    route.view = first;
    route.openId = number(second);
  }
  return route;
}

function currentHash() {
  if (state.view === "settings") return `#/settings/${state.settingsSection}`;
  const base = state.view === "inbox" ? `#/inbox/${state.tier}`
    : state.view === "label" || state.view === "channel" ? `#/${state.view}/${encodeURIComponent(state.arg)}`
    : `#/${state.view}`;
  return base + (state.openId ? `/${state.openId}` : "");
}

// `push` adds a history entry, so the browser's Back button retraces the owner's steps.
function writeHash(push = false) {
  const next = currentHash();
  if (location.hash === next) return;
  if (push) history.pushState(null, "", next);
  else history.replaceState(null, "", next);
}

// -- loading --------------------------------------------------------------------

const inboxMode = () => state.mode === "inbox";
const tierRows = (tier) => (tier === "P0" ? [...state.inbox.P0, ...state.unsorted] : state.inbox[tier] || []);
const currentRows = () => (inboxMode() ? tierRows(state.tier) : state.rows);
const hasFilters = () => Boolean(state.filters.channel || state.filters.category || state.filters.account);

function findRow(id) {
  for (const list of [state.rows, state.unsorted, ...Object.values(state.inbox)]) {
    const hit = list.find((t) => t.id === id);
    if (hit) return hit;
  }
  return state.detail && state.detail.id === id ? state.detail : null;
}

function listParams(extra) {
  const params = new URLSearchParams(extra);
  const filters = state.filters;
  const searching = Boolean(state.q);  // a search looks everywhere, whichever view it started from
  if (!searching && state.view === "label") params.set("category", state.arg);
  else if (filters.category) params.set("category", filters.category);
  if (!searching && state.view === "channel") {
    // An older server does not know connectors: fall back to the channel, which lumps Gmail and Proton Mail together.
    if (canMarkRead()) params.set("connector", state.arg);
    else params.set("channel", state.arg === "gmail" ? "email" : state.arg);
  } else if (filters.channel) params.set("channel", filters.channel);
  if (filters.account) params.set("account", filters.account);
  if (searching) params.set("q", state.q);
  return params;
}

function mergeFacets(a, b) {
  const out = {};
  for (const name of new Set([...Object.keys(a || {}), ...Object.keys(b || {})])) {
    out[name] = { ...((a || {})[name] || {}) };
    for (const [key, count] of Object.entries((b || {})[name] || {})) out[name][key] = (out[name][key] || 0) + count;
  }
  return out;
}

const newestFirst = (a, b) => (Date.parse(b.last_message_at || "") || 0) - (Date.parse(a.last_message_at || "") || 0);

// Fetches the rows for the current view into `state`. Draws nothing.
let listSeq = 0;
async function reloadRows() {
  if (state.view === "settings") return;
  const seq = ++listSeq;
  const q = state.q;
  try {
    if (state.view === "inbox" && !q) {
      // The old "To triage" and "Low priority" queues together are the inbox; its tabs split them by tier.
      const [queue, low] = await Promise.all([
        api("GET", `/api/threads?${listParams({ view: "queue", limit: "500" })}`),
        api("GET", `/api/threads?${listParams({ view: "low", limit: "500" })}`),
      ]);
      if (seq !== listSeq) return;
      const inbox = { P0: [], P1: [], P2: [], P3: [] };
      const unsorted = [];
      for (const t of [...queue.threads, ...low.threads]) {
        if (inbox[t.priority]) inbox[t.priority].push(t);
        else unsorted.push(t);
      }
      for (const tier of TIER_IDS) inbox[tier].sort(newestFirst);
      unsorted.sort(newestFirst);
      state.inbox = inbox;
      state.unsorted = unsorted;
      state.rows = [];
      state.total = queue.total + low.total;
      state.fetched = queue.threads.length + low.threads.length;
      state.facets = mergeFacets(queue.facets, low.facets);
      state.counts = queue.counts;
      if (queue.nav) state.nav = queue.nav;
      state.mode = "inbox";
    } else {
      const view = state.view === "inbox" ? "queue" : state.view === "label" || state.view === "channel" ? "all" : state.view;
      const data = await api("GET", `/api/threads?${listParams({ view, limit: "500" })}`);
      if (seq !== listSeq) return;
      state.rows = data.threads;
      state.total = data.total;
      state.fetched = data.threads.length;
      state.facets = data.facets;
      state.counts = data.counts;
      if (data.nav) state.nav = data.nav;
      state.mode = "flat";
    }
    state.shownQ = q;
    state.listError = "";
  } catch (error) {
    if (seq !== listSeq) return;
    state.listError = error.message;
  }
  state.loadingList = false;
  // Forget selections, and a cursor, that are no longer listed.
  const listed = new Set(currentRows().map((t) => t.id));
  for (const id of [...state.sel]) if (!listed.has(id)) state.sel.delete(id);
  if (state.cursorId != null && !listed.has(state.cursorId) && state.openId !== state.cursorId) state.cursorId = null;
}

function render() {
  renderChrome();
  if (state.view === "settings") renderSettings();
  else if (state.openId) renderConvo();
  else renderList();
}

async function loadList() {
  if (state.view === "settings") {
    renderChrome();
    renderSettings();
    if (!settingsUi.data) loadSettings();
    return;
  }
  await reloadRows();
  render();
}

// After something changed in the background: new rows, but never a redraw of a conversation being read.
async function refreshList() {
  await reloadRows();
  renderChrome();
  if (state.view === "settings") return;
  if (state.openId) redrawConvoToolbar(); else renderList();
}

async function loadDetail(id, { markRead = false } = {}) {
  state.loadingDetail = !state.detail || state.detail.id !== id;
  if (state.loadingDetail) renderConvo();
  try {
    let detail = await api("GET", `/api/threads/${id}`);
    if (state.openId !== id) return; // the owner moved on while this was loading
    if (markRead && detail.unread && canMarkRead()) {
      // Opening a conversation reads it. This is Dealflow's own marker: nothing changes in the mailbox.
      try {
        detail = await api("PATCH", `/api/threads/${id}`, { unread: false });
        const row = findRow(id);
        if (row) row.unread = false;
        loadStatus();
      } catch (error) { /* still readable */ }
      if (state.openId !== id) return;
    }
    state.detail = detail;
  } catch (error) {
    if (state.openId === id) state.detail = null;
    fail(error);
  }
  state.loadingDetail = false;
  renderConvo();
}

let statusTimer = null;
async function loadStatus() {
  clearTimeout(statusTimer);
  const wasRunning = state.status && state.status.job.running;
  const seen = () => JSON.stringify([state.counts || {}, state.nav]);
  const before = seen();
  try {
    state.status = await api("GET", "/api/status");
    state.counts = state.status.counts;
    if (state.status.nav) state.nav = state.status.nav;
  } catch (error) {
    /* transient: keep the last known status */
  }
  const running = state.status && state.status.job.running;
  if (wasRunning && !running && state.view === "settings") loadSettings();
  if (wasRunning && !running) {
    const result = state.status.job.result || {};
    const errors = Object.entries(result).filter(([, v]) => v && v.error).map(([k, v]) => `${k}: ${v.error}`);
    if (result.error) errors.push(result.error);
    if (errors.length) toast(errors[0], { tone: "error" });
    else toast(`${state.status.job.label}: done`);
    refreshList();
  } else if (!running && before !== seen() && state.view !== "settings" && !state.openId && !state.sel.size
      && $("#menu").hidden && !state.loadingList && !(document.activeElement && document.activeElement.matches("select"))) {
    // Something arrived or moved without a job we watched (a scheduled sync, another window): show it.
    refreshList();
  }
  renderChrome();
  if (state.view === "settings") updateSettingsStatus();
  statusTimer = setTimeout(loadStatus, running ? 1500 : 20000);
}

// -- navigation between views ---------------------------------------------------

function clearSearchBox() {
  state.q = "";
  searchInput.value = "";
  searchClear.hidden = true;
}

function goView(view, arg = "", { tier } = {}) {
  state.view = view;
  state.arg = arg;
  if (tier) state.tier = tier;
  clearSearchBox();
  state.filters = { channel: "", category: "", account: "" };
  state.openId = null;
  state.detail = null;
  state.sel.clear();
  state.cursorId = null;
  state.listScroll = 0;
  state.mode = view === "inbox" ? "inbox" : "flat";
  state.shownQ = "";
  state.loadingList = true;
  closeNav();
  closeMenu();
  writeHash(true);
  render();
  return loadList().then(() => { if (view === "inbox" && !tier) landOnFirstBusyTab(); });
}

// Arriving at the inbox without asking for a particular tab: if that tab is empty
// (often "Act now"), show the first one that has something in it instead of an empty page.
function landOnFirstBusyTab() {
  if (state.view !== "inbox" || !inboxMode() || state.openId || state.loadingList || tierRows(state.tier).length) return;
  const busy = TIER_IDS.find((tier) => tierRows(tier).length);
  if (!busy) return;
  state.tier = busy;
  writeHash();
  renderList();
}

function setTier(tier) {
  if (state.tier === tier) return;
  state.tier = tier;
  state.sel.clear();
  state.cursorId = null;
  writeHash(true);
  renderList();
  const scroller = $("#lscroll");
  if (scroller) scroller.scrollTop = 0;
}

// Opens the first unread conversation at the highest priority, so triage starts where it matters.
async function startTriage() {
  closeNav();
  if (state.view !== "inbox" || state.q || hasFilters() || state.openId || !inboxMode()) await goView("inbox");
  const pick = (test) => {
    for (const tier of TIER_IDS) {
      const hit = tierRows(tier).find(test);
      if (hit) return [tier, hit];
    }
    return null;
  };
  const unread = canMarkRead() ? pick((t) => t.unread) : null;
  const found = unread || pick(() => true);
  if (!found) { toast("The inbox is clear. Nothing to triage."); return; }
  if (canMarkRead() && !unread) toast("Nothing unread. Starting from the top of the inbox.");
  state.tier = found[0];
  openConvo(found[1].id);
}

function openConvo(id, { replace = false } = {}) {
  const scroller = $("#lscroll");
  if (!state.openId && scroller) state.listScroll = scroller.scrollTop;
  state.openId = id;
  state.cursorId = id;
  state.focusConvo = true;
  state.jevOpen = false;
  state.noteText = "";
  closeMenu();
  writeHash(!replace);
  renderChrome();
  return loadDetail(id, { markRead: true });
}

function closeConvo() {
  if (!state.openId) return;
  state.openId = null;
  state.detail = null;
  state.loadingDetail = false;
  closeMenu();
  writeHash(true);
  renderChrome();
  renderList();
  const row = state.cursorId != null ? $(`#lscroll .row[data-id="${state.cursorId}"]`) : null;
  (row || $("#pane")).focus({ preventScroll: true });
}

// A conversation from the same person that lives in another queue: open it over the full list.
function openInAll(id) {
  state.view = "all";
  state.arg = "";
  clearSearchBox();
  state.filters = { channel: "", category: "", account: "" };
  state.sel.clear();
  state.mode = "flat";
  state.shownQ = "";
  state.rows = [];
  state.openId = null;
  openConvo(id);
  reloadRows().then(() => { if (state.openId === id && !state.loadingDetail) renderConvo(); });
}

// -- actions --------------------------------------------------------------------

// Applies a change to one conversation, then puts the screen where the owner expects:
// in an open conversation that just left this list, on to the one that took its place.
async function patch(id, changes) {
  const index = currentRows().findIndex((t) => t.id === id);
  try {
    const detail = await api("PATCH", `/api/threads/${id}`, changes);
    if (state.openId === id) state.detail = detail;
    await reloadRows();
    renderChrome();
    if (state.view === "settings") return true;
    const rows = currentRows();
    const gone = index !== -1 && !rows.some((t) => t.id === id);
    const next = gone ? rows[Math.min(index, rows.length - 1)] : null;
    if (state.openId === id) {
      if (!gone) renderConvo();
      else if (next) openConvo(next.id, { replace: true });
      else closeConvo();
    } else if (!state.openId) {
      if (gone && state.cursorId == null && next) state.cursorId = next.id;
      renderList();
    } else redrawConvoToolbar();
    return true;
  } catch (error) {
    fail(error);
    return false;
  }
}

async function setStage(id, stage) {
  const thread = findRow(id);
  if (!thread || thread.stage === stage) return;
  const previous = thread.stage;
  const name = thread.name;
  const label = stage === "new" ? "back in the inbox" : stageLabel(stage);
  if (await patch(id, { stage })) {
    toast(`${name}: ${label}`, { action: "Undo", onAction: () => patch(id, { stage: previous }) });
  }
}

async function snooze(id, days) {
  const thread = findRow(id);
  if (!thread) return;
  const name = thread.name;
  if (await patch(id, { snooze_days: days })) {
    if (days > 0) toast(`${name}: snoozed ${days === 1 ? "until tomorrow" : `for ${days} days`}`, { action: "Undo", onAction: () => patch(id, { snooze_days: 0 }) });
  }
}

function toggleStar(id) {
  const thread = findRow(id);
  if (thread) patch(id, { starred: !thread.starred });
}

async function markUnread(id) {
  try {
    await api("PATCH", `/api/threads/${id}`, { unread: true });
    closeConvo();
    await refreshList();
  } catch (error) { fail(error); }
}

async function retriage(id) {
  try {
    toast("Re-sorting this conversation…");
    const detail = await api("POST", `/api/threads/${id}/retriage`);
    if (state.openId === id) state.detail = detail;
    await reloadRows();
    render();
  } catch (error) { fail(error); }
}

async function startJob(path, body, started) {
  try {
    const result = await api("POST", path, body);
    toast(result.started ? started : "Something is already running. Try again in a moment.");
    // Mark it running locally: a quick job can be over before the next poll, and
    // the poll only reloads the page's data when it sees running turn to finished.
    if (result.started && state.status) state.status = { ...state.status, job: { ...state.status.job, running: true } };
    loadStatus();
  } catch (error) { fail(error); }
}

// -- selection and bulk actions -------------------------------------------------

let lastSelId = null;

function toggleSel(id, range) {
  const rows = currentRows();
  const from = rows.findIndex((t) => t.id === lastSelId);
  const to = rows.findIndex((t) => t.id === id);
  if (range && from !== -1 && to !== -1) {
    const on = !state.sel.has(id);
    for (let i = Math.min(from, to); i <= Math.max(from, to); i++) {
      if (on) state.sel.add(rows[i].id); else state.sel.delete(rows[i].id);
    }
  } else if (state.sel.has(id)) state.sel.delete(id);
  else state.sel.add(id);
  lastSelId = id;
  renderList();
}

function selectBy(kind) {
  const tests = {
    all: () => true, none: () => false, read: (t) => !t.unread, unread: (t) => t.unread,
    starred: (t) => t.starred, unstarred: (t) => !t.starred,
  };
  state.sel = new Set(currentRows().filter(tests[kind]).map((t) => t.id));
  renderList();
}

async function bulk(change, message) {
  const rows = currentRows().filter((t) => state.sel.has(t.id));
  if (!rows.length) return;
  const ids = rows.map((t) => t.id);
  const before = rows.map((t) => ({ id: t.id, stage: t.stage, starred: Boolean(t.starred), unread: Boolean(t.unread), snoozed: isSnoozed(t) }));
  try {
    await api("POST", "/api/bulk", { ids, ...change });
    state.sel.clear();
    toast(message(ids.length), { action: "Undo", onAction: () => undoBulk(before, change) });
    await refreshList();
  } catch (error) { fail(error); }
}

// Puts each conversation back the way it was: they may not all have started in the same state.
async function undoBulk(before, change) {
  const groups = new Map();
  const add = (body, id) => {
    const key = JSON.stringify(body);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(id);
  };
  for (const row of before) {
    if ("stage" in change) add({ stage: row.stage }, row.id);
    if ("starred" in change) add({ starred: row.starred }, row.id);
    if ("unread" in change) add({ unread: row.unread }, row.id);
    if ("snooze_days" in change && !row.snoozed) add({ snooze_days: 0 }, row.id);
  }
  try {
    for (const [key, ids] of groups) await api("POST", "/api/bulk", { ids, ...JSON.parse(key) });
    await refreshList();
  } catch (error) { fail(error); }
}

async function passAllLow() {
  const ids = tierRows("P3").map((t) => t.id);
  if (!ids.length) return;
  try {
    await api("POST", "/api/bulk", { ids, stage: "passed" });
    state.sel.clear();
    toast(`Passed on ${ids.length} low-priority conversations`, {
      action: "Undo", onAction: () => api("POST", "/api/bulk", { ids, stage: "new" }).then(() => refreshList()).catch(fail),
    });
    await refreshList();
  } catch (error) { fail(error); }
}

async function seedDemo() {
  try { await api("POST", "/api/demo/seed"); await loadStatus(); await loadList(); }
  catch (error) { fail(error); }
}

async function clearDemo() {
  try {
    await api("POST", "/api/demo/clear");
    state.openId = null;
    state.detail = null;
    state.sel.clear();
    writeHash();
    await loadStatus();
    await loadList();
    toast("Sample conversations removed");
  } catch (error) { fail(error); }
}

// -- header and navigation ------------------------------------------------------

const searchInput = h("input", {
  id: "search", type: "search", placeholder: "Search all conversations", autocomplete: "off", spellcheck: "false",
  "aria-label": "Search all conversations",
});
const searchClear = h("button", { class: "ib", type: "button", "aria-label": "Clear search", title: "Clear search", hidden: true }, icon("x", 20));
let searchTimer = null;

function runSearch(value) {
  clearTimeout(searchTimer);
  state.q = value.trim();
  searchClear.hidden = !searchInput.value;
  if (state.view === "settings") {
    // Searching from Settings leaves it: the results are conversations.
    state.view = "inbox";
    state.arg = "";
    state.mode = state.q ? "flat" : "inbox";
    state.shownQ = state.q;
    state.loadingList = true;
  }
  const redraw = state.loadingList || Boolean(state.openId);
  state.openId = null;
  state.detail = null;
  state.sel.clear();
  state.cursorId = null;
  writeHash();
  if (redraw) render();
  loadList();
}

function isDark() {
  const theme = document.documentElement.dataset.theme;
  return theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function syncThemeButton() {
  $("#theme-btn").replaceChildren(icon(isDark() ? "sun" : "moon", 22));
}

function toggleTheme() {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("dealflow-theme", next); } catch (error) { /* not persisted */ }
  syncThemeButton();
}

function openNav() { $("#app").classList.add("nav-open"); $("#scrim").hidden = false; }
function closeNav() { $("#app").classList.remove("nav-open"); $("#scrim").hidden = true; }

// The menu button: a drawer on phones, the icon rail everywhere else.
function toggleNav() {
  const app = $("#app");
  if (isPhone()) {
    if (app.classList.contains("nav-open")) closeNav(); else openNav();
    return;
  }
  const rail = app.classList.toggle("rail");
  try { localStorage.setItem("dealflow-rail", rail ? "1" : "0"); } catch (error) { /* not persisted */ }
}

function setupChrome() {
  $("#menu-btn").append(icon("menu", 24));
  $("#gear").append(icon("gear", 22));
  $("#search-box").append(icon("search", 20), searchInput, searchClear);
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchClear.hidden = !searchInput.value;
    searchTimer = setTimeout(() => runSearch(searchInput.value), 220);
  });
  searchInput.addEventListener("keydown", (event) => { if (event.key === "Enter") runSearch(searchInput.value); });
  searchClear.addEventListener("click", () => { searchInput.value = ""; runSearch(""); searchInput.focus(); });
  $("#menu-btn").addEventListener("click", toggleNav);
  $("#gear").addEventListener("click", () => goSettings());
  $("#theme-btn").addEventListener("click", toggleTheme);
  $("#brand").addEventListener("click", (event) => { event.preventDefault(); goView("inbox"); });
  $("#scrim").addEventListener("click", closeNav);
  // The skip link moves focus without touching the address, which is this page's router.
  $(".skip").addEventListener("click", (event) => { event.preventDefault(); $("#pane").focus(); });
  try { if (localStorage.getItem("dealflow-rail") === "1") $("#app").classList.add("rail"); } catch (error) { /* default: full navigation */ }
  syncThemeButton();
}

function renderHeaderStatus() {
  const status = state.status;
  const bits = [];
  if (status) {
    if (status.demo_threads > 0) {
      bits.push(h("span", { class: "hdr-chip", title: "These are sample conversations, not your real inbox.", text: "Sample data" }));
    }
    const triage = status.triage;
    if (triage.last_error) {
      bits.push(h("button", { class: "hdr-chip warn", type: "button", title: triage.last_error, onclick: () => goSettings("sorting") }, "Sorting needs attention"));
    } else if (triage.engine !== "jev") {
      bits.push(h("button", {
        class: "hdr-chip warn", type: "button", onclick: () => goSettings("sorting"),
        title: "No OpenRouter key yet, so conversations are sorted by keyword rules. Open the Sorting settings.",
      }, "Keyword rules"));
    }
    if (triage.pending) bits.push(h("span", { class: "hdr-chip", title: "Conversations waiting for the next sorting run", text: `${triage.pending} to sort` }));
    const job = status.job;
    const latest = status.connectors.map((c) => c.last_ok_at).filter(Boolean).sort().pop();
    bits.push(h("span", { class: "synced", text: job.running ? (job.detail || job.label || "Working…") : latest ? `Synced ${since(latest)}` : "Not synced yet" }));
  }
  $("#hdr-status").replaceChildren(...bits);
}

function connectorState(c) {
  // Grey until something has actually been pulled or collected, the same as the dot on its Settings tab.
  const dot = c.last_error ? "bad" : !c.configured || !c.last_ok_at ? "off" : (c.name === "linkedin" && c.stale) ? "warn" : "ok";
  const line = c.last_error ? "Needs attention"
    : !c.configured ? "Not connected"
    : c.name === "linkedin" ? (c.last_ok_at ? (c.stale ? `Stale · ${since(c.last_ok_at)}` : `Collected ${since(c.last_ok_at)}`) : "Not collected yet")
    : c.last_ok_at ? `Synced ${since(c.last_ok_at)}` : "Ready to sync";
  return { dot, line };
}

function navItem({ key, label, lead, count, plain, on, title, onclick }) {
  return h("button", {
    class: "ni" + (on ? " on" : ""), type: "button", dataset: { key }, title: title || label,
    "aria-current": on ? "page" : null, onclick,
  }, lead, h("span", { class: "ni-l", text: label }),
    count ? h("span", { class: "cnt" + (plain ? " plain" : ""), text: String(count) }) : null);
}

function renderNav() {
  if (!state.boot) return;
  const nav = $("#nav");
  const counts = state.counts || {};
  const unread = state.nav;
  const focusedKey = nav.contains(document.activeElement) ? document.activeElement.dataset.key : null;
  const scroll = nav.scrollTop;

  const queues = QUEUES.map(([id, label, glyph]) => {
    let count = counts[id] || 0;
    let plain = true;
    if (id === "inbox") {
      if (unread) { count = unread.inbox_unread; plain = false; } else count = (counts.queue || 0) + (counts.low || 0);
    } else if (id === "review" || id === "waiting") plain = false;
    else if (id === "all") count = 0;
    return navItem({
      key: id, label, lead: icon(glyph, 20), count, plain, on: state.view === id,
      title: id === "inbox" && unread ? `Inbox: ${count} unread` : label, onclick: () => goView(id),
    });
  });

  const labels = state.boot.categories.map((category) => navItem({
    key: `label:${category.id}`, label: category.label,
    lead: h("span", { class: `lab c-${catColor(category.id)}` }, icon("label", 20)),
    count: unread && unread.labels ? unread.labels[category.id] : 0,
    on: state.view === "label" && state.arg === category.id, onclick: () => goView("label", category.id),
  }));

  const connectors = state.status ? state.status.connectors : Object.keys(CONNECTOR_LABEL).map((name) => ({ name }));
  const channels = connectors.filter((c) => CONNECTOR_LABEL[c.name]).map((c) => {
    const s = state.status ? connectorState(c) : { dot: "off", line: "" };
    return navItem({
      key: `channel:${c.name}`, label: CONNECTOR_LABEL[c.name], lead: h("span", { class: `chdot ${s.dot}` }),
      count: unread && unread.connectors ? unread.connectors[c.name] : 0,
      title: `${CONNECTOR_LABEL[c.name]}${s.line ? `: ${s.line}` : ""}`,
      on: state.view === "channel" && state.arg === c.name, onclick: () => goView("channel", c.name),
    });
  });

  // Two different jobs, two buttons that say what they do: fetch new messages, or start reading them.
  const syncing = Boolean(state.status && state.status.job.running);
  nav.replaceChildren(
    h("button", {
      class: "cta" + (syncing ? " busy" : ""), type: "button", disabled: syncing,
      title: syncing ? (state.status.job.detail || state.status.job.label || "Working…") : "Pull new messages from every connected channel",
      onclick: () => { closeNav(); startJob("/api/sync", { connector: "all" }, "Syncing all channels…"); },
    }, icon("refresh", 24), h("span", { text: syncing ? "Syncing…" : "Sync now" })),
    h("button", { class: "cta2", type: "button", title: "Open the first unread conversation at the highest priority", onclick: startTriage },
      icon("bolt", 20), h("span", { text: "Start triage" })),
    ...queues,
    h("h2", { class: "nh", text: "Labels" }), ...labels,
    h("h2", { class: "nh", text: "Channels" }), ...channels,
    h("div", { class: "nav-foot" }, navItem({ key: "settings", label: "Settings", lead: icon("gear", 20), on: state.view === "settings", onclick: () => goSettings() })));
  nav.scrollTop = scroll;
  if (focusedKey) {
    const again = [...nav.querySelectorAll(".ni")].find((item) => item.dataset.key === focusedKey);
    if (again) again.focus({ preventScroll: true });
  }
}

function renderChrome() {
  if (!state.boot) return;
  renderHeaderStatus();
  renderNav();
  const unread = state.nav ? state.nav.inbox_unread : 0;
  document.title = (unread ? `(${unread}) ` : "") + "Dealflow";
  const sync = $("#sync-btn");
  const running = Boolean(state.status && state.status.job.running);
  if (sync) { sync.disabled = running; sync.classList.toggle("spin", running); }
}

// The settings code below was written against the old sidebar; this is what it means now.
function renderSidebar() { renderChrome(); }

// -- menu -----------------------------------------------------------------------

let menuAnchor = null;

function openMenu(anchor, items) {
  const menu = $("#menu");
  if (!menu.hidden && menuAnchor === anchor) { closeMenu(); return; }
  menuAnchor = anchor;
  menu.replaceChildren(...items.map(([text, action]) =>
    h("button", { class: "mi", type: "button", role: "menuitem", onclick: () => { closeMenu(); action(); } }, text)));
  menu.hidden = false;
  const at = anchor.getBoundingClientRect();
  const host = $("#app").getBoundingClientRect();
  const left = Math.max(8, Math.min(at.left - host.left, host.width - menu.offsetWidth - 8));
  let top = at.bottom - host.top + 2;
  if (top + menu.offsetHeight > host.height - 8) top = Math.max(8, at.top - host.top - menu.offsetHeight - 2);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.firstElementChild.focus();
}

function closeMenu({ refocus = false } = {}) {
  const menu = $("#menu");
  if (menu.hidden) return;
  menu.hidden = true;
  if (refocus && menuAnchor && document.contains(menuAnchor)) menuAnchor.focus();
  menuAnchor = null;
}

// -- list -----------------------------------------------------------------------

function ibtn(glyph, label, onclick, extra = {}) {
  return h("button", { class: "ib", type: "button", title: label, "aria-label": label, onclick, ...extra }, icon(glyph, 20));
}

const tsep = () => h("span", { class: "tsep", "aria-hidden": "true" });
const stop = (fn) => (event) => { event.stopPropagation(); fn(event); };

function lb(text, tone, glyph, title) {
  return h("span", { class: `lb ${tone}`, title: title || null }, glyph ? icon(glyph, 12) : null, text);
}

function listTitle() {
  if (state.shownQ) return "Search results";
  if (state.view === "label") return categoryLabel(state.arg);
  if (state.view === "channel") return CONNECTOR_LABEL[state.arg] || "Channel";
  if (state.view === "inbox") return `Inbox: ${tierLabel(state.tier)}`;
  return QUEUE_LABEL[state.view] || "Conversations";
}

function rowChips(t) {
  const chips = [];
  const mixed = Boolean(state.shownQ) || ["starred", "snoozed", "all", "label", "channel"].includes(state.view);
  if (t.risk) chips.push(lb("Scam risk", "c-red", "alert"));
  if (t.needs_review) chips.push(lb("Needs review", "c-yellow", "eye", t.review_reason || "Jev was unsure about this one"));
  if (t.category_label && (state.view !== "label" || state.shownQ)) chips.push(lb(t.category_label, `c-${catColor(t.category)}`));
  if (mixed && t.stage && t.stage !== "new") chips.push(lb(stageLabel(t.stage), `c-${STAGE_COLOR[t.stage] || "default"}`));
  if (t.deadline) chips.push(lb(t.deadline, "outline", "calendar", "Timing mentioned in the conversation"));
  else if (t.urgent) chips.push(lb("Urgent", "c-orange", "bolt"));
  for (const amount of (t.amounts || []).slice(0, 2)) chips.push(lb(amount, "c-green"));
  if (t.waiting_days != null && t.waiting_days >= 2 && t.stage !== "passed" && t.stage !== "won" && !waitingOnMe(t)) {
    chips.push(lb(`Waiting ${t.waiting_days}d`, "c-orange", "clock"));
  }
  const account = accountName(t.channel, t.account);
  if (account) chips.push(lb(`to ${account}`, "outline", null, "The account of yours that received this"));
  if (t.channel_count > 1) chips.push(lb(`${t.channel_count} channels`, "outline", "people"));
  if (t.account_count > 1) chips.push(lb(`Wrote to ${t.account_count} of your accounts`, "outline", "people"));
  if (t.partial) chips.push(lb("Preview only", "outline"));
  if (isSnoozed(t)) chips.push(lb(`Snoozed to ${stamp(t.snoozed_until)}`, "outline", "clock"));
  if (t.stale_triage) chips.push(lb("New message, re-sorting", "outline"));
  else if (!t.triaged) chips.push(lb("Not sorted yet", "outline"));
  return chips;
}

function renderRow(t) {
  const checked = state.sel.has(t.id);
  const tier = TIER_IDS.includes(t.priority) ? t.priority : "none";
  const color = catColor(t.category);
  const select = stop((event) => toggleSel(t.id, event.shiftKey));

  // Bold: the subject, or Jev's one-line brief when there is no subject. Then the last message.
  const strong = t.subject || t.summary || "";
  let preview = t.subject && t.summary ? t.summary : (t.snippet ? (t.last_direction === "out" ? "You: " : "") + t.snippet : "");
  if (preview === strong) preview = "";
  const tail = waitingOnMe(t)
    ? h("span", { class: "nudge", text: `They wrote ${since(t.last_message_at)}. Reply?` })
    : preview ? h("span", { class: "snip", text: preview }) : null;
  const chips = rowChips(t);

  const actions = [
    t.stage !== "shortlist" ? ibtn("bookmark", "Shortlist", stop(() => setStage(t.id, "shortlist"))) : null,
    t.stage !== "passed" ? ibtn("archive", "Pass", stop(() => setStage(t.id, "passed")))
      : ibtn("inbox", "Move back to the inbox", stop(() => setStage(t.id, "new"))),
    ibtn("clock", "Snooze 3 days", stop(() => snooze(t.id, 3))),
    canMarkRead() ? ibtn(t.unread ? "mailOpen" : "email", t.unread ? "Mark as read" : "Mark as unread", stop(() => patch(t.id, { unread: !t.unread }))) : null,
  ];

  return h("div", {
    class: `row t-${tier}` + (t.unread ? " unread" : "") + (checked ? " checked" : "") + (state.cursorId === t.id ? " cursor" : ""),
    role: "listitem", tabindex: "0", dataset: { id: t.id },
    onclick: (event) => { if (!event.target.closest("button, a")) openConvo(t.id); },
  },
    h("button", { class: "ck", type: "button", role: "checkbox", "aria-checked": String(checked), "aria-label": `Select the conversation with ${t.name}`, onclick: select },
      h("span", { class: "box" + (checked ? " on" : "") }, checked ? icon("check", 14) : null)),
    h("button", { class: `av c-${color}`, type: "button", "aria-pressed": String(checked), "aria-label": `Select the conversation with ${t.name}`, onclick: select },
      checked ? icon("check", 20) : initial(t.name)),
    h("button", {
      class: "st" + (t.starred ? " on" : ""), type: "button", "aria-pressed": String(Boolean(t.starred)),
      "aria-label": t.starred ? "Remove star" : "Star", title: t.starred ? "Starred" : "Star", onclick: stop(() => toggleStar(t.id)),
    }, icon("star", 18)),
    h("span", { class: `imp t-${tier}`, title: tier === "none" ? "Not sorted yet" : `${tierLabel(tier)} · score ${Math.round(t.score || 0)} of 100` }, icon("imp", 18)),
    h("span", { class: "who", title: [t.name, t.company, CHANNEL_LABEL[t.channel]].filter(Boolean).join(" · ") },
      icon(t.channel, 14),
      h("span", { class: "who-n" }, t.replied ? `${firstName(t.name)}, me` : t.name,
        t.company && t.company !== t.name ? h("span", { class: "who-co", text: ` · ${t.company}` }) : null),
      t.message_count > 1 ? h("span", { class: "n", text: String(t.message_count) }) : null),
    h("span", { class: "subj" },
      h("span", { class: "line1" },
        chips.length ? h("span", { class: "lbls" }, chips) : null,
        h("span", { class: "txt" },
          strong ? h("span", { class: "s", text: strong }) : null,
          strong && tail ? h("span", { class: "snip dash", text: " - " }) : null,
          tail)),
      t.attachment ? h("span", { class: "att", title: t.attachment }, icon("file", 16), h("span", { text: t.attachment })) : null),
    h("time", { class: "when", datetime: t.last_message_at || "", title: stamp(t.last_message_at), text: ago(t.last_message_at) }),
    h("span", { class: "acts" }, actions));
}

function emptyState(connector) {
  const block = (title, text, ...actions) => h("div", { class: "empty" },
    h("h2", { class: "empty-t", text: title }), text ? h("p", { text }) : null,
    actions.filter(Boolean).length ? h("div", { class: "empty-actions" }, actions) : null);
  const clear = hasFilters() ? h("button", {
    class: "btn", type: "button",
    onclick: () => { state.filters = { channel: "", category: "", account: "" }; loadList(); },
  }, "Clear filters") : null;

  if (state.listError) {
    return block("Couldn't load conversations", state.listError,
      h("button", { class: "btn", type: "button", onclick: () => loadList() }, "Try again"));
  }
  if (state.shownQ) {
    return block(`Nothing matches “${state.shownQ}”`, "Search looks at names, companies, subjects and message text across every queue.", clear);
  }
  if (!((state.counts || {}).all > 0)) {
    return block("No conversations yet",
      "Connect Instagram, Proton Mail or LinkedIn and messages land here, sorted by what deserves your time. Or load a set of sample conversations to see how triage works.",
      h("button", { class: "btn btn-primary", type: "button", onclick: () => goSettings("sorting") }, "Open settings"),
      h("button", { class: "btn", type: "button", onclick: seedDemo }, "Load sample conversations"));
  }
  if (clear) return block("Nothing matches these filters", "Other channels, categories or accounts may still have conversations here.", clear);
  if (state.view === "inbox") {
    if (state.tier === "P3") return block("No low-priority messages", "Fan mail, vendor pitches and noise collect here so you can clear them in one go.");
    const others = TIER_IDS.some((tier) => tier !== "P3" && tierRows(tier).length);
    if (!others) return block("Queue clear", "Everything worth a look has been triaged. New messages appear here after the next sync.");
    return block(`Nothing in “${tierLabel(state.tier)}”`, "The other tabs still have conversations waiting.");
  }
  if (state.view === "label") {
    return block(`Nothing labelled ${categoryLabel(state.arg)}`, "Conversations Jev files under this label show up here, whatever queue they are in.");
  }
  if (state.view === "channel") {
    const name = CONNECTOR_LABEL[state.arg];
    if (connector && !connector.configured && state.arg !== "linkedin") {
      return block(`${name} isn't connected`, "Connect it in Settings and its conversations show up here.",
        h("button", { class: "btn btn-primary", type: "button", onclick: () => goSettings(state.arg) }, `Set up ${name}`));
    }
    return block(`No ${name} conversations yet`, state.arg === "linkedin"
      ? "Connect LinkedIn under Settings to read it in your browser when you press Sync, or import LinkedIn's data export."
      : "They appear here after the next sync.",
      h("button", { class: "btn", type: "button", onclick: () => goSettings(state.arg) }, `Open ${name} settings`));
  }
  const copy = {
    review: ["Nothing needs a second opinion", "Conversations land here when Jev was unsure about a call that would change the priority."],
    waiting: ["Nobody is waiting on you", "Shortlisted and in-talks conversations where they wrote last show up here, oldest first."],
    snoozed: ["Nothing snoozed", "Press Z on a conversation to hide it for three days."],
    starred: ["No starred conversations", "Press F to star one."],
  }[state.view] || ["Nothing here", "Conversations you move to this stage will appear here."];
  return block(copy[0], copy[1]);
}

function filterSelects() {
  const facets = state.facets || {};
  const scoped = !state.shownQ;
  const pick = (key) => (event) => { state.filters[key] = event.target.value; state.sel.clear(); loadList(); };
  const out = [];
  if (!(scoped && state.view === "channel")) {
    const facet = facets.channel || {};
    out.push(h("select", { class: "fsel" + (state.filters.channel ? " set" : ""), "aria-label": "Channel", onchange: pick("channel") },
      h("option", { value: "", text: "All channels" }),
      state.boot.channels.map((c) => h("option", { value: c.id, selected: state.filters.channel === c.id, text: `${c.label} (${facet[c.id] || 0})` }))));
  }
  if (!(scoped && state.view === "label")) {
    const facet = facets.category || {};
    out.push(h("select", { class: "fsel" + (state.filters.category ? " set" : ""), "aria-label": "Category", onchange: pick("category") },
      h("option", { value: "", text: "All categories" }),
      state.boot.categories.filter((c) => facet[c.id] || state.filters.category === c.id).map((c) =>
        h("option", { value: c.id, selected: state.filters.category === c.id, text: `${c.label} (${facet[c.id] || 0})` }))));
  }
  // Offered once the owner has more than one account anywhere; lists the ones with conversations here.
  const accountFacet = facets.account || {};
  const accountOptions = knownAccounts().filter((a) => accountFacet[a.id] || state.filters.account === a.id);
  if (knownAccounts().length > 1 && accountOptions.length) {
    out.push(h("select", { class: "fsel" + (state.filters.account ? " set" : ""), "aria-label": "Account", onchange: pick("account") },
      h("option", { value: "", text: "All accounts" }),
      accountOptions.map((a) => h("option", {
        value: a.id, selected: state.filters.account === a.id,
        text: `${a.channel === "instagram" ? "Instagram" : "Email"} · ${a.name} (${accountFacet[a.id] || 0})`,
      }))));
  }
  return out;
}

function listToolbar(rows) {
  const selected = rows.filter((t) => state.sel.has(t.id));
  const all = rows.length > 0 && selected.length === rows.length;
  const some = selected.length > 0 && !all;
  const running = Boolean(state.status && state.status.job.running);
  const kids = [
    h("button", {
      class: "ck-all", type: "button", role: "checkbox", "aria-checked": all ? "true" : some ? "mixed" : "false",
      "aria-label": "Select all", title: "Select", disabled: !rows.length, onclick: () => selectBy(all || some ? "none" : "all"),
    }, h("span", { class: "box" + (all ? " on" : some ? " mixed" : "") }, all ? icon("check", 14) : some ? icon("dash", 14) : null)),
    h("button", {
      class: "ib sm", type: "button", "aria-label": "Select by…", title: "Select by…", "aria-haspopup": "menu", disabled: !rows.length,
      onclick: (event) => openMenu(event.currentTarget, [
        ["All", () => selectBy("all")], ["None", () => selectBy("none")], ["Read", () => selectBy("read")],
        ["Unread", () => selectBy("unread")], ["Starred", () => selectBy("starred")], ["Unstarred", () => selectBy("unstarred")],
      ]),
    }, icon("caret", 20)),
  ];
  if (selected.length) {
    const starred = selected.every((t) => t.starred);
    const n = (count) => plural(count, "conversation");
    kids.push(tsep(),
      ibtn("bookmark", "Shortlist", () => bulk({ stage: "shortlist" }, (count) => `Shortlisted ${n(count)}`)),
      ibtn("archive", "Pass", () => bulk({ stage: "passed" }, (count) => `Passed on ${n(count)}`)),
      ibtn("clock", "Snooze 3 days", () => bulk({ snooze_days: 3 }, (count) => `Snoozed ${n(count)} for 3 days`)),
      tsep(),
      canMarkRead() ? ibtn("mailOpen", "Mark as read", () => bulk({ unread: false }, (count) => `Marked ${n(count)} as read`)) : null,
      canMarkRead() ? ibtn("email", "Mark as unread", () => bulk({ unread: true }, (count) => `Marked ${n(count)} as unread`)) : null,
      ibtn("star", starred ? "Remove star" : "Star", () => bulk({ starred: !starred }, (count) => (starred ? `Removed the star from ${n(count)}` : `Starred ${n(count)}`)),
        starred ? { class: "ib st on" } : {}),
      h("span", { class: "sel-n", "aria-live": "polite", text: `${selected.length} selected` }));
  } else {
    kids.push(
      h("button", {
        class: "ib" + (running ? " spin" : ""), id: "sync-btn", type: "button", title: "Sync all channels", "aria-label": "Sync all channels",
        disabled: running, onclick: () => startJob("/api/sync", { connector: "all" }, "Syncing all channels…"),
      }, icon("refresh", 20)),
      inboxMode() && state.tier === "P3" && rows.length
        ? h("button", { class: "tool-text", type: "button", onclick: passAllLow }, icon("archive", 18), `Pass all ${rows.length}`) : null,
      h("div", { class: "filters" }, filterSelects()));
  }
  kids.push(h("span", { class: "grow" }));
  if (!state.loadingList && rows.length) {
    kids.push(h("span", { class: "pager", text: plural(rows.length, "conversation") }));
  }
  return h("div", { class: "toolbar", role: "toolbar", "aria-label": "List actions" }, kids);
}

function inboxTabs() {
  return h("div", { class: "tabs", role: "tablist", "aria-label": "Priority" }, state.boot.tiers.map((tier) => {
    const rows = tierRows(tier.id);
    const unread = rows.filter((t) => t.unread);
    const on = state.tier === tier.id;
    const names = [...new Set(unread.map((t) => firstName(t.name)))].slice(0, 4).join(", ");
    const under = state.loadingList ? "" : !on && unread.length ? names : rows.length ? plural(rows.length, "conversation") : "Nothing here";
    return h("button", {
      class: `tabx t-${tier.id}` + (on ? " on" : ""), type: "button", role: "tab", "aria-selected": String(on), onclick: () => setTier(tier.id),
    }, icon(TIER_ICON[tier.id] || "inbox", 20),
      h("span", { class: "tab-txt" },
        h("span", { class: "tab-l" }, h("span", { text: tier.label }),
          !on && unread.length ? h("span", { class: "newchip", text: `${unread.length} new` }) : null),
        h("span", { class: "tab-prev", text: under })));
  }));
}

const LIST_KEYS = [["J / K", "Move"], ["O or Enter", "Open"], ["S", "Shortlist"], ["T", "In talks"], ["W", "Won"], ["X", "Pass"],
  ["Z", "Snooze 3 days"], ["F", "Star"], ["R", "Re-sort"], ["/", "Search"]];
const CONVO_KEYS = [["J / K", "Next / previous"], ["U", "Back to the list"], ["S", "Shortlist"], ["T", "In talks"], ["W", "Won"], ["X", "Pass"],
  ["N", "Back to the inbox"], ["Z", "Snooze 3 days"], ["F", "Star"], ["R", "Re-sort"], ["/", "Search"]];
const keyLegend = (keys) => h("p", { class: "keys" }, keys.map(([key, what]) => h("span", {}, h("kbd", { text: key }), what)));

function renderList() {
  if (state.view === "settings" || state.openId) return;
  const pane = $("#pane");
  const rows = state.loadingList || state.listError ? [] : currentRows();
  const key = [state.view, state.arg, inboxMode() ? state.tier : "", state.shownQ].join("|");
  const old = $("#lscroll");
  const keepScroll = old && pane.dataset.list === key ? old.scrollTop : state.listScroll || 0;
  const active = document.activeElement;
  const focusRow = active && old && old.contains(active) ? active.closest(".row") : null;
  const focus = focusRow ? { id: focusRow.dataset.id, part: active === focusRow ? "" : active.classList[0] } : null;

  const scroller = h("div", { class: "scroller", id: "lscroll" });
  const kids = [h("h1", { class: "view-title", text: listTitle() })];
  const status = state.status;
  if (status && status.demo_threads > 0) {
    kids.push(h("div", { class: "notice" },
      h("span", { text: "These are sample conversations, not your real inbox." }),
      h("button", { class: "link", type: "button", onclick: clearDemo }, "Remove samples")));
  }
  const connector = state.view === "channel" && status ? status.connectors.find((c) => c.name === state.arg) : null;
  if (connector && connector.last_error && !state.shownQ) {
    kids.push(h("div", { class: "notice bad" }, h("span", { text: connector.last_error }),
      h("button", { class: "link", type: "button", onclick: () => goSettings(state.arg) }, "Open settings")));
  }
  if (!state.loadingList && !state.listError && (state.shownQ || state.view === "label" || state.view === "channel")) {
    kids.push(h("div", { class: "qbar" },
      h("span", { text: state.shownQ ? `Results for “${state.shownQ}” across every queue` : state.view === "label"
        ? `Everything labelled ${categoryLabel(state.arg)}, in any queue` : `Everything that arrived through ${CONNECTOR_LABEL[state.arg]}` }),
      state.shownQ ? h("button", { class: "link", type: "button", onclick: () => { searchInput.value = ""; runSearch(""); } }, "Clear search") : null));
  }

  if (state.loadingList) {
    for (let i = 0; i < 8; i++) kids.push(h("div", { class: "row skel" }, h("i"), h("i")));
  } else if (!rows.length) {
    kids.push(emptyState(connector));
  } else {
    const list = h("div", { role: "list", "aria-label": listTitle() });
    const sorted = inboxMode() && state.tier === "P0" ? rows.filter((t) => TIER_IDS.includes(t.priority)) : rows;
    const waiting = inboxMode() && state.tier === "P0" ? rows.filter((t) => !TIER_IDS.includes(t.priority)) : [];
    sorted.forEach((t) => list.append(renderRow(t)));
    if (waiting.length) {
      list.append(h("div", { class: "subhead", text: "Not sorted yet" }));
      waiting.forEach((t) => list.append(renderRow(t)));
    }
    kids.push(list);
    if (state.total > state.fetched) {
      kids.push(h("p", { class: "list-more", text: `Showing the first ${state.fetched} of ${state.total}. Narrow with search or filters.` }));
    }
    kids.push(keyLegend(LIST_KEYS));
  }
  scroller.append(...kids);

  const parts = [listToolbar(rows)];
  if (inboxMode() && !state.listError) parts.push(inboxTabs());
  parts.push(scroller);
  pane.replaceChildren(...parts);
  pane.dataset.list = key;
  pane.dataset.convo = "";
  scroller.scrollTop = keepScroll;
  if (focus) {
    const row = [...scroller.querySelectorAll(".row")].find((item) => item.dataset.id === focus.id);
    const target = row && focus.part ? row.querySelector(`.${focus.part}`) : row;
    if (target) target.focus({ preventScroll: true });
  }
}

function setCursor(id) {
  state.cursorId = id;
  for (const row of document.querySelectorAll("#lscroll .row")) {
    const on = Number(row.dataset.id) === id;
    row.classList.toggle("cursor", on);
    if (on) { row.scrollIntoView({ block: "nearest" }); row.focus({ preventScroll: true }); }
  }
}

function moveCursor(delta) {
  const rows = currentRows();
  if (!rows.length) return;
  const index = rows.findIndex((t) => t.id === state.cursorId);
  const next = index === -1 ? (delta > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, index + delta));
  setCursor(rows[next].id);
}

// -- conversation ---------------------------------------------------------------

function identityChip(identity) {
  const meta = identity.meta || {};
  const bits = [];
  if (meta.followers != null) bits.push(`${compact(meta.followers)} followers`);
  if (meta.verified) bits.push("verified");
  if (meta.follows_me) bits.push("follows you");
  if (meta.connection) bits.push(`${meta.connection} connection`);
  const label = identity.handle || identity.name || CHANNEL_LABEL[identity.channel];
  const href = safeUrl(identity.url);
  const inner = [icon(identity.channel, 14), h("span", { class: "id-handle", text: label }), bits.length ? h("span", { class: "id-meta", text: bits.join(" · ") }) : null];
  return href
    ? h("a", { class: "identity", href, target: "_blank", rel: "noopener noreferrer" }, inner)
    : h("span", { class: "identity" }, inner);
}

function verdictSource(d) {
  if (!d.triaged) return "Waiting for the next sorting run";
  if (d.overridden) return "Set by you";
  if (d.source === "jev") return `Jev${d.category_confidence != null ? ` · ${Math.round(d.category_confidence * 100)}% sure of the category` : ""}`;
  if (d.source === "filter") return `Filed automatically: ${d.filter_reason || "automated email"}`;
  if (d.source === "rules") return "Keyword rules (add an OpenRouter key for Jev)";
  return "";
}

function meterBar(label, value, risk) {
  const percent = Math.round(value * 100);
  const fill = h("span", { class: "meter-fill" });
  fill.style.setProperty("--w", `${percent}%`);
  return h("span", { class: "meter" + (risk ? " risk" : ""), role: "img", "aria-label": `${label}: ${percent} percent` }, fill);
}

function jevCard(d) {
  const tier = TIER_IDS.includes(d.priority) ? d.priority : null;
  const title = !d.triaged ? "Not sorted yet" : d.overridden ? "Set by you"
    : d.source === "jev" ? "Sorted by Jev" : d.source === "rules" ? "Sorted by keyword rules"
    : d.source === "filter" ? "Filed without a Jev call" : "Sorted";
  const card = h("section", { class: "jev", "aria-label": "Why it is ranked here" });
  const parts = [h("div", { class: "jev-h" },
    icon("spark", 18), h("b", { text: title }),
    tier ? h("span", { class: `lb tier t-${tier}`, text: tierLabel(tier) }) : null,
    d.triaged && d.category_label ? lb(d.category_label, `c-${catColor(d.category)}`) : null,
    d.triaged ? h("span", { class: "muted", text: `Score ${Math.round(d.score || 0)} of 100` }) : null,
    h("span", { class: "muted", text: verdictSource(d) }))];
  parts[0].firstChild.classList.add("spark");
  if (d.summary) parts.push(h("p", { class: "jev-brief", text: d.summary }));

  const signals = d.signals || [];
  if (signals.length) {
    const scam = signals.find((row) => row.key === "risk" && row.value >= 0.5);
    const shown = scam ? [scam, ...signals.filter((row) => row !== scam).slice(0, 5)] : signals.slice(0, 6);
    parts.push(h("div", { class: "sig" }, shown.map((row) => {
      const risky = row.key === "risk" && row.value >= 0.5;
      return h("span", { class: "sg" + (risky ? " risk" : "") }, h("span", { text: row.label }), h("b", { text: row.level || `${Math.round(row.value * 100)}%` }));
    })));
  }

  if (d.needs_review) {
    // Settling a review is one click: the two tiers it sits between, or the categories Jev was torn over.
    const borderline = /^Borderline (P\d)/.exec(d.review_reason || "");
    let choices;
    if (borderline) {
      const index = state.boot.tiers.findIndex((t) => t.id === borderline[1]);
      choices = state.boot.tiers.slice(index, index + 2).map((t) => [t.label, { priority: t.id }]);
    } else {
      choices = (d.category_probs || []).slice(0, 3).map((option) => [option.label, { category: option.id }]);
    }
    const reason = borderline ? `Your call: right on the line for “${tierLabel(borderline[1])}”` : `Your call: ${d.review_reason}`;
    parts.push(h("div", { class: "review" },
      h("span", { class: "review-t", text: reason }),
      choices.map(([label, change]) => h("button", { class: "btn sm", type: "button", onclick: () => patch(d.id, change) }, label))));
  }
  if (d.risk) {
    parts.push(h("div", { class: "warning" }, icon("alert", 16),
      h("span", { text: "This looks like a scam or manipulation attempt. Links are not clickable here; do not share codes, passwords or payments." })));
  }

  const facts = d.extracted || {};
  const factRows = [];
  if (facts.ask) factRows.push(["They want", facts.ask]);
  if (facts.terms) factRows.push(["Terms", facts.terms]);
  if ((facts.amounts || []).length && !facts.terms) factRows.push(["Amounts mentioned", facts.amounts.join(", ")]);
  if ((facts.deadlines || []).length) factRows.push(["Timing", facts.deadlines.join(", ")]);
  if (facts.next_step) factRows.push(["Next step", facts.next_step]);
  if (factRows.length) parts.push(h("dl", { class: "facts" }, factRows.map(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])));

  // Everything Jev answered, and the owner's own say over it.
  const probs = d.category_probs || [];
  const answers = [];
  if (probs.length) {
    answers.push(h("div", { class: "ans-row" }, h("span", { class: "ans-q", text: "Category" }), h("span", { class: "ans-a", text: probs[0].label }),
      meterBar("Category", probs[0].p), h("span", { class: "ans-p", text: `${Math.round(probs[0].p * 100)}%` })));
  }
  for (const row of signals) {
    const percent = Math.round(row.value * 100);
    answers.push(h("div", { class: "ans-row" }, h("span", { class: "ans-q", text: row.label }),
      h("span", { class: "ans-a", text: row.level || (row.kind === "yesno" ? (row.value >= 0.5 ? "Yes" : "No") : `${percent} of 100`) }),
      meterBar(row.label, row.value, row.key === "risk" && row.value >= 0.5), h("span", { class: "ans-p", text: `${percent}%` })));
  }
  const more = h("details", { class: "more", open: state.jevOpen, ontoggle: (event) => { state.jevOpen = event.target.open; } },
    h("summary", {}, h("span", { text: answers.length ? `All ${answers.length} answers, and your own call` : "Set the category and priority yourself" }), icon("chevD", 16)),
    answers.length ? h("div", { class: "ans" }, answers) : null,
    probs.length > 1 ? h("p", { class: "probs" }, "Category read: ",
      probs.map((p, i) => [i ? ", " : "", h("span", { class: i === 0 ? "top" : "", text: `${p.label} ${Math.round(p.p * 100)}%` })])) : null,
    h("div", { class: "overrides" },
      h("label", { class: "field" }, h("span", { text: "Category" }),
        h("select", { class: "select", onchange: (event) => patch(d.id, { category: event.target.value || null }) },
          state.boot.categories.map((c) => h("option", { value: c.id, selected: c.id === d.category, text: c.label })))),
      h("div", { class: "field" }, h("span", { text: "Priority" }),
        h("div", { class: "seg", role: "group", "aria-label": "Priority" },
          state.boot.tiers.map((t) => h("button", {
            class: `seg-item t-${t.id}` + (d.priority === t.id ? " active" : ""), type: "button", title: t.label,
            "aria-pressed": d.priority === t.id ? "true" : "false", onclick: () => patch(d.id, { priority: t.id }),
          }, t.label)))),
      d.overridden ? h("button", { class: "link", type: "button", onclick: () => patch(d.id, { category: null, priority: null }) }, "Reset to model's call") : null));
  parts.push(more);
  card.append(...parts);
  return card;
}

function toggleMessage(id) {
  if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
  renderConvo();
}

function messageView(m, d, { open, foldable }) {
  const mine = m.direction === "out";
  const name = mine ? "You" : (m.sender || d.name);
  const avatar = h("span", { class: "m-av" + (mine ? " me" : ` c-${catColor(d.category)}`), "aria-hidden": "true", text: mine ? "Y" : initial(name) });
  const when = h("time", { class: "m-time", datetime: m.sent_at || "", text: open ? `${stamp(m.sent_at)} (${since(m.sent_at)})` : ago(m.sent_at) });
  if (!open) {
    const text = (m.text || "").replace(/\s+/g, " ").trim().slice(0, 240)
      || ((m.attachments || []).length ? plural(m.attachments.length, "attachment") : "No text");
    return h("button", { class: "m collapsed", type: "button", "aria-expanded": "false", title: "Show this message", onclick: () => toggleMessage(m.id) },
      avatar, h("span", { class: "m-main" }, h("span", { class: "m-head" }, h("span", { class: "m-name", text: name }), when),
        h("span", { class: "m-snip", text })));
  }
  const receivedOn = accountName(d.channel, d.account);
  const head = h("div", { class: "m-head" }, h("span", { class: "m-name", text: name }),
    h("span", { class: "m-addr", text: CHANNEL_LABEL[d.channel] || "" }), when);
  if (foldable) {
    head.setAttribute("role", "button");
    head.setAttribute("tabindex", "0");
    head.setAttribute("aria-expanded", "true");
    head.title = "Fold this message";
    head.addEventListener("click", () => toggleMessage(m.id));
    head.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleMessage(m.id); } });
  }
  return h("article", { class: "m" }, avatar,
    h("div", { class: "m-main" }, head,
      h("div", { class: "m-to", text: mine ? `to ${d.name}` : `to ${receivedOn || "me"}` }),
      m.text ? h("div", { class: "m-body", text: m.text }) : null,
      m.preview ? h("p", { class: "hint", text: "Inbox preview only: the rest of this message was not collected." }) : null,
      (m.attachments || []).length ? h("div", { class: "attcards" }, m.attachments.map((a) =>
        h("span", { class: "attcard" }, icon("file", 18), h("span", { text: a.name || a.type || "attachment" })))) : null));
}

function threadView(d) {
  const messages = d.messages || [];
  if (state.foldFor !== d.id) {
    // Fresh conversation: the newest message and anything carrying an attachment start open.
    state.foldFor = d.id;
    state.foldOpen = false;
    state.expanded = new Set(messages.filter((m, i) => i === messages.length - 1 || (m.attachments || []).length).map((m) => m.id));
  }
  if (state.foldCount !== messages.length) {
    state.foldCount = messages.length;
    if (messages.length) state.expanded.add(messages[messages.length - 1].id);
  }
  const foldable = messages.length > 1;
  const view = (m) => messageView(m, d, { open: state.expanded.has(m.id), foldable });
  const kids = [];
  if (messages.length > 3 && !state.foldOpen) {
    const hidden = messages.length - 2;
    kids.push(view(messages[0]),
      h("button", { class: "older", type: "button", onclick: () => { state.foldOpen = true; renderConvo(); } },
        h("span", { class: "older-n", text: String(hidden) }), h("span", { text: `${hidden} older messages` })),
      view(messages[messages.length - 1]));
  } else {
    messages.forEach((m) => kids.push(view(m)));
  }
  return h("section", { class: "thread", "aria-label": "Messages" }, kids);
}

function composer(d) {
  const body = [];
  let foot = null;
  if (state.boot.writer) {
    body.push(h("div", { class: "stances" }, state.boot.stances.map((s) =>
      h("button", { class: "btn sm", type: "button", disabled: state.draftBusy, onclick: () => makeDraft(d.id, s.id) }, s.label))));
    if (state.draftBusy) body.push(h("p", { class: "hint", text: "Writing…" }));
    if (d.draft_reply) {
      const area = h("textarea", { class: "textarea", rows: "6", "aria-label": "Reply draft", oninput: (event) => { d.draft_reply = event.target.value; } });
      area.value = d.draft_reply;
      body.push(area);
      foot = h("div", { class: "comp-f" },
        h("span", { text: "Nothing is sent from here. Copy it, then send it yourself." }),
        h("button", { class: "btn btn-primary sm", type: "button", onclick: () => navigator.clipboard.writeText(area.value).then(() => toast("Draft copied"), () => toast("Couldn't copy. Select the text and copy it manually.", { tone: "error" })) }, icon("copy", 16), "Copy"));
    }
  } else {
    body.push(h("p", { class: "hint", text: "Jev sorts but does not write. Set WRITER_MODEL in .env to any chat model on OpenRouter to get one-line briefs and reply drafts here." }));
  }
  return h("section", { class: "composer", "aria-label": "Draft a reply" },
    h("div", { class: "comp-h" }, icon("reply", 18), h("b", { text: "Draft a reply" }), h("span", { text: `to ${d.name}. This app never sends.` })),
    h("div", { class: "comp-body" }, body), foot);
}

async function makeDraft(id, stance) {
  state.draftBusy = true; renderConvo();
  try {
    const result = await api("POST", `/api/threads/${id}/draft`, { stance });
    if (state.detail && state.detail.id === id) state.detail.draft_reply = result.draft_reply;
  } catch (error) { fail(error); }
  state.draftBusy = false; renderConvo();
}

async function mergeContact(keep, drop) {
  try {
    await api("POST", "/api/contacts/merge", { keep, drop });
    toast("Contacts merged");
    await reloadRows();
    renderChrome();
    if (state.openId) await loadDetail(state.openId); else renderList();
  } catch (error) { fail(error); }
}

function convoToolbar(d, rows, index) {
  const kids = [ibtn("arrowBack", "Back to the list (U)", closeConvo)];
  if (d) {
    const stageButton = (stage, glyph, label, key) => h("button", {
      class: "ib" + (d.stage === stage ? " on" : ""), type: "button", title: `${label} (${key})`, "aria-label": label,
      "aria-pressed": String(d.stage === stage), onclick: () => setStage(d.id, stage),
    }, icon(glyph, 20));
    const snoozed = isSnoozed(d);
    const openUrl = safeUrl(d.url);
    const openLabel = (d.url || "").startsWith("https://mail.google.com/") ? "Open in Gmail"
      : d.channel === "email" ? "Reply by email" : `Open in ${CHANNEL_LABEL[d.channel]}`;
    kids.push(tsep(),
      d.stage !== "new" ? stageButton("new", "inbox", "Move back to the inbox", "N") : null,
      stageButton("shortlist", "bookmark", "Shortlist", "S"), stageButton("talking", "chat", "In talks", "T"),
      stageButton("won", "flag", "Won", "W"), stageButton("passed", "archive", "Pass", "X"),
      tsep(),
      h("button", {
        class: "ib" + (snoozed ? " on" : ""), type: "button", "aria-haspopup": "menu",
        title: snoozed ? `Snoozed to ${stamp(d.snoozed_until)}` : "Snooze (Z: 3 days)", "aria-label": snoozed ? "Snoozed" : "Snooze",
        onclick: (event) => openMenu(event.currentTarget, [
          ["Until tomorrow", () => snooze(d.id, 1)], ["3 days", () => snooze(d.id, 3)], ["1 week", () => snooze(d.id, 7)],
          ...(d.snoozed_until ? [["Wake now", () => snooze(d.id, 0)]] : []),
        ]),
      }, icon("clock", 20)),
      canMarkRead() ? ibtn("email", "Mark as unread", () => markUnread(d.id)) : null,
      ibtn("refresh", "Re-sort (R)", () => retriage(d.id)),
      openUrl ? h("a", { class: "tool-text", href: openUrl, target: "_blank", rel: "noopener noreferrer" }, icon("external", 18), h("span", { text: openLabel })) : null);
  }
  kids.push(h("span", { class: "grow" }));
  if (index !== -1) {
    const byTime = Boolean(state.shownQ) || !["review", "waiting", "snoozed", "passed"].includes(state.view);
    kids.push(h("span", { class: "pager", text: `${index + 1} of ${rows.length}` }),
      ibtn("back", byTime ? "Newer (K)" : "Previous (K)", () => openConvo(rows[index - 1].id, { replace: true }), { disabled: index === 0 }),
      ibtn("chevR", byTime ? "Older (J)" : "Next (J)", () => openConvo(rows[index + 1].id, { replace: true }), { disabled: index === rows.length - 1 }));
  }
  return h("div", { class: "toolbar", role: "toolbar", "aria-label": "Conversation actions" }, kids);
}

// Only the toolbar: its "3 of 7" follows the list without disturbing what is being read or typed below.
function redrawConvoToolbar() {
  const bar = $("#pane > .toolbar");
  const d = state.detail && state.detail.id === state.openId ? state.detail : null;
  if (!bar || !$("#cscroll") || !d) return;
  const rows = currentRows();
  bar.replaceWith(convoToolbar(d, rows, rows.findIndex((t) => t.id === state.openId)));
}

function renderConvo() {
  if (state.view === "settings" || !state.openId) return;
  const pane = $("#pane");
  const d = state.detail && state.detail.id === state.openId ? state.detail : null;
  const rows = currentRows();
  const index = rows.findIndex((t) => t.id === state.openId);
  const toolbar = convoToolbar(d, rows, index);
  const old = $("#cscroll");
  pane.dataset.list = "";

  if (!d) {
    pane.dataset.convo = "";
    pane.replaceChildren(toolbar, h("div", { class: "scroller", id: "cscroll" }, state.loadingDetail
      ? h("div", { class: "skel-block" }, h("i"), h("i"), h("i"), h("i"))
      : h("div", { class: "empty" }, h("h2", { class: "empty-t", text: "This conversation couldn't be opened" }),
        h("p", { text: "It may have been removed. The list has everything that is still here." }),
        h("div", { class: "empty-actions" }, h("button", { class: "btn", type: "button", onclick: closeConvo }, "Back to the list")))));
    return;
  }

  const tier = TIER_IDS.includes(d.priority) ? d.priority : null;
  const receivedOn = accountName(d.channel, d.account);
  const role = [d.title, d.company].filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).join(" · ");
  const head = h("header", { class: "c-head" },
    h("h1", { class: "c-subj", text: d.subject || `${CHANNEL_LABEL[d.channel] || "Conversation"} conversation with ${d.name}` }),
    d.category_label ? lb(d.category_label, `c-${catColor(d.category)}`) : null,
    tier ? h("span", { class: `lb tier t-${tier}`, text: tierLabel(tier) }) : null,
    d.stage !== "new" ? lb(stageLabel(d.stage), `c-${STAGE_COLOR[d.stage] || "default"}`) : null,
    d.needs_review ? lb("Needs review", "c-yellow", "eye", d.review_reason) : null,
    d.risk ? lb("Scam risk", "c-red", "alert") : null,
    isSnoozed(d) ? lb(`Snoozed to ${stamp(d.snoozed_until)}`, "outline", "clock") : null,
    d.stale_triage ? lb("New message, re-sorting", "outline") : null,
    h("span", { class: "grow" }),
    h("button", {
      class: "ib st" + (d.starred ? " on" : ""), type: "button", "aria-pressed": d.starred ? "true" : "false",
      "aria-label": d.starred ? "Remove star" : "Star", title: "Star (F)", onclick: () => toggleStar(d.id),
    }, icon("star", 20)));
  const who = h("div", { class: "c-who" },
    h("strong", { text: d.name }), role ? h("span", { text: role }) : null,
    (d.identities || []).map(identityChip),
    receivedOn ? h("span", { class: "identity own", title: "The account of yours that received this" }, `to ${receivedOn}`) : null);

  const facts = d.extracted || {};
  const links = (facts.links || []).filter(safeUrl);
  const noteInput = h("textarea", {
    class: "textarea", rows: "2", placeholder: "Add a private note", "aria-label": "New note",
    oninput: (event) => { state.noteText = event.target.value; },
  });
  noteInput.value = state.noteText;
  const saveNote = async () => {
    if (!noteInput.value.trim()) return;
    try {
      state.detail = await api("POST", `/api/threads/${d.id}/notes`, { body: noteInput.value });
      state.noteText = "";
      renderConvo();
    } catch (error) { fail(error); }
  };
  noteInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) saveNote(); });

  const convo = h("div", { class: "convo" },
    head, who, jevCard(d),
    (d.merge_suggestions || []).map((s) => h("div", { class: "merge" }, icon("merge", 16),
      h("span", { text: `Same person as ${s.name} on ${s.channels.map((c) => CHANNEL_LABEL[c] || c).join(" and ")}?` }),
      h("button", { class: "btn sm", type: "button", onclick: () => mergeContact(d.contact_id, s.id) }, "Merge"))),
    threadView(d),
    d.partial ? h("p", { class: "hint", text: "Only the inbox preview was collected for this LinkedIn thread. The full conversation arrives the next time the collector opens it." }) : null,
    links.length ? h("section", { class: "block" }, h("h2", { text: "Links in this conversation" }),
      h("ul", { class: "links" }, links.map((url) => h("li", {},
        d.risk ? h("span", { class: "link-text", text: url })
          : h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: url }))))) : null,
    composer(d),
    h("section", { class: "block" }, h("h2", { text: "Notes" }),
      (d.notes || []).map((n) => h("div", { class: "note" }, h("p", { text: n.body }),
        h("div", { class: "note-meta" }, h("time", { text: stamp(n.created_at) }),
          h("button", {
            class: "btn btn-icon", type: "button", "aria-label": "Delete note", title: "Delete note",
            onclick: async () => { try { await api("DELETE", `/api/notes/${n.id}`); await loadDetail(d.id); } catch (error) { fail(error); } },
          }, icon("trash", 16))))),
      noteInput,
      h("div", { class: "foot-row" }, h("span", { text: "⌘ Enter to save" }), h("button", { class: "btn sm", type: "button", onclick: saveNote }, "Save note"))),
    (d.other_threads || []).length ? h("section", { class: "block" }, h("h2", { text: `More from ${d.name}` }),
      d.other_threads.map((o) => h("button", { class: "other", type: "button", onclick: () => openInAll(o.id) },
        icon(o.channel, 16), h("span", { class: "other-text", text: o.subject || o.snippet || CHANNEL_LABEL[o.channel] }),
        accountName(o.channel, o.account) ? h("span", { class: "row-acct", text: `to ${accountName(o.channel, o.account)}` }) : null,
        h("time", { text: ago(o.last_message_at) })))) : null,
    keyLegend(CONVO_KEYS));

  const keepScroll = old && pane.dataset.convo === String(d.id) ? old.scrollTop : 0;
  const scroller = h("div", { class: "scroller", id: "cscroll" }, convo);
  pane.replaceChildren(toolbar, scroller);
  pane.dataset.convo = String(d.id);
  scroller.scrollTop = keepScroll;
  if (state.focusConvo) { state.focusConvo = false; pane.focus({ preventScroll: true }); }
}

// -- settings view --------------------------------------------------------------

const GUIDES = {
  sorting: [
    "Create an API key at openrouter.ai/keys and add a little credit. Sorting a conversation costs a fraction of a cent.",
    "Paste the key above and press Save, then Test connection.",
    "Describe yourself under “What matters to you”. Jev judges every message against that description.",
    "Press Re-sort everything whenever you change the policy.",
  ],
  instagram: [
    "Your Instagram account must be a professional (creator or business) account.",
    "At developers.facebook.com create a Business app and add the Instagram product, then open Instagram > API setup.",
    "If it says “API setup with Unified login”: generate the token (it starts with EAA) with the permissions instagram_basic and instagram_manage_messages, and copy your Instagram account ID (the number starting with 1784). Paste both above.",
    "If it says “API setup with Instagram login”: add your account and press Generate token (it starts with IGAA). Paste it above and leave the account ID blank.",
    "Save, then Test.",
    "In the Instagram app: Settings > Messages and story replies > Message controls > Connected tools > Allow access to messages.",
    "For another Instagram account press Add another account and repeat. One Unified login token can serve several accounts in the same business portfolio: paste the same token with each account's own ID.",
  ],
  gmail: [
    "Sign in with Google (recommended). Once, in Google Cloud (console.cloud.google.com): pick or create a project and enable the Gmail API for it (APIs & Services > Library > Gmail API > Enable).",
    "Set up its OAuth consent screen (Google Auth Platform: Branding, then Audience). On a Workspace domain choose Internal. For personal Gmail choose External and add your address as a test user; Google then asks you to sign in again every 7 days until the app is published.",
    "Under Clients (or Credentials), create an OAuth client of type Desktop app. Paste its client ID and secret into the two Google fields above and press Save changes.",
    "Press Sign in with Google on the account, choose the Google account and allow “Read your email”. You come back here signed in. For another inbox press Add another account and sign in again.",
    "The other way is an app password: turn on 2-Step Verification, create one at myaccount.google.com/apppasswords, enter the address and its 16 characters above, then Save and test. It opens the whole mailbox to whoever holds it, and a Workspace admin can switch app passwords off.",
  ],
  email: [
    "Install Proton Mail Bridge (it needs a paid Proton plan), sign in and let its first sync finish.",
    "In Bridge, open your account's Mailbox details. Copy the username, the IMAP port and the Bridge password into the fields above.",
    "In Bridge: Settings > Advanced settings > Export TLS certificates. Paste the full path of the exported cert.pem above.",
    "Save, then Test connection. Bridge must be running whenever the app syncs.",
  ],
  linkedin: [
    "LinkedIn has no inbox API, so Dealflow reads your inbox in a browser on this computer, on a profile kept for this app. Press Connect LinkedIn above: LinkedIn's own sign-in page opens, you sign in as usual, and the window closes by itself. Dealflow never sees or stores your password.",
    "From then on, pressing Sync (here, or the sync button above the inbox) opens that browser, reads the inbox list and stores what is new. It never runs on the timer.",
    "It only reads: nothing is typed, clicked or sent. Conversations you have not read yet on LinkedIn are not opened, so nothing gets marked as read and no read receipt is sent; their newest message comes from the inbox list.",
    "LinkedIn's User Agreement prohibits automated access, even to your own inbox, and an account that trips its checks can be restricted. A few syncs a day keeps the risk low, but it is your call.",
    "If LinkedIn asks you to sign in again or shows a security check, the sync stops and says so here. Press Sign in again; Dealflow never tries to get past it.",
    "For your full history, request LinkedIn's data export (Settings > Data privacy > Get a copy of your data) and import messages.csv above.",
  ],
};

const settingsUi = { data: null, form: {}, policy: null, results: {}, busy: "", newSlots: { instagram: [], gmail: [] } };
const ACCOUNT_NOUN = { instagram: "Instagram account", gmail: "Gmail account" };

function sectionState(id) {
  const status = settingsUi.data ? settingsUi.data.status : state.status;
  if (!status) return { dot: "off", line: "" };
  if (id === "general") return { dot: "", line: "" };
  if (id === "sorting") {
    const t = status.triage;
    if (t.last_error) return { dot: "bad", line: t.last_error, error: true };
    if (t.engine !== "jev") return { dot: "warn", line: "No OpenRouter key yet, so conversations are sorted by simple keyword rules. Add the key and everything is re-sorted by Jev." };
    return { dot: "ok", line: `Using ${t.model}${t.writer ? `, with ${t.writer} for briefs and drafts` : ""}. ${t.pending} waiting to be sorted. Spent ${money(status.spend.month)} this month, ${money(status.spend.total)} in total over ${status.spend.calls} calls.` };
  }
  const c = status.connectors.find((item) => item.name === id);
  if (!c) return { dot: "off", line: "" };
  if (c.accounts) {
    // Several accounts: each one reports its own problems in its own block below.
    const count = c.accounts.length;
    if (!count) return { dot: "off", line: "No account connected yet." };
    const failing = c.accounts.filter((a) => a.last_error).length;
    return {
      dot: failing ? "bad" : c.last_ok_at ? "ok" : "off",
      line: `${count} account${count > 1 ? "s" : ""}, ${c.threads} conversation${c.threads === 1 ? "" : "s"}.`
        + (c.last_ok_at ? ` Last synced ${since(c.last_ok_at)}.` : " Press Sync now to pull messages for the first time.")
        + (failing ? ` ${failing} need${failing > 1 ? "" : "s"} attention.` : ""),
    };
  }
  if (c.last_error) return { dot: "bad", line: c.last_error, error: true };
  if (id === "linkedin") {
    if (!c.last_ok_at) {
      return { dot: "off", line: (c.browser || {}).connected
        ? "Connected. Press Sync now to read your LinkedIn inbox for the first time."
        : "Not connected yet. Connect LinkedIn below to read it in your browser when you press Sync, or import your history." };
    }
    return { dot: c.stale ? "warn" : "ok", line: `${plural(c.threads, "conversation")}. Last collected ${since(c.last_ok_at)}${c.stale ? ", which is getting stale" : ""}.${Number(c.request_count) > 0 ? ` ${Number(c.request_count) === 1 ? "1 message request is" : `${Number(c.request_count)} message requests are`} waiting on LinkedIn.` : ""}` };
  }
  if (!c.configured) return { dot: "off", line: "Not connected." };
  if (!c.last_ok_at) return { dot: "off", line: "Saved. Press Sync now to pull messages for the first time." };
  const stats = c.last_stats || {};
  return { dot: "ok", line: `${plural(c.threads, "conversation")}. Last synced ${since(c.last_ok_at)}${stats.new_messages != null ? `, ${stats.new_messages} new messages` : ""}.${c.username ? ` Signed in as @${c.username}.` : ""}` };
}

async function loadSettings() {
  try {
    settingsUi.data = await api("GET", "/api/settings");
    state.status = settingsUi.data.status;
    takeGoogleResult(settingsUi.data);
  } catch (error) { fail(error); }
  renderSettings();
  // Opened while something is running (the sync at start-up, say): watch it closely, so the
  // buttons it disables come back the moment it ends rather than at the next slow check.
  if (state.status && state.status.job && state.status.job.running) loadStatus();
}

// After Google sends the browser back, the server hands over the outcome once.
function takeGoogleResult(data) {
  const result = data.google && data.google.result;
  if (!result) return;
  state.settingsSection = "gmail";
  toast(result.message, result.ok ? {} : { tone: "error" });  // the page opens at the top; the details sit lower
  if (!result.slot) { settingsUi.results.gmail = { ok: false, message: result.message }; return; }
  settingsUi.results[`gmail:${result.slot}`] = { ok: result.ok, message: result.message };
  // A failed sign-in on a block that was never saved: keep the block, so its message has a place.
  if (!data.accounts.gmail.slots.some((entry) => entry.slot === result.slot)) settingsUi.newSlots.gmail.push(result.slot);
}

function signedInWithGoogle(entry) {
  return Boolean(entry.secrets && entry.secrets.refresh_token && entry.secrets.refresh_token.set);
}

async function signInWithGoogle(slot) {
  settingsUi.busy = `google:${slot}`;
  renderSettings();
  try {
    const { url } = await api("POST", "/api/gmail/google/start", { slot });
    if (!/^https?:\/\//.test(url || "")) throw new Error("The server returned an unexpected sign-in address");
    location.assign(url);  // Google, then back to /oauth/google/callback, then here
  } catch (error) {
    settingsUi.busy = "";
    renderSettings();
    fail(error);
  }
}

function googlePanel() {
  const google = settingsUi.data.google || {};
  const copy = () => {
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(google.redirect_uri).then(() => toast("Redirect URI copied"), () => {});
  };
  return h("section", { class: "set-block" },
    h("h3", { text: "Sign in with Google (recommended)" }),
    h("p", { class: "set-help", text: "Dealflow asks Google for one permission, “Read your email” (gmail.readonly). It cannot send, delete or change anything, no password is stored, and you can withdraw it at any time at myaccount.google.com/permissions. An app password, by contrast, opens the whole mailbox." }),
    h("div", { class: "set-field" },
      h("label", { for: "set-google-redirect", text: "Redirect URI" }),
      h("div", { class: "set-secret" },
        h("input", { class: "input", id: "set-google-redirect", type: "text", readonly: true, spellcheck: "false",
          value: google.redirect_uri || "", "aria-describedby": "set-google-redirect-help", onfocus: (event) => event.target.select() }),
        h("button", { class: "btn btn-quiet", type: "button", onclick: copy }, icon("copy", 15), "Copy")),
      h("p", { class: "set-help", id: "set-google-redirect-help", text: "A Desktop app client needs nothing here. Only if you use a Web application client, add this exact address under its Authorized redirect URIs." })));
}

function sectionFields(id) {
  return settingsUi.data.fields.filter((field) => field.section === id);
}

// The accounts to show for a multi-account connector: the saved ones, plus any
// the owner has started adding. With none at all, one empty block to fill in.
function accountSlots(kind) {
  const info = settingsUi.data.accounts[kind];
  const saved = new Set(info.slots.map((entry) => entry.slot));
  const fresh = settingsUi.newSlots[kind].filter((slot) => !saved.has(slot));
  if (!info.slots.length && !fresh.length) fresh.push(1);
  return [
    ...info.slots,
    ...fresh.map((slot) => ({ slot, keys: info.key_names[String(slot)], values: {}, secrets: {}, locked: {}, fresh: true })),
  ].sort((a, b) => a.slot - b.slot);
}

function accountFieldSpecs(kind, entry) {
  // The Google sign-in is shown as a status, not a field; once signed in, the app password is not used.
  const google = kind === "gmail" && signedInWithGoogle(entry);
  return Object.entries(settingsUi.data.accounts[kind].fields)
    .filter(([role, spec]) => !spec.managed && !(google && role === "password"))
    .map(([role, spec]) => ({
    key: entry.keys[role], role, section: kind, label: spec.label, kind: spec.kind, help: spec.help,
    placeholder: spec.placeholder, locked: Boolean(entry.locked[role]), accountField: true,
    ...(spec.kind === "secret"
      ? { set: Boolean(entry.secrets[role] && entry.secrets[role].set), hint: (entry.secrets[role] || {}).hint || "" }
      : { value: entry.values[role] || "", is_default: !entry.values[role] }),
  }));
}

function dirtyKeys(id) {
  const keys = sectionFields(id).map((field) => field.key);
  if (settingsUi.data.accounts[id]) {
    for (const entry of accountSlots(id)) keys.push(...Object.values(entry.keys));
  }
  return keys.filter((key) => key in settingsUi.form);
}

function fieldControl(field, refresh) {
  const id = `set-${field.key}`;
  const original = field.kind === "secret" ? "" : String(field.value ?? "");
  const current = field.key in settingsUi.form ? settingsUi.form[field.key] : original;
  const change = (value) => {
    if (field.kind !== "secret" && String(value) === original) delete settingsUi.form[field.key];
    else if (field.kind === "secret" && value === "") delete settingsUi.form[field.key];
    else settingsUi.form[field.key] = value;
    refresh();
  };
  const common = { id, disabled: field.locked, "aria-describedby": field.help ? `${id}-help` : null };
  let control;
  if (field.kind === "toggle") {
    const box = h("input", { ...common, type: "checkbox", class: "switch", role: "switch", onchange: (event) => change(event.target.checked ? "true" : "false") });
    box.checked = String(current) === "true";
    return h("div", { class: "set-field set-toggle" },
      h("label", { for: id }, box, h("span", { text: field.label })),
      field.help ? h("p", { class: "set-help", id: `${id}-help`, text: field.help }) : null);
  }
  if (field.kind === "choice") {
    control = h("select", { ...common, class: "select", onchange: (event) => change(event.target.value) },
      field.options.map(([value, label]) => h("option", { value, selected: String(current) === value, text: label })));
  } else if (field.kind === "secret") {
    const removing = settingsUi.form[field.key] === null;
    control = h("div", { class: "set-secret" },
      h("input", {
        ...common, type: "password", class: "input", autocomplete: "new-password", spellcheck: "false",
        placeholder: removing ? "Will be removed when you save" : field.set ? `Saved${field.hint ? `, ends in ${field.hint}` : ""}. Paste a new one to replace it` : "Not set",
        value: typeof current === "string" ? current : "", oninput: (event) => change(event.target.value),
      }),
      field.set && !field.locked && !field.accountField ? h("button", {
        class: "btn btn-quiet", type: "button",
        onclick: () => { if (removing) delete settingsUi.form[field.key]; else settingsUi.form[field.key] = null; renderSettings(); },
      }, removing ? "Keep it" : "Remove") : null);
  } else {
    control = h("input", {
      ...common, class: "input", type: field.kind === "number" ? "number" : "text", spellcheck: "false", autocomplete: "off",
      min: field.min, max: field.max, placeholder: field.placeholder || "", value: current, oninput: (event) => change(event.target.value),
    });
  }
  return h("div", { class: "set-field" },
    h("label", { for: id, text: field.label }), control,
    field.locked ? h("p", { class: "set-help", text: "Set by an environment variable, so it cannot be changed here." })
      : field.help ? h("p", { class: "set-help", id: `${id}-help`, text: field.help }) : null);
}

async function saveSection(id, { thenTest = false } = {}) {
  const keys = dirtyKeys(id);
  if (keys.length) {
    settingsUi.busy = "save";
    renderSettings();
    try {
      const values = Object.fromEntries(keys.map((key) => [key, settingsUi.form[key]]));
      const data = await api("POST", "/api/settings", { values });
      settingsUi.data = data;
      state.status = data.status;
      keys.forEach((key) => delete settingsUi.form[key]);
      for (const name of Object.keys(settingsUi.results)) {
        if (name === id || name.startsWith(`${id}:`)) delete settingsUi.results[name];
      }
      if (settingsUi.newSlots[id]) settingsUi.newSlots[id] = [];
      state.boot = await api("GET", "/api/bootstrap");
      toast(data.restart_needed ? "Saved. The new port applies the next time you start the app." : "Saved");
    } catch (error) {
      settingsUi.busy = "";
      renderSettings();
      fail(error);
      return;
    }
    settingsUi.busy = "";
  }
  if (thenTest) return testSection(id, typeof thenTest === "object" ? thenTest.slot : undefined);
  renderSidebar();
  renderSettings();
}

// `slot` picks one account of a multi-account connector; its result is kept under "<id>:<slot>".
async function testSection(id, slot) {
  const name = slot ? `${id}:${slot}` : id;
  settingsUi.busy = `test:${name}`;
  delete settingsUi.results[name];
  renderSettings();
  try { settingsUi.results[name] = await api("POST", "/api/settings/test", { target: id, slot }); }
  catch (error) { settingsUi.results[name] = { ok: false, message: error.message }; }
  settingsUi.busy = "";
  try { settingsUi.data = await api("GET", "/api/settings"); state.status = settingsUi.data.status; } catch (error) { /* keep what we have */ }
  renderSidebar();
  renderSettings();
}

function resultLine(result) {
  return result ? h("p", { class: "set-result " + (result.ok ? "ok" : "bad"), role: "status" },
    icon(result.ok ? "check" : "alert", 15), h("span", { text: result.message })) : null;
}

// One of the owner's accounts on a multi-account connector: who it is, how its
// last sync went, its fields, and its own Test and Remove.
function accountBlock(kind, entry, position, refresh) {
  const connector = settingsUi.data.status.connectors.find((c) => c.name === kind) || {};
  const live = (connector.accounts || []).find((a) => a.slot === entry.slot);
  const keys = Object.values(entry.keys);
  const removing = !entry.fresh && keys.every((key) => settingsUi.form[key] === null);
  const busy = settingsUi.busy;
  const title = live ? live.name : entry.fresh ? `New ${ACCOUNT_NOUN[kind]}` : `${ACCOUNT_NOUN[kind]} ${position}`;
  const google = kind === "gmail" && signedInWithGoogle(entry);
  const via = google ? "Signed in with Google, read-only. " : "";

  let statusLine;
  if (removing) statusLine = h("p", { class: "acct-status", text: "Will be disconnected when you save. Conversations already pulled stay in your inbox." + (google ? " The Google sign-in is withdrawn too." : "") });
  else if (live && live.last_error) statusLine = h("p", { class: "acct-status error", text: live.last_error });
  else if (live && live.last_ok_at) statusLine = h("p", { class: "acct-status", text: `${via}${live.threads} conversation${live.threads === 1 ? "" : "s"}. Last synced ${since(live.last_ok_at)}.` });
  else if (live) statusLine = h("p", { class: "acct-status", text: via ? `${via}Not synced yet.` : "Saved. Not synced yet." });
  else if (!entry.fresh) statusLine = h("p", { class: "acct-status", text: "Incomplete: fill in the missing field and save." });
  else statusLine = null;

  const remove = () => {
    if (entry.fresh) {
      settingsUi.newSlots[kind] = settingsUi.newSlots[kind].filter((slot) => slot !== entry.slot);
      keys.forEach((key) => delete settingsUi.form[key]);
    } else if (removing) {
      keys.forEach((key) => delete settingsUi.form[key]);
    } else {
      keys.forEach((key) => { settingsUi.form[key] = null; });
    }
    renderSettings();
  };
  const removable = !Object.values(entry.locked).some(Boolean)
    && !(entry.fresh && accountSlots(kind).length === 1);  // the lone empty block has nothing to remove
  const testName = `${kind}:${entry.slot}`;
  const touched = () => keys.some((key) => key in settingsUi.form);
  const testButton = removing ? null : h("button", {
    class: "btn", type: "button", disabled: busy !== "",
    onclick: () => saveSection(kind, { thenTest: { slot: entry.slot } }),
  }, busy === `test:${testName}` ? "Testing…" : touched() || entry.fresh ? "Save and test" : "Test");

  // Gmail only: Sign in with Google. It leaves the page, so unsaved edits to this block must be dealt with first.
  const googleReady = Boolean(settingsUi.data.google && settingsUi.data.google.ready);
  const googleBlocked = () => !googleReady ? "Save the Google OAuth client ID and secret below first"
    : touched() ? "Save or undo your changes to this account first" : "";
  const googleButton = kind !== "gmail" || removing || Object.values(entry.locked).some(Boolean) ? null : h("button", {
    class: "btn" + (google ? "" : " btn-primary"), type: "button",
    disabled: busy !== "" || Boolean(googleBlocked()), title: googleBlocked(),
    onclick: () => signInWithGoogle(entry.slot),
  }, busy === `google:${entry.slot}` ? "Opening Google…" : google ? "Sign in again" : "Sign in with Google");

  return h("section", { class: "acct" + (removing ? " removing" : ""), "aria-label": title },
    h("div", { class: "acct-head" }, icon(kind, 17), h("h3", { text: title }),
      removable ? h("button", { class: "btn btn-quiet", type: "button", onclick: remove },
        removing ? "Keep it" : entry.fresh ? "Cancel" : "Remove account") : null),
    statusLine,
    removing ? null : accountFieldSpecs(kind, entry).map((field) => fieldControl(field, () => {
      refresh();
      if (testButton) testButton.textContent = touched() || entry.fresh ? "Save and test" : "Test";
      if (googleButton) { googleButton.disabled = settingsUi.busy !== "" || Boolean(googleBlocked()); googleButton.title = googleBlocked(); }
    })),
    removing ? null : h("div", { class: "set-actions" }, googleButton, testButton),
    googleButton && !googleReady && !google ? h("p", { class: "set-help", text: "Sign in with Google needs your own OAuth client first: see “How to set this up” below." }) : null,
    resultLine(settingsUi.results[testName]));
}

function accountsPanel(kind, refresh) {
  const info = settingsUi.data.accounts[kind];
  const slots = accountSlots(kind);
  const used = new Set(slots.map((entry) => entry.slot));
  const add = () => {
    // With nothing saved, the one empty block on screen is implied rather than listed.
    // List it first, or the new block would quietly take its place instead of joining it.
    if (!info.slots.length && !settingsUi.newSlots[kind].length) settingsUi.newSlots[kind].push(slots[0].slot);
    for (let slot = 1; slot <= info.max; slot++) {
      if (!used.has(slot)) { settingsUi.newSlots[kind].push(slot); break; }
    }
    renderSettings();
  };
  return h("div", { class: "accts" },
    slots.map((entry, index) => accountBlock(kind, entry, index + 1, refresh)),
    h("div", { class: "set-actions" },
      h("button", { class: "btn", type: "button", disabled: slots.length >= info.max, onclick: add },
        icon("plus", 15), "Add another account")));
}

function policyPanel() {
  const rubric = settingsUi.data.rubric;
  if (!settingsUi.policy) {
    settingsUi.policy = {
      owner_context: rubric.owner_context,
      values: Object.fromEntries(rubric.categories.map((c) => [c.id, c.value])),
      criteria: Object.fromEntries(rubric.categories.map((c) => [c.id, c.criteria])),
      tiers: { ...rubric.tiers },
      dirty: false,
    };
  }
  const policy = settingsUi.policy;
  const saveButton = h("button", { class: "btn btn-primary", type: "button", disabled: !policy.dirty, onclick: savePolicy }, "Save policy");
  const touch = () => { policy.dirty = true; saveButton.disabled = false; };

  const about = h("textarea", { class: "textarea", id: "set-owner", rows: "4", "aria-describedby": "set-owner-help",
    oninput: (event) => { policy.owner_context = event.target.value; touch(); } });
  about.value = policy.owner_context;

  const tierLabels = Object.fromEntries(state.boot.tiers.map((t) => [t.id, t.label]));
  return h("section", { class: "set-block" },
    h("h3", { text: "What matters to you" }),
    h("div", { class: "set-field" },
      h("label", { for: "set-owner", text: "About you" }), about,
      h("p", { class: "set-help", id: "set-owner-help", text: "Who you are and what you want from inbound messages. This goes to Jev with every conversation, so be specific: your work, audience size, the deals you want, the ones you never take." })),
    h("div", { class: "set-field" },
      h("span", { class: "set-label", text: "How much each category is worth" }),
      h("div", { class: "weights" }, rubric.categories.map((category) => {
        const out = h("output", { text: Math.round(policy.values[category.id] * 100) });
        const slider = h("input", { type: "range", min: "0", max: "100", step: "5", id: `w-${category.id}`,
          oninput: (event) => { policy.values[category.id] = Number(event.target.value) / 100; out.textContent = event.target.value; touch(); } });
        slider.value = String(Math.round(policy.values[category.id] * 100));
        return h("div", { class: "weight" }, h("label", { for: `w-${category.id}`, text: category.label }), slider, out);
      })),
      h("p", { class: "set-help", text: "Higher means conversations of that kind rise in the queue. Noise at 0 keeps spam at the bottom." })),
    h("div", { class: "set-field" },
      h("span", { class: "set-label", text: "Score needed for each priority" }),
      h("div", { class: "cutoffs" }, ["P0", "P1", "P2"].map((tier) => {
        const input = h("input", { class: "input", type: "number", min: "0", max: "100", id: `cut-${tier}`,
          oninput: (event) => { policy.tiers[tier] = Number(event.target.value); touch(); } });
        input.value = String(policy.tiers[tier] ?? "");
        return h("label", { class: "cutoff", for: `cut-${tier}` }, h("span", { text: tierLabels[tier] }), input);
      })),
      h("p", { class: "set-help", text: "Scores run from 0 to 100. Anything below the last number is Low." })),
    h("details", { class: "set-more" }, h("summary", { text: "How Jev recognises each category" }),
      h("p", { class: "set-help", text: "Jev returns probabilities, not explanations, so these descriptions are what steer it. Keep them concrete and non-overlapping." }),
      rubric.categories.map((category) => {
        const area = h("textarea", { class: "textarea", rows: "2", id: `crit-${category.id}`,
          oninput: (event) => { policy.criteria[category.id] = event.target.value; touch(); } });
        area.value = policy.criteria[category.id];
        return h("div", { class: "set-field" }, h("label", { for: `crit-${category.id}`, text: category.label }), area);
      })),
    h("div", { class: "set-actions" }, saveButton,
      h("button", { class: "btn", type: "button", disabled: state.status && state.status.job.running,
        onclick: () => startJob("/api/triage", { force: true }, "Re-sorting everything…") }, icon("refresh", 15), "Re-sort everything")));
}

async function savePolicy() {
  const policy = settingsUi.policy;
  try {
    const data = await api("POST", "/api/settings/rubric", {
      owner_context: policy.owner_context, category_values: policy.values, category_criteria: policy.criteria, tiers: policy.tiers,
    });
    settingsUi.data = data;
    settingsUi.policy = null;
    state.boot = await api("GET", "/api/bootstrap");
    toast("Policy saved. Re-sort to apply it to existing conversations.", {
      action: "Re-sort", onAction: () => startJob("/api/triage", { force: true }, "Re-sorting everything…"),
    });
    renderSettings();
  } catch (error) { fail(error); }
}

// LinkedIn in the owner's own browser: connect (they sign in on LinkedIn's page), sync on demand, disconnect.
function linkedinBrowserPanel() {
  const status = settingsUi.data.status;
  const connector = status.connectors.find((c) => c.name === "linkedin") || {};
  const browser = connector.browser || {};
  const busy = Boolean(status.job.running);
  const dirty = dirtyKeys("linkedin").length > 0;
  // Start a job, then redraw so the buttons stay disabled while it runs.
  const run = async (path, body, started) => {
    await startJob(path, body, started);
    if (state.status) settingsUi.data.status = state.status;
    renderSettings();
  };
  const signIn = () => run("/api/linkedin/browser/signin", {},
    "A browser window is opening on LinkedIn's sign-in page. Sign in there; it closes by itself once you are in.");
  const disconnect = async () => {
    try {
      settingsUi.data = await api("POST", "/api/linkedin/browser/disconnect", {});
      state.status = settingsUi.data.status;
      toast("LinkedIn disconnected. The conversations already collected are kept.");
    } catch (error) { fail(error); }
    settingsUi.linkedinDisconnect = false;
    renderSidebar();
    renderSettings();
  };

  const kids = [
    h("h3", { text: "Sync with your browser" }),
    h("p", { class: "set-help", text: "When you press Sync, Dealflow opens a browser on this computer, on a profile kept for this app, and reads your LinkedIn inbox there. It only reads, and never runs on the timer. LinkedIn's User Agreement prohibits automated access, even to your own inbox, and an account that trips its checks can be restricted, so using this is your call." }),
  ];
  if (!browser.available) {
    kids.push(h("p", { class: "set-result bad", role: "status" }, icon("alert", 15),
      h("span", { text: "No Chrome, Chromium, Brave or Edge was found on this computer. Install one, or enter its path under Advanced above." })));
  } else if (!browser.connected) {
    kids.push(
      h("p", { class: "set-help", text: `Uses ${browser.browser}. You sign in on LinkedIn's own page: Dealflow never sees or stores your password.` }),
      h("div", { class: "set-actions" },
        h("button", { class: "btn btn-primary", type: "button", disabled: busy, onclick: signIn }, "Connect LinkedIn")));
  } else {
    const confirming = Boolean(settingsUi.linkedinDisconnect);
    kids.push(
      h("p", { class: "set-status" + (browser.needs_sign_in ? " error" : ""), text: browser.needs_sign_in
        ? "LinkedIn asked for a sign-in or a security check, so the last sync stopped there. Press Sign in again."
        : `Connected in ${browser.browser} since ${stamp(browser.connected_at)}.` + (browser.last_sync_at ? ` Last synced ${since(browser.last_sync_at)}.` : " Not synced yet.") }),
      h("div", { class: "set-actions" },
        h("button", {
          class: "btn btn-primary", type: "button", disabled: busy || dirty, title: dirty ? "Save your changes first" : "",
          onclick: () => run("/api/sync", { connector: "linkedin" }, "Syncing LinkedIn…"),
        }, icon("refresh", 15), "Sync now"),
        h("button", { class: "btn", type: "button", disabled: busy, onclick: signIn }, "Sign in again"),
        confirming
          ? [h("button", { class: "btn", type: "button", disabled: busy, onclick: disconnect }, "Yes, disconnect"),
             h("button", { class: "btn btn-quiet", type: "button", onclick: () => { settingsUi.linkedinDisconnect = false; renderSettings(); } }, "Keep it")]
          : h("button", { class: "btn btn-quiet", type: "button", disabled: busy, onclick: () => { settingsUi.linkedinDisconnect = true; renderSettings(); } }, "Disconnect")),
      confirming ? h("p", { class: "set-help", text: "This signs Dealflow's browser out of LinkedIn and deletes its profile folder. Conversations already collected stay." }) : null);
  }
  return h("section", { class: "set-block" }, kids);
}

function linkedinPanel() {
  const days = h("input", { class: "input", type: "number", min: "1", max: "3650", id: "li-days", value: "365" });
  const file = h("input", { type: "file", accept: ".csv,text/csv", id: "li-file", class: "file" });
  const run = async () => {
    if (!file.files[0]) { toast("Choose messages.csv from LinkedIn's export first", { tone: "error" }); return; }
    try {
      const stats = await api("POST", "/api/linkedin/import", { csv: await file.files[0].text(), since_days: Number(days.value) || 365 });
      toast(`Imported ${stats.conversations} conversations, ${stats.new_messages} new messages`);
      await loadSettings();
      loadStatus();
    } catch (error) { fail(error); }
  };
  return h("section", { class: "set-block" },
    h("h3", { text: "Import your history" }),
    h("p", { class: "set-help", text: "LinkedIn's data export contains every message with exact timestamps. Importing it is safe to repeat and merges with what Claude collects." }),
    h("div", { class: "import" },
      h("div", { class: "set-field" }, h("label", { for: "li-file", text: "messages.csv" }), file),
      h("div", { class: "set-field" }, h("label", { for: "li-days", text: "Only the last (days)" }), days),
      h("button", { class: "btn", type: "button", onclick: run }, "Import")));
}

function goSettings(section) {
  if (state.view !== "settings") state.lastList = { view: state.view, arg: state.arg };
  state.view = "settings";
  state.settingsSection = section || state.settingsSection || "sorting";
  state.openId = null;
  state.detail = null;
  state.sel.clear();
  closeNav();
  closeMenu();
  writeHash(true);
  renderSidebar();
  renderSettings();
  loadSettings();
}

const DOT_WORD = { ok: "working", warn: "needs a look", bad: "needs attention", off: "not connected" };

function settingsJob(job) {
  return job.running ? [h("div", { class: "job-bar" }), h("div", { class: "job-text", text: job.detail || job.label })] : [];
}

function renderSettings() {
  if (state.view !== "settings") return;  // a late answer must not draw over the conversations
  const pane = $("#pane");
  pane.dataset.list = "";
  pane.dataset.convo = "";
  const toolbar = h("div", { class: "toolbar" },
    ibtn("arrowBack", "Back to conversations", () => goView(state.lastList.view, state.lastList.arg)),
    h("h1", { class: "set-title", text: "Settings" }));
  if (!settingsUi.data) {
    pane.replaceChildren(toolbar, h("div", { class: "scroller", id: "sscroll" }, h("div", { class: "set-page" }, h("p", { class: "hint", text: "Loading…" }))));
    return;
  }
  const data = settingsUi.data;
  const current = data.sections.find((s) => s.id === state.settingsSection) || data.sections[0];
  const id = current.id;
  const status = sectionState(id);
  const busy = settingsUi.busy;
  const dirty = dirtyKeys(id).length > 0;
  const jobRunning = data.status.job.running;

  // Redrawing must not cost the owner their place: remember what had focus, and where the caret was.
  const active = document.activeElement;
  const focusId = active && pane.contains(active) ? active.id : "";
  const focusTab = active && pane.contains(active) && active.classList.contains("set-tab") ? active.dataset.section : "";
  let caret = null;
  try { if (focusId && typeof active.selectionStart === "number") caret = [active.selectionStart, active.selectionEnd]; } catch (error) { /* not a text field */ }

  const tabs = h("div", { class: "tabs set-tabs", role: "tablist", "aria-label": "Settings sections" }, data.sections.map((section) => {
    const s = sectionState(section.id);
    const on = section.id === id;
    return h("button", {
      class: "set-tab" + (on ? " on" : ""), type: "button", role: "tab", "aria-selected": String(on), dataset: { section: section.id },
      "aria-label": s.dot ? `${section.label}, ${DOT_WORD[s.dot] || ""}` : null,
      onclick: () => { state.settingsSection = section.id; writeHash(true); renderSettings(); },
    }, h("span", { text: section.label }), s.dot ? h("span", { class: `dot ${s.dot}`, title: DOT_WORD[s.dot] || "" }) : null);
  }));

  const saveButton = h("button", { class: "btn btn-primary", type: "submit", disabled: !dirty || busy !== "" }, busy === "save" ? "Saving…" : "Save changes");
  const multi = MULTI_ACCOUNT.includes(id);  // these test each account separately, in its own block
  const testable = ["sorting", "email"].includes(id);
  const testButton = testable ? h("button", { class: "btn", type: "button", disabled: busy !== "",
    onclick: () => saveSection(id, { thenTest: true }) }, busy === `test:${id}` ? "Testing…" : "Test connection") : null;
  const refresh = () => {
    const changed = dirtyKeys(id).length > 0;
    saveButton.disabled = !changed || settingsUi.busy !== "";
    if (testButton) testButton.textContent = changed ? "Save and test" : "Test connection";
  };

  const fields = sectionFields(id);
  const basic = fields.filter((field) => !field.advanced);
  const advanced = fields.filter((field) => field.advanced);
  const form = h("form", { class: "set-form", autocomplete: "off", novalidate: true,
    onsubmit: (event) => { event.preventDefault(); saveSection(id); } },
    multi ? accountsPanel(id, refresh) : null,
    multi && basic.length ? h("h3", { class: "set-sub", text: id === "gmail" ? "For every Gmail account" : "For every Instagram account" }) : null,
    basic.map((field) => fieldControl(field, refresh)),
    advanced.length ? h("details", { class: "set-more", open: advanced.some((field) => field.key in settingsUi.form || !field.is_default && field.kind !== "secret") },
      h("summary", { text: "Advanced" }), advanced.map((field) => fieldControl(field, refresh))) : null,
    h("div", { class: "set-actions" }, fields.length || multi ? saveButton : null, testButton,
      ["instagram", "gmail", "email"].includes(id) ? h("button", { class: "btn", type: "button",
        disabled: jobRunning || dirty || !(data.status.connectors.find((c) => c.name === id) || {}).configured,
        title: dirty ? "Save your changes first" : "",
        onclick: () => startJob("/api/sync", { connector: id }, `Syncing ${CONNECTOR_LABEL[id]}…`) }, icon("refresh", 15), "Sync now") : null));
  refresh();

  const result = settingsUi.results[id];
  // After a test, its result is the freshest word: don't repeat an older sync error above it.
  const showStatus = status.line && !(status.error && result);
  const page = h("div", { class: "set-page" },
    h("div", { class: "set-head" }, h("h2", { text: current.title }),
      showStatus ? h("p", { class: "set-status" + (status.error ? " error" : ""), id: "set-status", text: status.line }) : null),
    h("div", { class: "job", id: "set-job" }, settingsJob(data.status.job)),
    form,
    resultLine(result),
    id === "gmail" ? googlePanel() : null,
    id === "sorting" ? policyPanel() : null,
    id === "linkedin" ? linkedinBrowserPanel() : null,
    id === "linkedin" ? linkedinPanel() : null,
    GUIDES[id] ? h("section", { class: "set-block" }, h("h3", { text: id === "linkedin" ? "How LinkedIn is collected" : "How to set this up" }),
      h("ol", { class: "guide" }, GUIDES[id].map((step) => h("li", { text: step })))) : null,
    id === "general" ? h("section", { class: "set-block" }, h("h3", { text: "Where things live" }),
      h("p", { class: "set-help", text: `Settings are saved to ${data.env_file} (readable only by you). The sorting policy is ${data.rubric.file}. Messages are stored in one database file on this computer. This app only reads: it never sends a message or changes anything in your accounts.` }),
      data.status.demo_threads ? h("div", { class: "set-actions" }, h("button", { class: "btn", type: "button", onclick: clearDemo }, "Remove sample conversations")) : null) : null);

  const old = $("#sscroll");
  const scroll = old ? old.scrollTop : 0;
  const scroller = h("div", { class: "scroller", id: "sscroll" }, page);
  pane.replaceChildren(toolbar, tabs, scroller);
  scroller.scrollTop = pane.dataset.section === id ? scroll : 0;
  pane.dataset.section = id;
  const again = focusTab ? [...tabs.children].find((tab) => tab.dataset.section === focusTab)
    : focusId ? document.getElementById(focusId) : null;
  if (again && pane.contains(again)) {
    again.focus({ preventScroll: true });
    try { if (caret) again.setSelectionRange(caret[0], caret[1]); } catch (error) { /* not a text field */ }
  }
}

function updateSettingsStatus() {
  // Called by the status poll: refresh what reports on the world without touching the form being typed in.
  if (!settingsUi.data || !state.status) return;
  settingsUi.data.status = state.status;
  const line = $("#set-status");
  const status = sectionState(state.settingsSection || "sorting");
  if (line && status.line) { line.textContent = status.line; line.classList.toggle("error", Boolean(status.error)); }
  for (const tab of document.querySelectorAll(".set-tab")) {
    const dot = $(".dot", tab);
    const s = sectionState(tab.dataset.section);
    if (dot && s.dot) { dot.className = `dot ${s.dot}`; dot.title = DOT_WORD[s.dot] || ""; }
  }
  const job = $("#set-job");
  if (job) job.replaceChildren(...settingsJob(state.status.job));
}

// -- keyboard -------------------------------------------------------------------

document.addEventListener("click", (event) => {
  if (!event.target.closest("#menu") && !(menuAnchor && menuAnchor.contains(event.target))) closeMenu();
});

$("#menu").addEventListener("keydown", (event) => {
  const items = [...$("#menu").querySelectorAll(".mi")];
  const index = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    event.stopPropagation();
    const step = event.key === "ArrowDown" ? 1 : -1;
    items[(index + step + items.length) % items.length].focus();
  } else if (event.key === "Tab") closeMenu({ refocus: true });
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const typing = target instanceof HTMLElement && (target.matches("input, textarea, select") || target.isContentEditable);
  if (event.key === "Escape") {
    if (!$("#menu").hidden) closeMenu({ refocus: true });
    else if ($("#app").classList.contains("nav-open")) closeNav();
    else if (typing) target.blur();
    else if (state.view === "settings") { /* nothing to step out of */ }
    else if (state.openId) closeConvo();
    else if (state.sel.size) { state.sel.clear(); renderList(); }
    return;
  }
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key.toLowerCase();
  const focusSearch = () => { searchInput.focus(); searchInput.select(); };
  if (key === "/") { event.preventDefault(); focusSearch(); return; }
  if (state.view === "settings" || !$("#menu").hidden) return;
  // Enter and Space belong to whatever control has focus.
  const onControl = target instanceof HTMLElement && target.closest("button, a, summary, [role='button']");
  if ((key === "enter" || key === " ") && onControl) return;

  const reading = Boolean(state.openId);
  const rows = currentRows();
  const focusedRow = !reading && target instanceof HTMLElement ? target.closest(".row") : null;
  const id = reading ? state.openId : focusedRow ? Number(focusedRow.dataset.id) : state.cursorId;
  const step = (delta) => {
    if (!reading) { moveCursor(delta); return; }
    const index = rows.findIndex((t) => t.id === state.openId);
    const next = index === -1 ? null : rows[index + delta];
    if (next) openConvo(next.id, { replace: true });
  };
  const actions = {
    j: () => step(1), k: () => step(-1),
    arrowdown: () => { if (!reading) moveCursor(1); }, arrowup: () => { if (!reading) moveCursor(-1); },
    o: () => { if (!reading && id) openConvo(id); }, enter: () => { if (!reading && id) openConvo(id); },
    u: () => { if (reading) closeConvo(); },
    s: () => id && setStage(id, "shortlist"), t: () => id && setStage(id, "talking"),
    w: () => id && setStage(id, "won"), x: () => id && setStage(id, "passed"), e: () => id && setStage(id, "passed"),
    n: () => id && setStage(id, "new"), z: () => id && snooze(id, 3),
    f: () => id && toggleStar(id), r: () => id && retriage(id),
  };
  if (!actions[key]) return;
  // In an open conversation the arrow keys scroll, as they should.
  if (reading && (key === "arrowdown" || key === "arrowup" || key === "enter" || key === "o")) return;
  event.preventDefault();
  actions[key]();
});

// The browser's Back and Forward buttons, and a hash typed by hand.
window.addEventListener("hashchange", () => {
  const route = parseHash();
  closeMenu();
  closeNav();
  if (route.view === "settings") {
    if (state.view !== "settings") state.lastList = { view: state.view, arg: state.arg };
    state.view = "settings";
    if (route.section) state.settingsSection = route.section;
    state.openId = null;
    state.detail = null;
    renderSidebar();
    renderSettings();
    if (!settingsUi.data) loadSettings();
    return;
  }
  const sameList = state.view === route.view && state.arg === route.arg;
  const tierChanged = Boolean(route.tier) && route.tier !== state.tier;
  const openChanged = route.openId !== state.openId;
  state.view = route.view;
  state.arg = route.arg;
  if (route.tier) state.tier = route.tier;
  state.openId = route.openId;
  if (!route.openId) state.detail = null;
  if (!sameList) {
    clearSearchBox();
    state.filters = { channel: "", category: "", account: "" };
    state.sel.clear();
    state.cursorId = null;
    state.listScroll = 0;
    state.mode = route.view === "inbox" ? "inbox" : "flat";
    state.shownQ = "";
    state.loadingList = true;
    state.loadingDetail = Boolean(state.openId);
    render();
    loadList().then(() => { if (state.openId) loadDetail(state.openId, { markRead: true }); });
  } else if (openChanged && state.openId) {
    state.cursorId = state.openId;
    state.focusConvo = true;
    renderChrome();
    loadDetail(state.openId, { markRead: true });
  } else if (openChanged || tierChanged) {
    if (tierChanged) { state.sel.clear(); state.cursorId = null; }
    render();
  }
});

// -- start ----------------------------------------------------------------------

(async function start() {
  setupChrome();
  try {
    state.boot = await api("GET", "/api/bootstrap");
  } catch (error) {
    document.body.replaceChildren(h("div", { class: "fatal" }, h("h1", { text: "Dealflow isn't responding" }), h("p", { text: error.message })));
    return;
  }
  const route = parseHash();
  state.view = route.view;
  state.arg = route.arg;
  if (route.tier) state.tier = route.tier;
  if (route.section) state.settingsSection = route.section;
  state.openId = route.view === "settings" ? null : route.openId;
  state.cursorId = state.openId;
  state.focusConvo = Boolean(state.openId);
  state.loadingDetail = Boolean(state.openId);
  state.mode = state.view === "inbox" ? "inbox" : "flat";
  render();
  await loadStatus();
  await loadList();
  if (state.view === "inbox" && !route.tier) landOnFirstBusyTab();
  if (state.view !== "settings" && state.openId) loadDetail(state.openId, { markRead: true });
})();
