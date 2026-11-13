// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The screens before the vault is open: pairing, the first run, and the locked vault.

import { api } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { bytes, num } from "./format.js";
import { dropZone } from "./manage.js";
import { button, chip, demoPassphraseNote, field, note, pathText, segmented, toast } from "./parts.js";
import {
  GRANT_LABEL,
  appFormFields,
  appFormProblem,
  connectBlocks,
  connectSub,
  connectTitle,
  grantSentence,
  loadConnect,
  newAppForm,
  submitAppForm,
} from "./sheets.js";

function brand(ctx) {
  return el(
    "div",
    { class: "gate-brand" },
    el("img", { src: "favicon.svg", width: 28, height: 28, alt: "" }),
    el("span", { class: "gate-name" }, PRODUCT),
    ctx.session && ctx.session.demo ? chip("Demo", "accent") : null,
  );
}

function gateScreen(ctx, key, content, options = {}) {
  return el(
    "main",
    { class: ["gate", options.wide && "gate-wide"], key },
    brand(ctx),
    content,
    el("p", { class: "gate-foot" }, options.foot || `Version ${ctx.session ? ctx.session.version : ""}. Everything on this page comes from ${PRODUCT} on this computer.`),
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

/** Shown when the app does not answer at all. */
export function offlineScreen(ctx) {
  // Started again, the app has a new address unless it was given its port, and a new
  // pairing code. Only the demo runs without one.
  const pairing = !(ctx.session && ctx.session.pairing === "none");
  return gateScreen(
    ctx,
    "offline",
    el(
      "section",
      { class: "gate-card" },
      el("div", { class: "gate-glyph gate-glyph-amber" }, icon("warning")),
      el("h1", { class: "gate-title" }, `${PRODUCT} is not answering`),
      el(
        "p",
        { class: "gate-lead" },
        `This page talks to ${PRODUCT} on this computer. It seems to have stopped. Start it again in a terminal. ` +
          `It prints its address${pairing ? " and a new pairing code" : ""} there. ` +
          `With the same address as before, this page continues by itself${pairing ? " and asks for the code" : ""}; ` +
          "with a new address, which is the usual case, open that one instead.",
      ),
      button("Try now", { onclick: () => ctx.start() }),
    ),
    { foot: `When ${PRODUCT} stops, the vault is locked.` },
  );
}

// -- pairing ------------------------------------------------------------------------

function tidyCode(text) {
  const letters = text.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  return letters.length > 4 ? `${letters.slice(0, 4)}-${letters.slice(4)}` : letters;
}

async function pair(ctx) {
  const form = ctx.ui.pair;
  if (form.busy || form.code.replace("-", "").length < 8) return;
  form.busy = true;
  form.error = "";
  ctx.render();
  try {
    await api.pair(form.code);
    form.code = "";
    form.busy = false;
    form.expired = false;
    await ctx.start();
  } catch (error) {
    form.busy = false;
    if (error.code === "pairing_disabled") form.disabled = true;
    form.error = error.code === "wrong_code" ? "That is not the code. Check the terminal and try again." : error.message;
    ctx.render();
  }
}

export function pairingScreen(ctx) {
  const form = ctx.ui.pair;
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
      el("div", { class: "gate-glyph" }, icon("link")),
      el("h1", { class: "gate-title" }, "Pair this browser"),
      form.expired ? note("plain", "The session ended. Pair again to continue.") : null,
      el(
        "p",
        { class: "gate-lead", id: "pair-lead" },
        `Type the pairing code from the terminal where you started ${PRODUCT}. It is on the line that begins with “pairing code”.`,
      ),
      form.disabled
        ? note("red", `Pairing is switched off after five wrong codes. Quit ${PRODUCT} in its terminal and start it again to get a new code.`)
        : [
            field({
              label: "Pairing code",
              id: "pair-code",
              error: form.error,
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
                "aria-describedby": form.error ? "pair-code-error" : "pair-lead",
                "aria-invalid": form.error ? "true" : null,
                oninput: (event) => {
                  form.code = tidyCode(event.target.value);
                  form.error = "";
                  ctx.render();
                },
              }),
            }),
            button(form.busy ? "Pairing…" : "Pair", {
              kind: "primary",
              type: "submit",
              block: true,
              large: true,
              busy: form.busy,
              disabled: form.code.replace("-", "").length < 8,
            }),
          ],
    ),
    { foot: "Anyone who can read that terminal can pair. The code is never part of an address or a file." },
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
  form.error = "";
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
    form.error = error.code === "wrong_passphrase" ? "That passphrase did not open the vault." : error.message;
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
  // Two things differ where the view that is kept is Basic: the newest entry of the record is
  // one of the record's own figures and is left to Pro, and a page that saw the vault lock says
  // what became of the passages that were waiting then.
  const basic = Boolean(state.interface && state.interface.view === "basic");
  const waited = basic ? form.waited || 0 : 0;
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
      el("h1", { class: "gate-title" }, "The vault is locked"),
      el(
        "div",
        { class: "vault-id" },
        el("span", { class: "vault-name" }, state.demo ? "Demo vault" : vaultName(state.store.path)),
        el("span", { class: "vault-path" }, pathText(state.store.path, !state.demo)),
      ),
      el(
        "dl",
        { class: "seal-facts" },
        el("div", null, el("dt", null, "Sealed files"), el("dd", null, num(state.store.sealed_files))),
        el("div", null, el("dt", null, "Size"), el("dd", null, bytes(state.store.sealed_bytes))),
        !basic && record && record.head_seq
          ? el(
              "div",
              { title: "The newest entry of the record of what was released: its number and the start of its hash" },
              el("dt", null, "Last entry"),
              el("dd", { class: "mono" }, `#${record.head_seq} ${record.head_hash}`),
            )
          : null,
      ),
      waited > 0
        ? el(
            "p",
            { class: "note", id: "locked-waiting" },
            waited === 1
              ? "1 passage was waiting when the vault locked. It was not released. If an app still needs one, it will ask again after you unlock."
              : `${num(waited)} passages were waiting when the vault locked. They were not released. If an app still needs one, it will ask again after you unlock.`,
          )
        : null,
      demoPassphraseNote(state.demo_passphrase, () => {
        form.passphrase = state.demo_passphrase;
        form.error = "";
        ctx.render();
        const input = document.getElementById("unlock-passphrase");
        if (input) input.focus();
      }),
      field({
        label: "Passphrase",
        id: "unlock-passphrase",
        error: form.error,
        control: el("input", {
          type: "password",
          id: "unlock-passphrase",
          class: "input input-large",
          value: form.passphrase,
          autocomplete: "current-password",
          autofocus: true,
          required: true,
          "aria-invalid": form.error ? "true" : null,
          "aria-describedby": form.error ? "unlock-passphrase-error" : null,
          oninput: (event) => {
            form.passphrase = event.target.value;
            ctx.render();
          },
        }),
      }),
      button(form.busy ? "Unlocking…" : "Unlock", { kind: "primary", type: "submit", block: true, large: true, busy: form.busy, disabled: !form.passphrase }),
    ),
    { foot: "While locked, apps get one answer: the vault is locked. The number and sizes of the sealed files are visible without the key; what is in them is not." },
  );
}

// -- the first run ------------------------------------------------------------------

const STEPS = ["Passphrase", "Documents", "App"];

function stepper(current) {
  return el(
    "ol",
    { class: "steps", "aria-label": "Setting up" },
    STEPS.map((label, index) =>
      el(
        "li",
        { class: ["step", index + 1 === current && "is-current", index + 1 < current && "is-done"], "aria-current": index + 1 === current ? "step" : null },
        el("span", { class: "step-dot" }, index + 1 < current ? icon("check") : String(index + 1)),
        el("span", { class: "step-label" }, label),
      ),
    ),
  );
}

async function createVault(ctx) {
  const form = ctx.ui.create;
  if (form.busy) return;
  form.busy = true;
  form.error = "";
  ctx.render();
  try {
    const state = await api.createStore(form.passphrase);
    form.passphrase = "";
    form.again = "";
    form.busy = false;
    form.shown = false;
    ctx.onboarding = { step: 2, app: null, mode: "approve", modeFor: "", connect: null, added: null, busy: false };
    ctx.applyState(state);
    await ctx.refresh();
  } catch (error) {
    form.busy = false;
    form.error = error.message;
    ctx.render();
  }
}

/** Step 1 of the first run: there is no vault yet. */
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
      stepper(1),
      el("h1", { class: "gate-title" }, "Choose a passphrase"),
      el(
        "p",
        { class: "gate-lead" },
        "Your documents are sealed with a key made from this passphrase. You type it each time you unlock the vault.",
      ),
      field({
        label: "Passphrase",
        id: "new-passphrase",
        hint: longEnough ? `${num(length)} characters. Long enough.` : `At least 12 characters; ${num(length)} so far. A few unrelated words work well.`,
        // It cannot be recovered, so it may be looked at while it is chosen.
        action: button(form.shown ? "Hide" : "Show", {
          kind: "quiet",
          small: true,
          id: "new-passphrase-show",
          pressed: form.shown ? "true" : "false",
          label: form.shown ? "Hide what is typed" : "Show what is typed",
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
        label: "The same passphrase again",
        id: "new-passphrase-again",
        error: mismatch ? "The two entries are different." : "",
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
      note("amber", "Losing the passphrase loses the vault. There is no recovery."),
      form.error ? note("red", form.error) : null,
      button(form.busy ? "Creating the vault…" : "Create the vault", {
        kind: "primary",
        type: "submit",
        block: true,
        large: true,
        busy: form.busy,
        disabled: !(longEnough && same),
      }),
      el("p", { class: "gate-where" }, "It will be created at ", pathText(ctx.state.store.path)),
    ),
    { wide: true },
  );
}

function finish(ctx) {
  ctx.onboarding = null;
  // A vault whose view is Basic opens on the Basic page, with the focus on its headline; any other on Overview.
  const basic = Boolean(ctx.state.interface && ctx.state.interface.view === "basic");
  ctx.navigate(basic ? "basic" : "overview");
  if (basic) ctx.ui.focusId = "basic-status";
  // The last step is a long card: the vault opens at its top, not where that card was left.
  window.scrollTo(0, 0);
  ctx.refresh();
}

function documentsStep(ctx) {
  const added = ctx.data.documents ? ctx.data.documents.collections.reduce((sum, collection) => sum + collection.documents.length, 0) : 0;
  return el(
    "section",
    { class: "gate-card", key: "step-2" },
    stepper(2),
    el("h1", { class: "gate-title" }, "Add documents"),
    el(
      "p",
      { class: "gate-lead" },
      "They go into a collection named “documents”. No app can read it until you say so in the next step.",
    ),
    dropZone(ctx, "documents", { large: true }),
    el(
      "div",
      { class: "gate-actions" },
      added
        ? null
        : button("Skip", {
            kind: "quiet",
            onclick: () => {
              ctx.onboarding.step = 3;
              ctx.render();
            },
          }),
      button(added ? `Continue with ${num(added)} ${added === 1 ? "document" : "documents"}` : "Continue", {
        kind: "primary",
        disabled: !added,
        onclick: () => {
          ctx.onboarding.step = 3;
          ctx.render();
        },
      }),
    ),
  );
}

async function addFirstApp(ctx) {
  const flow = ctx.onboarding;
  const form = flow.app;
  flow.busy = true;
  ctx.render();
  const app = await submitAppForm(form, () => ctx.render());
  if (!app) {
    flow.busy = false;
    ctx.render();
    return;
  }
  try {
    if (flow.mode !== "none") {
      const body = { name: "documents" };
      if (app.hosted) body.allow_hosted = true;
      await api.setCollection(body);
      await api.setGrant(app.name, "documents", flow.mode);
    }
  } catch (error) {
    if (error.code !== "cancelled") toast(`${app.name} was added, but it was not given the collection: ${error.message}`, "red");
  }
  flow.busy = false;
  flow.added = app;
  flow.connect = { app: app.name, host: form.choice, data: null, loading: false, error: "", copied: -1 };
  loadConnect(flow.connect, () => ctx.render());
  await ctx.refresh();
}

function appStep(ctx) {
  const flow = ctx.onboarding;
  const known = ctx.data.apps ? ctx.data.apps.known : null;
  if (!known) return el("section", { class: "gate-card", key: "step-3" }, stepper(3), el("p", { class: "muted" }, "Loading…"));
  if (!flow.app) flow.app = newAppForm(known);
  if (flow.added) {
    return el(
      "section",
      { class: "gate-card", key: "step-3-done" },
      stepper(4),
      el(
        "div",
        { class: "gate-heading" },
        el("h1", { class: "gate-title" }, connectTitle(known, flow.connect.host, flow.added.name)),
        el("p", { class: "sheet-sub" }, connectSub(flow.added.name)),
      ),
      el(
        "p",
        { class: "gate-lead" },
        "Copy this into the app’s own configuration, then restart the app. You find it again under Apps.",
      ),
      el("div", { class: "connect-body" }, connectBlocks(flow.connect, () => ctx.render())),
      el("div", { class: "gate-actions" }, button("Open the vault", { kind: "primary", onclick: () => finish(ctx), autofocus: true })),
    );
  }
  const form = flow.app;
  // An app that sends text to a provider starts at "Ask me each time": reading without
  // asking is given by a deliberate choice, not by leaving the form as it came.
  const picked = `${form.choice} ${form.hosted}`;
  if (flow.modeFor !== picked) {
    flow.modeFor = picked;
    flow.mode = form.hosted ? "approve" : "read";
  }
  const problem = appFormProblem(form, known);
  const described = { name: form.name, hosted: form.hosted, destination: form.destination.trim() };
  return el(
    "form",
    {
      class: "gate-card",
      key: "step-3",
      autocomplete: "off",
      onsubmit: (event) => {
        event.preventDefault();
        if (!problem && !flow.busy) addFirstApp(ctx);
      },
    },
    stepper(3),
    el("h1", { class: "gate-title" }, "Connect an app"),
    el("p", { class: "gate-lead" }, "Pick the app that may ask the vault for text, and decide what it may do with your documents."),
    appFormFields(form, known, () => ctx.render(), "first-app", flow.mode !== "none"),
    problem === "web"
      ? null
      : [
          el(
            "div",
            { class: "field" },
            el("span", { class: "field-label" }, "What it may do with “documents”"),
            segmented({
              name: "first-mode",
              legend: "What the app may do with the collection documents",
              value: flow.mode,
              options: ["none", "read", "approve"].map((mode) => ({ value: mode, label: GRANT_LABEL[mode] })),
              onchange: (mode) => {
                flow.mode = mode;
                ctx.render();
              },
            }),
          ),
          flow.mode !== "none" && (!form.hosted || described.destination) ? note("amber", grantSentence(described, "documents")) : null,
        ],
    el(
      "div",
      { class: "gate-actions" },
      button("Skip", { kind: "quiet", onclick: () => finish(ctx) }),
      button(flow.busy ? "Adding…" : "Add the app", { kind: "primary", type: "submit", disabled: Boolean(problem), busy: flow.busy }),
    ),
  );
}

/** Steps 2 and 3 of the first run: the vault exists and is unlocked. */
export function setupScreen(ctx) {
  return gateScreen(ctx, "setup", ctx.onboarding.step === 2 ? documentsStep(ctx) : appStep(ctx), {
    wide: true,
    foot: "You can do all of this later too, under Documents and Apps.",
  });
}
