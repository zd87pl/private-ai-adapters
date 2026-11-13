// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The interface of the app. This module holds what the page knows, asks for the
// state every two seconds, and decides which screen is shown.

import { ApiError, api, configure } from "./api.js";
import { basicBars, basicView } from "./basic.js";
import { PRODUCT } from "./brand.js";
import { el, icon, render } from "./dom.js";
import { at, num, parseTime } from "./format.js";
import { bootScreen, createScreen, lockedScreen, offlineScreen, pairingScreen, setupScreen } from "./gateway.js";
import { appsView, documentsView, vaultView } from "./manage.js";
import { button, chip, closeAllSheets, iconButton, segmented, toast, toastError, watchSheets } from "./parts.js";
import { requestReauth } from "./sheets.js";
import { activityView, approvalCardKey, approvalsView, detailPanel, overviewView } from "./views.js";

const POLL_MS = 2000;
const PAGE = 50;
/** The Basic page: how many answers that left it shows at first, and how many "Show more" adds. */
const LEFT_FIRST = 5;
const LEFT_MORE = 20;
/** The step "First question": how many of the newest calls of the app that is being connected it lists. */
const ASKED_FIRST = 5;
/** How the stopped calls of the last 24 hours are asked for: a page, and how many older pages at most. */
const STOPS_PAGE = 20;
const STOPS_MORE_PAGES = 5;
const DAY_MS = 86400000;
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
  basic: ["apps", "approvals", "settings", "documents", "left", "stops"],
  setup: ["documents", "apps"],
};

/** The steps of setting up that are addresses: "#/setup/app" and so on. */
const SETUP_STEPS = ["app", "documents", "connect"];

const VIEW_OPTIONS = [
  { value: "basic", label: "Basic", title: "The everyday view: approvals, what left, documents and apps." },
  { value: "pro", label: "Pro", title: "Every page and every setting." },
];

function freshActivity() {
  return { items: new Map(), order: [], nextBefore: null, filters: { app: "", kind: "" }, loaded: false, loadingMore: false, error: "", latest: null };
}

/** The answers that left, newest first, as the Basic page lists them. */
function freshLeft() {
  return { items: new Map(), order: [], nextBefore: null, loaded: false, loadingMore: false };
}

function freshUi() {
  return {
    pair: { code: "", busy: false, error: "", disabled: false, expired: false },
    // `waited`: how many passages were waiting when this page saw the vault lock.
    unlock: { passphrase: "", busy: false, error: "", shake: 0, waited: 0 },
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
    focusView: false,
    anchorKey: null,
    locking: false,
    // The Basic page: whether every waiting passage is shown, what was last said to a screen
    // reader about how many wait, what is unfolded, and which "Copy" was just pressed.
    allApprovals: false,
    waitingCount: null,
    waitingSaid: "",
    docsOpen: false,
    panelMore: false,
    copied: null,
    // How many sheets lie over the page. The Basic page has its cards and its sheets on one page.
    sheets: 0,
  };
}

const ctx = {
  session: null,
  state: null,
  phase: "boot", // boot, pair, stuck, ready
  offline: false,
  route: "overview",
  // The step of setting up that the address names, and on its last step the app it is about.
  setupStep: "app",
  setupApp: "",
  data: { apps: null, approvals: null, documents: null, settings: null },
  activity: freshActivity(),
  // What the Basic page lists: the answers that left, and the calls that were stopped.
  left: freshLeft(),
  stops: null,
  // The newest calls under the name of the app that is being connected, for the step "First question".
  asked: null,
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
  loadMoreLeft,
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

/** The choice between the two views. The one that is on screen is the one selected. */
function viewChoice(shown) {
  return segmented({ name: "view", legend: "View", value: shown, options: VIEW_OPTIONS, onchange: chooseView });
}

/** The view control of Pro, at the foot of the sidebar: the choice, or one button where the sidebar is icons. */
function viewSwitch() {
  return el(
    "div",
    { class: "view-switch", key: "view" },
    el("span", { class: "view-label" }, "View"),
    viewChoice("pro"),
    iconButton("view", "Switch to Basic view", { id: "view-basic", onclick: () => chooseView("basic") }),
  );
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
      viewSwitch(),
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

/**
 * While the view that is kept is Basic and a page of Pro is shown, the owner is on a
 * visit: they followed a link from Basic, or typed an address. The bar offers both
 * ways on, so that nobody has to press an option that is already selected.
 */
function visitBar() {
  const kept = ctx.state.interface;
  if (!kept || kept.view !== "basic") return null;
  return el(
    "nav",
    { class: "visit-bar", key: "visit", "aria-label": "Back to Basic" },
    el("a", { class: "btn btn-default btn-small", id: "visit-back", href: "#/basic" }, el("span", { class: "btn-label" }, "← Back to Basic")),
    el("span", { class: "visit-text" }, "You are in Pro, where every setting is. Your view stays Basic."),
    button("Stay in Pro", { kind: "quiet", small: true, id: "visit-stay", onclick: stayInPro }),
  );
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
      visitBar(),
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

function topbar() {
  const state = ctx.state;
  // While the app does not answer, the bar says that it does not know, not what it knew.
  const unknown = ctx.offline;
  let lock = "Unlocked until you lock it";
  if (unknown) lock = "State unknown";
  else if (state.locks_at) lock = `Unlocked until ${at(state.locks_at, ctx.now).replace(/^(at|on) /, "")}`;
  return el(
    "header",
    { class: "topbar", key: "topbar" },
    el(
      "div",
      { class: "brand" },
      el("img", { src: "favicon.svg", width: 24, height: 24, alt: "" }),
      el("span", { class: "brand-name" }, PRODUCT),
      state.demo ? chip("Demo", "accent") : null,
    ),
    el(
      "div",
      { class: "topbar-lock" },
      el("span", { class: ["lock-state", unknown && "is-unknown"] }, lock),
      el(
        "button",
        { type: "button", class: ["btn", "btn-default", "lock-now", ctx.ui.locking && "is-busy"], onclick: lockNow, disabled: ctx.ui.locking },
        icon("lock"),
        el("span", { class: "btn-label" }, "Lock now"),
      ),
    ),
  );
}

/** The frame of the Basic page: one column under a top bar, the view at its foot. */
function basicShell() {
  const problem = loadProblem();
  const bar = basicBars(ctx, problem);
  return el(
    "div",
    { class: ["basic", ctx.panel && "has-panel"], key: "basic" },
    // The top bar and, while there is one, the bar that says what is wrong: both stay in view.
    el("div", { class: "basic-top", key: "top" }, topbar(), bar ? el("div", { class: "basic-bar", key: "bar" }, bar) : null),
    el("main", { class: "basic-main", id: "main", tabindex: -1 }, el("div", { class: "page-body", key: "view-basic" }, basicView(ctx, problem))),
    el(
      "footer",
      { class: "viewbar", key: "viewbar" },
      el("span", { class: "view-label" }, "View"),
      viewChoice("basic"),
      el("p", { class: "viewbar-text" }, "Basic shows what you use every day; Pro shows every setting. Both show the same vault."),
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
  // The first run, as long as it lives in this page's memory. The focused steps of the
  // route "setup" take its place; until they are built that route opens the page below.
  if (ctx.onboarding) return setupScreen(ctx);
  if (ctx.route === "basic") return basicShell();
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
  // The icon of the tab carries a dot while a passage waits for the owner.
  const tabIcon = document.querySelector('link[rel="icon"]');
  const wanted = waiting > 0 ? "favicon-waiting.svg" : "favicon.svg";
  if (tabIcon && tabIcon.getAttribute("href") !== wanted) tabIcon.setAttribute("href", wanted);
  const showsCards = ctx.route === "approvals" || ctx.route === "basic" || ctx.route === "setup";
  if (ctx.ui.focusApproval && showsCards && ctx.data.approvals) {
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
  if (ctx.ui.focusView) {
    // After a switch the focus stays on the control that made it, which is in the same corner in both views.
    ctx.ui.focusView = false;
    const chosen = document.querySelector('input[name="view"]:checked');
    const narrow = document.getElementById("view-basic");
    const next = chosen && chosen.offsetParent !== null ? chosen : narrow;
    if (next && !document.querySelector("dialog[open]")) next.focus();
  }
}

/**
 * Put the focus on "Don't release" of one waiting request: the one named by a link,
 * the one after a card that was just decided, or the first.
 */
function focusApproval(wanted) {
  if (document.querySelector("dialog[open]")) return;
  const entries = Array.from(root.querySelectorAll(".page-body > .approval, .page-body > .decided, .approval-stack > .approval, .approval-stack > .decided"));
  const cards = entries.filter((node) => node.classList.contains("approval"));
  let card = cards[0];
  if (wanted.ref) {
    const key = approvalCardKey(wanted.app, wanted.ref);
    card = cards.find((node) => node.dataset.key === key) || card;
  } else if (typeof wanted.after === "number") {
    card = entries.slice(wanted.after).find((node) => node.classList.contains("approval")) || card;
  }
  const target = card ? card.querySelector('[data-key="dismiss"]') : null;
  if (!target) {
    // The last card of the Basic page was decided: its headline says so and takes the focus.
    const headline = document.getElementById("basic-status");
    if (headline && typeof wanted.after === "number") headline.focus({ preventScroll: true });
    return;
  }
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
  // The two lists of the Basic page are asked for by kind. Neither is made by picking from the newest entries.
  left: () => api.activity({ limit: LEFT_FIRST, kind: "release" }),
  stops: loadStops,
  // What one app asked for, by its name: for the step that waits for the first request of that app.
  asked: () => (ctx.setupApp ? api.activity({ limit: ASKED_FIRST, app: ctx.setupApp }) : Promise.resolve({ items: [] })),
};

/**
 * The entries of calls that were stopped, as far back as 24 hours. The record keeps one
 * entry per kind of refusal and hour with a count, so one page nearly always holds them
 * all. So that a count is never short, older pages are asked for while the oldest entry
 * in hand is still newer than 24 hours.
 */
async function loadStops() {
  const since = Date.now() - DAY_MS;
  let page = await api.activity({ limit: STOPS_PAGE, kind: "refusal" });
  const items = (page.items || []).slice();
  for (let round = 0; round < STOPS_MORE_PAGES; round += 1) {
    const oldest = items.length ? parseTime(items[items.length - 1].time) : null;
    const more = page.next_before !== null && page.next_before !== undefined;
    if (!more || !oldest || oldest.getTime() <= since) break;
    page = await api.activity({ limit: STOPS_PAGE, kind: "refusal", before: page.next_before });
    for (const item of page.items || []) items.push(item);
  }
  return items;
}

function sortList(list) {
  list.order = Array.from(list.items.keys()).sort((a, b) => b - a);
}

function sortActivity() {
  sortList(ctx.activity);
}

/** Take the newest page of a list that grows at its head into what is already shown of it. */
function mergeHead(list, page) {
  const items = page.items || [];
  const known = list.order.length ? list.order[0] : null;
  const oldest = items.length ? items[items.length - 1].seq : null;
  const gap = list.loaded && known !== null && oldest !== null && oldest > known && page.next_before !== null;
  if (!list.loaded || gap) {
    list.items = new Map();
    list.nextBefore = page.next_before === undefined ? null : page.next_before;
  }
  for (const item of items) list.items.set(item.seq, item);
  list.loaded = true;
  sortList(list);
}

function mergeActivityHead(page) {
  mergeHead(ctx.activity, page);
  ctx.activity.error = "";
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
  left: (value) => mergeHead(ctx.left, value),
  stops: (value) => {
    ctx.stops = value;
  },
  asked: (value) => {
    ctx.asked = value.items || [];
  },
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
  ctx.left = freshLeft();
  ctx.stops = null;
  ctx.asked = null;
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
    // This page saw the vault lock. What waited then was not released; the locked screen says how many.
    const waited = previous.today ? previous.today.approvals_waiting : 0;
    closeAllSheets();
    forget();
    ctx.ui.unlock.waited = waited;
    toast("The vault is locked.", "plain");
  }
  // Which page an address opens that names none is decided by the view that is kept, once the app has said it.
  if (next.interface && !(previous && previous.interface)) settleRoute();
  // Whenever the Basic page opens, the focus goes to its headline, never to a button of a card.
  if (isOpen && !wasOpen && ctx.route === "basic") ctx.ui.focusId = "basic-status";
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

/** "Show more" on the Basic page: the next answers that left, older than the oldest shown. */
async function loadMoreLeft() {
  const left = ctx.left;
  if (left.loadingMore || left.nextBefore === null || !left.order.length) return;
  left.loadingMore = true;
  schedule();
  try {
    const page = await api.activity({ limit: LEFT_MORE, kind: "release", before: left.order[left.order.length - 1] });
    if (ctx.left === left) {
      for (const item of page.items || []) left.items.set(item.seq, item);
      left.nextBefore = page.next_before === undefined ? null : page.next_before;
      sortList(left);
    }
  } catch (error) {
    toastError(error);
  }
  left.loadingMore = false;
  schedule();
}

let panelOpener = null;

function openPanel(seq, viaKeyboard = false) {
  if (ctx.panel && ctx.panel.seq === seq) {
    closePanel();
    return;
  }
  const active = document.activeElement;
  if (active && active.classList && (active.classList.contains("act-row") || active.classList.contains("stop-row"))) panelOpener = active;
  const panel = { seq, detail: null, error: "" };
  ctx.panel = panel;
  ctx.ui.panelMore = false;
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
  const [path, query = ""] = window.location.hash.replace(/^#\/?/, "").split("?");
  const [name, step = ""] = path.split("/");
  return { name, step, query: new URLSearchParams(query) };
}

/** The page that opens when the address names none: Basic where that is the view that is kept. */
function defaultRoute() {
  const kept = ctx.state ? ctx.state.interface : null;
  return kept && kept.view === "basic" ? "basic" : "overview";
}

/**
 * The route the address names. The six pages of Pro are always Pro pages and "#/basic"
 * is always the Basic page, whatever view is kept. "#/setup/…" names a step of setting
 * up; its second part is kept in `ctx.setupStep`.
 */
function routeFromHash() {
  const { name, step, query } = hashParts();
  if (Object.prototype.hasOwnProperty.call(VIEWS, name)) return name;
  if (name === "basic") return "basic";
  if (name === "setup") {
    ctx.setupStep = SETUP_STEPS.includes(step) ? step : "app";
    ctx.setupApp = query.get("app") || "";
  }
  // The focused steps are not built yet: until they are, "#/setup/…" opens what an address without a page opens.
  return defaultRoute();
}

/** The first state with the view is in: an address that names no page now has one. */
function settleRoute() {
  const name = routeFromHash();
  if (name === ctx.route) return;
  ctx.route = name;
  ctx.panel = null;
  ctx.ui.focusApproval = null;
  ctx.ui.approvalSignature = null;
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
  const from = ctx.route;
  ctx.route = name;
  // The details of an entry stay open between Overview and Activity, and nowhere else.
  if (name === "basic" || from === "basic" || (name !== "overview" && name !== "activity")) ctx.panel = null;
  ctx.ui.focusApproval = null;
  if (name === "approvals") {
    // The cards appear anew, so "Release once" waits again before it can be pressed.
    ctx.ui.focusApproval = approvalFromHash();
    ctx.ui.approvalSignature = null;
  }
  if (name === "basic" || name === "setup") {
    // Here too the cards appear anew. The focus goes to the headline, never to a button of a card.
    ctx.ui.approvalSignature = null;
    ctx.ui.allApprovals = false;
    if (name === "basic") ctx.ui.focusId = "basic-status";
  }
  ctx.ui.limits = null;
  window.scrollTo(0, 0);
  schedule();
  lastLoad = loadRoute();
}

/** Open a page by its name. The route changes at once, not when the browser has told of the new address. */
function navigate(name) {
  if (hashParts().name !== name) window.location.hash = `#/${name}`;
  onRoute();
}

/** Keep the view the owner chose. Resolves to whether it was kept. Nothing is said when it was: the screen is the answer. */
async function keepView(view) {
  try {
    applyState(await api.setInterface({ view }));
    return true;
  } catch (error) {
    toastError(error);
    return false;
  }
}

/**
 * The owner pressed "Basic" or "Pro" in the view control. The choice is kept and its
 * page opens: the Basic page, or Overview. On a visit to Pro the view that is kept is
 * Basic already, so going back keeps nothing new.
 */
async function chooseView(view) {
  const kept = ctx.state ? ctx.state.interface : null;
  const known = Boolean(kept && kept.stored && kept.view === view);
  if (!known && !(await keepView(view))) return;
  ctx.ui.focusView = view === "pro";
  navigate(view === "basic" ? "basic" : "overview");
}

/** "Stay in Pro" on the bar of a visit: Pro becomes the view that is kept, and the bar goes. */
async function stayInPro() {
  if (await keepView("pro")) ctx.ui.focusId = "main";
  schedule();
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

// The page is drawn again whenever a sheet has opened or closed, in the same turn: where cards
// lie under the sheet, "Release once" waits again before a press can reach it.
watchSheets((open) => {
  ctx.ui.sheets = open;
  schedule();
});

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

// Until the app has said which view is kept, an address that names no page counts as Overview.
ctx.route = routeFromHash();
ctx.ui.focusApproval = ctx.route === "approvals" ? approvalFromHash() : null;
start();
