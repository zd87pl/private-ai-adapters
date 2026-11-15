// Home, the sheets of its four tiles, the box that asks and the plain pages, walked in a browser
// against the real app.
//
// Run it from the top of the repository (after "npm install --no-save playwright@1.63.0"
// and "npx playwright install chromium"):
//
//   GATE_PYTHON=.venv/bin/python node tests/browser/journey_home.js
//
// Options (environment), all optional:
//   GATE_PYTHON         the Python that has wdva installed (default: python3)
//   GATE_BROWSER        chromium (default), webkit or firefox
//   GATE_SHOTS          where the screenshots go (default: shots/ next to this file)
//   GATE_SCALE          device scale factor of the screenshots (default: 1)
//   GATE_PORT           port of the interface of the first vault (default: the app picks a free one)
//   GATE_OWNER_SECONDS  how long owner mode lasts where the walk waits for its end (default: 6).
//                       Everywhere else it has the app's own value, 600.
//   GATE_RUNS           which of the five runs to walk, for example "A,D" (default: all of them)
//   GATE_RUN_DIR        where the vaults and the homes of the run lie (default: .run/ next to this file)
//   GATE_NO_CONNECT     "skip": walk on although this Python or this checkout lies where the app
//                       writes no entry for it; everything that connects is then left out, and said
//
// Five runs, each with a new vault and an empty home of its own, under the checkout:
//   A  the offered path with a file of the owner's, for Claude: pairing, the passphrase, the three
//      cards of a new vault, a file refused and one sealed, a question in the box, the tile, the
//      look, "Connect", what stands in the settings file, a call through that entry, a passage
//      released and read; Documents, Activity with the details of a row beside the rows, Settings;
//      Polish; a lock, and what Home and the sidebar say after it; the app stopped.
//   B  the write against files the walk made: no folder, other servers and keys, the copies, an
//      entry that points elsewhere, links, broken JSON, a file that changed, owner mode run out;
//      and three answers the walk cannot bring about, put into the app's answer on its way to the
//      page: written and not read back, written while it was read, a linked folder without a place.
//   C  ChatGPT and Grok (TOML), an entry under another name, a file written in a way the app does
//      not change, CODEX_HOME set, and the fourth tile.
//   D  a vault set up on the six pages: two groups, an app on this computer, a pause; what Home
//      and the sheets say then; the box for a document no app can ask for; "Open on".
//   E  the sample document in Polish; a question that finds nothing; five passages waiting; every
//      screen at 400 px; the app stopped under Home.
//
// Runs A and E count what the path costs a person from the first screen of the page: presses,
// fields typed into, file dialogs. Three figures: to the first passage in the box, to "Connected",
// and to the first passage a caller reads through the written entry. See COUNT below.
//
// "Connected" means here what the page means by it: the entry was written and read back. No AI
// app takes part in this walk. A call "through the written entry" is made by agent_call.py with
// the Python, the vault and the name that stand in the settings file the app wrote.
//
// The app writes no entry that names a Python or a vault in a temporary folder, in a folder
// everybody may write, or in one of another account. The walk asks that rule before it starts
// (launch_app.py --can-connect). Where it refuses, the walk says so in a line that begins with
// "#####" and fails, unless GATE_NO_CONNECT=skip allows it to walk what is left.
//
// Every step prints "ok" or "FAIL". Exit status 1 when a step failed, the page logged an error, a
// request failed, or an HTTP error came that no step announced.

'use strict';

const fs = require('fs');
const path = require('path');
const {
  FRAME,
  WAIT,
  agentCallThrough,
  apiGet,
  apiPost,
  canConnect,
  check,
  chooseLanguage,
  closePage,
  deadline,
  eventually,
  expectHttp,
  expectOldSessionRefused,
  finish,
  focused,
  lockElsewhere,
  newHome,
  newPage,
  notOfTable,
  note,
  openBrowser,
  openOn,
  openPage,
  pageReachedApp,
  pressCopy,
  pressCounter,
  readOptions,
  readSettings,
  reload,
  run,
  runDirectory,
  shortDirectory,
  shot,
  startApp,
  started,
  step,
  stopApp,
  textTable,
  textsIn,
  withdraw,
} = require('./common');

const options = readOptions({ ownerSeconds: 6 });
const RUNS = (process.env.GATE_RUNS || 'A,B,C,D,E').toUpperCase().split(/[\s,]+/).filter(Boolean);
/** How long owner mode lasts wherever the walk does not wait for its end: the app's own value. */
const OWNER_SECONDS = 600;
const PASSPHRASE = 'correct horse battery staple';
const REF = /ref=(d-[0-9a-f]{12})\.(\d+)/;

/**
 * What the path may cost a person, counted from the first screen of the page (pairing), in
 * presses. `designed` is what the interface was designed to; the walk fails above `limit`, and
 * says so in a note when a count is above the design.
 *
 *   1. to the first passage in the box that asks: "Create my vault", "Choose a file" (or "Use a
 *      sample document"), "Ask";
 *   2. to "Connected", the entry written and read back: then the tile "Claude" and "Connect";
 *   3. to the first passage a caller reads through the written entry: then "Release once".
 *
 * The sheet that says "Connected" is closed with the Escape key before figure 3: a key, which the
 * count of presses does not hold. Closed with its button "Done" the third figure is one higher.
 * Not counted, and not the page's to shorten: installing, the command in a terminal, reading the
 * pairing code there. The calls of the caller are not presses of the owner.
 */
const COUNT = {
  passage: { designed: 3, limit: 4 },
  connected: { designed: 5, limit: 6 },
  read: { designed: 6, limit: 7 },
};
/** The fields a person types into on that path, and the file dialogs: with a file of their own, and with the sample. */
const TYPED = { own: ['pair-code', 'new-passphrase', 'new-passphrase-again', 'ask-input'], sample: ['pair-code', 'new-passphrase', 'new-passphrase-again'] };

/** What the three figures were, for the last lines of the walk. */
const counted = [];

const PARAGRAPHS = [
  'The monthly rent is 1,180 EUR and is due on the third working day of each month. It is paid by standing order to the cooperative. The deposit was 2,360 EUR and lies in a separate account until the flat is handed back.',
  'The caretaker is reached on weekday mornings. Meter readings are announced a week ahead on the board in the staircase. The laundry room is booked on the list next to its door, and the bicycle space in the yard is for one bicycle.',
  'Quiet hours are from ten in the evening until seven in the morning. Waste is sorted into the bins in the yard. The staircase is kept free of furniture, prams and shoes, and smoking is not allowed in the staircase or in the cellar.',
  'Small repairs of up to 110 EUR each are paid by the tenant, at most 350 EUR in one year. Damage is reported without delay. Shelves, lamps and curtain rails may be put up without asking; changes to walls, floors, pipes or wiring need consent.',
  'Either party may end the lease with three months of written notice, counted from the first day of the month after the notice was received. Notice is given by letter; a message on the notice board or by telephone does not count.',
  'The service charges are settled each spring. The tenant pays 95 EUR a month in advance towards lighting, cleaning, the lift, the garden and the insurance of the building, and may look at the invoices in the office on any Tuesday afternoon.',
  'Liability insurance is held for the whole term. The landlord insures the building; the belongings of the tenant are not covered by that insurance. A lost key is reported at once, and further keys are ordered through the cooperative only.',
  'Pets are kept only with written consent, which is given for one animal at a time and can be withdrawn when neighbours complain twice within a year. Small animals in cages and fish in tanks of up to 100 litres need no consent at all.',
  'The cellar compartment has the number of the flat. Nothing that burns easily is stored there, and the passage is kept clear. The compartment is emptied and swept before the flat is handed back, and its padlock is the tenant\'s own.',
  'Windows are opened wide for ten minutes twice a day in winter, and never left tilted for hours. Mould that appears although the rooms were aired is reported at once; the cooperative then sends a surveyor within fourteen days.',
];

/** The files the owner adds in these runs. They are documents, not settings: they lie under /tmp. */
function writeFiles(directory) {
  fs.mkdirSync(directory);
  const files = {
    flat: path.join(directory, 'flat-notes.txt'),
    boiler: path.join(directory, 'boiler.md'),
    rules: path.join(directory, 'house-rules.txt'),
    word: path.join(directory, 'letter.docx'),
  };
  fs.writeFileSync(files.flat, `${['Notes about the flat'].concat(PARAGRAPHS.slice(0, 7)).join('\n\n')}\n`);
  fs.writeFileSync(files.boiler, '# Boiler\n\nThe boiler was serviced in March. The next service is due in March of next year.\nThe heating engineer left a card in the kitchen drawer.\n');
  fs.writeFileSync(files.rules, `${['House rules'].concat(PARAGRAPHS).join('\n\n')}\n`);
  // Not a text file and not a PDF: the first bytes of a zip archive, which is what a Word file is.
  fs.writeFileSync(files.word, Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x06, 0x00, 0x08, 0x00, 0x00, 0x00, 0x21, 0x00]));
  return files;
}

const trimmed = (text) => String(text || '').replace(/\s+/g, ' ').trim();

/** Whether the app would write an entry for this Python and a vault of this run. Asked once, in `main`. */
let connects = true;

/** A step that needs "Connect" to write. Where the app refuses this Python or this checkout, it is left out, and said. */
async function connectStep(name, body) {
  if (connects) return step(name, body);
  console.log(`SKIP  ${name}  (##### nothing can be connected on this machine, see the line at the start)`);
  return undefined;
}

/** The run that was begun last. */
let lastWalk = null;

/**
 * One vault, one app, one page: what a run works with. The vault and the app's home lie in the
 * directory of this run, under the checkout. The app is started at once; `pair` waits for it.
 */
async function begin(browser, name, { args = [], ownerSeconds = OWNER_SECONDS, port = 0, env = {} } = {}) {
  // A run that failed half way may have left its page open and its app running: neither belongs to the next run.
  if (lastWalk) {
    if (!lastWalk.page.isClosed()) await closePage(lastWalk.page).catch(() => {});
    await stopApp(lastWalk.app, 'SIGINT');
  }
  const store = path.join(runDirectory(), name);
  const walk = { name, store, home: newHome(), base: '', product: '', entry: '', code: '', ownerSeconds, args, env, tables: {} };
  lastWalk = walk;
  walk.app = startApp(options, { args: ['--store', store, ...args], ownerSeconds, port, home: walk.home, env });
  // A new app: from here on a request of the page that fails is a finding again.
  pageReachedApp();
  const page = await newPage(browser, options);
  walk.page = page;
  walk.counter = await pressCounter(page);
  /** Every request the page sent, in order: the method, the path, what followed the "?", and the body where the engine shows it. */
  walk.sent = [];
  /** The requests among them that went to another address than the app's. */
  walk.elsewhere = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    walk.sent.push({ method: request.method(), path: url.pathname, search: url.search, body: request.postData() });
    if (/^https?:$/.test(url.protocol) && walk.base && url.origin !== new URL(walk.base).origin) walk.elsewhere.push(`${request.method()} ${url.origin}${url.pathname}`);
  });
  walk.get = (pathname) => apiGet(page, walk.base, pathname);
  walk.state = () => walk.get('/api/state');
  walk.apps = () => walk.get('/api/apps');
  walk.approvals = () => walk.get('/api/approvals');
  walk.activity = async () => (await walk.get('/api/activity?limit=200')).items;
  walk.count = (selector) => page.locator(selector).count();
  walk.text = async (selector) => trimmed(await page.locator(selector).first().textContent({ timeout: WAIT }));
  walk.address = () => new URL(page.url()).hash;
  walk.sheet = (state) => page.locator(`dialog[open] .connect[data-state="${state}"]`);
  walk.noSheet = () => eventually(async () => (await walk.count('dialog[open]')) === 0, WAIT, 'a sheet stayed open');
  walk.noToast = () => eventually(async () => (await walk.count('#toasts .toast')) === 0, WAIT, 'a message stayed up');
  /** What the page sent since `mark` that was not a question: "POST /api/grants", in order. */
  walk.changesSince = (mark) => walk.sent.slice(mark).filter((entry) => entry.method !== 'GET').map((entry) => `${entry.method} ${entry.path}`);
  /** Where a path the app names with "~" lies in the home of this run. */
  walk.inHome = (shown) => path.join(walk.home, String(shown).replace(/^~[\\/]/, ''));
  /** The tile of the app's list by its word: "claude", "chatgpt", "grok", "custom". */
  walk.tile = async (word) => (await walk.apps()).tiles.find((tile) => tile.tile === word);
  /** Everything that lies in the home of this run, as read_settings.py lists it. */
  walk.tree = () => readSettings(options, 'tree', walk.home);
  /**
   * A picture of the page once the messages in its corner have gone, so that they cover nothing of
   * it, with the pointer put into the corner, so that no link or row is shown as pointed at.
   */
  walk.picture = async (shotName, how) => {
    await eventually(async () => (await walk.count('#toasts .toast')) === 0, WAIT).catch(() => {});
    await page.mouse.move(0, 0);
    await shot(page, shotName, how);
  };
  return walk;
}

/** Wait for the app and open its page on the first screen: the one that asks for the pairing code. */
async function open(walk) {
  const lines = await started(walk.app);
  walk.base = walk.app.url;
  const found = /^pairing code: ([A-Z0-9]{4}-[A-Z0-9]{4})\s/.exec(lines[3]);
  check(found, `pairing line: ${lines[3]}`);
  walk.code = found[1];
  await walk.page.goto(walk.base);
  await walk.page.locator('#pair-code').waitFor({ state: 'visible', timeout: WAIT });
  walk.product = await walk.page.locator('meta[name="application-name"]').getAttribute('content');
  walk.entry = await walk.page.locator('meta[name="application-entry"]').getAttribute('content');
  walk.tables = { en: await textTable(walk.page, walk.base, 'en'), pl: await textTable(walk.page, walk.base, 'pl') };
}

/** Type the pairing code. The eighth character sends it: there is no press on "Pair". */
async function pair(walk) {
  if (!walk.base) await open(walk);
  await walk.page.fill('#pair-code', walk.code);
}

/** Type the new passphrase twice and press "Create my vault". What opens then is for the caller to wait for. */
async function create(walk) {
  const page = walk.page;
  await page.locator('#new-passphrase').waitFor({ state: 'visible', timeout: WAIT });
  await page.fill('#new-passphrase', PASSPHRASE);
  await page.fill('#new-passphrase-again', PASSPHRASE);
  await page.locator('.gate-card button[type="submit"]').click();
}

/** Type the passphrase on the locked screen and press "Unlock". */
async function unlock(walk) {
  const page = walk.page;
  await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
  await page.fill('#unlock-passphrase', PASSPHRASE);
  await page.locator('.gate-card button[type="submit"]').click();
}

/** Pair, create the vault and wait for Home. */
async function toHome(walk) {
  await pair(walk);
  await create(walk);
  await walk.page.locator('#home-title').waitFor({ state: 'attached', timeout: WAIT });
}

/**
 * Stop the app with Ctrl-C and start it again on the same port, where the page that is still open
 * asks. The page is told that its session is gone and asks for the new code, which is given.
 * What opens then is the locked screen.
 */
async function restart(walk, { ownerSeconds = walk.ownerSeconds, env = walk.env, args = walk.args } = {}) {
  const page = walk.page;
  const exit = await stopApp(walk.app, 'SIGINT');
  check(exit && exit.code === 0, `the app ended with ${JSON.stringify(exit)}`);
  check(walk.app.err === '', `standard error: ${walk.app.err}`);
  const before = walk.code;
  walk.ownerSeconds = ownerSeconds;
  // Announced before the app is started, not once its start-up lines were read: the new run refuses
  // the page from its first moment, and the page asks for the state every two seconds. A question
  // that fell between that moment and an announcement made after the lines was a finding, once in
  // many runs: "HTTP 401 GET /api/state", with every step passed.
  const announced = expectOldSessionRefused();
  try {
    walk.app = startApp(options, { args: ['--store', walk.store, ...args], ownerSeconds, port: new URL(walk.base).port, home: walk.home, env });
    const lines = await started(walk.app);
    check(walk.app.url === walk.base, `the app came back at ${walk.app.url}, not at ${walk.base}`);
    const found = /^pairing code: (\S+)\s/.exec(lines[3]);
    check(found && found[1] !== before, `the new run has no code of its own: ${lines[3]}`);
    walk.code = found[1];
    await page.locator('#pair-code').waitFor({ state: 'visible', timeout: 40000 });
    pageReachedApp();
    await page.fill('#pair-code', walk.code);
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
  } finally {
    // From here on a 401 is a finding again, also when the start itself failed.
    withdraw(announced);
  }
}

/**
 * What a run left behind, looked at before its page is closed: the page asked no address but the
 * app's, and it keeps nothing in the browser but its session, which ends with the tab.
 */
async function leftNothingBehind(walk) {
  check(walk.elsewhere.length === 0, `the page asked other addresses than the app's: ${walk.elsewhere.join(', ')}`);
  const kept = await walk.page.evaluate(() => ({ lasting: Object.keys(window.localStorage), session: Object.keys(window.sessionStorage), cookie: document.cookie }));
  check(kept.lasting.length === 0 && kept.cookie === '' && kept.session.length <= 1, `the page keeps in the browser: ${JSON.stringify(kept)}`);
  check((await walk.page.context().cookies()).length === 0, 'the browser was given a cookie');
}

/** Stop the app for good at the end of a run, and close its page. */
async function end(walk) {
  await closePage(walk.page);
  const exit = await stopApp(walk.app, 'SIGINT');
  check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
  check(walk.app.err === '', `standard error: ${walk.app.err}`);
}

/** The last two steps of every run. `files` is what the home of the run must hold, and nothing else. */
async function closing(walk, files) {
  await step(`${walk.name}: in all of this the page asked no address but the app's and kept nothing in the browser but its session`, async () => {
    await leftNothingBehind(walk);
  });
  await step(`${walk.name}: at the end the app quits with status 0 and an empty standard error, and its home holds the files this run expects and nothing else`, async () => {
    await end(walk);
    if (files === null) return;
    const found = walk.tree();
    const wanted = files.slice().sort();
    check(JSON.stringify(found) === JSON.stringify(wanted), `the home holds ${JSON.stringify(found)}, not ${JSON.stringify(wanted)}`);
    check(!found.some((name) => /\.tmp|~$|\.part/.test(name)), `a temporary file was left: ${JSON.stringify(found)}`);
  });
}

/** A text of the table of `code` ("en", "pl") with its places filled; a plural value by the form of `values.n`. */
function worded(walk, code, key, values = {}) {
  let text = walk.tables[code][key];
  check(text !== undefined, `the table has no text ${key}`);
  if (typeof text !== 'string') text = text[new Intl.PluralRules(code).select(values.n)] || text.other;
  const all = { product: walk.product, entry: walk.entry, ...values };
  // Several names in one place are written as the language lists them: "ChatGPT and Codex".
  const written = (value) => (Array.isArray(value) ? new Intl.ListFormat(code, { style: 'long', type: 'conjunction' }).format(value.map(String)) : String(value));
  return text.replace(/\{([a-z]+)\}/g, (whole, name) => (Object.prototype.hasOwnProperty.call(all, name) ? written(all[name]) : whole));
}

/**
 * Wait until the text of an element is what is wanted: a text it must equal, a pattern it must
 * match, or a test it must pass. When it never is, the step fails with what was read last.
 */
async function says(locator, wanted, about) {
  await eventually(async () => {
    const read = trimmed(await locator.textContent({ timeout: 2000 }));
    const met = typeof wanted === 'string' ? read === trimmed(wanted) : wanted instanceof RegExp ? wanted.test(read) : wanted(read);
    check(met, `${about}: ${read}`);
    return true;
  });
}

/** Wait until the element `selector` names says the English text of `key`, with `values` in its places. */
function saysText(walk, selector, key, values = {}, code = 'en') {
  return says(walk.page.locator(selector).first(), worded(walk, code, key, values), `${selector} does not say the text of ${key}`);
}

/** Press "Choose a file" (or whatever opens the dialog of the system) as a person does, and answer the dialog with `files`. */
async function chooseFiles(walk, control, files) {
  const chooser = walk.page.waitForEvent('filechooser', { timeout: WAIT });
  chooser.catch(() => {});
  await control.click();
  await (await chooser).setFiles(files);
}

/** Type a question into the box and press "Ask"; wait until the box has answered. */
async function ask(walk, question, { typed = true } = {}) {
  const page = walk.page;
  if (typed) await page.fill('#ask-input', question);
  await page.locator('#ask-button').click();
  await eventually(async () => (await walk.count('#trial, #trial-none, #ask-error')) > 0 && !(await page.locator('#ask-button').getAttribute('aria-busy')), WAIT, 'the box did not answer');
}

/** The passage on the trial card: how many characters the card says it has and how many it holds, and its box against what the box holds. */
function passageBox(walk) {
  return walk.page.evaluate(() => {
    const node = document.querySelector('#trial pre');
    const said = /^([\d\s.,\u00a0\u202f]+)/.exec(document.querySelector('#trial-meta').textContent.trim());
    return { characters: said ? Number(said[1].replace(/\D/g, '')) : -1, held: Array.from(node.textContent).length, inner: node.scrollHeight, box: node.clientHeight, wide: node.scrollWidth, width: node.clientWidth };
  });
}

/** Where the rows of the page stand, where the head of the column ends, and where the panel with the details of an entry lies, if it is open. */
function rowsAndPanel(walk) {
  return walk.page.evaluate(() => {
    const edge = (node) => {
      const box = node.getBoundingClientRect();
      return { left: Math.round(box.left), right: Math.round(box.right), width: Math.round(box.width) };
    };
    const panel = document.querySelector('aside.panel');
    const title = document.querySelector('.column-top');
    return {
      window: window.innerWidth,
      wide: document.documentElement.scrollWidth,
      panel: panel ? edge(panel) : null,
      titleRight: title ? edge(title).right : 0,
      rows: Array.from(document.querySelectorAll('#main .row-open')).map((row) => ({ ...edge(row), timeRight: edge(row.querySelector('.row-time')).right })),
    };
  });
}

/** Press a tile and wait until its sheet shows `state` (a state of the sheet, or one of several). */
async function pressTile(walk, word, state) {
  await walk.page.locator(`#tile-${word}`).click();
  return sheetShows(walk, state);
}

/** Wait until the open sheet shows one of the wanted states; returns the one it shows. */
async function sheetShows(walk, state) {
  const wanted = Array.isArray(state) ? state : [state];
  const sheet = walk.page.locator('dialog[open] .connect');
  return eventually(
    async () => {
      const shown = await sheet.getAttribute('data-state', { timeout: 2000 });
      check(wanted.includes(shown), `the sheet shows the state "${shown}" (${await sheet.getAttribute('data-word')}), not ${JSON.stringify(wanted)}`);
      return shown;
    },
    WAIT,
    'no sheet of a tile is open',
  );
}

/** Close the open sheet with the Escape key, as a person at a keyboard does. */
async function escape(walk) {
  await walk.page.keyboard.press('Escape');
  await walk.noSheet();
}

/** Press the button of the open sheet that `id` names, and wait until the sheet has gone. */
async function closeWith(walk, id) {
  await walk.page.locator(`dialog[open] #${id}`).click();
  await walk.noSheet();
}

/** The entry of this vault in a settings file of the home, as read_settings.py reads it. */
function entryIn(walk, file) {
  return readSettings(options, 'entry', file, walk.entry);
}

/** Give a file a time of change that lies `seconds` back: the app writes no file that changed a moment ago. */
function aged(file, seconds = 30) {
  const then = new Date(Date.now() - seconds * 1000);
  fs.utimesSync(file, then, then);
}

/** What is in a folder, with the bytes, the mode and the kind of each name: to see that a refusal left it as it was. */
function folderAsIs(folder) {
  if (!fs.existsSync(folder)) return 'no folder';
  return JSON.stringify(
    fs
      .readdirSync(folder)
      .sort()
      .map((name) => {
        const found = fs.lstatSync(path.join(folder, name));
        return [name, found.mode, found.isSymbolicLink() ? fs.readlinkSync(path.join(folder, name)) : found.isFile() ? fs.readFileSync(path.join(folder, name)).toString('base64') : 'folder'];
      }),
  );
}

/** Say one figure of the count, with every press named, and fail above its limit. */
function judge(walk, which, what) {
  const { presses, typed, dialogs } = walk.counter;
  const { designed, limit } = COUNT[which];
  console.log(`count ${walk.name}, ${what}: ${presses.length} presses, ${typed.length} fields typed into, ${dialogs} file dialog(s), from the first screen of the page`);
  presses.forEach((press, index) => console.log(`      ${index + 1}. ${press}`));
  counted.push({ run: walk.name, which, presses: presses.length });
  check(presses.length <= limit, `${what} took ${presses.length} presses, more than ${limit}: ${presses.join(' | ')}`);
  if (presses.length > designed) note(`run ${walk.name}: ${what} took ${presses.length} presses, where the design counts ${designed}: ${presses.join(' | ')}`);
}

/**
 * Press "Release once" on the one card that waits, once, when it can be pressed, and wait until
 * the page has taken the decision. One press: the walk counts it.
 */
async function releaseOnce(walk) {
  const page = walk.page;
  const card = page.locator('.approval').first();
  await card.waitFor({ state: 'visible', timeout: WAIT });
  await card.locator('button[data-key="release"]:not(.is-waiting)').click({ timeout: WAIT });
  await eventually(async () => (await walk.approvals()).granted.length > 0 || (await walk.count('.approval')) === 0, WAIT, 'the press on "Release once" changed nothing');
}

/** Things a person can press on the page that is shown: links, buttons that are on, fields that are on. */
function pressables(walk, scope) {
  return walk.page.locator(scope).evaluate((root) => {
    const found = [];
    for (const node of root.querySelectorAll('a[href], button, label.file-button, input:not([type="file"]), select, textarea')) {
      if (node.disabled || node.closest('[hidden], dialog:not([open])')) continue;
      const box = node.getBoundingClientRect();
      if (box.width === 0 || box.height === 0) continue;
      found.push(node.id || String(node.textContent || node.getAttribute('aria-label') || node.tagName).trim().slice(0, 30));
    }
    return found;
  });
}

// =====================================================================================
// Run A: the offered path with a file of the owner's, for Claude
// =====================================================================================

async function runA(browser, files) {
  const walk = await begin(browser, 'A', { port: options.port });
  const page = walk.page;
  const en = (key, values) => worded(walk, 'en', key, values);
  let tile = null;
  let file = '';
  let written = null;
  let ref = '';

  await step('A: the first screen asks for the code; "Polski" and back change its words; the eighth character pairs, without a press', async () => {
    await open(walk);
    await walk.counter.start();
    await walk.counter.aside(async () => {
      const title = await walk.text('.gate-card h1');
      check(title === en('pair.title'), `the pairing screen is headed "${title}"`);
      await chooseLanguage(page, '.gate-lang', 'pl');
      check((await walk.text('.gate-card h1')) === worded(walk, 'pl', 'pair.title'), `in Polish the pairing screen is headed "${await walk.text('.gate-card h1')}"`);
      await walk.picture('h01-pair-polish');
      await chooseLanguage(page, '.gate-lang', 'en');
      check((await walk.text('.gate-card h1')) === title, 'back in English the heading is another one');
    });
    await page.locator('#pair-code').pressSequentially(walk.code.replace('-', '').slice(0, 7));
    check((await walk.count('#new-passphrase')) === 0, 'seven characters paired');
    await page.locator('#pair-code').pressSequentially(walk.code.replace('-', '').slice(7));
    await page.locator('#new-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    check(walk.counter.presses.length === 0, `pairing took a press: ${walk.counter.presses.join(' | ')}`);
  });

  await step('A: "Create my vault" opens Home for a new vault: three cards, one sentence, 15 things to press of which 8 in the column, and "Choose a file" the filled one', async () => {
    await create(walk);
    await page.locator('#first-title').waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/home', `the address is ${walk.address()}`);
    await page.locator('#tile-claude').waitFor({ state: 'visible', timeout: WAIT });
    await page.locator('#first-file').waitFor({ state: 'attached', timeout: WAIT });
    check((await walk.text('#first-title')) === en('home.new.title'), `the cards are headed "${await walk.text('#first-title')}"`);
    const titles = (await page.locator('.first-card .first-title').allTextContents()).map(trimmed);
    check(JSON.stringify(titles) === JSON.stringify([`1${en('home.new.card1.title')}`, `2${en('home.ask.title')}`, `3${en('home.new.card3.title')}`]), `the three cards are ${JSON.stringify(titles)}`);
    // Nothing is drawn twice: the box and the tiles stand in the cards, and no part of an ordinary day is there.
    check((await walk.count('#ask-input')) === 1 && (await walk.count('.tile')) === 4, 'the box or the tiles are drawn twice, or not at all');
    check((await walk.count('#home-ask-title, #home-connect-title, #home-left-title, #home-wait-title')) === 0, 'a part of an ordinary day is drawn beside the three cards');
    // Two sentences at most, and both from the first moment: the tiles can be pressed before a document is there, so what can be connected is said under them at once.
    const sentences = (await page.locator('#main .home-say').allTextContents()).map(trimmed);
    check(JSON.stringify(sentences) === JSON.stringify([en('home.new.say.1'), en('home.connect.say.1')]), `the sentences of the new Home are ${JSON.stringify(sentences)}`);
    await saysText(walk, '#ask-nodocs', 'ask.say.1.nodocs');
    const all = await pressables(walk, 'body');
    const column = await pressables(walk, '#main');
    check(column.length === 8, `the column of the new Home has ${column.length} things to press, not 8: ${column.join(', ')}`);
    check(all.length === 15, `the new Home has ${all.length} things to press, not 15: ${all.join(', ')}`);
    // One filled button: "Choose a file". The box is off until there is a document.
    const filled = await page.locator('.btn-primary').evaluateAll((nodes) => nodes.filter((node) => node.getBoundingClientRect().width > 0).map((node) => node.textContent.trim()));
    check(JSON.stringify(filled) === JSON.stringify([en('home.new.card1.button')]), `the filled buttons are ${JSON.stringify(filled)}`);
    check(await page.locator('#ask-input').isDisabled(), 'the box can be typed into before there is a document');
    check(await page.locator('#ask-button').isDisabled(), '"Ask" can be pressed before there is a document');
    const lines = (await page.locator('.tile .tile-state').allTextContents()).map(trimmed);
    check(JSON.stringify(lines) === JSON.stringify([en('tile.status.connect'), en('tile.status.connect'), en('tile.status.connect'), en('tile.status.custom.connect')]), `the tiles say ${JSON.stringify(lines)}`);
    check(await focused(page.locator('#home-title')), 'the title of Home does not have the focus');
    const state = await walk.state();
    check(state.interface.setup === 'open' && state.interface.view === 'basic', `the app keeps ${JSON.stringify(state.interface)}`);
    await walk.picture('h02-home-new', { whole: true });
  });

  await step('A: a Word file is refused with what to do about it; a text file is sealed, the box takes the focus, and "Ask" is the filled button', async () => {
    await walk.counter.aside(async () => {
      await chooseFiles(walk, page.locator('#first-add label.file-button'), [files.word]);
      await says(page.locator('#first-add .upload-error').first(), (read) => read.includes(en('docs.error.type')), 'the Word file is not refused with what to do about it');
      check((await walk.state()).encryption.documents === 0, 'the Word file was added');
    });
    await chooseFiles(walk, page.locator('#first-add label.file-button'), [files.flat]);
    await page.locator('#first-count').waitFor({ state: 'visible', timeout: WAIT });
    await saysText(walk, '#first-count', 'home.new.card1.status', { n: 1 });
    await saysText(walk, '#first-sealed', 'home.new.result.sealed');
    // The file that was refused before has been dealt with: no red line stays under a card that is marked done.
    check((await walk.count('#first-add .upload-error')) === 0, 'the line of the refused Word file stays after a file was sealed');
    await says(page.locator('#first-add .upload-who'), en('docs.result.nobody'), 'the card does not say that no app can ask for the file yet');
    await eventually(() => focused(page.locator('#ask-input')), WAIT, 'the input of the box does not have the focus');
    check((await page.locator('#ask-input').inputValue()) === '', 'the box holds a question for a file of the owner\'s own');
    const filled = await page.locator('.btn-primary').evaluateAll((nodes) => nodes.filter((node) => node.getBoundingClientRect().width > 0).map((node) => node.textContent.trim()));
    check(filled.length === 0 || JSON.stringify(filled) === JSON.stringify([en('ask.button.ask')]), `the filled buttons are ${JSON.stringify(filled)}`);
    // The second sentence of Home stands under the tiles, as it did before the document.
    await saysText(walk, '#home-connect-say', 'home.connect.say.1');
    check((await walk.count('#main .home-say')) === 2, 'Home says more than two sentences');
    check(fs.existsSync(files.flat), 'the file the owner chose is gone');
    await walk.picture('h03-home-document', { whole: true });
  });

  await step('A: a question shows the one passage, the words it was found by, and the sentence for a vault without an app; three presses to here', async () => {
    const before = (await walk.state()).record.head_hash;
    await ask(walk, 'When is the rent due?');
    await page.locator('#trial').waitFor({ state: 'visible', timeout: WAIT });
    await walk.counter.stop();
    judge(walk, 'passage', 'to the first passage in the box');
    check(JSON.stringify(walk.counter.typed) === JSON.stringify(TYPED.own), `the fields typed into are ${JSON.stringify(walk.counter.typed)}`);
    check(walk.counter.dialogs === 1, `${walk.counter.dialogs} file dialogs opened`);
    await saysText(walk, '#trial-say-1', 'ask.say.1.connect');
    await saysText(walk, '#trial-say-2', 'ask.say.2');
    const body = await walk.text('#trial pre');
    check(body.includes('1,180 EUR') && body.includes('due on the third working day'), `the passage is: ${body.slice(0, 120)}`);
    // The card says "only this passage": all of it is on the card, and none of it scrolls out of sight inside its box.
    const shown = await passageBox(walk);
    check(shown.characters === shown.held && shown.characters > 600, `the card says ${shown.characters} characters and holds ${shown.held}`);
    check(shown.inner <= shown.box && shown.wide <= shown.width, `the passage is ${shown.inner} px high in a box of ${shown.box} px: part of it is out of sight`);
    await says(page.locator('#trial-words'), (read) => read.startsWith(en('ask.status.words', { words: '' })) && /\brent\b/.test(read) && /\bdue\b/.test(read), 'the card does not say by which words the passage was found');
    await says(page.locator('#trial-meta'), (read) => read.includes('flat-notes'), 'the card does not name the document');
    await saysText(walk, '#ask-count', 'ask.status.count', { n: 1 });
    check((await walk.count('#trial.approval, #trial .approval-actions')) === 0, 'the answer of the box is drawn as an approval card');
    check((await walk.count('#trial .btn-primary')) === 0, 'a button of the answer is filled');
    await walk.picture('h04-trial', { whole: true });
    // Both buttons send nothing, and each says what would have happened.
    const mark = walk.sent.length;
    await page.locator('#trial-no').click();
    await saysText(walk, '#trial-result', 'ask.result.no');
    await page.locator('#trial-again').click();
    await eventually(() => focused(page.locator('#ask-input')), WAIT, '"Ask something else" does not lead back to the input');
    check(walk.changesSince(mark).length === 0, `the buttons of the answer sent ${walk.changesSince(mark).join(', ')}`);
    await ask(walk, 'How much is the deposit?');
    await page.locator('#trial-yes').click();
    await saysText(walk, '#trial-result', 'ask.result.yes');
    check(JSON.stringify(walk.changesSince(mark)) === JSON.stringify(['POST /api/try']), `since the first answer the page sent ${walk.changesSince(mark).join(', ')}`);
    // Nothing was recorded, nothing left, and nothing waits: the record's head is the one from before.
    const state = await walk.state();
    check(state.record.head_hash === before && before !== '', 'a trial question moved the head of the record');
    check(state.trial.questions === 2, `the app counts ${state.trial.questions} trial questions`);
    check((await walk.activity()).every((item) => item.kind !== 'release'), 'a trial question is listed as given to an app');
    await saysText(walk, '#ask-count', 'ask.status.count', { n: 2 });
    // A question of numbers only, and one that finds nothing, are each said in their own words.
    await page.locator('#trial-again').click();
    await ask(walk, '1180 2360');
    await saysText(walk, '#trial-none', 'ask.result.nowords');
    await ask(walk, 'zeppelin');
    await saysText(walk, '#trial-none', 'ask.result.none');
    await ask(walk, 'When is the rent due?');
    await page.locator('#trial').waitFor({ state: 'visible', timeout: WAIT });
  });

  await connectStep('A: the tile "Claude" looks at one file and offers: quit first, what is written where, where text goes; "Details" shows the command', async () => {
    tile = await walk.tile('claude');
    file = walk.inHome(tile.path);
    // As if Claude Desktop were installed: its settings folder is there, and no file in it.
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const mark = walk.sent.length;
    walk.counter.on = true;
    await pressTile(walk, 'claude', 'offer');
    walk.counter.on = false;
    check(JSON.stringify(walk.changesSince(mark)) === JSON.stringify(['POST /api/connect/claude-desktop/look']), `the press on the tile sent ${walk.changesSince(mark).join(', ')}`);
    check(walk.sent.slice(mark).every((entry) => entry.path !== '/api/connect/claude-desktop/look' || !entry.body || entry.body === '{}'), 'the look sent something in its body');
    await says(page.locator('dialog[open] h2').first(), en('connect.title', { label: tile.label, kind: en('connect.label.kind.desktop') }), 'the sheet is not headed by what the press will do');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.new', { readers: tile.readers, path: tile.path });
    await saysText(walk, 'dialog[open] #connect-say-2', 'connect.offer.say.2', { destination: tile.destination });
    check((await walk.text('dialog[open] #connect-say-1')).startsWith('Quit Claude Desktop first'), 'the offer does not ask to quit the program first');
    const filled = (await page.locator('dialog[open] .btn-primary').allTextContents()).map(trimmed);
    check(JSON.stringify(filled) === JSON.stringify([en('connect.button.connect')]), `the filled buttons of the sheet are ${JSON.stringify(filled)}`);
    check((await walk.count('dialog[open] #connect-disconnect')) === 0, '"Disconnect" is offered for an app that is not registered');
    await walk.counter.aside(async () => {
      await page.locator('dialog[open] #connect-details-button').click();
      const command = await walk.text('dialog[open] #connect-details pre');
      check(command.includes(options.python.split('/').pop()) || command.includes('python'), `the command shown is ${command}`);
      check(command.includes('--store') && command.includes(walk.store) && command.includes('claude-desktop'), `the command shown is ${command}`);
      await walk.picture('h05-sheet-offer');
      await page.locator('dialog[open] #connect-details-button').click();
    });
    check(!fs.existsSync(file), 'the look made the file');
  });

  await connectStep('A: "Connect" writes one entry and nothing else, mode 0600, and the sheet says "Written and read back; not tried with Claude Desktop itself"; five presses to here', async () => {
    const mark = walk.sent.length;
    walk.counter.on = true;
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    walk.counter.on = false;
    judge(walk, 'connected', 'to "Connected"');
    // (With an app in the vault the three cards are done: the page may tell the app so in the same moment.)
    const sent = walk.changesSince(mark).filter((entry) => entry !== 'POST /api/interface');
    check(JSON.stringify(sent) === JSON.stringify(['POST /api/connect/claude-desktop']), `"Connect" sent ${sent.join(', ')}`);
    const body = JSON.parse(walk.sent.slice(mark).find((entry) => entry.path === '/api/connect/claude-desktop').body || '{}');
    check(JSON.stringify(body) === JSON.stringify({ expect: 'no_file' }), `"Connect" sent ${JSON.stringify(body)}`);
    await says(page.locator('dialog[open] h2').first(), en('connect.done.title'), 'the sheet does not say "Connected"');
    await saysText(walk, 'dialog[open] #connect-result', 'connect.done.status.tried', { label: tile.label });
    check((await walk.text('dialog[open] #connect-result')) === 'Written and read back; not tried with Claude Desktop itself.', `the result says: ${await walk.text('dialog[open] #connect-result')}`);
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.done.say.1', { label: tile.label });
    await saysText(walk, 'dialog[open] #connect-say-2', 'connect.done.say.2', { label: tile.label });
    check((await walk.count('dialog[open] #connect-copy')) === 0, 'the sheet names a copy of a file that was not there before');
    const start = en('connect.ask.start');
    check((await page.locator('dialog[open] .copy-line pre').first().textContent()) === start, 'the line that starts a question is not shown as it is copied');
    await walk.counter.aside(() => pressCopy(page, options, page.locator('dialog[open] #connect-ask-copy'), start));
    await walk.picture('h06-sheet-connected');
    // The file in the home: the entry and nothing else.
    written = entryIn(walk, file);
    check(written.parses && JSON.stringify(written.keys) === JSON.stringify(['mcpServers']) && JSON.stringify(written.servers) === JSON.stringify([walk.entry]), `the file holds ${JSON.stringify(written)}`);
    check(written.mode === '0o600' && written.names === 1 && !written.link, `the file has mode ${written.mode} and ${written.names} names`);
    check(JSON.stringify(Object.keys(written.entry)) === JSON.stringify(['command', 'args']), `the entry holds ${JSON.stringify(Object.keys(written.entry))}`);
    check(written.entry.args.includes(walk.store) && written.entry.args.includes('claude-desktop'), `the entry is ${JSON.stringify(written.entry)}`);
    check(JSON.stringify(walk.tree()) === JSON.stringify(walk.tree().filter((name) => name.endsWith('/') || name === path.relative(walk.home, file))), `the home holds more than the one file: ${JSON.stringify(walk.tree())}`);
    // The vault: the app under the tile's name, with "ask me each time" for the one group, which is open to it.
    const apps = await walk.apps();
    const app = apps.apps.find((one) => one.name === tile.name);
    check(app && app.hosted && app.destination === tile.destination, `the app is ${JSON.stringify(app)}`);
    check(JSON.stringify(app.grants) === JSON.stringify({ documents: 'approve' }), `the app was given ${JSON.stringify(app.grants)}`);
    // The sheet is closed with the Escape key: a key, which the count of presses does not hold.
    await escape(walk);
  });

  await connectStep('A: the three cards go by themselves; the tile says "Waiting for its first request", and its sheet says what to do then', async () => {
    await eventually(async () => (await walk.count('#first-title')) === 0, WAIT, 'the three cards are still there');
    check((await walk.state()).interface.setup === 'done', 'the app was not told that the first steps are done');
    await page.locator('#home-ask-title').waitFor({ state: 'visible', timeout: WAIT });
    check((await walk.count('#ask-input')) === 1 && (await walk.count('.tile')) === 4, 'the box or the tiles are drawn twice, or not at all');
    await saysText(walk, '#tile-claude .tile-state', 'tile.status.never');
    await saysText(walk, '#home-left-none', 'home.left.status.none');
    await pressTile(walk, 'claude', 'on');
    await saysText(walk, 'dialog[open] #connect-on-file', 'connect.on.status.file', { path: tile.path });
    await saysText(walk, 'dialog[open] #connect-on-seen', 'tile.status.never');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.on.say.1', { label: tile.label });
    check((await walk.count('dialog[open] #connect-go')) === 0, '"Connect" is offered for an app that is on');
    const filled = (await page.locator('dialog[open] .btn-primary').allTextContents()).map(trimmed);
    check(JSON.stringify(filled) === JSON.stringify([en('common.button.done')]), `the filled buttons of the sheet are ${JSON.stringify(filled)}`);
    await walk.picture('h07-sheet-on');
    await closeWith(walk, 'connect-done');
  });

  await connectStep('A: a call through the command that stands in the file: the tile says "Last asked …"; a passage waits, the trial card has no buttons meanwhile', async () => {
    const status = agentCallThrough(written.entry, 'status');
    check(status.code === 'OK', `status through the written entry: ${status.code}`);
    await says(page.locator('#tile-claude .tile-state'), (read) => read.startsWith(en('tile.status.seen', { ago: '' })), 'the tile does not say when the app last asked');
    const search = agentCallThrough(written.entry, 'search', 'monthly rent due');
    const found = REF.exec(search.text);
    check(search.code === 'OK' && found, `search through the written entry: ${search.code} ${search.text.slice(0, 200)}`);
    ref = `${found[1]}.${found[2]}`;
    expectHttp(404, '/favicon.ico', { optional: true });
    const read = agentCallThrough(written.entry, 'read', ref);
    check(read.code === 'NEEDS_APPROVAL', `the first read through the written entry: ${read.code}`);
    check(read.text.includes(`open the ${walk.product} page in their browser`) && !read.text.includes('Approvals'), `the caller was told: ${read.text}`);
    await page.locator('.approval').first().waitFor({ state: 'visible', timeout: WAIT });
    await saysText(walk, '#home-wait-title', 'home.wait.title');
    // While a real passage waits, the trial card has no buttons, and says why; and nothing on Home is filled.
    await ask(walk, 'When is the rent due?');
    await saysText(walk, '#trial-real', 'ask.status.real');
    check((await walk.count('#trial-yes, #trial-no')) === 0, 'the trial card has its two buttons while a real passage waits');
    check((await walk.count('#main .btn-primary')) === 0, 'a button of Home is filled while a passage waits');
    await walk.picture('h08-home-waiting', { whole: true });
  });

  await connectStep('A: "Release once", and the caller reads the passage through the written entry; it is a row under "Recently given to apps"; six presses to here', async () => {
    await walk.counter.start();
    await releaseOnce(walk);
    await walk.counter.stop();
    const read = agentCallThrough(written.entry, 'read', ref);
    check(read.code === 'OK' && read.released > 0 && read.text.includes('1,180 EUR'), `the read after the release: ${read.code} ${read.text.slice(0, 160)}`);
    judge(walk, 'read', 'to the first passage a caller reads through the written entry');
    check(JSON.stringify(walk.counter.typed) === JSON.stringify(TYPED.own) && walk.counter.dialogs === 1, `fields ${JSON.stringify(walk.counter.typed)}, dialogs ${walk.counter.dialogs}`);
    await page.locator('#home-left-title').waitFor({ state: 'visible', timeout: WAIT });
    // The newest row is the passage that was read; the search before it is a row of its own.
    await says(page.locator('#main .act-rows > li').first(), (text) => text.includes('claude-desktop') && text.includes('flat-notes'), 'the newest row under "Recently given to apps" does not name the app and the document');
    check((await walk.count('#main .act-rows > li')) <= 3, 'Home lists more than three rows');
    await eventually(async () => (await walk.count('.approval')) === 0, WAIT, 'the card is still there');
    check((await walk.count('#home-wait-title')) === 0 || (await walk.count('.approval')) === 0, '"Waiting for you" still shows a card');
    await walk.picture('h09-home-day', { whole: true });
  });

  await step('A: Documents, Activity and Settings are one press away each, and say what the vault holds', async () => {
    await openPage(page, 'documents');
    await says(page.locator('#main'), (read) => read.includes('flat-notes'), 'the page Documents does not list the document');
    await openPage(page, 'activity');
    if (connects) {
      await eventually(async () => (await walk.count('#main .act-rows > li')) >= 1, WAIT, 'the page Activity lists nothing');
      // The details of a row open beside the rows: in this window no row stands under the panel, and none is cut.
      const closed = await rowsAndPanel(walk);
      await page.locator('#main .row-open').first().click();
      await page.locator('aside.panel .panel-title').waitFor({ state: 'visible', timeout: WAIT });
      const open = await eventually(async () => {
        const now = await rowsAndPanel(walk);
        // (The panel slides into its place: it is measured once it has come to rest at the window's edge.)
        check(now.panel && now.panel.right === now.window, 'the panel has not come to rest');
        return now;
      });
      check(open.window >= 1080, `the window of the walk is ${open.window} px wide`);
      check(open.rows.length >= 1 && open.rows.every((row) => row.right <= open.panel.left && row.timeRight <= open.panel.left), `with the details open, rows end at ${JSON.stringify(open.rows.map((row) => row.right))} and the panel begins at ${open.panel.left}`);
      check(open.titleRight <= open.panel.left && open.wide <= open.window, 'with the details open the head of the column stands under the panel, or the page scrolls sideways');
      check(open.rows.every((row) => row.width >= 368), `with the details open the rows are ${JSON.stringify(open.rows.map((row) => row.width))} px wide`);
      await walk.picture('h13-activity-details');
      await page.keyboard.press('Escape');
      await page.locator('aside.panel').waitFor({ state: 'detached', timeout: WAIT });
      const again = await rowsAndPanel(walk);
      check(JSON.stringify(again.rows) === JSON.stringify(closed.rows), 'with the details closed again the rows are not where they were');
    }
    await openPage(page, 'settings');
    check((await walk.count('#main a.row-link[href^="#/pro/"]')) === 6, 'Settings does not lead to the six pages');
    await openPage(page, 'home');
    await page.locator('#home-title').waitFor({ state: 'attached', timeout: WAIT });
  });

  await step('A: Polish chosen: every text on Home is the Polish table\'s; a reload keeps it, and so does the locked screen', async () => {
    const polish = walk.tables.pl;
    await chooseLanguage(page, '.side-foot', 'pl');
    await eventually(async () => (await walk.text('#home-ask-title, #first-title')) !== '', WAIT);
    const names = [walk.product, 'EN', 'PL', 'Claude', 'ChatGPT', 'Grok', 'flat-notes', 'claude-desktop', 'Anthropic', /^\d+$/, /^[\d\s.,:]+$/];
    const foreign = async () => notOfTable(await textsIn(page, 'body', 'pre, .path, time, noscript, dialog:not([open])'), polish, names);
    await eventually(async () => {
      const found = await foreign();
      check(found.length === 0, `in Polish, Home still shows: ${JSON.stringify(found)}`);
      return true;
    });
    for (const key of ['home.ask.title', 'home.connect.title', 'tile.custom', 'ask.button.ask']) {
      check(notOfTable(await textsIn(page, 'body', 'pre, .path, time, noscript'), { [key]: polish[key] }).length < (await textsIn(page, 'body', 'pre, .path, time, noscript')).length, `in Polish, Home does not show the text of ${key}`);
    }
    await walk.picture('h10-home-polish', { whole: true });
    await reload(page);
    await page.locator('html[lang="pl"]').waitFor({ state: 'attached', timeout: WAIT });
    await page.locator('#home-title').waitFor({ state: 'attached', timeout: WAIT });
    await eventually(async () => (await walk.text('#home-title')) === worded(walk, 'pl', 'home.title'), WAIT, 'after a reload Home is not Polish');
    check((await walk.state()).interface.language === 'pl', 'the app does not keep the language');
    await page.locator('.side-foot button.lock-now').click();
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    // (The name of the vault is the name of its folder, and its place is a path: data, not words of the page.)
    const locked = notOfTable(await textsIn(page, '.gate-card', 'time, .path'), polish, [walk.product, path.basename(walk.store), /^[\d\s.,:]+$/]);
    check(locked.length === 0, `in Polish, the locked screen still shows: ${JSON.stringify(locked)}`);
    await walk.picture('h11-locked-polish');
    await chooseLanguage(page, '.gate-lang', 'en');
    await unlock(walk);
    await page.locator('#home-title').waitFor({ state: 'attached', timeout: WAIT });
  });

  await step('A: after the lock Home still says how many trial questions were asked since the app was started; the sidebar says until when in one line, without a date for a time within 24 hours', async () => {
    // "Lock now" asked for no passphrase. It began the count since the unlock anew, and left the other.
    const before = (await walk.state()).trial;
    check(before.questions === 0 && before.since_start >= 5, `after the lock the app counts ${JSON.stringify(before)}`);
    await saysText(walk, '#ask-count-start', 'ask.status.count.start', { n: before.since_start });
    check((await walk.count('#ask-count')) === 0, 'Home shows a count since the unlock although nothing was asked since');
    await ask(walk, 'When is the rent due?');
    await saysText(walk, '#ask-count', 'ask.status.count', { n: 1 });
    await saysText(walk, '#ask-count-start', 'ask.status.count.start', { n: before.since_start + 1 });
    const record = (await walk.state()).record.head_seq;
    await ask(walk, 'zeppelin');
    const after = await walk.state();
    check(after.trial.questions === 2 && after.trial.since_start === before.since_start + 2 && after.record.head_seq === record, `the app counts ${JSON.stringify(after.trial)}`);
    await walk.picture('h14-home-after-lock', { whole: true });

    // The line in the sidebar. The vault locks itself in less than 24 hours: the time of day alone, on one line.
    const timeOnly = /^\d{1,2}[:.]\d{2}(\s?[AP]M)?$/i;
    const untilWhen = async () => {
      const read = trimmed(await page.locator('#side-lock').textContent());
      const lead = en('nav.status.until', { time: '' });
      check(read.startsWith(lead), `the sidebar says: ${read}`);
      const lines = await page.locator('#side-lock').evaluate((node) => Math.round(node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight)));
      return { time: read.slice(lead.length), lines };
    };
    const unlockFor = async (hours) => {
      // The lock comes from outside the page: whatever the page was asking at that moment is answered 409.
      const meanwhile = expectHttp(409, /^\/api\//, { times: 20, optional: true });
      await lockElsewhere(page, walk.base);
      await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
      withdraw(meanwhile);
      const state = await apiPost(page, walk.base, '/api/unlock', { passphrase: PASSPHRASE, hours });
      await page.locator('#home-title').waitFor({ state: 'attached', timeout: WAIT });
      return state.locks_at;
    };
    let line = await untilWhen();
    check(timeOnly.test(line.time) && line.lines === 1, `the sidebar says "${line.time}" on ${line.lines} lines`);
    // Unlocked until a moment after midnight that is less than 24 hours away: another day, and still no date.
    const now = new Date();
    const toMidnight = (new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1).getTime() - now.getTime()) / 3600000;
    const locksAt = new Date(await unlockFor((toMidnight + 24) / 2));
    check(locksAt.getDate() !== new Date().getDate(), `the vault locks itself at ${locksAt}, which is today`);
    line = await eventually(async () => {
      const read = await untilWhen();
      check(timeOnly.test(read.time) && read.lines === 1, `for a time after midnight the sidebar says "${read.time}" on ${read.lines} lines`);
      return read;
    });
    await walk.picture('h15-sidebar-after-midnight');
    // More than 24 hours away, the time of day alone would not say which day: the date stands with it.
    await unlockFor(30);
    await eventually(async () => {
      const read = await untilWhen();
      check(!timeOnly.test(read.time) && /\d{1,2}[:.]\d{2}/.test(read.time) && read.time.length > line.time.length, `for a time 30 hours away the sidebar says "${read.time}"`);
      return true;
    });
  });

  await step('A: Ctrl-C: Home says in a bar that the app is not answering, with the command that starts it again', async () => {
    await leftNothingBehind(walk);
    const exit = await stopApp(walk.app, 'SIGINT');
    check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
    check(walk.app.err === '', `standard error: ${walk.app.err}`);
    await page.locator('.offline-bar').waitFor({ state: 'visible', timeout: 40000 });
    await says(page.locator('.offline-bar'), (read) => read.includes(en('bar.say.1')) && read.includes(en('bar.say.2')), 'the bar does not say that the app is not answering and how to start it again');
    const command = trimmed(await page.locator('.offline-bar pre').first().textContent());
    check(command.includes('gate app') && command.includes(walk.store), `the command in the bar is ${command}`);
    await walk.picture('h12-home-stopped');
    await closePage(page);
    const wanted = connects ? [path.relative(walk.home, file)] : [];
    const found = walk.tree().filter((name) => !name.endsWith('/'));
    check(JSON.stringify(found) === JSON.stringify(wanted), `the home of the run holds ${JSON.stringify(found)}, not ${JSON.stringify(wanted)}`);
  });
}

// =====================================================================================
// Run B: the write against files the walk made (Claude, JSON)
// =====================================================================================

/** A settings file as another program might have left it: two other servers, three other keys, tabs, a text that is not ASCII, numbers written in several ways. */
const OTHER_JSON = '{\n\t"theme": "dark",\n\t"mcpServers": {\n\t\t"notes": {"command": "/usr/bin/notes", "args": ["--fast"], "env": {"TOKEN": "sk-MADE-UP-FOR-THIS-WALK"}},\n\t\t"mapa": {"command": "mapa", "args": [], "label": "Zażółć gęślą jaźń"}\n\t},\n\t"scale": 1.50,\n\t"recent": [1, 2.0, 1e3]\n}\n';

async function runB(browser) {
  const walk = await begin(browser, 'B');
  const page = walk.page;
  const en = (key, values) => worded(walk, 'en', key, values);
  let tile = null;
  let file = '';
  let folder = '';
  const copy = () => `${file}.before-${walk.entry}`;
  const last = () => `${file}.last-${walk.entry}`;
  /** Write the settings file as another program would have, some time ago. */
  const put = (text) => {
    fs.writeFileSync(file, text);
    aged(file);
  };
  /** Open the tile's sheet, do `body`, and see that the folder holds what it held. */
  const leavesFolder = async (state, body) => {
    const before = folderAsIs(folder);
    const shown = await pressTile(walk, 'claude', state);
    await body(shown);
    check(folderAsIs(folder) === before, 'the folder of the settings file does not hold what it held before');
  };

  await step('B: no settings folder: "not installed", with "Look again" the filled button and the text to paste a quiet link; the folder made, "Look again" offers', async () => {
    await toHome(walk);
    tile = await walk.tile('claude');
    file = walk.inHome(tile.path);
    folder = path.dirname(file);
    await pressTile(walk, 'claude', 'nofolder');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.nofolder.say.1', { label: tile.label, folder: path.dirname(tile.path) });
    await saysText(walk, 'dialog[open] #connect-say-2', 'connect.nofolder.say.2', { label: tile.label });
    const filled = (await page.locator('dialog[open] .btn-primary').allTextContents()).map(trimmed);
    check(JSON.stringify(filled) === JSON.stringify([en('connect.button.look')]), `the filled buttons are ${JSON.stringify(filled)}`);
    check((await page.locator('dialog[open] #connect-byhand.btn-quiet').count()) === 1, 'the text to paste is not a quiet link');
    check((await walk.count('dialog[open] #connect-go')) === 0, '"Connect" is offered where the program is not installed');
    check(JSON.stringify(walk.tree()) === '[]', `the look left something in the home: ${JSON.stringify(walk.tree())}`);
    await walk.picture('h20-sheet-not-installed');
    fs.mkdirSync(folder, { recursive: true });
    await page.locator('dialog[open] #connect-look').click();
    await sheetShows(walk, 'offer');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.new', { readers: tile.readers, path: tile.path });
    await closeWith(walk, 'connect-cancel');
    check(!fs.existsSync(file), 'a look or "Cancel" made the file');
    check((await walk.apps()).apps.length === 0, 'a look or "Cancel" registered an app');
  });

  await step('B: a file with two other servers and three other keys: after "Connect" everything else is equal, the bytes outside the entry are the old bytes, and the copy is the old file', async () => {
    put(OTHER_JSON);
    await pressTile(walk, 'claude', 'offer');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.add', { readers: tile.readers, path: tile.path });
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    await saysText(walk, 'dialog[open] #connect-copy', 'connect.done.status.copy', { copy: `${tile.path}.before-${walk.entry}` });
    await closeWith(walk, 'connect-done');
    check(fs.readFileSync(copy(), 'utf8') === OTHER_JSON, 'the first copy is not the file as it was');
    check((fs.statSync(copy()).mode & 0o777) === 0o600, `the copy has mode ${(fs.statSync(copy()).mode & 0o777).toString(8)}`);
    const compared = readSettings(options, 'compare', copy(), file, walk.entry);
    check(compared.equal_without_entry, 'without the entry the new file is not the old one');
    check(compared.old_piece === '' && compared.new_piece.includes(walk.entry), `the bytes differ by more than one piece put in: ${JSON.stringify(compared)}`);
    const written = entryIn(walk, file);
    check(JSON.stringify(written.servers.slice().sort()) === JSON.stringify(['mapa', 'notes', walk.entry].sort()), `the servers are ${JSON.stringify(written.servers)}`);
    check(JSON.stringify(written.keys) === JSON.stringify(['theme', 'mcpServers', 'scale', 'recent']), `the keys are ${JSON.stringify(written.keys)}`);
    check(!fs.existsSync(last()), 'a second copy was made at the first connect');
  });

  await step('B: "Disconnect" asks first, removes the entry and nothing else; "Connect" again: the first copy is still the first, the latest one the file from before', async () => {
    const withEntry = fs.readFileSync(file, 'utf8');
    aged(file);
    await pressTile(walk, 'claude', 'on');
    const mark = walk.sent.length;
    await page.locator('dialog[open] #connect-disconnect').click();
    const question = page.locator('dialog[open]').last();
    await says(question.locator('h2').first(), en('disconnect.title', { label: tile.label }), 'the second sheet does not ask');
    await says(question, (read) => read.includes(en('disconnect.say.1', { path: tile.path, label: tile.label })) && read.includes(en('disconnect.say.2', { readers: tile.readers })), 'the second sheet does not say what will be removed and to quit first');
    check(walk.changesSince(mark).length === 0, `before the answer the page sent ${walk.changesSince(mark).join(', ')}`);
    await walk.picture('h21-sheet-disconnect');
    await question.locator('.btn-danger').click();
    await walk.noSheet();
    await says(page.locator('#toasts .toast').last(), en('disconnect.result.done', { label: tile.label }), 'the message does not say that the app was disconnected');
    check(fs.readFileSync(file, 'utf8') === OTHER_JSON, 'after "Disconnect" the file is not the one from before the connect, byte for byte');
    check(fs.readFileSync(last(), 'utf8') === withEntry, 'the latest copy is not the file as it was with the entry');
    check((await walk.apps()).apps.length === 0, 'the app is still in the vault');
    await saysText(walk, '#tile-claude .tile-state', 'tile.status.connect');
    aged(file);
    await pressTile(walk, 'claude', 'offer');
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    await closeWith(walk, 'connect-done');
    check(fs.readFileSync(copy(), 'utf8') === OTHER_JSON, 'the first copy was replaced');
    check(fs.readFileSync(last(), 'utf8') === OTHER_JSON, 'the latest copy is not the file from before this connect');
  });

  await step('B: the entry removed behind the page\'s back: a press on the tile says that it is gone and offers to add it again', async () => {
    put(OTHER_JSON);
    await leavesFolder('offer', async () => {
      await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.gone', { readers: tile.readers, path: tile.path });
      // The app is registered: three buttons, "Connect" the filled one.
      const buttons = (await page.locator('dialog[open] .sheet-foot .btn').allTextContents()).map(trimmed);
      check(JSON.stringify(buttons) === JSON.stringify([en('connect.button.disconnect'), en('common.button.cancel'), en('connect.button.connect')]), `the buttons are ${JSON.stringify(buttons)}`);
      await closeWith(walk, 'connect-cancel');
    });
  });

  await step('B: an answer that says the file was written and could not be read back: the sheet says that the entry is in the file and that it was not read back, not "Written and read back", and no look is sent; a removal answered so says it in its message', async () => {
    // No walk-through can make the system refuse a read at that one moment. The word the app answers with then is put into its answer on the way to the page; everything else of the answer is the app's.
    const address = '**/api/connect/claude-desktop';
    await page.route(address, async (route) => {
      const response = await route.fetch();
      const answer = await response.json();
      await route.fulfill({ response, json: Object.assign(answer, { not_read_back: true }) });
    });
    await pressTile(walk, 'claude', 'offer');
    const mark = walk.sent.length;
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'on');
    await page.unroute(address);
    const sent = walk.changesSince(mark).filter((entry) => entry !== 'POST /api/interface');
    check(JSON.stringify(sent) === JSON.stringify(['POST /api/connect/claude-desktop']), `"Connect" sent ${sent.join(', ')}`);
    await saysText(walk, 'dialog[open] #connect-on-file', 'connect.on.status.file', { path: tile.path });
    check((await walk.count('dialog[open] #connect-result')) === 0, 'the sheet says "Written and read back" of a file that was not read back');
    await saysText(walk, 'dialog[open] #connect-unread', 'connect.status.unread');
    await says(page.locator('dialog[open] h2').first(), tile.label, 'the sheet is not the one of a connected app');
    check(entryIn(walk, file).servers.includes(walk.entry), 'the entry is not in the file');
    await closeWith(walk, 'connect-done');
    // A look reads the file: the sheet it draws says nothing of the kind.
    aged(file);
    await pressTile(walk, 'claude', 'on');
    check((await walk.count('dialog[open] #connect-unread')) === 0, 'the sheet of a file that was looked at says that it was not read back');
    // "Disconnect", answered in the same way: the message says that the app is disconnected, and what was not done.
    const removal = '**/api/connect/claude-desktop/remove';
    await page.route(removal, async (route) => {
      const response = await route.fetch();
      const answer = await response.json();
      await route.fulfill({ response, json: Object.assign(answer, { not_read_back: true }) });
    });
    await page.locator('dialog[open] #connect-disconnect').click();
    await page.locator('dialog[open]').last().locator('.btn-danger').click();
    await walk.noSheet();
    await page.unroute(removal);
    await says(page.locator('#toasts .toast').last(), `${en('disconnect.result.done', { label: tile.label })} ${en('connect.status.unread')}`, 'the message does not say that the file was not read back');
    check(!entryIn(walk, file).servers.includes(walk.entry), 'the entry is still in the file');
    check((await walk.apps()).apps.length === 0, 'the app is still in the vault');
    await walk.noToast();
  });

  await step('B: an entry that points elsewhere and holds "enabled": the offer says so; after "Connect" the other member is still there, and the sheet says the entry is switched off', async () => {
    const other = { mcpServers: { [walk.entry]: { command: '/usr/bin/other', args: ['serve'], enabled: false } }, theme: 'dark' };
    put(`${JSON.stringify(other, null, 2)}\n`);
    await pressTile(walk, 'claude', 'offer');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.replace', { readers: tile.readers, path: tile.path });
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    await closeWith(walk, 'connect-done');
    const written = entryIn(walk, file);
    check(written.entry.enabled === false && written.entry.command !== '/usr/bin/other' && written.entry.args.includes(walk.store), `the entry is ${JSON.stringify(written.entry)}`);
    check(JSON.stringify(Object.keys(written.entry)) === JSON.stringify(['command', 'args', 'enabled']), `the members of the entry are ${JSON.stringify(Object.keys(written.entry))}`);
    aged(file);
    await pressTile(walk, 'claude', 'on');
    await saysText(walk, 'dialog[open] #connect-on-off', 'connect.on.status.off', { label: tile.label });
    await says(page.locator('dialog[open] .sheet-facts'), (read) => read.includes(en('connect.on.status.more')), 'the sheet does not say that the entry holds more');
    await closeWith(walk, 'connect-done');
  });

  await step('B: "Disconnect" on an entry that points elsewhere: the file is as it was, the app is gone from the vault, and the message says that the entry was left', async () => {
    const other = { mcpServers: { [walk.entry]: { command: '/usr/bin/other', args: ['serve'] } } };
    put(`${JSON.stringify(other, null, 2)}\n`);
    const bytes = fs.readFileSync(file);
    await pressTile(walk, 'claude', 'offer');
    await page.locator('dialog[open] #connect-disconnect').click();
    const question = page.locator('dialog[open]').last();
    await says(question, (read) => read.includes(en('disconnect.say.1.vault', { label: tile.label })) && !read.includes(en('disconnect.say.2', { readers: tile.readers })), 'the second sheet does not say that the file is left as it is');
    await question.locator('.btn-danger').click();
    await walk.noSheet();
    await says(page.locator('#toasts .toast').last(), en('disconnect.result.differs', { label: tile.label, path: tile.path }), 'the message does not say that the entry was left');
    check(fs.readFileSync(file).equals(bytes), 'the file was changed');
    check((await walk.apps()).apps.length === 0, 'the app is still in the vault');
    await walk.noToast();
  });

  await step('B: a link in the file\'s place, a folder that is a link, and broken JSON are each refused in their own sentence, with the text to paste as the way on, and nothing is touched', async () => {
    // A link in the file's place.
    const target = path.join(walk.home, 'elsewhere.json');
    fs.writeFileSync(target, '{"mcpServers": {}}\n');
    fs.rmSync(file);
    fs.symlinkSync(target, file);
    await leavesFolder('refused', async () => {
      await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.not_read');
      await saysText(walk, 'dialog[open] #connect-say-2', 'connect.refused.say.2');
      const filled = (await page.locator('dialog[open] .btn-primary').allTextContents()).map(trimmed);
      check(JSON.stringify(filled) === JSON.stringify([en('connect.button.byhand')]), `the filled buttons are ${JSON.stringify(filled)}`);
      check((await walk.count('dialog[open] #connect-go')) === 0, '"Connect" is offered after a refusal');
      await closeWith(walk, 'connect-close');
    });
    check(fs.readFileSync(target, 'utf8') === '{"mcpServers": {}}\n', 'the file the link leads to was changed');
    fs.rmSync(file);
    fs.rmSync(target);
    // The folder a link: the sentence names where it leads, and nothing lies there afterwards.
    const kept = path.join(walk.home, 'kept-aside');
    const real = path.join(walk.home, 'real-folder');
    fs.renameSync(folder, kept);
    fs.mkdirSync(real);
    fs.symlinkSync(real, folder);
    await pressTile(walk, 'claude', 'refused');
    await says(page.locator('dialog[open] #connect-say-1'), (read) => read.includes(path.dirname(tile.path)) && read.includes('real-folder') && read === en('connect.refused.say.1.linked', { folder: path.dirname(tile.path), real: read.slice(read.indexOf(' is a link to ') + 14, read.indexOf(', and ')) }), 'the sheet does not name the folder and where it leads');
    await walk.picture('h22-sheet-refused');
    await closeWith(walk, 'connect-close');
    check(fs.readdirSync(real).length === 0, 'something lies in the folder the link leads to');
    fs.rmSync(folder);
    fs.rmdirSync(real);
    fs.renameSync(kept, folder);
    // Broken JSON.
    put('{"mcpServers": {"notes": {"command": "x"}}');
    await leavesFolder('refused', async () => {
      await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.unreadable', { path: tile.path, format: 'JSON' });
      await closeWith(walk, 'connect-close');
    });
  });

  await step('B: a file that was being written while it was looked at, and a linked folder of which the system does not say where it leads: each has a sentence of its own', async () => {
    // Neither moment can be made from outside: the word the app answers with then is put into its answer on the way to the page.
    put(OTHER_JSON);
    const before = folderAsIs(folder);
    const address = '**/api/connect/claude-desktop/look';
    const answering = async (more) => {
      await page.unroute(address).catch(() => {});
      await page.route(address, async (route) => {
        const response = await route.fetch();
        const answer = await response.json();
        await route.fulfill({ response, json: Object.assign(answer, more) });
      });
    };
    // Written while it was read: the sentence names the file, and the next thing is another look, not the text to paste.
    await answering({ state: 'busy' });
    await pressTile(walk, 'claude', 'refused');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.busy', { path: tile.path });
    check((await walk.count('dialog[open] #connect-say-2')) === 0, 'the sheet offers the text to paste for a file that is in order');
    check((await walk.count('dialog[open] #connect-go')) === 0, '"Connect" is offered after a refusal');
    let filled = (await page.locator('dialog[open] .btn-primary').allTextContents()).map(trimmed);
    check(JSON.stringify(filled) === JSON.stringify([en('connect.button.look')]), `the filled buttons are ${JSON.stringify(filled)}`);
    // "Look again", answered by the app alone: the file is in order and is offered.
    await page.unroute(address);
    await page.locator('dialog[open] #connect-look').click();
    await sheetShows(walk, 'offer');
    await closeWith(walk, 'connect-cancel');
    // A linked folder without a place: the sentence names the folder and no place, and does not read "a link to ,".
    await answering({ state: 'linked', real_folder: null });
    await pressTile(walk, 'claude', 'refused');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.linked.unknown', { folder: path.dirname(tile.path) });
    check(!(await walk.text('dialog[open] #connect-say-1')).includes(' to ,'), 'the sentence names a place that is not there');
    filled = (await page.locator('dialog[open] .btn-primary').allTextContents()).map(trimmed);
    check(JSON.stringify(filled) === JSON.stringify([en('connect.button.byhand')]), `the filled buttons are ${JSON.stringify(filled)}`);
    await page.unroute(address);
    await closeWith(walk, 'connect-close');
    check(folderAsIs(folder) === before, 'the folder of the settings file does not hold what it held before');
  });

  await step('B: the file changed after the sheet opened: nothing is written, "Look again"; the file changed a second ago: "changed a moment ago"', async () => {
    put(OTHER_JSON);
    await pressTile(walk, 'claude', 'offer');
    put('{}\n');
    const before = folderAsIs(folder);
    expectHttp(409, '/api/connect/claude-desktop');
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'refused');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.changed');
    check(folderAsIs(folder) === before, 'something was written although the file had changed');
    check((await walk.apps()).apps.length === 0, 'an app was registered although nothing was written');
    const filled = (await page.locator('dialog[open] .btn-primary').allTextContents()).map(trimmed);
    check(JSON.stringify(filled) === JSON.stringify([en('connect.button.look')]), `the filled buttons are ${JSON.stringify(filled)}`);
    await page.locator('dialog[open] #connect-look').click();
    await sheetShows(walk, 'offer');
    // Now the file is changed this very second.
    fs.writeFileSync(file, '{}\n');
    const now = folderAsIs(folder);
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'refused');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.recent', { path: tile.path });
    check((await walk.text('dialog[open] #connect-say-1')).includes('was changed a moment ago'), 'the sheet does not say "changed a moment ago"');
    check(folderAsIs(folder) === now, 'something was written into a file that changed a moment ago');
    check((await walk.apps()).apps.length === 0, 'an app was registered although nothing was written');
    await closeWith(walk, 'connect-close');
  });

  await step('B: after owner mode has run out, "Connect" and a "Disconnect" that changes the file ask for the passphrase; closed, nothing is written; answered, it is done', async () => {
    await restart(walk, { ownerSeconds: options.ownerSeconds });
    await unlock(walk);
    await page.locator('#home-title').waitFor({ state: 'attached', timeout: WAIT });
    const passed = async () => {
      const over = Date.parse((await walk.state()).owner_mode_until) + 1500;
      await eventually(() => Date.now() >= over, options.ownerSeconds * 1000 + WAIT, 'owner mode did not run out');
    };
    put(OTHER_JSON);
    await passed();
    const before = folderAsIs(folder);
    await pressTile(walk, 'claude', 'offer');
    expectHttp(403, '/api/connect/claude-desktop', { times: 2 });
    await page.locator('dialog[open] #connect-go').click();
    await page.locator('dialog[open] #reauth-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    await page.keyboard.press('Escape');
    await saysText(walk, 'dialog[open] #connect-error', 'common.error.cancelled');
    check(folderAsIs(folder) === before, 'something was written without the passphrase');
    check((await walk.apps()).apps.length === 0, 'an app was registered without the passphrase');
    await page.locator('dialog[open] #connect-go').click();
    await page.locator('dialog[open] #reauth-passphrase').fill(PASSPHRASE);
    await page.locator('dialog[open] #reauth-passphrase').press('Enter');
    await sheetShows(walk, 'connected');
    await closeWith(walk, 'connect-done');
    check(entryIn(walk, file).entry !== null, 'after the passphrase the entry is not in the file');
    // "Disconnect" changes the file here, so it asks too.
    await passed();
    aged(file);
    const held = folderAsIs(folder);
    await pressTile(walk, 'claude', 'on');
    expectHttp(403, '/api/connect/claude-desktop/remove', { times: 2 });
    await page.locator('dialog[open] #connect-disconnect').click();
    await page.locator('dialog[open]').last().locator('.btn-danger').click();
    await page.locator('dialog[open] #reauth-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    await page.keyboard.press('Escape');
    await sheetShows(walk, 'on');
    check(folderAsIs(folder) === held, 'the file was changed without the passphrase');
    check((await walk.apps()).apps.length === 1, 'the app was taken out of the vault without the passphrase');
    await page.locator('dialog[open] #connect-disconnect').click();
    await page.locator('dialog[open]').last().locator('.btn-danger').click();
    await page.locator('dialog[open] #reauth-passphrase').fill(PASSPHRASE);
    await page.locator('dialog[open] #reauth-passphrase').press('Enter');
    await walk.noSheet();
    check(entryIn(walk, file).entry === null && (await walk.apps()).apps.length === 0, 'after the passphrase the entry or the app is still there');
  });

  const name = path.relative(walk.home, file);
  await closing(walk, [`${path.dirname(name)}/`, name, `${name}.before-${walk.entry}`, `${name}.last-${walk.entry}`].concat(path.dirname(name).includes('/') ? walk.tree().filter((entry) => entry.endsWith('/') && path.dirname(name).startsWith(entry.slice(0, -1)) && entry !== `${path.dirname(name)}/`) : []));
}

// =====================================================================================
// Run C: ChatGPT and Grok (TOML), an entry under another name, CODEX_HOME, the fourth tile
// =====================================================================================

async function runC(browser) {
  const walk = await begin(browser, 'C');
  const page = walk.page;
  const en = (key, values) => worded(walk, 'en', key, values);
  let chatgpt = null;
  let grok = null;
  let codexFile = '';
  let grokFile = '';
  const GROK_BEFORE = '# Settings of another program\nsize = "large"   # a comment after a value\n\n[ui]\ntheme = "dark"\n\n[[profiles]]\nname = "work"\n';

  await step('C: the tile "Other app": a name and a receiver, the sentence of that app, then three texts, and "Copy" copies what is shown; no file is written', async () => {
    await toHome(walk);
    chatgpt = await walk.tile('chatgpt');
    grok = await walk.tile('grok');
    codexFile = walk.inHome(chatgpt.path);
    grokFile = walk.inHome(grok.path);
    await page.locator('#tile-custom').click();
    await page.locator('dialog[open] #custom-name').waitFor({ state: 'visible', timeout: WAIT });
    await says(page.locator('dialog[open] h2').first(), en('custom.title'), 'the sheet of the fourth tile is not headed "Other app (MCP)"');
    await saysText(walk, 'dialog[open] #custom-say-1', 'custom.say.1');
    await saysText(walk, 'dialog[open] #custom-say-2', 'docs.say.2.general');
    check(await page.locator('dialog[open] #custom-add').isDisabled(), '"Add" can be pressed before the app is described');
    await page.fill('dialog[open] #custom-name', 'codex');
    await page.fill('dialog[open] #custom-destination', 'OpenAI');
    await saysText(walk, 'dialog[open] #custom-say-2', 'custom.say.2', { name: 'codex', destination: 'OpenAI' });
    await walk.picture('h30-sheet-other-app');
    const mark = walk.sent.length;
    await page.locator('dialog[open] #custom-add').click();
    await page.locator('dialog[open] #custom-json-copy').waitFor({ state: 'visible', timeout: WAIT });
    check(JSON.stringify(walk.changesSince(mark)) === JSON.stringify(['POST /api/apps', 'POST /api/collections', 'POST /api/grants']), `"Add" sent ${walk.changesSince(mark).join(', ')}`);
    const labels = (await page.locator('dialog[open] .copy-label').allTextContents()).map(trimmed);
    check(JSON.stringify(labels) === JSON.stringify([en('custom.label.command'), en('custom.label.json'), en('custom.label.claudecode')]), `the three texts are ${JSON.stringify(labels)}`);
    for (const id of ['custom-command-copy', 'custom-json-copy', 'custom-line-copy']) {
      const block = page.locator('dialog[open] .copy-block', { has: page.locator(`#${id}`) });
      const shown = await block.locator('pre').textContent();
      check(shown.includes('codex') && shown.includes(walk.store), `the text beside ${id} is ${shown.slice(0, 120)}`);
      await pressCopy(page, options, page.locator(`dialog[open] #${id}`), shown);
    }
    await walk.picture('h31-sheet-other-app-texts');
    await closeWith(walk, 'custom-done');
    await saysText(walk, '#tile-custom .tile-state', 'tile.status.custom', { n: 1 });
    const app = (await walk.apps()).apps.find((one) => one.name === 'codex');
    check(app && app.hosted && app.destination === 'OpenAI' && JSON.stringify(app.grants) === JSON.stringify({ documents: 'approve' }), `the app is ${JSON.stringify(app)}`);
    check(JSON.stringify(walk.tree()) === '[]', `the fourth tile wrote into the home: ${JSON.stringify(walk.tree())}`);
  });

  await step('C: ChatGPT, an empty ~/.codex: the sentence names ChatGPT and Codex, and "Connect" makes a new file with one table', async () => {
    fs.mkdirSync(path.dirname(codexFile), { recursive: true });
    await pressTile(walk, 'chatgpt', 'offer');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.new', { readers: chatgpt.readers, path: chatgpt.path });
    check(JSON.stringify(chatgpt.readers) === JSON.stringify(['ChatGPT', 'Codex']), `the readers of the file are named ${JSON.stringify(chatgpt.readers)}`);
    check((await walk.text('dialog[open] #connect-say-1')).startsWith('Quit ChatGPT and Codex first'), 'the offer does not name ChatGPT and Codex');
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    await saysText(walk, 'dialog[open] #connect-result', 'connect.done.status.tried', { label: chatgpt.label });
    await closeWith(walk, 'connect-done');
    const written = entryIn(walk, codexFile);
    check(written.parses && JSON.stringify(written.keys) === JSON.stringify(['mcp_servers']) && JSON.stringify(written.servers) === JSON.stringify([walk.entry]), `the file holds ${JSON.stringify(written)}`);
    check(written.entry.args.includes('chatgpt-desktop') && written.entry.args.includes(walk.store) && written.mode === '0o600', `the entry is ${JSON.stringify(written.entry)}, mode ${written.mode}`);
  });

  await step('C: Grok, a file with other tables and comments: the block is appended and every old line is there; "Disconnect" removes its three lines and leaves the comments', async () => {
    fs.mkdirSync(path.dirname(grokFile), { recursive: true });
    fs.writeFileSync(grokFile, GROK_BEFORE);
    aged(grokFile);
    await pressTile(walk, 'grok', 'offer');
    await says(page.locator('dialog[open] h2').first(), en('connect.title', { label: grok.label, kind: en('connect.label.kind.terminal') }), 'the sheet does not say that Grok Build is a terminal program');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.add', { readers: grok.readers, path: grok.path });
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.done.say.1.session', { label: grok.label });
    await closeWith(walk, 'connect-done');
    const copy = `${grokFile}.before-${walk.entry}`;
    check(fs.readFileSync(copy, 'utf8') === GROK_BEFORE, 'the first copy is not the file as it was');
    const compared = readSettings(options, 'compare', copy, grokFile, walk.entry);
    check(compared.equal_without_entry && compared.old_lines_kept, `the new file is not the old one with a block added: ${JSON.stringify(compared)}`);
    check(fs.readFileSync(grokFile, 'utf8').startsWith(GROK_BEFORE), 'the block was not appended to the old text');
    const added = fs.readFileSync(grokFile, 'utf8').slice(GROK_BEFORE.length).split('\n').filter((line) => line.trim());
    check(added.length === 3 && added[0] === `[mcp_servers.${walk.entry}]` && added[1].startsWith('command = ') && added[2].startsWith('args = '), `the lines added are ${JSON.stringify(added)}`);
    aged(grokFile);
    await pressTile(walk, 'grok', 'on');
    await page.locator('dialog[open] #connect-disconnect').click();
    await page.locator('dialog[open]').last().locator('.btn-danger').click();
    await walk.noSheet();
    const after = fs.readFileSync(grokFile, 'utf8');
    check(readSettings(options, 'entry', grokFile, walk.entry).entry === null, 'the entry is still in the file');
    for (const line of GROK_BEFORE.split('\n')) check(after.split('\n').includes(line), `the line ${JSON.stringify(line)} is gone`);
    check(!after.includes(walk.entry), `the file still names the entry: ${after}`);
    await walk.noToast();
  });

  await step('C: a table of that name that points elsewhere, switched off, with a comment and a table below it: two lines are changed in their place, the rest stands, and the sheet says it is switched off', async () => {
    const before = `# my settings\nprofile = "work"\n\n[mcp_servers.${walk.entry}]\ncommand = "/usr/bin/other"\nargs = ["serve"]\nenabled = false\n# a comment inside the table\n\n[mcp_servers.${walk.entry}.tools.search]\napproval = "never"\n\n[other]\nx = 1\n`;
    fs.writeFileSync(codexFile, before);
    aged(codexFile);
    await pressTile(walk, 'chatgpt', 'offer');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.replace', { readers: chatgpt.readers, path: chatgpt.path });
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    await closeWith(walk, 'connect-done');
    const old = before.split('\n');
    const now = fs.readFileSync(codexFile, 'utf8').split('\n');
    check(now.length === old.length, `the file has ${now.length} lines where it had ${old.length}`);
    const changed = old.map((line, index) => (line === now[index] ? null : index)).filter((index) => index !== null);
    check(JSON.stringify(changed) === JSON.stringify([4, 5]) && now[4].startsWith('command = ') && now[5].startsWith('args = ') && now[5].includes('chatgpt-desktop'), `the lines that changed are ${JSON.stringify(changed.map((index) => now[index]))}`);
    aged(codexFile);
    await pressTile(walk, 'chatgpt', 'on');
    await saysText(walk, 'dialog[open] #connect-on-off', 'connect.on.status.off', { label: chatgpt.label });
    await says(page.locator('dialog[open] .sheet-facts'), (read) => read.includes(en('connect.on.status.more')), 'the sheet does not say that the entry holds more');
    await walk.picture('h32-sheet-switched-off');
    await closeWith(walk, 'connect-done');
  });

  await step('C: an entry made for "codex", with the app codex in the vault: "under another name", no "Connect", and the way to Settings', async () => {
    const made = fs.readFileSync(codexFile, 'utf8').replace('"chatgpt-desktop"', '"codex"');
    check(made.includes('"codex"'), 'the walk could not make the entry of codex');
    fs.writeFileSync(codexFile, made);
    aged(codexFile);
    const before = folderAsIs(path.dirname(codexFile));
    await pressTile(walk, 'chatgpt', 'as');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.as.say.1', { path: chatgpt.path, readers: chatgpt.readers, name: 'codex' });
    await saysText(walk, 'dialog[open] #connect-say-2', 'connect.as.say.2', { name: 'codex' });
    check((await walk.count('dialog[open] #connect-go')) === 0, '"Connect" is offered where the file connects the program under another name');
    check((await page.locator('dialog[open] #connect-settings').getAttribute('href')) === '#/pro/apps', 'the sheet does not lead to Settings');
    await closeWith(walk, 'connect-done');
    check(folderAsIs(path.dirname(codexFile)) === before, 'the folder does not hold what it held before');
  });

  await step('C: "mcp_servers" written as an inline table: "written in a way … does not change"; the text to paste is shown with the place of the file, and no line claims a place in it', async () => {
    fs.writeFileSync(grokFile, 'mcp_servers = { other = { command = "other" } }\n');
    aged(grokFile);
    const before = folderAsIs(path.dirname(grokFile));
    await pressTile(walk, 'grok', 'offer');
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'refused');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.by_hand', { path: grok.path });
    await saysText(walk, 'dialog[open] #connect-say-2', 'connect.refused.say.2');
    check(folderAsIs(path.dirname(grokFile)) === before, 'the file was changed');
    await page.locator('dialog[open] #connect-byhand').click();
    await sheetShows(walk, 'hand');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.hand.say.1', { path: grok.path });
    await saysText(walk, 'dialog[open] #connect-say-2', 'connect.offer.say.2', { destination: grok.destination });
    const registered = (await walk.apps()).apps.some((app) => app.name === grok.name);
    if (!registered) await page.locator('dialog[open] #connect-show').click();
    await page.locator('dialog[open] #connect-text-copy').waitFor({ state: 'visible', timeout: WAIT });
    const text = await page.locator('dialog[open] .copy-block pre').textContent();
    check(text.includes(`[mcp_servers.${walk.entry}]`) && text.includes('grok-build') && text.includes(walk.store), `the text to paste is ${text}`);
    check(trimmed(await page.locator('dialog[open] .copy-line pre').first().textContent()) === grok.path, 'the place of the file is not shown with "Copy"');
    check((await walk.count('dialog[open] #connect-where')) === 0, 'a line claims a place in a file the app cannot change');
    await walk.picture('h33-sheet-by-hand');
    await closeWith(walk, 'connect-close');
    check(folderAsIs(path.dirname(grokFile)) === before, 'the way by hand changed the file');
  });

  await step('C: started once more with CODEX_HOME set: the tile "ChatGPT" says why it writes nothing', async () => {
    await restart(walk, { env: { GATE_CODEX_HOME: path.join(walk.home, 'another-codex') } });
    await unlock(walk);
    await page.locator('#home-title').waitFor({ state: 'attached', timeout: WAIT });
    const before = folderAsIs(path.dirname(codexFile));
    await pressTile(walk, 'chatgpt', 'refused');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.moved', { readers: chatgpt.readers });
    check((await walk.count('dialog[open] #connect-go')) === 0, '"Connect" is offered while CODEX_HOME is set');
    await closeWith(walk, 'connect-close');
    check(folderAsIs(path.dirname(codexFile)) === before && !fs.existsSync(path.join(walk.home, 'another-codex')), 'something was written while CODEX_HOME was set');
  });

  const codex = path.relative(walk.home, codexFile);
  const grokName = path.relative(walk.home, grokFile);
  await closing(walk, ['.codex/', '.grok/', codex, `${codex}.last-${walk.entry}`, grokName, `${grokName}.before-${walk.entry}`, `${grokName}.last-${walk.entry}`]);
}

// =====================================================================================
// Run D: a vault set up on the six pages
// =====================================================================================

async function runD(browser, files) {
  const walk = await begin(browser, 'D', { args: ['--view', 'pro'] });
  const page = walk.page;
  const en = (key, values) => worded(walk, 'en', key, values);
  const post = (pathname, body) => apiPost(page, walk.base, pathname, body);
  let tile = null;
  let file = '';

  await step('D: started with "--view pro", "Create my vault" opens Overview; the vault gets two groups, an app on this computer and a pause; "← Home" shows the three cards with two of them done', async () => {
    await pair(walk);
    await create(walk);
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/pro/overview', `the address is ${walk.address()}`);
    // Set up as on the six pages, by a second window of the owner: the walk is about what Home says then.
    await post('/api/collections', { name: 'home' });
    await post('/api/collections', { name: 'tax' });
    await post('/api/documents?collection=home&name=flat-notes.txt', fs.readFileSync(files.flat));
    await post('/api/documents?collection=tax&name=boiler.md', fs.readFileSync(files.boiler));
    await post('/api/apps', { name: 'lm-studio', hosted: false });
    await post('/api/grants', { app: 'lm-studio', collection: 'home', mode: 'read' });
    await post('/api/apps', { name: 'chatgpt-desktop', hosted: false });
    await post('/api/serving', { paused: ['chatgpt-desktop'] });
    await page.locator('#pro-home').click();
    await page.locator(FRAME.simple).waitFor({ state: 'visible', timeout: WAIT });
    // The vault holds a document and an app, and nobody has seen a passage yet: the first and the third card are done.
    await page.locator('#first-title').waitFor({ state: 'visible', timeout: WAIT });
    // (What a second window changed is on this page at its next question to the app.)
    await eventually(async () => {
      const done = await page.locator('.first-card').evaluateAll((cards) => cards.map((card) => Boolean(card.querySelector('.first-num.is-done'))));
      check(JSON.stringify(done) === JSON.stringify([true, false, true]), `the cards that are done: ${JSON.stringify(done)}`);
      return true;
    });
    await saysText(walk, '#first-count', 'home.new.card1.status', { n: 2 });
    // Its sentence is about what Home gives: it is not said while an app holds "Can read".
    check((await walk.count('#first-say')) === 0, 'the sentence about "only after your yes" is said while an app can read without asking');
    await saysText(walk, '#tile-chatgpt .tile-state', 'tile.status.paused');
    await saysText(walk, '#tile-custom .tile-state', 'tile.status.custom', { n: 1 });
    tile = await walk.tile('claude');
    file = walk.inHome(tile.path);
    fs.mkdirSync(path.dirname(file), { recursive: true });
  });

  await step('D: the box: a document no app can ask for is said so; one an app holds "Can read" for names that app; with a passage seen the three cards go', async () => {
    await ask(walk, 'When was the boiler serviced?');
    await saysText(walk, '#trial-say-1', 'ask.say.1.none');
    await page.locator('#home-ask-title').waitFor({ state: 'visible', timeout: WAIT });
    check((await walk.count('#first-title')) === 0 && (await walk.state()).interface.setup === 'done', 'the three cards did not go by themselves');
    await says(page.locator('#trial-meta'), (read) => read.includes('boiler'), 'the answer is not from the document in the group no app has');
    await page.locator('#trial-no').click();
    await page.locator('#trial-again').click();
    await ask(walk, 'How much is the monthly rent?');
    await says(page.locator('#trial-say-1'), (read) => read.includes('lm-studio') && read === en('ask.say.1.read', { apps: 'lm-studio' }), 'the first sentence does not name the app that can read without asking');
    await walk.picture('h40-trial-can-read', { whole: true });
  });

  await connectStep('D: the offer says before the press that the app gets no group and that apps on this computer will wait for a choice; "Connect" then grants nothing and says so; Home shows the notice', async () => {
    await pressTile(walk, 'claude', 'offer');
    await saysText(walk, 'dialog[open] #connect-notice', 'notice.choice_ahead.say.1');
    await saysText(walk, 'dialog[open] #connect-say-2', 'connect.offer.say.2.groups', { label: tile.label, destination: tile.destination });
    await walk.picture('h41-sheet-offer-groups');
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    await says(page.locator('dialog[open] #connect-groups'), (read) => read.startsWith(en('connect.done.status.groups', { label: tile.label })), 'the sheet does not say that the app has no group yet');
    check((await page.locator('dialog[open] #connect-settings').getAttribute('href')) === '#/pro/apps', 'the sheet does not lead to Settings');
    await closeWith(walk, 'connect-done');
    const apps = await walk.apps();
    const app = apps.apps.find((one) => one.name === tile.name);
    check(app && JSON.stringify(app.grants) === '{}', `the app was given ${JSON.stringify(app && app.grants)}`);
    check(apps.collections.every((group) => group.allow_hosted !== true), `a group was opened to apps that send text to a provider: ${JSON.stringify(apps.collections)}`);
    check((await walk.state()).serving.choice_needed === true, 'the state does not say that a choice is needed');
    await saysText(walk, '#main .notices .notice-text', 'notice.choice_needed.say.1');
    check((await page.locator('#notice-choice').getAttribute('href')) === '#/pro/apps', 'the notice does not lead to Settings');
    await walk.picture('h42-home-notice', { whole: true });
  });

  await step('D: a name that is registered as an app on this computer: the sheet does not connect and leads to Settings', async () => {
    const before = walk.tree();
    await pressTile(walk, 'chatgpt', 'refused');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.refused.say.1.other_kind', { name: 'chatgpt-desktop' });
    check((await walk.count('dialog[open] #connect-go, dialog[open] #connect-byhand')) === 0, 'the sheet offers to connect or to paste');
    check((await page.locator('dialog[open] #connect-settings.btn-primary').getAttribute('href')) === '#/pro/apps', 'the filled button does not lead to Settings');
    await closeWith(walk, 'connect-close');
    check(JSON.stringify(walk.tree()) === JSON.stringify(before), 'the home was changed');
  });

  await step('D: Documents asks which group a chosen file goes to, with no group chosen; "Cancel" sends nothing', async () => {
    await openPage(page, 'documents');
    const mark = walk.sent.length;
    await chooseFiles(walk, page.locator('#main label.file-button').first(), [files.rules]);
    await page.locator('dialog[open] input[name="docs-group"]').first().waitFor({ state: 'attached', timeout: WAIT });
    check((await page.locator('dialog[open] input[name="docs-group"]').count()) === 2 && (await page.locator('dialog[open] input[name="docs-group"]:checked').count()) === 0, 'a group is chosen for the owner');
    await page.locator('dialog[open] button', { hasText: en('common.button.cancel') }).click();
    await walk.noSheet();
    check(walk.changesSince(mark).length === 0, `"Cancel" sent ${walk.changesSince(mark).join(', ')}`);
    await openPage(page, 'home');
  });

  await step('D: the vault opens on Overview, as it was made; "Open on" set to "Home": a restart opens Home; set back to "All settings": Overview', async () => {
    await openPage(page, 'settings');
    check(await page.locator('input[name="start"][value="pro"]').isChecked(), '"Open on" does not show what the vault was made with');
    await openOn(page, 'basic');
    await restart(walk);
    await unlock(walk);
    await page.locator(FRAME.simple).waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/home' || walk.address() === '#/settings', `after a restart the address is ${walk.address()}`);
    await openPage(page, 'settings');
    await openOn(page, 'pro');
    await page.goto(`${walk.base}#/`);
    await reload(page);
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/pro/overview' || walk.address() === '#/', `with "All settings" kept, a page without an address shows ${walk.address()}`);
  });

  const folders = walk.tree().filter((entry) => entry.endsWith('/'));
  await closing(walk, connects && file ? folders.concat([path.relative(walk.home, file)]) : folders);
}

// =====================================================================================
// Run E: the sample in Polish, five passages waiting, 400 px, the app stopped
// =====================================================================================

async function runE(browser, files) {
  const walk = await begin(browser, 'E');
  const page = walk.page;
  const pl = (key, values) => worded(walk, 'pl', key, values);
  let tile = null;
  let file = '';
  let written = null;

  await step('E: Polish chosen on the first screen; "Użyj przykładowego dokumentu" adds the sample, whose title says that it is invented, and one press on "Zapytaj" shows passage 2 of 3 and the words it was found by; three presses', async () => {
    await open(walk);
    await walk.counter.start();
    await walk.counter.aside(() => chooseLanguage(page, '.gate-lang', 'pl'));
    await pair(walk);
    await create(walk);
    await page.locator('#first-title').waitFor({ state: 'visible', timeout: WAIT });
    await saysText(walk, '#first-title', 'home.new.title', {}, 'pl');
    // Polish keeps a word of one letter on the line of the word after it: the page puts a no-break space there, in the text of the table.
    const firstSentence = (await page.locator('#first-say').textContent()) || '';
    check(firstSentence.includes('i\u00a0dopiero'), `the first sentence does not bind "i" to the word after it: ${JSON.stringify(firstSentence)}`);
    await page.locator('#first-sample').click();
    await page.locator('#first-sample-said').waitFor({ state: 'visible', timeout: WAIT });
    await saysText(walk, '#first-sample-said', 'home.new.result.sample', {}, 'pl');
    await eventually(() => focused(page.locator('#ask-input')), WAIT, 'the input of the box does not have the focus');
    check((await page.locator('#ask-input').inputValue()) === pl('ask.sample.question'), `the box holds "${await page.locator('#ask-input').inputValue()}"`);
    check((await walk.count('#first-sealed')) === 0, 'the page says of the sample that the owner\'s file is still where it was');
    await ask(walk, '', { typed: false });
    await page.locator('#trial').waitFor({ state: 'visible', timeout: WAIT });
    await walk.counter.stop();
    judge(walk, 'passage', 'to the first passage in the box, with the sample');
    check(JSON.stringify(walk.counter.typed) === JSON.stringify(TYPED.sample) && walk.counter.dialogs === 0, `fields ${JSON.stringify(walk.counter.typed)}, dialogs ${walk.counter.dialogs}`);
    const documents = (await walk.get('/api/documents')).collections.flatMap((group) => group.documents);
    check(documents.length === 1 && documents[0].title.includes('zmyślony przykład') && documents[0].passages === 3, `the vault holds ${JSON.stringify(documents.map((one) => [one.title, one.passages]))}`);
    await saysText(walk, '#trial-meta', 'ask.status.meta', { n: (await walk.text('#trial-meta')).match(/\d+/)[0], passage: 2, passages: 3, title: documents[0].title }, 'pl');
    await says(page.locator('#trial-words'), (read) => read.startsWith('Znaleziono po słowach: ') && read.length > 'Znaleziono po słowach: '.length, 'the card does not say in Polish by which words the passage was found');
    await saysText(walk, '#trial-say-1', 'ask.say.1.connect', {}, 'pl');
    await walk.picture('h50-home-sample-polish', { whole: true });
  });

  await step('E: a Polish question in another case form finds nothing, and the page says so with its example', async () => {
    await page.locator('#trial-no').click();
    await page.locator('#trial-again').click();
    await ask(walk, 'Ile kaucji zapłaciłem?');
    await saysText(walk, '#trial-none', 'ask.result.none', {}, 'pl');
  });

  await connectStep('E: the tile "Claude" and "Połącz": five presses to "Connected"; "Udostępnij raz" and a read through the written entry: six', async () => {
    tile = await walk.tile('claude');
    file = walk.inHome(tile.path);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    await walk.counter.start();
    await pressTile(walk, 'claude', 'offer');
    await saysText(walk, 'dialog[open] #connect-say-1', 'connect.offer.say.1.new', { readers: tile.readers, path: tile.path }, 'pl');
    await page.locator('dialog[open] #connect-go').click();
    await sheetShows(walk, 'connected');
    await walk.counter.stop();
    judge(walk, 'connected', 'to "Connected", with the sample');
    await saysText(walk, 'dialog[open] #connect-result', 'connect.done.status.tried', { label: tile.label }, 'pl');
    // The line the owner sends on to an AI app is the table's text as it stands: no no-break space is put into what is copied.
    const sentOn = (await page.locator('dialog[open] .sheet-block .copy-line code').first().textContent()) || '';
    check(sentOn.trim() === pl('connect.ask.start').trim() && !sentOn.includes('\u00a0'), `the line to copy is ${JSON.stringify(sentOn)}`);
    await walk.picture('h51-sheet-connected-polish');
    await escape(walk);
    written = entryIn(walk, file);
    const search = agentCallThrough(written.entry, 'search', 'umowa najmu kończy');
    const found = REF.exec(search.text);
    check(search.code === 'OK' && found, `search through the written entry: ${search.code} ${search.text.slice(0, 200)}`);
    const read = agentCallThrough(written.entry, 'read', `${found[1]}.${found[2]}`);
    check(read.code === 'NEEDS_APPROVAL', `the first read: ${read.code}`);
    await page.locator('.approval').first().waitFor({ state: 'visible', timeout: WAIT });
    await walk.counter.start();
    await releaseOnce(walk);
    await walk.counter.stop();
    const again = agentCallThrough(written.entry, 'read', `${found[1]}.${found[2]}`);
    check(again.code === 'OK' && again.released > 0, `the read after the release: ${again.code}`);
    judge(walk, 'read', 'to the first passage a caller reads through the written entry, with the sample');
    check(JSON.stringify(walk.counter.typed) === JSON.stringify(TYPED.sample) && walk.counter.dialogs === 0, `fields ${JSON.stringify(walk.counter.typed)}, dialogs ${walk.counter.dialogs}`);
  });

  await connectStep('E: five passages wait: three cards and "Pokaż jeszcze 2", with nothing filled', async () => {
    await apiPost(page, walk.base, '/api/documents?collection=documents&name=house-rules.txt', fs.readFileSync(files.rules));
    const refs = new Set();
    for (const query of ['kaucja najemca', 'czynsz miesięcznie', 'wypowiedzenie cisza nocna', 'quiet hours evening', 'small repairs tenant', 'written notice lease', 'service charges spring', 'liability insurance', 'pets consent', 'cellar compartment', 'windows winter mould', 'caretaker meter readings', 'monthly rent deposit']) {
      if (refs.size >= 5) break;
      const search = agentCallThrough(written.entry, 'search', query);
      for (const line of search.text.split('\n')) {
        const found = REF.exec(line);
        if (found && refs.size < 5 && !refs.has(`${found[1]}.${found[2]}`)) {
          const read = agentCallThrough(written.entry, 'read', `${found[1]}.${found[2]}`);
          if (read.code === 'NEEDS_APPROVAL') refs.add(`${found[1]}.${found[2]}`);
        }
      }
    }
    check(refs.size === 5, `only ${refs.size} passages could be made to wait`);
    await eventually(async () => (await walk.count('.approval')) === 3, WAIT, 'Home does not show three cards');
    await saysText(walk, '#home-more-approvals', 'home.wait.button.more', { n: 2 }, 'pl');
    check((await walk.text('#home-more-approvals')) === 'Pokaż jeszcze 2', `the button says "${await walk.text('#home-more-approvals')}"`);
    await saysText(walk, '#home-wait-count', 'home.wait.title', {}, 'pl').catch(() => {});
    check((await walk.text('#home-wait-count')) === '5', `the count beside "Czeka na Ciebie" is ${await walk.text('#home-wait-count')}`);
    check((await walk.count('#main .btn-primary')) === 0, 'a button of Home is filled while passages wait');
    await walk.picture('h52-home-five-waiting', { whole: true });
    await page.locator('#home-more-approvals').click();
    await eventually(async () => (await walk.count('.approval')) === 5, WAIT, '"Pokaż jeszcze 2" does not show all five');
    check((await walk.count('.approval button[data-key="release"].is-waiting')) > 0 || (await walk.count('.approval button[data-key="release"]')) === 5, 'the cards have no "Release once"');
  });

  await step('E: at 400 px no screen scrolls sideways, the bar with the four entries stays in view at the bottom, a tile\'s state has two lines at most, and a sheet is as wide as the window with its buttons under each other', async () => {
    const size = page.viewportSize();
    await page.setViewportSize({ width: 400, height: 800 });
    try {
      for (const name of ['home', 'documents', 'activity', 'settings']) {
        await openPage(page, name);
        const measured = await page.evaluate(() => {
          const bar = document.querySelector('nav.side-nav').getBoundingClientRect();
          return { wide: document.documentElement.scrollWidth, window: window.innerWidth, barBottom: Math.round(bar.bottom), barTop: Math.round(bar.top), height: window.innerHeight };
        });
        check(measured.wide <= measured.window, `at 400 px the page ${name} is ${measured.wide} px wide`);
        check(measured.barBottom <= measured.height && measured.barTop > measured.height / 2, `at 400 px the bar of the page ${name} lies at ${measured.barTop} to ${measured.barBottom} of ${measured.height}`);
        await walk.picture(`h53-400-${name}`);
      }
      // The details of an entry cover the page at this width, as they always did: the column makes room for them only in a wide window.
      await openPage(page, 'activity');
      if ((await walk.count('#main .row-open')) > 0) {
        const closed = await rowsAndPanel(walk);
        await page.locator('#main .row-open').first().click();
        await page.locator('aside.panel .panel-title').waitFor({ state: 'visible', timeout: WAIT });
        const open = await eventually(async () => {
          const now = await rowsAndPanel(walk);
          check(now.panel && now.panel.left === 0 && now.panel.width === now.window, `at 400 px the panel lies at ${JSON.stringify(now.panel)}`);
          return now;
        });
        check(JSON.stringify(open.rows) === JSON.stringify(closed.rows), 'at 400 px the rows moved when the details opened');
        await page.keyboard.press('Escape');
        await page.locator('aside.panel').waitFor({ state: 'detached', timeout: WAIT });
      } else if (connects) check(false, 'at 400 px the page Activity lists no row to open');
      await openPage(page, 'home');
      const lines = await page.locator('.tile .tile-state').evaluateAll((nodes) => nodes.map((node) => Math.round(node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight))));
      check(lines.every((count) => count <= 2), `the states of the tiles have ${JSON.stringify(lines)} lines`);
      // A sheet with three buttons: an app that is registered whose entry is gone, or the form of the fourth tile with its two.
      if (connects) {
        fs.writeFileSync(file, '{}\n');
        aged(file);
        await pressTile(walk, 'claude', 'offer');
      } else {
        await page.locator('#tile-custom').click();
        await page.locator('dialog[open] #custom-name').waitFor({ state: 'visible', timeout: WAIT });
      }
      const measure = () =>
        page.locator('dialog[open]').evaluate((node) => {
          const box = node.getBoundingClientRect();
          const buttons = Array.from(node.querySelectorAll('.sheet-foot .btn')).map((button) => button.getBoundingClientRect());
          return { left: Math.round(box.left), width: Math.round(box.width), window: window.innerWidth, scroll: node.scrollWidth - node.clientWidth, tops: buttons.map((button) => Math.round(button.top)), lefts: buttons.map((button) => Math.round(button.left)), widths: buttons.map((button) => Math.round(button.width)) };
        });
      // The sheet rises into its place: it is measured once it has come to rest there.
      const sheet = await eventually(async () => {
        const now = await measure();
        check(now.width >= now.window - 2 && now.left <= 1, `at 400 px the sheet is ${now.width} px wide at ${now.left}`);
        return now;
      });
      check(sheet.scroll <= 0, `at 400 px the sheet scrolls sideways by ${sheet.scroll} px`);
      if (connects) {
        // Under each other, the filled one on top: where the thumb reaches last, the eye reaches first.
        check(sheet.tops.length === 3 && new Set(sheet.tops).size === 3 && sheet.tops[2] < sheet.tops[1] && sheet.tops[1] < sheet.tops[0], `the three buttons of the sheet are at ${JSON.stringify(sheet.tops)}`);
        check(new Set(sheet.lefts).size === 1 && new Set(sheet.widths).size === 1, `the three buttons are not under each other: ${JSON.stringify(sheet)}`);
      }
      await walk.picture('h54-400-sheet');
      await escape(walk);
    } finally {
      await page.setViewportSize(size);
    }
  });

  await step('E: the app stopped under Home: the bar says so in Polish, with the command that starts it again', async () => {
    await leftNothingBehind(walk);
    const exit = await stopApp(walk.app, 'SIGINT');
    check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
    check(walk.app.err === '', `standard error: ${walk.app.err}`);
    await page.locator('.offline-bar').waitFor({ state: 'visible', timeout: 40000 });
    await says(page.locator('.offline-bar'), (read) => read.includes(pl('bar.say.1')) && read.includes(pl('bar.say.2')), 'the bar does not say in Polish that the app is not answering');
  });

  const folders = walk.tree().filter((entry) => entry.endsWith('/'));
  await closing(walk, connects && file ? folders.concat([path.relative(walk.home, file)]) : folders);
}

// =====================================================================================
// The runs
// =====================================================================================

run(async () => {
  deadline(20);
  const asked = canConnect(options);
  if (!asked.ok) {
    console.log(`##### NOTHING CAN BE CONNECTED ON THIS MACHINE. ${asked.line}`);
    if (process.env.GITHUB_ACTIONS) console.log(`::error title=The walk-through cannot connect::${asked.line}`);
    if (process.env.GATE_NO_CONNECT !== 'skip') {
      console.log('FAIL  the walk-through of Home needs "Connect" to write. Set GATE_NO_CONNECT=skip to walk what is left without it.');
      process.exit(1);
    }
    console.log('##### GATE_NO_CONNECT=skip: every step that connects is left out, and the runs B and C are not started.');
    connects = false;
  } else console.log(`${asked.line}: the app writes entries for this Python and for vaults under ${runDirectory()}`);
  const files = writeFiles(path.join(shortDirectory('gf-'), 'files'));
  const browser = await openBrowser(options, 'h');
  const runs = { A: runA, B: runB, C: runC, D: runD, E: runE };
  for (const name of RUNS) {
    if (!runs[name]) continue;
    if (!connects && (name === 'B' || name === 'C')) {
      console.log(`SKIP  run ${name} is not started  (##### nothing can be connected on this machine, see the line at the start)`);
      continue;
    }
    await runs[name](browser, files);
  }
  for (const figure of counted) console.log(`presses, run ${figure.run}, ${figure.which}: ${figure.presses}`);
  await finish(browser);
});
