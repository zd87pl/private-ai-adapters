// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The screens where the owner changes things: Documents, Apps and Vault.

import { api } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { ago, bytes, count, dayShort, list, megabytes, num, when, whenFull } from "./format.js";
import {
  button,
  card,
  chip,
  confirmSheet,
  destinationChip,
  emptyState,
  iconButton,
  meterRow,
  note,
  pathText,
  segmented,
  toast,
  toastError,
  toggle,
} from "./parts.js";
import {
  BOTH_KINDS_SENTENCE,
  GRANT_LABEL,
  LIMIT_FIELDS,
  changeGrant,
  chooseServing,
  numberRow,
  openAddApp,
  openAppLimits,
  openCollectionSheet,
  openConnect,
  readNumber,
} from "./sheets.js";

export function loadingBlock() {
  return el("div", { class: "loading", role: "status" }, el("span", { class: "spinner" }), "Loading…");
}

// -- adding files -------------------------------------------------------------------

function uploadState(ctx, collection) {
  if (!ctx.ui.uploads[collection]) ctx.ui.uploads[collection] = { items: [], over: 0, busy: false };
  return ctx.ui.uploads[collection];
}

function extension(name) {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot).toLowerCase();
}

/** Send files to the vault one after the other and keep a line per file. */
export async function uploadFiles(ctx, collection, files) {
  const rules = (ctx.data.documents && ctx.data.documents.upload) || { max_bytes: 20971520, types: [], pdf: true };
  const state = uploadState(ctx, collection);
  const batch = Array.from(files).map((file) => ({ file, name: file.name, status: "waiting", message: "" }));
  if (!batch.length) return;
  state.items = state.items.concat(batch);
  state.busy = true;
  ctx.render();
  let added = 0;
  for (const item of batch) {
    const kind = extension(item.name);
    if (rules.types.length && !rules.types.includes(kind)) {
      item.status = "error";
      item.message = `Not added: only ${list(rules.types)} files can be read.`;
    } else if (kind === ".pdf" && rules.pdf === false) {
      item.status = "error";
      item.message = `Not added: reading PDFs is not installed with ${PRODUCT}.`;
    } else if (item.file.size > rules.max_bytes) {
      item.status = "error";
      item.message = `Not added: larger than ${megabytes(rules.max_bytes)}.`;
    } else if (item.file.size === 0) {
      item.status = "error";
      item.message = "Not added: the file is empty.";
    } else {
      item.status = "sending";
      ctx.render();
      try {
        const answer = await api.addDocument(collection, item.name, item.file);
        const document = answer.document || {};
        item.status = "done";
        item.message = `Sealed: ${count(document.passages, "passage", "passages")}, ${num(document.characters)} characters.`;
        added += 1;
      } catch (error) {
        item.status = "error";
        item.message = error.code === "duplicate" ? "Not added: this document is already in the vault." : `Not added: ${error.message}`;
        if (error.status === 401 || error.code === "locked") break;
      }
    }
    item.file = null;
    ctx.render();
  }
  state.busy = false;
  const failed = batch.length - added;
  if (added && !failed) toast(`${count(added, "document", "documents")} added to “${collection}”.`, "green");
  else if (added) toast(`${num(added)} added to “${collection}”, ${num(failed)} not added.`, "amber");
  else toast(batch.length === 1 ? batch[0].message : "No document was added.", "red");
  await ctx.refresh();
}

function sealedCount(state) {
  return state.items.filter((item) => item.status === "done").length;
}

/**
 * What is true of a file after it was added. The browser hands the app the file's
 * name and content, never its place, so the app can say only this.
 */
export function leftOutsideText(sealed) {
  return sealed === 1
    ? "The file you chose is still where it was, not encrypted. Removing or moving it is up to you: this app cannot do it for you, and it cannot hand a file back out of the vault."
    : "The files you chose are still where they were, not encrypted. Removing or moving them is up to you: this app cannot do it for you, and it cannot hand a file back out of the vault.";
}

/** The place to drop files, with a button for people who would rather pick them. */
export function dropZone(ctx, collection, options = {}) {
  const state = uploadState(ctx, collection);
  const rules = (ctx.data.documents && ctx.data.documents.upload) || { types: [], max_bytes: 20971520 };
  const inputId = `files-${collection}`;
  const hasFiles = (event) => event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files");
  return el(
    "div",
    {
      class: ["drop", state.over > 0 && "is-over", options.large && "drop-large"],
      key: `drop-${collection}`,
      "data-collection": collection,
      ondragenter: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        state.over += 1;
        ctx.render();
      },
      ondragover: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      },
      ondragleave: () => {
        state.over = Math.max(0, state.over - 1);
        ctx.render();
      },
      ondrop: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.stopPropagation();
        state.over = 0;
        uploadFiles(ctx, collection, event.dataTransfer.files);
      },
    },
    el(
      "div",
      { class: "drop-line" },
      icon("upload"),
      el(
        "span",
        { class: "drop-text" },
        el("strong", null, state.over > 0 ? `Drop to add to “${collection}”` : "Drop files here"),
        state.over > 0 ? null : " or ",
      ),
      el(
        "label",
        { class: ["btn", "btn-default", "btn-small", "file-button"], for: inputId },
        el("span", { class: "btn-label" }, "Choose files"),
        el("input", {
          type: "file",
          id: inputId,
          class: "sr-only",
          multiple: true,
          accept: rules.types.join(","),
          "aria-label": `Choose files to add to ${collection}`,
          onchange: (event) => {
            const files = Array.from(event.target.files || []);
            event.target.value = "";
            uploadFiles(ctx, collection, files);
          },
        }),
      ),
    ),
    el(
      "p",
      { class: "drop-hint" },
      `Text files and PDFs, up to ${megabytes(rules.max_bytes)} each. A sealed copy of each file goes into the vault.`,
    ),
    state.items.length
      ? el(
          "ul",
          { class: "upload-list", "aria-label": "Files from this session" },
          state.items.map((item, index) =>
            el(
              "li",
              { class: ["upload", `upload-${item.status}`], key: `u${index}-${item.name}` },
              item.status === "sending" ? el("span", { class: "spinner" }) : icon(item.status === "done" ? "check" : item.status === "error" ? "warning" : "clock"),
              el(
                "span",
                { class: "upload-text" },
                el("span", { class: "upload-name", title: item.name }, item.name),
                el("span", { class: "upload-note" }, item.status === "sending" ? "Sealing…" : item.status === "waiting" ? "Waiting" : item.message),
              ),
            ),
          ),
          // Said once per list: where the file came from is not something the app is told.
          sealedCount(state)
            ? el("li", { class: "upload-outside", key: "outside" }, note("amber", leftOutsideText(sealedCount(state))))
            : null,
          !state.busy
            ? el(
                "li",
                { class: "upload-clear", key: "clear" },
                button("Clear this list", {
                  kind: "quiet",
                  small: true,
                  onclick: () => {
                    state.items = [];
                    ctx.render();
                  },
                }),
              )
            : null,
        )
      : null,
  );
}

// -- Documents ----------------------------------------------------------------------

function releasedText(released) {
  const parts = [];
  if (released.hosted > 0) parts.push(`${num(released.hosted)} to hosted apps`);
  if (released.local > 0) parts.push(`${num(released.local)} to local apps`);
  return parts.length ? parts.map((part) => el("span", { class: "released-part" }, part)) : "Nothing yet";
}

async function removeDocument(ctx, document) {
  const agreed = await confirmSheet({
    title: `Remove “${document.title}”?`,
    text: "Apps can no longer search or read it. What was released earlier stays in the record.",
    sentenceText: "Its files are deleted, not shredded; backups keep what they have.",
    confirmLabel: "Remove",
    danger: true,
  });
  if (!agreed) return;
  try {
    await api.removeDocument(document.id);
    toast(`Removed “${document.title}”.`, "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

async function setCollectionFlag(ctx, collection, key, wanted) {
  if (key === "allow_hosted" && wanted) {
    const agreed = await confirmSheet({
      title: `Open “${collection.name}” to hosted apps?`,
      text: "Nothing is given to any app yet: you still choose, app by app, under Apps.",
      sentenceText: `Text from “${collection.name}” may then be sent to the provider of any hosted app you give it to.`,
      confirmLabel: "Open to hosted apps",
      cautious: true,
    });
    if (!agreed) {
      ctx.render();
      return;
    }
  }
  try {
    await api.setCollection({ name: collection.name, [key]: wanted });
    if (key === "allow_hosted") {
      toast(wanted ? `“${collection.name}” can now be given to hosted apps.` : `Apps you declared hosted no longer get “${collection.name}”.`, "green");
    } else {
      toast(wanted ? `Apps now see the titles in “${collection.name}”.` : `Apps no longer see the titles in “${collection.name}”.`, "green");
    }
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

async function saveNote(ctx, collection) {
  const draft = ctx.ui.notes[collection.name];
  if (draft === undefined || draft.trim() === collection.note) {
    delete ctx.ui.notes[collection.name];
    ctx.render();
    return;
  }
  try {
    await api.setCollection({ name: collection.name, note: draft.trim() });
    toast("Note saved.", "green");
  } catch (error) {
    toastError(error);
  }
  delete ctx.ui.notes[collection.name];
  await ctx.refresh();
}

async function removeCollection(ctx, collection) {
  const agreed = await confirmSheet({
    title: `Remove the collection “${collection.name}”?`,
    text: "It is empty. Apps that were given it lose that access.",
    confirmLabel: "Remove",
    danger: true,
  });
  if (!agreed) return;
  try {
    await api.removeCollection(collection.name);
    toast(`Removed the collection “${collection.name}”.`, "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

function collectionCard(ctx, collection) {
  const name = collection.name;
  const draft = ctx.ui.notes[name];
  const documents = collection.documents;
  return el(
    "section",
    { class: "card collection", key: `c-${name}`, "aria-label": `Collection ${name}` },
    el(
      "header",
      { class: "collection-head" },
      el(
        "div",
        { class: "collection-name" },
        el("h2", { class: "card-title" }, name),
        el(
          "span",
          { class: "collection-meta" },
          documents.length ? `${count(documents.length, "document", "documents")} · ${num(collection.characters)} characters` : "Empty",
        ),
      ),
      el("input", {
        type: "text",
        class: "input input-quiet collection-note",
        value: draft === undefined ? collection.note : draft,
        placeholder: "Add a note that says what is inside",
        maxlength: 200,
        "aria-label": `Note for ${name}. Apps that are given this collection see it.`,
        title: "Apps that are given this collection see this note.",
        oninput: (event) => {
          ctx.ui.notes[name] = event.target.value;
        },
        onchange: () => saveNote(ctx, collection),
        onkeydown: (event) => {
          if (event.key === "Enter") event.target.blur();
        },
      }),
    ),
    el(
      "div",
      { class: "collection-switches" },
      toggle({
        id: `hosted-${name}`,
        label: "Hosted apps may be granted this collection",
        hint: "Needed before you give it to an app that sends text to a provider.",
        checked: collection.allow_hosted,
        onchange: (wanted) => setCollectionFlag(ctx, collection, "allow_hosted", wanted),
      }),
      toggle({
        id: `titles-${name}`,
        label: "Show titles to apps",
        hint: "Titles start out as file names. While off, apps see a document number only.",
        checked: collection.show_titles,
        onchange: (wanted) => setCollectionFlag(ctx, collection, "show_titles", wanted),
      }),
    ),
    documents.length
      ? el(
          "div",
          { class: "table-wrap" },
          el(
            "table",
            { class: "table doc-table" },
            el("caption", { class: "sr-only" }, `Documents in ${name}`),
            el(
              "thead",
              null,
              el(
                "tr",
                null,
                el("th", { scope: "col", class: "col-title" }, "Title"),
                el("th", { scope: "col", class: "num col-passages" }, "Passages"),
                el("th", { scope: "col", class: "num col-characters" }, "Characters"),
                el("th", { scope: "col", class: "col-added" }, "Added"),
                el("th", { scope: "col", class: "col-released" }, "Released so far"),
                el("th", { scope: "col", class: "col-action" }, el("span", { class: "sr-only" }, "Remove")),
              ),
            ),
            el(
              "tbody",
              null,
              documents.map((document) =>
                el(
                  "tr",
                  { key: document.id },
                  el(
                    "th",
                    { scope: "row", class: "doc-title" },
                    el("span", { class: "doc-name", title: document.title }, document.title),
                    el("span", { class: "doc-id mono" }, document.id),
                  ),
                  el("td", { class: "num" }, num(document.passages)),
                  el("td", { class: "num" }, num(document.characters)),
                  el("td", { class: "col-added", title: whenFull(document.added) }, dayShort(document.added, ctx.now)),
                  el("td", { class: ["col-released", document.released.hosted + document.released.local > 0 ? null : "muted"] }, releasedText(document.released)),
                  el(
                    "td",
                    { class: "col-action" },
                    iconButton("trash", `Remove ${document.title}`, { onclick: () => removeDocument(ctx, document) }),
                  ),
                ),
              ),
            ),
          ),
        )
      : null,
    dropZone(ctx, name),
    documents.length
      ? null
      : el(
          "div",
          { class: "collection-foot" },
          button("Remove this empty collection", { kind: "quiet", small: true, onclick: () => removeCollection(ctx, collection) }),
        ),
  );
}

export function documentsView(ctx) {
  const data = ctx.data.documents;
  const actions = [
    button("New collection", {
      glyph: "plus",
      onclick: async () => {
        const created = await openCollectionSheet();
        if (created) {
          toast(`Created the collection “${created}”.`, "green");
          await ctx.refresh();
        }
      },
    }),
  ];
  if (!data) return { title: "Documents", actions, body: loadingBlock() };
  if (!data.collections.length) {
    return {
      title: "Documents",
      actions,
      body: el(
        "section",
        { class: "card card-empty", key: "no-documents" },
        emptyState({
          glyph: "documents",
          title: "The vault is empty",
          text: "Add your first documents. They go into a collection named “documents”, which no app can read until you say so.",
        }),
        dropZone(ctx, "documents", { large: true }),
      ),
    };
  }
  return {
    title: "Documents",
    lead: "Collections are what you give to apps. Each document is cut into passages; an app reads one passage at a time.",
    actions,
    body: data.collections.map((collection) => collectionCard(ctx, collection)),
  };
}

// -- Apps ---------------------------------------------------------------------------

function kindsRegistered(apps) {
  return { hosted: apps.some((app) => app.hosted), local: apps.some((app) => !app.hosted) };
}

/**
 * The choice between apps that send text to a provider, apps that run on this Mac,
 * or both, when both kinds are registered. What is in force is shown as selected;
 * while a choice is still wanted, picking it again is the answer.
 */
export function servingStrip(ctx, apps) {
  const serving = ctx.state.serving;
  const kinds = kindsRegistered(apps);
  if (!serving || !(kinds.hosted && kinds.local)) return null;
  const needed = serving.choice_needed;
  const current = ["hosted", "local", "both"].includes(serving.kinds) ? serving.kinds : null;
  let title = "Answered in this session";
  let text = "You have apps that send text to a provider and apps that run on this Mac. The choice lasts until the vault locks.";
  if (needed) {
    title = "Choose which apps are answered";
    if (current === "hosted") title = "Apps that run on this Mac are not answered yet";
    if (current === "local") title = "Apps that send text to a provider are not answered yet";
    text = "You have apps that send text to a provider and apps that run here. Choose which may ask in this session.";
  } else if (current === "both") {
    text = BOTH_KINDS_SENTENCE;
  }
  return el(
    "section",
    { class: ["strip", needed && "strip-amber"], key: "serving", "aria-label": "Which apps are answered" },
    el("div", { class: "strip-text" }, el("h2", { class: "strip-title" }, title), el("p", null, text)),
    segmented({
      name: "serving-kinds",
      legend: "Which apps are answered",
      value: current,
      options: [
        { value: "hosted", label: "Provider apps only" },
        { value: "local", label: "Apps on this Mac only" },
        { value: "both", label: "Both" },
      ],
      onchange: (value) => chooseServing(ctx, value),
      onreselect: needed ? (value) => chooseServing(ctx, value) : null,
    }),
  );
}

async function setPaused(ctx, app, paused) {
  const names = new Set((ctx.state.serving && ctx.state.serving.paused) || []);
  if (paused) names.add(app.name);
  else names.delete(app.name);
  try {
    ctx.applyState(await api.setServing({ paused: Array.from(names) }));
    toast(paused ? `${app.name} is paused until you resume it or the vault locks.` : `${app.name} is answered again.`, "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

async function removeApp(ctx, app) {
  const agreed = await confirmSheet({
    title: `Remove ${app.name}?`,
    text: "It loses access to every collection. Text it already received stays where it was sent.",
    confirmLabel: "Remove",
    danger: true,
  });
  if (!agreed) return;
  try {
    await api.removeApp(app.name);
    toast(`Removed ${app.name}.`, "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

function grantRow(ctx, app, collection) {
  const current = app.grants[collection.name] || "none";
  const closed = app.hosted && !collection.allow_hosted;
  return el(
    "div",
    { class: "grant-row", key: `g-${collection.name}` },
    el("span", { class: "grant-name", title: collection.name }, collection.name),
    closed
      ? el(
          "span",
          { class: "grant-closed" },
          "Not open to hosted apps. ",
          el("a", { href: "#/documents", class: "link" }, "Open it under Documents"),
        )
      : segmented({
          name: `grant-${app.name}-${collection.name}`,
          legend: `What ${app.name} may do with ${collection.name}`,
          value: current,
          options: ["none", "read", "approve"].map((mode) => ({ value: mode, label: GRANT_LABEL[mode] })),
          onchange: (mode) => changeGrant(ctx, app, collection.name, mode),
        }),
  );
}

function appCard(ctx, app, collections, ceilings) {
  const seen = app.last_seen;
  // Not paused and still not answered: its kind is not answered in this session.
  const kindOff = !app.served && !app.paused;
  return el(
    "section",
    { class: ["card", "app-card", !app.served && "is-idle"], key: `app-${app.name}`, "aria-label": `App ${app.name}` },
    el(
      "header",
      { class: "app-head" },
      el(
        "div",
        { class: "app-id" },
        el(
          "div",
          { class: "app-name-line" },
          el("h2", { class: "app-name" }, app.name),
          chip(app.hosted ? "Hosted" : "Local", "outline"),
          destinationChip(app),
          app.paused ? chip("Paused", "amber", "pause") : !app.served ? chip("Not answered in this session", "quiet") : null,
        ),
        el(
          "p",
          { class: "app-seen" },
          seen && seen.time
            ? [
                "Last call ",
                el("time", { datetime: seen.time, title: whenFull(seen.time) }, ago(seen.time, ctx.now)),
                seen.claimed ? ` · says it is ${seen.claimed}, unverified` : "",
              ]
            : "Has not called yet",
        ),
      ),
      el(
        "div",
        { class: "app-actions" },
        toggle({
          id: `answering-${app.name}`,
          label: "Answering",
          compact: true,
          checked: app.served,
          disabled: kindOff,
          title: kindOff ? "Apps of this kind are not answered in this session. The choice is at the top of this page." : "Switch off to pause this app until you resume it or the vault locks.",
          onchange: (wanted) => setPaused(ctx, app, !wanted),
        }),
        button("Connect", { glyph: "link", small: true, onclick: () => openConnect(ctx, app.name, app.name) }),
        button("Limits", { glyph: "sliders", small: true, onclick: () => openAppLimits(ctx, app, ceilings) }),
        iconButton("trash", `Remove ${app.name}`, { onclick: () => removeApp(ctx, app) }),
      ),
    ),
    el(
      "div",
      { class: "app-body" },
      el(
        "div",
        { class: "app-grants" },
        el("h3", { class: "mini-title" }, "Collections"),
        collections.length
          ? collections.map((collection) => grantRow(ctx, app, collection))
          : el("p", { class: "muted" }, "There are no collections yet. ", el("a", { href: "#/documents", class: "link" }, "Add documents")),
      ),
      el(
        "div",
        { class: "app-usage" },
        el("h3", { class: "mini-title" }, "Released to this app"),
        meterRow("Last 24 hours", app.usage.chars_day, app.limits.chars_per_day),
        meterRow("Last 30 days", app.usage.chars_30_days, app.limits.chars_per_30_days),
        meterRow("Searches, last 24 hours", app.usage.searches_day, app.limits.searches_per_day, "searches"),
      ),
    ),
  );
}

function groupCard(title, group, hintText) {
  return el(
    "div",
    { class: "group" },
    el("h3", { class: "mini-title" }, title),
    meterRow("Last 24 hours", group.used_day, group.chars_per_day),
    meterRow("Last 30 days", group.used_30_days, group.chars_per_30_days),
    el("p", { class: "muted small" }, hintText),
  );
}

export function appsView(ctx) {
  const data = ctx.data.apps;
  const actions = [button("Add app", { kind: "primary", glyph: "plus", onclick: () => openAddApp(ctx), disabled: !data })];
  if (!data) return { title: "Apps", actions, body: loadingBlock() };
  const ceilings = ctx.data.settings ? ctx.data.settings.ceilings : null;
  if (!data.apps.length) {
    // The empty state carries the one action; a second button for it in the head would only compete.
    return {
      title: "Apps",
      body: el(
        "section",
        { class: "card card-empty", key: "no-apps" },
        emptyState({
          glyph: "apps",
          title: "No app has been added",
          text: "Add an app, such as Claude Desktop, choose what it may read, then connect it. Until then nothing can leave the vault.",
          action: button("Add app", { kind: "primary", glyph: "plus", onclick: () => openAddApp(ctx) }),
        }),
      ),
    };
  }
  const kinds = kindsRegistered(data.apps);
  return {
    title: "Apps",
    lead: `Names are labels, and “hosted” and “local” are what you declared: ${PRODUCT} cannot check who is really calling.`,
    actions,
    body: [
      servingStrip(ctx, data.apps),
      data.apps.map((app) => appCard(ctx, app, data.collections, ceilings)),
      card(
        "All apps together",
        el(
          "div",
          { class: "groups" },
          kinds.hosted ? groupCard("Hosted apps together", data.groups.hosted, "Every hosted app counts toward this, so a second name does not double what can leave.") : null,
          kinds.local ? groupCard("Local apps together", data.groups.local, "Counted apart from hosted apps.") : null,
        ),
        { key: "groups", action: el("a", { href: "#/vault", class: "link" }, "Change in Vault") },
      ),
    ],
  };
}

// -- Vault --------------------------------------------------------------------------

function fact(label, value, options = {}) {
  return el("div", { class: "fact", key: options.key }, el("dt", null, label), el("dd", { class: options.mono ? "mono" : null }, value));
}

const GROUP_FIELDS = [
  ["chars_per_day", "Characters in 24 hours", 0, 100000000],
  ["chars_per_30_days", "Characters in 30 days", 0, 100000000],
];

function limitsDraft(settings) {
  const draft = {};
  for (const [key] of LIMIT_FIELDS) draft[`defaults.${key}`] = num(settings.defaults[key]);
  for (const kind of ["hosted", "local"]) {
    for (const [key] of GROUP_FIELDS) draft[`groups.${kind}.${key}`] = num(settings.groups[kind][key]);
  }
  draft["per_document.max_chars"] = num(settings.per_document.max_chars);
  draft["per_document.max_percent"] = String(Math.round(settings.per_document.max_fraction * 100));
  draft["approval.ttl_minutes"] = String(settings.approval.ttl_minutes);
  return draft;
}

/** The fields of the limits form with their ranges: [draft key, label, min, max, suffix]. */
function limitsFields(settings) {
  const ceilings = settings.ceilings || {};
  const fields = [];
  for (const [key, label, min, max] of LIMIT_FIELDS) {
    fields.push({ group: "Each app, unless it has limits of its own", key: `defaults.${key}`, label, min, max: key === "chars_per_call" && ceilings.chars_per_call ? ceilings.chars_per_call : max });
  }
  for (const [kind, title] of [
    ["hosted", "Hosted apps together"],
    ["local", "Local apps together"],
  ]) {
    for (const [key, label, min, max] of GROUP_FIELDS) fields.push({ group: title, key: `groups.${kind}.${key}`, label, min, max });
  }
  fields.push({ group: "Each document, without your approval", key: "per_document.max_chars", label: "At most this many characters", min: 0, max: 4000000 });
  fields.push({ group: "Each document, without your approval", key: "per_document.max_percent", label: "And at most this share of it", min: 0, max: 100, suffix: "%" });
  fields.push({ group: "Approvals", key: "approval.ttl_minutes", label: "An approval stays valid for", min: 1, max: ceilings.ttl_minutes || 60, suffix: "minutes" });
  return fields;
}

function currentValue(settings, key) {
  if (key === "per_document.max_percent") return Math.round(settings.per_document.max_fraction * 100);
  return key.split(".").reduce((value, name) => value[name], settings);
}

async function saveLimits(ctx, settings, fields) {
  const edit = ctx.ui.limits;
  const body = {};
  for (const item of fields) {
    const value = readNumber(edit.draft[item.key], item.min, item.max);
    if (value === null) return;
    if (value === currentValue(settings, item.key)) continue;
    const path = item.key.split(".");
    if (item.key === "per_document.max_percent") {
      body.per_document = body.per_document || {};
      body.per_document.max_fraction = value / 100;
    } else if (path.length === 2) {
      body[path[0]] = body[path[0]] || {};
      body[path[0]][path[1]] = value;
    } else {
      body.groups = body.groups || {};
      body.groups[path[1]] = body.groups[path[1]] || {};
      body.groups[path[1]][path[2]] = value;
    }
  }
  if (!Object.keys(body).length) {
    ctx.ui.limits = null;
    ctx.render();
    return;
  }
  edit.busy = true;
  edit.error = "";
  ctx.render();
  try {
    ctx.data.settings = await api.setSettings(body);
    ctx.ui.limits = null;
    ctx.ui.focusId = "limits-edit";
    toast("Limits changed.", "green");
  } catch (error) {
    edit.busy = false;
    edit.error = error.code === "cancelled" ? "" : error.message;
  }
  await ctx.refresh();
}

function limitsCard(ctx, settings) {
  const edit = ctx.ui.limits;
  const fields = limitsFields(settings);
  const groups = [];
  for (const item of fields) {
    let group = groups.find((entry) => entry.title === item.group);
    if (!group) {
      group = { title: item.group, items: [] };
      groups.push(group);
    }
    group.items.push(item);
  }
  if (!edit) {
    return card(
      "Limits",
      [
        el(
          "div",
          { class: "limit-groups" },
          groups.map((group) =>
            el(
              "div",
              { class: "limit-group", key: group.title },
              el("h3", { class: "mini-title" }, group.title),
              el(
                "dl",
                { class: "limit-rows" },
                group.items.map((item) =>
                  el(
                    "div",
                    { class: "limit-row", key: item.key },
                    el("dt", { class: "limit-label" }, item.label),
                    el("dd", { class: "limit-value" }, `${num(currentValue(settings, item.key))}${item.suffix ? (item.suffix === "%" ? "%" : ` ${item.suffix}`) : ""}`),
                  ),
                ),
              ),
            ),
          ),
        ),
        el(
          "p",
          { class: "muted small" },
          "A document's allowance without approval is the smaller of the two figures, counted over all apps of a kind, and it never refills. Limits hold for callers that use the tools, while the record is intact.",
        ),
      ],
      {
        key: "limits",
        class: "span-2",
        action: el(
          "button",
          {
            type: "button",
            class: "btn btn-default btn-small",
            id: "limits-edit",
            onclick: () => {
              ctx.ui.limits = { draft: limitsDraft(settings), busy: false, error: "" };
              ctx.ui.focusId = "set-defaults-chars_per_day";
              ctx.render();
            },
          },
          el("span", { class: "btn-label" }, "Edit"),
        ),
      },
    );
  }
  const valid = fields.every((item) => readNumber(edit.draft[item.key], item.min, item.max) !== null);
  return card(
    "Limits",
    el(
      "form",
      {
        class: "limits-form",
        autocomplete: "off",
        onsubmit: (event) => {
          event.preventDefault();
          if (valid && !edit.busy) saveLimits(ctx, settings, fields);
        },
      },
      el(
        "div",
        { class: "limit-groups" },
        groups.map((group) =>
          el(
            "div",
            { class: "limit-group", key: group.title, role: "group", "aria-label": group.title },
            el("h3", { class: "mini-title" }, group.title),
            el(
              "div",
              { class: "limit-rows" },
              group.items.map((item) =>
                numberRow({
                  id: `set-${item.key.replace(/\./g, "-")}`,
                  label: item.label,
                  value: edit.draft[item.key],
                  min: item.min,
                  max: item.max,
                  suffix: item.suffix,
                  oninput: (event) => {
                    edit.draft[item.key] = event.target.value;
                    ctx.render();
                  },
                }),
              ),
            ),
          ),
        ),
      ),
      edit.error ? note("red", edit.error) : null,
      el(
        "div",
        { class: "form-actions" },
        el("span", { class: "muted small" }, "Raising a limit needs your passphrase when you last typed it more than 10 minutes ago."),
        button("Cancel", {
          onclick: () => {
            ctx.ui.limits = null;
            ctx.ui.focusId = "limits-edit";
            ctx.render();
          },
        }),
        button(edit.busy ? "Saving…" : "Save limits", { kind: "primary", type: "submit", disabled: !valid, busy: edit.busy }),
      ),
    ),
    { key: "limits", class: "span-2" },
  );
}

async function addWord(ctx) {
  const form = ctx.ui.word;
  const text = form.text.trim();
  if (text.length < 2 || form.busy) return;
  form.busy = true;
  form.error = "";
  ctx.render();
  try {
    const answer = await api.addReplacedString(text);
    form.text = "";
    if (ctx.data.settings) ctx.data.settings.replaced_strings = answer.replaced_strings;
    toast(`Added. ${count(answer.replaced_strings, "word or number is", "words or numbers are")} replaced now.`, "green");
  } catch (error) {
    form.error = error.code === "cancelled" ? "" : error.message;
  }
  form.busy = false;
  ctx.render();
  // Ask again, so that an answer that was already on its way cannot bring back the old count.
  await ctx.refresh();
}

const CANNOT = [
  "Take text back. What an app received is with that app and, for a hosted app, with its provider; the app keeps its own copy too.",
  "Tell who is calling. App names are labels: any program running as you can call under a name that is answered.",
  "Notice instructions hidden in a document. Limits bound what can be lost; one allowed passage is enough to leak that passage.",
  "Keep a whole document in. Search answers show which passages contain the words asked for, and passages add up, one by one.",
  "Replace more than the words you listed. Other forms of the same name or number pass.",
  "Protect your passphrase from other programs running as you, or bring back a lost one.",
  "Prove anything with its record. It is a log of what was released; whoever can delete files can roll it back.",
  "Shred. Removing a document deletes files, and backups keep what they have.",
];

export function vaultView(ctx) {
  const settings = ctx.data.settings;
  const state = ctx.state;
  const facts = state.encryption || {};
  const record = state.record || {};
  const form = ctx.ui.word;
  if (!settings) return { title: "Vault", body: loadingBlock() };
  const ceiling = settings.ceilings && settings.ceilings.replaced_strings;
  return {
    title: "Vault",
    body: el(
      "div",
      { class: "grid-2", key: "vault" },
      card(
        "Location and size",
        el(
          "dl",
          { class: "facts" },
          fact("Location", pathText(state.store.path, !state.demo)),
          fact("Sealed files", `${num(state.store.sealed_files)} · ${bytes(state.store.sealed_bytes)}`),
          fact("Inside", `${count(facts.documents, "document", "documents")} in ${count(facts.collections, "collection", "collections")} · ${num(facts.characters)} characters`),
          fact("Copies of the files you added", facts.originals_kept > 0 ? `${num(facts.originals_kept)}, sealed like the text` : "None"),
          fact("Last entry of the record", `#${record.head_seq} ${record.head_hash}`, { mono: true }),
          fact("Kept beside the vault, not sealed", "Which view you use, and whether setup was finished"),
        ),
        { key: "location" },
      ),
      card(
        "Passphrase and key",
        [
          el(
            "dl",
            { class: "facts" },
            fact("Files are sealed with", el("span", { class: "nowrap" }, facts.cipher || "–")),
            fact("The key comes from", `your passphrase, with ${facts.kdf || "scrypt"}`),
            fact("Unlocked", when(state.unlocked_at, ctx.now)),
          ),
          note("amber", "Losing the passphrase loses the vault. There is no recovery."),
          el(
            "p",
            { class: "muted small" },
            "The passphrase is never stored. Other programs running as you can capture it the next time you type it.",
          ),
        ],
        { key: "passphrase" },
      ),
      limitsCard(ctx, settings),
      card(
        "Words to replace",
        el(
          "div",
          { class: "words" },
          el(
            "form",
            {
              class: "word-form",
              autocomplete: "off",
              onsubmit: (event) => {
                event.preventDefault();
                addWord(ctx);
              },
            },
            el("label", { class: "field-label", for: "replace-word" }, "Add a word or number that must never leave"),
            el(
              "div",
              { class: "inline-form" },
              el("input", {
                type: "text",
                id: "replace-word",
                class: "input",
                value: form.text,
                maxlength: 200,
                autocomplete: "off",
                spellcheck: "false",
                placeholder: "A name, an account number…",
                oninput: (event) => {
                  form.text = event.target.value;
                  ctx.render();
                },
              }),
              button(form.busy ? "Adding…" : "Add", { type: "submit", disabled: form.text.trim().length < 2 || (ceiling && settings.replaced_strings >= ceiling), busy: form.busy }),
            ),
            form.error ? el("p", { class: "field-error", role: "alert" }, form.error) : null,
          ),
          el(
            "div",
            { class: "words-text" },
            el(
              "p",
              { class: "word-count" },
              el("strong", null, num(settings.replaced_strings)),
              settings.replaced_strings === 1 ? " word or number is replaced with " : " words or numbers are replaced with ",
              el("span", { class: "mono" }, "[REDACTED]"),
              " in everything that leaves",
              ceiling ? ` (${num(ceiling)} at most).` : ".",
              " They are sealed with the vault and never shown again.",
            ),
            el(
              "p",
              { class: "muted small" },
              "Other forms (inflected, abbreviated, reversed order, split across lines) are different strings. Add each one you want replaced.",
            ),
          ),
        ),
        { key: "words", class: "span-2" },
      ),
      card(
        `What ${PRODUCT} cannot do`,
        el("ul", { class: "cannot" }, CANNOT.map((text, index) => el("li", { key: `cannot-${index}` }, text))),
        { key: "cannot", class: "span-2" },
      ),
      el(
        "p",
        { class: "version span-2", key: "version" },
        `${PRODUCT} ${state.version} · a preview: it has not yet been tried against the agent applications it lists, and it does not run on Windows.`,
      ),
    ),
  };
}
