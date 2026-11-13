// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The pieces every screen is made of: buttons, chips, meters, switches, sheets,
// toasts, and one row of activity.

import { el, icon, render } from "./dom.js";
import { agoShort, num, sentence, whenFull } from "./format.js";

export function button(label, options = {}) {
  const { kind = "default", onclick, disabled, type = "button", glyph, small, large, block, title, autofocus, busy, waiting } = options;
  return el(
    "button",
    {
      type,
      class: ["btn", `btn-${kind}`, small && "btn-small", large && "btn-large", block && "btn-block", busy && "is-busy", waiting && "is-waiting"],
      onclick,
      disabled: disabled || busy,
      // A button that waits stays where the Tab key expects it; its handler decides what a press does.
      "aria-disabled": waiting ? "true" : null,
      title,
      autofocus,
      id: options.id,
      key: options.key,
      "aria-label": options.label,
      "aria-describedby": options.describedby,
      "aria-pressed": options.pressed,
      // For a button that unfolds something below it: "true" or "false", and the id of what it unfolds.
      "aria-expanded": options.expanded,
      "aria-controls": options.controls,
    },
    glyph ? icon(glyph) : null,
    label ? el("span", { class: "btn-label" }, label) : null,
  );
}

export function iconButton(glyph, label, options = {}) {
  return el(
    "button",
    {
      type: "button",
      class: ["icon-btn", options.kind && `icon-btn-${options.kind}`],
      onclick: options.onclick,
      disabled: options.disabled,
      title: label,
      "aria-label": label,
      id: options.id,
      key: options.key,
    },
    icon(glyph),
  );
}

export function chip(label, tone = "quiet", glyph = null, title = null, extraClass = null) {
  return el("span", { class: ["chip", `chip-${tone}`, extraClass], title }, glyph ? icon(glyph) : null, el("span", null, label));
}

/** Where text goes once an app has it. `item` has `hosted` and `destination`. */
export function destinationText(item) {
  if (item.hosted === true) return `Sends to ${item.destination || "its provider"}`;
  if (item.hosted === false) return "Stays on this Mac, if the app really is local";
  return "";
}

/**
 * The destination as a chip: short in lists, in full on cards. A local app is
 * called "Local" in lists, the owner's own word for it: where its text stays is
 * not something the gate has checked.
 */
export function destinationChip(item, short = false) {
  if (item.hosted === true) {
    return chip(short ? item.destination || "hosted" : destinationText(item), "plain", "out", destinationText(item), "chip-dest");
  }
  if (item.hosted === false) {
    return chip(short ? "Local" : destinationText(item), "plain", "mac", destinationText(item), "chip-dest");
  }
  return null;
}

/** A file path that wraps between its folders and never inside a name. */
export function pathText(path, mono = true) {
  const parts = String(path === null || path === undefined ? "" : path).match(/[^\\/]*[\\/]?/g) || [];
  return el(
    "span",
    { class: ["path", mono && "mono"] },
    parts.filter(Boolean).map((part) => el("span", { class: "path-seg" }, part)),
  );
}

/** What happened, as a chip. Only the two states that need the owner's eye carry colour. */
export function statusChip(item) {
  if (item.kind === "release") {
    return item.approved ? chip("Released after approval", "quiet", "check") : chip("Released", "quiet");
  }
  // An app asked what it may use. Nothing left the vault, so the chip is the quietest of all.
  if (item.kind === "question") return chip("Question", "outline", null, "A question about what the app may use. No document text was released.");
  if (item.kind === "refusal") {
    return item.code === "NEEDS_APPROVAL" ? chip("Needs approval", "amber") : chip("Refused", "red");
  }
  if (item.kind === "approval") return chip("You approved", "quiet", "you");
  if (item.kind === "session") return chip("Vault", "quiet", "key");
  return chip("You changed", "quiet", "you");
}

/** A slim bar: how much of a limit is used. */
export function meter(used, limit, label) {
  const share = limit > 0 ? used / limit : used > 0 ? 1 : 0;
  const tone = share >= 1 ? "full" : share >= 0.8 ? "high" : "calm";
  return el(
    "div",
    {
      class: ["meter", `meter-${tone}`],
      role: "meter",
      "aria-label": label,
      "aria-valuemin": 0,
      "aria-valuemax": limit,
      "aria-valuenow": Math.min(used, limit),
      "aria-valuetext": `${num(used)} of ${num(limit)}`,
    },
    el("div", { class: "meter-fill", fill: share }),
  );
}

/** A labelled meter line: name on the left, "4,204 of 12,000" on the right, the bar below. */
export function meterRow(label, used, limit, unit = "characters") {
  return el(
    "div",
    { class: "meter-row" },
    el(
      "div",
      { class: "meter-head" },
      el("span", { class: "meter-label" }, label),
      el("span", { class: "meter-value" }, el("strong", null, num(used)), ` of ${num(limit)} ${unit}`),
    ),
    meter(used, limit, `${label}: ${num(used)} of ${num(limit)} ${unit}`),
  );
}

/** A switch with its label and an optional sentence below it. `id` must be unique on the page. */
export function toggle({ label, hint, checked, onchange, disabled, id, compact, tone, title }) {
  const ident = id;
  return el(
    "label",
    { class: ["switch-row", compact && "switch-row-compact", disabled && "is-disabled"], for: ident, title },
    el(
      "span",
      { class: "switch-text" },
      el("span", { class: "switch-label" }, label),
      hint ? el("span", { class: "switch-hint" }, hint) : null,
    ),
    el("input", {
      type: "checkbox",
      role: "switch",
      class: ["switch", tone && `switch-${tone}`],
      id: ident,
      checked,
      disabled,
      onchange: (event) => {
        // The page shows what the vault says, so the control goes back until the answer is in.
        const wanted = event.target.checked;
        event.target.checked = !wanted;
        onchange(wanted);
      },
    }),
  );
}

/** A choice between a few options, shown as one segmented control. */
export function segmented({ name, legend, value, options, onchange, onreselect, disabled }) {
  return el(
    "div",
    { class: ["segmented", disabled && "is-disabled"], role: "radiogroup", "aria-label": legend },
    options.map((option) =>
      el(
        "label",
        { class: ["segment", option.value === value && "is-selected", option.disabled && "is-disabled"], title: option.title },
        el("input", {
          type: "radio",
          name,
          value: option.value,
          checked: option.value === value,
          disabled: disabled || option.disabled,
          class: "sr-only",
          // Picking the option that is in force is an answer too, where one is asked for.
          onclick: onreselect && option.value === value ? () => onreselect(option.value) : null,
          onchange: (event) => {
            // Back to what the vault says until the change has gone through.
            const group = event.target.closest(".segmented");
            for (const input of group.querySelectorAll("input")) input.checked = input.value === String(value);
            onchange(option.value);
          },
        }),
        el("span", null, option.label),
      ),
    ),
  );
}

export function emptyState({ glyph, title, text, action }) {
  return el(
    "div",
    { class: "empty" },
    glyph ? el("div", { class: "empty-glyph" }, icon(glyph)) : null,
    title ? el("p", { class: "empty-title" }, title) : null,
    el("p", { class: "empty-text" }, text),
    action || null,
  );
}

export function note(tone, text, extra = null) {
  const glyph = tone === "amber" || tone === "red" ? "warning" : "info";
  return el("div", { class: ["note", `note-${tone}`] }, icon(glyph), el("div", { class: "note-text" }, text, extra));
}

/**
 * In the demo only: the passphrase of the demo vault in plain words, with a button
 * that types it. `passphrase` is what the app sent with its state, which it does for
 * the demo vault alone; without one nothing is shown. `fill` puts it into the field.
 */
export function demoPassphraseNote(passphrase, fill) {
  if (typeof passphrase !== "string" || !passphrase) return null;
  return el(
    "div",
    { class: "note note-plain demo-pass", key: "demo-pass" },
    icon("info"),
    el(
      "div",
      { class: "note-text" },
      el("p", null, "This is the demo. The passphrase of its vault is ", el("strong", { class: "mono demo-pass-word" }, passphrase), "."),
      el("p", { class: "muted small" }, "It is shown because the demo holds invented documents only. The passphrase of a real vault is never shown."),
      button("Fill it in", { small: true, onclick: fill, id: "demo-pass-fill" }),
    ),
  );
}

export function card(title, content, options = {}) {
  return el(
    "section",
    { class: ["card", options.class], key: options.key, "aria-label": options.label || (typeof title === "string" ? title : null) },
    title || options.action
      ? el(
          "header",
          { class: "card-head" },
          typeof title === "string" ? el("h2", { class: "card-title" }, title) : title,
          options.action || null,
        )
      : null,
    content,
  );
}

/** A label above a form control, with an optional hint or error below. */
export function field({ label, id, control, hint, error, action }) {
  return el(
    "div",
    { class: ["field", error && "has-error"] },
    action
      ? el("div", { class: "field-top" }, el("label", { class: "field-label", for: id }, label), action)
      : el("label", { class: "field-label", for: id }, label),
    control,
    error ? el("p", { class: "field-error", id: `${id}-error`, role: "alert" }, error) : null,
    hint && !error ? el("p", { class: "field-hint", id: `${id}-hint` }, hint) : null,
  );
}

// -- toasts -----------------------------------------------------------------------

/**
 * Where the browser can, the messages sit in the layer above sheets. They are put
 * there once, and again whenever a sheet has been opened over them: when the next
 * message comes, and when a sheet opens while messages are showing.
 */
function lift(host) {
  if (typeof host.showPopover !== "function" || !host.hasAttribute("popover")) return;
  try {
    const shown = host.matches(":popover-open");
    if (shown && !document.querySelector("dialog[open]")) return;
    // Shown again, the messages that are there must not arrive a second time.
    for (const item of host.children) item.classList.add("is-settled");
    if (shown) host.hidePopover();
    host.showPopover();
  } catch (_error) {
    host.removeAttribute("popover");
  }
}

/** A sheet has opened: the messages that are showing stay readable above it and its backdrop. */
function liftOverSheet() {
  const host = document.getElementById("toasts");
  if (host && host.children.length) lift(host);
}

/** A short message at the bottom of the window that goes away by itself. */
export function toast(message, tone = "plain") {
  const host = document.getElementById("toasts");
  if (!host || !message) return;
  lift(host);
  const item = el(
    "div",
    { class: ["toast", `toast-${tone}`] },
    icon(tone === "red" ? "warning" : tone === "green" ? "check" : "info"),
    el("span", { class: "toast-text" }, message),
  );
  host.appendChild(item);
  while (host.children.length > 3) host.firstChild.remove();
  const stay = tone === "red" ? 7000 : 4200;
  setTimeout(() => {
    item.classList.add("is-leaving");
    setTimeout(() => item.remove(), 220);
  }, stay);
}

/** Show what went wrong, unless the owner simply closed the passphrase sheet. */
export function toastError(error) {
  if (error && (error.code === "cancelled" || error.status === 401)) return;
  toast(error && error.message ? error.message : "Something went wrong.", "red");
}

// -- sheets -----------------------------------------------------------------------

const openSheets = new Set();

let sheetsWatcher = null;

/** `watcher(open)` is called whenever a sheet has opened or closed, with how many are open then. */
export function watchSheets(watcher) {
  sheetsWatcher = watcher;
}

function sheetsChanged() {
  if (sheetsWatcher) sheetsWatcher(openSheets.size);
}

/**
 * Open a sheet over the window. `build(sheet)` returns its content and is called
 * again by `sheet.update()`. `sheet.close(value)` closes it; `sheet.closed`
 * resolves to that value (undefined when the owner pressed Escape).
 */
export function openSheet(build, options = {}) {
  const dialog = document.createElement("dialog");
  dialog.className = options.wide ? "sheet sheet-wide" : "sheet";
  if (options.label) dialog.setAttribute("aria-label", options.label);
  const body = el("div", { class: "sheet-frame" });
  dialog.appendChild(body);
  document.body.appendChild(dialog);
  let settle;
  const sheet = {
    result: undefined,
    closed: new Promise((resolve) => {
      settle = resolve;
    }),
    update() {
      if (dialog.isConnected) render(body, build(sheet));
    },
    close(value) {
      sheet.result = value;
      if (dialog.open) dialog.close();
    },
  };
  dialog.addEventListener("close", () => {
    openSheets.delete(sheet);
    dialog.remove();
    settle(sheet.result);
    sheetsChanged();
  });
  openSheets.add(sheet);
  sheetsChanged();
  sheet.update();
  dialog.showModal();
  liftOverSheet();
  return sheet;
}

/** Close every sheet, for when the vault locks or the session ends under them. */
export function closeAllSheets() {
  for (const sheet of Array.from(openSheets)) sheet.close(undefined);
}

export function sheetHead(title, text = null, sub = null) {
  return el(
    "header",
    { class: "sheet-head" },
    el("h2", { class: "sheet-title" }, title),
    sub ? el("p", { class: "sheet-sub" }, sub) : null,
    text ? el("p", { class: "sheet-lead" }, text) : null,
  );
}

export function sheetFoot(...buttons) {
  return el("footer", { class: "sheet-foot" }, buttons);
}

/** Ask before something that cannot be taken back. Resolves to true or false. */
export function confirmSheet({ title, text, sentenceText, confirmLabel, cancelLabel = "Cancel", danger = false, cautious = false }) {
  const sheet = openSheet(
    (self) =>
      el(
        "div",
        { class: "sheet-content" },
        sheetHead(title, text),
        sentenceText ? note(danger ? "red" : "amber", sentenceText) : null,
        sheetFoot(
          button(cancelLabel, { onclick: () => self.close(false), autofocus: cautious || danger }),
          button(confirmLabel, { kind: danger ? "danger" : "primary", onclick: () => self.close(true), autofocus: !(cautious || danger) }),
        ),
      ),
    { label: title },
  );
  return sheet.closed.then((value) => value === true);
}

/** Put text on the clipboard. Resolves to false when the browser does not allow it. */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_error) {
    // Fall through to the older way.
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.className = "sr-only";
  const host = document.querySelector("dialog[open]") || document.body;
  host.appendChild(area);
  area.select();
  let done = false;
  try {
    done = document.execCommand("copy");
  } catch (_error) {
    done = false;
  }
  area.remove();
  return done;
}

// -- activity ---------------------------------------------------------------------

const TOOL_GLYPH = { search: "search", read: "read", status: "info" };

/** The name that stands for every call made under a name that is not answered. */
export const UNANSWERED = "(not registered)";

function rowGlyph(item) {
  if (item.kind === "refusal") return item.code === "NEEDS_APPROVAL" ? "approvals" : "refused";
  if (item.kind === "question") return "info";
  if (item.kind === "release") return TOOL_GLYPH[item.tool] || "out";
  if (item.kind === "approval") return "check";
  if (item.kind === "session") return "key";
  return "sliders";
}

/** Who did it, for the start of a row: the app's name, or "You". */
export function actor(item) {
  if (item.app === null || item.app === undefined) return "You";
  return item.app === UNANSWERED ? "A name not in use" : item.app;
}

const REFERENCE = /\bd-[0-9a-f]{6,}\.(\d+)\b/;

/** "passage 2 of 9 of tax-note-2025 in tax", as far as the page knows the passage. */
function passageWords(number, known, collections) {
  const info = known || {};
  const count = info.passages ? ` of ${num(info.passages)}` : "";
  const place = info.collection || (collections && collections.length === 1 ? collections[0] : "");
  return `passage ${num(info.passage || number)}${count} of ${info.title || "a document"}${place ? ` in ${place}` : ""}`;
}

/**
 * The entry's sentence as the row shows it: without the leading "you", which the
 * row shows as the actor, and with a passage in words where the entry names it by
 * its reference. `passages` maps references to what the page has learned about them.
 */
export function deed(item, passages = null) {
  let text = item.summary || "";
  if (item.app === null || item.app === undefined) text = text.replace(/^you\s+/i, "");
  if (item.app === UNANSWERED && item.code === "NOT_ALLOWED") return "asked and was refused: no app is answered under that name";
  const found = REFERENCE.exec(text);
  if (!found) return text;
  const known = passages ? passages.get(found[0]) : null;
  return text.replace(found[0], passageWords(Number(found[1]), known, item.collections));
}

/**
 * One row of activity: when, who, what, how much, and how it ended. A row opens the
 * details of its entry. With `plain` it is a line that opens nothing, for a screen
 * that has no place for the details.
 */
export function activityRow(item, { onopen, selected, now, passages, plain = false }) {
  const tone = item.kind === "refusal" ? (item.code === "NEEDS_APPROVAL" ? "amber" : "red") : "plain";
  const what = sentence(deed(item, passages));
  const repeated = item.count > 1;
  const last = repeated && item.last_time ? ` · last ${agoShort(item.last_time, now)}` : "";
  const shape = ["act-row", `act-${tone}`, selected && "is-selected", item.characters > 0 ? null : "act-nochars"];
  return el(
    "li",
    { class: "act-item", key: `a${item.seq}` },
    el(
      plain ? "div" : "button",
      plain
        ? { class: shape.concat("act-still") }
        : {
            type: "button",
            class: shape,
            onclick: (event) => onopen(item, event.detail === 0),
            "aria-pressed": selected ? "true" : "false",
            "aria-label": `${actor(item)} ${deed(item, passages)}. Open the details.`,
          },
      el("span", { class: "act-glyph" }, icon(rowGlyph(item))),
      el("time", { class: "act-time", datetime: item.time, title: whenFull(item.time) }, agoShort(item.time, now)),
      el(
        "span",
        { class: "act-who" },
        el("span", { class: ["act-app", item.app === UNANSWERED && "act-app-quiet"], title: actor(item) }, actor(item)),
        destinationChip(item, true),
      ),
      el(
        "span",
        { class: "act-what", title: what },
        what,
        repeated
          ? el(
              "span",
              { class: "act-count", title: `${num(item.count)} calls answered the same way${item.last_time ? `, the last one ${whenFull(item.last_time)}` : ""}` },
              `×${num(item.count)}${last}`,
            )
          : null,
      ),
      item.characters > 0
        ? el(
            "span",
            { class: "act-chars" },
            el("strong", null, num(item.characters)),
            el("span", { class: "act-unit" }, " characters"),
            el("span", { class: "act-unit-short" }, " chars"),
          )
        : null,
      el("span", { class: "act-status" }, statusChip(item)),
    ),
  );
}
