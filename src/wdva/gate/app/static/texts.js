// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The texts of the page, in the language it shows. This is the one module that reads
// the two tables. A script asks for a text by its key, `t("pair.title")`, and hands
// over what the text has places for: `t("card.title", { app, title })`.
//
// Which language is shown: what the owner pressed since this page was loaded; else
// what the app keeps beside the vault; else the browser's. The press is kept in this
// module's memory and nowhere in the browser. Keeping it beside the vault is the
// app's work, and main.js asks for that.

import { ENTRY, PRODUCT } from "./brand.js";
import { list, num, useLanguage } from "./format.js";
import { TEXT as ENGLISH } from "./text-en.js";
import { TEXT as POLISH } from "./text-pl.js";

export const LANGUAGES = ["en", "pl"];

const TABLES = { en: ENGLISH, pl: POLISH };

/** The first language of the browser's list that begins with "pl" or "en", else "en". */
export function firstLanguage(listed) {
  for (const entry of Array.from(listed || [])) {
    const found = /^(pl|en)(-|$)/i.exec(String(entry));
    if (found) return found[1].toLowerCase();
  }
  return "en";
}

let shown = "en";
/** What the owner pressed since this page was loaded, or null. It is gone with the page. */
let pressed = null;
const plurals = {};

/** "en" or "pl": the language the page shows now. */
export function language() {
  return shown;
}

/** Show the page in `code` from now on. Sets the language of the document and tells format.js. */
export function setLanguage(code) {
  if (!LANGUAGES.includes(code)) return;
  shown = code;
  document.documentElement.lang = code;
  useLanguage(code, { justNow: TABLES[code]["common.status.justnow"] });
}

/** The owner pressed a language: it is shown at once and remembered while this page is loaded. */
export function chooseLanguage(code) {
  if (!LANGUAGES.includes(code)) return;
  pressed = code;
  setLanguage(code);
}

/** The language the owner pressed since this page was loaded, or null. */
export function chosenLanguage() {
  return pressed;
}

/** The browser's own choice, which decides while nothing was pressed and nothing is kept. */
function browserLanguage() {
  return firstLanguage(navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language]);
}

/**
 * The app said which language it keeps beside the vault, or that it keeps none
 * (null). Unless the owner pressed one on this page, that language is shown, and
 * the browser's own where none is kept.
 */
export function keptLanguage(code) {
  if (pressed !== null) return;
  const wanted = LANGUAGES.includes(code) ? code : browserLanguage();
  if (wanted !== shown) setLanguage(wanted);
}

function plural(code, n) {
  if (!plurals[code]) plurals[code] = new Intl.PluralRules(code);
  return plurals[code].select(n);
}

/** One value in its place: a number as the language writes it, a list joined by the language's rule, anything else as it is. */
function written(value) {
  if (value === null || value === undefined) return "–";
  if (typeof value === "number") return num(value);
  if (Array.isArray(value)) return list(value);
  return String(value);
}

/** The two texts the owner sends on to an AI app: they are copied and typed exactly as the table has them. */
const SENT_ON = ["ask.sample.question", "connect.ask.start"];

/** Polish texts that were bound already, by the text of the table. */
const bound = new Map();

/**
 * A Polish text of the table as Polish is set: a word of one letter ("i", "w", "z",
 * "a", "o", "u") stays on the line of the word after it, and a number on the line of
 * its noun. The space between them becomes a no-break space (U+00A0). Only the text
 * of the table is changed, never a value that is put into it. Twice, because a word
 * that follows one that was just bound is not seen the first time.
 */
function bind(text) {
  let made = bound.get(text);
  if (made === undefined) {
    const one = /(^|[\s(„])([aiouwz]) /gi;
    made = text.replace(one, "$1$2\u00a0").replace(one, "$1$2\u00a0").replace(/\{n\} /g, "{n}\u00a0");
    bound.set(text, made);
  }
  return made;
}

/**
 * The text of a key in the language shown. `values` fills its places: `{app}` takes
 * `values.app`. `{product}` needs no value, and neither does `{entry}`, the name the
 * product's entry has in the settings file of an AI app, where the caller has none
 * from a look at such a file. A key with plural forms takes the number as `n`. A key
 * the Polish table lacks is shown in English, and one that English lacks as the key
 * itself between ⟦ ⟧; the tests see to it that neither happens.
 */
export function t(key, values = {}) {
  const own = Object.prototype.hasOwnProperty;
  let found = own.call(TABLES[shown], key) ? TABLES[shown][key] : own.call(ENGLISH, key) ? ENGLISH[key] : null;
  if (found !== null && typeof found === "object") found = found[plural(shown, values.n)] || found.other;
  if (typeof found !== "string") return `⟦${key}⟧`;
  if (shown === "pl" && !SENT_ON.includes(key)) found = bind(found);
  // A place is "{" and lower-case letters and "}", so a text may hold "${" untouched.
  return found.replace(/(\$?)\{([a-z]+)\}/g, (whole, dollar, name) => {
    if (dollar) return whole;
    if (name === "product") return PRODUCT;
    if (own.call(values, name)) return written(values[name]);
    return name === "entry" && ENTRY ? ENTRY : whole;
  });
}

/**
 * The text of a key in two parts, for one value that the page draws as an element of
 * its own: what stands before the place `{name}`, and what stands after it.
 */
export function around(key, name, values = {}) {
  const mark = "\u0000";
  const parts = t(key, Object.assign({}, values, { [name]: mark })).split(mark);
  return [parts[0], parts.slice(1).join("")];
}

// Until the app has said what it keeps, the browser's own language decides.
setLanguage(browserLanguage());
