// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The screens the owner reads: Overview, Activity with its detail panel, and
// Approvals.

import { api } from "./api.js";
import { el, icon } from "./dom.js";
import { ago, at, count, list, num, sentence, untilShort, when, whenFull } from "./format.js";
import { loadingBlock, servingStrip } from "./manage.js";
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

// -- Overview -----------------------------------------------------------------------

function servingSentence(ctx) {
  const serving = ctx.state.serving || { hosted: [], local: [], paused: [] };
  const served = serving.hosted.concat(serving.local);
  const registered = ctx.data.apps ? ctx.data.apps.apps.map((app) => app.name) : served;
  const idle = registered.filter((name) => !served.includes(name));
  if (!registered.length) return "No app is connected, so nothing can leave.";
  const first = served.length ? `Answering ${list(served)}.` : "No app is answered right now.";
  if (!idle.length) return first;
  return `${first} Not answered: ${list(idle)}.`;
}

function stat(value, label, tone) {
  return el("div", { class: ["stat", tone && `stat-${tone}`] }, el("span", { class: "stat-value" }, value), el("span", { class: "stat-label" }, label));
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
    ),
  );
}

/** Notices the page words itself, because they need a consequence said with them. */
const NOTICE_TEXT = {
  previous_session_unclean:
    "WDVA Gate ended without locking last time (a crash or a forced quit). The files stayed sealed; calls per minute are counted from zero again.",
};

/** Notices that ask nothing of the owner: they can be put away. */
const ASKS_NOTHING = new Set(["previous_session_unclean"]);

/** What the owner has put away. It stays away until the page is loaded again. */
const putAway = new Set();

function warningList(ctx) {
  const warnings = (ctx.state.warnings || []).filter((warning) => warning.code !== "both_kinds_served" && !putAway.has(warning.code));
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
      text: "No app is connected yet, so nothing can leave the vault.",
      action: el("a", { class: "btn btn-default btn-small", href: "#/apps" }, el("span", { class: "btn-label" }, "Add an app")),
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
  return `#/approvals?app=${encodeURIComponent(item.app)}&ref=${encodeURIComponent(item.ref)}`;
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
      waiting.length > 2 ? el("a", { class: "link more-link", href: "#/approvals" }, `and ${num(waiting.length - 2)} more`) : null,
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
        ? el("p", { class: "muted small card-foot-line" }, "Last time WDVA Gate ended without locking.")
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
    action: el("a", { class: "link", href: "#/activity" }, "All activity"),
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
            text: "Every search and every passage an app asks for appears here, with what was released and what was refused.",
            action: el("a", { class: "btn btn-default btn-small", href: "#/apps" }, el("span", { class: "btn-label" }, "Connect an app")),
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

const RULE_TEXT = { "grant:read": "Can read", approval: "Your approval, used once" };

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

function panelBody(ctx, panel) {
  const item = panel.detail;
  if (panel.error) return note("red", panel.error);
  if (!item) return loadingBlock();
  const hosted = item.hosted === true;
  const refusal = item.kind === "refusal";
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
          item.claimed ? el("span", { class: "muted" }, `Says it is ${item.claimed}, unverified`) : null,
        )
      : null,
    refusal ? note(item.code === "NEEDS_APPROVAL" ? "amber" : "plain", (item.code === "OVER_LIMIT" && VARIANT_TEXT[item.variant]) || REFUSAL_TEXT[item.code] || "Nothing was released.") : null,
    refusal && item.code === "NEEDS_APPROVAL" ? el("a", { class: "btn btn-default btn-small panel-link", href: "#/approvals" }, el("span", { class: "btn-label" }, "Open Approvals")) : null,
    refusal && item.count > 1
      ? el(
          "p",
          { class: "muted" },
          `This entry stands for ${num(item.count)} calls that were answered the same way${item.last_time ? `, the last one ${ago(item.last_time, ctx.now)}` : ""}.`,
        )
      : null,
    item.query
      ? el("section", { class: "panel-section" }, el("h3", { class: "mini-title" }, "What it searched for"), el("pre", { class: "code code-wrap", tabindex: 0 }, el("code", null, item.query)))
      : null,
    item.text
      ? el(
          "section",
          { class: "panel-section" },
          el("h3", { class: "mini-title" }, "The exact text that left", el("span", { class: "mini-aside" }, `${num(item.characters)} characters`)),
          el("pre", { class: "code code-wrap released", tabindex: 0, "aria-label": "The exact text that left" }, el("code", null, item.text)),
          hosted ? el("p", { class: "gone" }, icon("out"), el("span", null, `This text is with ${item.destination} now. The gate cannot take it back.`)) : null,
          item.hosted === false ? el("p", { class: "gone" }, icon("mac"), el("span", null, "This text is with the app now. It stays on this Mac only if the app really is local.")) : null,
        )
      : null,
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

/** The side panel with one entry in full. It lies over the page; the list under it keeps its shape. */
export function detailPanel(ctx) {
  const panel = ctx.panel;
  if (!panel) return null;
  return el(
    "aside",
    { class: "panel", key: "panel", "aria-label": `Details of entry ${panel.seq}`, tabindex: -1, id: "panel" },
    el(
      "header",
      { class: "panel-head" },
      el("span", { class: "panel-kicker" }, `Entry #${panel.seq}`),
      iconButton("x", "Close the details", { onclick: () => ctx.closePanel() }),
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

function passagePhrase(item) {
  const of = item.passages ? ` of ${num(item.passages)}` : "";
  return `passage ${num(item.passage)}${of} of “${item.title}”`;
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
      toast(`Approved once for ${item.app}. It can read this passage until ${when(answer.valid_until, ctx.now)}.`, "green");
    } else {
      await api.dismiss(item.app, item.ref);
      outcome = { release: false, until: null };
      toast(`Not released. If ${item.app} asks again, the passage comes back here.`, "plain");
    }
  } catch (error) {
    if (error.code === "changed") changed = true;
    else toastError(error);
  }
  if (outcome) {
    // The card stays in its place as one line for a moment: the cards below it do not jump up.
    ctx.ui.decided.set(key, Object.assign(outcome, { item, position: Math.max(0, position) }));
    setTimeout(() => {
      ctx.ui.decided.delete(key);
      ctx.render();
    }, DECIDED_MS);
  }
  // The card stays as it is until the new list is in; then the message appears with it.
  await ctx.refresh();
  if (changed) {
    ctx.ui.approvalNotes[key] = "The text changed since it was shown, so nothing was released. Read it again before you decide.";
  }
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

function decidedLine(ctx, done) {
  const item = done.item;
  return el(
    "div",
    { class: ["decided", done.release ? "decided-yes" : "decided-no"], key: approvalCardKey(item.app, item.ref), role: "status" },
    icon(done.release ? "check" : "x"),
    el(
      "span",
      null,
      done.release
        ? [
            "Approved once for ",
            el("strong", null, item.app),
            `: ${passagePhrase(item)}, valid until ${when(done.until, ctx.now)}`,
          ]
        : ["Not released to ", el("strong", null, item.app), `: ${passagePhrase(item)}`],
    ),
  );
}

function approvalCard(ctx, item, index) {
  const busy = ctx.ui.deciding.has(pairKey(item));
  const ready = releaseArmed(ctx) && !ctx.ui.deciding.size;
  const changed = ctx.ui.approvalNotes[pairKey(item)];
  const document = item.document || {};
  const today = item.today || {};
  const headingId = `approval-${index}`;
  return el(
    "article",
    { class: "card approval", key: approvalCardKey(item.app, item.ref), "aria-labelledby": headingId },
    el(
      "header",
      { class: "approval-head" },
      el(
        "div",
        { class: "approval-who" },
        el(
          "h2",
          { class: "approval-title", id: headingId },
          el("strong", null, item.app),
          item.hosted
            ? [` asks to send ${passagePhrase(item)} (${item.collection}) to `, el("strong", null, item.destination)]
            : ` asks for ${passagePhrase(item)} (${item.collection})`,
        ),
        el(
          "p",
          { class: "approval-meta" },
          destinationChip(item),
          item.claimed ? el("span", null, `Says it is ${item.claimed}, unverified`) : null,
        ),
      ),
      el(
        "p",
        { class: "approval-asked" },
        item.times_asked > 1 ? `Asked ${num(item.times_asked)} times, first ` : "Asked ",
        el("time", { datetime: item.first_asked, title: whenFull(item.first_asked) }, ago(item.first_asked, ctx.now)),
      ),
    ),
    changed ? note("amber", changed) : null,
    el(
      "div",
      { class: "approval-text" },
      el(
        "h3",
        { class: "mini-title" },
        "The exact text that would be released",
        el("span", { class: "mini-aside" }, `${num(item.characters)} characters · ${item.ref}`),
      ),
      el("pre", { class: "code code-wrap released", tabindex: 0, "aria-label": "The exact text that would be released" }, el("code", null, item.body)),
    ),
    el(
      "ul",
      { class: "approval-counts" },
      el(
        "li",
        null,
        `This document: ${num(document.released)} of ${num(document.characters)} characters already released to ${document.to || "apps of this kind"}; ${num(document.after)} after this.`,
      ),
      el("li", null, `This app, last 24 hours: ${num(today.used)} of ${num(today.limit)} characters used.`),
      item.inside_free_allowance
        ? el("li", null, "This passage is inside the document’s free allowance. It waits for you because this app is set to “Ask me each time”.")
        : null,
      item.fits_limits === false
        ? el("li", { class: "is-amber" }, "The app’s limits would refuse this passage right now, even with your approval.")
        : null,
    ),
    el(
      "footer",
      { class: "approval-foot" },
      el(
        "p",
        { class: "approval-sentence" },
        item.hosted
          ? ["Once released, this text is sent to ", el("strong", null, item.destination), " and cannot be recalled."]
          : "Once released, this text is with the app. It stays on this Mac only if the app really is local.",
      ),
      el(
        "div",
        { class: "approval-actions" },
        button("Don’t release", { key: "dismiss", onclick: (event) => decide(ctx, item, false, event), waiting: busy }),
        button("Release once", {
          key: "release",
          onclick: (event) => decide(ctx, item, true, event),
          waiting: !ready,
          title: ready || busy ? null : "Read the text first",
        }),
      ),
    ),
  );
}

async function withdraw(ctx, item) {
  try {
    await api.dismiss(item.app, item.ref);
    toast(`Withdrawn. ${item.app} has to ask again before it can read that passage.`, "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

function grantedLine(ctx, item) {
  const known = Object.assign({}, ctx.passages.get(item.ref), item.title ? item : null);
  const left = untilShort(item.valid_until, ctx.now);
  return el(
    "li",
    { key: `${item.app}-${item.ref}` },
    icon("clock"),
    el(
      "span",
      { class: "granted-text" },
      el("strong", null, item.app),
      " may read ",
      known.title ? `passage ${num(known.passage)}${known.passages ? ` of ${num(known.passages)}` : ""} of “${known.title}”` : ["the passage ", el("span", { class: "mono" }, item.ref)],
      ` once, until ${when(item.valid_until, ctx.now)}`,
      left ? el("span", { class: "muted" }, ` (${left})`) : null,
    ),
    item.withdrawable === true ? button("Withdraw", { small: true, onclick: () => withdraw(ctx, item), label: `Withdraw the approval for ${item.app}` }) : null,
  );
}

export function approvalsView(ctx) {
  const data = ctx.data.approvals;
  if (!data) return { title: "Approvals", body: loadingBlock() };
  const entries = approvalEntries(ctx, data.waiting);
  // Whenever the cards are not the ones of the last draw, or not in their places, releasing waits again.
  const signature = entries.map((entry) => (entry.item ? approvalKey(entry.item) : `done ${pairKey(entry.done.item)}`)).join("\n\n");
  if (signature !== ctx.ui.approvalSignature) {
    ctx.ui.approvalSignature = signature;
    disarm(ctx);
  }
  const granted = data.granted || [];
  const body = [];
  if (!entries.length) {
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
    body.push(entries.map((entry, index) => (entry.item ? approvalCard(ctx, entry.item, index) : decidedLine(ctx, entry.done))));
  }
  if (granted.length) {
    body.push(card("Approved, not read yet", el("ul", { class: "granted-list" }, granted.map((item) => grantedLine(ctx, item))), { key: "granted" }));
  }
  return {
    title: "Approvals",
    lead: data.waiting.length ? "Each approval is for one passage, one app, one time. An approval that is not used expires." : null,
    body,
  };
}
