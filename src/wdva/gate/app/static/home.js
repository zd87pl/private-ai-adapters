// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// Home: one quiet column. Top to bottom it shows what the owner has to know (the
// notices), what waits for the owner (the approval cards, which views.js draws), a
// box that asks the owner's own documents and shows the one passage an app would
// get, four tiles that connect an app, and the last few things that were given to
// apps. A part that has nothing to show is not drawn. For a vault whose first steps
// are open, the box and the tiles are the second and third of three numbered cards
// under "Start here", with the adding of a document as the first; nothing is drawn
// twice. In a demo one card says where the demo is.
//
// Every text is a text of the table. An app is named by the name it has in the
// vault; the readable name of a program is read for a tile and for nothing else.
//
// What this page changes: it adds a document, and beside the vault it says that the
// first steps are done. A trial question changes nothing and is recorded nowhere;
// the two buttons under its answer send nothing at all. What a tile does is in
// connect.js. In a demo one request has the played app ask once more.

import { api } from "./api.js";
import { openConnect, openCustom } from "./connect.js";
import { el, icon } from "./dom.js";
import { agoShort, megabytes, num, parseTime } from "./format.js";
import { SAMPLE, fileLine, loadingBlock, sampleOf, sessionFiles, uploadFiles, whoLine } from "./manage.js";
import { activityRow, addToWhichGroup } from "./pages.js";
import { button, chip, copyLine, errorText, pathText, toast, toastError } from "./parts.js";
import { language, t } from "./texts.js";
import { approvalCards, putAway } from "./views.js";

/** How many waiting passages are shown as cards before "Show N more" is pressed. */
const CARDS_AT_FIRST = 3;
/** How many of the newest things that were given to apps Home shows. */
const LEFT_SHOWN = 3;
const DAY_MS = 86400000;
/**
 * How long after "Have the played app ask again" the page asks for the lists itself.
 * The played app has made its call well before: it looks for the request several
 * times a second.
 */
const AGAIN_MS = 2000;

/** The group that documents go to in a vault that has none yet. */
const FIRST_GROUP = "documents";

/**
 * What Home remembers. It is this page's memory and nothing else: a lock or a reload
 * starts it anew, and leaving Home drops the answer of the box.
 */
export function freshHome() {
  return {
    // The box that asks: what the owner typed, the request that is under way, the answer,
    // what went wrong, which button under the answer was pressed, and whether a passage was seen.
    ask: { question: "", typed: false, busy: false, answer: null, failed: null, pressed: null, saw: false },
    // "Start here": the sample that is being sent, the sample this page added, and whether
    // the app was told that the first steps are done.
    first: { sampleBusy: false, sample: null, said: false },
    // The demo's card: whether it was hidden, the owner's last answer to a waiting passage as
    // far as this page saw it, the answers it has taken note of, the entry of a passage that
    // was read after a yes, and whether the played app was just told to ask again.
    guide: { hidden: false, decision: null, noted: new WeakSet(), read: null, asking: false },
  };
}

/** Leaving Home drops the answer of the box and what was typed into it. */
export function leaveHome(ctx) {
  const saw = ctx.ui.home.ask.saw;
  ctx.ui.home.ask = freshHome().ask;
  ctx.ui.home.ask.saw = saw;
}

// -- pieces -------------------------------------------------------------------------

/** One part of Home under its heading, named by that heading for a screen reader. */
function part(key, title, aside, content) {
  const id = `${key}-title`;
  return el(
    "section",
    { class: "home-part", key, "aria-labelledby": id },
    el("div", { class: "part-head" }, el("h2", { class: "part-title", id }, title), aside),
    content,
  );
}

/** Whether a passage waits for the owner: then no button of the page is filled. */
function somethingWaits(ctx) {
  return ctx.state.today ? ctx.state.today.approvals_waiting > 0 : false;
}

/** How many documents the vault holds. */
function documentsHeld(ctx) {
  return ctx.state.encryption ? ctx.state.encryption.documents : 0;
}

// -- the notices --------------------------------------------------------------------

/** The sentence of each warning of the state that the page words itself, by the warning's code. */
const NOTICE = {
  previous_session_unclean: "notice.previous_session_unclean.say.1",
  both_kinds_served: "notice.both_kinds_served.say.1",
  synced_folder: "notice.synced_folder.say.1",
  documents_unavailable: "notice.documents_unavailable.say.1",
  no_sdk: "notice.no_sdk.say.1",
  python_too_old: "notice.python_too_old.say.1",
};

/** Notices that ask nothing of the owner: they can be put away. */
const ASKS_NOTHING = ["previous_session_unclean"];

/** A notice whose detail is a command: it is a line with "Copy". Any other detail is a list of names or places. */
const DETAIL_IS_A_COMMAND = ["no_sdk"];

/** The calls that were stopped in the last 24 hours, counted. A passage that waits for the owner is not among them: it is a card. */
function stopsOfTheDay(ctx) {
  const since = ctx.now.getTime() - DAY_MS;
  let calls = 0;
  for (const item of ctx.stops || []) {
    const time = parseTime(item.time);
    if (item.code === "NEEDS_APPROVAL" || !time || time.getTime() <= since) continue;
    calls += typeof item.count === "number" ? item.count : 1;
  }
  return calls;
}

/** The warnings of the state that Home shows: all of them but what the owner has put away. */
function warningsShown(ctx) {
  return (ctx.state.warnings || []).filter((warning) => !putAway.has(warning.code));
}

/** One notice: its one sentence, and what unfolds or puts it away. */
function notice(ctx, warning) {
  const own = Object.prototype.hasOwnProperty;
  const detail = warning.detail || [];
  const open = ctx.ui.openWarnings.has(warning.code);
  const quiet = ASKS_NOTHING.includes(warning.code);
  // A warning the table has no sentence for is shown in the app's own English words.
  const words = own.call(NOTICE, warning.code) ? t(NOTICE[warning.code], { n: detail.length }) : warning.text;
  return el(
    "li",
    { class: ["notice", quiet && "notice-quiet"], key: `notice-${warning.code}` },
    icon(quiet ? "info" : "warning"),
    el(
      "div",
      { class: "notice-text" },
      el("p", null, words),
      open && detail.length
        ? DETAIL_IS_A_COMMAND.includes(warning.code)
          ? detail.map((line, index) => copyLine(ctx, `notice-copy-${warning.code}-${index}`, line, words))
          : el("ul", { class: "path-list" }, detail.map((line) => el("li", null, pathText(line))))
        : null,
    ),
    detail.length
      ? button(open ? t("common.button.hide") : t("notice.button.show"), {
          kind: "quiet",
          small: true,
          expanded: open ? "true" : "false",
          onclick: () => {
            if (open) ctx.ui.openWarnings.delete(warning.code);
            else ctx.ui.openWarnings.add(warning.code);
            ctx.render();
          },
        })
      : null,
    quiet
      ? button(t("notice.button.ok"), {
          small: true,
          onclick: () => {
            putAway.add(warning.code);
            ctx.render();
          },
        })
      : null,
  );
}

/**
 * The notices: one line per warning of the state, one when apps on this computer wait
 * for the owner's choice, and one when calls were stopped in the last 24 hours.
 * Nothing is drawn when there is none.
 */
function notices(ctx) {
  const lines = warningsShown(ctx).map((warning) => notice(ctx, warning));
  if (ctx.state.serving && ctx.state.serving.choice_needed) {
    lines.push(
      el(
        "li",
        { class: "notice", key: "notice-choice" },
        icon("warning"),
        el("div", { class: "notice-text" }, el("p", null, t("notice.choice_needed.say.1"))),
        el("a", { class: "btn btn-default btn-small", id: "notice-choice", href: "#/pro/apps" }, el("span", { class: "btn-label" }, t("nav.settings"))),
      ),
    );
  }
  const stopped = stopsOfTheDay(ctx);
  if (stopped > 0) {
    lines.push(
      el(
        "li",
        { class: "notice notice-quiet", key: "notice-stops" },
        icon("refused"),
        el("div", { class: "notice-text" }, el("p", null, t("home.notice.status.stops", { n: stopped }))),
        el("a", { class: "btn btn-quiet btn-small", id: "home-stops", href: "#/activity?show=stopped" }, el("span", { class: "btn-label" }, t("home.notice.button.see"))),
      ),
    );
  }
  return lines.length ? el("ul", { class: "notices", key: "notices" }, lines) : null;
}

/** What stands above the cards and can come or go, in one text: a change of it makes "Release once" wait again. */
function noticesSignature(ctx) {
  const codes = warningsShown(ctx).map((warning) => `${warning.code}${ctx.ui.openWarnings.has(warning.code) ? "+" : ""}`);
  if (ctx.state.serving && ctx.state.serving.choice_needed) codes.push("choice");
  if (stopsOfTheDay(ctx) > 0) codes.push("stops");
  return codes.join(",");
}

// -- Waiting for you ----------------------------------------------------------------

/**
 * What a screen reader is told when the number of waiting passages rises, or returns to
 * none. What is kept is the number that was said, not the words: the words are those of
 * the language the page shows now.
 */
function waitingSaid(ctx, waiting) {
  const ui = ctx.ui;
  if (waiting !== null) {
    if (ui.waitingCount !== null) {
      if (waiting > ui.waitingCount) ui.waitingSaid = waiting;
      else if (waiting === 0 && ui.waitingCount > 0) ui.waitingSaid = 0;
    }
    ui.waitingCount = waiting;
  }
  if (typeof ui.waitingSaid !== "number") return "";
  return ui.waitingSaid > 0 ? t("home.wait.status.said", { n: ui.waitingSaid }) : t("home.wait.status.none");
}

/**
 * "Waiting for you": the cards of the passages that wait, three at first, and under
 * them what was released and not read yet. Drawn only when there is one of the two.
 * The cards come from the one function that draws cards, with everything near them
 * that this page can change.
 */
function waitingPart(ctx, problem) {
  const data = ctx.data.approvals;
  const all = ctx.ui.allApprovals === true;
  const box = ctx.ui.home.ask;
  // Everything near the cards that this page can change. A change of it makes "Release once" wait again.
  const around = [
    ctx.offline ? "offline" : problem,
    ctx.ui.copied === "bar-launch-copy" ? "copied" : "",
    noticesSignature(ctx),
    all ? "all" : "",
    ctx.panel ? "panel" : "",
    // A sheet of this page lies over the cards. When it closes, a second press that was meant
    // for one of its buttons would meet whatever is under it.
    ctx.ui.sheets > 0 ? "sheet" : "",
    // The answer of the box that asks stands under the cards: it comes and goes with a press.
    box.answer ? "trial" : "",
  ].join("\n");
  const drawn = approvalCards(ctx, { limit: all ? null : CARDS_AT_FIRST, around });
  const said = el("p", { class: "sr-only", role: "status", "aria-live": "polite", key: "said" }, waitingSaid(ctx, data ? data.waiting.length : null));
  if (drawn === null || (drawn.cards.length === 0 && !drawn.granted)) return said;
  const waiting = data.waiting.length;
  return [
    said,
    part(
      "home-wait",
      t("home.wait.title"),
      waiting > 0 ? el("span", { class: "count-badge", id: "home-wait-count" }, num(waiting)) : null,
      el(
        "div",
        { class: "approval-stack", key: "stack" },
        drawn.cards,
        drawn.more > 0
          ? button(t("home.wait.button.more", { n: drawn.more }), {
              id: "home-more-approvals",
              key: "more",
              onclick: () => {
                ctx.ui.allApprovals = true;
                ctx.render();
              },
            })
          : null,
        drawn.granted,
      ),
    ),
  ];
}

// -- the box that asks, and its answer -----------------------------------------------

/** The sample that is the vault's only document, as its language: "en", "pl", or null. */
function onlySample(ctx) {
  const data = ctx.data.documents;
  if (!data) return null;
  const held = [];
  for (const group of data.collections) {
    for (const document of group.documents) held.push(document);
  }
  return held.length === 1 ? sampleOf(held[0]) : null;
}

/**
 * What the input of the box holds: what the owner typed; and until something was
 * typed, the sample question while the sample of the language shown is the vault's
 * only document, so that one press on "Ask" shows a passage.
 */
function shownQuestion(ctx) {
  const box = ctx.ui.home.ask;
  if (box.typed) return box.question;
  return onlySample(ctx) === language() ? t("ask.sample.question") : "";
}

/**
 * "Ask": the owner's own question goes to the app, which answers with the one passage
 * an app would get for it. Nothing is sent anywhere else and nothing is recorded. The
 * app may ask for the passphrase first; that is the work of the one module that sends
 * requests, and typing it there opens this box and nothing else.
 */
async function ask(ctx) {
  const box = ctx.ui.home.ask;
  const question = shownQuestion(ctx).trim();
  if (box.busy || !question || documentsHeld(ctx) === 0) return;
  box.question = question;
  box.typed = true;
  box.busy = true;
  box.failed = null;
  ctx.render();
  try {
    const answer = await api.tryQuestion(question);
    box.answer = answer;
    box.pressed = null;
    if (answer.found === true) box.saw = true;
  } catch (error) {
    // The owner closed the sheet that asks for the passphrase: nothing was asked, and nothing is said.
    box.failed = error.code === "cancelled" ? null : error;
  }
  box.busy = false;
  // How many trial questions were asked is part of the state, which a trial does not move.
  await ctx.refresh();
  ctx.render();
}

/**
 * Which first sentence the answer gets. It is chosen from what the vault's settings
 * say about the document that was found, never fixed: who can ask for it, and how.
 */
function trialSentence(passage) {
  const apps = passage.apps || [];
  const readers = apps.filter((app) => app.mode === "read").map((app) => app.name);
  // An app that holds "Can read" is named by the name it has in the vault.
  if (readers.length) return t("ask.say.1.read", { apps: readers });
  // Every app that can ask for it asks the owner each time.
  if (apps.length) return t("ask.say.1");
  // No app can ask for it: with one group or none, a connect from Home would let one; with several, none can yet.
  return passage.groups > 1 ? t("ask.say.1.none") : t("ask.say.1.connect");
}

/**
 * The answer of the box: the one passage, the words it was found by, and the two
 * answers of a real card, which here send nothing: a press says what would have
 * happened, and nothing happened. It is no approval card: it has a class of its own,
 * neither button is filled, and neither waits. While a real passage waits for the
 * owner it has no buttons at all: a press that is harmless here must not train the
 * hand for the card above, where it is not.
 */
function trialCard(ctx) {
  const box = ctx.ui.home.ask;
  const answer = box.answer;
  if (!answer) return null;
  if (answer.found !== true) {
    return el("p", { class: "trial-none", id: "trial-none", key: "trial", role: "status" }, t(answer.reason === "no_words" ? "ask.result.nowords" : "ask.result.none"));
  }
  const passage = answer.passage;
  const real = ctx.data.approvals ? ctx.data.approvals.waiting.length > 0 : somethingWaits(ctx);
  const again = () => {
    box.question = "";
    box.typed = true;
    box.answer = null;
    box.pressed = null;
    ctx.ui.focusId = "ask-input";
    ctx.render();
  };
  let foot;
  if (real) {
    foot = el("p", { class: "trial-real", id: "trial-real", key: "real" }, t("ask.status.real"));
  } else if (box.pressed) {
    foot = el(
      "div",
      { class: "trial-result", key: "result" },
      el("p", { id: "trial-result", role: "status" }, t(box.pressed === "yes" ? "ask.result.yes" : "ask.result.no")),
      button(t("ask.button.again"), { id: "trial-again", small: true, onclick: again }),
    );
  } else {
    foot = el(
      "div",
      { class: "trial-actions", key: "actions" },
      button(t("card.button.no"), {
        id: "trial-no",
        onclick: () => {
          box.pressed = "no";
          ctx.ui.focusId = "trial-again";
          ctx.render();
        },
      }),
      button(t("card.button.yes"), {
        id: "trial-yes",
        onclick: () => {
          box.pressed = "yes";
          ctx.ui.focusId = "trial-again";
          ctx.render();
        },
      }),
    );
  }
  return el(
    "article",
    { class: "card trial", id: "trial", key: "trial", "aria-label": t("ask.chip.trial") },
    el("div", { class: "trial-head" }, chip(t("ask.chip.trial"), "accent")),
    el("p", { class: "trial-say", id: "trial-say-1" }, trialSentence(passage)),
    el("pre", { class: "code code-wrap released", tabindex: 0 }, el("code", null, passage.body)),
    el(
      "div",
      { class: "trial-facts" },
      el("p", { id: "trial-meta" }, t("ask.status.meta", { n: passage.characters, passage: passage.passage, passages: passage.passages, title: passage.title })),
      // By which words of the question it was found: a passage found by "is" alone must not look like an answer.
      passage.words && passage.words.length ? el("p", { id: "trial-words" }, t("ask.status.words", { words: passage.words.join(", ") })) : null,
      passage.replaced > 0 ? el("p", { id: "trial-replaced" }, t("ask.status.replaced", { n: passage.replaced })) : null,
    ),
    el("p", { class: "trial-say", id: "trial-say-2" }, t("ask.say.2")),
    foot,
  );
}

/**
 * The box itself: one input and "Ask" in one rounded frame, under them what went
 * wrong or the answer, and in small grey type how many trial questions were asked
 * since the vault was unlocked. Where more were asked since the app was started, a
 * second line says how many: locking needs no passphrase and begins the first number
 * anew, so the first alone would not show that somebody asked before the last lock.
 * While the vault holds no document the input and "Ask" are off and one line says why.
 * "Ask" is the filled button of Home once the vault holds a document, and never while
 * a passage waits.
 */
function asker(ctx) {
  const box = ctx.ui.home.ask;
  const none = documentsHeld(ctx) === 0;
  const question = shownQuestion(ctx);
  const asked = ctx.state.trial ? ctx.state.trial.questions : 0;
  const sinceStart = ctx.state.trial ? ctx.state.trial.since_start : 0;
  let failed = null;
  if (box.failed) failed = box.failed.code === "too_many" ? t("ask.error.slow") : errorText(box.failed);
  return [
    el(
      "form",
      {
        class: ["asker", none && "is-off"],
        key: "asker",
        autocomplete: "off",
        onsubmit: (event) => {
          event.preventDefault();
          ask(ctx);
        },
      },
      el("input", {
        type: "text",
        id: "ask-input",
        class: "asker-input",
        value: question,
        placeholder: t("ask.placeholder"),
        "aria-label": t("ask.label.input"),
        "aria-describedby": none ? "ask-nodocs" : null,
        maxlength: 500,
        disabled: none,
        oninput: (event) => {
          box.question = event.target.value;
          box.typed = true;
          ctx.render();
        },
      }),
      button(box.busy ? t("ask.button.busy") : t("ask.button.ask"), {
        kind: none || somethingWaits(ctx) ? "default" : "primary",
        type: "submit",
        id: "ask-button",
        busy: box.busy,
        disabled: none || !question.trim(),
      }),
    ),
    none ? el("p", { class: "asker-note", id: "ask-nodocs", key: "nodocs" }, t("ask.say.1.nodocs")) : null,
    failed ? el("p", { class: "field-error", id: "ask-error", key: "error", role: "alert" }, failed) : null,
    none ? null : trialCard(ctx),
    asked > 0 ? el("p", { class: "asker-count", id: "ask-count", key: "count" }, t("ask.status.count", { n: asked })) : null,
    sinceStart > asked ? el("p", { class: "asker-count", id: "ask-count-start", key: "count-start" }, t("ask.status.count.start", { n: sinceStart })) : null,
  ];
}

// -- the tiles ----------------------------------------------------------------------

/** The word of each tile, by what the app calls it. */
const TILE_NAME = { claude: "tile.claude", chatgpt: "tile.chatgpt", grok: "tile.grok", custom: "tile.custom" };

/**
 * One tile: a button with a mark, the name and one line of state. The line is read
 * from the vault and from nothing else: whether an app of the tile's name is
 * registered, whether it is paused, when it last asked. No tile says that an app is
 * connected: that word needs a look at a file, which happens on a press. A press
 * opens the tile's sheet.
 */
function tile(ctx, entry, apps) {
  const own = Object.prototype.hasOwnProperty;
  const custom = entry.tile === "custom";
  const name = t(own.call(TILE_NAME, entry.tile) ? TILE_NAME[entry.tile] : "tile.custom");
  let line;
  let on = false;
  if (custom) {
    // Every app that is none of the three tiles' own.
    const named = ctx.data.apps.tiles.filter((other) => other.tile !== "custom").map((other) => other.name);
    const others = apps.filter((app) => !named.includes(app.name)).length;
    on = others > 0;
    line = on ? t("tile.status.custom", { n: others }) : t("tile.status.custom.connect");
  } else {
    const app = apps.find((one) => one.name === entry.name);
    on = Boolean(app);
    if (!app) line = t("tile.status.connect");
    else if (app.paused) line = t("tile.status.paused");
    else if (app.last_seen && app.last_seen.time) line = t("tile.status.seen", { ago: agoShort(app.last_seen.time, ctx.now) });
    else line = t("tile.status.never");
  }
  const id = `tile-${entry.tile}`;
  return el(
    "button",
    {
      type: "button",
      class: ["tile", on && "is-on"],
      id,
      key: id,
      "aria-label": t("tile.label.open", { label: custom ? name : entry.label }),
      "aria-describedby": `${id}-state`,
      onclick: () => (custom ? openCustom(ctx) : openConnect(ctx, entry)),
    },
    el("span", { class: "tile-mark" }, icon("plug")),
    el("span", { class: "tile-name" }, name),
    el("span", { class: "tile-state", id: `${id}-state` }, line),
  );
}

/** The four tiles, in the order the app names them, and under them the sentence about what can be connected. */
function tiles(ctx, sentence) {
  const data = ctx.data.apps;
  if (!data) return loadingBlock();
  return [
    el("div", { class: "tiles", key: "tiles" }, (data.tiles || []).map((entry) => tile(ctx, entry, data.apps))),
    sentence ? el("p", { class: "home-say", id: "home-connect-say", key: "say" }, t("home.connect.say.1")) : null,
  ];
}

// -- Recently given to apps ---------------------------------------------------------

/** The three newest things that were given to apps, as the rows the page Activity has, and the way to all of them. */
function leftPart(ctx) {
  const data = ctx.data.apps;
  if (!data || !data.apps.length) return null;
  const left = ctx.left;
  let content;
  if (!left.loaded) content = loadingBlock();
  else if (!left.order.length) content = el("p", { class: "rows-none", id: "home-left-none", key: "none" }, t("home.left.status.none"));
  else {
    content = el(
      "ul",
      { class: "rows act-rows", key: "rows", "aria-label": t("home.left.title") },
      left.order.slice(0, LEFT_SHOWN).map((seq) => activityRow(ctx, left.items.get(seq))),
    );
  }
  return part("home-left", t("home.left.title"), el("a", { class: "link part-link", id: "home-all", href: "#/activity" }, t("home.left.button.all")), content);
}

// -- Start here ---------------------------------------------------------------------

/** Whether the app keeps a word about the first steps for this vault, and that word says they are open. Never in a demo. */
function firstOpen(ctx) {
  const kept = ctx.state.interface;
  return Boolean(kept && kept.stored && kept.setup === "open") && ctx.state.demo !== true;
}

/**
 * What is true of the vault now, of the three things the cards lead to: it holds a
 * document; the owner saw a passage in the box, or an app has asked; an app is
 * registered. Null until the list of apps is in.
 */
function firstFacts(ctx) {
  const data = ctx.data.apps;
  if (!data) return null;
  return {
    document: documentsHeld(ctx) > 0,
    asked: ctx.ui.home.ask.saw || data.apps.some((app) => Boolean(app.last_seen)),
    app: data.apps.length > 0,
  };
}

/**
 * Say that the first steps are done. The app keeps that as one word beside the vault,
 * not sealed; nothing of the vault changes. Nothing is sent for a vault whose file
 * does not say that they are open. Resolves to null, or to what went wrong.
 */
async function firstDone(ctx) {
  if (!firstOpen(ctx)) return null;
  try {
    ctx.applyState(await api.setInterface({ setup: "done" }));
    return null;
  } catch (error) {
    return error;
  }
}

/**
 * The cards go by themselves: once the vault holds a document, the owner has seen a
 * passage or an app has asked, and an app is registered, the page tells the app that
 * the first steps are done, once, on whatever page it is. It is the one request the
 * page sends without a press, and it is never sent for a vault whose file does not
 * say that the first steps are open.
 */
export function settleFirst(ctx) {
  if (!ctx.state || ctx.state.locked || !firstOpen(ctx) || ctx.ui.home.first.said) return;
  const facts = firstFacts(ctx);
  if (!facts || !facts.document || !facts.asked || !facts.app) return;
  ctx.ui.home.first.said = true;
  firstDone(ctx);
}

/** The groups of the vault by name, or the one that documents go to in a vault that has none. */
function groupNames(ctx) {
  const data = ctx.data.documents;
  const names = data ? data.collections.map((group) => group.name) : [];
  return names.length ? names : [FIRST_GROUP];
}

/** After a document was added, the box is next: its input takes the focus. */
function toTheBox(ctx) {
  ctx.ui.focusId = "ask-input";
  ctx.render();
}

/** "Choose a file": the files go to the vault's one group. With several groups the owner is asked first which one. */
async function addFiles(ctx, files) {
  if (!files.length) return;
  const groups = groupNames(ctx);
  if (groups.length > 1) await addToWhichGroup(ctx, files);
  else await uploadFiles(ctx, groups[0], files);
  if (documentsHeld(ctx) > 0) toTheBox(ctx);
}

/**
 * "Use a sample document": the invented sample of the language the page shows goes to
 * the vault through the route every document takes. It is sent here and by nothing
 * else, on the press of its own button, so it is never among the files the owner chose.
 */
async function addSample(ctx, group) {
  const first = ctx.ui.home.first;
  if (first.sampleBusy) return;
  const sample = SAMPLE[language()] || SAMPLE.en;
  first.sampleBusy = true;
  ctx.render();
  try {
    await api.addDocument(group, sample.file, sample.text);
    first.sample = sample.title;
    // The box then holds the sample question, whatever was typed into it before.
    ctx.ui.home.ask.typed = false;
  } catch (error) {
    toastError(error);
  }
  first.sampleBusy = false;
  await ctx.refresh();
  if (first.sample) toTheBox(ctx);
}

/** The head of one of the three cards: its number in a circle, or a check once its fact holds, and its title. */
function cardHead(number, done, title) {
  return el(
    "h3",
    { class: "first-title" },
    // The check says "done", never "sealed": it is not drawn in green.
    el("span", { class: ["first-num", done && "is-done"] }, done ? [icon("check"), el("span", { class: "sr-only" }, t("home.new.label.done"))] : String(number)),
    el("span", null, title),
  );
}

/**
 * Card 1, "Add a document": two buttons and the line that says what can be read.
 * Under them one line per file says what became of it, who can ask for it from now
 * on, and once that the file which was chosen is still where it was. Once the vault
 * holds a document, the buttons give way to one line that says how many it holds.
 */
function addCard(ctx, facts) {
  const first = ctx.ui.home.first;
  const data = ctx.data.documents;
  const rules = (data && data.upload) || { types: [], max_bytes: 20971520 };
  const groups = groupNames(ctx);
  const chosen = [];
  for (const name of groups) {
    for (const item of sessionFiles(ctx, name)) chosen.push({ item, group: name });
  }
  const sealed = chosen.filter((entry) => entry.item.status === "done");
  // The command that adds what reads PDFs, shown once where a file was refused for the lack of it.
  const install = chosen.some((entry) => entry.item.install) && typeof rules.install === "string" ? rules.install : "";
  const held = documentsHeld(ctx);
  const filled = !somethingWaits(ctx);
  // The line about the sample is said while the vault holds a document: a sample that was removed again is not spoken of.
  const sample = first.sample && held > 0 ? first.sample : null;
  // Adding a document never asks for the passphrase, so who can ask for it is said where it is added.
  const who = groups.filter((name) => sessionFiles(ctx, name).some((item) => item.status === "done") || (sample && groups.length === 1));
  return el(
    "section",
    { class: "first-card", id: "first-add", key: "first-add" },
    cardHead(1, facts.document, t("home.new.card1.title")),
    held > 0
      ? el("p", { class: "first-status", id: "first-count", key: "count" }, t("home.new.card1.status", { n: held }))
      : !data
        ? loadingBlock()
        : [
            el(
              "div",
              { class: "first-actions", key: "actions" },
              el(
                "label",
                { class: ["btn", filled ? "btn-primary" : "btn-default", "file-button"], for: "first-file" },
                el("span", { class: "btn-label" }, t("home.new.card1.button")),
                el("input", {
                  type: "file",
                  id: "first-file",
                  class: "sr-only",
                  multiple: true,
                  accept: rules.types.join(","),
                  "aria-label": t("home.new.card1.button"),
                  onchange: (event) => {
                    const files = Array.from(event.target.files || []);
                    event.target.value = "";
                    addFiles(ctx, files);
                  },
                }),
              ),
              // The sample goes to the vault's one group: where there are several, none is chosen for the owner.
              groups.length === 1
                ? button(t("home.new.card1.button.sample"), { kind: "quiet", id: "first-sample", busy: first.sampleBusy, onclick: () => addSample(ctx, groups[0]) })
                : null,
            ),
            // What can be read stands where the choice is made: the file dialog greys the rest out without a word.
            el("p", { class: "first-hint", key: "types" }, t("docs.status.types", { size: megabytes(rules.max_bytes) })),
          ],
    chosen.length || sample
      ? el(
          "ul",
          { class: "upload-list first-files", key: "files" },
          chosen.map((entry, index) => fileLine(entry.item, `u${index}-${entry.item.name}`, groups.length > 1 ? entry.group : null)),
          sample ? el("li", { class: "upload upload-done", id: "first-sample-said", key: "sample" }, icon("check"), el("span", { class: "upload-text" }, el("span", { class: "upload-note" }, t("home.new.result.sample")))) : null,
          who.map((name) => whoLine(ctx, name)),
          install ? el("li", { class: "upload-install", key: "install" }, el("span", { class: "upload-install-label" }, t("docs.label.install")), copyLine(ctx, "install-copy-first", install, t("docs.label.install"))) : null,
          // Said once: a sealed copy is in the vault, and the file that was chosen is still where it was.
          sealed.length ? el("li", { class: "upload-said", key: "outside" }, el("p", { class: "first-sealed", id: "first-sealed" }, t("home.new.result.sealed"))) : null,
        )
      : null,
  );
}

/**
 * "Start here": the three cards of a new vault. They are the parts of Home
 * themselves: card 2 holds the box that asks, card 3 the four tiles. "Hide" ends
 * them for good, and they go by themselves once all three are done.
 */
function firstSteps(ctx) {
  const facts = firstFacts(ctx) || { document: documentsHeld(ctx) > 0, asked: false, app: false };
  const apps = ctx.data.apps ? ctx.data.apps.apps : [];
  // The sentence is true of what Home gives. While an app holds "Can read", it would be read as a statement about that app.
  const reads = apps.some((app) => Object.values(app.grants || {}).includes("read"));
  return el(
    "section",
    { class: "first", key: "first", "aria-labelledby": "first-title" },
    el(
      "div",
      { class: "part-head" },
      el("h2", { class: "part-title", id: "first-title" }, t("home.new.title")),
      button(t("common.button.hide"), {
        kind: "quiet",
        small: true,
        id: "first-hide",
        label: t("home.new.label.hide"),
        onclick: async () => {
          const failed = await firstDone(ctx);
          if (failed) toastError(failed);
          else toast(t("home.new.result.hidden"), "plain");
        },
      }),
    ),
    reads ? null : el("p", { class: "home-say", id: "first-say", key: "say" }, t("home.new.say.1")),
    addCard(ctx, facts),
    el("section", { class: "first-card", id: "first-ask", key: "first-ask" }, cardHead(2, facts.asked, t("home.ask.title")), asker(ctx)),
    // The tiles can be pressed from the first moment. The sentence under them is drawn once the vault holds a document.
    el("section", { class: "first-card", id: "first-connect", key: "first-connect" }, cardHead(3, facts.app, t("home.new.card3.title")), tiles(ctx, facts.document)),
  );
}

// -- the demo's card ----------------------------------------------------------------

/** Whether the demo's card is on the page. */
function guideOpen(ctx) {
  return ctx.state.demo === true && !ctx.ui.home.guide.hidden;
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
  const guide = ctx.ui.home.guide;
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

/** The title and the one sentence of each moment of the demo, and whether it offers "Have the played app ask again". */
const GUIDE = {
  watch: { title: "guide.watch.title", say: "guide.watch.say.1", again: false },
  turn: { title: "guide.turn.title", say: "guide.turn.say.1", again: false },
  released: { title: "guide.released.title", say: "guide.released.say.1", again: true },
  kept: { title: "guide.kept.title", say: "guide.kept.say.1", again: true },
  read: { title: "guide.read.title", say: "guide.read.say.1", again: false },
  asked: { title: "guide.asked.title", say: "guide.asked.say.1", again: true },
};

/**
 * "Have the played app ask again": what telling a real app "try again" is. The app
 * that is played then makes one call, which is answered like any app's call, and
 * this page hears of it like of any call. Nothing tells the played app what the
 * owner decided: after a release it gets the passage, after a "no" the passage waits
 * again.
 */
async function askAgain(ctx) {
  const guide = ctx.ui.home.guide;
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
    // says so takes it: the title of the page, or the title of the card. Never a button of a card.
    const held = document.activeElement;
    if (ctx.route === "home" && guideOpen(ctx) && (!held || held === document.body || held.id === "demo-again")) {
      const beat = guideBeat(ctx);
      if (beat === "turn") ctx.ui.focusId = "home-title";
      else if (beat === "read") ctx.ui.focusId = "guide-title";
    }
    ctx.render();
  }, AGAIN_MS);
}

/**
 * The demo's card, between "Waiting for you" and the box that asks. It stands below
 * the approval card, which is the real one and complete without it. It never leads
 * to one answer: both answers are followed by the same button. From the owner's
 * first answer on it also shows the command that starts a vault of the owner's own.
 * No sentence of it names a time.
 */
function demoCard(ctx) {
  if (!guideOpen(ctx)) return null;
  const guide = ctx.ui.home.guide;
  const beat = guideBeat(ctx);
  const words = beat ? GUIDE[beat] : null;
  // From the owner's first answer on, the way to a vault of one's own is shown.
  const answered = beat !== null && beat !== "watch" && beat !== "turn";
  const command = (ctx.state.launch || {}).command;
  return el(
    "section",
    { class: "card guide", key: "demo-guide", "aria-labelledby": "guide-title" },
    el(
      "header",
      { class: "guide-head" },
      chip(t("common.chip.demo"), "accent"),
      el("h2", { class: "guide-title", id: "guide-title", tabindex: -1 }, words ? t(words.title) : ""),
      button(t("common.button.hide"), {
        kind: "quiet",
        small: true,
        id: "guide-hide",
        label: t("guide.label.hide"),
        onclick: () => {
          guide.hidden = true;
          // The button has gone: the input of the box takes the focus.
          ctx.ui.focusId = "ask-input";
          ctx.render();
        },
      }),
    ),
    words ? el("p", { class: "guide-say", id: "guide-say", key: "say" }, t(words.say)) : loadingBlock(),
    words && words.again
      ? el(
          "div",
          { class: "guide-actions", key: "again" },
          // Never filled: the page has one filled button at most, and it is not this one.
          button(guide.asking ? t("guide.button.busy") : t("guide.button.again"), { id: "demo-again", busy: guide.asking, onclick: () => askAgain(ctx) }),
        )
      : null,
    answered && command ? el("div", { class: "guide-own", key: "own" }, el("p", { class: "guide-say", id: "guide-own" }, t("guide.say.2")), copyLine(ctx, "guide-copy", command, t("bar.label.command"))) : null,
  );
}

// -- the page -----------------------------------------------------------------------

/**
 * Everything in the column of Home, under the bar that says what is wrong. Its title
 * is said to a screen reader and takes the focus when the page opens: never a button
 * of a card.
 */
export function homePage(ctx, problem) {
  const first = firstOpen(ctx);
  return {
    title: null,
    body: [
      el("h1", { class: "sr-only", id: "home-title", tabindex: -1, key: "title" }, t("home.title")),
      notices(ctx),
      waitingPart(ctx, problem),
      demoCard(ctx),
      first
        ? firstSteps(ctx)
        : [
            part("home-ask", t("home.ask.title"), null, asker(ctx)),
            part("home-connect", t("home.connect.title"), null, tiles(ctx, true)),
            // What was given to apps is not drawn while the three cards are.
            leftPart(ctx),
          ],
      el("footer", { class: "home-foot", key: "foot" }, el("a", { class: "link", id: "home-how", href: "#/how" }, t("home.button.how"))),
    ],
  };
}
