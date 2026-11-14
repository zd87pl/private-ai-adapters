// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// Numbers, sizes and times as a person reads them, in the language the page shows.
// Each is made by the browser's own rules for that language (`Intl`). Times arrive
// as UTC and are shown in the local time of this computer.
//
// This module imports nothing; texts.js tells it the language (`useLanguage`). Two of
// its functions hold English words and are for the pages that stay English: `count`
// and `at`.

/** The browser's own first language where that is a kind of English, else British English. */
function english() {
  try {
    const listed = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language];
    const first = String(listed[0] || "");
    if (/^en(-|$)/i.test(first) && Intl.NumberFormat.supportedLocalesOf([first]).length) return first;
  } catch (_error) {
    // A browser that names no language, or names one this engine does not know.
  }
  return "en-GB";
}

/** The language everything here is made for: "pl", or a kind of English. */
let tag = english();
/** What stands for a moment less than a minute ago. texts.js hands over the text of its table. */
let justNow = "just now";
/** What was made for this language so far, by name. */
let made = {};

/**
 * Make everything for the language `code` from now on ("en" or "pl"). `words.justNow`
 * is the text for a moment less than a minute ago, which no rule of the browser gives
 * the way a person says it.
 */
export function useLanguage(code, words = {}) {
  tag = code === "pl" ? "pl" : english();
  if (typeof words.justNow === "string" && words.justNow) justNow = words.justNow;
  made = {};
}

function once(name, make) {
  if (!made[name]) made[name] = make();
  return made[name];
}

const clockFormat = () => once("clock", () => new Intl.DateTimeFormat(tag, { hour: "numeric", minute: "2-digit" }));
const dayFormat = () => once("day", () => new Intl.DateTimeFormat(tag, { day: "numeric", month: "short" }));
const dayYearFormat = () => once("day-year", () => new Intl.DateTimeFormat(tag, { day: "numeric", month: "short", year: "numeric" }));
const relative = (numeric, style) => once(`relative-${numeric}-${style}`, () => new Intl.RelativeTimeFormat(tag, { numeric, style }));

/** 6912 becomes "6,912", and "6912" in Polish, which groups from five digits on. Anything that is not a number becomes a dash. */
export function num(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "–";
  return once("number", () => new Intl.NumberFormat(tag)).format(value);
}

/** "1 passage", "52 passages". English words: for the pages that stay English. */
export function count(value, one, many) {
  return `${num(value)} ${value === 1 ? one : many}`;
}

/** A share between 0 and 1 as a whole percentage: "50%". */
export function percent(share) {
  return once("percent", () => new Intl.NumberFormat(tag, { style: "percent", maximumFractionDigits: 0 })).format(share);
}

/** A number with a unit of the browser's list: "4.8 MB", "512 bytes". */
function withUnit(value, unit, digits, long = false) {
  const options = { style: "unit", unit, unitDisplay: long ? "long" : "short", minimumFractionDigits: digits[0], maximumFractionDigits: digits[1] };
  return once(`unit-${unit}-${digits.join("-")}`, () => new Intl.NumberFormat(tag, options)).format(value);
}

/** A size in the decimal units a Mac shows: "4.8 MB". */
export function bytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "–";
  if (value < 1000) return withUnit(value, "byte", [0, 0], true);
  const units = ["kilobyte", "megabyte", "gigabyte", "terabyte"];
  let amount = value;
  let unit = -1;
  do {
    amount /= 1000;
    unit += 1;
  } while (amount >= 1000 && unit < units.length - 1);
  return withUnit(amount, units[unit], amount >= 100 ? [0, 0] : [1, 1]);
}

/** An upload limit, which is counted in units of 1024: "20 MB". */
export function megabytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "–";
  const amount = value / 1048576;
  return withUnit(amount >= 10 ? Math.round(amount) : amount, "megabyte", [0, 1]);
}

export function parseTime(text) {
  if (!text) return null;
  const moment = new Date(text);
  return Number.isNaN(moment.getTime()) ? null : moment;
}

function startOfDay(moment) {
  return new Date(moment.getFullYear(), moment.getMonth(), moment.getDate()).getTime();
}

/** How many calendar days `moment` lies after today (negative: before). */
function dayOffset(moment, now) {
  return Math.round((startOfDay(moment) - startOfDay(now)) / 86400000);
}

/** The day of a moment that is not today: "yesterday", "tomorrow", "3 Nov", "3 Nov 2025". */
function dayName(moment, now) {
  const offset = dayOffset(moment, now);
  if (offset === 0) return "";
  if (offset === -1 || offset === 1) return relative("auto", "long").format(offset, "day");
  return moment.getFullYear() === now.getFullYear() ? dayFormat().format(moment) : dayYearFormat().format(moment);
}

/** Local time of day, with the day when it is not today: "17:31", "yesterday, 17:31", "3 Nov, 09:02". For a list, not inside a sentence. */
export function when(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "–";
  const day = dayName(moment, now);
  const time = clockFormat().format(moment);
  return day ? `${day}, ${time}` : time;
}

/** The same, always with the date written out; for places where the exact moment matters. */
export function whenFull(text) {
  const moment = parseTime(text);
  if (!moment) return "";
  return `${dayYearFormat().format(moment)}, ${clockFormat().format(moment)}`;
}

/**
 * A moment as it stands inside a sentence of the text table ("until {time}"): the
 * time of day, and the date with it for a moment of another day that does not lie
 * within the next 24 hours. "Until 01:55" late in the evening can only mean the
 * coming night, and the line that says so stays one line. In Polish that date is
 * written in numbers ("16.11, 18:00"): the name of a month would need an ending there.
 */
export function clock(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "–";
  const ahead = moment.getTime() - now.getTime();
  if (dayOffset(moment, now) === 0 || (ahead >= 0 && ahead < 86400000)) return clockFormat().format(moment);
  const month = tag === "pl" ? "numeric" : "short";
  return once("dated", () => new Intl.DateTimeFormat(tag, { day: "numeric", month, hour: "numeric", minute: "2-digit" })).format(moment);
}

/**
 * How long ago, by three rules. Under a minute: the words for "just now". Minutes and
 * hours: always with their number, and never 0. From a day on: in days, where the
 * language has a word that word ("yesterday"), then in months and years.
 */
function since(moment, now, style) {
  const seconds = Math.round((now.getTime() - moment.getTime()) / 1000);
  if (seconds < 60) return justNow;
  if (seconds < 3600) return relative("always", style).format(-Math.floor(seconds / 60), "minute");
  if (seconds < 86400) return relative("always", style).format(-Math.floor(seconds / 3600), "hour");
  const days = Math.max(1, -dayOffset(moment, now));
  if (days < 60) return relative("auto", style).format(-days, "day");
  if (days < 365) return relative("auto", style).format(-Math.floor(days / 30), "month");
  return relative("auto", style).format(-Math.floor(days / 365), "year");
}

/** A past moment relative to now, for a sentence: "just now", "5 minutes ago", "3 hours ago", "yesterday", "5 days ago". */
export function ago(text, now = new Date()) {
  const moment = parseTime(text);
  return moment ? since(moment, now, "long") : "–";
}

/** The same in its short form, for a narrow column: "5 min ago", "yesterday". The full moment belongs in the element's title. */
export function agoShort(text, now = new Date()) {
  const moment = parseTime(text);
  return moment ? since(moment, now, "short") : "–";
}

/** The day alone, for a column of dates: "Today", "Yesterday", "10 Nov", "10 Nov 2025". */
export function dayShort(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "–";
  return sentence(dayName(moment, now) || relative("auto", "long").format(0, "day"));
}

/** The day of a moment for a row ("added {date}"): "12 Nov", and with its year when that is not this one. */
export function day(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "–";
  return moment.getFullYear() === now.getFullYear() ? dayFormat().format(moment) : dayYearFormat().format(moment);
}

/**
 * A future moment for an English sentence: "at 01:00", "tomorrow at 01:00", "on 3 Nov
 * at 09:02". English words, for the pages that stay English: the name of the day is
 * English whatever language is shown.
 */
export function at(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "";
  const offset = dayOffset(moment, now);
  const time = clockFormat().format(moment);
  if (offset === 0) return `at ${time}`;
  if (offset === 1) return `tomorrow at ${time}`;
  if (offset === -1) return `yesterday at ${time}`;
  const day = { day: "numeric", month: "short" };
  if (moment.getFullYear() !== now.getFullYear()) day.year = "numeric";
  return `on ${new Intl.DateTimeFormat(english(), day).format(moment)} at ${time}`;
}

/** How long until a moment, in its short form: "in 8 min", "in 2 hr", or "" when it has passed. */
export function untilShort(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "";
  const seconds = Math.round((moment.getTime() - now.getTime()) / 1000);
  if (seconds <= 0) return "";
  if (seconds < 3600) return relative("always", "short").format(Math.max(1, Math.round(seconds / 60)), "minute");
  return relative("always", "short").format(Math.round(seconds / 3600), "hour");
}

/** "a", "a and b", "a, b and c"; in Polish "a, b i c". */
export function list(items) {
  return once("list", () => new Intl.ListFormat(tag, { type: "conjunction" })).format(Array.from(items, String));
}

/** First letter in upper case, the rest untouched. */
export function sentence(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}
