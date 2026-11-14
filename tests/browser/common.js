// What the three walk-throughs of the app share (journey_demo.js, journey_real.js,
// journey_basic.js).
//
// Nothing in this file knows a step. It reads the options from the environment,
// starts and stops the app through launch_app.py, opens the browser engine that
// was asked for, listens for what the page does wrong, and prints the result.
//
// Rules the walk-throughs keep, so that they run in Chromium, WebKit and Firefox
// alike and on a slow machine:
//   - no launch options, no protocol sessions, no selector one engine alone has;
//   - a wait is a wait for a condition with a timeout, never a guess at a duration;
//   - the app's state is read over its own API with the session of the page, not by
//     running script inside the page. The session is a token that the app tells the
//     page and the page sends in a header (never a cookie: a browser sends cookies
//     to every port of 127.0.0.1). `watch` notes the token as the page is told it,
//     and `apiGet` sends it the way the page does;
//   - what a button copied is read back from the clipboard only where the engine lets a
//     test do that (Chromium). Everywhere the text is compared with what the page shows
//     beside the button, which is what the button copies (`pressCopy`).

'use strict';

const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const ENGINES = ['chromium', 'webkit', 'firefox'];

/** How long a condition may take before a step fails. A met condition costs no time. */
const WAIT = 20000;
/** How long the app may take to print its start-up lines (a cold Python, the demo's vault). */
const START_WAIT = 120000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const firstLine = (error) => String(error && error.message ? error.message : error).split('\n')[0];

function option(name, fallback) {
  const value = process.env[name];
  return value === undefined || value === '' ? fallback : value;
}

/**
 * The options of a run, from the environment. `defaults.ownerSeconds` differs per
 * walk-through. A value that cannot be used ends the process with one line and status 1.
 */
function readOptions(defaults) {
  const refuse = (message) => {
    console.log(`FAIL  ${message}`);
    process.exit(1);
  };
  const browser = option('GATE_BROWSER', 'chromium');
  if (!ENGINES.includes(browser)) refuse(`GATE_BROWSER is one of ${ENGINES.join(', ')}, not "${browser}"`);
  const scale = Number(option('GATE_SCALE', '1'));
  if (!(scale > 0 && scale <= 4)) refuse(`GATE_SCALE is a number above 0, 4 at most, not "${process.env.GATE_SCALE}"`);
  const port = Number(option('GATE_PORT', '0'));
  if (!Number.isInteger(port) || port < 0 || port > 65535) refuse(`GATE_PORT is a port number, not "${process.env.GATE_PORT}"`);
  const ownerSeconds = Number(option('GATE_OWNER_SECONDS', String(defaults.ownerSeconds)));
  if (!(ownerSeconds >= 5)) refuse(`GATE_OWNER_SECONDS is a number of seconds, 5 at least, not "${process.env.GATE_OWNER_SECONDS}"`);
  return {
    python: option('GATE_PYTHON', 'python3'),
    browser,
    shots: path.resolve(option('GATE_SHOTS', path.join(HERE, 'shots'))),
    scale,
    port, // 0: the app picks a free port and says which
    ownerSeconds,
  };
}

// -- the result -----------------------------------------------------------------------

const failures = [];
const problems = [];
const said = new Set();
const expected = []; // HTTP errors a step announced: {status, path, left, optional}
const running = new Set(); // apps this process started and has not seen end
const leftovers = new Set(); // directories to remove when the process ends

/** True between a deliberate stop of the app and its next start: the page's requests fail then, as they must. */
let appDown = false;
/** True while the walk-through itself closes a page or ends: nothing a page does then is a finding. */
let hushed = false;
let shotsDirectory = null;
let currentPage = null;
/** Above zero while a screenshot is being taken. */
let shooting = 0;
/** Above zero while a walk-through loads a page again. */
let reloading = 0;

function check(condition, message) {
  if (!condition) throw new Error(message);
}

/** A line that is worth reading and fails nothing. Said once. */
function note(text) {
  if (said.has(text)) return;
  said.add(text);
  console.log(`note  ${text}`);
}

function problem(text) {
  if (!problems.includes(text)) problems.push(text);
}

/**
 * Announce an HTTP error that the next actions are meant to cause. `times` answers
 * with that status on that path (a string, or a pattern for several paths) are then no finding. With `optional` it is no
 * finding either when they do not come.
 */
function expectHttp(status, pathname, { times = 1, optional = false } = {}) {
  const entry = { status, path: pathname, left: times, optional };
  expected.push(entry);
  return entry;
}

/**
 * Announce what an app that was started again answers to a page that is still open. The page has
 * the session of the run before: whatever it had already asked, and whatever it asks before it
 * has noticed, is refused with 401, its state as much as its lists. Withdraw the announcement
 * once the page has paired anew; from then on a 401 is a finding.
 */
function expectOldSessionRefused() {
  return expectHttp(401, /^\/api\//, { times: 50, optional: true });
}

/** Take an announcement back, for an error that turned out not to be due. */
function withdraw(entry) {
  const at = expected.indexOf(entry);
  if (at >= 0) expected.splice(at, 1);
}

/**
 * Call `probe` until it returns something true. A probe may throw (`check` does):
 * the message of its last throw is what the step fails with when time runs out.
 */
async function eventually(probe, timeout = WAIT, what = 'what was waited for did not happen') {
  const until = Date.now() + timeout;
  let last = '';
  for (;;) {
    try {
      const value = await probe();
      if (value) return value;
      last = '';
    } catch (error) {
      last = firstLine(error);
    }
    if (Date.now() >= until) throw new Error(`${last || what} (waited ${Math.round(timeout / 1000)} s)`);
    await sleep(150);
  }
}

/**
 * A screenshot into the directory of this run. Running animations are put at their end first.
 * With `whole` the picture holds the whole page and not only what the window shows of it:
 * the window is made as tall as the page for the moment of the picture, so that a bar that
 * sticks to an edge of the window stands where the page begins or ends, as it would in a
 * window of that height. Afterwards the window has its size again and the page its place.
 *
 * In WebKit Playwright adds a style element to the page for the length of each
 * screenshot. The app's content security policy allows no inline style, so the
 * engine may refuse that element and say so; `watch` does not count that against
 * the app. The page is asked one more thing afterwards so that every such report
 * has arrived before the picture counts as taken.
 */
async function shot(page, name, { whole = false } = {}) {
  if (!shotsDirectory) return;
  shooting += 1;
  const size = page.viewportSize();
  let scrolled = null;
  try {
    if (whole && size) {
      const measured = await page.evaluate(() => [document.documentElement.scrollHeight, window.scrollY]);
      if (measured[0] > size.height) {
        scrolled = measured[1];
        await page.setViewportSize({ width: size.width, height: measured[0] });
      }
    }
    await page.screenshot({ path: path.join(shotsDirectory, `${name}.png`), animations: 'disabled' });
    await page.title();
  } finally {
    if (scrolled !== null) {
      await page.setViewportSize(size).catch(() => {});
      await page.evaluate((y) => window.scrollTo(0, y), scrolled).catch(() => {});
    }
    shooting -= 1;
  }
}

/** Whether a console message reports an inline style that the content security policy refused. */
function refusedInlineStyle(text) {
  return /^CSP-VIOLATION style-src\S* inline$/.test(text) || (/content security policy/i.test(text) && /style/i.test(text));
}

let stepNumber = 0;
let shotPrefix = '';

/** One step: a line that begins with "ok" or "FAIL". A failed step leaves a picture of the page. */
async function step(name, body) {
  stepNumber += 1;
  const number = stepNumber;
  const began = Date.now();
  const took = () => `[${((Date.now() - began) / 1000).toFixed(1)} s]`;
  try {
    await body();
    console.log(`ok    ${name}  ${took()}`);
  } catch (error) {
    failures.push(`${name}: ${firstLine(error)}`);
    console.log(`FAIL  ${name}: ${firstLine(error)}  ${took()}`);
    if (currentPage && !currentPage.isClosed()) {
      await shot(currentPage, `${shotPrefix}FAILED-step-${String(number).padStart(2, '0')}`).catch(() => {});
      await closeSheets(currentPage).catch(() => {});
    }
  }
}

/** After a failed step: close a sheet it left open, so that the next step meets the page and not the sheet. */
async function closeSheets(page) {
  const open = () => page.locator('dialog[open]').count();
  for (let round = 0; round < 3; round += 1) {
    const before = await open();
    if (before === 0) return;
    await page.keyboard.press('Escape');
    await eventually(async () => (await open()) < before, 2000).catch(() => {});
  }
}

// -- the app --------------------------------------------------------------------------

/**
 * Start `wdva gate app --no-browser` with `args`, through launch_app.py, which changes
 * how long owner mode lasts and gives the app a home of its own. Returns at once;
 * `started` waits for the lines.
 *
 * `home` is that home: an empty folder this run owns (`app.home`), made here unless one
 * is given. The app looks for the settings file of an AI app under it and nowhere else,
 * so a walk-through never reads the settings of a program on this computer.
 */
function startApp(options, { args = [], ownerSeconds = options.ownerSeconds, port = options.port, home = shortDirectory('gh-') } = {}) {
  const command = [path.join(HERE, 'launch_app.py'), String(ownerSeconds), 'gate', 'app', '--no-browser', '--port', String(port), ...args];
  const child = spawn(options.python, command, { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, GATE_HOME: home } });
  const app = { child, home, out: '', err: '', exit: null, lines: [], url: '', port: 0 };
  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');
  child.stdout.on('data', (text) => {
    app.out += text;
  });
  child.stderr.on('data', (text) => {
    app.err += text;
  });
  child.on('error', (error) => {
    app.err += `${options.python} could not be started: ${error.message}\n`;
    app.exit = app.exit || { code: null, signal: null };
    running.delete(app);
  });
  // "close" comes after the last output was read, so nothing printed at the end is missed.
  child.on('close', (code, signal) => {
    app.exit = { code, signal };
    running.delete(app);
  });
  running.add(app);
  return app;
}

const OPEN_LINE = /^open:\s+(http:\/\/127\.0\.0\.1:(\d+)\/)$/;

/** The complete lines the app has printed, once the address and the two lines after it are there. */
function startupLines(app) {
  const lines = app.out.split('\n').slice(0, -1);
  const open = lines.findIndex((line) => OPEN_LINE.test(line));
  return open >= 0 && lines.length >= open + 3 ? lines : null;
}

/** Wait for the start-up lines; sets `app.lines`, `app.url` and `app.port`. */
async function started(app) {
  await eventually(
    () => startupLines(app) || app.exit,
    START_WAIT,
    'the app printed no start-up lines',
  );
  const lines = startupLines(app);
  if (!lines) {
    const exit = app.exit ? ` (exit ${JSON.stringify(app.exit)})` : '';
    throw new Error(`the app did not start${exit}: ${(app.err || app.out).trim().split('\n').pop() || 'it printed nothing'}`);
  }
  const found = OPEN_LINE.exec(lines.find((line) => OPEN_LINE.test(line)));
  app.lines = lines;
  app.url = found[1];
  app.port = Number(found[2]);
  return lines;
}

/**
 * Send the app a signal and wait until it has ended. Returns {code, signal}, or null
 * when it did not end. From here on a request of the page that fails is no finding,
 * until `pageReachedApp` says that the page has had an answer again.
 */
async function stopApp(app, signal = 'SIGINT', timeout = 30000) {
  if (app.exit !== null) return app.exit;
  appDown = true;
  app.child.kill(signal);
  const until = Date.now() + timeout;
  while (app.exit === null && Date.now() < until) await sleep(50);
  return app.exit;
}

/** Say that the page has had an answer from a newly started app: failed requests count again. */
function pageReachedApp() {
  appDown = false;
}

/** One call of an agent application, made by agent_call.py. Returns {code, text, released}. */
function agentCall(options, store, client, ...rest) {
  const made = spawnSync(options.python, [path.join(HERE, 'agent_call.py'), store, client, ...rest], { encoding: 'utf8', timeout: 120000 });
  if (made.error) throw new Error(`the agent's call could not be made: ${made.error.message}`);
  if (made.status !== 0) throw new Error(`the agent's call failed: ${(made.stderr || '').trim().split('\n').pop()}`);
  return JSON.parse(made.stdout);
}

/** A fresh directory with a short path. A vault's socket lies below it, and a socket path ends at about 100 bytes. */
function shortDirectory(prefix) {
  // Not os.tmpdir(): on macOS that is a long path under /var/folders.
  const made = fs.realpathSync(fs.mkdtempSync(path.join('/tmp', prefix)));
  leftovers.add(made);
  return made;
}

// -- the browser ----------------------------------------------------------------------

/** Launch the engine named by GATE_BROWSER, with no options of its own. */
async function openBrowser(options, prefix) {
  let playwright;
  try {
    playwright = require('playwright');
  } catch (error) {
    throw new Error(`playwright is not installed (npm install --no-save playwright@1.63.0): ${firstLine(error)}`);
  }
  shotPrefix = prefix;
  shotsDirectory = options.shots;
  fs.mkdirSync(shotsDirectory, { recursive: true });
  const browser = await playwright[options.browser].launch();
  console.log(`engine ${options.browser} ${browser.version()} on ${process.platform} ${process.arch}, python "${options.python}", scale ${options.scale}`);
  return browser;
}

/**
 * Addresses a browser may ask for by itself, whatever the page says: its own search
 * for an icon. The page names its icon (favicon.svg), so a 404 here is the engine's
 * question and not a request of the page. It is said as a note.
 */
const ENGINE_ASKS = new Set(['/favicon.ico', '/apple-touch-icon.png', '/apple-touch-icon-precomposed.png']);

/** The session of each browser context: a promise of the token its page was told last, or of null. */
const sessions = new WeakMap();

/** Note the session when the app tells it to the page: in the answer to pairing, or to a demo's first question. */
function noteSession(page, response) {
  const pathname = new URL(response.url()).pathname;
  if (response.status() !== 200 || (pathname !== '/api/session' && pathname !== '/api/session/pair')) return;
  const context = page.context();
  const before = sessions.get(context) || Promise.resolve(null);
  sessions.set(
    context,
    response.json().then(
      (answer) => (answer && typeof answer.session === 'string' ? answer.session : before),
      () => before,
    ),
  );
}

/** Report what a page does wrong: console errors, page errors, failed requests, HTTP errors nobody announced. */
function watch(page) {
  page.on('response', (response) => noteSession(page, response));
  // What still arrives from a page after the walk-through closed it is the closing, not the page.
  const silent = () => hushed || page.isClosed();
  page.on('console', (message) => {
    const kind = message.type();
    if (silent() || (kind !== 'error' && kind !== 'warning')) return;
    const text = message.text();
    // The engine itself logs every HTTP error and every refused connection; those are judged below.
    if (text.startsWith('Failed to load resource')) return;
    if (shooting > 0 && refusedInlineStyle(text)) {
      note(`while a screenshot was taken the engine refused an inline style, which is the screenshot's own and not the app's: ${text}`);
      return;
    }
    if (appDown) note(`while the app was stopped the console said (${kind}): ${text}`);
    else if (kind === 'warning') note(`console warning: ${text}`);
    else problem(`console error: ${text}`);
  });
  page.on('pageerror', (error) => {
    if (!silent()) problem(`page error: ${firstLine(error)}`);
  });
  page.on('requestfailed', (request) => {
    // A page that is loaded again breaks off what it was asking at that moment: that is the reload, not the page.
    if (silent() || appDown || reloading > 0) return;
    const failure = request.failure();
    problem(`request failed: ${request.method()} ${new URL(request.url()).pathname}${failure ? ` (${failure.errorText})` : ''}`);
  });
  page.on('response', (response) => {
    if (silent() || response.status() < 400) return;
    const pathname = new URL(response.url()).pathname;
    const known = expected.find((entry) => entry.left > 0 && entry.status === response.status() && (typeof entry.path === 'string' ? entry.path === pathname : entry.path.test(pathname)));
    if (known) known.left -= 1;
    else if (response.status() === 404 && ENGINE_ASKS.has(pathname)) note(`the engine itself asked for ${pathname}, which the page names nowhere, and was told 404`);
    else problem(`HTTP ${response.status()} ${response.request().method()} ${pathname}`);
  });
}

/** A new page in a context of its own (its own session), watched. */
async function newPage(browser, options) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: options.scale });
  // A breach of the page's content security policy becomes a console error, in every engine.
  await context.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => console.error(`CSP-VIOLATION ${event.violatedDirective} ${event.blockedURI}`));
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  watch(page);
  currentPage = page;
  return page;
}

/** Load a page again, as the owner does with the button of the browser. What the page was asking at that moment breaks off, which is no finding. */
async function reload(page) {
  reloading += 1;
  try {
    await page.reload();
    // Every report about the page that was left has arrived once the new one has answered a question.
    await page.title();
  } finally {
    reloading -= 1;
  }
}

/** Close a page and its context without counting what breaks off as a finding. */
async function closePage(page) {
  hushed = true;
  await page.context().close();
  hushed = false;
  if (currentPage === page) currentPage = null;
}

/**
 * Press "Release once" (`action` "release") or "Don't release" ("dismiss") on the card
 * of one waiting passage, and wait until the page has taken the decision. The page
 * ignores a press on purpose while a card has only just appeared or moved, so the
 * press is repeated until it counts. `key` is "ap-<app>-<ref>".
 */
async function decideApproval(page, key, action) {
  const card = page.locator(`.approval[data-key="${key}"]`);
  const decided = page.locator(`.decided[data-key="${key}"]`);
  const taken = async () => (await decided.count()) > 0 || (await card.count()) === 0;
  await card.waitFor({ state: 'visible', timeout: WAIT });
  await eventually(async () => {
    if (await taken()) return true;
    await card.locator(`button[data-key="${action}"]:not(.is-waiting)`).click({ timeout: 5000 });
    return eventually(taken, 6000, 'the press on the card changed nothing');
  }, 45000);
}

/** What tells the two views apart: the Basic page has a bar at its top, Pro a sidebar. */
const FRAME = { basic: 'header.topbar[data-key="topbar"]', pro: 'aside.sidebar' };

/** Press "Basic" or "Pro" in the view control of the page that is open, and wait for that view. */
async function switchView(page, view) {
  await page.locator('label.segment', { has: page.locator(`input[name="view"][value="${view}"]`) }).click();
  await page.locator(FRAME[view]).waitFor({ state: 'visible', timeout: WAIT });
}

/** GET one address of the app's API with the session of `page`. Returns the parsed answer. */
async function apiGet(page, base, pathname) {
  const headers = { Accept: 'application/json' };
  const session = await sessions.get(page.context());
  if (session) headers['X-WDVA-Session'] = session;
  const response = await page.context().request.get(new URL(pathname, base).href, { headers, timeout: WAIT });
  if (!response.ok()) throw new Error(`GET ${pathname} answered ${response.status()}`);
  return response.json();
}

/**
 * Lock the vault the way a second window of the owner does: with the same request, under
 * the session of `page`. The page that is open learns of it at its next question, as it
 * does when the vault locks by itself.
 */
async function lockElsewhere(page, base) {
  const headers = { Accept: 'application/json', 'Content-Type': 'application/json', 'X-WDVA-Gate': '1' };
  const session = await sessions.get(page.context());
  if (session) headers['X-WDVA-Session'] = session;
  const response = await page.context().request.post(new URL('/api/lock', base).href, { headers, data: {}, timeout: WAIT });
  if (!response.ok()) throw new Error(`POST /api/lock answered ${response.status()}`);
}

/** Whether the element has the focus of its page. */
function focused(locator) {
  return locator.evaluate((node) => node === document.activeElement);
}

/**
 * Press a button that copies `text`, and see that it did. The page says "Copied" on the
 * button once the text is on the clipboard. Where the engine lets a test read the
 * clipboard (Chromium), it is read back and compared. An engine that lets no page copy
 * without a person at the keyboard makes the page say so in a red message: that is noted
 * and fails nothing, and the caller has compared `text` with what the page shows.
 */
async function pressCopy(page, options, button, text) {
  const says = async () => ((await button.textContent()) || '').trim();
  if (options.browser === 'chromium') {
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(page.url()).origin });
    await button.click();
    await eventually(async () => (await says()) === 'Copied', WAIT, 'the button does not say that the text was copied');
    const held = await page.evaluate(() => navigator.clipboard.readText());
    check(held === text, `the clipboard holds other text than the page shows: ${JSON.stringify(held.slice(0, 120))}`);
    return;
  }
  // A refusal is said in a red message. One from an earlier step may still be up: it goes by itself.
  const refusals = () => page.locator('#toasts .toast-red').count();
  await eventually(async () => (await refusals()) === 0, WAIT, 'a red message of an earlier step stayed up');
  await button.click();
  const outcome = await eventually(async () => {
    if ((await says()) === 'Copied') return 'says that the text was copied';
    if ((await refusals()) > 0) return 'says that copying is not allowed here';
    return null;
  }, 8000).catch(() => 'says nothing');
  note(`clipboard in ${options.browser}: after "Copy" the page ${outcome}; this engine gives a test no way to read the clipboard, so its content is not checked`);
}

/**
 * Count what a person does on `page`, for the count of what the first run costs:
 * presses, the addresses the page was at, the fields that were typed into, the file
 * dialogs that opened. Call it before the page is loaded.
 *
 * A press is a click the page received, whoever made it and on whatever. A click on a
 * label that the browser passes on to the label's control is one press. An address is
 * counted without what follows its "?". Nothing counts before `start()`. `aside(body)`
 * runs a part of the walk that is not on the path being counted, and says so by being
 * there: everything else between `start()` and `stop()` counts.
 */
async function pressCounter(page) {
  const counter = { presses: [], addresses: [], typed: [], dialogs: 0, on: false };
  const place = (hash) => {
    const address = String(hash || '').split('?')[0];
    if (address && !counter.addresses.includes(address)) counter.addresses.push(address);
  };
  await page.exposeBinding('__walkCount', (_source, kind, what) => {
    if (!counter.on) return;
    if (kind === 'press') counter.presses.push(String(what));
    else if (kind === 'typed' && !counter.typed.includes(String(what))) counter.typed.push(String(what));
    else if (kind === 'place') place(what);
  });
  await page.addInitScript(() => {
    const report = (kind, what) => {
      if (typeof window.__walkCount === 'function') window.__walkCount(kind, what);
    };
    const words = (node) => {
      const control = (node.closest && node.closest('button, a, label, input, select, textarea')) || node;
      const text = String(control.innerText || control.value || '').trim().replace(/\s+/g, ' ').slice(0, 60);
      return `${String(control.tagName).toLowerCase()}${control.id ? `#${control.id}` : ''} "${text}"`;
    };
    // The control a label has just been clicked for: the browser clicks it next, in the same turn.
    let passedOn = null;
    document.addEventListener(
      'click',
      (event) => {
        const target = event.target;
        if (target === passedOn) return;
        const label = target && target.closest ? target.closest('label') : null;
        if (label && label.control && label.control !== target) {
          passedOn = label.control;
          setTimeout(() => {
            passedOn = null;
          }, 0);
        }
        report('press', words(target));
      },
      true,
    );
    document.addEventListener(
      'input',
      (event) => {
        const field = event.target;
        const typed = 'textarea, input:not([type]), input[type="text"], input[type="password"], input[type="number"], input[type="search"]';
        if (field && field.matches && field.matches(typed)) report('typed', field.id || field.name || String(field.tagName).toLowerCase());
      },
      true,
    );
    window.addEventListener('hashchange', () => report('place', window.location.hash));
    report('place', window.location.hash);
  });
  page.on('filechooser', () => {
    if (counter.on) counter.dialogs += 1;
  });
  // What the page has told so far is in before the counting changes: one more question to the page sees to that.
  const settle = () => page.title().catch(() => {});
  counter.start = async () => {
    await settle();
    counter.on = true;
    place(new URL(page.url()).hash);
  };
  counter.stop = async () => {
    await settle();
    counter.on = false;
  };
  /** Forget what was counted, for a second count on the same page. */
  counter.reset = () => {
    counter.presses.length = 0;
    counter.addresses.length = 0;
    counter.typed.length = 0;
    counter.dialogs = 0;
  };
  counter.aside = async (body) => {
    await settle();
    const was = counter.on;
    counter.on = false;
    try {
      return await body();
    } finally {
      await settle();
      counter.on = was;
    }
  };
  return counter;
}

// -- the end --------------------------------------------------------------------------

/** The last resort, when the process ends with an app still running: no time is left to ask it nicely. */
function cleanUp() {
  for (const app of running) {
    try {
      app.child.kill('SIGKILL');
    } catch (_error) {
      // It has gone already.
    }
  }
  for (const directory of leftovers) fs.rmSync(directory, { recursive: true, force: true });
}

process.on('exit', cleanUp);

/** End the process. An app that still runs is asked to stop first, so that it locks and tidies up. */
async function end(status) {
  hushed = true;
  await Promise.all(Array.from(running, (app) => stopApp(app, 'SIGINT', 10000)));
  process.exit(status);
}

let ending = false;
for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(signal, () => {
    if (ending) return;
    ending = true;
    console.log(`FAIL  the walk-through was stopped by ${signal}`);
    end(1);
  });
}

/** End the process with status 1 when the walk-through has not ended by itself in `minutes`. */
function deadline(minutes) {
  const timer = setTimeout(() => {
    failures.push(`the walk-through did not end within ${minutes} minutes`);
    console.log(`FAIL  the walk-through did not end within ${minutes} minutes`);
    end(summary());
  }, minutes * 60000);
  timer.unref();
}

function summary() {
  for (const entry of expected) {
    if (entry.left > 0 && !entry.optional) problem(`an announced HTTP ${entry.status} on ${entry.path} did not come`);
  }
  console.log(problems.length ? `PROBLEMS (${problems.length}):\n${problems.join('\n')}` : 'no console errors, no failed requests, no unannounced HTTP errors');
  console.log(failures.length ? `FAILED STEPS (${failures.length}):\n${failures.join('\n')}` : 'every step passed');
  return failures.length || problems.length ? 1 : 0;
}

/** Close the browser, print the summary and end the process: 0 when nothing failed, else 1. */
async function finish(browser) {
  hushed = true;
  if (browser) await browser.close().catch(() => {});
  await end(summary());
}

/** Run a walk-through. Whatever it throws outside a step is a failure of its own. */
function run(main) {
  main().catch((error) => {
    failures.push(`the walk-through itself: ${firstLine(error)}`);
    console.log(`FAIL  the walk-through itself: ${firstLine(error)}`);
    return end(summary());
  });
}

module.exports = {
  FRAME,
  WAIT,
  agentCall,
  apiGet,
  check,
  closePage,
  deadline,
  decideApproval,
  eventually,
  expectHttp,
  expectOldSessionRefused,
  finish,
  focused,
  lockElsewhere,
  newPage,
  note,
  openBrowser,
  pageReachedApp,
  pressCopy,
  pressCounter,
  problem,
  readOptions,
  reload,
  run,
  shortDirectory,
  shot,
  sleep,
  startApp,
  started,
  step,
  stopApp,
  switchView,
  withdraw,
};
