// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The Basic page: one column that says what waits for the owner, what left the
// vault, which documents it holds and which apps may ask. It states the vault as
// it is. Where one value does not describe the whole vault it has no control:
// it says what is in force in a sentence and links to the place in Pro. Every
// line about a setting is made from an answer of the app; this file holds no
// copy of the values of a new vault.
//
// What it changes: documents are added and removed, an app is removed, and where
// the vault has one group of documents an app's access to it is set. Nothing else
// of the vault. Beside the vault it changes one word: that setting up is over.
//
// In a demo the page also holds the guide. Its one request changes nothing of the
// owner's: it has the app that is played ask once more, as a person would tell a
// real app "try again". The app tells the page nothing about what is played.

import { api } from "./api.js";
import { PRODUCT } from "./brand.js";
import { el, icon } from "./dom.js";
import { ago, at, count, dayShort, num, parseTime, percent, whenFull } from "./format.js";
import { CANNOT, dropZone, isSample, loadingBlock, removeApp, removeDocument, uploadFiles, versionLine, whoMayAsk } from "./manage.js";
import {
  UNANSWERED,
  activityRow,
  button,
  chip,
  copyText,
  destinationChip,
  iconButton,
  meter,
  openSheet,
  sheetFoot,
  sheetHead,
  toast,
  toastError,
} from "./parts.js";
import { GENERAL_SENTENCE, GRANT_LABEL, basicGrantSentence, codeWords, grantSentence } from "./sheets.js";
import { approvalCards, warningList } from "./views.js";

/** How many waiting passages are shown as cards before "Show the other N" is pressed. */
const CARDS_AT_FIRST = 3;
const DAY_MS = 86400000;
/**
 * How long after "Have the played app ask again" the page asks for the lists itself.
 * The played app has made its call well before: it looks for the request several
 * times a second.
 */
const AGAIN_MS = 2000;

/** What starts a question that makes an AI app use the vault. The space at its end is part of it. */
export const ASK_START = `Use ${PRODUCT} to search my documents and answer this: `;

/** Said wherever a name stands for something that happened, and where names are managed. */
const NAME_IS_A_LABEL = `A name is a label: ${PRODUCT} cannot check which program is really asking.`;

/** The group that documents go to in a vault that has none yet. */
export const FIRST_GROUP = "documents";

/** What an app may do with one group, as the app said it: "approve", "read" or "none", and nothing else. */
function grantOf(app, group) {
  const grants = app.grants || {};
  const mode = Object.prototype.hasOwnProperty.call(grants, group) ? grants[group] : "none";
  return mode === "approve" || mode === "read" ? mode : "none";
}

// -- pieces -------------------------------------------------------------------------

/**
 * One part of the page under its heading, named by that heading for a screen reader.
 * With `fold` (see `foldOf`) the heading carries a quiet button, and while the part
 * is folded nothing but the heading is drawn.
 */
function section(key, title, aside, content, fold = null) {
  const id = `${key}-title`;
  const words = fold && fold.open ? "Hide" : "Show";
  return el(
    "section",
    { class: ["basic-section", fold && "is-foldable"], key, "aria-labelledby": id },
    el(
      "div",
      { class: "section-head" },
      el("h2", { class: "section-title", id }, title),
      aside ? el("span", { class: "section-aside" }, aside) : null,
      fold
        ? button(words, { kind: "quiet", small: true, id: fold.id, label: `${words}: ${title}`, expanded: fold.open ? "true" : "false", onclick: fold.toggle })
        : null,
    ),
    fold && !fold.open ? null : content,
  );
}

/** "Copy", which says "Copied" for a moment. `id` tells the buttons of the page apart. `words` is said in place of "Copy". */
export function copyButton(ctx, id, text, what, words = "Copy") {
  const copied = ctx.ui.copied === id;
  return button(copied ? "Copied" : words, {
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
export function copyLine(ctx, id, text, what) {
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
export function waitingSaid(ctx, waiting) {
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
    // A sheet of this page lies over the cards. When it closes, a second press that was meant
    // for one of its buttons would meet whatever is under it.
    ctx.ui.sheets > 0 ? "sheet" : "",
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

// -- setting up ---------------------------------------------------------------------

/**
 * What is true of the vault now, of the three things that setting up leads to: it
 * has an app, it holds a document, an app has asked. Null until the list of apps is in.
 */
export function setupFacts(ctx) {
  const data = ctx.data.apps;
  if (!data) return null;
  const held = ctx.state.encryption ? ctx.state.encryption.documents : 0;
  return { app: data.apps.length > 0, document: held > 0, asked: data.apps.some((app) => Boolean(app.last_seen)) };
}

/** Whether the app keeps a word about setting up for this vault, and that word says it is not over. */
function setupOpen(ctx) {
  const kept = ctx.state.interface;
  return Boolean(kept && kept.stored && kept.setup === "open");
}

/**
 * Say that setting up is over. The app keeps that as one word beside the vault, not
 * sealed; nothing of the vault changes. Nothing is sent for a vault whose file does
 * not say that setting up is open. Resolves to null, or to what went wrong.
 */
export async function setupDone(ctx) {
  if (!setupOpen(ctx)) return null;
  try {
    ctx.applyState(await api.setInterface({ setup: "done" }));
    return null;
  } catch (error) {
    return error;
  }
}

/**
 * The first run completes itself: once the vault has an app, holds a document and an
 * app has asked, the page tells the app that setting up is over, once, on whatever
 * screen it is. It is the one request the page sends without a press, and it is
 * never sent for a vault that has no such file.
 */
export function settleSetup(ctx) {
  if (!ctx.state || ctx.state.locked || ctx.state.demo || !setupOpen(ctx) || ctx.ui.setupSaid) return;
  const facts = setupFacts(ctx);
  if (!facts || !facts.app || !facts.document || !facts.asked) return;
  ctx.ui.setupSaid = true;
  setupDone(ctx);
}

/**
 * One line that names the next step of setting up, until setting up is over or the
 * owner hides it. "Hide" ends it for good: what it offered stays on the page.
 */
function setupCard(ctx) {
  if (ctx.state.demo || !setupOpen(ctx)) return null;
  const facts = setupFacts(ctx);
  if (!facts) return null;
  let next = null;
  if (!facts.app) next = { words: "Next: choose your AI app.", action: "Continue", href: "#/setup/app" };
  else if (!facts.document) next = { words: "Next: add a document.", action: "Continue", href: "#/setup/documents" };
  else if (!facts.asked) next = { words: "Next: ask your AI app a first question.", action: "Show me how", href: "#/setup/connect" };
  if (!next) return null;
  // The vault exists: that is the first of the four.
  const done = 1 + [facts.app, facts.document, facts.asked].filter(Boolean).length;
  // While a passage waits, no button on the page is filled.
  const waits = ctx.state.today ? ctx.state.today.approvals_waiting > 0 : false;
  return el(
    "section",
    { class: "card card-setup", key: "setup", "aria-label": "Finish setting up" },
    el(
      "header",
      { class: "card-head" },
      el("h2", { class: "card-title" }, "Finish setting up"),
      el("span", { class: "card-aside" }, `${num(done)} of 4 done`),
      button("Hide", {
        kind: "quiet",
        small: true,
        id: "setup-hide",
        onclick: async () => {
          const failed = await setupDone(ctx);
          if (failed) toastError(failed);
          else toast("Setup is hidden. Adding an app, adding documents and the connection steps stay on this page.", "plain");
        },
      }),
    ),
    el(
      "div",
      { class: "setup-line" },
      el("p", { class: "setup-next" }, next.words),
      el("a", { class: ["btn", waits ? "btn-default" : "btn-primary"], id: "setup-continue", href: next.href }, el("span", { class: "btn-label" }, next.action)),
    ),
  );
}

// -- the demo's guide ---------------------------------------------------------------

/**
 * What this page remembers of the demo's guide. It is the page's memory and nothing
 * else: a lock or a reload starts the guide again from what the lists say.
 */
export function freshGuide() {
  return {
    hidden: false,
    // Whether "Now with your own documents" was opened by its button, and whether the demo has come far enough to show it without one.
    own: false,
    reached: false,
    // The owner's last answer to a waiting passage as far as this page saw it, and the answers it has taken note of.
    decision: null,
    noted: new WeakSet(),
    // The entry of a passage that was read after an approval, once this page has seen its row.
    read: null,
    // Which of the folded parts of the page were opened.
    unfolded: new Set(),
    // Whether the played app was just told to ask again.
    asking: false,
  };
}

/** Whether the demo's guide is on the page. */
function guideOpen(ctx) {
  return ctx.state.demo === true && !ctx.ui.guide.hidden;
}

/**
 * While the guide is open, "Documents", "Apps" and "Also set for this vault, in Pro"
 * are folded to their headings: the demo's vault has several groups, several apps and
 * both kinds of app, which is not what a new owner will have. Each heading has a
 * button that unfolds its part. Null when nothing is folded: outside a demo, and once
 * the guide is hidden.
 */
function foldOf(ctx, name) {
  if (!guideOpen(ctx)) return null;
  const unfolded = ctx.ui.guide.unfolded;
  const open = unfolded.has(name);
  return {
    id: `fold-${name}`,
    open,
    toggle: () => {
      if (open) unfolded.delete(name);
      else unfolded.add(name);
      ctx.render();
    },
  };
}

/**
 * Where the demo is: read from the lists this page loads anyway and from the
 * owner's own presses on this page. The app tells the page nothing about what is
 * played. Null until the lists are in.
 *
 * "watch": no app has asked for a passage that needs approval. "turn": a passage
 * waits. "released": an approval stands and was not used. "kept": the owner's last
 * answer on this page was "Don't release". "read": a passage was read after an
 * approval. "asked": a passage was asked for and the lists do not say what became of
 * it (an approval that was withdrawn or is over; a page that was loaded again).
 */
function guideBeat(ctx) {
  const guide = ctx.ui.guide;
  const approvals = ctx.data.approvals;
  const left = ctx.left;
  if (!approvals || !left.loaded || !ctx.stops) return null;
  // The owner's last answer. "Don't release" writes nothing a page could read back, so it is kept here.
  const settled = Array.from(ctx.ui.decided.values());
  for (const done of settled) {
    if (guide.noted.has(done)) continue;
    guide.noted.add(done);
    guide.decision = { release: done.release === true };
  }
  if (guide.read === null) {
    const seq = left.order.find((entry) => left.items.get(entry).approved === true);
    if (seq !== undefined) guide.read = seq;
  }
  // A passage that was answered a moment ago is not waiting, though the list may still hold it.
  const waiting = approvals.waiting.filter((item) => !settled.some((done) => done.item.app === item.app && done.item.ref === item.ref));
  if (waiting.length) return "turn";
  if (guide.read !== null) return "read";
  if ((approvals.granted || []).length) return "released";
  if (guide.decision && !guide.decision.release) return "kept";
  if (guide.decision || ctx.stops.some((item) => item.code === "NEEDS_APPROVAL")) return "asked";
  return "watch";
}

/** The title and the sentences of one moment of the guide, and whether it offers "Have the played app ask again". */
function guideWords(ctx, beat) {
  if (beat === "turn") {
    const waiting = ctx.data.approvals.waiting;
    return {
      title: "Your turn",
      text:
        waiting.length === 1
          ? `A played app asks, under the name ${waiting[0].app}, for the passage above. Read the exact text, see where it would go, and choose. Either answer is fine.`
          : "Played apps ask for the passages above. Read the exact text of each, see where it would go, and choose. Either answer is fine.",
      again: false,
    };
  }
  if (beat === "released") {
    return {
      title: "You released it once",
      text: "Nothing has been sent yet. A real app now has to ask again; you would tell it “try again”. Here this button does that for the played app.",
      again: true,
    };
  }
  if (beat === "kept") {
    return {
      title: "You did not release it",
      text: "Nothing from that passage left. If an app asks again, the passage waits here again. This button makes the played app ask again.",
      again: true,
    };
  }
  if (beat === "read") {
    const left = ctx.left;
    const seq = ctx.ui.guide.read;
    let row = "The row marked “Released after approval” under “What left the vault” shows the exact text that left.";
    if (left.order[0] === seq) row = "The newest row under “What left the vault” shows the exact text that left.";
    else if (!left.items.has(seq)) row = "The row marked “Released after approval” under “What left the vault” shows the exact text that left. It is among the older rows by now, which “Show more” brings.";
    return {
      title: "It asked again and got the passage",
      text: `${row} That is the whole of it: an app asked, you saw the exact text and where it would go, you decided, and what left is on the record.`,
      again: false,
    };
  }
  if (beat === "asked") {
    return {
      title: "Nothing is waiting now",
      text: "A played app asked for a passage that needed your approval. If it has not been given that passage, this button makes it ask again, and the passage waits above again.",
      again: true,
    };
  }
  return {
    title: "Watch an app ask for documents",
    text:
      `Nothing here is real: the documents are invented, and the apps that ask are played by ${PRODUCT} itself. ` +
      "Soon one of them asks for a passage that needs your approval; it appears above, under “Approvals”. " +
      "The rows under “What left the vault” are what the played apps were given so far.",
    again: false,
  };
}

/**
 * "Have the played app ask again": what telling a real app "try again" is. The app
 * that is played then makes one call, which is answered like any app's call, and
 * this page hears of it like of any call. Nothing tells the played app what the
 * owner decided: after a release it gets the passage, after a "no" the passage waits
 * again.
 */
async function askAgain(ctx) {
  const guide = ctx.ui.guide;
  if (guide.asking) return;
  guide.asking = true;
  ctx.render();
  try {
    await api.demoAgain();
  } catch (error) {
    guide.asking = false;
    toastError(error);
    ctx.render();
    return;
  }
  setTimeout(async () => {
    await ctx.refresh();
    guide.asking = false;
    // The button goes once the passage waits again or was read. Where it had the focus, what
    // says so takes it: the headline of the page, or the title of the guide. Never a button of a card.
    const held = document.activeElement;
    if (ctx.route === "basic" && guideOpen(ctx) && (!held || held === document.body || held.id === "demo-again")) {
      const beat = guideBeat(ctx);
      if (beat === "turn") ctx.ui.focusId = "basic-status";
      else if (beat === "read") ctx.ui.focusId = "guide-title";
    }
    ctx.render();
  }, AGAIN_MS);
}

/**
 * The end of the guide: how to go on with a vault of one's own. The command is the
 * one the app sent, which in a demo starts this installation without the demo. No
 * button here starts anything: a vault of one's own needs the pairing code that only
 * a new start prints.
 */
function ownVault(ctx) {
  const command = (ctx.state.launch || {}).command;
  return el(
    "div",
    { class: "guide-own", key: "guide-own" },
    el("h3", { class: "guide-own-title" }, "Now with your own documents"),
    el(
      "ol",
      { class: "guide-steps" },
      el("li", null, "In the terminal where this demo runs, press Ctrl-C. The demo vault is deleted."),
      command
        ? el("li", null, el("p", null, "Run this:"), copyLine(ctx, "guide-copy", command, `the command that starts ${PRODUCT} with a vault of your own`))
        : el("li", null, `Start ${PRODUCT} again the way you started this demo, without “--demo”.`),
      el(
        "li",
        null,
        !command || opensPage(command)
          ? "The page that opens asks for a pairing code, which the terminal shows, and then for a passphrase."
          : "The terminal prints the address of a page. That page asks for a pairing code, which the terminal shows, and then for a passphrase.",
      ),
    ),
    el("p", { class: "guide-ready" }, "Have ready: a passphrase you can type from memory (it cannot be recovered), one document, and the AI app you use."),
  );
}

/**
 * The guide of the demo, under "Approvals" and the notices. It stands below the
 * approval card, which is the real one and complete without it. It never leads to one
 * answer: both answers are followed by the same button. No sentence of it names a time.
 */
function demoGuide(ctx, stops) {
  if (!guideOpen(ctx)) return null;
  const guide = ctx.ui.guide;
  const beat = guideBeat(ctx);
  const words = beat ? guideWords(ctx, beat) : null;
  // From the owner's first answer on, the way to a vault of one's own is open without a press.
  const decided = beat !== null && beat !== "watch" && beat !== "turn";
  if (decided || guide.decision) guide.reached = true;
  return el(
    "section",
    { class: "card card-guide", key: "demo-guide", "aria-labelledby": "guide-title" },
    el(
      "header",
      { class: "card-head" },
      chip("Demo", "accent"),
      el("h2", { class: "card-title", id: "guide-title", tabindex: -1 }, words ? words.title : "The demo"),
      button("Hide", {
        kind: "quiet",
        small: true,
        id: "guide-hide",
        label: "Hide the guide",
        onclick: () => {
          guide.hidden = true;
          // The button has gone: the next control of the page takes the focus.
          ctx.ui.focusId = "basic-ask-copy";
          ctx.render();
          toast("The guide is hidden. It is back when this page is loaded again.", "plain");
        },
      }),
    ),
    words ? el("p", { class: "guide-text", key: "text" }, words.text) : loadingBlock(),
    words && words.again
      ? el(
          "div",
          { class: "guide-actions", key: "again" },
          // Never filled: the page has one filled button at most, and it is not this one.
          button(guide.asking ? "Asking…" : "Have the played app ask again", { id: "demo-again", busy: guide.asking, onclick: () => askAgain(ctx) }),
        )
      : null,
    decided && stops.some((stop) => stop.kind === "client-day")
      ? el(
          "p",
          { class: "guide-text", key: "limit" },
          "Meanwhile a second played app read a letter with an instruction hidden in it and then asked for every passage it could find. " +
            "Its limit for 24 hours stopped it: see “Stopped in the last 24 hours” below.",
        )
      : null,
    guide.reached
      ? null
      : el(
          "div",
          { class: "guide-more", key: "more" },
          button("Use your own documents", {
            kind: "quiet",
            small: true,
            id: "guide-own",
            expanded: guide.own ? "true" : "false",
            onclick: () => {
              guide.own = !guide.own;
              ctx.render();
            },
          }),
        ),
    guide.reached || guide.own ? ownVault(ctx) : null,
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
          el("th", { scope: "col", class: "col-action" }, el("span", { class: "sr-only" }, "Remove")),
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
              // Removing narrows what apps can get, so it is offered whatever else the vault holds.
              el("td", { class: "col-action" }, iconButton("trash", `Remove ${document.title}`, { onclick: () => removeDocument(ctx, document) })),
            ),
          ),
        ),
      ),
    ),
  );
}

/** "3 files: a.txt, b.txt, c.txt" and, past three names, "and 2 more". */
function fileNames(files) {
  const names = files.slice(0, 3).map((file) => file.name).join(", ");
  const more = files.length > 3 ? ` and ${num(files.length - 3)} more` : "";
  return `${count(files.length, "file", "files")}: ${names}${more}`;
}

/**
 * The sheet that asks which group the files go to, in a vault with several. No
 * group is chosen for the owner. Each group says who can ask for what is in it, and
 * once one is chosen that an app which sends text to a provider can use, the
 * sentence about that stands above the buttons. "Cancel" drops the files: nothing
 * is sent. It is built from the lists as they are when it opens.
 */
async function addToWhichGroup(ctx, files) {
  if (!files.length || !ctx.data.apps || !ctx.data.documents) return;
  const apps = ctx.data.apps.apps;
  const groups = ctx.data.documents.collections.map((group) => group.name);
  const form = { group: null };
  const sheet = openSheet(
    (self) => {
      const senders = form.group === null ? [] : apps.filter((app) => app.hosted && grantOf(app, form.group) !== "none");
      return el(
        "div",
        { class: "sheet-content" },
        sheetHead("Add to which group?"),
        el("p", { class: "sheet-files" }, fileNames(files)),
        el(
          "div",
          { class: "choice-list choice-stack", role: "radiogroup", "aria-label": "Add to which group?" },
          groups.map((group, index) => {
            const names = whoMayAsk(apps, group, `${GRANT_LABEL.read}: passages can leave without asking`);
            return el(
              "label",
              { class: ["choice", form.group === group && "is-selected"], key: `group-${group}` },
              el("input", {
                type: "radio",
                name: "basic-group",
                value: group,
                checked: form.group === group,
                "aria-labelledby": `basic-group-${index}-name`,
                "aria-describedby": `basic-group-${index}-who`,
                onchange: () => {
                  form.group = group;
                  self.update();
                },
              }),
              el(
                "span",
                { class: "choice-text" },
                el("span", { class: "choice-label", id: `basic-group-${index}-name` }, group),
                el("span", { class: "choice-hint", id: `basic-group-${index}-who` }, names.length ? names.join(", ") : "No app can ask for what is in it."),
              ),
            );
          }),
        ),
        senders.length
          ? el(
              "p",
              { class: "note note-amber", role: "note" },
              icon("warning"),
              el("span", { class: "note-text" }, senders.length === 1 ? grantSentence(senders[0], form.group) : GENERAL_SENTENCE),
            )
          : null,
        sheetFoot(
          button("Cancel", { onclick: () => self.close(null), autofocus: true }),
          // Only this press sends the files: a key pressed on a choice does not.
          button("Add", {
            kind: "primary",
            disabled: form.group === null,
            onclick: () => {
              if (form.group !== null) self.close(form.group);
            },
          }),
        ),
      );
    },
    { label: "Add to which group?" },
  );
  const group = await sheet.closed;
  if (typeof group === "string") await uploadFiles(ctx, group, files);
}

function documentsSection(ctx) {
  const fold = foldOf(ctx, "documents");
  if (fold && !fold.open) return section("basic-documents", "Documents", null, null, fold);
  const data = ctx.data.documents;
  const apps = ctx.data.apps ? ctx.data.apps.apps : [];
  if (!data) return section("basic-documents", "Documents", null, loadingBlock(), fold);
  const groups = data.collections;
  const total = groups.reduce((sum, group) => sum + group.documents.length, 0);
  const sample = groups.reduce((found, group) => found || group.documents.find(isSample) || null, null);
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
    sample
      ? el(
          "p",
          { class: "basic-sample", key: "sample" },
          el("span", null, "1 of them is the invented sample letter."),
          button("Remove it", { small: true, id: "sample-remove", onclick: () => removeDocument(ctx, sample) }),
        )
      : null,
    senders.length
      ? el(
          "p",
          { class: "note note-amber", id: "docs-sentence", key: "sentence", role: "note" },
          icon("warning"),
          el("span", { class: "note-text" }, senders.length === 1 ? basicGrantSentence(senders[0]) : GENERAL_SENTENCE),
        )
      : null,
    // The one place on this page that takes files. It is drawn once the apps are known, so
    // that it never stands without the sentence above it or the line about who can ask.
    !ctx.data.apps
      ? loadingBlock()
      : groups.length > 1
        ? // Several groups: the files are held, and the owner is asked which group they go to.
          dropZone(ctx, "basic", { who: true, lists: groups.map((group) => group.name), take: (files) => addToWhichGroup(ctx, files) })
        : dropZone(ctx, groups.length ? groups[0].name : FIRST_GROUP, { who: true }),
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
  ], fold);
}

// -- Apps ---------------------------------------------------------------------------

/** Whether an app has a limit that is not the one every app gets. */
function hasOwnLimits(app, settings) {
  return Object.keys(settings.defaults).some((key) => app.limits[key] !== settings.defaults[key]);
}

/** What an app may do, in the three labels. With several groups: a summary, and one line per group. */
function accessOf(app, groups) {
  const mode = (group) => grantOf(app, group.name);
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

/** The lines under each of the three things an app may do. The two figures of "Can read" are the vault's own. */
function accessLines(settings) {
  const share = percent(settings.per_document.max_fraction);
  const most = settings.per_document.max_chars;
  return {
    approve:
      "It can search and learns which passages match its words, not what they say. Every passage waits for your approval. " +
      "Each yes is a visit to this page, and the app has to ask again.",
    read:
      `It can search with short snippets and read passages without asking: up to ${share} of each document ` +
      `(${num(most)} characters at most), inside its limits. Past that, each passage waits for your approval.`,
    none: "It is told there is nothing for it.",
  };
}

/** The sentence that says where the text goes, at the moment access is given. Drawing it is what allows "Save". */
function accessSentence(form) {
  form.sentenceShown = true;
  return el(
    "p",
    { class: "note note-amber", id: "basic-sentence", key: "sentence", role: "note" },
    icon("warning"),
    el("span", { class: "note-text" }, basicGrantSentence(form.app)),
  );
}

/** The line that says what else "Save" does, when the documents are not yet open to apps of this kind. */
function opensLine(form) {
  form.opensShown = true;
  return el(
    "p",
    { class: "basic-opens", id: "basic-opens", key: "opens" },
    "Saving also opens your documents to apps that send text to a provider. No other app gets access by that.",
  );
}

/**
 * "Save" in the app's sheet: this app's access to the vault's one group, and nothing
 * else. Both requests go through the one function that sends requests, so a change
 * that lets more text leave asks for the passphrase when it was last typed too long
 * ago, and is then sent again.
 */
async function saveAccess(ctx, sheet, form, mode) {
  if (!form.sentenceShown && mode !== "none") return;
  if (form.busy || mode === form.inForce) return;
  form.busy = true;
  form.error = "";
  sheet.update();
  // What was done before something failed, so that the sheet never says less than happened.
  let opened = false;
  let made = false;
  try {
    if (mode !== "none" && (form.opensShown || !form.exists)) {
      // The group is opened to apps that send text to a provider only when the sheet says so.
      const body = { name: form.group };
      if (form.opensShown) body.allow_hosted = true;
      await api.setCollection(body);
      opened = form.opensShown;
      made = !form.exists;
      form.exists = true;
      if (opened) form.closed = false;
    }
    await api.setGrant(form.app.name, form.group, mode);
    sheet.close(mode);
  } catch (error) {
    form.busy = false;
    const gaveUp = error.code === "cancelled";
    if (opened) {
      form.error = `Your documents are now open to apps that send text to a provider, but ${form.app.name} was not given access.${gaveUp ? "" : ` ${error.message}`}`;
    } else if (made && gaveUp) {
      form.error = `${form.app.name} was not given access.`;
    } else {
      // "Nothing was changed." when the owner closed the sheet that asks for the passphrase.
      form.error = error.message;
    }
    // The choice goes back to what is in force: the sheet shows what the vault says.
    if (gaveUp) form.mode = form.inForce;
    sheet.update();
    if (opened || made) await ctx.refresh();
  }
}

/**
 * The sheet behind "Change": what one app may do with the documents of a vault that
 * has one group, or none yet. It starts on what is in force and chooses nothing for
 * the owner. It is built from the lists as they are when it opens, and is never
 * opened for a vault with several groups: there one value does not say what an app
 * may do.
 */
async function openAppSheet(ctx, name) {
  const data = ctx.data.apps;
  const settings = ctx.data.settings;
  const app = data ? data.apps.find((entry) => entry.name === name) : null;
  if (!app || !settings || data.collections.length > 1) return;
  const group = data.collections.length ? data.collections[0] : null;
  const form = {
    app,
    group: group ? group.name : FIRST_GROUP,
    exists: group !== null,
    // Whether the group still has to be opened before an app that sends text to a provider can be given it.
    closed: app.hosted && !(group && group.allow_hosted),
    inForce: group ? grantOf(app, group.name) : "none",
    mode: group ? grantOf(app, group.name) : "none",
    busy: false,
    error: "",
    sentenceShown: false,
    opensShown: false,
  };
  const lines = accessLines(settings);
  const sheet = openSheet(
    (self) => {
      // Both are true only while their words are on the sheet.
      form.sentenceShown = false;
      form.opensShown = false;
      return el(
        "div",
        { class: "sheet-content" },
        sheetHead(el("span", { class: "mono" }, app.name)),
        el("div", { class: "sheet-who" }, destinationChip(app)),
        el("p", { class: "basic-hint" }, NAME_IS_A_LABEL),
        el(
          "div",
          { class: "choice-list choice-stack", role: "radiogroup", "aria-labelledby": "basic-mode-legend" },
          el("div", { class: "choice-head field-label", id: "basic-mode-legend" }, "What it may do with your documents"),
          ["approve", "read", "none"].map((mode) =>
            el(
              "label",
              { class: ["choice", form.mode === mode && "is-selected"], key: `mode-${mode}` },
              el("input", {
                type: "radio",
                name: "basic-mode",
                value: mode,
                checked: form.mode === mode,
                disabled: form.busy,
                "aria-labelledby": `basic-mode-${mode}-name`,
                "aria-describedby": `basic-mode-${mode}-lines`,
                onchange: () => {
                  form.mode = mode;
                  form.error = "";
                  self.update();
                },
              }),
              el(
                "span",
                { class: "choice-text" },
                el("span", { class: "choice-label", id: `basic-mode-${mode}-name` }, GRANT_LABEL[mode]),
                el("span", { class: "choice-hint", id: `basic-mode-${mode}-lines` }, lines[mode]),
              ),
            ),
          ),
        ),
        // Shown at the moment access is given, in the sheet itself and never behind a press.
        form.mode !== "none" ? accessSentence(form) : null,
        form.mode !== "none" && form.closed ? opensLine(form) : null,
        form.error ? el("p", { class: "field-error", id: "basic-error", key: "error", role: "alert" }, form.error) : null,
        sheetFoot(
          button("Cancel", { onclick: () => self.close(null), autofocus: true }),
          // Only this press saves: a key pressed on a choice does not.
          button(form.busy ? "Saving…" : "Save", {
            kind: "primary",
            id: "basic-save",
            disabled: form.mode === form.inForce,
            busy: form.busy,
            onclick: () => saveAccess(ctx, self, form, form.mode),
          }),
        ),
        el(
          "div",
          { class: "sheet-more" },
          el(
            "a",
            { class: "link", id: "basic-steps", href: `#/setup/connect?app=${encodeURIComponent(app.name)}`, onclick: () => self.close(null) },
            "Show the connection steps",
          ),
          button("Remove this app", {
            kind: "quiet",
            small: true,
            id: "basic-remove",
            onclick: async () => {
              if (await removeApp(ctx, app)) self.close(null);
            },
          }),
        ),
      );
    },
    { label: app.name, wide: true },
  );
  const saved = await sheet.closed;
  if (typeof saved !== "string") return;
  await ctx.refresh();
  toast(`${app.name}: ${GRANT_LABEL[saved]}.`, "green");
}

/** "Change" in the row of an app: it opens the app's sheet. The sheet needs the vault's own figures, so it waits for them. */
function changeButton(ctx, app, settings) {
  return button("Change", {
    small: true,
    key: `change-${app.name}`,
    label: `Change what ${app.name} may do`,
    disabled: !settings,
    onclick: () => openAppSheet(ctx, app.name),
  });
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
      groups.length > 1 ? proLink("Change in Pro", "#/apps", { key: `change-${app.name}`, class: "link basic-app-change" }) : changeButton(ctx, app, settings),
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
  const fold = foldOf(ctx, "apps");
  if (fold && !fold.open) return section("basic-apps", "Apps", null, null, fold);
  const data = ctx.data.apps;
  if (!data) return section("basic-apps", "Apps", null, loadingBlock(), fold);
  const apps = data.apps;
  const groups = data.collections;
  // An app is added on this page only where one value will describe what it may do, and
  // where adding it cannot change which kinds of apps are answered.
  const expressible = groups.length <= 1 && !apps.some((app) => !app.hosted);
  return section("basic-apps", "Apps", null, [
    apps.length
      ? el("p", { class: "basic-hint", id: "basic-labels", key: "labels" }, NAME_IS_A_LABEL)
      : el("p", { class: "basic-none", key: "none" }, "No app has been added, so no app gets an answer."),
    apps.length ? el("div", { class: "card basic-app-list", key: "list" }, apps.map((app) => appRow(ctx, app, groups, ctx.data.settings, stops))) : null,
    el(
      "p",
      { class: "basic-app-add", key: "add" },
      expressible
        ? // The step that adds an app gives it "Ask me each time" for the vault's one group, and says so on its button.
          el("a", { class: "btn btn-default", id: "basic-add-app", href: "#/setup/app" }, el("span", { class: "btn-label" }, apps.length ? "Add an app" : "Choose your AI app"))
        : proLink("Add an app (in Pro)", "#/apps", { id: "basic-add-app" }),
    ),
  ], fold);
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
  // The heading is there only when the part has a line, folded or not.
  const fold = foldOf(ctx, "also");
  if (fold && !fold.open) return section("basic-also", "Also set for this vault, in Pro", null, null, fold);
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
  ], fold);
}

// -- the foot -----------------------------------------------------------------------

/** The sheet that says what the product is not: the statements of the Vault page, and the line about this version. */
function openCannot(ctx) {
  const title = `What ${PRODUCT} cannot do`;
  openSheet(
    (self) =>
      el(
        "div",
        { class: "sheet-content" },
        sheetHead(title),
        el("ul", { class: "cannot" }, CANNOT.map((statement, index) => el("li", { key: `cannot-${index}` }, statement))),
        el("p", { class: "version" }, versionLine(ctx.state)),
        sheetFoot(button("Close", { onclick: () => self.close(), autofocus: true })),
      ),
    { label: title, wide: true },
  );
}

/**
 * How the vault is opened again on another day: the command this page was told, and
 * what running it brings. Null in a demo, which is not opened again by that command:
 * its guide has the one for a vault of one's own.
 */
export function launchBlock(ctx) {
  const launch = ctx.state.launch || {};
  const command = ctx.state.demo ? null : launch.command;
  if (!command) return null;
  return el(
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
  );
}

function foot(ctx) {
  const state = ctx.state;
  return el(
    "footer",
    { class: "basic-foot", key: "basic-foot" },
    launchBlock(ctx),
    state.record && state.record.verified ? el("p", { key: "record" }, "The record of what left was checked when you unlocked.") : null,
    el("p", { key: "cannot" }, el("button", { type: "button", class: "link", id: "basic-cannot", onclick: () => openCannot(ctx) }, `What ${PRODUCT} cannot do`)),
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
    // Under the notices: the card that names the next step of setting up, or in a demo its guide.
    setupCard(ctx),
    demoGuide(ctx, stops),
    leftSection(ctx, stops),
    documentsSection(ctx),
    appsSection(ctx, stops),
    alsoSection(ctx),
    foot(ctx),
  ];
}
