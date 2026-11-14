// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The screens before the vault is open: pairing, the passphrase of a new vault, the
// locked vault, and the app that does not answer. Each is one card in the middle of
// the window, with the choice of the language top right. They take every text from
// the text table. A new vault opens on Home, which shows what to do first: there is
// no screen of steps between.

import { api } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { bytes, num } from "./format.js";
import { button, chip, demoPassphraseNote, errorText, field, languageSwitch, note, pathText } from "./parts.js";
import { chosenLanguage, t } from "./texts.js";

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
 * top right, the name, one card.
 */
function gateScreen(ctx, key, content) {
  return el(
    "main",
    { class: "gate", key },
    el("div", { class: "gate-lang", key: "lang" }, languageSwitch(ctx.chooseLanguage)),
    brand(ctx),
    content,
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
    // Unlocked here, a vault that opens on Home does so, whatever the address named.
    if (state.interface && state.interface.view === "basic") ctx.navigate("home");
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
    // A new vault opens on Home, which shows the three things to do first. One that was
    // started to open on all settings opens on Overview, where "Getting started" takes over.
    ctx.navigate(state.interface && state.interface.view === "basic" ? "home" : "pro/overview");
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
