// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The interface of the app. This module holds what the page knows, asks for the
// state every two seconds, and decides which screen is shown. An open vault is shown
// in one of two frames: the simple one (a sidebar of four entries, which at 720 px
// and narrower becomes a bar at the top and one at the bottom) for Home, Documents,
// Activity, Settings and "How it works"; and the frame of the six pages with every
// setting, under "#/pro/…", which keeps its English.

import { ApiError, api, configure } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon, render } from "./dom.js";
import { at, clock, num, parseTime } from "./format.js";
import { bootScreen, createScreen, lockedScreen, offlineScreen, pairingScreen } from "./gateway.js";
import { freshHome, homePage, leaveHome, settleFirst } from "./home.js";
import { appsView, documentsView, vaultView } from "./manage.js";
import { activityPage, documentsPage, howPage, settingsPage } from "./pages.js";
import { button, chip, closeAllSheets, languageSwitch, problemBar, toast, toastError, watchSheets } from "./parts.js";
import { requestReauth } from "./sheets.js";
import { chooseLanguage, chosenLanguage, keptLanguage, t } from "./texts.js";
import { activityView, approvalCardKey, approvalsView, detailPanel, overviewView } from "./views.js";

const POLL_MS = 2000;
const PAGE = 50;
/** Home: how many of the newest things that were given to apps it asks for. */
const LEFT_FIRST = 3;
/** The page Activity: how many rows it asks for at a time. */
const ROWS_PAGE = 20;
/** How the stopped calls of the last 24 hours are asked for: a page, and how many older pages at most. */
const STOPS_PAGE = 20;
const STOPS_MORE_PAGES = 5;
const DAY_MS = 86400000;
const root = document.getElementById("app");

/** The four entries of the simple frame: the page each opens, the key of its word, its glyph. */
const NAV = [
  ["home", "nav.home", "home"],
  ["documents", "nav.documents", "documents"],
  ["activity", "nav.activity", "activity"],
  ["settings", "nav.settings", "gear"],
];

/** The pages of the simple frame beside Home, by the word of their address. */
const PAGES = {
  documents: documentsPage,
  activity: activityPage,
  settings: settingsPage,
  how: howPage,
};

/** The six pages with every setting, in their own frame and in English: the word after "#/pro/", its name, its glyph. */
const PRO_NAV = [
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

/** Of the six pages, those whose address from before ("#/apps") is not the address of a simple page. */
const OLD_ADDRESSES = ["overview", "approvals", "apps", "vault"];

/** What each screen needs to be told, by its route. */
const NEEDS = {
  "pro/overview": ["apps", "approvals", "latest"],
  "pro/activity": ["apps", "approvals", "activity"],
  "pro/approvals": ["approvals"],
  "pro/documents": ["documents"],
  "pro/apps": ["apps", "settings"],
  "pro/vault": ["settings"],
  // The documents are asked for on Home too: its first card adds one, and its box that asks
  // has to know whether the invented sample is the only one.
  home: ["apps", "approvals", "documents", "left", "stops"],
  documents: ["documents", "apps"],
  activity: ["apps", "rows"],
  settings: [],
  how: [],
};

function freshActivity() {
  return { items: new Map(), order: [], nextBefore: null, filters: { app: "", kind: "" }, loaded: false, loadingMore: false, error: "", latest: null };
}

/** A list of entries of one kind, newest first, as far as it was loaded: the answers that left, as Home lists them. */
function freshLeft() {
  return { items: new Map(), order: [], nextBefore: null, loaded: false, loadingMore: false };
}

/** The page Activity: which of its two lists is shown, and each as far as it was loaded. */
function freshRows(tab = "release") {
  return { tab, release: freshLeft(), refusal: freshLeft() };
}

function freshUi() {
  return {
    // `refused`: the code the app refused last, which is not sent again as it stands. `failed`: another error, as it came.
    pair: { code: "", busy: false, wrong: false, refused: "", failed: null, disabled: false, expired: false },
    // `waited`: how many passages were waiting when this page saw the vault lock.
    unlock: { passphrase: "", busy: false, failed: null, shake: 0, waited: 0 },
    create: { passphrase: "", again: "", busy: false, failed: null, shown: false },
    uploads: {},
    notes: Object.create(null), // by group name, and a group may be named "constructor"
    limits: null,
    word: { text: "", busy: false, error: "" },
    openWarnings: new Set(),
    verifying: false,
    verifyFailure: "",
    deciding: new Set(),
    decided: new Map(),
    // By app and reference: the cards that say their text changed, and the cards whose details are unfolded.
    approvalNotes: {},
    approvalDetails: new Set(),
    approvalSignature: null,
    releaseArmedAt: Number.POSITIVE_INFINITY,
    dismissQuietUntil: 0,
    armTimer: null,
    focusApproval: null,
    focusPanel: false,
    focusId: null,
    anchorKey: null,
    locking: false,
    // Home: whether every waiting passage is shown, what was last said to a screen reader
    // about how many wait, what is unfolded, and which "Copy" was just pressed.
    allApprovals: false,
    waitingCount: null,
    waitingSaid: null,
    panelMore: false,
    copied: null,
    // How many sheets lie over the page. Home has its cards and its sheets on one page.
    sheets: 0,
    // What Home remembers: the box that asks and its answer, "Start here", the demo's card.
    home: freshHome(),
  };
}

const ctx = {
  session: null,
  state: null,
  phase: "boot", // boot, pair, stuck, ready
  offline: false,
  route: "pro/overview",
  data: { apps: null, approvals: null, documents: null, settings: null },
  activity: freshActivity(),
  // What Home lists: the newest things that were given to apps, and the calls that were stopped.
  left: freshLeft(),
  stops: null,
  // What the page Activity lists: what was given to apps, and what was stopped.
  rows: freshRows(),
  // What the page has learned about passages, by reference: title, number, collection.
  passages: new Map(),
  panel: null,
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
  showRows,
  moreRows,
  keepView,
  start,
  chooseLanguage: pressLanguage,
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

/** The mark, the name and, in a demo, its chip: the head of both sidebars. */
function brandMark() {
  return [
    el("img", { src: "favicon.svg", width: 24, height: 24, alt: "" }),
    el("span", { class: "brand-name" }, PRODUCT),
    ctx.state.demo ? chip(t("common.chip.demo"), "accent") : null,
  ];
}

/** The sidebar of the six pages with every setting. Its first entry leads back to Home. */
function sidebar() {
  const state = ctx.state;
  const waiting = state.today ? state.today.approvals_waiting : 0;
  // While the app does not answer, what this page last heard of the vault may no longer
  // hold: the foot then says that it does not know, not what it knew.
  const unknown = ctx.offline;
  return el(
    "aside",
    { class: "sidebar", key: "sidebar" },
    el("div", { class: "brand" }, brandMark()),
    el(
      "nav",
      { class: "nav", "aria-label": "Sections" },
      // One entry above the six: the way back to the simple pages.
      el("a", { class: "nav-item nav-home", id: "pro-home", href: "#/home", key: "home" }, icon("home"), el("span", { class: "nav-home-label" }, t("pro.button.home"))),
      PRO_NAV.map(([name, label, glyph]) =>
        el(
          "a",
          {
            class: ["nav-item", ctx.route === `pro/${name}` && "is-current"],
            href: `#/pro/${name}`,
            "aria-current": ctx.route === `pro/${name}` ? "page" : null,
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
      languageSwitch(pressLanguage),
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

/** The lists the screen that is shown needs. */
function needs() {
  return NEEDS[ctx.route];
}

function loadProblem() {
  const name = needs().find((entry) => ctx.loadErrors[entry]);
  return name ? ctx.loadErrors[name] : "";
}

/** The frame of the six pages with every setting: today's sidebar, the page, the side panel. */
function shell() {
  const view = VIEWS[ctx.route.slice("pro/".length)](ctx);
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
        problemBar(ctx, loadProblem()),
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

/**
 * The sidebar of the simple frame: the mark and the name, the four entries, and at
 * its foot the language, until when the vault is unlocked, and "Lock now". At 720 px
 * and narrower the stylesheet lays the same parts out as a bar at the top (the mark,
 * the name, the two languages as "EN" and "PL", "Lock now" as a button with an icon)
 * and a bar at the bottom of the window with the four entries.
 */
function simpleSidebar() {
  const state = ctx.state;
  const waiting = state.today ? state.today.approvals_waiting : 0;
  // While the app does not answer, what this page last heard of the vault may no longer
  // hold: the foot then says that it does not know, not what it knew.
  const unknown = ctx.offline;
  let lock = t("nav.status.open");
  if (unknown) lock = t("nav.status.unknown");
  else if (state.locks_at) lock = t("nav.status.until", { time: clock(state.locks_at, ctx.now) });
  return el(
    "aside",
    { class: "side", key: "side" },
    el("div", { class: "side-brand brand" }, brandMark()),
    el(
      "nav",
      { class: "side-nav", "aria-label": t("nav.label") },
      NAV.map(([name, key, glyph]) =>
        el(
          "a",
          { class: ["side-item", ctx.route === name && "is-current"], href: `#/${name}`, "aria-current": ctx.route === name ? "page" : null, key: name },
          icon(glyph),
          el("span", { class: "side-label" }, t(key)),
          // What waits for the owner is on Home: its entry carries the number.
          name === "home" && waiting > 0 ? el("span", { class: "nav-count", "aria-label": t("nav.count.waiting", { n: waiting }) }, num(waiting)) : null,
        ),
      ),
    ),
    el(
      "div",
      { class: "side-foot" },
      languageSwitch(pressLanguage),
      el("p", { class: ["side-lock", unknown && "is-unknown"], id: "side-lock" }, lock),
      el(
        "button",
        { type: "button", class: ["btn", "btn-default", "btn-block", "lock-now", ctx.ui.locking && "is-busy"], onclick: lockNow, disabled: ctx.ui.locking, "aria-label": t("nav.button.lock") },
        icon("lock"),
        el("span", { class: "btn-label" }, t("nav.button.lock")),
      ),
    ),
  );
}

/**
 * The frame of the simple pages: the sidebar, one column, the side panel. The block
 * at the head of the column stays in view: it holds the bar that says what is wrong,
 * while there is one, and under it the title of the page.
 */
function simpleShell() {
  const problem = loadProblem();
  // Home is told what is wrong with the page: its cards wait again when the bar above them comes or goes.
  const page = ctx.route === "home" ? homePage(ctx, problem) : PAGES[ctx.route](ctx);
  return el(
    "div",
    { class: ["simple", ctx.panel && "has-panel"], key: "simple" },
    simpleSidebar(),
    el(
      "main",
      { class: "column", id: "main", tabindex: -1 },
      el(
        "div",
        { class: "column-top", key: "top" },
        problemBar(ctx, problem),
        page.title
          ? el(
              "header",
              { class: "column-head", key: "head" },
              el("h1", { class: "column-title", id: "page-title", tabindex: -1 }, page.title),
              page.aside ? el("span", { class: "column-aside", id: "page-count" }, page.aside) : null,
            )
          : null,
      ),
      el("div", { class: "page-body", key: `view-${ctx.route}` }, page.body),
    ),
    detailPanel(ctx),
  );
}

function stuckScreen() {
  return el(
    "main",
    { class: "gate", key: "stuck" },
    el("div", { class: "gate-lang", key: "lang" }, languageSwitch(pressLanguage)),
    el(
      "section",
      { class: "gate-card" },
      el("h1", { class: "gate-title" }, t("stuck.title")),
      el("p", { class: "gate-lead" }, t("stuck.say.1")),
      button(t("stuck.button.reload"), { onclick: () => window.location.reload() }),
    ),
  );
}

function screen() {
  if (ctx.phase === "pair") return pairingScreen(ctx);
  if (ctx.phase === "stuck") return stuckScreen();
  if (!ctx.state) return ctx.offline ? offlineScreen(ctx) : bootScreen();
  if (!ctx.state.store.exists) return createScreen(ctx);
  if (ctx.state.locked) return lockedScreen(ctx);
  if (ctx.route.startsWith("pro/")) return shell();
  return simpleShell();
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
  if (state && state.store.exists && state.locked) title = t("nav.tab.locked");
  else if (waiting > 0) title = `(${num(waiting)}) ${PRODUCT}`;
  if (document.title !== title) document.title = title;
  // The icon of the tab carries a dot while a passage waits for the owner.
  const tabIcon = document.querySelector('link[rel="icon"]');
  const wanted = waiting > 0 ? "favicon-waiting.svg" : "favicon.svg";
  if (tabIcon && tabIcon.getAttribute("href") !== wanted) tabIcon.setAttribute("href", wanted);
  const showsCards = ctx.route === "pro/approvals" || ctx.route === "home";
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
    // A control that replaced the one just used takes over the focus. What is to take it may
    // belong to a list that is still on its way: it stays wanted until it is on the page.
    const next = document.getElementById(ctx.ui.focusId);
    if (next) {
      ctx.ui.focusId = null;
      if (!document.querySelector("dialog[open]")) next.focus();
    }
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
    // The last card was decided. On Home the title of the page takes the focus. Never a button.
    const headline = document.getElementById("home-title");
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
  // The two lists of Home are asked for by kind. Neither is made by picking from the newest entries.
  left: () => api.activity({ limit: LEFT_FIRST, kind: "release" }),
  stops: loadStops,
  // The list of the page Activity that is shown, by its kind: what was given to apps, or what was stopped.
  rows: loadRows,
};

async function loadRows() {
  const tab = ctx.rows.tab;
  return { tab, page: await api.activity({ limit: ROWS_PAGE, kind: tab }) };
}

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
  // The answer belongs to the list it was asked for, whichever of the two is shown by now.
  rows: (value) => mergeHead(ctx.rows[value.tab], value.page),
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
  // The first steps end by themselves once the vault holds a document, a passage was seen
  // or an app has asked, and an app is registered.
  if (ctx.state && !ctx.state.locked) settleFirst(ctx);
  schedule();
}

function loadRoute() {
  if (!ctx.state || ctx.state.locked || !ctx.state.store.exists) return Promise.resolve();
  return load(needs());
}

/** Drop everything that came out of the vault, for when it locks or the session ends. */
function forget() {
  ctx.data = { apps: null, approvals: null, documents: null, settings: null };
  ctx.activity = freshActivity();
  ctx.left = freshLeft();
  ctx.stops = null;
  ctx.rows = freshRows(ctx.rows.tab);
  ctx.passages = new Map();
  ctx.panel = null;
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
    toast(t("nav.result.locked"), "plain");
  }
  // The language the app keeps beside the vault is the one shown, unless the owner pressed one on this page.
  if (next.interface) keptLanguage(next.interface.language);
  // Which page an address opens that names none is decided by the view that is kept, once the app has said it.
  if (next.interface && !(previous && previous.interface)) settleRoute();
  // Whenever Home opens, the focus goes to its title, never to a button of a card.
  if (isOpen && !wasOpen && ctx.route === "home") ctx.ui.focusId = "home-title";
  const retry = Object.keys(ctx.loadErrors).length > 0;
  if (isOpen && (force || retry || !wasOpen || previous.cursor !== next.cursor)) lastLoad = loadRoute();
  schedule();
  // A language that was pressed before the app could keep it (on the pairing screen) is sent now.
  keepLanguage();
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

/** The page Activity shows the other of its two lists. The address says which, so that a reload shows the same one. */
function showRows(tab) {
  if ((tab !== "release" && tab !== "refusal") || ctx.rows.tab === tab) return;
  ctx.rows.tab = tab;
  // The details that are open belong to a row of the list that goes.
  ctx.panel = null;
  if (ctx.route === "activity") window.history.replaceState(null, "", tab === "refusal" ? "#/activity?show=stopped" : "#/activity");
  schedule();
  load(["rows"]);
}

/** "Show more" on the page Activity: the next rows of the list that is shown, older than the oldest one. */
async function moreRows() {
  const tab = ctx.rows.tab;
  const rows = ctx.rows[tab];
  if (rows.loadingMore || rows.nextBefore === null || !rows.order.length) return;
  rows.loadingMore = true;
  schedule();
  try {
    const page = await api.activity({ limit: ROWS_PAGE, kind: tab, before: rows.order[rows.order.length - 1] });
    if (ctx.rows[tab] === rows) {
      for (const item of page.items || []) rows.items.set(item.seq, item);
      rows.nextBefore = page.next_before === undefined ? null : page.next_before;
      sortList(rows);
    }
  } catch (error) {
    toastError(error);
  }
  rows.loadingMore = false;
  schedule();
}

let panelOpener = null;

function openPanel(seq, viaKeyboard = false) {
  if (ctx.panel && ctx.panel.seq === seq) {
    closePanel();
    return;
  }
  const active = document.activeElement;
  const opens = ["act-row", "stop-row", "row-open"];
  if (active && active.classList && opens.some((name) => active.classList.contains(name))) panelOpener = active;
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

/** The page that opens when the address names none: Home where that is what the app keeps, else Overview of the six pages. */
function defaultRoute() {
  const kept = ctx.state ? ctx.state.interface : null;
  return kept && kept.view === "basic" ? "home" : "pro/overview";
}

/** The addresses from before that lead to Home: the page that was called Basic, and the steps of the first run. */
const HOME_BEFORE = ["basic", "setup"];

/**
 * The route the address names. "#/home", "#/documents", "#/activity", "#/settings"
 * and "#/how" are the simple pages, and "#/pro/…" the six pages with every setting,
 * whatever the app keeps. An address from before leads where its page is now:
 * "#/basic", "#/setup" and "#/setup/…" are Home, and "#/overview", "#/approvals",
 * "#/apps" and "#/vault" are pages under "#/pro/". Only an address that names no
 * page is decided by what the app keeps.
 */
function routeFromHash() {
  const { name, step } = hashParts();
  if (name === "home" || Object.prototype.hasOwnProperty.call(PAGES, name)) return name;
  if (name === "pro") return Object.prototype.hasOwnProperty.call(VIEWS, step) ? `pro/${step}` : "pro/overview";
  if (HOME_BEFORE.includes(name)) return "home";
  if (OLD_ADDRESSES.includes(name)) return `pro/${name}`;
  return defaultRoute();
}

/**
 * Where the address named a page by an address from before, the address becomes the
 * one the page has now: for one of the six pages with what followed its "?", for
 * Home without. An address that names no page is left as it is. Nothing is loaded
 * for it and no entry is added to the history.
 */
function settleAddress() {
  const { name } = hashParts();
  if (HOME_BEFORE.includes(name)) {
    window.history.replaceState(null, "", "#/home");
    return;
  }
  if (!OLD_ADDRESSES.includes(name)) return;
  const query = window.location.hash.includes("?") ? window.location.hash.slice(window.location.hash.indexOf("?")) : "";
  window.history.replaceState(null, "", `#/${ctx.route}${query}`);
}

/** Which of its two lists the page Activity shows, as the address says: "#/activity?show=stopped" is the second. */
function settleRows() {
  if (ctx.route !== "activity") return;
  const tab = hashParts().query.get("show") === "stopped" ? "refusal" : "release";
  if (tab !== ctx.rows.tab) ctx.rows.tab = tab;
}

/** The first state with the view is in: an address that names no page now has one. */
function settleRoute() {
  const before = ctx.route;
  const name = routeFromHash();
  ctx.route = name;
  settleRows();
  if (name === before) return;
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
  const from = ctx.route;
  const shown = ctx.rows.tab;
  const name = routeFromHash();
  ctx.route = name;
  settleAddress();
  settleRows();
  if (name === from) {
    if (name === "pro/approvals" && hashParts().query.get("ref")) {
      ctx.ui.focusApproval = approvalFromHash();
      schedule();
    }
    if (name === "activity" && ctx.rows.tab !== shown) {
      // The address names the other list of the page Activity.
      ctx.panel = null;
      schedule();
      lastLoad = loadRoute();
    }
    return;
  }
  // The details of an entry stay open between Overview and Activity of the six pages, and nowhere else.
  const keeps = (route) => route === "pro/overview" || route === "pro/activity";
  if (!keeps(name) || !keeps(from)) ctx.panel = null;
  ctx.ui.focusApproval = null;
  // What was to take the focus on the screen that is left is not looked for on the next one.
  ctx.ui.focusId = null;
  if (name === "pro/approvals") {
    // The cards appear anew, so "Release once" waits again before it can be pressed.
    ctx.ui.focusApproval = approvalFromHash();
    ctx.ui.approvalSignature = null;
  }
  if (name === "home") {
    // Here too the cards appear anew. The focus goes to the title, never to a button of a card.
    ctx.ui.approvalSignature = null;
    ctx.ui.allApprovals = false;
    ctx.ui.focusId = "home-title";
  }
  // The answer of the box that asks is gone with the page it stood on.
  if (from === "home") leaveHome(ctx);
  ctx.ui.limits = null;
  window.scrollTo(0, 0);
  schedule();
  lastLoad = loadRoute();
}

/**
 * Open a page by its address without the "#/": "home", "pro/overview". The route
 * changes at once, not when the browser has told of the new address.
 */
function navigate(address) {
  const [path] = address.split("?");
  const shown = hashParts();
  const there = address.includes("/") || address.includes("?") ? window.location.hash === `#/${address}` : shown.name === path;
  if (!there) window.location.hash = `#/${address}`;
  onRoute();
}

/**
 * Keep the page the app opens on, as the owner chose it under "Open on": "basic" is
 * Home, "pro" is Overview of the six pages. Resolves to whether it was kept. Nothing
 * is said when it was: the screen is the answer.
 */
async function keepView(view) {
  try {
    applyState(await api.setInterface({ view }));
    return true;
  } catch (error) {
    toastError(error);
    return false;
  }
}

// -- the language ---------------------------------------------------------------------

/** The language this page last asked the app to keep, so that one press is one request. */
let languageSent = null;

/**
 * Have the app keep the language the owner pressed, beside the vault. That needs a
 * vault and a paired page: before there is one, the press stays in this page's memory
 * and goes along with the passphrase of the new vault. Nothing is said when it
 * worked: the page is the answer.
 */
async function keepLanguage() {
  const wanted = chosenLanguage();
  const state = ctx.state;
  if (wanted === null || wanted === languageSent || ctx.phase !== "ready" || !state || !state.store.exists) return;
  if (state.interface && state.interface.language === wanted) return;
  languageSent = wanted;
  try {
    applyState(await api.setInterface({ language: wanted }));
  } catch (error) {
    toastError(error);
  }
}

/** The owner pressed "English" or "Polski": the page changes at once, then the app is asked to keep the word. */
function pressLanguage(code) {
  chooseLanguage(code);
  languageSent = null;
  schedule();
  keepLanguage();
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
  // The app tells the kept language before pairing, so that the pairing screen is in it.
  keptLanguage(session.language);
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

// The sheet that asks for the passphrase is told which refusal opened it, and hands that on.
configure({ unauthorized: sessionEnded, reauth: (scope) => requestReauth(ctx.state ? ctx.state.demo_passphrase : null, scope) });

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

// Until the app has said on which page it opens, an address that names no page counts as Overview.
ctx.route = routeFromHash();
settleAddress();
settleRows();
ctx.ui.focusApproval = ctx.route === "pro/approvals" ? approvalFromHash() : null;
start();
