// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The Basic page: one column that says what waits for the owner, what left the
// vault, which documents it holds and which apps may ask. It states the vault as
// it is. Where one value does not describe the whole vault it has no control:
// it says what is in force in a sentence and links to the place in Pro. Every
// line about a setting is made from an answer of the app; this file holds no
// copy of the values of a new vault.

import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { ago, at, count, dayShort, num, parseTime, whenFull } from "./format.js";
import { loadingBlock } from "./manage.js";
import { UNANSWERED, activityRow, button, chip, copyText, destinationChip, meter, toast } from "./parts.js";
import { GENERAL_SENTENCE, GRANT_LABEL, basicGrantSentence, codeWords } from "./sheets.js";
import { approvalCards, warningList } from "./views.js";

/** How many waiting passages are shown as cards before "Show the other N" is pressed. */
const CARDS_AT_FIRST = 3;
const DAY_MS = 86400000;

/** What starts a question that makes an AI app use the vault. The space at its end is part of it. */
const ASK_START = `Use ${PRODUCT} to search my documents and answer this: `;

// -- pieces -------------------------------------------------------------------------

/** One part of the page under its heading, named by that heading for a screen reader. */
function section(key, title, aside, content) {
  const id = `${key}-title`;
  return el(
    "section",
    { class: "basic-section", key, "aria-labelledby": id },
    el(
      "div",
      { class: "section-head" },
      el("h2", { class: "section-title", id }, title),
      aside ? el("span", { class: "section-aside" }, aside) : null,
    ),
    content,
  );
}

/** "Copy", which says "Copied" for a moment. `id` tells the buttons of the page apart. */
function copyButton(ctx, id, text, what) {
  const copied = ctx.ui.copied === id;
  return button(copied ? "Copied" : "Copy", {
    small: true,
    id,
    glyph: copied ? "check" : "copy",
    label: copied ? "Copied" : `Copy: ${what}`,
    onclick: async () => {
      const done = await copyText(text);
      ctx.ui.copied = done ? id : null;
      ctx.render();
      if (!done) toast("Copying is not allowed here. Select the text and copy it yourself.", "red");
      setTimeout(() => {
        if (ctx.ui.copied === id) {
          ctx.ui.copied = null;
          ctx.render();
        }
      }, 2000);
    },
  });
}

/** A line of text to copy: the text in a box, the button beside it. */
function copyLine(ctx, id, text, what) {
  return el(
    "div",
    { class: "copy-line" },
    // The text wraps at its spaces and never inside an option; copied, it is one unbroken line.
    el("pre", { class: "code code-wrap", tabindex: 0, "aria-label": what }, el("code", null, codeWords(text))),
    copyButton(ctx, id, text, what),
  );
}

/** A link from Basic into Pro. It is a visit: the address changes and nothing is stored. */
function proLink(text, href, props = {}) {
  return el("a", Object.assign({ class: "link", href }, props), text);
}

/** "at 9:06 AM", "yesterday at 9:06 AM": when the vault was unlocked. */
function unlockedAt(ctx) {
  return at(ctx.state.unlocked_at, ctx.now);
}

/** Whether starting the app with this command opens a page by itself. The flag, when there, ends the command. */
function opensPage(command) {
  return !/ --no-browser( --window)?$/.test(command);
}

// -- the bars -----------------------------------------------------------------------

/**
 * The bar that says what is wrong, between the top bar and the page. `problem` is
 * the sentence of a list that could not be loaded, or "". While the app does not
 * answer, the bar also holds the command that starts it again: the one this page
 * was last told. A demo is not started again by that command, so a demo gets the
 * sentence alone.
 */
export function basicBars(ctx, problem) {
  if (ctx.offline) {
    const launch = ctx.state.launch || {};
    const command = ctx.state.demo ? null : launch.command;
    return el(
      "div",
      { class: "offline-bar", role: "alert", key: "offline" },
      icon("warning"),
      el(
        "div",
        { class: "bar-text" },
        el("p", null, `${PRODUCT} is not answering. Is it still running in its terminal? What you see may be out of date.`),
        command
          ? [
              el("p", null, "To start it again, run this in Terminal:"),
              copyLine(ctx, "bar-launch-copy", command, "the command that starts it again"),
              el(
                "p",
                null,
                launch.port_fixed === true
                  ? "This page continues by itself."
                  : opensPage(command)
                    ? "It opens a new page. This one can then be closed."
                    : "It prints the address of a new page. Open that one; this one can then be closed.",
              ),
            ]
          : null,
      ),
    );
  }
  if (problem) {
    return el(
      "div",
      { class: "offline-bar", role: "alert", key: "load-error" },
      icon("warning"),
      el("span", null, `This page could not be brought up to date: ${problem} Trying again.`),
    );
  }
  return null;
}

// -- Approvals ----------------------------------------------------------------------

function headline(waiting) {
  if (waiting === null) return "";
  if (waiting === 0) return "Nothing is waiting for you";
  return waiting === 1 ? "1 passage is waiting for you" : `${num(waiting)} passages are waiting for you`;
}

/** What a screen reader is told when the number of waiting passages rises, or returns to none. */
function waitingSaid(ctx, waiting) {
  const ui = ctx.ui;
  if (waiting === null) return ui.waitingSaid;
  if (ui.waitingCount !== null) {
    if (waiting > ui.waitingCount) {
      ui.waitingSaid = waiting === 1 ? "1 passage is waiting for your approval." : `${num(waiting)} passages are waiting for your approval.`;
    } else if (waiting === 0 && ui.waitingCount > 0) {
      ui.waitingSaid = "Nothing is waiting.";
    }
  }
  ui.waitingCount = waiting;
  return ui.waitingSaid;
}

function approvalsSection(ctx, problem) {
  const data = ctx.data.approvals;
  const apps = ctx.data.apps;
  const waiting = data ? data.waiting.length : null;
  const all = ctx.ui.allApprovals === true;
  // Everything near the cards that this page can change. A change of it makes "Release once" wait again.
  const around = [
    ctx.offline ? "offline" : problem,
    ctx.ui.copied === "bar-launch-copy" ? "copied" : "",
    all ? "all" : "",
    ctx.panel ? "panel" : "",
  ].join("\n");
  const drawn = approvalCards(ctx, { limit: all ? null : CARDS_AT_FIRST, around });
  const nothing = drawn !== null && drawn.cards.length === 0;
  return el(
    "section",
    { class: "basic-section basic-approvals", key: "basic-approvals", "aria-label": "Approvals" },
    // Above the cards stand the top bar, a bar that says what is wrong, this label and
    // the headline, and nothing else: what comes and goes on this page does so below them.
    el("p", { class: "basic-label" }, "Approvals"),
    el("h1", { class: "basic-status", id: "basic-status", tabindex: -1 }, headline(waiting)),
    el("p", { class: "sr-only", role: "status", key: "said" }, waitingSaid(ctx, waiting)),
    drawn === null
      ? loadingBlock()
      : drawn.cards.length === 0 && !drawn.granted
        ? null
        : el(
          "div",
          { class: "approval-stack", key: "stack" },
          drawn.cards,
          drawn.more > 0
            ? button(`Show the other ${num(drawn.more)}`, {
                id: "basic-more-approvals",
                key: "more",
                onclick: () => {
                  ctx.ui.allApprovals = true;
                  ctx.render();
                },
              })
            : null,
          drawn.granted,
        ),
    nothing && apps
      ? el(
          "p",
          { class: "basic-none", key: "none" },
          apps.apps.length
            ? "When an app asks for a passage that needs your approval, it appears here with the exact text. That passage is not released until you say so."
            : "No app has been added, so no app gets an answer.",
        )
      : null,
    el(
      "p",
      { class: "basic-again", key: "again" },
      "After you release a passage, the app gets it when it asks again. If it does not ask by itself, tell it: “try again”.",
    ),
  );
}

// -- What left the vault ------------------------------------------------------------

/** Which sentence a stopped call gets: by the name it came under, its code and its variant. */
function stopKind(item) {
  if (item.app === UNANSWERED) return "unanswered";
  if (item.code === "OVER_LIMIT") {
    if (item.variant === "pool-day" || item.variant === "pool-30-days") return "shared";
    return ["client-day", "client-30-days", "searches", "calls"].includes(item.variant) ? item.variant : "other";
  }
  return ["NOT_FOUND", "BAD_REQUEST", "UNAVAILABLE"].includes(item.code) ? item.code : "other";
}

function times(calls) {
  return calls === 1 ? "once" : `${num(calls)} times`;
}

function stopSentence(stop) {
  const name = stop.app;
  const calls = stop.calls;
  if (stop.kind === "unanswered") {
    return calls === 1
      ? "1 call came under a name that is not answered here, and got nothing."
      : `${num(calls)} calls came under a name that is not answered here, and got nothing.`;
  }
  if (stop.kind === "client-day") {
    return `${name} was stopped ${times(calls)} by its limit for 24 hours. It can get more as earlier answers become older than 24 hours.`;
  }
  if (stop.kind === "client-30-days") return `${name} was stopped ${times(calls)} by its limit for 30 days.`;
  if (stop.kind === "shared") return `${name} was stopped ${times(calls)} by a limit that all apps of its kind share. It is set in Pro.`;
  if (stop.kind === "searches") return `${name} was stopped ${times(calls)} by its limit of searches in 24 hours.`;
  if (stop.kind === "calls") return `${name} made too many calls in a minute and was told to wait, ${times(calls)}.`;
  if (stop.kind === "NOT_FOUND") return `${name} asked ${times(calls)} for something that is not there for it.`;
  if (stop.kind === "BAD_REQUEST") {
    return calls === 1 ? `1 request of ${name} could not be read.` : `${num(calls)} requests of ${name} could not be read.`;
  }
  if (stop.kind === "UNAVAILABLE") {
    return calls === 1 ? `Once the vault could not answer ${name} at that moment.` : `${num(calls)} times the vault could not answer ${name} at that moment.`;
  }
  return `${name} was refused ${times(calls)}.`;
}

/**
 * The calls that were stopped in the last 24 hours, one line per app and kind of
 * stop, the newest first. A passage that waits for approval is not among them: it
 * is a card. An entry of the record stands for one or more calls that were answered
 * the same way, and says how many.
 */
function stopsOfTheDay(ctx) {
  const since = ctx.now.getTime() - DAY_MS;
  const stops = new Map();
  for (const item of ctx.stops || []) {
    const time = parseTime(item.time);
    if (item.code === "NEEDS_APPROVAL" || !time || time.getTime() <= since) continue;
    const kind = stopKind(item);
    const key = `${item.app}\n${kind}`;
    const stop = stops.get(key) || { app: item.app, kind, calls: 0, seq: 0, limit: false };
    stop.calls += typeof item.count === "number" ? item.count : 1;
    stop.seq = Math.max(stop.seq, item.seq);
    // A stop by another limit than the app's own for 24 hours, which is the one figure its row shows.
    stop.limit = stop.limit || (item.code === "OVER_LIMIT" && item.variant !== "client-day");
    stops.set(key, stop);
  }
  return Array.from(stops.values()).sort((a, b) => b.seq - a.seq);
}

function leftRows(ctx) {
  const left = ctx.left;
  const since = parseTime(ctx.state.unlocked_at);
  const items = left.order.map((seq) => left.items.get(seq));
  const older = (item) => {
    const time = parseTime(item.time);
    return Boolean(since && time && time.getTime() < since.getTime());
  };
  // Newest first, so everything from the first older row on was there before this unlock.
  const split = items.findIndex(older);
  const rows = [];
  items.forEach((item, index) => {
    if (index === split) {
      if (index === 0) {
        rows.push(el("li", { class: "act-none", key: "none-since" }, `Nothing has left since you unlocked ${unlockedAt(ctx)}.`));
      }
      const words = `Before you unlocked ${unlockedAt(ctx)}`;
      rows.push(el("li", { class: "act-divider", key: "divider", role: "separator", "aria-label": words }, words));
    }
    rows.push(
      activityRow(item, {
        onopen: (row, keyboard) => ctx.openPanel(row.seq, keyboard),
        selected: ctx.panel && ctx.panel.seq === item.seq,
        now: ctx.now,
        passages: ctx.passages,
      }),
    );
  });
  return rows;
}

function leftSection(ctx, stops) {
  const today = ctx.state.today || { releases: 0, characters: 0 };
  const apps = ctx.data.apps ? ctx.data.apps.apps : [];
  const left = ctx.left;
  const aside =
    today.releases > 0
      ? `Last 24 hours: ${count(today.releases, "answer", "answers")}, ${count(today.characters, "character", "characters")}`
      : "Nothing left in the last 24 hours.";
  let list;
  if (!left.loaded) list = loadingBlock();
  else if (!left.order.length) list = el("p", { class: "basic-none", key: "none" }, "Nothing has left this vault yet.");
  else {
    const search = left.order.some((seq) => left.items.get(seq).tool === "search");
    const asks = apps.some((app) => Object.values(app.grants || {}).includes("approve"));
    list = [
      el("div", { class: "card card-flush", key: "rows" }, el("ul", { class: "act-list", "aria-label": "What left the vault, newest first" }, leftRows(ctx))),
      el(
        "div",
        { class: "basic-under", key: "under" },
        el(
          "div",
          { class: "basic-hints" },
          el("p", { class: "basic-hint" }, "Press a row to see the exact text that left."),
          search && asks
            ? el("p", { class: "basic-hint" }, "For an app set to “Ask me each time”, a search answer says which passages match. It holds no passage text.")
            : null,
          left.nextBefore === null
            ? el("p", { class: "basic-hint" }, "That is everything that left. Your own changes and every other entry are in Pro, under Activity.")
            : null,
        ),
        left.nextBefore !== null
          ? button(left.loadingMore ? "Loading…" : "Show more", { id: "basic-left-more", small: true, busy: left.loadingMore, onclick: () => ctx.loadMoreLeft() })
          : null,
      ),
    ];
  }
  return section("basic-left", "What left the vault", aside, [
    apps.length
      ? el(
          "div",
          { class: "basic-ask", key: "basic-ask" },
          el("p", null, "To use your documents, start your question in your AI app with:"),
          copyLine(ctx, "basic-ask-copy", ASK_START, "the start of a question"),
        )
      : null,
    list,
    stops.length
      ? el(
          "div",
          { class: "basic-stops", key: "stops" },
          el("h3", { class: "mini-title" }, "Stopped in the last 24 hours"),
          el(
            "ul",
            { class: "basic-stopped", key: "basic-stopped" },
            stops.map((stop) =>
              el(
                "li",
                { key: `a${stop.seq}` },
                el(
                  "button",
                  {
                    type: "button",
                    class: ["stop-row", ctx.panel && ctx.panel.seq === stop.seq && "is-selected"],
                    onclick: (event) => ctx.openPanel(stop.seq, event.detail === 0),
                    "aria-pressed": ctx.panel && ctx.panel.seq === stop.seq ? "true" : "false",
                  },
                  icon("refused"),
                  el("span", null, stopSentence(stop)),
                ),
              ),
            ),
          ),
        )
      : null,
  ]);
}

// -- Documents ----------------------------------------------------------------------

/** How much of a document has left so far, by the kind of app that received it. */
function leftSoFar(released) {
  const parts = [];
  if (released.hosted > 0) parts.push(`${num(released.hosted)} characters to apps that send text to a provider`);
  if (released.local > 0) parts.push(`${num(released.local)} characters to apps on this computer`);
  return parts.length ? parts.map((part) => el("span", { class: "released-part" }, part)) : "Nothing yet";
}

function documentsTable(ctx, groups) {
  const several = groups.length > 1;
  return el(
    "div",
    { class: "table-wrap", key: "table" },
    el(
      "table",
      { class: "table doc-table basic-doc-table" },
      el("caption", { class: "sr-only" }, "The documents in the vault"),
      el(
        "thead",
        null,
        el(
          "tr",
          null,
          el("th", { scope: "col", class: "col-title" }, "Title"),
          el("th", { scope: "col", class: "num col-passages" }, "Passages"),
          el("th", { scope: "col", class: "col-added" }, "Added"),
          el("th", { scope: "col", class: "col-left" }, "Left so far"),
        ),
      ),
      el(
        "tbody",
        null,
        groups.map((group) =>
          group.documents.map((document) =>
            el(
              "tr",
              { key: document.id },
              el(
                "th",
                { scope: "row", class: "doc-title" },
                el("span", { class: "doc-name", title: document.title }, document.title),
                several ? el("span", { class: "doc-group" }, group.name) : null,
              ),
              el("td", { class: "num" }, num(document.passages)),
              el("td", { class: "col-added", title: whenFull(document.added) }, dayShort(document.added, ctx.now)),
              el("td", { class: ["col-left", document.released.hosted + document.released.local > 0 ? null : "muted"] }, leftSoFar(document.released)),
            ),
          ),
        ),
      ),
    ),
  );
}

function documentsSection(ctx) {
  const data = ctx.data.documents;
  const apps = ctx.data.apps ? ctx.data.apps.apps : [];
  if (!data) return section("basic-documents", "Documents", null, loadingBlock());
  const groups = data.collections;
  const total = groups.reduce((sum, group) => sum + group.documents.length, 0);
  let counted;
  if (groups.length > 1) {
    const each = groups.map((group) => `${group.name} (${num(group.documents.length)})`).join(", ");
    counted = `${count(total, "document", "documents")} in ${num(groups.length)} groups (collections in Pro): ${each}.`;
  } else if (total === 0) counted = "No documents yet.";
  else counted = total === 1 ? "1 document, sealed in the vault." : `${num(total)} documents, sealed in the vault.`;
  // Said wherever documents are added while an app that sends text to a provider may use them.
  const senders = apps.filter((app) => app.hosted && Object.keys(app.grants || {}).length > 0);
  const open = ctx.ui.docsOpen === true;
  return section("basic-documents", "Documents", null, [
    el("p", { class: "basic-count", key: "count" }, counted),
    senders.length
      ? el(
          "p",
          { class: "note note-amber", id: "docs-sentence", key: "sentence", role: "note" },
          icon("warning"),
          el("span", { class: "note-text" }, senders.length === 1 ? basicGrantSentence(senders[0]) : GENERAL_SENTENCE),
        )
      : null,
    total > 0
      ? el(
          "div",
          { class: "basic-fold", key: "fold" },
          button(open ? "Hide the documents" : "Show the documents", {
            kind: "quiet",
            small: true,
            id: "basic-docs-toggle",
            expanded: open ? "true" : "false",
            onclick: () => {
              ctx.ui.docsOpen = !open;
              ctx.render();
            },
          }),
        )
      : null,
    total > 0 && open
      ? [
          el("div", { class: "card basic-docs", key: "documents" }, documentsTable(ctx, groups)),
          groups
            .filter((group) => !group.show_titles)
            .map((group) =>
              el(
                "p",
                { class: "basic-hint", key: `titles-${group.name}` },
                groups.length > 1 ? `In “${group.name}”, apps are shown a document's number, not its file name.` : "Apps are shown a document's number, not its file name.",
              ),
            ),
        ]
      : null,
  ]);
}

// -- Apps ---------------------------------------------------------------------------

/** Whether an app has a limit that is not the one every app gets. */
function hasOwnLimits(app, settings) {
  return Object.keys(settings.defaults).some((key) => app.limits[key] !== settings.defaults[key]);
}

/** What an app may do, in the three labels. With several groups: a summary, and one line per group. */
function accessOf(app, groups) {
  const mode = (group) => app.grants[group.name] || "none";
  if (groups.length <= 1) return { summary: GRANT_LABEL[groups.length ? mode(groups[0]) : "none"], lines: [] };
  const modes = groups.map(mode);
  const same = modes.every((entry) => entry === modes[0]);
  let summary = "Different for each group";
  if (same) summary = modes[0] === "none" ? GRANT_LABEL.none : `${GRANT_LABEL[modes[0]]} for every group`;
  const lines = groups.map((group) =>
    app.hosted && !group.allow_hosted
      ? `${group.name}: ${GRANT_LABEL.none} (not open to apps that send text to a provider)`
      : `${group.name}: ${GRANT_LABEL[mode(group)]}`,
  );
  return { summary, lines };
}

function appRow(ctx, app, groups, settings, stops) {
  const access = accessOf(app, groups);
  const seen = app.last_seen;
  const used = app.usage.chars_day;
  const limit = app.limits.chars_per_day;
  // From where the meter turns amber.
  const close = limit > 0 ? used / limit >= 0.8 : used > 0;
  const others =
    Boolean(settings) &&
    (hasOwnLimits(app, settings) || settings.changed_from_new.length > 0 || stops.some((stop) => stop.app === app.name && stop.limit));
  return el(
    "div",
    { class: ["basic-app", !app.served && "is-idle"], key: `row-app-${app.name}` },
    el(
      "div",
      { class: "basic-app-line" },
      el("span", { class: "basic-app-name mono", title: app.name }, app.name),
      destinationChip(app),
      el("span", { class: "basic-app-access" }, access.summary),
      app.paused ? chip("Paused", "amber", "pause") : !app.served ? chip("Not answered in this session", "quiet") : null,
      // With one group the access is one value, and the control that changes it stands here.
      // With several it is one value for each: Basic says them and changes none.
      groups.length > 1 ? proLink("Change in Pro", "#/apps", { key: `change-${app.name}`, class: "link basic-app-change" }) : null,
    ),
    access.lines.length ? el("ul", { class: "basic-app-groups" }, access.lines.map((line) => el("li", null, line))) : null,
    el(
      "div",
      { class: "basic-app-meter" },
      el(
        "div",
        { class: "basic-app-used" },
        el("span", null, `In its last 24 hours: ${count(app.usage.releases_day, "answer", "answers")}, ${num(used)} of ${num(limit)} characters`),
        meter(used, limit, `${app.name}: ${num(used)} of ${num(limit)} characters in its last 24 hours`),
      ),
      // The figure is this app's own limit for 24 hours. It is never shown as the whole account.
      others ? el("p", { class: "basic-app-others" }, "Other limits apply too. They are set in Pro.") : null,
      close ? el("p", { class: "basic-app-close" }, "Close to its limit for 24 hours. Requests past it are refused.") : null,
    ),
    el(
      "p",
      { class: "basic-app-seen" },
      seen && seen.time
        ? [
            "Last asked ",
            el("time", { datetime: seen.time, title: whenFull(seen.time) }, ago(seen.time, ctx.now)),
            seen.claimed ? ` · says it is ${seen.claimed}, unverified` : "",
          ]
        : "Has not asked yet",
    ),
  );
}

function appsSection(ctx, stops) {
  const data = ctx.data.apps;
  if (!data) return section("basic-apps", "Apps", null, loadingBlock());
  const apps = data.apps;
  const groups = data.collections;
  // An app is added on this page only where one value will describe what it may do, and
  // where adding it cannot change which kinds of apps are answered.
  const expressible = groups.length <= 1 && !apps.some((app) => !app.hosted);
  return section("basic-apps", "Apps", null, [
    apps.length
      ? el("p", { class: "basic-hint", id: "basic-labels", key: "labels" }, `A name is a label: ${PRODUCT} cannot check which program is really asking.`)
      : el("p", { class: "basic-none", key: "none" }, "No app has been added, so no app gets an answer."),
    apps.length ? el("div", { class: "card basic-app-list", key: "list" }, apps.map((app) => appRow(ctx, app, groups, ctx.data.settings, stops))) : null,
    expressible ? null : el("p", { class: "basic-app-add", key: "add" }, proLink("Add an app (in Pro)", "#/apps", { id: "basic-add-app" })),
  ]);
}

// -- Also set for this vault, in Pro -------------------------------------------------

/** What Pro set that Basic has no control for, as sentences, by the page of Pro where each is set. */
function alsoLines(ctx) {
  const apps = ctx.data.apps;
  const documents = ctx.data.documents;
  const settings = ctx.data.settings;
  const kinds = ctx.state.serving ? ctx.state.serving.kinds : null;
  const under = { apps: [], documents: [], vault: [] };
  for (const app of apps.apps) {
    if (hasOwnLimits(app, settings)) under.apps.push(`${app.name} has limits of its own.`);
  }
  for (const app of apps.apps) {
    if (app.paused) under.apps.push(`${app.name} is paused until the vault locks.`);
  }
  if (apps.apps.some((app) => app.hosted) && apps.apps.some((app) => !app.hosted)) {
    const answered = { hosted: "only the provider apps are answered", local: "only the apps on this computer are answered", both: "both kinds are answered" }[kinds];
    if (answered) {
      under.apps.push(
        `This vault has apps that send text to a provider and apps that run on this computer. In this session ${answered}. Which kinds are answered is chosen in Pro after each unlock.`,
      );
    }
  }
  const groups = documents.collections;
  if (groups.length > 1) {
    under.documents.push(`The documents are in ${num(groups.length)} groups (collections in Pro). What an app may do is set for each group.`);
  }
  for (const group of groups) {
    if (group.note) under.documents.push(`“${group.name}” carries a note that apps are shown.`);
  }
  for (const group of groups) {
    if (group.show_titles) under.documents.push(`Apps are shown the titles of documents in “${group.name}”.`);
  }
  if (groups.length > 1) {
    for (const group of groups) {
      if (!group.allow_hosted) under.documents.push(`“${group.name}” is not open to apps that send text to a provider.`);
    }
  }
  if (settings.changed_from_new.length > 0) under.vault.push("Limits were changed from the values of a new vault.");
  if (settings.replaced_strings > 0) {
    under.vault.push(
      settings.replaced_strings === 1
        ? "1 word or number is replaced with [REDACTED] in everything that leaves."
        : `${num(settings.replaced_strings)} words or numbers are replaced with [REDACTED] in everything that leaves.`,
    );
  }
  return under;
}

function alsoSection(ctx) {
  // Drawn when the three lists it reads are in, and only when it has a line.
  if (!ctx.data.apps || !ctx.data.documents || !ctx.data.settings) return null;
  const under = alsoLines(ctx);
  const pages = [
    ["apps", "Under Apps"],
    ["documents", "Under Documents"],
    ["vault", "Under Vault"],
  ].filter(([page]) => under[page].length > 0);
  if (!pages.length) return null;
  return section("basic-also", "Also set for this vault, in Pro", null, [
    el("p", { class: "basic-hint", key: "lead" }, "Parts of this vault were set up in Pro. Basic shows them and does not change them."),
    pages.map(([page, title]) =>
      el(
        "div",
        { class: "also-group", key: `also-group-${page}` },
        el("div", { class: "also-head" }, el("h3", { class: "mini-title" }, title), proLink("Open in Pro", `#/${page}`, { key: `also-${page}` })),
        el("ul", { class: "also-list" }, under[page].map((line) => el("li", null, line))),
      ),
    ),
  ]);
}

// -- the foot -----------------------------------------------------------------------

function foot(ctx) {
  const state = ctx.state;
  const launch = state.launch || {};
  // A demo is not opened again by this command: its guide has the one for a vault of one's own.
  const command = state.demo ? null : launch.command;
  return el(
    "footer",
    { class: "basic-foot", key: "basic-foot" },
    command
      ? el(
          "div",
          { class: "launch", key: "launch" },
          el("p", null, `To open ${PRODUCT} again after you quit it or restart this computer, run this in Terminal:`),
          copyLine(ctx, "launch-copy", command, `the command that opens ${PRODUCT} again`),
          el(
            "p",
            null,
            opensPage(command)
              ? "A page opens and asks for a new pairing code from that terminal, then for your passphrase."
              : "It prints the address of a page. That page asks for a new pairing code from that terminal, then for your passphrase.",
          ),
        )
      : null,
    state.record && state.record.verified ? el("p", { key: "record" }, "The record of what left was checked when you unlocked.") : null,
    el(
      "p",
      { key: "words" },
      "A name or number that must never leave? Add it under “",
      proLink("Words to replace", "#/vault", { id: "basic-words" }),
      "” in Pro.",
    ),
    el("p", { class: "gate-foot", key: "version" }, `Version ${state.version}. Everything on this page comes from ${PRODUCT} on this computer.`),
  );
}

// -- the page -----------------------------------------------------------------------

/** Everything between the top bar and the bar with the view: the content of `#/basic`. */
export function basicView(ctx, problem) {
  const stops = stopsOfTheDay(ctx);
  return [
    approvalsSection(ctx, problem),
    // Notices stand under the cards. Basic has no strip for the two kinds of apps, so it shows that notice too.
    warningList(ctx, { bothKinds: true }),
    leftSection(ctx, stops),
    documentsSection(ctx),
    appsSection(ctx, stops),
    alsoSection(ctx),
    foot(ctx),
  ];
}
