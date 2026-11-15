// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The sheets of the four tiles on Home, and the sheet that asks before an app is
// disconnected.
//
// Three tiles stand for a program whose settings file the app can write: a press
// has the app look at that one file, and the sheet then says what will be written
// and where, with one button "Connect". The file is looked at on a press (the one
// that opens the sheet, "Look again") and at no other time, and what is sent with
// "Connect" or "Disconnect" is the state of the file that the sheet was drawn from.
// "Connected" means that the entry was written and read back; the sheet says in the
// same place that this was not tried with the program itself. Where the entry was
// written and the system did not let the file be read back, the sheet of a connected
// app is shown instead, with a line that says so. Where the app writes nothing, the
// sheet says why and shows the text to paste, or offers another look. The fourth tile is for
// any other app: it only ever shows text to copy.
//
// This is the only script that asks for a look at a settings file, for the write
// and for the removal. The sentences of these sheets name the program by its
// readable name ("Claude Desktop"): they say what a press will do in that
// program's file and what to do in that program. The name the app has in the
// vault is said where two names could be taken for each other.

import { api } from "./api.js";
import { el, icon } from "./dom.js";
import { agoShort, day } from "./format.js";
import { button, chip, confirmSheet, copyBlock, copyLine, errorText, field, openSheet, pathText, sheetFoot, sheetHead, toast } from "./parts.js";
import { t } from "./texts.js";

/** The group that documents go to in a vault that has none yet. */
const FIRST_GROUP = "documents";

/** The name an app of the fourth tile may have in the vault. */
const NAME_PATTERN = /^[a-z][a-z0-9-]{0,31}$/;

// -- what the sheets share ------------------------------------------------------------

/**
 * A sentence of the table in which a place of a file or a folder is an element of its
 * own, so that it breaks between its folders and never inside a name.
 */
function sentence(key, values = {}) {
  const marked = Object.assign({}, values);
  const places = [];
  for (const name of ["path", "folder", "real", "copy"]) {
    if (typeof values[name] !== "string") continue;
    marked[name] = `\u0000${places.length}\u0000`;
    places.push(values[name]);
  }
  return t(key, marked)
    .split("\u0000")
    .map((piece, index) => (index % 2 ? pathText(places[Number(piece)], false) : piece));
}

/** One sentence of a sheet, as a paragraph. */
function say(id, content) {
  return el("p", { class: "sheet-say", id, key: id }, content);
}

/**
 * The sentence that says where text goes once the app has it, in an amber note. It is
 * what allows the press that gives access: `view.told` is true only while these words
 * are on the sheet, and every draw of a sheet starts without them.
 */
function whereTextGoes(view, id, text) {
  view.told = true;
  return amberNote(id, text);
}

/** An amber note with one sentence. Drawing it allows nothing. */
function amberNote(id, text) {
  return el("p", { class: "note note-amber", id, key: id, role: "note" }, icon("warning"), el("span", { class: "note-text" }, text));
}

/** What a sheet hands to the parts that copy: where the pressed button is remembered, and how the sheet is drawn again. */
function copying(view, sheet) {
  return { ui: view, render: () => sheet.update() };
}

/** The line that starts a question in an AI app, with "Copy". It ends with one space, which is copied with it. */
function askLine(view, sheet) {
  return el(
    "div",
    { class: "sheet-block", key: "ask" },
    el("h3", { class: "sheet-label" }, t("connect.label.ask")),
    copyLine(copying(view, sheet), "connect-ask-copy", t("connect.ask.start"), t("connect.label.ask")),
  );
}

/** A link that closes the sheet and leads to a page of the six with every setting. */
function settingsLink(sheet, id, filled = false) {
  return el(
    "a",
    { class: filled ? "btn btn-primary" : "link", id, href: "#/pro/apps", onclick: () => sheet.close(null) },
    filled ? el("span", { class: "btn-label" }, t("nav.settings")) : t("nav.settings"),
  );
}

/** The apps of the vault as the page last heard of them. */
function registeredApps(ctx) {
  return ctx.data.apps ? ctx.data.apps.apps : [];
}

/**
 * Add an app that sends text to a provider and give it "ask me each time" for the
 * vault's one group, in three ordinary requests. A vault without a group gets the
 * group that documents go to; in a vault with several groups no group is touched
 * and nothing is given. It writes no file.
 */
async function addWithAccess(ctx, name, destination) {
  await api.addApp({ name, hosted: true, destination });
  const groups = ctx.data.apps ? ctx.data.apps.collections : [];
  if (groups.length > 1) return;
  const group = groups.length ? groups[0].name : FIRST_GROUP;
  // The group is opened to apps that send text to a provider: the sheet has said where text goes.
  await api.setCollection({ name: group, allow_hosted: true });
  await api.setGrant(name, group, "approve");
}

// -- the three tiles that write: what a look found ------------------------------------

/** Why nothing is written, as the sentence of each word the app answers with. */
const REFUSED = {
  unreadable: "connect.refused.say.1.unreadable",
  not_read: "connect.refused.say.1.not_read",
  busy: "connect.refused.say.1.busy",
  linked: "connect.refused.say.1.linked",
  no_access: "connect.refused.say.1.no_access",
  by_hand: "connect.refused.say.1.by_hand",
  no_copy: "connect.refused.say.1.no_copy",
  unsupported: "connect.refused.say.1.unsupported",
  unstable: "connect.refused.say.1.unstable",
  moved: "connect.refused.say.1.moved",
  recent: "connect.refused.say.1.recent",
  changed: "connect.refused.say.1.changed",
  failed: "connect.refused.say.1.failed",
  kept: "connect.refused.say.1.failed.kept",
  not_ready: "connect.refused.say.1.not_ready",
  other_kind: "connect.refused.say.1.other_kind",
  demo: "connect.refused.say.1.demo",
};

/** The words after which the text to paste is the way out: the sheet says so in a second sentence. */
const PASTE_INSTEAD = ["unreadable", "not_read", "linked", "no_access", "by_hand", "no_copy", "unsupported", "unstable", "moved"];

/** The words after which another look is the next thing to do. */
const LOOK_AGAIN = ["recent", "changed", "busy", "failed", "kept"];

/**
 * Which state of the sheet a look leads to: the first line of the table that applies.
 * Sets `view.state` and, for a refusal, `view.word`.
 */
function settle(ctx, view, look) {
  const own = registeredApps(ctx);
  view.look = look;
  view.word = "";
  if (look.app && look.app.differs) {
    view.state = "refused";
    view.word = "other_kind";
  } else if (look.problem === "expands") view.state = "expands";
  else if (look.problem === "unstable" || look.problem === "moved") {
    view.state = "refused";
    view.word = look.problem;
  } else if (look.state === "no_folder") view.state = "nofolder";
  else if (["linked", "no_access", "unreadable", "not_read", "busy", "unsupported"].includes(look.state)) {
    view.state = "refused";
    view.word = look.state;
  } else if (look.state === "present_as" && own.some((app) => app.name === look.other_name)) view.state = "as";
  else if ((look.state === "present" || look.state === "present_more") && look.app && look.app.registered) view.state = "on";
  else view.state = "offer";
}

/**
 * Look at the settings file of the tile's program: the one request that reads a file
 * outside the vault. It is made by the press that opens the sheet and by "Look
 * again", and by nothing else: no timer, no load, no draw.
 */
async function lookAt(ctx, sheet, view) {
  view.state = "looking";
  view.failed = null;
  view.answer = null;
  view.unread = false;
  view.hand = null;
  sheet.update();
  try {
    settle(ctx, view, await api.connectLook(view.tile.host));
  } catch (error) {
    if (error.code === "demo") {
      view.state = "refused";
      view.word = "demo";
    } else {
      view.state = "failed";
      view.failed = error;
    }
  }
  sheet.update();
}

// -- the three tiles that write: the states of the sheet ------------------------------

/** What the sentences of a sheet have places for: the names of the program, and the places of the file. */
function values(view, more = {}) {
  const tile = view.tile;
  const look = view.look || {};
  const answer = view.answer || {};
  return Object.assign(
    {
      label: tile.label,
      readers: tile.readers,
      destination: tile.destination,
      entry: look.entry,
      path: answer.path || look.path || tile.path || "",
      folder: look.folder || "",
      real: answer.real_folder || look.real_folder || "",
      format: look.format || "",
      name: tile.name,
    },
    more,
  );
}

/** The title of the sheet: what a press will do, the program by the name sentences use for it, and what kind of program it is. */
function title(view) {
  return t("connect.title", { label: view.tile.label, kind: t(view.tile.desktop ? "connect.label.kind.desktop" : "connect.label.kind.terminal") });
}

/** The first sentence of the offer: what will be written and where, by what the look found and whether the app is registered. */
function offerSentence(view) {
  const look = view.look;
  const registered = Boolean(look.app && look.app.registered);
  let key = "connect.offer.say.1.add";
  if (look.state === "differs" || look.state === "present_as") key = "connect.offer.say.1.replace";
  else if (look.state === "present" || look.state === "present_more") key = "connect.offer.say.1.again";
  else if (registered) key = "connect.offer.say.1.gone";
  else if (look.state === "no_file") key = "connect.offer.say.1.new";
  return sentence(key, values(view));
}

/**
 * The second sentence of the offer, which says where text goes: that the app can read
 * without asking where it already holds "Can read"; that the owner chooses in
 * Settings where the vault has several groups and the app has none; else that each
 * passage waits for a yes.
 */
function goesSentence(ctx, view) {
  const look = view.look || {};
  const app = registeredApps(ctx).find((one) => one.name === view.tile.name);
  const access = app ? Object.keys(app.grants || {}).length > 0 : false;
  if (look.app && look.app.reads) return t("connect.offer.say.2.read", values(view));
  if (look.groups > 1 && !access) return t("connect.offer.say.2.groups", values(view));
  return t("connect.offer.say.2", values(view));
}

/** The warning of the state that keeps every app from connecting, as its sentence; null when there is none. */
function notReady(ctx) {
  const codes = (ctx.state.warnings || []).map((warning) => warning.code);
  if (codes.includes("python_too_old")) return t("notice.python_too_old.say.1");
  if (codes.includes("no_sdk")) return t("notice.no_sdk.say.1");
  return null;
}

/** One line for each kept copy that lies beside the file: its place, and the day it was made. */
function copyLines(ctx, view) {
  return (view.look.copies || []).map((copy, index) =>
    el("p", { class: "sheet-fact", key: `copy-${index}` }, sentence("connect.status.copy", { copy: copy.path, date: day(copy.made_at, ctx.now) })),
  );
}

/** What "Details" unfolds: the command the entry holds, the variable that decided the place, the copies beside the file. */
function details(ctx, view) {
  const look = view.look;
  return el(
    "div",
    { class: "sheet-details", id: "connect-details", key: "details" },
    el("h3", { class: "sheet-label" }, t("connect.label.command", { label: view.tile.label })),
    // The command is the owner's own data: one part per line.
    el("pre", { class: "code", tabindex: 0, "aria-label": t("connect.label.command", { label: view.tile.label }) }, el("code", null, (look.command || []).join("\n"))),
    look.variable ? el("p", { class: "sheet-fact" }, t("connect.status.variable", { variable: look.variable })) : null,
    copyLines(ctx, view),
  );
}

/**
 * "Connect": the app registers the program under the tile's name and writes this
 * vault's entry into its settings file. What is sent is the state of the file that
 * this sheet was drawn from, and nothing is sent unless the sentence that says where
 * text goes is on the sheet.
 */
async function connect(ctx, sheet, view) {
  if (!view.told || view.busy) return;
  const host = view.tile.host;
  const look = view.look;
  view.busy = true;
  view.failed = null;
  sheet.update();
  try {
    const answer = await api.connectWrite(host, look.state);
    view.answer = answer;
    // Written, but the system did not let the file be read back: the sheet that says "read back" is not shown.
    // The sheet of a connected app is, from what the answer says of the file and of the app, with a line that says what was not done; no file is looked at for it.
    view.unread = answer.connected === true && answer.not_read_back === true;
    if (answer.connected === true && answer.not_read_back === true) settle(ctx, view, Object.assign({}, look, { state: answer.state, app: answer.app }));
    else if (answer.connected === true) view.state = "connected";
    else if (answer.state === "expands") view.state = "expands";
    else if (answer.state === "present_as") {
      view.state = "as";
      view.look = Object.assign({}, look, { other_name: answer.other_name });
    } else {
      view.state = "refused";
      view.word = answer.state === "failed" && answer.kept === true ? "kept" : answer.state;
    }
  } catch (error) {
    if (error.code === "changed" || error.code === "demo") {
      view.state = "refused";
      view.word = error.code;
    } else view.failed = error;
  }
  view.busy = false;
  // The tile says what the vault says: the list of apps is asked for again.
  await ctx.refresh();
  sheet.update();
}

/**
 * "Disconnect": a second sheet asks first. Its first sentence follows what the look
 * found: the entry is removed where it is this vault's, else the file is left as it
 * is; in either case the app is no longer answered. What is sent is the state of the
 * file that the sheet was drawn from. The answer is a message, by what became of
 * the file.
 */
async function disconnect(ctx, sheet, view) {
  const host = view.tile.host;
  const look = view.look;
  const ours = look.state === "present" || look.state === "present_more";
  let first = "disconnect.say.1.vault";
  if (look.state === "present") first = "disconnect.say.1";
  else if (look.state === "present_more") first = "disconnect.say.1.more";
  const agreed = await confirmSheet({
    title: t("disconnect.title", values(view)),
    text: sentence(first, values(view)),
    // Where the file will be changed, the program should not be running.
    sentenceText: ours ? t("disconnect.say.2", values(view)) : null,
    confirmLabel: t("connect.button.disconnect"),
    cancelLabel: t("common.button.cancel"),
    danger: true,
  });
  if (!agreed) return;
  try {
    const answer = await api.connectRemove(host, look.state);
    const said = { removed: "disconnect.result.done", no_entry: "disconnect.result.vault", differs: "disconnect.result.differs" };
    const key = Object.prototype.hasOwnProperty.call(said, answer.file) ? said[answer.file] : "disconnect.result.kept";
    const done = t(key, values(view, { path: answer.path || look.path }));
    // Removed, but the system did not let the file be read back: the message says so, and is not the green one.
    const unread = answer.file === "removed" && answer.not_read_back === true;
    toast(unread ? [done, t("connect.status.unread")].join(" ") : done, !unread && (answer.file === "removed" || answer.file === "no_entry") ? "green" : "amber");
    sheet.close(null);
  } catch (error) {
    if (error.code === "changed") {
      view.state = "refused";
      view.word = "changed";
    } else if (error.code !== "cancelled") view.failed = error;
    sheet.update();
  }
  await ctx.refresh();
}

/** Where in the file the text goes, and which piece of the text is pasted there: by what the look found. */
function whereToPaste(look, block) {
  const whole = block.text;
  const inner = block.inner || null;
  const state = look ? look.state : "";
  const section = look ? look.section : null;
  // No file, or one that holds nothing: the text is the whole file.
  if (state === "no_folder" || state === "no_file" || (state === "empty" && section !== "empty")) return { key: "connect.hand.status.empty", text: whole };
  if (block.language === "json" && inner && (state === "empty" || state === "other")) {
    // An entry that is put before others needs a comma after it.
    if (section === "empty") return { key: "connect.hand.status.inside", text: inner.entry };
    if (section === "filled") return { key: "connect.hand.status.inside", text: `${inner.entry},` };
    if (section === "none") return { key: "connect.hand.status.none", text: `${inner.section},` };
  }
  if (block.language === "toml" && state === "other") return { key: "connect.hand.status.end", text: whole };
  // The file could not be read, or holds an entry of that name: no line says where the text goes.
  return { key: null, text: whole };
}

/**
 * "Add the app and show the text": where the app is not registered yet, it is added
 * with "ask me each time" for the vault's one group, as "Connect" would; then the
 * app is asked for the text. No file is written. Nothing is sent unless the sentence
 * that says where text goes is on the sheet.
 */
async function showText(ctx, sheet, view) {
  const hand = view.hand;
  if (!view.told || hand.busy) return;
  const tile = view.tile;
  hand.busy = true;
  hand.failed = null;
  sheet.update();
  try {
    if (!registeredApps(ctx).some((app) => app.name === tile.name)) {
      await addWithAccess(ctx, tile.name, tile.destination);
      await ctx.refresh();
    }
    hand.data = await api.connect(tile.name, tile.host);
  } catch (error) {
    hand.failed = error;
    await ctx.refresh();
  }
  hand.busy = false;
  sheet.update();
}

/** "Show the text to paste": the sheet turns to the way by hand. It changes nothing. */
function byHand(ctx, sheet, view) {
  view.state = "hand";
  view.hand = { data: null, busy: false, failed: null };
  sheet.update();
  // Where the app is registered already, there is nothing to add: the text is asked for at once.
  if (registeredApps(ctx).some((app) => app.name === view.tile.name)) showText(ctx, sheet, view);
}

/** The state "by hand": the place of the file, where in it the text goes, the text, and the line that starts a question. */
function handState(ctx, sheet, view) {
  const hand = view.hand;
  const look = view.look;
  const local = copying(view, sheet);
  const path = (look && look.path) || view.tile.path || "";
  // Where the app is registered already, nothing is added: there is no button, and the text is on its way.
  const registered = registeredApps(ctx).some((app) => app.name === view.tile.name);
  let shown = null;
  if (hand.data) {
    const format = look && look.format ? look.format.toLowerCase() : "";
    const blocks = hand.data.blocks || [];
    // The block that belongs into the settings file: the one of the file's own format.
    const block = blocks.find((one) => one.language === format && one.where && one.where.length) || blocks.find((one) => one.where && one.where.length) || blocks[0];
    // After the app found the file written in a way it does not change, no line claims a place in it.
    const where = block ? (view.word === "by_hand" ? { key: null, text: block.text } : whereToPaste(look, block)) : null;
    shown = block
      ? [
          path ? el("div", { class: "sheet-block", key: "file" }, el("h3", { class: "sheet-label" }, t("connect.hand.label.file")), copyLine(local, "connect-file-copy", path, t("connect.hand.label.file"))) : null,
          where.key ? el("p", { class: "sheet-fact", id: "connect-where", key: "where" }, t(where.key)) : null,
          copyBlock(local, "connect-text-copy", t("connect.hand.label.text"), where.text),
          askLine(view, sheet),
        ]
      : null;
  }
  return [
    say("connect-say-1", sentence("connect.hand.say.1", values(view, { path }))),
    whereTextGoes(view, "connect-say-2", goesSentence(ctx, view)),
    hand.failed ? el("p", { class: "field-error", id: "connect-error", key: "error", role: "alert" }, errorText(hand.failed)) : null,
    !shown && registered && hand.busy ? el("p", { class: "sheet-looking", key: "looking", role: "status" }, el("span", { class: "spinner" }), t("common.status.loading")) : null,
    shown,
    sheetFoot(
      button(t(shown ? "common.button.done" : "common.button.close"), { kind: shown ? "primary" : "default", id: "connect-close", onclick: () => sheet.close(null) }),
      shown || registered ? null : button(t("connect.hand.button.show"), { kind: "primary", id: "connect-show", busy: hand.busy, onclick: () => showText(ctx, sheet, view) }),
    ),
  ];
}

/** The state "refused": why nothing is written, and the buttons that are the way on from there. */
function refusedState(ctx, sheet, view) {
  const word = view.word;
  const own = Object.prototype.hasOwnProperty;
  let key = own.call(REFUSED, word) ? REFUSED[word] : "connect.refused.say.1.failed";
  // A linked folder whose real place the system would not tell: the sentence names no place.
  if (word === "linked" && !values(view).real) key = "connect.refused.say.1.linked.unknown";
  const paste = PASTE_INSTEAD.includes(word);
  const again = LOOK_AGAIN.includes(word);
  // In a demo: the command that starts a vault of the owner's own.
  const command = word === "demo" ? (ctx.state.launch || {}).command : null;
  const close = (filled) => button(t("common.button.close"), { kind: filled ? "primary" : "default", id: "connect-close", onclick: () => sheet.close(null) });
  let buttons;
  if (paste) buttons = [close(false), button(t("connect.button.byhand"), { kind: "primary", id: "connect-byhand", onclick: () => byHand(ctx, sheet, view) })];
  else if (again) {
    buttons = [
      close(false),
      word === "kept" ? button(t("connect.button.disconnect"), { id: "connect-disconnect", onclick: () => disconnect(ctx, sheet, view) }) : null,
      button(t("connect.button.look"), { kind: "primary", id: "connect-look", onclick: () => lookAt(ctx, sheet, view) }),
    ];
  } else if (word === "other_kind") buttons = [close(false), settingsLink(sheet, "connect-settings", true)];
  else buttons = [close(true)];
  return [
    say("connect-say-1", sentence(key, values(view))),
    paste ? say("connect-say-2", t("connect.refused.say.2")) : null,
    command ? copyLine(copying(view, sheet), "connect-launch-copy", command, t("bar.label.command")) : null,
    sheetFoot(buttons),
    // After a write that went wrong, the text to paste is still a way: a quiet link.
    word === "failed" ? el("p", { class: "sheet-more", key: "more" }, button(t("connect.button.byhand"), { kind: "quiet", small: true, id: "connect-byhand", onclick: () => byHand(ctx, sheet, view) })) : null,
  ];
}

/** Everything the sheet of one of the three tiles shows, by its state. "Connect" is drawn in the state "offer" and in no other. */
function drawConnect(ctx, sheet, view) {
  const tile = view.tile;
  const look = view.look;
  // Every draw starts without the sentence that says where text goes: only drawing it says that it is shown.
  view.told = false;
  let head = sheetHead(title(view));
  let body;
  if (view.state === "looking") {
    body = [el("p", { class: "sheet-looking", id: "connect-looking", key: "looking", role: "status" }, el("span", { class: "spinner" }), t("connect.status.looking"))];
  } else if (view.state === "failed") {
    body = [
      el("p", { class: "field-error", id: "connect-error", key: "error", role: "alert" }, errorText(view.failed)),
      sheetFoot(button(t("common.button.close"), { id: "connect-close", onclick: () => sheet.close(null) }), button(t("connect.button.look"), { kind: "primary", id: "connect-look", onclick: () => lookAt(ctx, sheet, view) })),
    ];
  } else if (view.state === "offer") {
    const blocked = notReady(ctx);
    const registered = Boolean(look.app && look.app.registered);
    body = [
      // What keeps every app from connecting, or what a connect will bring about, is said before the press.
      blocked ? amberNote("connect-notice", blocked) : null,
      !blocked && look.answered_change ? amberNote("connect-notice", t("notice.choice_ahead.say.1")) : null,
      say("connect-say-1", offerSentence(view)),
      whereTextGoes(view, "connect-say-2", goesSentence(ctx, view)),
      view.failed ? el("p", { class: "field-error", id: "connect-error", key: "error", role: "alert" }, errorText(view.failed)) : null,
      el(
        "div",
        { class: "sheet-more", key: "more" },
        button(t("connect.button.details"), {
          kind: "quiet",
          small: true,
          id: "connect-details-button",
          expanded: view.details ? "true" : "false",
          controls: "connect-details",
          onclick: () => {
            view.details = !view.details;
            sheet.update();
          },
        }),
      ),
      view.details ? details(ctx, view) : null,
      sheetFoot(
        registered ? button(t("connect.button.disconnect"), { kind: "quiet", id: "connect-disconnect", onclick: () => disconnect(ctx, sheet, view) }) : null,
        button(t("common.button.cancel"), { id: "connect-cancel", onclick: () => sheet.close(null) }),
        button(view.busy ? t("connect.button.busy") : t("connect.button.connect"), { kind: "primary", id: "connect-go", busy: view.busy, disabled: Boolean(blocked), onclick: () => connect(ctx, sheet, view) }),
      ),
    ];
  } else if (view.state === "connected") {
    const answer = view.answer;
    // Nothing was written where the entry was there already.
    const kept = look.state === "present" || look.state === "present_more";
    const app = registeredApps(ctx).find((one) => one.name === tile.name);
    const nothing = answer.groups > 1 && !(app && Object.keys(app.grants || {}).length > 0);
    head = sheetHead(t("connect.done.title"));
    body = [
      say("connect-say-1", t(tile.desktop ? "connect.done.say.1" : "connect.done.say.1.session", values(view))),
      say("connect-say-2", t("connect.done.say.2", values(view))),
      // Where the result is shown, the sheet says that it was not tried with the program itself.
      el("p", { class: "sheet-result", id: "connect-result", key: "result" }, icon("check"), el("span", null, t(kept ? "connect.done.status.kept" : "connect.done.status.tried", values(view)))),
      answer.changed_since ? el("p", { class: "sheet-fact", id: "connect-since", key: "since" }, t("connect.done.status.since")) : null,
      askLine(view, sheet),
      answer.copy ? el("p", { class: "sheet-fact", id: "connect-copy", key: "copy" }, sentence("connect.done.status.copy", { copy: answer.copy })) : null,
      nothing ? el("p", { class: "sheet-fact", id: "connect-groups", key: "groups" }, t("connect.done.status.groups", values(view)), " ", settingsLink(sheet, "connect-settings")) : null,
      sheetFoot(button(t("common.button.done"), { kind: "primary", id: "connect-done", onclick: () => sheet.close(null) })),
    ];
  } else if (view.state === "on") {
    const seen = look.app.last_seen;
    head = sheetHead(tile.label);
    body = [
      el(
        "div",
        { class: "sheet-facts", key: "facts" },
        el("p", { class: "sheet-fact", id: "connect-on-file" }, sentence("connect.on.status.file", values(view))),
        // After a connect whose result the system did not let the app read back.
        view.unread ? el("p", { class: "sheet-fact", id: "connect-unread" }, t("connect.status.unread")) : null,
        look.state === "present_more" ? el("p", { class: "sheet-fact" }, t("connect.on.status.more")) : null,
        look.switched_off ? el("p", { class: "sheet-fact", id: "connect-on-off" }, t("connect.on.status.off", values(view))) : null,
        el("p", { class: "sheet-fact", id: "connect-on-seen" }, seen && seen.time ? t("tile.status.seen", { ago: agoShort(seen.time, ctx.now) }) : t("tile.status.never")),
        seen && seen.claimed ? el("p", { class: "sheet-fact" }, t("card.status.claimed", { claimed: seen.claimed })) : null,
      ),
      // Connected, and nothing arrives: the sentence says what to do there.
      seen ? null : say("connect-say-1", t(tile.desktop ? "connect.on.say.1" : "connect.on.say.1.session", values(view))),
      askLine(view, sheet),
      copyLines(ctx, view),
      view.failed ? el("p", { class: "field-error", id: "connect-error", key: "error", role: "alert" }, errorText(view.failed)) : null,
      sheetFoot(
        button(t("connect.button.disconnect"), { id: "connect-disconnect", onclick: () => disconnect(ctx, sheet, view) }),
        button(t("common.button.done"), { kind: "primary", id: "connect-done", onclick: () => sheet.close(null) }),
      ),
    ];
  } else if (view.state === "as") {
    body = [
      say("connect-say-1", sentence("connect.as.say.1", values(view, { name: look.other_name }))),
      say("connect-say-2", t("connect.as.say.2", { name: look.other_name })),
      sheetFoot(settingsLink(sheet, "connect-settings"), button(t("common.button.done"), { kind: "primary", id: "connect-done", onclick: () => sheet.close(null) })),
    ];
  } else if (view.state === "nofolder") {
    body = [
      say("connect-say-1", sentence("connect.nofolder.say.1", values(view))),
      say("connect-say-2", t("connect.nofolder.say.2", values(view))),
      sheetFoot(button(t("common.button.close"), { id: "connect-close", onclick: () => sheet.close(null) }), button(t("connect.button.look"), { kind: "primary", id: "connect-look", onclick: () => lookAt(ctx, sheet, view) })),
      el("p", { class: "sheet-more", key: "more" }, button(t("connect.button.byhand"), { kind: "quiet", small: true, id: "connect-byhand", onclick: () => byHand(ctx, sheet, view) })),
    ];
  } else if (view.state === "expands") {
    // No text to paste: the pasted text would hold the same "${".
    body = [
      say("connect-say-1", t("connect.expands.say.1", values(view))),
      say("connect-say-2", t("connect.expands.say.2", values(view))),
      sheetFoot(button(t("common.button.close"), { kind: "primary", id: "connect-close", onclick: () => sheet.close(null) })),
    ];
  } else if (view.state === "hand") body = handState(ctx, sheet, view);
  else body = refusedState(ctx, sheet, view);
  return el("div", { class: "sheet-content connect", "data-state": view.state, "data-word": view.word || null }, head, body);
}

/**
 * A press on one of the three tiles that write: its sheet opens, and the app looks at
 * the settings file of the tile's program. In a demo no look is asked: its vault is
 * deleted when it stops, so nothing can be connected to it, and the sheet says so.
 */
export function openConnect(ctx, tile) {
  const demo = ctx.state.demo === true;
  const view = { tile, state: demo ? "refused" : "looking", word: demo ? "demo" : "", look: null, answer: null, unread: false, hand: null, busy: false, failed: null, details: false, told: false, copied: null };
  const sheet = openSheet((self) => drawConnect(ctx, self, view), { label: title(view), wide: true });
  if (!demo) lookAt(ctx, sheet, view);
  return sheet.closed;
}

// -- the fourth tile: any other app ---------------------------------------------------

/** The three texts for an app of the fourth tile, each with "Copy": the command, the JSON, and the line for a terminal. */
async function loadTexts(ctx, sheet, view, name) {
  view.name = name;
  view.state = "texts";
  view.texts = null;
  view.failed = null;
  sheet.update();
  try {
    const [general, code] = await Promise.all([api.connect(name, "generic"), api.connect(name, "claude-code")]);
    const shell = (data) => (data.blocks || []).find((block) => block.language === "shell");
    const json = (general.blocks || []).find((block) => block.language === "json");
    view.texts = { command: shell(general), json, line: shell(code) };
  } catch (error) {
    view.failed = error;
  }
  sheet.update();
}

/**
 * "Add": an app that sends text to a provider is added under the name that was typed,
 * with "ask me each time" for the vault's one group, and the texts to paste are
 * shown. Nothing is sent unless the sentence that says where text goes is on the sheet.
 */
async function addCustom(ctx, sheet, view) {
  const form = view.form;
  const name = form.name;
  const destination = form.destination.trim();
  if (!view.told || form.busy || !NAME_PATTERN.test(name) || !destination) return;
  form.busy = true;
  form.failed = null;
  form.taken = "";
  sheet.update();
  try {
    await addWithAccess(ctx, name, destination);
  } catch (error) {
    form.busy = false;
    if (error.code === "conflict") form.taken = name;
    else if (error.code !== "cancelled") form.failed = error;
    await ctx.refresh();
    sheet.update();
    // Where the app was added and a later step failed, the list of apps above the form shows it.
    return;
  }
  form.busy = false;
  await ctx.refresh();
  await loadTexts(ctx, sheet, view, name);
}

/** "Remove this app": a sheet asks first, then the app is no longer answered. */
async function removeCustom(ctx, sheet, view) {
  const name = view.name;
  const agreed = await confirmSheet({
    title: t("remove.title", { title: name }),
    confirmLabel: t("common.button.remove"),
    cancelLabel: t("common.button.cancel"),
    danger: true,
  });
  if (!agreed) return;
  try {
    await api.removeApp(name);
    toast(t("custom.result.removed", { name }), "green");
    sheet.close(null);
  } catch (error) {
    if (error.code !== "cancelled") view.failed = error;
    sheet.update();
  }
  await ctx.refresh();
}

/** The apps of the vault that are none of the three tiles' own. */
function otherApps(ctx) {
  const data = ctx.data.apps;
  if (!data) return [];
  const named = (data.tiles || []).filter((tile) => tile.tile !== "custom").map((tile) => tile.name);
  return data.apps.filter((app) => !named.includes(app.name));
}

/** Everything the sheet of the fourth tile shows: the form, or the texts of one app. */
function drawCustom(ctx, sheet, view) {
  const local = copying(view, sheet);
  view.told = false;
  if (view.state === "texts") {
    const texts = view.texts;
    return el(
      "div",
      { class: "sheet-content connect", "data-state": "texts" },
      sheetHead(el("span", { class: "mono" }, view.name)),
      view.failed ? el("p", { class: "field-error", id: "custom-error", key: "error", role: "alert" }, errorText(view.failed)) : null,
      !texts && !view.failed ? el("p", { class: "sheet-looking", key: "looking", role: "status" }, el("span", { class: "spinner" }), t("common.status.loading")) : null,
      // Three forms of the same thing: one of them is used, the one the app asks for.
      texts ? say("custom-texts-say", t("custom.texts.say.1")) : null,
      texts && texts.command ? copyBlock(local, "custom-command-copy", t("custom.label.command"), texts.command.text) : null,
      texts && texts.json ? copyBlock(local, "custom-json-copy", t("custom.label.json"), texts.json.text) : null,
      texts && texts.line ? copyBlock(local, "custom-line-copy", t("custom.label.claudecode"), texts.line.text) : null,
      texts ? askLine(view, sheet) : null,
      sheetFoot(
        button(t("custom.button.removeapp"), { kind: "quiet", id: "custom-remove", onclick: () => removeCustom(ctx, sheet, view) }),
        button(t("common.button.done"), { kind: "primary", id: "custom-done", onclick: () => sheet.close(null) }),
      ),
    );
  }
  const form = view.form;
  const others = otherApps(ctx);
  const destination = form.destination.trim();
  const fits = NAME_PATTERN.test(form.name);
  const ready = fits && Boolean(destination);
  // A name that does not fit keeps "Add" off: the field says so in red, with a name that would do.
  let nameError = "";
  if (form.taken && form.taken === form.name) nameError = t("custom.error.taken", { name: form.taken });
  else if (form.name && !fits) nameError = t("custom.error.name");
  return el(
    "form",
    {
      class: "sheet-content connect",
      "data-state": "form",
      autocomplete: "off",
      onsubmit: (event) => {
        event.preventDefault();
        addCustom(ctx, sheet, view);
      },
    },
    sheetHead(t("custom.title")),
    others.length
      ? el(
          "div",
          { class: "sheet-block", key: "others" },
          el("h3", { class: "sheet-label" }, t("custom.label.existing")),
          el(
            "ul",
            { class: "rows custom-rows" },
            others.map((app) =>
              el(
                "li",
                { class: "row", key: `app-${app.name}` },
                el("span", { class: "row-what mono" }, app.name),
                // Where its text goes: the receiver's name, or that it stays here if the app really is local.
                app.hosted ? chip(app.destination, "plain", "out", null, "chip-dest") : chip(t("card.chip.local"), "plain", "mac", null, "chip-dest"),
                button(t("custom.button.text"), { small: true, onclick: () => loadTexts(ctx, sheet, view, app.name) }),
              ),
            ),
          ),
        )
      : null,
    say("custom-say-1", t("custom.say.1")),
    field({
      label: t("custom.label.name"),
      id: "custom-name",
      hint: t("custom.status.name"),
      error: nameError,
      control: el("input", {
        type: "text",
        id: "custom-name",
        class: "input input-mono",
        value: form.name,
        maxlength: 32,
        autocomplete: "off",
        autocapitalize: "off",
        spellcheck: "false",
        autofocus: true,
        "aria-describedby": "custom-name-hint",
        oninput: (event) => {
          form.name = event.target.value;
          sheet.update();
        },
      }),
    }),
    field({
      label: t("custom.label.destination"),
      id: "custom-destination",
      control: el("input", {
        type: "text",
        id: "custom-destination",
        class: "input",
        value: form.destination,
        placeholder: t("custom.placeholder.destination"),
        maxlength: 80,
        autocomplete: "off",
        oninput: (event) => {
          form.destination = event.target.value;
          sheet.update();
        },
      }),
    }),
    // The sentence of this app once both fields say what it is: only that one allows "Add". Until then the one that holds for every app stands in its place.
    ready ? whereTextGoes(view, "custom-say-2", t("custom.say.2", { name: form.name, destination })) : amberNote("custom-say-2", t("docs.say.2.general")),
    form.failed ? el("p", { class: "field-error", id: "custom-error", key: "error", role: "alert" }, errorText(form.failed)) : null,
    sheetFoot(
      button(t("common.button.cancel"), { id: "custom-cancel", onclick: () => sheet.close(null) }),
      button(form.busy ? t("custom.button.busy") : t("custom.button.add"), { kind: "primary", type: "submit", id: "custom-add", busy: form.busy, disabled: !ready }),
    ),
    // An app whose model runs on this computer changes which apps are answered: it is added where that is explained.
    el("p", { class: "sheet-more", key: "local" }, el("a", { class: "link", id: "custom-local", href: "#/pro/apps", onclick: () => sheet.close(null) }, t("custom.button.local"))),
  );
}

/** A press on the fourth tile: the sheet for any other app, which shows text to copy and writes no file. */
export function openCustom(ctx) {
  const view = { state: "form", form: { name: "", destination: "", busy: false, failed: null, taken: "" }, name: "", texts: null, failed: null, told: false, copied: null };
  const sheet = openSheet((self) => drawCustom(ctx, self, view), { label: t("custom.title"), wide: true });
  return sheet.closed;
}
