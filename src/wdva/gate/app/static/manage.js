// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The screens where the owner changes things: Documents, Apps and Vault. The zone that
// takes files and its lines, the sheet that asks before a document is removed and the
// eight statements of what the product cannot do are the same on every page, and take
// their texts from the text table.

import { api } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { ago, bytes, count, dayShort, megabytes, num, when, whenFull } from "./format.js";
import {
  button,
  card,
  chip,
  confirmSheet,
  copyLine,
  destinationChip,
  emptyState,
  errorText,
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
import { t } from "./texts.js";

export function loadingBlock() {
  return el("div", { class: "loading", role: "status" }, el("span", { class: "spinner" }), t("common.status.loading"));
}

// -- adding files -------------------------------------------------------------------

function uploadState(ctx, collection) {
  // A collection may have any name, also one that every object answers to ("constructor").
  if (!Object.prototype.hasOwnProperty.call(ctx.ui.uploads, collection)) ctx.ui.uploads[collection] = { items: [], over: 0, busy: false };
  return ctx.ui.uploads[collection];
}

function extension(name) {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot).toLowerCase();
}

/**
 * Why a file could not be read: the word the app sends beside its English sentence,
 * and the key of the line that says the same in the language of the page.
 */
const REFUSED = {
  unsupported: "docs.error.type",
  empty: "docs.error.empty",
  no_text: "docs.error.notext",
  not_utf8: "docs.error.encoding",
  no_pdf_support: "docs.error.pdf",
  pdf_encrypted: "docs.error.password",
  pdf_no_text: "docs.error.scan",
  pdf_unreadable: "docs.error.unreadable",
  pdf_too_much: "docs.error.toomuch",
  pdf_too_slow: "docs.error.slow",
};

/**
 * Why the app did not take a file, as the line of that file. The page has words for a
 * document that is there already and for every reason the app names by a word. Where
 * the app sent no word, or one this page does not know, the line holds the app's own
 * sentence, in English: also for the code "too large", which speaks of the text in the
 * file and not of its size, a size the page has checked before it sent the file.
 */
function refusedWords(error, rules) {
  if (error.code === "duplicate") return () => t("docs.error.duplicate");
  const reason = error.code === "invalid" && typeof error.reason === "string" ? error.reason : "";
  if (reason === "too_large") return () => t("docs.error.large", { size: megabytes(rules.max_bytes) });
  if (Object.prototype.hasOwnProperty.call(REFUSED, reason)) return () => t(REFUSED[reason]);
  return () => t("docs.error.other", { reason: errorText(error) });
}

/** Whether a file was refused because this installation cannot read PDFs: the line then comes with the command that adds that. */
function needsPdfSupport(error) {
  return error.code === "invalid" && error.reason === "no_pdf_support";
}

/**
 * Once a document was sealed, the lines of the files that were refused before it go:
 * the owner has gone on, and a red line under a card that is marked done reads as a
 * fault that is still there. `before` are the lines that stood there before the files
 * now sent; a line that comes with the command which adds PDF reading stays.
 */
function dropRefused(state, before) {
  state.items = state.items.filter((item) => !before.includes(item) || item.status !== "error" || item.install === true);
}

/** The same for a document that was added without a file of the owner's: the sample. */
export function forgetRefused(ctx, collection) {
  const state = uploadState(ctx, collection);
  dropRefused(state, state.items);
}

/**
 * Send files to the vault one after the other and keep a line per file. What a line
 * says is kept as a function, so that it is said in the language the page shows when
 * it is drawn. A file that was not added has its reason in its line, so the message
 * at the foot of the window only says how many were added.
 */
export async function uploadFiles(ctx, collection, files) {
  const rules = (ctx.data.documents && ctx.data.documents.upload) || { max_bytes: 20971520, types: [], pdf: true };
  const state = uploadState(ctx, collection);
  const batch = Array.from(files).map((file) => ({ file, name: file.name, collection, status: "waiting", words: null }));
  if (!batch.length) return;
  const before = state.items;
  state.items = state.items.concat(batch);
  state.busy = true;
  ctx.render();
  let added = 0;
  for (const item of batch) {
    const kind = extension(item.name);
    const refuse = (words) => {
      item.status = "error";
      item.words = words;
    };
    if (rules.types.length && !rules.types.includes(kind)) {
      refuse(() => t("docs.error.type"));
    } else if (kind === ".pdf" && rules.pdf === false) {
      refuse(() => t("docs.error.pdf"));
      item.install = true;
    } else if (item.file.size > rules.max_bytes) {
      refuse(() => t("docs.error.large", { size: megabytes(rules.max_bytes) }));
    } else if (item.file.size === 0) {
      refuse(() => t("docs.error.empty"));
    } else {
      item.status = "sending";
      ctx.render();
      try {
        const answer = await api.addDocument(collection, item.name, item.file);
        const passages = (answer.document || {}).passages;
        item.status = "done";
        item.words = () => t("docs.result.sealed", { n: passages });
        added += 1;
        dropRefused(state, before);
      } catch (error) {
        refuse(refusedWords(error, rules));
        item.install = needsPdfSupport(error);
        if (error.status === 401 || error.code === "locked") break;
      }
    }
    item.file = null;
    ctx.render();
  }
  state.busy = false;
  const failed = batch.length - added;
  if (added && !failed) toast(t("docs.result.added", { n: added }), "green");
  else if (added) toast(t("docs.result.mixed", { added, failed }), "amber");
  else toast(t("docs.result.noneadded"), "red");
  await ctx.refresh();
}

function sealedCount(state) {
  return state.items.filter((item) => item.status === "done").length;
}

/** Where in a list of lines the last file stands that was sealed; -1 when none was. */
export function lastSealed(items) {
  return items.map((item) => item.status).lastIndexOf("done");
}

/** The lines of the files that were chosen for one collection since this page was loaded. */
export function sessionFiles(ctx, collection) {
  return uploadState(ctx, collection).items;
}

/**
 * The line of one file that was chosen: its name and what became of it. `group` is the
 * group it went to, said where the vault has several.
 */
export function fileLine(item, key, group = null) {
  return el(
    "li",
    { class: ["upload", `upload-${item.status}`], key },
    item.status === "sending" ? el("span", { class: "spinner" }) : icon(item.status === "done" ? "check" : item.status === "error" ? "warning" : "clock"),
    el(
      "span",
      { class: "upload-text" },
      el("span", { class: "upload-name", title: item.name }, item.name),
      group ? el("span", { class: "upload-group" }, group) : null,
      el("span", { class: "upload-note" }, item.status === "sending" ? t("docs.status.sealing") : item.words ? item.words() : t("docs.status.waiting")),
    ),
  );
}

/**
 * What is true of the files that were just sealed into one collection: which apps can ask
 * for them from now on. Adding a document never asks for the passphrase, so this is said
 * where the files are added, and it is never left out. Names are the names the apps have
 * in the vault.
 */
export function whoLine(ctx, collection) {
  const data = ctx.data.apps;
  if (!data) return null;
  // By the two words of an access and nothing else: a collection may be named like something every object has.
  const names = data.apps.filter((app) => ["approve", "read"].includes(app.grants[collection])).map((app) => app.name);
  return el(
    "li",
    { class: "upload-said", key: `who-${collection}` },
    el("p", { class: "upload-who", role: "status" }, names.length ? t("docs.result.who", { apps: names }) : t("docs.result.nobody")),
  );
}

/**
 * The place to drop files, with a button for people who would rather pick them.
 *
 * `options.large` is the zone of an empty vault, and with `options.filled` its button
 * is the filled one of its screen. `options.who` says, under the files that were
 * sealed, which apps can ask for them from now on. `options.said` is for a page that
 * itself says, above the zone, that the file which was added stays where it was, not
 * encrypted: the zone then does not say it a second time. `options.take(files)`
 * is handed the files in place of sending them to `collection`: for a vault with
 * several groups, where the owner is asked first which group they go to. `collection`
 * is then only the name of the zone, and `options.lists` names the collections whose
 * files it lists.
 */
export function dropZone(ctx, collection, options = {}) {
  const held = typeof options.take === "function";
  // The name of a zone that only holds files is not the name of a collection: its own state is kept apart.
  const zone = uploadState(ctx, held ? `(${collection})` : collection);
  const lists = (options.lists || [collection]).map((name) => ({ name, list: uploadState(ctx, name) }));
  const state = { items: [].concat(...lists.map((entry) => entry.list.items)), busy: lists.some((entry) => entry.list.busy) };
  const rules = (ctx.data.documents && ctx.data.documents.upload) || { types: [], max_bytes: 20971520 };
  // The command that adds what reads PDFs, shown once under the files where one was refused for the lack of it.
  const install = state.items.some((item) => item.install) && typeof rules.install === "string" ? rules.install : "";
  const inputId = `files-${collection}`;
  const hasFiles = (event) => event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files");
  const accept = (files) => (held ? options.take(Array.from(files)) : uploadFiles(ctx, collection, files));
  let index = 0;
  return el(
    "div",
    {
      class: ["drop", zone.over > 0 && "is-over", options.large && "drop-large"],
      key: `drop-${collection}`,
      "data-collection": held ? null : collection,
      ondragenter: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        zone.over += 1;
        ctx.render();
      },
      ondragover: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      },
      ondragleave: () => {
        zone.over = Math.max(0, zone.over - 1);
        ctx.render();
      },
      ondrop: (event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.stopPropagation();
        zone.over = 0;
        accept(event.dataTransfer.files);
      },
    },
    el(
      "div",
      { class: "drop-line" },
      icon("upload"),
      el("span", { class: "drop-text" }, t(zone.over > 0 ? "docs.label.dropnow" : "docs.label.drop")),
      el(
        "label",
        { class: ["btn", options.filled ? "btn-primary" : "btn-default", "btn-small", "file-button"], for: inputId },
        el("span", { class: "btn-label" }, t("docs.button.add")),
        el("input", {
          type: "file",
          id: inputId,
          class: "sr-only",
          multiple: true,
          accept: rules.types.join(","),
          "aria-label": t("docs.button.add"),
          onchange: (event) => {
            const files = Array.from(event.target.files || []);
            event.target.value = "";
            accept(files);
          },
        }),
      ),
    ),
    // What can be read stands where the choice is made: the file dialog greys the rest out without a word.
    el("p", { class: "drop-hint" }, t("docs.status.types", { size: megabytes(rules.max_bytes) })),
    state.items.length
      ? el(
          "ul",
          { class: "upload-list" },
          lists.map((entry) => {
            const lines = entry.list.items.map((item) => {
              index += 1;
              // With several groups each file says which one it went to.
              return fileLine(item, `u${index - 1}-${item.name}`, lists.length > 1 ? entry.name : null);
            });
            // Said for the files of each collection, since each has its own apps: under the last
            // file that was sealed, and so never under one that was refused after it.
            const who = options.who && sealedCount(entry.list) ? whoLine(ctx, entry.name) : null;
            if (who) lines.splice(lastSealed(entry.list.items) + 1, 0, who);
            return lines;
          }),
          install
            ? el(
                "li",
                { class: "upload-install", key: "install" },
                el("span", { class: "upload-install-label" }, t("docs.label.install")),
                copyLine(ctx, `install-copy-${collection}`, install, t("docs.label.install")),
              )
            : null,
          // Said once per list: a sealed copy is in the vault, and the file that was chosen is
          // still where it was, not encrypted. Where it lies is not something the app is told.
          sealedCount(state) && !options.said ? el("li", { class: "upload-outside", key: "outside" }, note("amber", t("docs.say.1"))) : null,
          !state.busy
            ? el(
                "li",
                { class: "upload-clear", key: "clear" },
                button(t("docs.button.clear"), {
                  kind: "quiet",
                  small: true,
                  onclick: () => {
                    for (const entry of lists) entry.list.items = [];
                    ctx.render();
                  },
                }),
              )
            : null,
        )
      : null,
  );
}

// -- the invented sample document ------------------------------------------------------

/**
 * A rental agreement that says of itself that it is invented, in its title and in its
 * first paragraph, for a person who has no file at hand: one in each language of the
 * page. Each has five paragraphs with one empty line between them, which the vault
 * takes as three passages. The page holds them as text and adds one only when its
 * button is pressed. They are documents, not texts of the interface, so they stand
 * here and not in the text tables.
 */
export const SAMPLE = {
  en: {
    file: "Sample rental agreement (invented).txt",
    title: "Sample rental agreement (invented)",
    text: [
      "Rental agreement for the flat at 14 Example Lane, second floor, with one cellar room. This is a sample document and it is invented: the landlord, Example Homes Ltd, and the tenant, Sam Sample, do not exist. It was added to try out the vault, and you can remove it at any time under Documents.",
      "Rent. The monthly rent is 950 EUR. It is due on the third working day of each month and is paid by standing order to the landlord's account. Heating and water are not part of the rent; the tenant pays 120 EUR a month for them in advance, and the difference is settled once a year, in June.",
      "Deposit. The tenant pays a deposit of 1,900 EUR before the keys are handed over. The landlord keeps the deposit in a separate account and returns it within 30 days after the flat is given back, less the cost of any damage that goes beyond normal wear. No interest is paid on the deposit.",
      "Term. The rental agreement starts on 1 March 2025. The rental agreement will end on 28 February 2027 without notice from either side. If both sides want to go on after that day, they sign a new rental agreement no later than two months before it, and the rent may then be set anew.",
      "Notice and house rules. Either side may give three months' notice in writing, to the end of a calendar month. Quiet hours are from 10 pm to 6 am. Pets need the landlord's consent in writing. Smoking is not allowed on the stairs or in the cellar, and bicycles are kept in the yard.",
    ].join("\n\n"),
  },
  pl: {
    file: "Umowa najmu (zmyślony przykład).txt",
    title: "Umowa najmu (zmyślony przykład)",
    text: [
      "Umowa najmu mieszkania przy ulicy Przykładowej 14, drugie piętro, z jedną komórką w piwnicy. To jest przykładowy dokument i jest zmyślony: wynajmujący, spółka Przykładowe Domy, i najemca, Jan Przykładowy, nie istnieją. Dodano go, żeby wypróbować sejf, i w każdej chwili możesz go usunąć w Dokumentach.",
      "Czynsz. Czynsz wynosi 2800 zł miesięcznie. Jest płatny do trzeciego dnia roboczego każdego miesiąca zleceniem stałym na rachunek wynajmującego. Ogrzewanie i woda nie wchodzą w skład czynszu; najemca płaci za nie zaliczkę 350 zł miesięcznie, a różnica jest rozliczana raz w roku, w czerwcu.",
      "Kaucja. Najemca wpłaca kaucję w wysokości 5600 zł przed wydaniem kluczy. Wynajmujący trzyma kaucję na osobnym rachunku i zwraca ją w ciągu 30 dni od oddania mieszkania, po potrąceniu kosztów szkód wykraczających poza zwykłe zużycie. Kaucja nie jest oprocentowana i nie zastępuje ostatniego czynszu.",
      "Okres najmu. Umowa najmu zaczyna się 1 marca 2025. Umowa najmu kończy się 28 lutego 2027 bez wypowiedzenia przez którąkolwiek ze stron. Jeśli obie strony chcą ją kontynuować po tym dniu, podpisują nową umowę najpóźniej dwa miesiące przed nim, a wysokość czynszu można wtedy ustalić na nowo.",
      "Wypowiedzenie i porządek domowy. Każda ze stron może wypowiedzieć umowę na piśmie z trzymiesięcznym okresem wypowiedzenia, na koniec miesiąca kalendarzowego. Cisza nocna trwa od 22 do 6. Na zwierzęta potrzebna jest pisemna zgoda wynajmującego. Na klatce schodowej i w piwnicy nie wolno palić, a rowery stoją na podwórzu.",
    ].join("\n\n"),
  },
};

/**
 * Which of the two samples a document of the vault is, by its title and its number of
 * characters together: "en", "pl", or null for a document that is neither.
 */
export function sampleOf(document) {
  for (const code of Object.keys(SAMPLE)) {
    if (document.title === SAMPLE[code].title && document.characters === SAMPLE[code].text.length) return code;
  }
  return null;
}

/** Whether a document of the vault is one of the two samples. */
export function isSample(document) {
  return sampleOf(document) !== null;
}

// -- Documents ----------------------------------------------------------------------

function releasedText(released) {
  const parts = [];
  if (released.hosted > 0) parts.push(`${num(released.hosted)} to hosted apps`);
  if (released.local > 0) parts.push(`${num(released.local)} to local apps`);
  return parts.length ? parts.map((part) => el("span", { class: "released-part" }, part)) : "Nothing yet";
}

export async function removeDocument(ctx, document) {
  const agreed = await confirmSheet({
    title: t("remove.title", { title: document.title }),
    text: t("remove.say.1"),
    sentenceText: t("remove.say.2"),
    confirmLabel: t("common.button.remove"),
    cancelLabel: t("common.button.cancel"),
    danger: true,
  });
  if (!agreed) return;
  try {
    await api.removeDocument(document.id);
    toast(t("remove.result.done", { title: document.title }), "green");
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

/** Remove one app after asking. Resolves to whether it was removed. */
export async function removeApp(ctx, app) {
  const agreed = await confirmSheet({
    title: `Remove ${app.name}?`,
    text: "It loses access to every collection. Text it already received stays where it was sent.",
    confirmLabel: "Remove",
    danger: true,
  });
  if (!agreed) return false;
  let removed = false;
  try {
    await api.removeApp(app.name);
    removed = true;
    toast(`Removed ${app.name}.`, "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
  return removed;
}

function grantRow(ctx, app, collection) {
  // By the entry of the group itself: a group may be named like something every object has ("constructor").
  const current = Object.prototype.hasOwnProperty.call(app.grants, collection.name) ? app.grants[collection.name] : "none";
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
          el("a", { href: "#/pro/documents", class: "link" }, "Open it under Documents"),
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
          : el("p", { class: "muted" }, "There are no collections yet. ", el("a", { href: "#/pro/documents", class: "link" }, "Add documents")),
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
        { key: "groups", action: el("a", { href: "#/pro/vault", class: "link" }, "Change in Vault") },
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

/**
 * What the product is not, in eight statements: the keys of their texts. Every page
 * that shows the statements takes them from this one list, through `cannotList`.
 */
export const CANNOT = [
  "how.cannot.1",
  "how.cannot.2",
  "how.cannot.3",
  "how.cannot.4",
  "how.cannot.5",
  "how.cannot.6",
  "how.cannot.7",
  "how.cannot.8",
];

/** The eight statements as a list, in the language the page shows. */
export function cannotList() {
  return el("ul", { class: "cannot" }, CANNOT.map((key) => el("li", { key }, t(key))));
}

/** The line that says which version this is, and that it is a preview. */
export function versionLine(state) {
  return t("how.version", { version: state.version });
}

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
          fact("Kept beside the vault, not sealed", "Your language, the page it opens on, and whether the first steps are done"),
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
        cannotList(),
        { key: "cannot", class: "span-2" },
      ),
      el("p", { class: "version span-2", key: "version" }, versionLine(state)),
    ),
  };
}
