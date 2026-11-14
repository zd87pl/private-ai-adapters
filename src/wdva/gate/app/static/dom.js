// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// Building and updating the page.
//
// Every screen is described from scratch as a tree of nodes and then merged into
// the nodes that are already on the page. Nodes that stay keep their identity, so
// scroll positions, focus, text selections and running transitions survive an
// update. Text is only ever added as text nodes: nothing that comes from a
// document, an app or an agent is parsed as markup.

const SVG_NS = "http://www.w3.org/2000/svg";

/** Event handlers per node. One listener per event type on the document looks them up. */
const handlers = new WeakMap();

const DELEGATED = [
  "click",
  "input",
  "change",
  "submit",
  "keydown",
  "dragenter",
  "dragover",
  "dragleave",
  "drop",
];

function dispatch(event) {
  let node = event.target;
  while (node && node !== document) {
    const own = handlers.get(node);
    if (own && own[event.type]) {
      own[event.type](event, node);
      if (event.cancelBubble) return;
    }
    node = node.parentNode;
  }
}

for (const type of DELEGATED) document.addEventListener(type, dispatch);

function listen(node, type, handler) {
  let own = handlers.get(node);
  if (!own) {
    own = {};
    handlers.set(node, own);
  }
  own[type] = handler;
}

function appendChildren(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) appendChildren(node, child);
    else if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  }
}

/**
 * Create an element.
 *
 * `props` may hold attributes, `class` (text or a list with empty entries), `key`
 * (identity among siblings), `on<event>` handlers, `value` and `checked` for form
 * controls, and `fill` (0 to 1) for a meter. Children are nodes, text, or lists.
 */
export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  const given = props || {};
  if (given.type) node.setAttribute("type", given.type);
  for (const [name, value] of Object.entries(given)) {
    if (value === null || value === undefined || value === false || name === "type") continue;
    if (name === "class") {
      const text = Array.isArray(value) ? value.filter(Boolean).join(" ") : value;
      if (text) node.className = text;
    } else if (name === "key") {
      node.dataset.key = String(value);
    } else if (name.startsWith("on") && typeof value === "function") {
      listen(node, name.slice(2), value);
    } else if (name === "value") {
      if (given.type === "radio" || given.type === "checkbox") {
        node.setAttribute("value", String(value));
      } else if (tag !== "select") {
        node.value = String(value);
      }
    } else if (name === "checked") {
      node.checked = Boolean(value);
    } else if (name === "fill") {
      const share = Math.max(0, Math.min(1, Number(value) || 0));
      node.dataset.fill = (share * 100).toFixed(1);
    } else {
      node.setAttribute(name, value === true ? "" : String(value));
    }
  }
  appendChildren(node, children);
  if (tag === "select" && given.value !== null && given.value !== undefined) {
    node.value = String(given.value);
  }
  return node;
}

// Icons: one set, drawn as strokes on a 20 by 20 grid. "p" are paths, "c" circles (cx, cy, r),
// "d" filled dots.
const ICONS = {
  overview: { p: ["M3.5 3.5h5v5h-5z", "M11.5 3.5h5v5h-5z", "M3.5 11.5h5v5h-5z", "M11.5 11.5h5v5h-5z"] },
  activity: { p: ["M2.5 10.5H6l2-6 4 11 2-5h3.5"] },
  approvals: {
    p: [
      "M3 11.5l2.3-6.3a1 1 0 0 1 .9-.7h7.6a1 1 0 0 1 .9.7L17 11.5",
      "M3 11.5V15a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5h-4l-1 2H8l-1-2z",
    ],
  },
  documents: {
    p: [
      "M5.5 2.5h6L15.5 6.5V16a1.5 1.5 0 0 1-1.5 1.5H5.5A1.5 1.5 0 0 1 4 16V4a1.5 1.5 0 0 1 1.5-1.5z",
      "M11.5 2.5v4h4",
      "M7 10.5h6",
      "M7 13.5h6",
    ],
  },
  apps: {
    p: [
      "M4 3.5h12A1.5 1.5 0 0 1 17.5 5v7a1.5 1.5 0 0 1-1.5 1.5H4A1.5 1.5 0 0 1 2.5 12V5A1.5 1.5 0 0 1 4 3.5z",
      "M7 17h6",
      "M10 13.5V17",
    ],
  },
  vault: {
    p: [
      "M4.5 3.5h11A1.5 1.5 0 0 1 17 5v9.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 14.5V5a1.5 1.5 0 0 1 1.5-1.5z",
      "M10 7.3V6.2",
      "M5.5 16v1.5",
      "M14.5 16v1.5",
    ],
    c: [[10, 9.8, 2.5]],
  },
  lock: {
    p: ["M5.5 9h9a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1z", "M7 9V6.5a3 3 0 0 1 6 0V9"],
  },
  unlock: {
    p: ["M5.5 9h9a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1z", "M7 9V6.5a3 3 0 0 1 5.7-1.3"],
  },
  check: { p: ["M4.5 10.5l3.5 3.5 7.5-8"] },
  x: { p: ["M5 5l10 10", "M15 5L5 15"] },
  right: { p: ["M7.5 4.5L13 10l-5.5 5.5"] },
  down: { p: ["M4.5 7.5L10 13l5.5-5.5"] },
  out: { p: ["M6.5 13.5L14 6", "M8 6h6v6"] },
  mac: { p: ["M4.5 4.5h11a1 1 0 0 1 1 1V13h-13V5.5a1 1 0 0 1 1-1z", "M2 15.5h16"] },
  upload: { p: ["M10 13V3.5", "M6 7l4-3.5L14 7", "M3.5 13v2.5a1 1 0 0 0 1 1h11a1 1 0 0 0 1-1V13"] },
  copy: {
    p: [
      "M8 7.5h7.5a1 1 0 0 1 1 1V16a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V8.5a1 1 0 0 1 1-1z",
      "M4.5 12.5h-1a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1H11a1 1 0 0 1 1 1v1",
    ],
  },
  plus: { p: ["M10 4.5v11", "M4.5 10h11"] },
  trash: {
    p: [
      "M4 5.5h12",
      "M8 5.5V4a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1.5",
      "M5.5 5.5l.7 10.1a1 1 0 0 0 1 .9h5.6a1 1 0 0 0 1-.9l.7-10.1",
    ],
  },
  warning: { p: ["M10 3.5l7 12.5H3z", "M10 8.5V12"], d: [[10, 14, 0.9]] },
  info: { p: ["M10 9.2V14"], c: [[10, 10, 7]], d: [[10, 6.6, 0.9]] },
  search: { p: ["M13 13l3.5 3.5"], c: [[9, 9, 5]] },
  read: { p: ["M2 10s3-5.5 8-5.5S18 10 18 10s-3 5.5-8 5.5S2 10 2 10z"], c: [[10, 10, 2.2]] },
  refused: { p: ["M5.1 5.1l9.8 9.8"], c: [[10, 10, 7]] },
  clock: { p: ["M10 6v4.2l2.8 1.8"], c: [[10, 10, 7]] },
  key: { p: ["M8.8 11.2L16 4", "M13 7l2 2", "M11 9l1.5 1.5"], c: [[6.5, 13.5, 3]] },
  pause: { p: ["M7 4.5v11", "M13 4.5v11"] },
  link: {
    p: [
      "M8.2 11.8l3.6-3.6",
      "M10.5 5.8l1-1a3 3 0 0 1 4.2 4.2l-1 1",
      "M9.5 14.2l-1 1a3 3 0 0 1-4.2-4.2l1-1",
    ],
  },
  refresh: { p: ["M16 10a6 6 0 1 1-1.8-4.3", "M16 3.5v3.3h-3.3"] },
  you: { p: ["M4 16.5c.8-3 3.2-4.5 6-4.5s5.2 1.5 6 4.5"], c: [[10, 7, 3]] },
  sliders: { p: ["M3.5 6h6", "M13.5 6h3", "M3.5 14h3", "M10.5 14h6"], c: [[11.5, 6, 2], [8.5, 14, 2]] },
  more: { d: [[4.5, 10, 1.3], [10, 10, 1.3], [15.5, 10, 1.3]] },
  view: {
    p: [
      "M5 3.5h10A1.5 1.5 0 0 1 16.5 5v10a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 15V5A1.5 1.5 0 0 1 5 3.5z",
      "M6.5 7.5h7",
      "M6.5 10.5h7",
      "M6.5 13.5h4",
    ],
  },
  dot: { d: [[10, 10, 3]] },
};

/** One icon of the set, as an SVG element that takes the colour of the text around it. */
export function icon(name, extraClass) {
  const shape = ICONS[name] || ICONS.dot;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("class", extraClass ? `icon ${extraClass}` : "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const d of shape.p || []) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.appendChild(path);
  }
  for (const [cx, cy, r] of shape.c || []) {
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("cx", cx);
    circle.setAttribute("cy", cy);
    circle.setAttribute("r", r);
    svg.appendChild(circle);
  }
  for (const [cx, cy, r] of shape.d || []) {
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", cx);
    dot.setAttribute("cy", cy);
    dot.setAttribute("r", r);
    dot.setAttribute("class", "icon-dot");
    svg.appendChild(dot);
  }
  return svg;
}

function keyOf(node) {
  return node.nodeType === 1 ? node.getAttribute("data-key") : null;
}

/** A field may change between hidden and shown typing and stay the same field. */
const TYPED = new Set(["text", "password"]);

function sameKind(a, b) {
  if (a.nodeType !== b.nodeType) return false;
  if (a.nodeType !== 1) return true;
  if (a.nodeName !== b.nodeName || keyOf(a) !== keyOf(b)) return false;
  return a.nodeName !== "INPUT" || a.type === b.type || (TYPED.has(a.type) && TYPED.has(b.type));
}

function syncAttributes(current, next) {
  for (const { name } of Array.from(current.attributes)) {
    if (name.startsWith("data-m-") || name === "style" || name === "open") continue;
    if (!next.hasAttribute(name)) current.removeAttribute(name);
  }
  for (const { name, value } of Array.from(next.attributes)) {
    if (name === "style") continue;
    if (current.getAttribute(name) !== value) current.setAttribute(name, value);
  }
}

function syncNode(current, next) {
  if (current.nodeType !== 1) {
    if (current.data !== next.data) current.data = next.data;
    return;
  }
  syncAttributes(current, next);
  const own = handlers.get(next);
  if (own) handlers.set(current, own);
  else handlers.delete(current);
  syncChildren(current, next);
  const name = current.nodeName;
  if (name === "INPUT" || name === "TEXTAREA" || name === "SELECT") {
    if (current.type === "checkbox" || current.type === "radio") {
      if (current.checked !== next.checked) current.checked = next.checked;
    } else if (current.type !== "file" && current.value !== next.value) {
      current.value = next.value;
    }
  }
}

/** False while a root is filled for the first time: then nothing counts as newly arrived. */
let marking = true;

function syncChildren(parent, source) {
  const keyed = new Map();
  for (const child of parent.childNodes) {
    const key = keyOf(child);
    if (key !== null) keyed.set(key, child);
  }
  let cursor = parent.firstChild;
  for (const next of Array.from(source.childNodes)) {
    const key = keyOf(next);
    let match = null;
    if (key !== null) {
      const candidate = keyed.get(key);
      if (candidate && sameKind(candidate, next)) {
        match = candidate;
        keyed.delete(key);
      }
    } else {
      let candidate = cursor;
      while (candidate && keyOf(candidate) !== null) candidate = candidate.nextSibling;
      if (candidate && sameKind(candidate, next)) match = candidate;
    }
    if (match) {
      if (match === cursor) cursor = cursor.nextSibling;
      else parent.insertBefore(match, cursor);
      syncNode(match, next);
    } else {
      if (marking && next.nodeType === 1) next.setAttribute("data-m-new", "");
      parent.insertBefore(next, cursor);
    }
  }
  while (cursor) {
    const following = cursor.nextSibling;
    parent.removeChild(cursor);
    cursor = following;
  }
}

function applyFills(root) {
  const pending = [];
  for (const node of root.querySelectorAll("[data-fill]")) {
    const wanted = `${node.dataset.fill}%`;
    if (node.style.getPropertyValue("--fill") !== wanted) pending.push([node, wanted]);
  }
  if (!pending.length) return;
  // One frame later, so that a meter that has just appeared grows from zero.
  requestAnimationFrame(() => {
    for (const [node, wanted] of pending) node.style.setProperty("--fill", wanted);
  });
}

/** Make the children of `root` equal to `nodes`, keeping the nodes that are already there. */
export function render(root, ...nodes) {
  const source = document.createElement("div");
  appendChildren(source, nodes);
  marking = root.hasAttribute("data-m-ready");
  try {
    syncChildren(root, source);
  } finally {
    marking = true;
  }
  root.setAttribute("data-m-ready", "");
  applyFills(root);
}

/** Text that only screen readers get. */
export function srOnly(text) {
  return el("span", { class: "sr-only" }, text);
}
