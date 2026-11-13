// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The only module that talks to WDVA Gate. Every address it uses is written out
// below, one function per endpoint.

/** An answer that is an error: the HTTP status, a short code and one sentence for a person. */
export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const hooks = { unauthorized: null, reauth: null };

/**
 * `unauthorized()` is called when the session has ended. `reauth()` is called when
 * an action needs the passphrase again; it resolves to true once the passphrase
 * was accepted and to false when the owner gave up.
 */
export function configure({ unauthorized, reauth }) {
  hooks.unauthorized = unauthorized;
  hooks.reauth = reauth;
}

const FALLBACK = {
  0: "WDVA Gate is not answering. Is it still running in its terminal?",
  400: "WDVA Gate could not use that.",
  403: "WDVA Gate refused that request.",
  404: "That is no longer there.",
  409: "That does not fit the current state of the vault.",
  413: "That is larger than WDVA Gate accepts.",
  503: "The vault cannot be reached right now.",
};

async function send(method, path, options) {
  let url = path;
  if (options.query) {
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(options.query)) {
      if (value !== null && value !== undefined && value !== "") query.set(name, String(value));
    }
    const text = query.toString();
    if (text) url += `?${text}`;
  }
  const headers = { Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin", cache: "no-store" };
  if (method !== "GET") headers["X-WDVA-Gate"] = "1";
  if (options.raw !== undefined) {
    headers["Content-Type"] = "application/octet-stream";
    init.body = options.raw;
  } else if (method === "POST") {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body || {});
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (_error) {
    throw new ApiError(0, "offline", FALLBACK[0]);
  }
  let data = null;
  try {
    data = await response.json();
  } catch (_error) {
    data = null;
  }
  if (response.ok) return data === null ? {} : data;
  const error = data && typeof data.error === "object" && data.error ? data.error : {};
  const message =
    typeof error.message === "string" && error.message
      ? error.message
      : FALLBACK[response.status] || "Something went wrong in WDVA Gate.";
  throw new ApiError(response.status, typeof error.code === "string" ? error.code : "error", message);
}

async function call(method, path, options = {}) {
  try {
    return await send(method, path, options);
  } catch (error) {
    if (error.status === 401 && !options.session) {
      if (hooks.unauthorized) hooks.unauthorized();
      throw error;
    }
    if (error.code === "reauth_required" && hooks.reauth && !options.plain) {
      const accepted = await hooks.reauth();
      if (!accepted) throw new ApiError(403, "cancelled", "Nothing was changed.");
      return send(method, path, options);
    }
    throw error;
  }
}

const part = encodeURIComponent;

export const api = {
  session: () => call("GET", "/api/session", { session: true }),
  pair: (code) => call("POST", "/api/session/pair", { body: { code }, session: true }),

  state: () => call("GET", "/api/state"),
  createStore: (passphrase) => call("POST", "/api/store/create", { body: { passphrase } }),
  unlock: (passphrase) => call("POST", "/api/unlock", { body: { passphrase } }),
  lock: () => call("POST", "/api/lock"),
  reauth: (passphrase) => call("POST", "/api/reauth", { body: { passphrase }, plain: true }),
  setServing: (body) => call("POST", "/api/serving", { body }),
  verify: () => call("POST", "/api/verify"),

  activity: (query) => call("GET", "/api/activity", { query }),
  activityDetail: (seq) => call("GET", `/api/activity/${part(seq)}`),

  approvals: () => call("GET", "/api/approvals"),
  release: (app, ref, bodySha256) =>
    call("POST", "/api/approvals/release", { body: { app, ref, body_sha256: bodySha256 } }),
  dismiss: (app, ref) => call("POST", "/api/approvals/dismiss", { body: { app, ref } }),

  documents: () => call("GET", "/api/documents"),
  addDocument: (collection, name, file) =>
    call("POST", "/api/documents", { query: { collection, name }, raw: file }),
  removeDocument: (id) => call("DELETE", `/api/documents/${part(id)}`),
  setCollection: (body) => call("POST", "/api/collections", { body }),
  removeCollection: (name) => call("DELETE", `/api/collections/${part(name)}`),

  apps: () => call("GET", "/api/apps"),
  addApp: (body) => call("POST", "/api/apps", { body }),
  removeApp: (name) => call("DELETE", `/api/apps/${part(name)}`),
  setGrant: (app, collection, mode) => call("POST", "/api/grants", { body: { app, collection, mode } }),
  setAppLimits: (name, limits) => call("POST", `/api/apps/${part(name)}/limits`, { body: limits }),
  connect: (name, host) => call("GET", `/api/apps/${part(name)}/connect`, { query: { host } }),

  settings: () => call("GET", "/api/settings"),
  setSettings: (body) => call("POST", "/api/settings", { body }),
  addReplacedString: (text) => call("POST", "/api/replaced-strings", { body: { text } }),
};
