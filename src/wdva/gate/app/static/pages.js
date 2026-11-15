// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The plain pages of the simple interface: Documents, Activity, Settings, and "How it
// works and what it cannot do". Each is one quiet column. Every text they show is a
// text of the table, in the language the page shows; what stays English is data: a
// name, a path, a command, and the app's own sentence where the page has no text.
//
// What these pages change: documents are added and removed, and beside the vault the
// page the app opens on. Nothing else. An app is named by the name it has in the vault.

import { el, icon } from "./dom.js";
import { agoShort, day, list, whenFull } from "./format.js";
import { cannotList, dropZone, isSample, loadingBlock, removeDocument, uploadFiles, versionLine } from "./manage.js";
import { UNANSWERED, button, chip, copyLine, iconButton, languageSwitch, openSheet, pathText, segmented, sheetFoot, sheetHead } from "./parts.js";
import { basicGrantSentence, generalSentence } from "./sheets.js";
import { around, t } from "./texts.js";

/** The group that documents go to in a vault that has none yet. */
const FIRST_GROUP = "documents";

// -- Documents ----------------------------------------------------------------------

/** What an app may do with one group, as the app said it: "approve", "read" or "none", and nothing else. */
function grantOf(app, group) {
  const grants = app.grants || {};
  const mode = Object.prototype.hasOwnProperty.call(grants, group) ? grants[group] : "none";
  return mode === "approve" || mode === "read" ? mode : "none";
}

/** The apps that can ask for what is in `group`, or with null for what is in any group. */
function readersOf(apps, group) {
  if (group !== null) return apps.filter((app) => grantOf(app, group) !== "none");
  return apps.filter((app) => Object.keys(app.grants || {}).some((name) => grantOf(app, name) !== "none"));
}

/**
 * The sentence that says where text from the documents goes: the one of the app when
 * exactly one app can ask, the general one when several can, none when no app can.
 */
function whereTextGoes(readers) {
  if (!readers.length) return null;
  return readers.length === 1 ? basicGrantSentence(readers[0]) : generalSentence();
}

/** "3 files: a.txt, b.txt and c.txt" and, past three names, "5 files: a.txt, b.txt, c.txt and 2 more". */
function fileNames(files) {
  const names = files.slice(0, 3).map((file) => file.name);
  if (files.length <= 3) return t("group.status.files", { n: files.length, names });
  // Past three names, the names are a plain row and the last words say how many more there are.
  const first = t("group.status.files", { n: files.length, names: names.join(", ") });
  return [first, t("group.status.more", { n: files.length - 3 })].join(" ");
}

/**
 * The sheet that asks which group the files go to, in a vault with several. No group
 * is chosen for the owner. Each group says who can ask for what is in it, and once a
 * group is chosen that an app can use, the sentence about where text goes stands
 * above the buttons. "Cancel" drops the files: nothing is sent. It is built from the
 * lists as they are when it opens.
 */
export async function addToWhichGroup(ctx, files) {
  if (!files.length || !ctx.data.apps || !ctx.data.documents) return;
  const apps = ctx.data.apps.apps;
  const groups = ctx.data.documents.collections.map((group) => group.name);
  const form = { group: null };
  const sheet = openSheet(
    (self) => {
      const sentence = form.group === null ? null : whereTextGoes(readersOf(apps, form.group));
      return el(
        "div",
        { class: "sheet-content" },
        sheetHead(t("group.title")),
        el("p", { class: "sheet-files" }, fileNames(files)),
        el(
          "div",
          { class: "choice-list choice-stack", role: "radiogroup", "aria-label": t("group.title") },
          groups.map((group, index) => {
            const names = readersOf(apps, group).map((app) => app.name);
            return el(
              "label",
              { class: ["choice", form.group === group && "is-selected"], key: `group-${group}` },
              el("input", {
                type: "radio",
                name: "docs-group",
                value: group,
                checked: form.group === group,
                "aria-labelledby": `docs-group-${index}-name`,
                "aria-describedby": `docs-group-${index}-who`,
                onchange: () => {
                  form.group = group;
                  self.update();
                },
              }),
              el(
                "span",
                { class: "choice-text" },
                el("span", { class: "choice-label", id: `docs-group-${index}-name` }, group),
                el("span", { class: "choice-hint", id: `docs-group-${index}-who` }, names.length ? list(names) : t("group.status.nobody")),
              ),
            );
          }),
        ),
        sentence ? el("p", { class: "note note-amber", id: "group-sentence", role: "note" }, icon("warning"), el("span", { class: "note-text" }, sentence)) : null,
        sheetFoot(
          button(t("common.button.cancel"), { onclick: () => self.close(null), autofocus: true }),
          // Only this press sends the files: a key pressed on a choice does not.
          button(t("group.button.add"), {
            kind: "primary",
            disabled: form.group === null,
            onclick: () => {
              if (form.group !== null) self.close(form.group);
            },
          }),
        ),
      );
    },
    { label: t("group.title") },
  );
  const group = await sheet.closed;
  if (typeof group === "string") await uploadFiles(ctx, group, files);
}

/** One document of the list: its title, how many passages it has and when it was added, and the bin. */
function documentRow(ctx, entry, several) {
  const document = entry.document;
  return el(
    "li",
    { class: "row doc-row", key: document.id },
    el(
      "div",
      { class: "row-text" },
      el(
        "span",
        { class: "row-what" },
        el("span", { class: "doc-name", title: document.title }, document.title),
        // With several groups each document says which one it is in.
        several ? el("span", { class: "row-group" }, entry.group) : null,
      ),
      el("span", { class: "row-sub", title: whenFull(document.added) }, t("docs.status.row", { n: document.passages, date: day(document.added, ctx.now) })),
    ),
    // Removing narrows what apps can get, so it is offered whatever else the vault holds.
    iconButton("trash", t("docs.label.remove", { title: document.title }), { onclick: () => removeDocument(ctx, document) }),
  );
}

/**
 * Documents: how many the vault holds, the two sentences, the zone that takes files
 * with the lines of the files added on this page, and the documents, newest first.
 */
export function documentsPage(ctx) {
  const title = t("docs.title");
  const data = ctx.data.documents;
  // The zone is drawn once the apps are known, so that it never stands without the line about who can ask.
  if (!data || !ctx.data.apps) return { title, body: loadingBlock() };
  const apps = ctx.data.apps.apps;
  const groups = data.collections;
  const several = groups.length > 1;
  const held = [];
  for (const group of groups) {
    for (const document of group.documents) held.push({ document, group: group.name });
  }
  held.sort((a, b) => String(b.document.added).localeCompare(String(a.document.added)));
  const sample = held.find((entry) => isSample(entry.document));
  const second = whereTextGoes(readersOf(apps, null));
  // While a passage waits for the owner, no button of the page is filled.
  const waits = ctx.state.today ? ctx.state.today.approvals_waiting > 0 : false;
  const zone = { who: true, said: true, filled: !waits };
  return {
    title,
    aside: held.length ? t("docs.status.count", { n: held.length }) : t("docs.status.none"),
    body: [
      el("div", { class: "says", key: "says" }, el("p", { id: "docs-say-1" }, t("docs.say.1")), second ? el("p", { id: "docs-say-2" }, second) : null),
      several
        ? // Several groups: the files are held, and the owner is asked which group they go to.
          dropZone(ctx, "chosen", Object.assign(zone, { lists: groups.map((group) => group.name), take: (files) => addToWhichGroup(ctx, files) }))
        : dropZone(ctx, groups.length ? groups[0].name : FIRST_GROUP, zone),
      sample
        ? el(
            "p",
            { class: "doc-sample", key: "sample" },
            el("span", null, t("docs.status.sample")),
            button(t("docs.button.sample.remove"), { small: true, id: "sample-remove", onclick: () => removeDocument(ctx, sample.document) }),
          )
        : null,
      held.length
        ? el(
            "ul",
            { class: "rows doc-rows", key: "documents", "aria-label": title },
            held.map((entry) => documentRow(ctx, entry, several)),
          )
        : null,
    ],
  };
}

// -- Activity -----------------------------------------------------------------------

/** The text of a call that a limit stopped, by which limit it was. The two limits that apps of a kind share have one text. */
const STOPPED_BY_LIMIT = {
  "client-day": "row.stop.client-day",
  "client-30-days": "row.stop.client-30-days",
  "pool-day": "row.stop.shared",
  "pool-30-days": "row.stop.shared",
  searches: "row.stop.searches",
  calls: "row.stop.calls",
};

/** The text of a call that was stopped for another reason, by the code of its answer. */
const STOPPED_BY_CODE = {
  NEEDS_APPROVAL: "row.stop.NEEDS_APPROVAL",
  NOT_FOUND: "row.stop.NOT_FOUND",
  BAD_REQUEST: "row.stop.BAD_REQUEST",
  UNAVAILABLE: "row.stop.UNAVAILABLE",
};

/** Which text a stopped call gets: by the name it came under, by the limit that stopped it, else by its code. */
function stopKey(item) {
  const own = Object.prototype.hasOwnProperty;
  if (item.app === UNANSWERED) return "row.stop.unanswered";
  const table = item.code === "OVER_LIMIT" ? STOPPED_BY_LIMIT : STOPPED_BY_CODE;
  const word = item.code === "OVER_LIMIT" ? item.variant : item.code;
  return typeof word === "string" && own.call(table, word) ? table[word] : "row.stop.other";
}

/** What the page knows of the passage an entry names: the entry's own fields first, then what was learned by its reference. */
function passageOf(item, passages) {
  const known = (passages && item.ref && passages.get(item.ref)) || {};
  const title = typeof item.title === "string" && item.title ? item.title : known.title;
  let number = typeof item.passage === "number" ? item.passage : known.passage;
  if (typeof number !== "number") {
    // A reference ends in the number of its passage.
    const tail = /\.(\d+)$/.exec(String(item.ref || ""));
    number = tail ? Number(tail[1]) : null;
  }
  return title && number ? { title, passage: number } : null;
}

/**
 * What one entry says, as the sentence of its row: what an app was given, or what
 * stopped its call. It is made from fields of the entry and never from the app's
 * English sentence. `passages` maps references to what the page has learned of them.
 */
export function rowSentence(item, passages = null) {
  const app = item.app;
  if (item.kind === "refusal") return t(stopKey(item), { app });
  if (item.tool === "search") {
    if (typeof item.results !== "number") return t("row.search.plain", { app });
    return item.results > 0 ? t("row.search", { app, n: item.results }) : t("row.search.none", { app });
  }
  const known = passageOf(item, passages);
  return known ? t("row.read", { app, passage: known.passage, title: known.title }) : t("row.read.plain", { app });
}

/** Whether an entry is a search whose answer held no text of a document: only which passages hold the words. */
export function withoutDocumentText(item) {
  return item.tool === "search" && item.document_text === false;
}

/**
 * One row of what was given to apps or of what was stopped: its sentence, then how
 * much and when. How much is counted in the owner's own text: for a passage its own
 * length, which is the figure its card showed, and for a search whose answer held no
 * text of a document the words that say so. A press opens the details of its entry.
 */
export function activityRow(ctx, item) {
  const selected = Boolean(ctx.panel && ctx.panel.seq === item.seq);
  const stopped = item.kind === "refusal";
  const none = withoutDocumentText(item);
  const characters = typeof item.passage_characters === "number" ? item.passage_characters : item.characters;
  const time = (stopped && item.last_time) || item.time;
  return el(
    "li",
    { class: "row-item", key: `a${item.seq}` },
    el(
      "button",
      {
        type: "button",
        class: ["row", "row-open", selected && "is-selected"],
        title: t("row.label.open"),
        "aria-pressed": selected ? "true" : "false",
        onclick: (event) => ctx.openPanel(item.seq, event.detail === 0),
      },
      el("span", { class: "row-what" }, rowSentence(item, ctx.passages)),
      el(
        "span",
        { class: "row-sub" },
        item.approved === true ? chip(t("row.chip.approved"), "quiet", "check") : null,
        stopped && item.count > 1 ? el("span", { class: "row-times" }, t("row.status.times", { n: item.count })) : null,
        !stopped && none ? el("span", { class: "row-chars" }, t("row.status.notext")) : null,
        !stopped && !none && characters > 0 ? el("span", { class: "row-chars" }, t("row.status.chars", { n: characters })) : null,
        el("time", { class: "row-time", datetime: time, title: whenFull(time) }, agoShort(time, ctx.now)),
      ),
    ),
  );
}

/**
 * Activity: what was given to apps, or what was stopped, newest first, twenty at a
 * time. Every entry, the owner's own changes included, is one press away, in English.
 */
export function activityPage(ctx) {
  const title = t("activity.title");
  const shown = ctx.rows.tab;
  const rows = ctx.rows[shown];
  let content;
  if (!rows.loaded) content = loadingBlock();
  else if (!rows.order.length) {
    content = el("p", { class: "rows-none", key: "none" }, shown === "refusal" ? t("activity.status.none.stopped") : t("home.left.status.none"));
  } else {
    content = [
      el(
        "ul",
        { class: "rows act-rows", key: `rows-${shown}`, "aria-label": shown === "refusal" ? t("activity.label.stopped") : t("activity.label.left") },
        rows.order.map((seq) => activityRow(ctx, rows.items.get(seq))),
      ),
      el(
        "div",
        { class: "rows-foot", key: "foot" },
        rows.nextBefore !== null
          ? button(t("common.button.more"), { id: "rows-more", small: true, busy: rows.loadingMore, onclick: () => ctx.moreRows() })
          : el("span", { class: "rows-end" }, t("activity.status.end")),
      ),
    ];
  }
  return {
    title,
    body: [
      el(
        "div",
        { class: "rows-head", key: "head" },
        segmented({
          name: "activity-kind",
          legend: t("activity.label.filter"),
          value: shown,
          options: [
            { value: "release", label: t("activity.label.left") },
            { value: "refusal", label: t("activity.label.stopped") },
          ],
          onchange: (tab) => ctx.showRows(tab),
        }),
      ),
      // What a press on a row shows: the text that left, or for a call that was stopped what was asked and why.
      el("div", { class: "says", key: "says" }, el("p", null, t(shown === "refusal" ? "activity.say.1.stopped" : "activity.say.1"))),
      content,
      el("p", { class: "page-more", key: "full" }, el("a", { class: "link", id: "activity-full", href: "#/pro/activity" }, t("activity.button.full"))),
    ],
  };
}

// -- Settings -----------------------------------------------------------------------

/** The six pages with every setting, in their order: the address of each and the key of its row. */
const ALL_SETTINGS = [
  ["overview", "settings.label.overview"],
  ["activity", "settings.label.activity"],
  ["approvals", "settings.label.approvals"],
  ["documents", "settings.label.documents"],
  ["apps", "settings.label.apps"],
  ["vault", "settings.label.vault"],
];

/**
 * Settings: the language, the six pages with every setting, the command that opens
 * the app again, the way to the page that says how it works, and on which page the
 * app opens. Keeping a language or that page says nothing when it worked: the screen
 * is the answer.
 */
export function settingsPage(ctx) {
  const state = ctx.state;
  const kept = state.interface || {};
  // A demo is not opened again by the command the app names: there it is not shown.
  const command = state.demo ? null : (state.launch || {}).command;
  const [beforePath, afterPath] = around("settings.status.vault", "path");
  return {
    title: t("settings.title"),
    body: [
      el(
        "section",
        { class: "setting", key: "language" },
        el("h2", { class: "setting-label", id: "setting-language" }, t("common.label.language")),
        languageSwitch(ctx.chooseLanguage),
      ),
      el(
        "section",
        { class: "setting-block", key: "all", "aria-labelledby": "setting-all" },
        el("h2", { class: "block-title", id: "setting-all" }, t("settings.all.title")),
        el("div", { class: "says" }, el("p", null, t("settings.say.1"))),
        el(
          "ul",
          { class: "rows link-rows" },
          ALL_SETTINGS.map(([page, key]) =>
            el(
              "li",
              { class: "row-item", key: `all-${page}` },
              el("a", { class: "row row-link", href: `#/pro/${page}` }, icon(page), el("span", { class: "row-what" }, t(key)), icon("right", "row-go")),
            ),
          ),
        ),
      ),
      command
        ? el(
            "section",
            { class: "setting-block", key: "launch" },
            el("div", { class: "says" }, el("p", null, t("settings.say.2"))),
            copyLine(ctx, "settings-launch-copy", command, t("bar.label.command")),
          )
        : null,
      el("p", { class: "page-more", key: "how" }, el("a", { class: "link", id: "settings-how", href: "#/how" }, t("how.title"))),
      el(
        "section",
        { class: "setting", key: "start" },
        el("h2", { class: "setting-label", id: "setting-start" }, t("settings.label.start")),
        segmented({
          name: "start",
          legend: t("settings.label.start"),
          value: kept.view === "basic" ? "basic" : "pro",
          options: [
            { value: "basic", label: t("settings.label.start.home") },
            { value: "pro", label: t("settings.label.start.all") },
          ],
          onchange: (view) => ctx.keepView(view),
        }),
      ),
      el(
        "div",
        { class: "page-facts", key: "facts" },
        el("p", null, t("settings.status.version", { version: state.version })),
        el("p", null, beforePath, pathText(state.store.path, false), afterPath),
      ),
    ],
  };
}

// -- How it works and what it cannot do ---------------------------------------------

/** The parts of the page, top to bottom: a heading, and its sentences or the eight statements. */
const HOW = [
  {
    title: "how.works.title",
    lines: ["how.works.1", "how.works.2", "how.works.3", "how.works.4", "how.works.5", "how.works.6", "how.works.7"],
  },
  { title: "how.ask.title", lines: ["how.ask.1", "how.ask.2", "how.ask.3", "how.ask.4", "how.ask.5"] },
  {
    title: "how.connect.title",
    lines: [
      "how.connect.1",
      "how.connect.2",
      "how.connect.3",
      "how.connect.4",
      "how.connect.5",
      "how.connect.6",
      "how.connect.7",
      "how.connect.8",
      "how.connect.9",
      "how.connect.10",
      "how.connect.11",
      "how.connect.12",
    ],
  },
  { title: "how.files.title", lines: ["how.files.1", "how.files.2", "how.files.3"] },
  { title: "how.cannot.title", lines: null },
  { title: "how.locked.title", lines: ["how.locked.1", "how.locked.2"] },
];

/**
 * The one page of the simple interface that explains at length: how a passage reaches
 * an app, what the box that asks does, what "Connect" writes and what it never does,
 * where the owner's files stay, what the product cannot do, what a locked vault
 * shows, and which version this is.
 */
export function howPage(ctx) {
  return {
    title: t("how.title"),
    body: [
      HOW.map((part) =>
        el(
          "section",
          { class: "how", key: part.title, "aria-labelledby": part.title.replace(/\./g, "-") },
          el("h2", { class: "block-title", id: part.title.replace(/\./g, "-") }, t(part.title)),
          part.lines ? part.lines.map((key) => el("p", { key }, t(key))) : cannotList(),
        ),
      ),
      el("p", { class: "version", key: "version" }, versionLine(ctx.state)),
      el("p", { class: "page-more", key: "back" }, el("a", { class: "link", id: "how-back", href: "#/home" }, t("how.button.back"))),
    ],
  };
}
