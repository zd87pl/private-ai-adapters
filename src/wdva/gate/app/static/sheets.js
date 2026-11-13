// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The sheets that ask the owner for a decision: the passphrase again, a grant,
// a new app, the configuration that connects it, limits, which kinds of apps
// are answered.

import { api } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { num } from "./format.js";
import {
  button,
  confirmSheet,
  copyText,
  demoPassphraseNote,
  destinationChip,
  field,
  note,
  openSheet,
  pathText,
  sheetFoot,
  sheetHead,
  toast,
  toastError,
} from "./parts.js";

export const GRANT_LABEL = { none: "No access", read: "Can read", approve: "Ask me each time" };

export const BOTH_KINDS_SENTENCE =
  "While both kinds are answered, any program running as you, including a hosted agent that can run " +
  `commands, can call ${PRODUCT} under a local app's name.`;

/** The sentence shown wherever an app is given a collection. */
export function grantSentence(app, collection) {
  if (app.hosted) {
    return `Text this app receives from "${collection}" is sent to ${app.destination} and cannot be recalled.`;
  }
  return (
    `${PRODUCT} cannot check that this app is local. If it uses a cloud model, or has tools that send ` +
    `data out, text it receives from "${collection}" leaves this Mac.`
  );
}

// -- the passphrase again -----------------------------------------------------------

let reauthOpen = null;

/**
 * Ask for the passphrase. Resolves to true once it was accepted, false when the owner gave up.
 * `demoPassphrase` is the passphrase of the demo vault when this is the demo: the sheet then
 * shows it, because nobody could answer otherwise.
 */
export function requestReauth(demoPassphrase = null) {
  if (reauthOpen) return reauthOpen;
  const form = { passphrase: "", busy: false, error: "" };
  const submit = async (event, sheet) => {
    event.preventDefault();
    if (form.busy || !form.passphrase) return;
    form.busy = true;
    form.error = "";
    sheet.update();
    try {
      await api.reauth(form.passphrase);
      form.passphrase = "";
      sheet.close(true);
    } catch (error) {
      form.busy = false;
      form.passphrase = "";
      form.error = error.code === "wrong_passphrase" ? "That passphrase is not right." : error.message;
      sheet.update();
      const input = document.getElementById("reauth-passphrase");
      if (input) input.focus();
    }
  };
  const sheet = openSheet(
    (self) =>
      el(
        "form",
        { class: "sheet-content", onsubmit: (event) => submit(event, self), autocomplete: "off" },
        el("div", { class: "sheet-glyph" }, icon("key")),
        sheetHead(
          "Enter your passphrase again",
          "A change that lets more text leave the vault needs your passphrase when you last typed it more than 10 minutes ago.",
        ),
        demoPassphraseNote(demoPassphrase, () => {
          form.passphrase = demoPassphrase;
          form.error = "";
          self.update();
          const input = document.getElementById("reauth-passphrase");
          if (input) input.focus();
        }),
        field({
          label: "Passphrase",
          id: "reauth-passphrase",
          error: form.error,
          control: el("input", {
            type: "password",
            id: "reauth-passphrase",
            class: "input",
            value: form.passphrase,
            autocomplete: "current-password",
            autofocus: true,
            required: true,
            "aria-invalid": form.error ? "true" : null,
            "aria-describedby": form.error ? "reauth-passphrase-error" : null,
            oninput: (event) => {
              form.passphrase = event.target.value;
            },
          }),
        }),
        sheetFoot(
          button("Cancel", { onclick: () => self.close(false) }),
          button(form.busy ? "Checking…" : "Continue", { kind: "primary", type: "submit", busy: form.busy }),
        ),
      ),
    { label: "Enter your passphrase again" },
  );
  reauthOpen = sheet.closed.then((value) => {
    reauthOpen = null;
    return value === true;
  });
  return reauthOpen;
}

// -- grants -------------------------------------------------------------------------

const MODE_TEXT = {
  read:
    "It can search this collection, sees short snippets, and reads passages within the limits. Once " +
    "a document's free allowance is used up, each further passage needs your approval.",
  approve:
    "It can search this collection and learns which passages contain its words, without their text. " +
    "Every passage needs your approval before it is released.",
};

/** Change what one app may do with one collection, asking first when it widens access. */
export async function changeGrant(ctx, app, collection, mode) {
  const current = app.grants[collection] || "none";
  if (mode === current) return;
  const widens = mode === "read" || (mode === "approve" && current === "none");
  if (widens) {
    const agreed = await confirmSheet({
      title: mode === "read" ? `Let ${app.name} read “${collection}”?` : `Let ${app.name} ask for passages of “${collection}”?`,
      text: MODE_TEXT[mode],
      sentenceText: grantSentence(app, collection),
      confirmLabel: "Allow",
      cautious: true,
    });
    if (!agreed) return;
  }
  try {
    await api.setGrant(app.name, collection, mode);
    const said = {
      none: `${app.name} no longer has access to “${collection}”.`,
      read: `${app.name} can now read “${collection}”.`,
      approve: `${app.name} now needs your approval for each passage of “${collection}”.`,
    };
    toast(said[mode], "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

// -- which kinds of apps are answered -----------------------------------------------

export async function chooseServing(ctx, kinds) {
  if (kinds === "both") {
    const agreed = await confirmSheet({
      title: "Answer both kinds of apps together?",
      text: "Apps that send text to a provider and apps that run on this Mac may then both ask, until the vault locks.",
      sentenceText: BOTH_KINDS_SENTENCE,
      confirmLabel: "Answer both",
      cautious: true,
    });
    if (!agreed) return;
  }
  try {
    ctx.applyState(await api.setServing({ kinds }));
    const words = {
      hosted: "Only apps that send text to a provider are answered now.",
      local: "Only apps that run on this Mac are answered now.",
      both: "Both kinds of apps are answered now.",
    };
    toast(words[kinds], "green");
  } catch (error) {
    toastError(error);
  }
  await ctx.refresh();
}

// -- a new app ----------------------------------------------------------------------

const NAME_PATTERN = /^[a-z][a-z0-9-]{0,31}$/;

export function newAppForm(known) {
  const first = known.find((entry) => entry.local_program) || { id: "generic", name: "", hosted: true, destination: "" };
  return { choice: first.id, name: first.name, hosted: first.hosted, destination: first.destination, error: "", busy: false };
}

function pick(form, entry) {
  form.choice = entry.id;
  form.name = entry.name;
  form.hosted = entry.hosted;
  form.destination = entry.destination;
  form.error = "";
}

function knownEntry(known, id) {
  return known.find((entry) => entry.id === id) || null;
}

/** Why the form cannot be sent yet, or "" when it can. */
export function appFormProblem(form, known) {
  const entry = knownEntry(known, form.choice);
  if (entry && !entry.local_program) return "web";
  if (!NAME_PATTERN.test(form.name)) return "name";
  if (form.hosted && !form.destination.trim()) return "destination";
  return "";
}

/** The fields of the "add an app" form: which application, its name, where text goes. */
export function appFormFields(form, known, update, prefix, short = false) {
  const entry = knownEntry(known, form.choice);
  const web = entry && !entry.local_program;
  const generic = form.choice === "generic";
  const described = { name: form.name || "this app", hosted: form.hosted, destination: form.destination.trim() };
  return [
    el(
      "div",
      { class: "choice-list", role: "radiogroup", "aria-labelledby": `${prefix}-which` },
      el("div", { class: "choice-head field-label", id: `${prefix}-which` }, "Which app?"),
      known.map((option) =>
        el(
          "label",
          { class: ["choice", option.id === form.choice && "is-selected"], key: option.id },
          el("input", {
            type: "radio",
            name: `${prefix}-known`,
            value: option.id,
            checked: option.id === form.choice,
            onchange: () => {
              pick(form, option);
              update();
            },
          }),
          el(
            "span",
            { class: "choice-text" },
            el("span", { class: "choice-label" }, option.label),
            el(
              "span",
              { class: "choice-hint" },
              !option.local_program
                ? "Cannot reach a program on this Mac"
                : option.id === "generic"
                  ? "You say where its text goes"
                  : `Sends to ${option.destination}`,
            ),
          ),
        ),
      ),
    ),
    web
      ? note(
          "plain",
          `${entry.label} cannot use this vault. It only calls servers that ${entry.destination || "its provider"}'s cloud can reach, and ${PRODUCT} runs on your Mac only. Use the same company's desktop app instead.`,
        )
      : [
          generic
            ? el(
                "div",
                { class: "choice-row", role: "radiogroup", "aria-labelledby": `${prefix}-where` },
                el("span", { class: "choice-row-label field-label", id: `${prefix}-where` }, "Where does its model run?"),
                [
                  [true, "On someone else's servers"],
                  [false, "On this Mac"],
                ].map(([hosted, label]) =>
                  el(
                    "label",
                    { class: "radio" },
                    el("input", {
                      type: "radio",
                      name: `${prefix}-kind`,
                      value: hosted ? "hosted" : "local",
                      checked: form.hosted === hosted,
                      onchange: () => {
                        form.hosted = hosted;
                        form.destination = hosted ? "" : "local model on this computer";
                        update();
                      },
                    }),
                    el("span", null, label),
                  ),
                ),
              )
            : null,
          generic && form.hosted
            ? field({
                label: "Who receives the text?",
                id: `${prefix}-destination`,
                hint: "The company or service behind the app, for example its model provider.",
                control: el("input", {
                  type: "text",
                  id: `${prefix}-destination`,
                  class: "input",
                  value: form.destination,
                  maxlength: 80,
                  autocomplete: "off",
                  oninput: (event) => {
                    form.destination = event.target.value;
                    update();
                  },
                }),
              })
            : null,
          field({
            label: "Name in this vault",
            id: `${prefix}-name`,
            hint: "Lower-case letters, digits and hyphens. This is how the app appears in Activity.",
            error: form.name && !NAME_PATTERN.test(form.name) ? "Use lower-case letters, digits and hyphens, starting with a letter; 32 at most." : "",
            control: el("input", {
              type: "text",
              id: `${prefix}-name`,
              class: "input input-mono",
              value: form.name,
              maxlength: 32,
              autocomplete: "off",
              autocapitalize: "off",
              spellcheck: "false",
              oninput: (event) => {
                form.name = event.target.value;
                update();
              },
            }),
          }),
          el(
            "div",
            { class: ["goes-to", short && "goes-to-short"] },
            el("span", { class: "field-label" }, "Where text goes"),
            form.hosted && !described.destination
              ? el("p", { class: "muted" }, "Say who receives the text.")
              : [
                  destinationChip(described),
                  short
                    ? null
                    : el(
                        "p",
                        { class: "muted" },
                        form.hosted
                          ? `Whatever this app reads from the vault is sent to ${described.destination} and cannot be recalled.`
                          : `${PRODUCT} cannot check this. If the app uses a cloud model, or has tools that send data out, what it reads leaves this Mac.`,
                      ),
                ],
          ),
        ],
    form.error ? note("red", form.error) : null,
  ];
}

/** Register the app the form describes. Resolves to the new app, or null when it failed. */
export async function submitAppForm(form, update) {
  form.busy = true;
  form.error = "";
  update();
  try {
    const body = { name: form.name, hosted: form.hosted };
    if (form.destination.trim()) body.destination = form.destination.trim();
    const answer = await api.addApp(body);
    form.busy = false;
    return answer.app;
  } catch (error) {
    form.busy = false;
    form.error = error.code === "cancelled" ? "" : error.message;
    update();
    return null;
  }
}

/** The "Add app" sheet. Afterwards the configuration for the new app is shown. */
export async function openAddApp(ctx) {
  const known = (ctx.data.apps && ctx.data.apps.known) || [];
  const form = newAppForm(known);
  const sheet = openSheet(
    (self) => {
      const problem = appFormProblem(form, known);
      return el(
        "form",
        {
          class: "sheet-content",
          autocomplete: "off",
          onsubmit: async (event) => {
            event.preventDefault();
            if (problem || form.busy) return;
            const app = await submitAppForm(form, () => self.update());
            if (app) self.close({ app, host: form.choice });
          },
        },
        sheetHead("Add an app", "An app is an agent program on this Mac, such as Claude Desktop, that may ask the vault for text. A new app can read nothing until you give it a collection."),
        appFormFields(form, known, () => self.update(), "add-app"),
        sheetFoot(
          button("Cancel", { onclick: () => self.close(null) }),
          button(form.busy ? "Adding…" : "Add app", { kind: "primary", type: "submit", disabled: Boolean(problem), busy: form.busy }),
        ),
      );
    },
    { label: "Add an app", wide: true },
  );
  const added = await sheet.closed;
  if (!added) return;
  toast(`Added ${added.app.name}. It can read nothing until you give it a collection.`, "green");
  await ctx.refresh();
  openConnect(ctx, added.app.name, added.host);
}

// -- connecting an application ------------------------------------------------------

/** "Connect Claude Desktop" where the program is a known one, else the app's name in the vault. */
export function connectTitle(known, hostId, appName) {
  const entry = known.find((item) => item.id === hostId);
  return entry && entry.id !== "generic" ? `Connect ${entry.label}` : `Connect ${appName}`;
}

/** The line under that title: the name the app has in the vault. */
export function connectSub(appName) {
  return ["Name in this vault: ", el("span", { class: "mono" }, appName)];
}

/** Load the configuration that connects one app into `view`. */
export async function loadConnect(view, update) {
  view.loading = true;
  view.error = "";
  update();
  const wanted = view.host;
  try {
    const data = await api.connect(view.app, wanted);
    if (view.host !== wanted) return;
    view.data = data;
  } catch (error) {
    if (view.host !== wanted) return;
    view.data = null;
    view.error = error.message;
  }
  view.loading = false;
  update();
}

/**
 * A configuration as words and the spaces between them, so that a long line wraps
 * at a space and never inside an option such as "--store". Only a word that is
 * longer than a whole line wraps inside itself (the style sheet sees to that).
 */
function codeWords(text) {
  return String(text)
    .split(/(\s+)/)
    .filter(Boolean)
    .map((part) => (/^\s+$/.test(part) ? part : el("span", { class: "code-word" }, part)));
}

/** The configuration blocks with their copy buttons, the file locations and the notes. */
export function connectBlocks(view, update) {
  const data = view.data;
  if (view.error) return note("red", view.error);
  if (!data) return el("p", { class: "muted" }, "Loading the configuration…");
  const copy = async (index, text) => {
    const done = await copyText(text);
    view.copied = done ? index : -1;
    update();
    if (!done) toast("Copying is not allowed here. Select the text and copy it yourself.", "red");
    setTimeout(() => {
      if (view.copied === index) {
        view.copied = -1;
        update();
      }
    }, 2000);
  };
  return [
    data.ready && data.ready.message ? note("amber", data.ready.message) : null,
    data.explanation ? note("plain", data.explanation) : null,
    data.blocks.map((block, index) =>
      el(
        "section",
        { class: "config", key: `block-${index}` },
        el(
          "header",
          { class: "config-head" },
          el("h3", { class: "config-title" }, block.title),
          button(view.copied === index ? "Copied" : "Copy", {
            small: true,
            glyph: view.copied === index ? "check" : "copy",
            onclick: () => copy(index, block.text),
            label: view.copied === index ? "Copied" : `Copy: ${block.title}`,
          }),
        ),
        el("pre", { class: "code", tabindex: 0, "aria-label": block.title }, el("code", null, codeWords(block.text))),
        block.where && block.where.length
          ? el(
              "div",
              { class: "config-where" },
              el("span", { class: "config-where-label" }, block.where.length === 1 ? "Where to put it" : "Where to put it, in the one that exists"),
              el("ul", { class: "path-list" }, block.where.map((path) => el("li", null, pathText(path)))),
            )
          : null,
      ),
    ),
    data.notes && data.notes.length ? el("ul", { class: "plain-list" }, data.notes.map((text) => el("li", null, text))) : null,
    data.blocks.length
      ? el(
          "p",
          { class: "muted small" },
          `This configuration holds no passphrase and no key. It starts a small MCP server that passes each search and each passage request to ${PRODUCT}, which decides and records it.`,
        )
      : null,
  ];
}

/** The known product the sheet is set to, when it is one that cannot reach a program on this Mac. */
function cannotConnect(known, view) {
  const entry = known.find((item) => item.id === view.host);
  return entry && entry.local_program === false ? entry : null;
}

/** The "Connect" sheet of one app. */
export function openConnect(ctx, appName, hostId) {
  const known = (ctx.data.apps && ctx.data.apps.known) || [];
  const fallback = known.some((entry) => entry.id === "generic") ? "generic" : known.length ? known[0].id : "generic";
  const view = {
    app: appName,
    host: known.some((entry) => entry.id === hostId) ? hostId : known.some((entry) => entry.id === appName) ? appName : fallback,
    data: null,
    loading: false,
    error: "",
    copied: -1,
  };
  const sheet = openSheet(
    (self) =>
      el(
        "div",
        { class: "sheet-content" },
        // A product that cannot reach this Mac gets its explanation, not a promise of a configuration.
        cannotConnect(known, view)
          ? sheetHead(`${cannotConnect(known, view).label} cannot be connected`, null, connectSub(appName))
          : sheetHead(
              connectTitle(known, view.host, appName),
              "Add the vault to the app's own configuration. Nothing is changed for you: copy the block and put it where the app reads it.",
              connectSub(appName),
            ),
        field({
          label: "Configuration for",
          id: "connect-host",
          control: el(
            "select",
            {
              id: "connect-host",
              class: "select",
              value: view.host,
              onchange: (event) => {
                view.host = event.target.value;
                view.copied = -1;
                loadConnect(view, () => self.update());
              },
            },
            known.map((entry) => el("option", { value: entry.id }, entry.label)),
          ),
        }),
        el("div", { class: ["connect-body", view.loading && "is-loading"] }, connectBlocks(view, () => self.update())),
        sheetFoot(button("Done", { kind: "primary", onclick: () => self.close(), autofocus: true })),
      ),
    { label: `Connect ${appName}`, wide: true },
  );
  loadConnect(view, () => sheet.update());
  return sheet.closed;
}

// -- limits -------------------------------------------------------------------------

export const LIMIT_FIELDS = [
  ["chars_per_day", "Characters in 24 hours", 0, 100000000],
  ["chars_per_30_days", "Characters in 30 days", 0, 100000000],
  ["searches_per_day", "Searches in 24 hours", 0, 10000],
  ["chars_per_call", "Characters per search result", 500, 8000],
  ["calls_per_minute", "Calls per minute", 1, 120],
];

/** A whole number from a form field, or null when the field does not hold one in range. */
export function readNumber(text, min, max) {
  const cleaned = String(text).replace(/[,\s]/g, "");
  if (!/^\d+$/.test(cleaned)) return null;
  const value = Number(cleaned);
  return value >= min && value <= max ? value : null;
}

/**
 * One limit as a row: its name on the left and a short field where the figure
 * stands when it is only read. The range is the field's title, and is said in
 * full under the row when what was typed does not fit it.
 */
export function numberRow({ id, label, value, min, max, oninput, suffix }) {
  const bad = readNumber(value, min, max) === null;
  return el(
    "div",
    { class: ["limit-row", bad && "has-error"], key: id },
    el("label", { class: "limit-label", for: id }, label),
    el(
      "span",
      { class: "limit-input" },
      el("input", {
        type: "text",
        inputmode: "numeric",
        id,
        class: "input input-number input-compact",
        value,
        autocomplete: "off",
        title: `${num(min)} to ${num(max)}`,
        "aria-invalid": bad ? "true" : null,
        "aria-describedby": bad ? `${id}-error` : null,
        oninput,
      }),
      suffix ? el("span", { class: "affix" }, suffix) : null,
    ),
    bad ? el("p", { class: "field-error limit-error", id: `${id}-error`, role: "alert" }, `A whole number from ${num(min)} to ${num(max)}.`) : null,
  );
}

/** The sheet that changes the limits of one app. */
export async function openAppLimits(ctx, app, ceilings) {
  const draft = {};
  for (const [key] of LIMIT_FIELDS) draft[key] = num(app.limits[key]);
  const form = { busy: false, error: "" };
  const maxOf = (key, max) => (key === "chars_per_call" && ceilings ? ceilings.chars_per_call : max);
  const sheet = openSheet(
    (self) => {
      const values = {};
      let valid = true;
      for (const [key, , min, max] of LIMIT_FIELDS) {
        values[key] = readNumber(draft[key], min, maxOf(key, max));
        if (values[key] === null) valid = false;
      }
      return el(
        "form",
        {
          class: "sheet-content",
          autocomplete: "off",
          onsubmit: async (event) => {
            event.preventDefault();
            if (!valid || form.busy) return;
            const changed = {};
            for (const [key] of LIMIT_FIELDS) if (values[key] !== app.limits[key]) changed[key] = values[key];
            if (!Object.keys(changed).length) {
              self.close(false);
              return;
            }
            form.busy = true;
            form.error = "";
            self.update();
            try {
              await api.setAppLimits(app.name, changed);
              self.close(true);
            } catch (error) {
              form.busy = false;
              form.error = error.code === "cancelled" ? "" : error.message;
              self.update();
            }
          },
        },
        sheetHead(`Limits of ${app.name}`, "How much text this app may receive. Raising a limit needs your passphrase when you last typed it more than 10 minutes ago."),
        el(
          "div",
          { class: "limit-rows" },
          LIMIT_FIELDS.map(([key, label, min, max]) =>
            numberRow({
              id: `limit-${key}`,
              label,
              value: draft[key],
              min,
              max: maxOf(key, max),
              oninput: (event) => {
                draft[key] = event.target.value;
                self.update();
              },
            }),
          ),
        ),
        form.error ? note("red", form.error) : null,
        sheetFoot(
          button("Cancel", { onclick: () => self.close(false) }),
          button(form.busy ? "Saving…" : "Save", { kind: "primary", type: "submit", disabled: !valid, busy: form.busy }),
        ),
      );
    },
    { label: `Limits of ${app.name}` },
  );
  if (await sheet.closed) {
    toast(`Limits of ${app.name} changed.`, "green");
    await ctx.refresh();
  }
}

/** A sheet with one or two text fields; resolves to the entered values or null. */
export function openCollectionSheet() {
  const form = { name: "", note: "", busy: false, error: "" };
  const sheet = openSheet(
    (self) => {
      const good = /^[a-z0-9][a-z0-9-]{0,31}$/.test(form.name);
      return el(
        "form",
        {
          class: "sheet-content",
          autocomplete: "off",
          onsubmit: async (event) => {
            event.preventDefault();
            if (!good || form.busy) return;
            form.busy = true;
            form.error = "";
            self.update();
            try {
              const body = { name: form.name };
              if (form.note.trim()) body.note = form.note.trim();
              await api.setCollection(body);
              self.close(form.name);
            } catch (error) {
              form.busy = false;
              form.error = error.code === "cancelled" ? "" : error.message;
              self.update();
            }
          },
        },
        sheetHead("New collection", "A collection is a group of documents that you give to apps as a whole. A new one is not open to hosted apps."),
        field({
          label: "Name",
          id: "collection-name",
          hint: "Lower-case letters, digits and hyphens, for example “home” or “tax-2026”.",
          error: form.name && !good ? "Use lower-case letters, digits and hyphens; 32 at most." : "",
          control: el("input", {
            type: "text",
            id: "collection-name",
            class: "input input-mono",
            value: form.name,
            maxlength: 32,
            autofocus: true,
            autocapitalize: "off",
            spellcheck: "false",
            oninput: (event) => {
              form.name = event.target.value;
              self.update();
            },
          }),
        }),
        field({
          label: "Note (optional)",
          id: "collection-note",
          hint: "One line that says what is inside. Apps that are given this collection see it.",
          control: el("input", {
            type: "text",
            id: "collection-note",
            class: "input",
            value: form.note,
            maxlength: 200,
            oninput: (event) => {
              form.note = event.target.value;
            },
          }),
        }),
        form.error ? note("red", form.error) : null,
        sheetFoot(
          button("Cancel", { onclick: () => self.close(null) }),
          button(form.busy ? "Creating…" : "Create", { kind: "primary", type: "submit", disabled: !good, busy: form.busy }),
        ),
      );
    },
    { label: "New collection" },
  );
  return sheet.closed;
}

