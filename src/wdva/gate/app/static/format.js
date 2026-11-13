// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// Numbers, sizes and times as a person reads them. Times arrive as UTC and are
// shown in the local time of this computer.

const numbers = new Intl.NumberFormat("en-US");
const clockFormat = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const dayFormat = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const dayYearFormat = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });

/** 6912 becomes "6,912". Anything that is not a number becomes a dash. */
export function num(value) {
  return typeof value === "number" && Number.isFinite(value) ? numbers.format(value) : "–";
}

/** "1 passage", "52 passages". */
export function count(value, one, many) {
  return `${num(value)} ${value === 1 ? one : many}`;
}

/** A size in the decimal units a Mac shows: "4.8 MB". */
export function bytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "–";
  if (value < 1000) return count(value, "byte", "bytes");
  const units = ["kB", "MB", "GB", "TB"];
  let amount = value;
  let unit = -1;
  do {
    amount /= 1000;
    unit += 1;
  } while (amount >= 1000 && unit < units.length - 1);
  return `${amount >= 100 ? Math.round(amount) : amount.toFixed(1)} ${units[unit]}`;
}

/** An upload limit, which is counted in units of 1024: "20 MB". */
export function megabytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "\u2013";
  const amount = value / 1048576;
  return `${amount >= 10 ? Math.round(amount) : Math.round(amount * 10) / 10} MB`;
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

function dayName(moment, now) {
  const offset = dayOffset(moment, now);
  if (offset === 0) return "";
  if (offset === -1) return "yesterday";
  if (offset === 1) return "tomorrow";
  return moment.getFullYear() === now.getFullYear() ? dayFormat.format(moment) : dayYearFormat.format(moment);
}

/** Local time of day, with the day when it is not today: "17:31", "yesterday, 17:31", "3 Nov, 09:02". */
export function when(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "–";
  const day = dayName(moment, now);
  const clock = clockFormat.format(moment);
  return day ? `${day}, ${clock}` : clock;
}

/** The same, always with the date written out; for places where the exact moment matters. */
export function whenFull(text) {
  const moment = parseTime(text);
  if (!moment) return "";
  return `${dayYearFormat.format(moment)}, ${clockFormat.format(moment)}`;
}

/** A past moment relative to now, for lists: "just now", "2 min ago", "3 h ago", "yesterday, 17:31". */
export function ago(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "–";
  const seconds = Math.round((now.getTime() - moment.getTime()) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3570) return `${Math.max(1, Math.round(seconds / 60))} min ago`;
  if (seconds < 6 * 3600) return `${Math.round(seconds / 3600)} h ago`;
  return when(text, now);
}

/**
 * A past moment for a narrow column: relative while it is fresh, then the time of day,
 * and for earlier days the day alone ("yesterday", "3 Nov"). The full moment belongs
 * in the element's title.
 */
export function agoShort(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "\u2013";
  const seconds = Math.round((now.getTime() - moment.getTime()) / 1000);
  if (seconds < 6 * 3600) return ago(text, now);
  return dayName(moment, now) || clockFormat.format(moment);
}

/** The day alone, for a column of dates: "Today", "Yesterday", "10 Nov", "10 Nov 2025". */
export function dayShort(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "\u2013";
  const name = dayName(moment, now);
  return name ? sentence(name) : "Today";
}

/** A future moment for a sentence: "at 01:00", "tomorrow at 01:00", "on 3 Nov at 09:02". */
export function at(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "";
  const offset = dayOffset(moment, now);
  const clock = clockFormat.format(moment);
  if (offset === 0) return `at ${clock}`;
  if (offset === 1 || offset === -1) return `${dayName(moment, now)} at ${clock}`;
  return `on ${dayName(moment, now)} at ${clock}`;
}

/** How long until a moment, in words: "in 8 min", "in 2 h", or "" when it has passed. */
export function untilShort(text, now = new Date()) {
  const moment = parseTime(text);
  if (!moment) return "";
  const seconds = Math.round((moment.getTime() - now.getTime()) / 1000);
  if (seconds <= 0) return "";
  if (seconds < 90) return "in 1 min";
  if (seconds < 3600) return `in ${Math.round(seconds / 60)} min`;
  return `in ${Math.round(seconds / 3600)} h`;
}

/** "a", "a and b", "a, b and c". */
export function list(items) {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** First letter in upper case, the rest untouched. */
export function sentence(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}
