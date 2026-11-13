// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The interface of the app. This module holds what the page knows, asks for the
// state every two seconds, and decides which screen is shown.

import { ApiError, api, configure } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon, render } from "./dom.js";
import { at, num } from "./format.js";
import { bootScreen, createScreen, lockedScreen, offlineScreen, pairingScreen, setupScreen } from "./gateway.js";
import { appsView, documentsView, vaultView } from "./manage.js";
import { button, chip, closeAllSheets, toast, toastError } from "./parts.js";
import { requestReauth } from "./sheets.js";
import { activityView, approvalCardKey, approvalsView, detailPanel, overviewView } from "./views.js";

const POLL_MS = 2000;
const PAGE = 50;
const root = document.getElementById("app");

const NAV = [
  ["overview", "Overview", "overview"],
  ["activity", "Activity", "activity"],
  ["approvals", "Approvals", "approvals"],
  ["documents", "Documents", "documents"],
  ["apps", "Apps", "apps"],
  ["vault", "Vault", "vault"],
];

const VIEWS = {
  overview: overviewView,
  activity: activityView,
  approvals: approvalsView,
  documents: documentsView,
  apps: appsView,
  vault: vaultView,
};

/** What each screen needs to be told. */
const NEEDS = {
  overview: ["apps", "approvals", "latest"],
  activity: ["apps", "approvals", "activity"],
  approvals: ["approvals"],
  documents: ["documents"],
  apps: ["apps", "settings"],
  vault: ["settings"],
  setup: ["documents", "apps"],
};

function freshActivity() {
  return { items: new Map(), order: [], nextBefore: null, filters: { app: "", kind: "" }, loaded: false, loadingMore: false, error: "", latest: null };
}

function freshUi() {
  return {
    pair: { code: "", busy: false, error: "", disabled: false, expired: false },
    unlock: { passphrase: "", busy: false, error: "", shake: 0 },
    create: { passphrase: "", again: "", busy: false, error: "", shown: false },
    uploads: {},
    notes: {},
    limits: null,
    word: { text: "", busy: false, error: "" },
    openWarnings: new Set(),
    verifying: false,
    verifyFailure: "",
    deciding: new Set(),
    decided: new Map(),
    approvalNotes: {},
    approvalSignature: null,
    releaseArmedAt: Number.POSITIVE_INFINITY,
    dismissQuietUntil: 0,
    armTimer: null,
    focusApproval: null,
    focusPanel: false,
    focusId: null,
    anchorKey: null,
    locking: false,
  };
}

const ctx = {
  session: null,
  state: null,
  phase: "boot", // boot, pair, stuck, ready
  offline: false,
  route: "overview",
  data: { apps: null, approvals: null, documents: null, settings: null },
  activity: freshActivity(),
  // What the page has learned about passages, by reference: title, number, collection.
  passages: new Map(),
  panel: null,
  onboarding: null,
  loadErrors: {},
  now: new Date(),
  ui: freshUi(),
  render: schedule,
  refresh,
  navigate,
  applyState,
  openPanel,
  closePanel,
  setActivityFilters,
  loadMoreActivity,
  start,
};

// -- drawing ------------------------------------------------------------------------

let queued = false;

function schedule() {
  if (queued) return;
  queued = true;
  queueMicrotask(() => {
    queued = false;
    paint();
  });
}

function sidebar() {
  const state = ctx.state;
  const waiting = state.today ? state.today.approvals_waiting : 0;
  // While the app does not answer, what this page last heard of the vault may no longer
  // hold: the foot then says that it does not know, not what it knew.
  const unknown = ctx.offline;
  return el(
    "aside",
    { class: "sidebar", key: "sidebar" },
    el(
      "div",
      { class: "brand" },
      el("img", { src: "favicon.svg", width: 24, height: 24, alt: "" }),
      el("span", { class: "brand-name" }, PRODUCT),
      state.demo ? chip("Demo", "accent") : null,
    ),
    el(
      "nav",
      { class: "nav", "aria-label": "Sections" },
      NAV.map(([name, label, glyph]) =>
        el(
          "a",
          {
            class: ["nav-item", ctx.route === name && "is-current"],
            href: `#/${name}`,
            "aria-current": ctx.route === name ? "page" : null,
            title: label,
            key: name,
          },
          icon(glyph),
          el("span", { class: "nav-label" }, label),
          name === "approvals" && waiting > 0
            ? el("span", { class: "nav-count", "aria-label": `${num(waiting)} waiting` }, num(waiting))
            : null,
        ),
      ),
    ),
    el(
      "div",
      { class: "sidebar-foot" },
      el(
        "div",
        {
          class: ["lock-state", unknown && "is-unknown"],
          title: unknown
            ? `${PRODUCT} is not answering, so this page cannot tell whether the vault is unlocked.`
            : state.locks_at
              ? `Unlocked. Locks itself ${at(state.locks_at, ctx.now)}.`
              : "Unlocked",
        },
        el("span", { class: "lock-glyph" }, icon(unknown ? "warning" : "unlock")),
        el(
          "span",
          { class: "lock-text" },
          el("span", { class: "lock-title" }, unknown ? "State unknown" : "Unlocked"),
          el("span", { class: "lock-sub" }, unknown ? `${PRODUCT} is not answering` : state.locks_at ? `Locks itself ${at(state.locks_at, ctx.now)}` : "Until you lock it"),
        ),
      ),
      el(
        "button",
        {
          type: "button",
          class: ["btn", "btn-default", "btn-block", "lock-now", ctx.ui.locking && "is-busy"],
          onclick: lockNow,
          disabled: ctx.ui.locking,
          title: unknown
            ? `${PRODUCT} is not answering. Lock now`
            : state.locks_at
              ? `Unlocked until ${at(state.locks_at, ctx.now).replace(/^(at|on) /, "")}. Lock now`
              : "Unlocked. Lock now",
        },
        icon("lock", "lock-now-act"),
        icon(unknown ? "warning" : "unlock", "lock-now-state"),
        el("span", { class: "btn-label" }, "Lock now"),
      ),
    ),
  );
}

function loadProblem() {
  const needed = ctx.onboarding ? NEEDS.setup : NEEDS[ctx.route];
  const name = needed.find((entry) => ctx.loadErrors[entry]);
  return name ? ctx.loadErrors[name] : "";
}

function shell() {
  const view = VIEWS[ctx.route](ctx);
  return el(
    "div",
    { class: ["shell", ctx.panel && "has-panel"], key: "shell" },
    sidebar(),
    el(
      "main",
      { class: "main", id: "main", tabindex: -1 },
      // What is wrong with the page stands above its title, in the block that stays in view.
      el(
        "div",
        { class: "page-top", key: "top" },
        ctx.offline
          ? el("div", { class: "offline-bar", role: "alert", key: "offline" }, icon("warning"), el("span", null, `${PRODUCT} is not answering. Is it still running in its terminal? What you see may be out of date.`))
          : loadProblem()
            ? el("div", { class: "offline-bar", role: "alert", key: "load-error" }, icon("warning"), el("span", null, `This page could not be brought up to date: ${loadProblem()} Trying again.`))
            : null,
        el(
          "header",
          { class: "page-head", key: "head" },
          el(
            "div",
            { class: "page-heading" },
            el("h1", { class: "page-title" }, view.title),
            view.lead ? el("p", { class: "page-lead" }, view.lead) : null,
          ),
          view.actions ? el("div", { class: "page-actions" }, view.actions) : null,
        ),
      ),
      el("div", { class: "page-body", key: `view-${ctx.route}` }, view.body),
    ),
    detailPanel(ctx),
  );
}

function stuckScreen() {
  return el(
    "main",
    { class: "gate", key: "stuck" },
    el(
      "section",
      { class: "gate-card" },
      el("h1", { class: "gate-title" }, "This page has no session"),
      el("p", { class: "gate-lead" }, `${PRODUCT} did not give this page a session. Reload the page. If that does not help, quit ${PRODUCT} in its terminal and start it again.`),
      button("Reload", { kind: "primary", onclick: () => window.location.reload() }),
    ),
  );
}

function screen() {
  if (ctx.phase === "pair") return pairingScreen(ctx);
  if (ctx.phase === "stuck") return stuckScreen();
  if (!ctx.state) return ctx.offline ? offlineScreen(ctx) : bootScreen();
  if (!ctx.state.store.exists) return createScreen(ctx);
  if (ctx.state.locked) return lockedScreen(ctx);
  if (ctx.onboarding) return setupScreen(ctx);
  return shell();
}

/**
 * Draw, and keep what the owner is reading where it is: when rows arrive above it,
 * the page is scrolled by exactly as much as they pushed it down.
 */
function drawInPlace() {
  let anchor = null;
  if (ctx.ui.anchorKey) {
    // The row that was just opened or closed stays put while the rows around it reflow.
    anchor = root.querySelector(`.page-body [data-key="${ctx.ui.anchorKey}"]`);
    ctx.ui.anchorKey = null;
  }
  if (!anchor && window.scrollY > 0) {
    const main = document.getElementById("main");
    const box = main ? main.getBoundingClientRect() : null;
    const hit = box ? document.elementFromPoint(box.left + Math.min(160, box.width / 2), 150) : null;
    anchor = hit && hit.closest ? hit.closest(".page-body [data-key]") : null;
  }
  const before = anchor ? anchor.getBoundingClientRect().top : 0;
  render(root, screen());
  if (anchor && anchor.isConnected) {
    const moved = anchor.getBoundingClientRect().top - before;
    if (Math.abs(moved) >= 1) window.scrollBy(0, moved);
  }
}

function paint() {
  ctx.now = new Date();
  drawInPlace();
  const state = ctx.state;
  const waiting = state && state.today ? state.today.approvals_waiting : 0;
  let title = PRODUCT;
  if (state && state.store.exists && state.locked) title = `${PRODUCT} – locked`;
  else if (waiting > 0) title = `(${num(waiting)}) ${PRODUCT}`;
  if (document.title !== title) document.title = title;
  if (ctx.ui.focusApproval && ctx.route === "approvals" && ctx.data.approvals) {
    focusApproval(ctx.ui.focusApproval);
    ctx.ui.focusApproval = null;
  }
  if (ctx.ui.focusPanel) {
    ctx.ui.focusPanel = false;
    const shown = document.getElementById("panel");
    if (shown) shown.focus();
  }
  if (ctx.ui.focusId) {
    // A control that replaced the one just used takes over the focus.
    const next = document.getElementById(ctx.ui.focusId);
    ctx.ui.focusId = null;
    if (next && !document.querySelector("dialog[open]")) next.focus();
  }
}

/**
 * Put the focus on "Don't release" of one waiting request: the one named by a link,
 * the one after a card that was just decided, or the first.
 */
function focusApproval(wanted) {
  if (document.querySelector("dialog[open]")) return;
  const entries = Array.from(root.querySelectorAll(".page-body > .approval, .page-body > .decided"));
  const cards = entries.filter((node) => node.classList.contains("approval"));
  let card = cards[0];
  if (wanted.ref) {
    const key = approvalCardKey(wanted.app, wanted.ref);
    card = cards.find((node) => node.dataset.key === key) || card;
  } else if (typeof wanted.after === "number") {
    card = entries.slice(wanted.after).find((node) => node.classList.contains("approval")) || card;
  }
  const target = card ? card.querySelector('[data-key="dismiss"]') : null;
  if (!target) return;
  // After a decision nothing may move under the pointer, so the page is not scrolled then.
  target.focus({ preventScroll: wanted.still === true || typeof wanted.after === "number" });
  if (wanted.ref && wanted.show) card.scrollIntoView({ block: "start" });
}

// -- what the page knows ------------------------------------------------------------

const tickets = {};

const fetchers = {
  apps: () => api.apps(),
  approvals: () => api.approvals(),
  documents: () => api.documents(),
  settings: () => api.settings(),
  latest: () => api.activity({ limit: 8 }),
  activity: () => api.activity({ limit: PAGE, app: ctx.activity.filters.app, kind: ctx.activity.filters.kind }),
};

function sortActivity() {
  ctx.activity.order = Array.from(ctx.activity.items.keys()).sort((a, b) => b - a);
}

function mergeActivityHead(page) {
  const activity = ctx.activity;
  const items = page.items || [];
  const known = activity.order.length ? activity.order[0] : null;
  const oldest = items.length ? items[items.length - 1].seq : null;
  const gap = activity.loaded && known !== null && oldest !== null && oldest > known && page.next_before !== null;
  if (!activity.loaded || gap) {
    activity.items = new Map();
    activity.nextBefore = page.next_before === undefined ? null : page.next_before;
  }
  for (const item of items) activity.items.set(item.seq, item);
  activity.loaded = true;
  activity.error = "";
  sortActivity();
}

const adopt = {
  apps: (value) => {
    ctx.data.apps = value;
  },
  approvals: (value) => {
    ctx.data.approvals = value;
    for (const item of (value.waiting || []).concat(value.granted || [])) {
      if (item.title) ctx.passages.set(item.ref, { title: item.title, passage: item.passage, passages: item.passages, collection: item.collection });
    }
  },
  documents: (value) => {
    ctx.data.documents = value;
  },
  settings: (value) => {
    if (!ctx.ui.limits || !ctx.ui.limits.busy) ctx.data.settings = value;
  },
  latest: (value) => {
    ctx.activity.latest = value.items || [];
  },
  activity: mergeActivityHead,
};

async function load(names) {
  await Promise.all(
    names.map(async (name) => {
      const ticket = (tickets[name] = (tickets[name] || 0) + 1);
      try {
        const value = await fetchers[name]();
        if (tickets[name] === ticket && ctx.state && !ctx.state.locked) adopt[name](value);
        delete ctx.loadErrors[name];
      } catch (error) {
        const quiet = !(error instanceof ApiError) || error.code === "locked" || error.status === 401;
        if (!quiet && tickets[name] === ticket) {
          ctx.loadErrors[name] = error.message;
          if (name === "activity") {
            ctx.activity.error = error.message;
            ctx.activity.loaded = true;
          }
        }
      }
    }),
  );
  schedule();
}

function loadRoute() {
  if (!ctx.state || ctx.state.locked || !ctx.state.store.exists) return Promise.resolve();
  return load(ctx.onboarding ? NEEDS.setup : NEEDS[ctx.route]);
}

/** Drop everything that came out of the vault, for when it locks or the session ends. */
function forget() {
  ctx.data = { apps: null, approvals: null, documents: null, settings: null };
  ctx.activity = freshActivity();
  ctx.passages = new Map();
  ctx.panel = null;
  ctx.onboarding = null;
  ctx.loadErrors = {};
  const kept = ctx.ui;
  clearTimeout(kept.armTimer);
  ctx.ui = freshUi();
  ctx.ui.pair = kept.pair;
  ctx.ui.unlock = kept.unlock;
  for (const name of Object.keys(tickets)) tickets[name] += 1;
}

let stateTicket = 0;
let lastLoad = Promise.resolve();

/** Take a state that was sent and reload what the current screen shows when it changed. */
function applyState(next, force = false) {
  stateTicket += 1;
  const previous = ctx.state;
  ctx.state = next;
  ctx.offline = false;
  const wasOpen = Boolean(previous && !previous.locked);
  const isOpen = !next.locked;
  if (wasOpen && !isOpen) {
    closeAllSheets();
    forget();
    toast("The vault is locked.", "plain");
  }
  const retry = Object.keys(ctx.loadErrors).length > 0;
  if (isOpen && (force || retry || !wasOpen || previous.cursor !== next.cursor)) lastLoad = loadRoute();
  schedule();
}

async function fetchState(force) {
  const ticket = (stateTicket += 1);
  let next;
  try {
    next = await api.state();
  } catch (error) {
    if (error.code === "offline" || error.status >= 500) {
      ctx.offline = true;
      schedule();
    }
    return;
  }
  if (ticket !== stateTicket) {
    if (force) lastLoad = loadRoute();
    return;
  }
  applyState(next, force);
}

let timer = null;

async function poll() {
  clearTimeout(timer);
  if (ctx.phase === "ready") await fetchState(false);
  timer = setTimeout(poll, POLL_MS);
}

/** Ask for the state now and reload what the current screen shows; resolves when both are in. */
async function refresh() {
  await fetchState(true);
  await lastLoad;
}

// -- activity -----------------------------------------------------------------------

function setActivityFilters(change) {
  const kept = ctx.activity.latest;
  const filters = Object.assign({}, ctx.activity.filters, change);
  ctx.activity = freshActivity();
  ctx.activity.filters = filters;
  ctx.activity.latest = kept;
  schedule();
  load(["activity"]);
}

async function loadMoreActivity() {
  const activity = ctx.activity;
  if (activity.loadingMore || activity.nextBefore === null) return;
  activity.loadingMore = true;
  schedule();
  try {
    const page = await api.activity({ limit: PAGE, before: activity.nextBefore, app: activity.filters.app, kind: activity.filters.kind });
    if (ctx.activity === activity) {
      for (const item of page.items || []) activity.items.set(item.seq, item);
      activity.nextBefore = page.next_before === undefined ? null : page.next_before;
      sortActivity();
    }
  } catch (error) {
    toastError(error);
  }
  activity.loadingMore = false;
  schedule();
}

let panelOpener = null;

function openPanel(seq, viaKeyboard = false) {
  if (ctx.panel && ctx.panel.seq === seq) {
    closePanel();
    return;
  }
  const active = document.activeElement;
  if (active && active.classList && active.classList.contains("act-row")) panelOpener = active;
  const panel = { seq, detail: null, error: "" };
  ctx.panel = panel;
  ctx.ui.focusPanel = viaKeyboard;
  ctx.ui.anchorKey = `a${Number(seq)}`;
  schedule();
  api
    .activityDetail(seq)
    .then((detail) => {
      panel.detail = detail;
    })
    .catch((error) => {
      panel.error = error.message;
    })
    .then(() => {
      if (ctx.panel === panel) schedule();
    });
}

function closePanel() {
  if (!ctx.panel) return;
  ctx.ui.anchorKey = `a${Number(ctx.panel.seq)}`;
  ctx.panel = null;
  schedule();
  const opener = panelOpener;
  panelOpener = null;
  const shown = document.getElementById("panel");
  if (opener && opener.isConnected && shown && shown.contains(document.activeElement)) opener.focus();
}

// -- moving around ------------------------------------------------------------------

function hashParts() {
  const [name, query = ""] = window.location.hash.replace(/^#\/?/, "").split("?");
  return { name: name.split("/")[0], query: new URLSearchParams(query) };
}

function routeFromHash() {
  const name = hashParts().name;
  return Object.prototype.hasOwnProperty.call(VIEWS, name) ? name : "overview";
}

/** The request a link to Approvals points at, or the first one. */
function approvalFromHash() {
  const query = hashParts().query;
  const ref = query.get("ref");
  return ref ? { app: query.get("app") || "", ref, show: true } : { first: true };
}

function onRoute() {
  const name = routeFromHash();
  if (name === ctx.route) {
    if (name === "approvals" && hashParts().query.get("ref")) {
      ctx.ui.focusApproval = approvalFromHash();
      schedule();
    }
    return;
  }
  ctx.route = name;
  if (name !== "overview" && name !== "activity") ctx.panel = null;
  if (name === "approvals") {
    // The cards appear anew, so "Release once" waits again before it can be pressed.
    ctx.ui.focusApproval = approvalFromHash();
    ctx.ui.approvalSignature = null;
  }
  ctx.ui.limits = null;
  window.scrollTo(0, 0);
  schedule();
  lastLoad = loadRoute();
}

function navigate(name) {
  if (routeFromHash() === name && ctx.route === name) return;
  window.location.hash = `#/${name}`;
}

async function lockNow() {
  if (ctx.ui.locking) return;
  ctx.ui.locking = true;
  schedule();
  try {
    applyState(await api.lock());
  } catch (error) {
    toastError(error);
  }
  ctx.ui.locking = false;
  schedule();
}

// -- starting -----------------------------------------------------------------------

/** Find out whether this page is paired, then show the vault or the pairing page. */
async function start() {
  let session;
  try {
    session = await api.session();
  } catch (_error) {
    ctx.offline = true;
    ctx.state = null;
    if (ctx.phase !== "pair") ctx.phase = "boot";
    schedule();
    clearTimeout(timer);
    timer = setTimeout(start, POLL_MS);
    return;
  }
  ctx.session = session;
  ctx.offline = false;
  if (!session.paired) {
    // Without pairing (the demo) the session comes with the answer itself. When it
    // did not, there is no code to ask for either.
    ctx.phase = session.pairing === "none" ? "stuck" : "pair";
    schedule();
    return;
  }
  ctx.phase = "ready";
  await fetchState(true);
  schedule();
  poll();
}

function sessionEnded() {
  if (ctx.phase !== "ready") return;
  ctx.phase = "boot";
  clearTimeout(timer);
  closeAllSheets();
  forget();
  ctx.state = null;
  ctx.ui.pair.expired = true;
  schedule();
  start();
}

configure({ unauthorized: sessionEnded, reauth: () => requestReauth(ctx.state ? ctx.state.demo_passphrase : null) });

window.addEventListener("hashchange", onRoute);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && ctx.panel && !document.querySelector("dialog[open]")) closePanel();
});

// A file dropped beside a drop zone must not replace the page.
for (const type of ["dragover", "drop"]) {
  window.addEventListener(type, (event) => {
    if (event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files")) event.preventDefault();
  });
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && ctx.phase === "ready") poll();
});

// A hairline under the page's title once something has scrolled beneath it.
let scrolled = false;
window.addEventListener(
  "scroll",
  () => {
    const now = window.scrollY > 6;
    if (now === scrolled) return;
    scrolled = now;
    document.documentElement.classList.toggle("is-scrolled", now);
  },
  { passive: true },
);

ctx.route = routeFromHash();
ctx.ui.focusApproval = ctx.route === "approvals" ? approvalFromHash() : null;
start();
