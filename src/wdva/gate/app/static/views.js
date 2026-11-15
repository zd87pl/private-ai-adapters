// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The screens the owner reads: Overview, Activity with its detail panel, and
// Approvals. The approval card, what was released and not read yet, and the simple
// parts of the detail panel are the same on every page and take their texts from the
// text table; what only the six pages show keeps its own English texts. On a page that is not one of the six, the
// panel says everything in the language of the page, and the record's own figures
// stand behind one button.

import { api } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { ago, at, clock, count, list, num, sentence, when, whenFull } from "./format.js";
import { loadingBlock, servingStrip } from "./manage.js";
import { rowSentence, withoutDocumentText } from "./pages.js";
import {
  UNANSWERED,
  activityRow,
  actor,
  button,
  card,
  chip,
  deed,
  destinationChip,
  emptyState,
  iconButton,
  meter,
  note,
  pathText,
  segmented,
  statusChip,
  toast,
  toastError,
} from "./parts.js";
import { t } from "./texts.js";

// -- Overview -----------------------------------------------------------------------

function servingSentence(ctx) {
  const serving = ctx.state.serving || { hosted: [], local: [], paused: [] };
  const served = serving.hosted.concat(serving.local);
  const registered = ctx.data.apps ? ctx.data.apps.apps.map((app) => app.name) : served;
  const idle = registered.filter((name) => !served.includes(name));
  if (!registered.length) return "No app has been added, so nothing can leave.";
  const first = served.length ? `Answering ${list(served)}.` : "No app is answered right now.";
  if (!idle.length) return first;
  return `${first} Not answered: ${list(idle)}.`;
}

function stat(value, label) {
  return el("div", { class: "stat" }, el("span", { class: "stat-value" }, value), el("span", { class: "stat-label" }, label));
}

function stateCard(ctx) {
  const state = ctx.state;
  const today = state.today || { releases: 0, characters: 0, refusals: 0, approvals_waiting: 0 };
  return el(
    "section",
    { class: "card hero", key: "hero", "aria-label": "State of the vault" },
    el("div", { class: "hero-glyph" }, icon("unlock")),
    el(
      "div",
      { class: "hero-text" },
      el(
        "h2",
        { class: "hero-title" },
        "Unlocked",
        state.locks_at ? el("span", { class: "hero-until" }, ` until ${at(state.locks_at, ctx.now).replace(/^(at|on) /, "")}`) : null,
      ),
      el("p", { class: "hero-line" }, servingSentence(ctx)),
    ),
    el(
      "div",
      { class: "stats-block", role: "group", "aria-label": "Last 24 hours" },
      el("span", { class: "stats-caption" }, "Last 24 hours"),
      el(
        "div",
        { class: "stats" },
        stat(num(today.characters), today.characters === 1 ? "character released" : "characters released"),
        stat(num(today.releases), today.releases === 1 ? "release" : "releases"),
        stat(num(today.refusals), today.refusals === 1 ? "call refused" : "calls refused"),
      ),
      // A question about what an app may use releases no text, so it is not among the releases.
      today.questions > 0
        ? el(
            "span",
            { class: "stats-aside", title: "An app asked what it may use and was told. No document text was released." },
            today.questions === 1 ? "and 1 question without document text" : `and ${num(today.questions)} questions without document text`,
          )
        : null,
    ),
  );
}

/**
 * What a new vault still needs before an app can ask it anything, in the order to
 * do it. Shown until an app has made its first call; never in the demo, which
 * starts with all of it done.
 */
function startCard(ctx) {
  const data = ctx.data.apps;
  if (!data || ctx.state.demo) return null;
  if (ctx.state.interface && ctx.state.interface.stored && ctx.state.interface.setup === "done") return null;
  const apps = data.apps;
  const documents = ctx.state.encryption ? ctx.state.encryption.documents : 0;
  const steps = [
    {
      done: documents > 0,
      title: "Add documents",
      text: "Drop text files or PDFs into a collection. A sealed copy of each goes into the vault.",
      href: "#/pro/documents",
      action: "Add documents",
    },
    {
      done: apps.length > 0,
      title: "Add an app",
      text: "Say which assistant may ask, for example Claude Desktop, and where the text it receives goes.",
      href: "#/pro/apps",
      action: "Add an app",
    },
    {
      done: apps.some((app) => Object.keys(app.grants || {}).length > 0),
      title: "Give the app a collection",
      text: "Under Apps, choose “Can read” or “Ask me each time” for a collection. Until you do, the app is told there is nothing for it.",
      href: "#/pro/apps",
      action: "Open Apps",
    },
    {
      done: apps.some((app) => app.last_seen),
      title: "Connect the app",
      text: "Under Apps, press Connect, copy the configuration into the app’s own settings and restart the app. Its first call then shows up under Activity.",
      href: "#/pro/apps",
      action: "Open Apps",
    },
  ];
  const next = steps.findIndex((step) => !step.done);
  if (next < 0) return null;
  return card(
    "Getting started",
    el(
      "ol",
      { class: "start-list" },
      steps.map((step, index) =>
        el(
          "li",
          { class: ["start-step", step.done && "is-done", index === next && "is-next"], key: `start-${index}`, "aria-current": index === next ? "step" : null },
          el("span", { class: "start-dot" }, step.done ? icon("check") : String(index + 1)),
          el(
            "div",
            { class: "start-text" },
            el("p", { class: "start-title" }, step.title, step.done ? el("span", { class: "sr-only" }, " (done)") : null),
            step.done ? null : el("p", { class: "start-hint" }, step.text),
          ),
          index === next ? el("a", { class: "btn btn-primary btn-small", href: step.href }, el("span", { class: "btn-label" }, step.action)) : null,
        ),
      ),
    ),
    { key: "start", class: "card-start", action: el("span", { class: "card-aside" }, `Step ${num(next + 1)} of ${num(steps.length)}`) },
  );
}

/** Notices the page words itself, because they need a consequence said with them. */
const NOTICE_TEXT = {
  previous_session_unclean:
    `${PRODUCT} ended without locking last time (a crash or a forced quit). The files stayed sealed; calls per minute are counted from zero again.`,
};

/** Notices that ask nothing of the owner: they can be put away. */
const ASKS_NOTHING = new Set(["previous_session_unclean"]);

/** What the owner has put away, on Home or on Overview. It stays away until the page is loaded again. */
export const putAway = new Set();

/**
 * The notices of the state. Where a strip says that both kinds of apps are answered
 * (Overview, Apps), that notice is left to the strip; a page without the strip passes
 * `bothKinds` and shows it in the app's own words.
 */
export function warningList(ctx, { bothKinds = false } = {}) {
  const warnings = (ctx.state.warnings || []).filter((warning) => (bothKinds || warning.code !== "both_kinds_served") && !putAway.has(warning.code));
  if (!warnings.length) return null;
  const pressing = warnings.some((warning) => !ASKS_NOTHING.has(warning.code));
  return el(
    "ul",
    { class: ["warnings", pressing && "warnings-amber"], key: "warnings", "aria-label": "Things to know" },
    warnings.map((warning) => {
      const open = ctx.ui.openWarnings.has(warning.code);
      const detail = warning.detail || [];
      const quiet = ASKS_NOTHING.has(warning.code);
      return el(
        "li",
        { class: ["warning", quiet && "warning-quiet"], key: `w-${warning.code}` },
        icon(quiet ? "info" : "warning"),
        el(
          "div",
          { class: "note-text" },
          el("span", null, NOTICE_TEXT[warning.code] || warning.text, " "),
          detail.length
            ? button(open ? "Hide" : detail.length === 1 ? "Show it" : `Show all ${num(detail.length)}`, {
                kind: "quiet",
                small: true,
                onclick: () => {
                  if (open) ctx.ui.openWarnings.delete(warning.code);
                  else ctx.ui.openWarnings.add(warning.code);
                  ctx.render();
                },
              })
            : null,
          open && detail.length ? el("ul", { class: "path-list" }, detail.map((line) => el("li", null, pathText(line)))) : null,
        ),
        quiet
          ? button("Got it", {
              small: true,
              onclick: () => {
                putAway.add(warning.code);
                ctx.render();
              },
            })
          : null,
      );
    }),
  );
}

function sentCard(ctx) {
  const data = ctx.data.apps;
  let content;
  if (!data) content = loadingBlock();
  else if (!data.apps.length) {
    content = emptyState({
      text: "No app has been added yet, so nothing can leave the vault.",
      action: el("a", { class: "btn btn-default btn-small", href: "#/pro/apps" }, el("span", { class: "btn-label" }, "Add an app")),
    });
  } else {
    content = el(
      "ul",
      { class: "left-list" },
      data.apps.map((app) =>
        el(
          "li",
          { class: ["left-item", !app.served && "is-idle"], key: app.name },
          el(
            "div",
            { class: "left-head" },
            el("span", { class: "left-app", title: app.name }, app.name),
            destinationChip(app, true),
            app.paused ? chip("Paused", "amber") : !app.served ? chip("Not answered", "quiet") : null,
            el(
              "span",
              { class: "left-value" },
              typeof app.usage.releases_day === "number" ? `${count(app.usage.releases_day, "release", "releases")} · ` : null,
              el("strong", null, num(app.usage.chars_day)),
              ` of ${num(app.limits.chars_per_day)}`,
            ),
          ),
          meter(app.usage.chars_day, app.limits.chars_per_day, `${app.name}: ${num(app.usage.chars_day)} of ${num(app.limits.chars_per_day)} characters in the last 24 hours`),
        ),
      ),
    );
  }
  return card("Sent to apps, last 24 hours", content, {
    key: "left",
    action: data && data.apps.length ? el("span", { class: "card-aside" }, "characters, of each app’s limit") : null,
  });
}

/** The address of one waiting request on the Approvals screen. */
function approvalLink(item) {
  return `#/pro/approvals?app=${encodeURIComponent(item.app)}&ref=${encodeURIComponent(item.ref)}`;
}

function waitingCard(ctx) {
  const data = ctx.data.approvals;
  const waiting = data ? data.waiting : [];
  const total = ctx.state.today ? ctx.state.today.approvals_waiting : waiting.length;
  if (!data) return card("Waiting for you", loadingBlock(), { key: "waiting" });
  if (!waiting.length) {
    return card("Waiting for you", el("p", { class: "quiet-line" }, icon("check"), "Nothing needs your approval."), { key: "waiting" });
  }
  return card(
    el("h2", { class: "card-title" }, "Waiting for you", el("span", { class: "count-badge" }, num(Math.max(total, waiting.length)))),
    [
      el(
        "ul",
        { class: "waiting-list" },
        waiting.slice(0, 2).map((item) =>
          el(
            "li",
            { class: "waiting-item", key: `${item.app}-${item.ref}` },
            el(
              "div",
              { class: "waiting-text" },
              el("p", { class: "waiting-line" }, el("strong", null, item.app), ` asks for passage ${num(item.passage)} of ${num(item.passages)} of “${item.title}”`),
              el("p", { class: "waiting-meta" }, destinationChip(item, true), el("span", null, `${num(item.characters)} characters · asked ${ago(item.last_asked, ctx.now)}`)),
            ),
            el(
              "a",
              { class: "btn btn-default btn-small", href: approvalLink(item), "aria-label": `Review the request of ${item.app} for passage ${num(item.passage)} of ${item.title}` },
              el("span", { class: "btn-label" }, "Review"),
            ),
          ),
        ),
      ),
      waiting.length > 2 ? el("a", { class: "link more-link", href: "#/pro/approvals" }, `and ${num(waiting.length - 2)} more`) : null,
    ],
    { key: "waiting", class: "card-amber", label: "Waiting for you" },
  );
}

async function verifyNow(ctx) {
  if (ctx.ui.verifying) return;
  ctx.ui.verifying = true;
  ctx.ui.verifyFailure = "";
  ctx.render();
  try {
    const answer = await api.verify();
    if (answer.verified) {
      toast(`The record verifies: ${count(answer.records, "entry", "entries")} checked in ${answer.seconds} s.`, "green");
    } else {
      ctx.ui.verifyFailure = answer.message || `The record does not verify from entry ${num(answer.first_bad)}.`;
      toast(ctx.ui.verifyFailure, "red");
    }
  } catch (error) {
    toastError(error);
  }
  ctx.ui.verifying = false;
  await ctx.refresh();
}

function encryptionCard(ctx) {
  const state = ctx.state;
  const facts = state.encryption || {};
  const record = state.record || {};
  const failed = ctx.ui.verifyFailure;
  return card(
    "Encryption",
    [
      el(
        "ul",
        { class: "check-list" },
        el("li", null, icon("check"), el("span", null, "Files are sealed with ", el("strong", { class: "nowrap" }, facts.cipher || "–"))),
        el("li", null, icon("check"), el("span", null, "The key comes from your passphrase, with ", el("strong", null, facts.kdf || "–"))),
        el(
          "li",
          null,
          icon("check"),
          el(
            "span",
            null,
            `${count(facts.documents, "document", "documents")} in ${count(facts.collections, "collection", "collections")}, ${num(facts.characters)} characters, in ${count(state.store.sealed_files, "sealed file", "sealed files")}`,
          ),
        ),
      ),
      el(
        "div",
        { class: ["record", failed && "record-bad"] },
        el(
          "div",
          { class: "record-text" },
          el("span", { class: "record-label" }, "Record of what was released"),
          el("span", { class: "record-head mono", title: "The newest entry: its number and the start of its hash" }, `#${record.head_seq} ${record.head_hash}`),
          el(
            "span",
            { class: ["record-when", !failed && "nowrap"] },
            failed ? failed : record.verified ? `Checked ${when(record.verified_at, ctx.now)}` : "Not checked in this session",
          ),
        ),
        button(ctx.ui.verifying ? "Verifying…" : "Verify now", { small: true, glyph: "refresh", busy: ctx.ui.verifying, onclick: () => verifyNow(ctx) }),
      ),
      record.previous_session === "unclean" && putAway.has("previous_session_unclean")
        ? el("p", { class: "muted small card-foot-line" }, `Last time ${PRODUCT} ended without locking.`)
        : null,
    ],
    { key: "encryption" },
  );
}

function latestCard(ctx) {
  const items = ctx.activity.latest;
  let content;
  if (!items) content = loadingBlock();
  else if (!items.length) {
    content = emptyState({ text: "Nothing has happened yet. Calls from apps appear here as they come in." });
  } else {
    content = el(
      "ul",
      { class: "act-list act-compact" },
      items.slice(0, 8).map((item) =>
        activityRow(item, {
          onopen: (row, keyboard) => ctx.openPanel(row.seq, keyboard),
          selected: ctx.panel && ctx.panel.seq === item.seq,
          now: ctx.now,
          passages: ctx.passages,
        }),
      ),
    );
  }
  return card("Latest activity", content, {
    key: "latest",
    class: "card-flush",
    action: el("a", { class: "link", href: "#/pro/activity" }, "All activity"),
  });
}

export function overviewView(ctx) {
  const apps = ctx.data.apps ? ctx.data.apps.apps : [];
  return {
    title: "Overview",
    body: [
      stateCard(ctx),
      ctx.state.serving && ctx.state.serving.choice_needed ? servingStrip(ctx, apps) : null,
      warningList(ctx),
      startCard(ctx),
      el(
        "div",
        { class: "overview-grid", key: "grid" },
        el("div", { class: "overview-main" }, sentCard(ctx), latestCard(ctx)),
        el("div", { class: "overview-side" }, waitingCard(ctx), encryptionCard(ctx)),
      ),
    ],
  };
}

// -- Activity -----------------------------------------------------------------------

const KINDS = [
  { value: "", label: "All" },
  { value: "release", label: "Released" },
  { value: "question", label: "Questions" },
  { value: "refusal", label: "Refused" },
  { value: "approval", label: "Approvals" },
  { value: "change", label: "Changes" },
  { value: "session", label: "Vault" },
];

export function activityView(ctx) {
  const activity = ctx.activity;
  const names = ctx.data.apps ? ctx.data.apps.apps.map((app) => app.name) : [];
  if (activity.filters.app && !names.includes(activity.filters.app) && activity.filters.app !== UNANSWERED) {
    names.push(activity.filters.app);
  }
  const filtered = Boolean(activity.filters.app || activity.filters.kind);
  const filters = el(
    "div",
    { class: "filters", key: "filters", role: "group", "aria-label": "Filter the activity" },
    el(
      "label",
      { class: "filter" },
      el("span", { class: "filter-label" }, "App"),
      el(
        "select",
        {
          class: "select",
          id: "filter-app",
          value: activity.filters.app,
          onchange: (event) => ctx.setActivityFilters({ app: event.target.value }),
        },
        el("option", { value: "" }, "All apps"),
        names.map((name) => el("option", { value: name }, name)),
        el("option", { value: UNANSWERED }, "Names not in use"),
      ),
    ),
    segmented({
      name: "filter-kind",
      legend: "Kind of entry",
      value: activity.filters.kind,
      options: KINDS,
      onchange: (kind) => ctx.setActivityFilters({ kind }),
    }),
  );
  let content;
  if (!activity.loaded) content = loadingBlock();
  else if (activity.error && !activity.order.length) content = note("red", activity.error);
  else if (!activity.order.length) {
    content = el(
      "section",
      { class: "card card-empty", key: "none" },
      filtered
        ? emptyState({
            glyph: "activity",
            text: "No entry matches these filters.",
            action: button("Show everything", { small: true, onclick: () => ctx.setActivityFilters({ app: "", kind: "" }) }),
          })
        : emptyState({
            glyph: "activity",
            title: "Nothing has happened yet",
            text: "Every search and every passage an app asks for appears here, with what was released and what was refused. Your own changes are listed too.",
            action: el("a", { class: "btn btn-default btn-small", href: "#/pro/apps" }, el("span", { class: "btn-label" }, "Open Apps")),
          }),
    );
  } else {
    content = [
      el(
        "section",
        { class: "card card-flush", key: "list", "aria-label": "Activity, newest first" },
        el(
          "ul",
          { class: "act-list" },
          activity.order.map((seq) => {
            const item = activity.items.get(seq);
            return activityRow(item, {
              onopen: (row, keyboard) => ctx.openPanel(row.seq, keyboard),
              selected: ctx.panel && ctx.panel.seq === seq,
              now: ctx.now,
              passages: ctx.passages,
            });
          }),
        ),
      ),
      el(
        "div",
        { class: "list-foot", key: "foot" },
        activity.nextBefore !== null
          ? button(activity.loadingMore ? "Loading…" : "Load 50 more", { onclick: () => ctx.loadMoreActivity(), busy: activity.loadingMore })
          : el("span", { class: "muted" }, `That is everything: ${count(activity.order.length, "entry", "entries")}.`),
      ),
    ];
  }
  return { title: "Activity", lead: "Newest first. Each row is one entry of the sealed record.", body: [filters, content] };
}

const REFUSAL_TEXT = {
  NEEDS_APPROVAL: "Nothing was released. The passage waits for your decision under Approvals.",
  NOT_ALLOWED: "Nothing was released. The name the call was made under is not registered, or it is paused or not answered in this session.",
  NOT_FOUND: "Nothing was released. The app asked for something that does not exist for it.",
  BAD_REQUEST: "Nothing was released. The request could not be used.",
  LOCKED: "Nothing was released. The vault was locked.",
  UNAVAILABLE: "Nothing was released. The vault could not answer at that moment.",
};

const VARIANT_TEXT = {
  "client-day": "Nothing was released. This app had used up its limit for 24 hours.",
  "client-30-days": "Nothing was released. This app had used up its limit for 30 days.",
  "pool-day": "Nothing was released. Apps of this kind together had used up their limit for 24 hours.",
  "pool-30-days": "Nothing was released. Apps of this kind together had used up their limit for 30 days.",
  searches: "Nothing was released. This app had made too many searches in 24 hours.",
  calls: "Nothing was released. This app was calling too fast.",
};

/** What allowed a release, in words: one entry for every rule the app can record. */
const RULE_TEXT = {
  "grant:search": "The access you gave this app",
  "grant:read": "Can read",
  approval: "Your approval, used once",
};

/** What an entry of the kind "question" means: a call that asks what the app may use. */
const QUESTION_TEXT =
  "No document text was released. The app asked what it may use and was told its collections and how much it may still receive. " +
  "Such a question is not a release and is listed at most once an hour for each app.";

function detailRow(label, value, mono = false) {
  if (value === null || value === undefined || value === "") return null;
  return el("div", { class: "fact", key: label }, el("dt", null, label), el("dd", { class: mono ? "mono" : null }, value));
}

/** What the entry itself says about the passage it names, added to what the page knows. */
function passagesOf(ctx, item) {
  if (!item.ref) return ctx.passages;
  const known = Object.assign({}, ctx.passages.get(item.ref));
  for (const [label, value] of item.details || []) {
    if (label === "document") known.title = String(value);
    const parts = label === "passage" ? /^(\d+)(?: of (\d+))?$/.exec(String(value)) : null;
    if (parts) {
      known.passage = Number(parts[1]);
      if (parts[2]) known.passages = Number(parts[2]);
    }
  }
  const merged = new Map(ctx.passages);
  merged.set(item.ref, known);
  return merged;
}

function connectionText(item) {
  if (!item.protocol) return null;
  const protocol = el("span", { class: "nowrap" }, `protocol ${item.protocol}`);
  if (item.transport === "stdio") return ["Started by the app itself (", protocol, ")"];
  return [`${item.transport || "Unknown"}, `, protocol];
}

/** The two blocks that say where an entry stands in the record: what it holds, and its place in the chain. */
function recordBlocks(item) {
  return [
    el(
      "section",
      { class: "panel-section" },
      el("h3", { class: "mini-title" }, "Entry"),
      el(
        "dl",
        { class: "facts facts-tight" },
        (item.details || []).map(([label, value]) => detailRow(sentence(String(label)), String(value))),
        detailRow("Collections", item.collections && item.collections.length ? list(item.collections) : null),
        detailRow("Reference", item.ref, true),
        detailRow("Allowed by", item.rule ? RULE_TEXT[item.rule] || item.rule : null),
        detailRow("Number for this app", item.client_seq ? `Release #${item.client_seq}` : null),
        detailRow("Connection", connectionText(item)),
      ),
    ),
    el(
      "section",
      { class: "panel-section" },
      el("h3", { class: "mini-title", title: "Each entry holds the hash of the one before it" }, "Chain"),
      el(
        "dl",
        { class: "facts facts-tight" },
        detailRow("Entry hash", item.record_hash, true),
        detailRow("Hash of the entry before", item.previous_hash, true),
      ),
    ),
  ];
}

/** Whether the page that is shown is one of the six pages with every setting. */
function onProPage(ctx) {
  return String(ctx.route).startsWith("pro/");
}

/** The button that unfolds the record's own figures of an entry, and those figures while it is unfolded. */
function recordFold(ctx, panel, item) {
  const open = ctx.ui.panelMore === true;
  return [
    el(
      "div",
      { class: "panel-more", key: "more" },
      button(t("panel.button.record"), {
        kind: "quiet",
        small: true,
        id: "panel-more",
        expanded: open ? "true" : "false",
        onclick: () => {
          ctx.ui.panelMore = !open;
          ctx.render();
        },
      }),
    ),
    open ? el("p", { class: "panel-kicker", key: "number" }, t("panel.label.entry", { seq: String(panel.seq) })) : null,
    open ? recordBlocks(item) : null,
  ];
}

/**
 * What the app searched for, and the exact text that left with where it is now: the
 * same on every page. A search that quoted nothing says that in the place of the two
 * sentences: they would be read as "this much of my documents left". It is true also
 * of an answer that names a document, where the group shows titles to apps.
 */
function leftBlocks(item, figure) {
  const hosted = item.hosted === true;
  const none = withoutDocumentText(item);
  return [
    item.query
      ? el("section", { class: "panel-section" }, el("h3", { class: "mini-title" }, t("panel.label.query")), el("pre", { class: "code code-wrap", tabindex: 0 }, el("code", null, item.query)))
      : null,
    item.text
      ? el(
          "section",
          { class: "panel-section" },
          el("h3", { class: "mini-title" }, el("span", null, t("panel.label.text")), el("span", { class: "mini-aside" }, figure)),
          el("pre", { class: "code code-wrap released", tabindex: 0, "aria-label": t("panel.label.text") }, el("code", null, item.text)),
          // Where the text is now, and that it cannot be taken back: two sentences, each an element of its own.
          none ? el("p", { class: "gone", id: "panel-notext" }, icon("info"), el("span", null, t("panel.say.1.notext"))) : null,
          hosted && !none ? el("p", { class: "gone" }, icon("out"), el("span", null, el("span", null, t("panel.say.1", { destination: item.destination })), " ", el("span", null, t("panel.say.2")))) : null,
          item.hosted === false && !none ? el("p", { class: "gone" }, icon("mac"), el("span", null, t("panel.say.1.local"))) : null,
        )
      : null,
  ];
}

/**
 * The details of an entry on a page that is not one of the six: the row's sentence as
 * the title, where the text went, the exact text and its two sentences, what was
 * searched for. Everything is said in the language of the page. The record's own
 * figures, which keep the app's English labels, stand behind "Record details".
 */
function simplePanelBody(ctx, panel, item) {
  const refusal = item.kind === "refusal";
  return [
    el(
      "div",
      { class: "panel-top" },
      item.approved ? chip(t("row.chip.approved"), "quiet", "check") : el("span", null),
      el("time", { class: "panel-time", datetime: item.time }, whenFull(item.time)),
    ),
    el("h2", { class: "panel-title", id: "panel-title" }, rowSentence(item, passagesOf(ctx, item))),
    // Where the text went is a name, and the sentences under the text say the rest.
    item.app && item.app !== UNANSWERED && ((item.hosted === true && item.destination) || item.claimed)
      ? el(
          "div",
          { class: "panel-who" },
          item.hosted === true && item.destination ? chip(item.destination, "plain", "out", null, "chip-dest") : null,
          item.claimed ? el("span", { class: "muted" }, t("card.status.claimed", { claimed: item.claimed })) : null,
        )
      : null,
    refusal ? el("p", { class: "panel-nothing", key: "nothing" }, t("panel.status.nothing")) : null,
    refusal && item.count > 1 ? el("p", { class: "muted", key: "count" }, t("panel.status.count", { n: item.count })) : null,
    // A passage: how much of it is the owner's document, and how much left with the lines added around it.
    leftBlocks(item, typeof item.passage_characters === "number" ? t("panel.status.both", { n: item.passage_characters, total: item.characters }) : t("row.status.chars", { n: item.characters })),
    recordFold(ctx, panel, item),
  ];
}

function panelBody(ctx, panel) {
  const item = panel.detail;
  if (panel.error) return note("red", panel.error);
  if (!item) return loadingBlock();
  const refusal = item.kind === "refusal";
  // On every page that is not one of the six, the record's own figures stand behind one button.
  const folded = !onProPage(ctx);
  if (folded) return simplePanelBody(ctx, panel, item);
  return [
    el(
      "div",
      { class: "panel-top" },
      statusChip(item),
      el("time", { class: "panel-time", datetime: item.time }, whenFull(item.time)),
    ),
    el("h2", { class: "panel-title", id: "panel-title" }, `${actor(item)} ${deed(item, passagesOf(ctx, item))}`),
    item.app && item.app !== UNANSWERED
      ? el(
          "div",
          { class: "panel-who" },
          destinationChip(item),
          item.claimed ? el("span", { class: "muted" }, t("card.status.claimed", { claimed: item.claimed })) : null,
        )
      : null,
    refusal ? note(item.code === "NEEDS_APPROVAL" ? "amber" : "plain", (item.code === "OVER_LIMIT" && VARIANT_TEXT[item.variant]) || REFUSAL_TEXT[item.code] || "Nothing was released.") : null,
    item.kind === "question" ? note("plain", QUESTION_TEXT) : null,
    refusal && item.code === "NEEDS_APPROVAL" ? el("a", { class: "btn btn-default btn-small panel-link", href: "#/pro/approvals" }, el("span", { class: "btn-label" }, "Open Approvals")) : null,
    refusal && item.count > 1
      ? el(
          "p",
          { class: "muted" },
          `This entry stands for ${num(item.count)} calls that were answered the same way${item.last_time ? `, the last one ${ago(item.last_time, ctx.now)}` : ""}.`,
        )
      : null,
    leftBlocks(item, count(item.characters, "character", "characters")),
    recordBlocks(item),
  ];
}

/**
 * The side panel with one entry in full. It lies over the page, and the list under it
 * keeps its shape. In the simple frame, in a wide window, the stylesheet has the column
 * make room for it instead, so that no row stands under it.
 */
export function detailPanel(ctx) {
  const panel = ctx.panel;
  if (!panel) return null;
  return el(
    "aside",
    { class: "panel", key: "panel", "aria-label": t("panel.label.entry", { seq: String(panel.seq) }), tabindex: -1, id: "panel" },
    el(
      "header",
      { class: "panel-head" },
      // The number of the entry belongs to the record's own figures: only the six pages show it at the head.
      el("span", { class: "panel-kicker" }, onProPage(ctx) ? t("panel.label.entry", { seq: String(panel.seq) }) : null),
      iconButton("x", t("panel.label.close"), { onclick: () => ctx.closePanel() }),
    ),
    el("div", { class: "panel-scroll", key: `p-${panel.seq}` }, panelBody(ctx, panel)),
  );
}

// -- Approvals ----------------------------------------------------------------------

/** How long "Release once" stays inert after the cards appeared, changed or moved. */
const ARM_MS = 1500;
/** How long a decided card stays in its place as one line, so that nothing slides under the pointer. */
const DECIDED_MS = 2000;
/** How long "Don't release" ignores a press after a decision, against a key that is held down. */
const QUIET_MS = 700;

function pairKey(item) {
  return `${item.app}\n${item.ref}`;
}

function approvalKey(item) {
  return `${item.app}\n${item.ref}\n${item.body_sha256}`;
}

/** The key of the card of one request in the page. */
export function approvalCardKey(app, ref) {
  return `ap-${app}-${ref}`;
}

/** Make every "Release once" wait again. Called for each press and each change of the cards. */
function disarm(ctx) {
  ctx.ui.releaseArmedAt = performance.now() + ARM_MS;
  clearTimeout(ctx.ui.armTimer);
  ctx.ui.armTimer = setTimeout(() => ctx.render(), ARM_MS + 40);
}

function releaseArmed(ctx) {
  return performance.now() >= ctx.ui.releaseArmedAt;
}

async function decide(ctx, item, release, event) {
  // The second press of a double click is not a second decision.
  if (event && event.detail > 1) return;
  // One decision at a time, and none by a press that was aimed at a card that has just gone.
  if (ctx.ui.deciding.size) return;
  if (release ? !releaseArmed(ctx) : performance.now() < ctx.ui.dismissQuietUntil) return;
  const key = pairKey(item);
  const position = approvalEntries(ctx, ctx.data.approvals ? ctx.data.approvals.waiting : []).findIndex((entry) => entry.item && pairKey(entry.item) === key);
  ctx.ui.deciding.add(key);
  disarm(ctx);
  delete ctx.ui.approvalNotes[key];
  ctx.render();
  let changed = false;
  let outcome = null;
  try {
    if (release) {
      const answer = await api.release(item.app, item.ref, item.body_sha256);
      outcome = { release: true, until: answer.valid_until };
    } else {
      await api.dismiss(item.app, item.ref);
      outcome = { release: false, until: null };
    }
  } catch (error) {
    if (error.code === "changed") changed = true;
    else toastError(error);
  }
  if (outcome) {
    // The card stays in its place as one line for a moment: the cards below it do not jump up.
    // The same words are a message at the foot of the window.
    Object.assign(outcome, { item, position: Math.max(0, position) });
    ctx.ui.decided.set(key, outcome);
    ctx.ui.approvalDetails.delete(key);
    toast(decidedWords(ctx, outcome), release ? "green" : "plain");
    setTimeout(() => {
      ctx.ui.decided.delete(key);
      ctx.render();
    }, DECIDED_MS);
  }
  // The card stays as it is until the new list is in; then the message appears with it.
  await ctx.refresh();
  // The text changed since it was shown, so nothing was released: the card says so.
  if (changed) ctx.ui.approvalNotes[key] = true;
  ctx.ui.deciding.delete(key);
  ctx.ui.dismissQuietUntil = performance.now() + QUIET_MS;
  disarm(ctx);
  ctx.ui.focusApproval = changed ? { app: item.app, ref: item.ref } : { after: Math.max(0, position) };
  ctx.render();
}

/** The cards in the order they are shown, with the lines of what was just decided in their old places. */
function approvalEntries(ctx, waiting) {
  const entries = waiting.filter((item) => !ctx.ui.decided.has(pairKey(item))).map((item) => ({ item }));
  const decided = Array.from(ctx.ui.decided.values()).sort((a, b) => a.position - b.position);
  for (const done of decided) entries.splice(Math.min(done.position, entries.length), 0, { done });
  return entries;
}

/** What a decision brought, in one text: the line that takes the card's place, and the message. */
function decidedWords(ctx, done) {
  const app = done.item.app;
  return done.release ? t("card.result.yes", { app, time: clock(done.until, ctx.now) }) : t("card.result.no", { app });
}

function decidedLine(ctx, done) {
  const item = done.item;
  return el(
    "div",
    { class: ["decided", done.release ? "decided-yes" : "decided-no"], key: approvalCardKey(item.app, item.ref), role: "status" },
    icon(done.release ? "check" : "x"),
    el("span", null, decidedWords(ctx, done)),
  );
}

/**
 * Why a passage waits for the owner, as the key of its sentence: the app may read,
 * but this passage goes past what a document gives without asking; or the app asks
 * for every passage.
 */
function whyItWaits(item) {
  return item.mode === "read" && !item.inside_free_allowance ? "card.details.say.1.allowance" : "card.details.say.1.each";
}

/**
 * One waiting passage. Top to bottom: who asks for what; one line of facts (where the
 * text would go, what the caller says it is, when it asked); the exact text and its
 * figures; the two sentences that stay where the decision is made; the two answers,
 * the careful one first and neither filled; "Details", which unfolds three lines.
 */
function approvalCard(ctx, item, index) {
  const key = pairKey(item);
  const busy = ctx.ui.deciding.has(key);
  const ready = releaseArmed(ctx) && !ctx.ui.deciding.size;
  const changed = ctx.ui.approvalNotes[key] === true;
  const document = item.document || {};
  const today = item.today || {};
  const headingId = `approval-${index}`;
  const detailsId = `approval-details-${index}`;
  const open = ctx.ui.approvalDetails.has(key);
  const asked = ago(item.first_asked, ctx.now);
  return el(
    "article",
    { class: "card approval", key: approvalCardKey(item.app, item.ref), "aria-labelledby": headingId },
    el(
      "header",
      { class: "approval-head" },
      // The app is named by the name it has in the vault, never by a readable one.
      el("h2", { class: "approval-title", id: headingId }, t("card.title", { app: item.app, title: item.title })),
      el(
        "p",
        { class: "approval-meta" },
        chip(item.hosted ? t("card.chip.hosted", { destination: item.destination }) : t("card.chip.local"), "plain", item.hosted ? "out" : "mac", null, "chip-dest"),
        item.claimed ? el("span", { class: "approval-claimed" }, t("card.status.claimed", { claimed: item.claimed })) : null,
        el(
          "span",
          { class: "approval-asked", title: whenFull(item.first_asked) },
          item.times_asked > 1 ? t("card.status.asked.times", { n: item.times_asked, ago: asked }) : t("card.status.asked", { ago: asked }),
        ),
      ),
    ),
    changed ? note("amber", t("card.result.changed")) : null,
    el(
      "div",
      { class: "approval-text" },
      el("h3", { class: "mini-title" }, t("card.label.text")),
      el("pre", { class: "code code-wrap released", tabindex: 0, "aria-label": t("card.label.text") }, el("code", null, item.body)),
      // A document of one passage: "passage 1 of 1" would hide that this is all of it.
      el("p", { class: "approval-figures" }, t(item.passages === 1 ? "card.status.meta.whole" : "card.status.meta", { n: item.characters, passage: item.passage, passages: item.passages })),
    ),
    el(
      "div",
      { class: "approval-says" },
      el("p", { class: "approval-sentence" }, item.hosted ? t("card.say.1", { destination: item.destination }) : t("card.say.1.local")),
      el("p", { class: "approval-label" }, t("card.say.2", { app: item.app })),
      // The one third sentence of a card, and only in this case: it is never behind "Details".
      item.fits_limits === false ? el("p", { class: "approval-nofit" }, icon("warning"), el("span", null, t("card.say.3.nofit"))) : null,
    ),
    el(
      "div",
      { class: "approval-actions" },
      button(t("card.button.no"), { key: "dismiss", onclick: (event) => decide(ctx, item, false, event), waiting: busy }),
      button(t("card.button.yes"), {
        key: "release",
        onclick: (event) => decide(ctx, item, true, event),
        waiting: !ready,
        title: ready || busy ? null : t("card.label.wait"),
      }),
    ),
    el(
      "div",
      { class: "approval-more" },
      button(t("card.button.details"), {
        kind: "quiet",
        small: true,
        key: "more",
        expanded: open ? "true" : "false",
        controls: detailsId,
        onclick: () => {
          // What unfolds moves the cards below, so "Release once" waits again (see approvalCards).
          if (open) ctx.ui.approvalDetails.delete(key);
          else ctx.ui.approvalDetails.add(key);
          ctx.render();
        },
      }),
      open
        ? el(
            "ul",
            { class: "approval-details", id: detailsId, key: "more-lines" },
            el("li", null, t("card.status.document", { released: document.released, characters: document.characters, after: document.after })),
            el("li", null, t("card.status.today", { used: today.used, limit: today.limit })),
            el("li", null, t(whyItWaits(item))),
          )
        : null,
    ),
  );
}

async function withdraw(ctx, item) {
  try {
    await api.dismiss(item.app, item.ref);
    toast(t("home.granted.result.withdrawn", { app: item.app }), "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

/**
 * One release that was not read yet: which app may read which passage once, and until
 * when, and what the owner has to tell the app: it gets nothing until it asks again.
 * A document the page has not heard the title of is named by its number.
 */
function grantedLine(ctx, item) {
  const known = Object.assign({}, ctx.passages.get(item.ref), item.title ? item : null);
  const [document, number] = String(item.ref).split(".");
  const passage = typeof known.passage === "number" ? known.passage : Number(number);
  return el(
    "li",
    { key: `${item.app}-${item.ref}` },
    icon("clock"),
    el("span", { class: "granted-text" }, t("home.granted.result.line", { app: item.app, passage, title: known.title || document, time: clock(item.valid_until, ctx.now) })),
    item.withdrawable === true ? button(t("home.granted.button.withdraw"), { small: true, onclick: () => withdraw(ctx, item) }) : null,
  );
}

/**
 * The cards of the passages that wait, the lines of what was just decided, and what
 * was approved and not read yet. Every screen that shows a card gets it from here.
 *
 * `limit` is how many cards and lines are drawn at most (all of them without it);
 * `more` in the answer is how many waiting passages were left out. `around` is one
 * text made of everything near the cards that the caller can change: a bar above
 * them, a panel beside them, whether all are shown. Whenever the cards are not those
 * of the last draw, or not in their places, or `around` differs from the last draw,
 * "Release once" waits again. Null while the list of approvals is not in.
 */
export function approvalCards(ctx, { limit = null, around = "" } = {}) {
  const data = ctx.data.approvals;
  if (!data) return null;
  const entries = approvalEntries(ctx, data.waiting);
  const shown = limit === null ? entries : entries.slice(0, limit);
  // What was unfolded on a card that is gone is forgotten: a passage that waits anew starts folded.
  for (const key of Array.from(ctx.ui.approvalDetails)) {
    if (!data.waiting.some((item) => pairKey(item) === key)) ctx.ui.approvalDetails.delete(key);
  }
  // A card whose details are unfolded is not the card of the last draw: the cards below it have moved.
  const unfolded = (item) => (ctx.ui.approvalDetails.has(pairKey(item)) ? "\ndetails" : "");
  const signature = [around].concat(shown.map((entry) => (entry.item ? approvalKey(entry.item) + unfolded(entry.item) : `done ${pairKey(entry.done.item)}`))).join("\n\n");
  if (signature !== ctx.ui.approvalSignature) {
    ctx.ui.approvalSignature = signature;
    disarm(ctx);
  }
  const granted = data.granted || [];
  return {
    cards: shown.map((entry, index) => (entry.item ? approvalCard(ctx, entry.item, index) : decidedLine(ctx, entry.done))),
    more: entries.slice(shown.length).filter((entry) => entry.item).length,
    granted: granted.length
      ? card(t("home.granted.title"), el("ul", { class: "granted-list" }, granted.map((item) => grantedLine(ctx, item))), { key: "granted" })
      : null,
  };
}

export function approvalsView(ctx) {
  const data = ctx.data.approvals;
  if (!data) return { title: "Approvals", body: loadingBlock() };
  // What can come or go above the cards of this page is the bar that says what is wrong.
  const drawn = approvalCards(ctx, { around: ctx.offline ? "offline" : "" });
  const body = [];
  if (!drawn.cards.length) {
    body.push(
      el(
        "section",
        { class: "card card-empty", key: "none" },
        emptyState({
          glyph: "approvals",
          title: "Nothing is waiting",
          text: "When an app asks for a passage that needs your approval, it appears here with the exact text. That passage is not released until you say so.",
        }),
      ),
    );
  } else {
    body.push(drawn.cards);
  }
  if (drawn.granted) body.push(drawn.granted);
  return {
    title: "Approvals",
    lead: data.waiting.length ? "Each approval is for one passage, one app, one time. An approval that is not used expires." : null,
    body,
  };
}
