// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The screens before the vault is open (pairing, the passphrase of a new vault, the
// locked vault, the app that does not answer), and the three focused steps of
// setting up: the app, a document, the first question. Each step is an address.
// Which step comes next is read from the vault, never from what this page remembers.
//
// The four screens take every text from the text table and carry the choice of the
// language. The three steps keep their own English texts.

import { api } from "./api.js";
import { ASK_START, FIRST_GROUP, basicBars, copyButton, copyLine, launchBlock, setupDone, setupFacts, waitingSaid } from "./basic.js";
import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { at, bytes, count, num, parseTime, when } from "./format.js";
import { SAMPLE, dropZone, isSample, loadingBlock, sessionFiles, whoLine } from "./manage.js";
import { UNANSWERED, activityRow, button, chip, demoPassphraseNote, errorText, field, languageSwitch, note, pathText, toast } from "./parts.js";
import { GENERAL_SENTENCE, appFormProblem, basicGrantSentence, connectSub, loadConnect, otherAppFields } from "./sheets.js";
import { chosenLanguage, t } from "./texts.js";
import { approvalCards } from "./views.js";

function brand(ctx) {
  return el(
    "div",
    { class: "gate-brand" },
    el("img", { src: "favicon.svg", width: 28, height: 28, alt: "" }),
    el("span", { class: "gate-name" }, PRODUCT),
    ctx.session && ctx.session.demo ? chip(t("common.chip.demo"), "accent") : null,
  );
}

/**
 * The frame of every screen that is not the vault itself: the choice of the language
 * top right, the name, one card. `options.step` is the frame of a step of setting
 * up: the card starts at the top of the window, so that what grows in it moves
 * nothing that stands above, a bar that says what is wrong (`options.bar`) stands
 * over the card, and a line (`options.foot`) under it.
 */
function gateScreen(ctx, key, content, options = {}) {
  return el(
    "main",
    { class: ["gate", options.wide && "gate-wide", options.wider && "gate-wider", options.step && "gate-step"], key },
    el("div", { class: "gate-lang", key: "lang" }, languageSwitch(ctx.chooseLanguage)),
    brand(ctx),
    options.bar ? el("div", { class: "gate-bar", key: "bar" }, options.bar) : null,
    content,
    options.foot ? el("p", { class: "gate-foot" }, options.foot) : null,
  );
}

/** The foot of a card: its last sentences, each an element of its own, under a hairline. */
function cardFoot(...sentences) {
  const said = sentences.filter(Boolean);
  return el(
    "p",
    { class: "gate-foot gate-foot-in", key: "foot" },
    said.map((text, index) => [index ? " " : null, el("span", null, text)]),
  );
}

export function bootScreen() {
  return el(
    "div",
    { class: "boot", key: "boot" },
    el("img", { src: "favicon.svg", width: 40, height: 40, alt: "" }),
    el("p", { class: "boot-name" }, PRODUCT),
  );
}

/**
 * Shown when the app does not answer at all. Started again, the app has a new address
 * unless it was given its port, and this page would ask at the old one for ever: so
 * the screen sends the owner to the address the terminal prints, and promises nothing
 * about itself.
 */
export function offlineScreen(ctx) {
  return gateScreen(
    ctx,
    "offline",
    el(
      "section",
      { class: "gate-card" },
      el("div", { class: "gate-glyph gate-glyph-amber" }, icon("warning")),
      el("h1", { class: "gate-title" }, t("offline.title")),
      el("p", { class: "gate-lead" }, t("offline.say.1")),
      button(t("offline.button.try"), { onclick: () => ctx.start() }),
      cardFoot(t("offline.say.2")),
    ),
  );
}

// -- pairing ------------------------------------------------------------------------

function tidyCode(text) {
  const letters = text.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  return letters.length > 4 ? `${letters.slice(0, 4)}-${letters.slice(4)}` : letters;
}

/** Whether all eight characters of the code are typed. */
function complete(code) {
  return code.replace("-", "").length === 8;
}

async function pair(ctx) {
  const form = ctx.ui.pair;
  const code = form.code;
  // A code that was refused is not sent again as it stands: five wrong codes switch pairing off.
  if (form.busy || !complete(code) || code === form.refused) return;
  form.busy = true;
  form.wrong = false;
  form.failed = null;
  ctx.render();
  try {
    await api.pair(code);
    form.code = "";
    form.refused = "";
    form.busy = false;
    form.expired = false;
    await ctx.start();
  } catch (error) {
    form.busy = false;
    if (error.code === "pairing_disabled") form.disabled = true;
    else if (error.code === "wrong_code") {
      // Nothing is emptied: the owner corrects what is there.
      form.refused = code;
      form.wrong = form.code === code;
    } else form.failed = error;
    ctx.render();
  }
}

export function pairingScreen(ctx) {
  const form = ctx.ui.pair;
  const error = form.wrong ? t("pair.error.wrong") : form.failed ? errorText(form.failed) : "";
  return gateScreen(
    ctx,
    "pair",
    el(
      "form",
      {
        class: "gate-card",
        autocomplete: "off",
        onsubmit: (event) => {
          event.preventDefault();
          pair(ctx);
        },
      },
      el("h1", { class: "gate-title" }, t("pair.title")),
      form.expired ? note("plain", t("pair.error.expired")) : null,
      el("p", { class: "gate-lead", id: "pair-lead" }, t("pair.say.1")),
      form.disabled
        ? note("red", t("pair.error.disabled"))
        : [
            field({
              label: t("pair.label.code"),
              id: "pair-code",
              error,
              control: el("input", {
                type: "text",
                id: "pair-code",
                class: "input input-code",
                value: form.code,
                placeholder: "XXXX-XXXX",
                maxlength: 9,
                autocomplete: "off",
                autocapitalize: "characters",
                spellcheck: "false",
                autofocus: true,
                "aria-describedby": error ? "pair-code-error" : "pair-lead",
                "aria-invalid": error ? "true" : null,
                oninput: (event) => {
                  form.code = tidyCode(event.target.value);
                  form.failed = null;
                  // The code that was refused is as wrong when it stands there again.
                  form.wrong = form.code === form.refused && form.refused !== "";
                  ctx.render();
                  // The eighth character sends the code: "Pair" is no press of its own on the way in.
                  pair(ctx);
                },
              }),
            }),
            // For a code that was corrected after a refusal, and for a request that did not arrive.
            button(form.busy ? t("pair.button.busy") : t("pair.button.pair"), {
              kind: "primary",
              type: "submit",
              block: true,
              large: true,
              busy: form.busy,
              disabled: !complete(form.code) || form.code === form.refused,
            }),
          ],
      cardFoot(t("pair.say.2")),
    ),
  );
}

// -- the locked vault ---------------------------------------------------------------

function vaultName(path) {
  const parts = String(path || "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : "vault";
}

async function unlock(ctx) {
  const form = ctx.ui.unlock;
  if (form.busy || !form.passphrase) return;
  form.busy = true;
  form.failed = null;
  ctx.render();
  try {
    const state = await api.unlock(form.passphrase);
    form.passphrase = "";
    form.busy = false;
    form.waited = 0;
    // Unlocked here, a vault whose view is Basic opens on the Basic page, whatever the address named.
    if (state.interface && state.interface.view === "basic") ctx.navigate("basic");
    ctx.applyState(state);
    await ctx.refresh();
  } catch (error) {
    form.busy = false;
    form.passphrase = "";
    form.failed = error;
    form.shake = (form.shake || 0) + 1;
    ctx.render();
    const input = document.getElementById("unlock-passphrase");
    if (input) input.focus();
  }
}

export function lockedScreen(ctx) {
  const state = ctx.state;
  const form = ctx.ui.unlock;
  const record = state.record;
  // The figures of the sealed files and of the record, and the sentence that explains them,
  // stand only where the view that is kept is Pro: the everyday view shows no figure here.
  const pro = !(state.interface && state.interface.view === "basic");
  // What waited when the vault locked is said by a page that saw it lock, in either view.
  const waited = form.waited || 0;
  const error = form.failed ? errorText(form.failed, { wrong_passphrase: "locked.error.wrong", busy: "common.error.busy" }) : "";
  return gateScreen(
    ctx,
    "locked",
    el(
      "form",
      {
        class: ["gate-card", form.shake ? `shake-${form.shake % 2}` : null],
        autocomplete: "off",
        onsubmit: (event) => {
          event.preventDefault();
          unlock(ctx);
        },
      },
      el("div", { class: "gate-glyph gate-glyph-sealed" }, icon("lock")),
      el("h1", { class: "gate-title" }, t("locked.title")),
      el(
        "div",
        { class: "vault-id" },
        el("span", { class: "vault-name" }, state.demo ? t("locked.label.demo") : vaultName(state.store.path)),
        el("span", { class: "vault-path" }, pathText(state.store.path, !state.demo)),
      ),
      pro
        ? el(
            "dl",
            { class: "seal-facts", key: "facts" },
            el("div", null, el("dt", null, t("locked.label.files")), el("dd", null, num(state.store.sealed_files))),
            el("div", null, el("dt", null, t("locked.label.size")), el("dd", null, bytes(state.store.sealed_bytes))),
            record && record.head_seq
              ? el("div", null, el("dt", null, t("locked.label.last")), el("dd", { class: "mono" }, `#${record.head_seq} ${record.head_hash}`))
              : null,
          )
        : null,
      waited > 0 ? el("p", { class: "note", id: "locked-waiting", key: "waited" }, t("locked.status.waited", { n: waited })) : null,
      demoPassphraseNote(state.demo_passphrase, () => {
        form.passphrase = state.demo_passphrase;
        form.failed = null;
        ctx.render();
        const input = document.getElementById("unlock-passphrase");
        if (input) input.focus();
      }),
      field({
        label: t("locked.label.passphrase"),
        id: "unlock-passphrase",
        error,
        control: el("input", {
          type: "password",
          id: "unlock-passphrase",
          class: "input input-large",
          value: form.passphrase,
          autocomplete: "current-password",
          autofocus: true,
          required: true,
          "aria-invalid": error ? "true" : null,
          "aria-describedby": error ? "unlock-passphrase-error" : null,
          oninput: (event) => {
            form.passphrase = event.target.value;
            ctx.render();
          },
        }),
      }),
      button(form.busy ? t("locked.button.busy") : t("locked.button.unlock"), { kind: "primary", type: "submit", block: true, large: true, busy: form.busy, disabled: !form.passphrase }),
      cardFoot(t("locked.say.1"), pro ? t("locked.say.2") : null),
    ),
  );
}

// -- the passphrase of a new vault ----------------------------------------------------

async function createVault(ctx) {
  const form = ctx.ui.create;
  if (form.busy) return;
  form.busy = true;
  form.failed = null;
  ctx.render();
  try {
    // A language the owner pressed here or on the pairing screen goes along; none is chosen for them.
    const state = await api.createStore(form.passphrase, chosenLanguage());
    form.passphrase = "";
    form.again = "";
    form.busy = false;
    form.shown = false;
    // A new vault whose view is Basic goes on to the step that adds an app. One that was
    // started with the view Pro opens on Overview, where "Getting started" takes over.
    ctx.navigate(state.interface && state.interface.view === "basic" ? "setup/app" : "overview");
    ctx.applyState(state);
    await ctx.refresh();
  } catch (error) {
    form.busy = false;
    form.failed = error;
    ctx.render();
  }
}

/** There is no vault yet: the owner chooses the passphrase that will open it. */
export function createScreen(ctx) {
  const form = ctx.ui.create;
  const length = Array.from(form.passphrase).length;
  const longEnough = length >= 12;
  const same = form.passphrase === form.again;
  const mismatch = form.again.length > 0 && !same;
  return gateScreen(
    ctx,
    "create",
    el(
      "form",
      {
        class: "gate-card",
        autocomplete: "off",
        onsubmit: (event) => {
          event.preventDefault();
          if (longEnough && same) createVault(ctx);
        },
      },
      el("h1", { class: "gate-title" }, t("create.title")),
      field({
        label: t("create.label.passphrase"),
        id: "new-passphrase",
        hint: t(longEnough ? "create.status.long" : "create.status.short", { n: length }),
        // It cannot be recovered, so it may be looked at while it is chosen.
        action: button(t(form.shown ? "create.button.hide" : "create.button.show"), {
          kind: "quiet",
          small: true,
          id: "new-passphrase-show",
          pressed: form.shown ? "true" : "false",
          label: t(form.shown ? "create.label.hide" : "create.label.show"),
          onclick: () => {
            form.shown = !form.shown;
            ctx.render();
          },
        }),
        control: el("input", {
          type: form.shown ? "text" : "password",
          id: "new-passphrase",
          class: "input input-large",
          value: form.passphrase,
          autocomplete: "new-password",
          autofocus: true,
          "aria-describedby": "new-passphrase-hint",
          oninput: (event) => {
            form.passphrase = event.target.value;
            ctx.render();
          },
        }),
      }),
      field({
        label: t("create.label.again"),
        id: "new-passphrase-again",
        error: mismatch ? t("create.error.mismatch") : "",
        control: el("input", {
          type: form.shown ? "text" : "password",
          id: "new-passphrase-again",
          class: "input input-large",
          value: form.again,
          autocomplete: "new-password",
          "aria-invalid": mismatch ? "true" : null,
          "aria-describedby": mismatch ? "new-passphrase-again-error" : null,
          oninput: (event) => {
            form.again = event.target.value;
            ctx.render();
          },
        }),
      }),
      // What the passphrase is for, and that nobody can reset it.
      note("amber", [el("span", null, t("create.say.1")), " ", el("span", null, t("create.say.2"))]),
      form.failed ? note("red", errorText(form.failed)) : null,
      button(form.busy ? t("create.button.busy") : t("create.button.create"), {
        kind: "primary",
        type: "submit",
        block: true,
        large: true,
        busy: form.busy,
        disabled: !(longEnough && same),
      }),
      el("p", { class: "gate-where" }, t("create.status.where", { path: ctx.state.store.path })),
    ),
  );
}

// -- the first run ------------------------------------------------------------------

const STEPS = ["Passphrase", "App", "Documents", "First question"];

/**
 * The four parts of setting up, with the one that is on screen marked. `done` says
 * for each part whether it is true of the vault: such a part carries a check, and
 * one that was skipped keeps its number.
 */
function stepper(current, done = []) {
  return el(
    "ol",
    { class: "steps", "aria-label": "Setting up" },
    STEPS.map((label, index) => {
      const here = index + 1 === current;
      const checked = done[index] === true && !here;
      return el(
        "li",
        { class: ["step", here && "is-current", checked && "is-done"], "aria-current": here ? "step" : null },
        el("span", { class: "step-dot" }, checked ? icon("check") : String(index + 1)),
        el("span", { class: "step-label" }, label, checked ? el("span", { class: "sr-only" }, " (done)") : null),
      );
    }),
  );
}

// -- setting up: what the three steps share -----------------------------------------

function freshAsk(app = "") {
  return {
    // The app the step "First question" is about, and the answer that says how it is connected.
    app,
    connect: null,
    // The last look at the app's settings file: what was found, or why the look failed.
    check: null,
    checkError: "",
    checking: false,
    // What is unfolded: the text without a look, the other way to add it, the help, the steps after the first request.
    all: false,
    other: false,
    help: false,
    steps: false,
    // Whether this page saw the app before its first request, and when that request then came.
    sawNone: false,
    first: "",
  };
}

/**
 * What the page remembers while a step of setting up is on screen. None of it is
 * kept anywhere but in this page: a reload starts the step again from what the vault
 * says.
 */
export function freshFirstRun() {
  return {
    // The app step. No app is chosen for the owner, and nothing is stored before the press that adds one.
    setup: { choice: null, hosted: true, destination: "", name: "", web: false, sentenceShown: false, busy: false, error: "", taken: "" },
    // Across the steps: whether the stepper is drawn, the app this page added, the sample letter it added.
    steps: { guided: null, added: "", sampleBusy: false, sample: null },
    ask: freshAsk(),
    // The owner's last answer to a waiting passage, as far as this page saw it.
    lastDecision: null,
    setupSaid: false,
  };
}

/** A step of setting up opens: its form starts empty, and whether the stepper is drawn is decided anew. */
export function openStep(ctx) {
  ctx.ui.setup = freshFirstRun().setup;
  ctx.ui.steps.guided = null;
  // What a look at a settings file found was true of the moment of the look.
  const ask = ctx.ui.ask;
  ask.check = null;
  ask.checkError = "";
  ask.all = false;
  ask.help = false;
  ask.steps = false;
  // The focus starts on the title, so that no key ticks a row or presses a button by accident.
  ctx.ui.focusId = "setup-title";
}

/**
 * Which app the step "First question" is about: the one the address names; else the
 * one this page added; else the first that has never asked, one that has access
 * before one that has none; else the first. The list comes in the order of the
 * names and says of no app when it was added.
 */
export function stepApp(ctx) {
  const apps = ctx.data.apps ? ctx.data.apps.apps : [];
  const named = (name) => (name ? apps.find((app) => app.name === name) : null);
  const silent = apps.filter((app) => !app.last_seen);
  const chosen = named(ctx.setupApp) || named(ctx.ui.steps.added) || silent.find((app) => Object.keys(app.grants || {}).length > 0) || silent[0] || apps[0];
  return chosen ? chosen.name : "";
}

/** The readable name of a listed program as it stands inside a sentence: "the ChatGPT desktop app", "Claude Desktop". */
function inSentence(label) {
  if (!label) return "the app";
  return / app$/.test(label) ? `the ${label}` : label;
}

/** The stepper of a step, while setting up was open when the step was opened. */
function stepsHead(ctx, current) {
  if (!ctx.ui.steps.guided) return null;
  const facts = setupFacts(ctx) || { app: false, document: false, asked: false };
  return stepper(current, [true, facts.app, facts.document, facts.asked]);
}

function stepTitle(text) {
  return el("h1", { class: "gate-title", id: "setup-title", tabindex: -1 }, text);
}

/** A step whose lists are still on their way. Its title takes no focus: the one of the step itself will. */
function stepWaits(ctx, key, current, text) {
  return el("section", { class: "gate-card", key }, stepsHead(ctx, current), el("h1", { class: "gate-title" }, text), loadingBlock());
}

/** The one sentence of a step that cannot be taken in this vault. It takes the focus in place of a title. */
function stepOnly(text) {
  return el("p", { class: "gate-lead", id: "setup-title", tabindex: -1 }, text);
}

/** A sentence in amber, with its sign. */
function amber(id, text) {
  return el("p", { class: "note note-amber", id, key: id, role: "note" }, icon("warning"), el("span", { class: "note-text" }, text));
}

/** What an app may do with one group, as the app said it; "" when it has no access to it. */
function accessTo(app, group) {
  const grants = app.grants || {};
  return Object.prototype.hasOwnProperty.call(grants, group) && typeof grants[group] === "string" ? grants[group] : "";
}

/** How many documents the vault holds. */
function documentsHeld(ctx) {
  return ctx.state.encryption ? ctx.state.encryption.documents : 0;
}

// -- step "App" ---------------------------------------------------------------------

/** The owner chose a row. The form takes what the list says about that app; nothing is sent. */
function chooseApp(ctx, entry) {
  const form = ctx.ui.setup;
  form.choice = entry.id;
  form.name = entry.name;
  form.hosted = entry.hosted;
  form.destination = entry.destination;
  form.error = "";
  form.taken = "";
  form.sentenceShown = false;
  ctx.render();
}

/**
 * The sentence that says where the text goes. Before an app is chosen and described it
 * is the one that holds for every app that sends text to a provider. Once it is the
 * sentence of the chosen app, the press that gives that app access can count.
 */
function firstSentence(form, described) {
  let text = GENERAL_SENTENCE;
  if (described) {
    text = basicGrantSentence(described);
    form.sentenceShown = true;
  }
  return amber("first-sentence", text);
}

/**
 * "Let {name} ask me each time": add the app, open the vault's one group to its kind
 * where that is needed, and give it "Ask me each time" for that group. The access is
 * that one for every app; the first run offers no other.
 */
async function addFirstApp(ctx) {
  const form = ctx.ui.setup;
  if (!form.sentenceShown) return;
  const data = ctx.data.apps;
  if (form.busy || !data) return;
  const group = data.collections.length ? data.collections[0].name : FIRST_GROUP;
  const host = form.choice;
  form.busy = true;
  form.error = "";
  form.taken = "";
  ctx.render();
  let app;
  try {
    const body = { name: form.name, hosted: form.hosted };
    if (form.destination.trim()) body.destination = form.destination.trim();
    app = (await api.addApp(body)).app;
  } catch (error) {
    // "Nothing was changed." when the owner closed the sheet that asks for the passphrase.
    form.busy = false;
    form.error = error.message;
    form.taken = error.code === "conflict" ? form.name : "";
    ctx.render();
    return;
  }
  try {
    const body = { name: group };
    if (app.hosted) body.allow_hosted = true;
    await api.setCollection(body);
    await api.setGrant(app.name, group, "approve");
  } catch (error) {
    const why = error.code === "cancelled" ? "." : `: ${error.message}`;
    toast(`${app.name} was added, but it was not given your documents${why}`, "red");
  }
  // How the app is connected is asked for ahead, for the step "First question".
  ctx.ui.steps.added = app.name;
  ctx.ui.ask = freshAsk(app.name);
  ctx.ui.ask.connect = { app: app.name, host, data: null, loading: false, error: "" };
  loadConnect(ctx.ui.ask.connect, () => ctx.render());
  const next = ctx.ui.steps.guided && documentsHeld(ctx) === 0 ? "setup/documents" : `setup/connect?app=${encodeURIComponent(app.name)}`;
  form.busy = false;
  ctx.navigate(next);
  await ctx.refresh();
}

function appStep(ctx) {
  const data = ctx.data.apps;
  const settings = ctx.data.settings;
  const form = ctx.ui.setup;
  const guided = ctx.ui.steps.guided;
  if (!data || !settings) return stepWaits(ctx, "step-3-wait", 2, "Which AI app do you use?");
  if (data.collections.length > 1 || data.apps.some((app) => !app.hosted)) {
    // Here one value would not say what a new app may do, or adding one could change which kinds of apps are answered.
    return el(
      "section",
      { class: "gate-card", key: "step-3-pro" },
      stepOnly("Apps are added in Pro for this vault: it has several groups of documents, or an app that runs on this computer."),
      el("p", null, el("a", { class: "link", href: "#/apps" }, "Open Apps in Pro")),
    );
  }
  const known = data.known;
  const group = data.collections.length ? data.collections[0] : null;
  // "Another app" is offered while the vault has no app: afterwards its kind could change which apps are answered.
  const entries = known.filter((entry) => entry.local_program && (entry.id !== "generic" || data.apps.length === 0));
  const listed = form.choice !== null && form.choice !== "generic";
  const ready = form.choice !== null && !appFormProblem(form, known);
  // Every draw starts without the sentence of an app; only drawing it says that it is shown.
  form.sentenceShown = false;
  let described = null;
  if (form.choice !== null && !form.hosted) described = { name: form.name, hosted: false, destination: "" };
  else if (ready) described = { name: form.name, hosted: true, destination: form.destination.trim() };
  const missing = (ctx.state.warnings || []).find((warning) => warning.code === "no_sdk" || warning.code === "python_too_old");
  const leave = () => ctx.navigate(guided && documentsHeld(ctx) === 0 ? "setup/documents" : "basic");
  return el(
    "form",
    {
      class: "gate-card",
      key: "step-3",
      autocomplete: "off",
      "aria-labelledby": "setup-title",
      onsubmit: (event) => {
        event.preventDefault();
        if (ready && !form.busy) addFirstApp(ctx);
      },
    },
    stepsHead(ctx, 2),
    stepTitle("Which AI app do you use?"),
    el("p", { class: "gate-lead" }, "It gets a way to ask this vault for text, under this rule:"),
    el(
      "div",
      { class: "setup-rule", key: "first-rule", id: "first-rule" },
      el(
        "p",
        null,
        el("strong", null, "Ask me each time."),
        " It can search your documents and learns which passages contain its words, not their text. Every passage waits for your approval, with the exact text shown. Each yes is a visit to this page, and the app has to ask again.",
      ),
      el("p", { class: "setup-rule-small" }, `A passage is one piece of a document, at most ${num(settings.ceilings.passage_chars)} characters.`),
    ),
    el(
      "div",
      { class: "choice-list", role: "radiogroup", "aria-labelledby": "setup-title", "aria-describedby": "first-rule" },
      entries.map((entry) =>
        el(
          "label",
          { class: ["choice", form.choice === entry.id && "is-selected"], key: entry.id },
          el("input", {
            type: "radio",
            name: "first-app-known",
            value: entry.id,
            checked: form.choice === entry.id,
            disabled: form.busy,
            onchange: () => chooseApp(ctx, entry),
          }),
          el(
            "span",
            { class: "choice-text" },
            el("span", { class: "choice-label" }, entry.label),
            el("span", { class: "choice-hint" }, entry.id === "generic" ? "You say where its text goes" : `Sends to ${entry.destination}`),
          ),
        ),
      ),
    ),
    data.apps.length
      ? el(
          "p",
          { class: "setup-aside", id: "first-other", key: "first-other" },
          "Another app is added in Pro, where you say where its text goes. ",
          el("a", { class: "link", href: "#/apps" }, "Open Apps in Pro"),
        )
      : null,
    el(
      "div",
      { class: "setup-web", key: "first-web" },
      button("ChatGPT, Claude or Grok on the web or on a phone?", {
        kind: "quiet",
        small: true,
        id: "first-web",
        expanded: form.web ? "true" : "false",
        controls: "first-web-note",
        onclick: () => {
          form.web = !form.web;
          ctx.render();
        },
      }),
      form.web
        ? el(
            "p",
            { class: "setup-aside", id: "first-web-note" },
            `Those cannot use this vault. They call only servers that their company's cloud can reach, and ${PRODUCT} runs on this computer only. Use the same company's desktop app instead.`,
          )
        : null,
    ),
    form.choice === "generic" ? el("div", { class: "setup-fields", key: "first-fields" }, otherAppFields(form, () => ctx.render(), "first-app")) : null,
    // What follows a choice is read out when it changes.
    el(
      "div",
      { class: "setup-said", key: "first-said", "aria-live": "polite" },
      listed
        ? el(
            "p",
            { id: "first-listed", key: "first-listed" },
            "It will be listed under the name ",
            el("span", { class: "mono" }, form.name),
            ". The name is a label you give this connection.",
          )
        : null,
      firstSentence(form, described),
      form.choice !== null && form.hosted && group && !group.allow_hosted
        ? el("p", { class: "setup-aside", id: "first-opens", key: "first-opens" }, "This also opens your documents to apps that send text to a provider. No other app gets access by that.")
        : null,
      missing ? amber("first-missing", `One thing is missing before an AI app can connect: ${missing.text}`) : null,
      form.error
        ? el(
            "p",
            { class: "field-error", id: "first-error", key: "first-error", role: "alert" },
            form.error,
            form.taken
              ? [
                  " ",
                  button("Show its connection steps", {
                    small: true,
                    id: "first-existing",
                    onclick: () => ctx.navigate(`setup/connect?app=${encodeURIComponent(form.taken)}`),
                  }),
                ]
              : null,
          )
        : null,
    ),
    el(
      "div",
      { class: "gate-actions" },
      button(guided ? "Skip" : "Cancel", { kind: "quiet", onclick: leave }),
      button(form.busy ? "Adding…" : ready ? `Let ${form.name} ask me each time` : "Choose an app", {
        kind: "primary",
        type: "submit",
        id: "first-add",
        disabled: !ready,
        busy: form.busy,
      }),
    ),
  );
}

// -- step "Documents" ---------------------------------------------------------------

/**
 * Add the invented sample letter to the vault. It is sent here and by nothing else,
 * on the press of its own button, so it is never among the files the owner chose.
 */
async function addSample(ctx, group) {
  const steps = ctx.ui.steps;
  if (steps.sampleBusy) return;
  steps.sampleBusy = true;
  ctx.render();
  try {
    const answer = await api.addDocument(group, SAMPLE.file, SAMPLE.text);
    steps.sample = { group, passages: (answer.document || {}).passages || 0 };
  } catch (error) {
    toast(error.code === "duplicate" ? "The sample letter is in the vault already." : `The sample letter was not added: ${error.message}`, "red");
  }
  steps.sampleBusy = false;
  await ctx.refresh();
}

function documentsStep(ctx) {
  const data = ctx.data.documents;
  const apps = ctx.data.apps;
  const steps = ctx.ui.steps;
  if (!data || !apps) return stepWaits(ctx, "step-2-wait", 3, "Add a document");
  const groups = data.collections;
  if (groups.length > 1) {
    return el(
      "section",
      { class: "gate-card", key: "step-2-several" },
      stepOnly("This vault has several groups of documents. Add documents on the Basic page or in Pro."),
      el("div", { class: "gate-actions" }, button("Open my vault", { kind: "primary", onclick: () => ctx.navigate("basic") })),
    );
  }
  const group = groups.length ? groups[0].name : FIRST_GROUP;
  const held = groups.length ? groups[0].documents : [];
  const total = held.length;
  const withAccess = apps.apps.filter((app) => accessTo(app, group));
  const senders = withAccess.filter((app) => app.hosted);
  // The files chosen on this page that are not on their way any more.
  const chosen = sessionFiles(ctx, group);
  const settled = chosen.length > 0 && chosen.every((item) => item.status === "done" || item.status === "error");
  const refused = total === 0 && settled && chosen.every((item) => item.status === "error");
  const sample = steps.sample && steps.sample.group === group && held.some(isSample) ? steps.sample : null;
  const next = () => {
    if (!apps.apps.length) ctx.navigate("basic");
    else ctx.navigate(apps.apps.some((app) => app.name === steps.added) ? `setup/connect?app=${encodeURIComponent(steps.added)}` : "setup/connect");
  };
  return el(
    "section",
    { class: "gate-card", key: "step-2", "aria-labelledby": "setup-title" },
    stepsHead(ctx, 3),
    stepTitle("Add a document"),
    el(
      "p",
      { class: "gate-lead" },
      "Start with one you would ask your AI app about: a letter or a contract that you have as a PDF with text in it, or a text file. A scan, a photo or a Word file cannot be read.",
    ),
    el(
      "div",
      { class: "setup-who", key: "docs-who" },
      withAccess.length
        ? withAccess.map((app) =>
            el(
              "p",
              { class: "gate-lead" },
              accessTo(app, group) === "approve"
                ? `${app.name} can then search it and ask for passages; each passage waits for your approval.`
                : `${app.name} can then search it and read passages of it without asking, inside its limits.`,
            ),
          )
        : el("p", { class: "gate-lead" }, apps.apps.length ? "No app can ask for it yet." : "No app can ask for it until you add one."),
    ),
    senders.length ? amber("docs-sentence", senders.length === 1 ? basicGrantSentence(senders[0]) : GENERAL_SENTENCE) : null,
    // The filled button of the step: "Choose files" while the vault holds no document.
    dropZone(ctx, group, { large: true, who: true, filled: total === 0 && !refused }),
    refused
      ? el(
          "p",
          { class: "setup-aside", id: "docs-refused", key: "docs-refused", role: "status" },
          "None of those files could be added. You can go on with an invented sample letter and add your own documents later.",
        )
      : null,
    held.some(isSample)
      ? null
      : el(
          "p",
          { class: "setup-sample", key: "sample-offer" },
          el("span", null, "No file at hand?"),
          button(steps.sampleBusy ? "Adding…" : "Add an invented sample letter", {
            kind: refused ? "primary" : "default",
            small: true,
            id: "sample-add",
            busy: steps.sampleBusy,
            onclick: () => addSample(ctx, group),
          }),
        ),
    sample
      ? el(
          "ul",
          { class: "upload-list", key: "sample-said", "aria-label": "The sample letter" },
          el(
            "li",
            { class: "upload upload-done", key: "sample" },
            icon("check"),
            el(
              "span",
              { class: "upload-text" },
              el(
                "span",
                { class: "upload-note" },
                `Added: an invented sample letter (${count(sample.passages, "passage", "passages")}). Its text says that it is invented. You can remove it at any time.`,
              ),
            ),
          ),
          // Said once: the line under the files the owner chose says the same.
          chosen.some((item) => item.status === "done") ? null : whoLine(ctx, group),
        )
      : null,
    el(
      "div",
      { class: "gate-actions" },
      total ? null : button("Skip", { kind: "quiet", onclick: next }),
      button(total ? `Continue with ${count(total, "document", "documents")}` : "Continue", { kind: "primary", disabled: !total, onclick: next }),
    ),
  );
}

// -- step "First question" ----------------------------------------------------------

/** The name of the protocol, which no screen of setting up shows. A title that holds it is said in other words. */
const NAMES_PROTOCOL = /\bMCP\b|Model Context Protocol/;
/** The address of a page somewhere else, which no screen of setting up shows either. */
const HOLDS_ADDRESS = /\w:\/\S/;

/** What this page remembers of the step for one app. The answer that says how the app is connected is asked for once. */
function askOf(ctx, name, host) {
  if (ctx.ui.ask.app !== name) ctx.ui.ask = freshAsk(name);
  const ask = ctx.ui.ask;
  if (!ask.connect) {
    ask.connect = { app: name, host, data: null, loading: false, error: "" };
    loadConnect(ask.connect, () => ctx.render());
  }
  return ask;
}

/** A text of several lines to copy: the text in a box that scrolls inside itself, the button beside it. */
function copyBlock(ctx, id, text, what) {
  return el(
    "div",
    { class: "copy-block", key: id },
    el("pre", { class: "code ask-text", tabindex: 0, "aria-label": "Text to copy" }, el("code", null, text)),
    copyButton(ctx, id, text, what),
  );
}

/** The place of a file, with a button that copies it. */
function pathLine(ctx, path) {
  return el("div", { class: "ask-path", key: "ask-path" }, pathText(path), copyButton(ctx, "ask-copy-path", path, "the place of the file", "Copy the path"));
}

/** Look at the settings file of the app, on this press and at no other time. */
async function checkFile(ctx, ask, name, host) {
  if (ask.checking) return;
  ask.checking = true;
  ask.all = false;
  ctx.render();
  try {
    ask.check = await api.checkConnect(name, host);
    ask.checkError = "";
  } catch (error) {
    ask.check = null;
    ask.checkError = error.message;
  }
  ask.checking = false;
  // What was found is what the owner reads next.
  if (ctx.ui.ask === ask) ctx.ui.focusId = "ask-found";
  ctx.render();
}

/** The name the entry has in the other program's settings, read from the text the app sends for it. */
function entryName(block) {
  if (block.language === "toml") return block.text.split("\n")[0].trim();
  const found = block.inner ? /^"([^"]+)"/.exec(block.inner.entry) : null;
  return found ? found[1] : "";
}

/** The button that looks at the file. It is the filled one only before the first look, while no request has come. */
function checkButton(ctx, ask, app, connect, filled) {
  const again = ask.check !== null || ask.checkError !== "";
  return button(ask.checking ? "Checking…" : again ? "Check again" : "Check the file", {
    kind: filled && !again ? "primary" : "default",
    id: "ask-check",
    key: "ask-check",
    busy: ask.checking,
    onclick: () => checkFile(ctx, ask, app.name, connect.host),
  });
}

/** The whole text for the settings file and, for a file that may hold other settings, the entry alone. */
function wholeText(ctx, block) {
  return [
    el("p", { class: "ask-label", key: "text-label" }, "The text"),
    copyBlock(ctx, "ask-copy-text", block.text, "the text for the settings file"),
    block.inner
      ? [
          el("p", { key: "entry-label" }, "If the file holds other settings, add this inside “mcpServers”:"),
          copyBlock(ctx, "ask-copy-entry", block.inner.entry, "the entry alone"),
        ]
      : null,
  ];
}

/**
 * What to do after a look at the file, as steps a, b and c: open the file, put the
 * text into it, look again. Nothing here is done for the owner, and no sentence says
 * what another program does: the file is opened in whatever plain-text editor the
 * owner has.
 */
function fileSteps(ctx, ask, app, connect, block, check) {
  const state = check.state;
  const toml = block.language === "toml";
  const open = el("li", { key: "a" }, el("p", null, state === "no_file" ? "Make this file in a plain-text editor:" : "Open this file in a plain-text editor:"), pathLine(ctx, check.path));
  const again = el("li", { key: "c" }, checkButton(ctx, ask, app, connect, false));
  const text = copyBlock(ctx, "ask-copy-text", block.text, "the text for the settings file");
  let put;
  if (state === "differs") {
    put = toml
      ? [el("p", null, "Replace that section, down to the next line that starts with “[”, with this:"), text]
      : [el("p", null, "Replace that entry with this one:"), copyBlock(ctx, "ask-copy-entry", block.inner.entry, "the entry alone")];
    put.push(el("p", null, "Then save the file."));
  } else if (state === "other" && !toml) {
    const section = check.section;
    if (section === "filled") {
      put = [el("p", null, "Inside “mcpServers”, after its last entry, type a comma, then paste this:"), copyBlock(ctx, "ask-copy-entry", block.inner.entry, "the entry alone")];
    } else if (section === "empty") {
      put = [el("p", null, "Between the braces after “mcpServers”, paste this:"), copyBlock(ctx, "ask-copy-entry", block.inner.entry, "the entry alone")];
    } else {
      put = [el("p", null, "Right after the first “{”, paste this, then type a comma:"), copyBlock(ctx, "ask-copy-entry", block.inner.section, "the section alone")];
    }
    put.push(el("p", null, "Then save the file."));
  } else if (state === "no_file") {
    put = [el("p", null, "Put the text below into it and save it."), text];
  } else if (toml) {
    // A section is added at the end of such a file, so nothing in it has to be merged.
    put = [el("p", null, "Put the text below at the very end of it, on a new line, and save it."), text];
  } else {
    put = [el("p", null, "Replace everything in it with the text below and save it."), text];
  }
  return el("ol", { class: "ask-abc", key: "ask-abc" }, open, el("li", { key: "b" }, put), again);
}

/** Each block as the app sends it, for an app whose settings file this page cannot look at. */
function plainBlocks(ctx, connect) {
  return [
    connect.blocks.map((block, index) =>
      el(
        "div",
        { class: "ask-block", key: `block-${index}` },
        el("p", { class: "ask-label" }, NAMES_PROTOCOL.test(block.title) ? "The text, in the form most apps of this kind read" : block.title),
        copyBlock(ctx, `ask-copy-block-${index}`, block.text, "the text"),
        block.where_here ? el("p", { class: "ask-where" }, "Where to put it: ", pathText(block.where_here)) : null,
      ),
    ),
    // The notes of the app, without the one that names a page of documentation.
    (connect.notes || [])
      .filter((text) => !HOLDS_ADDRESS.test(text) && !NAMES_PROTOCOL.test(text))
      .map((text, index) => el("p", { class: "setup-aside", key: `note-${index}` }, text)),
  ];
}

const OUTSIDE = "This part happens outside this page. Nothing is changed for you: you copy the text and put it where the app reads it. The text holds no passphrase and no key.";

/** What the check found, in one sentence, or "" before a look. */
function foundWords(check, label, block) {
  const kind = block.language === "toml" ? "TOML" : "JSON";
  const name = entryName(block);
  switch (check.state) {
    case "present":
      return `✓ ${PRODUCT} is in the settings file of ${label}.`;
    case "no_folder":
      return `The settings folder of ${label} is not on this computer: ${check.folder}`;
    case "no_file":
      return "The file is not there yet.";
    case "empty":
      return "The file is there and holds no settings yet.";
    case "other":
      return "The file holds other settings. Keep them.";
    case "differs":
      return block.language === "toml"
        ? `The file already has a section “${name}”, and it points somewhere else: another vault, or an earlier installation.`
        : `The file already has an entry named “${name}”, and it points somewhere else: another vault, or an earlier installation.`;
    case "unreadable":
      return `${PRODUCT} could not read the file as ${kind}, so it cannot say what is in it. A comma or a bracket may be missing or doubled.`;
    case "not_read":
      return `${PRODUCT} does not read a settings file that is a link, or larger than 1 MiB, so it cannot say what is in it.`;
    default:
      return `${PRODUCT} cannot check this kind of file on this computer.`;
  }
}

/** Line 1 for an app with one settings file this page can have looked at. */
function guidedLine(ctx, ask, app, connect, label, filled) {
  const block = connect.blocks[0];
  const check = ask.check;
  const where = [el("p", { key: "where-label" }, `The settings file of ${label} on this computer:`), el("p", { class: "ask-where", key: "where" }, pathText(block.where_here))];
  const showAll = button("Show the text without checking", {
    kind: "quiet",
    small: true,
    id: "ask-all",
    key: "ask-all",
    expanded: ask.all ? "true" : "false",
    onclick: () => {
      ask.all = !ask.all;
      ctx.render();
    },
  });
  const all = ask.all ? el("div", { class: "ask-all", key: "ask-all-text" }, wholeText(ctx, block)) : null;
  if (ask.checkError) {
    // The look itself failed: the app's sentence stands where the result would.
    return [
      el("p", { key: "outside" }, OUTSIDE),
      where,
      el("p", { class: "ask-found", id: "ask-found", key: "ask-found", role: "status", tabindex: -1 }, ask.checkError),
      el("div", { class: "ask-check", key: "check" }, checkButton(ctx, ask, app, connect, false)),
      showAll,
      all,
    ];
  }
  if (!check) {
    return [
      el("p", { key: "outside" }, OUTSIDE),
      where,
      el(
        "div",
        { class: "ask-check", key: "check" },
        checkButton(ctx, ask, app, connect, filled),
        el("p", { class: "ask-check-note" }, `${PRODUCT} looks at that one file and tells this page only whether its own entry is there. Then this page says what to do.`),
      ),
      showAll,
      all,
    ];
  }
  const found = el("p", { class: "ask-found", id: "ask-found", key: "ask-found", role: "status", tabindex: -1 }, foundWords(check, label, block));
  if (check.state === "present") {
    return [el("p", { key: "outside" }, OUTSIDE), found, el("p", { class: "ask-where", key: "where" }, pathText(check.path)), el("div", { class: "ask-check", key: "check" }, checkButton(ctx, ask, app, connect, false)), showAll, all];
  }
  if (check.state === "no_folder") {
    return [
      el("p", { key: "outside" }, OUTSIDE),
      found,
      el("p", { key: "installed" }, `Is ${label} installed? Open it once, then check again.`),
      el("div", { class: "ask-check", key: "check" }, checkButton(ctx, ask, app, connect, false)),
      showAll,
      all,
    ];
  }
  if (check.state === "unreadable" || check.state === "not_read") {
    return [
      el("p", { key: "outside" }, OUTSIDE),
      found,
      el("p", { key: "open" }, "Open this file in a plain-text editor:"),
      pathLine(ctx, check.path),
      wholeText(ctx, block),
      el("div", { class: "ask-check", key: "check" }, checkButton(ctx, ask, app, connect, false)),
    ];
  }
  if (check.state === "unsupported") {
    return [el("p", { key: "outside" }, OUTSIDE), found, plainBlocks(ctx, connect)];
  }
  return [el("p", { key: "outside" }, OUTSIDE), found, fileSteps(ctx, ask, app, connect, block, check)];
}

/** Line 1 for an app that is told with one command line. */
function commandLine(ctx, ask, connect, label) {
  const first = connect.blocks[0];
  const second = connect.blocks[1] || null;
  const mac = connect.platform === "macos";
  return [
    el(
      "p",
      { key: "terminal" },
      mac ? `Open a new Terminal window (⌘N; the first one is busy running ${PRODUCT}), paste this line and press Return:` : "Open a new terminal window, paste this line and press Return:",
    ),
    copyLine(ctx, "ask-copy-line", first.text, "the line for the terminal", "Text to copy"),
    el(
      "p",
      { key: "tells" },
      `It tells ${label} how to start the small program that carries its requests to ${PRODUCT}. The line holds no passphrase and no key. Nothing is changed for you: you run it yourself.`,
    ),
    second
      ? button("Use a settings file instead", {
          kind: "quiet",
          small: true,
          id: "ask-other-way",
          key: "ask-other-way",
          expanded: ask.other ? "true" : "false",
          onclick: () => {
            ask.other = !ask.other;
            ctx.render();
          },
        })
      : null,
    second && ask.other
      ? el(
          "div",
          { class: "ask-block", key: "other-way" },
          el("p", { class: "ask-label" }, second.title),
          copyBlock(ctx, "ask-copy-other", second.text, "the text for the settings file"),
          second.where_here || second.where.length ? el("p", { class: "ask-where" }, "Where to put it: ", pathText(second.where_here || second.where[0])) : null,
          el("p", null, "Use one of the two, not both."),
        )
      : null,
  ];
}

/** One of the three numbered lines. */
function askStep(key, number, title, body, done = false) {
  return el(
    "li",
    { class: ["ask-step", done && "is-done"], key },
    el("span", { class: "ask-num", "aria-hidden": "true" }, done ? icon("check") : String(number)),
    el("div", { class: "ask-step-body" }, el("h2", { class: "ask-step-title" }, title), body),
  );
}

/** The three numbered lines: add the product to the app; copy the question; go back to the app and paste. */
function askSteps(ctx, ask, app, label, filled) {
  const view = ask.connect;
  const connect = view.data;
  const theApp = inSentence(label);
  if (view.error) return note("red", view.error);
  if (!connect) return loadingBlock();
  const blocked = connect.ready && connect.ready.message ? amber("ask-ready", connect.ready.message) : null;
  if (ctx.state.demo) {
    const launch = ctx.state.launch || {};
    return el(
      "ol",
      { class: "ask-steps", key: "ask-steps" },
      askStep("ask-1", 1, `Add ${PRODUCT} to ${theApp}`, [
        el("p", { key: "demo" }, "This is the demo. Its vault is deleted when the demo stops, so there is nothing to connect an app to. Start a vault of your own first:"),
        launch.command ? copyLine(ctx, "ask-copy-line", launch.command, `the command that starts ${PRODUCT} with a vault of your own`, "Text to copy") : null,
      ]),
    );
  }
  const first = connect.blocks[0] || null;
  const unsupported = Boolean(ask.check && ask.check.state === "unsupported");
  let shape = "plain";
  if (first && first.where_here && (first.language === "json" || first.language === "toml")) shape = "guided";
  else if (first && first.language === "shell" && label) shape = "command";
  let one;
  if (shape === "guided") one = guidedLine(ctx, ask, app, connect, theApp, filled);
  else if (shape === "command") one = commandLine(ctx, ask, connect, theApp);
  else one = [el("p", { key: "outside" }, OUTSIDE), plainBlocks(ctx, connect)];
  const documents = ctx.data.documents;
  const held = documents ? [].concat(...documents.collections.map((group) => group.documents)) : null;
  const sampleOnly = Boolean(held && held.length === 1 && isSample(held[0]));
  const question = sampleOnly ? `Use ${PRODUCT} to search my documents for the sample rental agreement. When does it end?` : ASK_START;
  const after = connect.after || { title: `Restart ${theApp} and paste the question`, text: "" };
  return el(
    "ol",
    { class: "ask-steps", key: "ask-steps" },
    askStep(
      "ask-1",
      1,
      `Add ${PRODUCT} to ${theApp}`,
      [blocked, one, connect.plain_note ? el("p", { class: "setup-aside", key: "plain-note" }, connect.plain_note) : null],
      shape === "guided" && !unsupported && Boolean(ask.check && ask.check.state === "present"),
    ),
    askStep("ask-2", 2, sampleOnly ? "Copy this question" : "Copy the start of your question", [
      copyLine(ctx, "ask-copy", question, sampleOnly ? "the question" : "the start of your question", "Text to copy"),
      el(
        "p",
        { key: "finish" },
        sampleOnly
          ? "Paste it into a new chat as it is."
          : `You finish the sentence in ${theApp}. Ask about something you know is in the file you added, in the file's own words: the search matches whole words.`,
      ),
      held && held.length === 0
        ? el(
            "div",
            { class: "ask-note", key: "no-document" },
            amber("ask-no-document", "There is no document in the vault yet, so a search finds nothing."),
            button("Add a document first", { kind: "quiet", small: true, id: "ask-add-doc", onclick: () => ctx.navigate("setup/documents") }),
          )
        : null,
      Object.keys(app.grants || {}).length === 0 ? amber("ask-no-access", `${app.name} has no access yet, so it is told there is nothing for it.`) : null,
    ]),
    askStep("ask-3", 3, after.title, [
      after.text ? el("p", { key: "after" }, after.text) : null,
      el("p", { key: "allow" }, `If ${theApp} asks whether it may use the ${PRODUCT} tools, allow it.`),
    ]),
  );
}

/** "Nothing arrives?": what to look at while no request has come. */
function helpList(ctx, ask, label) {
  const connect = ask.connect.data;
  const theApp = inSentence(label);
  const first = connect && connect.blocks[0];
  const guided = Boolean(first && first.where_here && (first.language === "json" || first.language === "toml")) && !(ask.check && ask.check.state === "unsupported");
  const command = Boolean(first && first.language === "shell" && label);
  const desktop = Boolean(connect && connect.after && connect.after.desktop);
  const mac = Boolean(connect && connect.platform === "macos");
  const since = parseTime(ctx.state.unlocked_at);
  // A call under a name this vault does not answer, since this unlock: the entry in the other app may carry another name.
  const stray = (ctx.stops || []).find((item) => {
    const time = parseTime(item.last_time || item.time);
    return item.app === UNANSWERED && time && since && time.getTime() > since.getTime();
  });
  let restarted = `Was ${theApp} restarted after the text was put where it reads it?`;
  if (desktop) restarted = `Was ${theApp} really quit and opened again?${mac ? " On a Mac, closing the window is not enough." : ""}`;
  else if (command) restarted = `Was a new session of ${theApp} started after the line was run?`;
  return el(
    "ul",
    { class: "ask-help-list", id: "ask-help-list", key: "ask-help-list" },
    el("li", null, restarted),
    el("li", null, `Ask it plainly: “Use the ${PRODUCT} status tool.” If ${theApp} does not know such a tool, it has not read the entry.`),
    el(
      "li",
      null,
      guided
        ? ["Press “Check the file” in step 1. It says whether the entry is in ", pathText(first.where_here), "."]
        : `The text names the place of this vault and of the program that runs ${PRODUCT}. If either has moved, copy the text again.`,
    ),
    el("li", null, `${PRODUCT} must be running and the vault unlocked. While it is locked, the app is told so.`),
    stray
      ? el(
          "li",
          null,
          `A request arrived ${at(stray.last_time || stray.time, ctx.now)} under a name this vault does not answer. The entry in the other app may carry another name.`,
        )
      : null,
  );
}

/** The passage of a released entry in words, as far as this page has learned what the reference stands for. */
function passageOf(ctx, item) {
  const known = (item.ref && ctx.passages.get(item.ref)) || {};
  const found = /\.(\d+)$/.exec(item.ref || "");
  const number = known.passage || (found ? Number(found[1]) : 0);
  const of = known.passages ? ` of ${num(known.passages)}` : "";
  const title = known.title ? `“${known.title}”` : "a document";
  return number ? `Passage ${num(number)}${of} of ${title}` : `A passage of ${title}`;
}

function connectStep(ctx, problem) {
  const data = ctx.data.apps;
  if (!data) return stepWaits(ctx, "step-4-wait", 4, "Ask your first question");
  if (!data.apps.length) {
    return el(
      "section",
      { class: "gate-card", key: "step-4-none" },
      stepOnly("No app has been added yet."),
      el("div", { class: "gate-actions" }, button("Choose your AI app", { kind: "primary", onclick: () => ctx.navigate("setup/app") })),
    );
  }
  const app = data.apps.find((entry) => entry.name === ctx.setupApp);
  // The name is settled as soon as the list is in; until then there is nothing to say about an app.
  if (!app) return stepWaits(ctx, "step-4-wait", 4, "Ask your first question");
  const name = app.name;
  const host = data.known.find((entry) => entry.id === name && entry.id !== "generic") || null;
  // The readable name of the program on the owner's computer, for instructions about that program only.
  const label = host ? host.label : "";
  const theApp = inSentence(label);
  const ask = askOf(ctx, name, host ? host.id : "generic");
  const connect = ask.connect.data;
  const approvals = ctx.data.approvals;
  const seen = app.last_seen;
  const called = Boolean(seen);
  const blocked = Boolean(connect && connect.ready && connect.ready.message);
  // The time of the first request: when this page saw it come, or the oldest entry where the app's entries are all in hand.
  if (!called) ask.sawNone = true;
  else if (ask.sawNone && !ask.first) ask.first = seen.time;
  const asked = ctx.asked && ctx.asked.app === name ? ctx.asked : null;
  const entries = asked ? asked.items : [];
  let first = ask.first;
  if (!first && asked && !asked.more && entries.length) first = entries[entries.length - 1].time;
  let status = "waiting";
  let words = `Waiting for the first request under the name ${name}. This page notices it by itself.`;
  if (blocked) {
    status = "blocked";
    words = "No request can arrive until this is fixed.";
  } else if (called) {
    status = "arrived";
    words = first ? `A first request arrived under the name ${name} ${at(first, ctx.now)}.` : `Requests have arrived under the name ${name}, the last one ${at(seen.time, ctx.now)}.`;
    // What the caller says it is can be of any length; the line has the room of two.
    if (seen.claimed) words += ` It says it is ${seen.claimed.length > 48 ? `${seen.claimed.slice(0, 47)}…` : seen.claimed}, unverified.`;
  }
  const waiting = approvals ? approvals.waiting.filter((item) => item.app === name) : [];
  const granted = approvals ? (approvals.granted || []).filter((item) => item.app === name) : [];
  const anyWaiting = approvals ? approvals.waiting.length : ctx.state.today ? ctx.state.today.approvals_waiting : 0;
  // The owner's last answer on this page. "Don't release" writes nothing a page could read back, so it is kept here.
  for (const done of ctx.ui.decided.values()) {
    const last = ctx.ui.lastDecision;
    if (done.item.app === name && (!last || last.done !== done)) ctx.ui.lastDecision = { done, app: name, release: done.release, at: Date.now() };
  }
  const decision = ctx.ui.lastDecision && ctx.ui.lastDecision.app === name ? ctx.ui.lastDecision : null;
  const released = entries.find((item) => item.kind === "release" && item.approved === true) || null;
  const releasedAt = released ? parseTime(released.time) : null;
  const kept = Boolean(decision && !decision.release && !(releasedAt && releasedAt.getTime() > decision.at));
  // Everything near the cards that this screen can change. A change of it makes "Release once" wait again.
  const all = ctx.ui.allApprovals === true;
  const around = [words, ctx.offline ? "offline" : problem, ctx.ui.copied === "bar-launch-copy" ? "copied" : "", all ? "all" : "", ctx.ui.sheets > 0 ? "sheet" : ""].join("\n");
  const drawn = approvalCards(ctx, { limit: all ? null : 3, around });
  const cards = drawn && (drawn.cards.length > 0 || drawn.granted);
  // While a passage waits no button of the screen is filled.
  const filled = anyWaiting === 0;
  const documents = ctx.data.documents;
  const held = documents ? [].concat(...documents.collections.map((group) => group.documents)) : [];
  const sampleOnly = held.length === 1 && isSample(held[0]);
  const leave = async (then) => {
    await setupDone(ctx);
    ctx.navigate("basic");
    if (then) then();
  };
  const endButtons = (seq) =>
    el(
      "div",
      { class: "ask-actions", key: "ask-actions" },
      sampleOnly
        ? button("Add a document of your own", {
            kind: filled ? "primary" : "default",
            id: "ask-own",
            onclick: () =>
              leave(() => {
                ctx.ui.focusId = `files-${documents.collections[0].name}`;
              }),
          })
        : null,
      button("Open my vault", {
        kind: filled && !sampleOnly ? "primary" : "default",
        id: "ask-open",
        onclick: () =>
          leave(() => {
            if (seq !== null) ctx.openPanel(seq);
          }),
      }),
    );
  let outcome = null;
  if (called && !waiting.length) {
    if (granted.length) {
      outcome = [
        el(
          "p",
          { class: "ask-end-title", id: "ask-end-title", key: "title", tabindex: -1 },
          `Approved once. Go back to ${theApp} and tell it: “try again”. It can read this passage until ${when(granted[0].valid_until, ctx.now)}.`,
        ),
        el("p", { class: "ask-end-wait", key: "wait" }, el("span", { class: "spinner" }), "Waiting for it to read the passage."),
      ];
    } else if (kept) {
      outcome = [
        el("h2", { class: "ask-end-title", id: "ask-end-title", key: "title", tabindex: -1 }, "Not released"),
        el("p", { key: "text" }, `Nothing from that passage left. If ${name} asks again, the passage waits here again.`),
        endButtons(null),
        launchBlock(ctx),
      ];
    } else if (released) {
      outcome = [
        el("h2", { class: "ask-end-title", id: "ask-end-title", key: "title", tabindex: -1 }, `Released after your approval ${at(released.time, ctx.now)}`),
        el(
          "p",
          { key: "text" },
          `${passageOf(ctx, released)} went to ${app.hosted ? app.destination : "the app"}: ${count(released.characters, "character", "characters")}. It is in the record with its exact text.`,
        ),
        endButtons(released.seq),
        launchBlock(ctx),
      ];
    } else {
      outcome = [el("p", { class: "ask-end-title", id: "ask-end-title", key: "title", tabindex: -1 }, "When it asks for a passage, the exact text appears here, under “Approvals”."), endButtons(null)];
    }
  }
  // After the first request the three lines have done their work and fold away.
  const folded = called && !blocked;
  const open = !folded || ask.steps;
  const search = entries.some((item) => item.kind === "release" && item.tool === "search");
  const modes = Object.keys(app.grants || {}).map((group) => accessTo(app, group));
  const asksEachTime = modes.length > 0 && modes.every((mode) => mode === "approve");
  const newest = entries.length ? entries[0] : null;
  const nothingFound = Boolean(newest && newest.kind === "release" && newest.tool === "search" && /: no results$/.test(newest.summary || ""));
  return el(
    "section",
    { class: "gate-card gate-wider", key: "step-4", "aria-labelledby": "setup-title" },
    stepsHead(ctx, 4),
    stepTitle("Ask your first question"),
    el("p", { class: "connect-sub" }, connectSub(name)),
    el(
      "p",
      { class: ["ask-wait", `ask-${status}`], key: "ask-wait", role: "status" },
      el("span", { class: "ask-wait-glyph" }, status === "waiting" ? el("span", { class: "spinner" }) : icon(status === "blocked" ? "warning" : "check")),
      el("span", { class: "ask-wait-text" }, words),
    ),
    el("p", { class: "sr-only", role: "status", key: "said" }, waitingSaid(ctx, approvals ? approvals.waiting.length : null)),
    cards
      ? el(
          "section",
          { class: "ask-approvals", key: "ask-approvals", "aria-label": "Approvals" },
          el("p", { class: "basic-label" }, "Approvals"),
          waiting.length
            ? el(
                "h2",
                { class: "ask-heading" },
                waiting.length === 1 ? "It asks for a passage. This is the exact text." : `It asks for ${num(waiting.length)} passages. This is the exact text of each.`,
              )
            : null,
          el(
            "div",
            { class: "approval-stack", key: "stack" },
            drawn.cards,
            drawn.more > 0
              ? button(`Show the other ${num(drawn.more)}`, {
                  id: "ask-more-approvals",
                  key: "more",
                  onclick: () => {
                    ctx.ui.allApprovals = true;
                    ctx.render();
                  },
                })
              : null,
            drawn.granted,
          ),
        )
      : null,
    outcome ? el("div", { class: "ask-end", key: "ask-end" }, outcome) : null,
    folded
      ? el(
          "div",
          { class: "ask-fold", key: "ask-fold" },
          button(ask.steps ? "Hide the three steps" : "Show the three steps", {
            kind: "quiet",
            small: true,
            id: "ask-steps-toggle",
            expanded: ask.steps ? "true" : "false",
            onclick: () => {
              ask.steps = !ask.steps;
              ctx.render();
            },
          }),
        )
      : null,
    open ? askSteps(ctx, ask, app, label, filled && !called) : null,
    entries.length
      ? [
          el(
            "ul",
            { class: "act-list ask-rows", key: "ask-rows", "aria-label": `What was asked under the name ${name}, newest first` },
            entries.map((item) => activityRow(item, { now: ctx.now, passages: ctx.passages, plain: true })),
          ),
          search && asksEachTime ? el("p", { class: "setup-aside", key: "ask-search" }, "A search answer says which passages match. It holds no passage text.") : null,
          nothingFound ? el("p", { class: "setup-aside", key: "ask-nothing" }, "It searched and found nothing. Ask again with words that are in your document.") : null,
        ]
      : null,
    // In a demo there is no app to connect, so there is nothing to look at when none asks.
    (!called || blocked) && !ctx.state.demo
      ? [
          el(
            "div",
            { class: "ask-fold", key: "ask-help" },
            button("Nothing arrives?", {
              kind: "quiet",
              small: true,
              id: "ask-help",
              expanded: ask.help ? "true" : "false",
              controls: "ask-help-list",
              onclick: () => {
                ask.help = !ask.help;
                ctx.render();
              },
            }),
          ),
          ask.help ? helpList(ctx, ask, label) : null,
        ]
      : null,
    !called || blocked
      ? el("div", { class: "gate-actions", key: "actions" }, button("Finish later", { kind: "quiet", id: "ask-later", onclick: () => ctx.navigate("basic") }))
      : outcome && !granted.length
        ? null
        : // A passage waits, or an approval is not used yet: the way out stays, and it is not the filled button.
          el("div", { class: "gate-actions", key: "actions" }, button("Open my vault", { kind: "quiet", id: "ask-open", onclick: () => leave(null) })),
  );
}

// -- the three steps ----------------------------------------------------------------

/**
 * A step of setting up, named by the address: the app, a document, the first
 * question. `problem` is the sentence of a list that could not be loaded, or "".
 */
export function setupScreen(ctx, problem = "") {
  const steps = ctx.ui.steps;
  const kept = ctx.state.interface;
  // Decided when the step opens and kept while it is shown: setting up ends by itself at the
  // first request, and what stands above an approval card must not come or go under it.
  if (steps.guided === null) steps.guided = Boolean(kept && kept.stored && kept.setup === "open" && !ctx.state.demo);
  const step = ctx.setupStep;
  const content = step === "documents" ? documentsStep(ctx) : step === "connect" ? connectStep(ctx, problem) : appStep(ctx);
  return gateScreen(ctx, "setup", content, {
    step: true,
    wide: step !== "connect",
    wider: step === "connect",
    bar: basicBars(ctx, problem),
    foot: [
      "You can stop at any point. What you have done stays. ",
      el(
        "a",
        {
          class: "link",
          id: "setup-pro",
          href: "#/overview",
          onclick: (event) => {
            event.preventDefault();
            ctx.setUpInPro();
          },
        },
        "Set everything up yourself in Pro",
      ),
    ],
  });
}
