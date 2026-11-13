// The first run as the page offers it, and the Basic view, walked in a browser against the real app.
//
// Run it from the top of the repository (after "npm install --no-save playwright@1.63.0"
// and "npx playwright install chromium"):
//
//   GATE_PYTHON=.venv/bin/python node tests/browser/journey_basic.js
//
// Options (environment), all optional:
//   GATE_PYTHON         the Python that has wdva installed (default: python3)
//   GATE_BROWSER        chromium (default), webkit or firefox
//   GATE_SHOTS          where the screenshots go (default: shots/ next to this file)
//   GATE_SCALE          device scale factor of the screenshots (default: 1)
//   GATE_PORT           port of the interface of the first vault (default: the app picks a free one)
//   GATE_OWNER_SECONDS  how long owner mode lasts after the last restart of run A, where the walk waits
//                       for its end (default: 6). Everywhere else it has the app's own value, 600.
//   GATE_RUNS           which of the five runs to walk, for example "A,D" (default: all of them)
//
// Five runs, each with a new vault in an empty directory with a short path under /tmp:
//   A  the first run as offered, for Claude Desktop: the app, a document, the first question with the
//      look at the settings file, the first request, the first approval; then the Basic page on an
//      ordinary day, with a passage waiting, its sheet for an app, locked; the switch to Pro and back
//      across two restarts; the passphrase asked for again; the app stopped.
//   B  skipping: every step left by "Skip", by a reload, by a lock; the empty Basic page and its
//      line about setting up; "Hide"; the app stopped under a step.
//   C  the invented sample letter: a reload, a lock and a restart in the middle of the first run;
//      "Don't release" as an ending; the letter named on the Basic page and removed.
//   D  a vault made in Pro, opened in Basic: what Basic says of every setting it has no control for,
//      and that it sends nothing but questions; a visit to Pro and back; "Stay in Pro".
//   E  the first run for Claude Code; a lock under its last step and the way back to that step from
//      the Basic page; a busy day: five passages waiting.
//
// Runs A and E count what the offered path costs a person (presses, addresses, typed fields, file
// dialogs) from "Create the vault" to the first request seen on the page, and fail above the numbers
// the first run was designed to. See COUNT below for the numbers, and for the one that is higher
// than the design and why.
//
// The app is started through launch_app.py, which changes one constant (how long owner mode lasts)
// and gives the app an empty home of its own. "Check the file" therefore looks into a home this walk
// owns: the settings file of an AI app that it finds there is one the walk put there. Calls of an AI
// app are made by agent_call.py through RemoteGate, the path the program an app starts uses.
//
// Every step prints "ok" or "FAIL". Exit status 1 when a step failed, the page logged an error, a
// request failed, or an HTTP error came that no step announced.

'use strict';

const fs = require('fs');
const path = require('path');
const {
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
  finish,
  focused,
  lockElsewhere,
  newPage,
  note,
  openBrowser,
  pageReachedApp,
  pressCopy,
  pressCounter,
  readOptions,
  reload,
  run,
  shortDirectory,
  shot,
  startApp,
  started,
  step,
  stopApp,
  switchView,
  withdraw,
} = require('./common');

const options = readOptions({ ownerSeconds: 6 });
const RUNS = (process.env.GATE_RUNS || 'A,B,C,D,E').toUpperCase().split(/[\s,]+/).filter(Boolean);
/** How long owner mode lasts wherever the walk does not wait for its end: the app's own value. */
const OWNER_SECONDS = 600;
const PASSPHRASE = 'correct horse battery staple';
const DESKTOP = 'claude-desktop'; // an app with one settings file, which the page can look at
const CODE = 'claude-code'; // an app that is told with one command line
const LOCAL = 'lm-studio'; // an app the owner declares local, in Pro
const REF = /ref=(d-[0-9a-f]{12})\.(\d+)/;
/** How long "Release once" stays inert after the cards appeared, changed or moved (ARM_MS in static/views.js). */
const ARM_MS = 1500;

/**
 * What the offered path may cost, from "Create the vault" to the first request of the owner's own
 * app seen on the page, with one document. The first run was designed to these numbers: the walk
 * fails above them, so that a change which makes the first run longer is seen.
 *
 * For Claude Desktop the design counts 7 presses, with one line for a terminal that opens the
 * settings file and puts the text on the clipboard. That line is on the page only once it has been
 * tried against the real applications, which has not been done. In the wording that is built
 * instead, the place of the file and the text are two things to copy where the line would be one:
 * 8 presses. (The design names the same one press more for a computer where such a line cannot
 * fill the clipboard.) When the line is on the page, this number goes to 7.
 *
 * Not counted, here or in the design: the second look at the settings file ("Check again"), which
 * only says whether it worked.
 */
const COUNT = {
  [DESKTOP]: { presses: 8, addresses: ['#/setup/app', '#/setup/documents', '#/setup/connect'], typed: 0, dialogs: 1 },
  [CODE]: { presses: 6, addresses: ['#/setup/app', '#/setup/documents', '#/setup/connect'], typed: 0, dialogs: 1 },
};
/** From the first request to the first released passage, in addition: one press ("Release once") on the screen the owner is on. */
const FIRST_RELEASE = { presses: 1, screens: 0 };

/** The words of the interface this walk reads. Everything else is found by role, id, key or class. */
const TEXT = {
  steps: ['Passphrase', 'App', 'Documents', 'First question'],
  apps: ['Claude Desktop', 'Claude Code', 'ChatGPT desktop app', 'Codex', 'OpenClaw', 'Another app'],
  chooseAnApp: 'Choose an app',
  general: 'Text that an app receives from your documents is sent to the company behind the app and cannot be recalled.',
  sentence: (name, destination) => `Text that ${name} receives from your documents is sent to ${destination} and cannot be recalled.`,
  listed: (name) => `It will be listed under the name ${name}. The name is a label you give this connection.`,
  letAsk: (name) => `Let ${name} ask me each time`,
  cannotBeRead: 'A scan, a photo or a Word file cannot be read.',
  canThen: (name) => `${name} can then search it and ask for passages; each passage waits for your approval.`,
  untilYouAddOne: 'No app can ask for it until you add one.',
  notAdded: /^Not added: /,
  noneAdded: 'None of those files could be added. You can go on with an invented sample letter and add your own documents later.',
  whoAsks: (name) => `Apps that may use your documents can ask for it from now on: ${name} (Ask me each time).`,
  whoReads: (name) => `Apps that may use your documents can ask for it from now on: ${name} (Can read: passages of it can leave without asking).`,
  notEncrypted: /not encrypted/i,
  sampleAdded: 'Added: an invented sample letter (1 passage). Its text says that it is invented. You can remove it at any time.',
  waitingFor: (name) => `Waiting for the first request under the name ${name}. This page notices it by itself.`,
  arrived: (name) => new RegExp(`^A first request arrived under the name ${name} (yesterday )?at \\d.*\\. It says it is journey-agent 1\\.0, unverified\\.$`),
  notTried: (label) => `It has not been tried with ${label} itself.`,
  outside: 'Nothing is changed for you: you copy the text and put it where the app reads it.',
  looksAt: (product) => `${product} looks at that one file and tells this page only whether its own entry is there. Then this page says what to do.`,
  noFolder: (label) => `The settings folder of ${label} is not on this computer: `,
  installed: (label) => `Is ${label} installed? Open it once, then check again.`,
  notThere: 'The file is not there yet.',
  makeIt: 'Make this file in a plain-text editor:',
  putItIn: 'Put the text below into it and save it.',
  isIn: (product, label) => `✓ ${product} is in the settings file of ${label}.`,
  otherSettings: 'The file holds other settings. Keep them.',
  typeAComma: 'Inside “mcpServers”, after its last entry, type a comma, then paste this:',
  notRead: (product) => `${product} could not read the file as JSON, so it cannot say what is in it. A comma or a bracket may be missing or doubled.`,
  pointsElsewhere: /^The file already has an entry named “[^”]+”, and it points somewhere else: another vault, or an earlier installation\.$/,
  replaceIt: 'Replace that entry with this one:',
  askStart: (product) => `Use ${product} to search my documents and answer this: `,
  askSample: (product) => `Use ${product} to search my documents for the sample rental agreement. When does it end?`,
  pasteAsItIs: 'Paste it into a new chat as it is.',
  searchAnswer: 'A search answer says which passages match. It holds no passage text.',
  whenItAsks: 'When it asks for a passage, the exact text appears here, under “Approvals”.',
  itAsks: 'It asks for a passage. This is the exact text.',
  approvedOnce: (label) => `Approved once. Go back to ${label} and tell it: “try again”. It can read this passage until `,
  releasedAfter: /^Released after your approval (yesterday )?at \d/,
  wentTo: (title, destination) => new RegExp(`^Passage 1 of 3 of “${title}” went to ${destination}: [\\d,]+ characters\\. It is in the record with its exact text\\.$`),
  notReleased: 'Not released',
  nothingLeft: (name) => `Nothing from that passage left. If ${name} asks again, the passage waits here again.`,
  openAgain: (product) => `To open ${product} again after you quit it or restart this computer, run this in Terminal:`,
  itPrints: 'It prints the address of a page. That page asks for a new pairing code from that terminal, then for your passphrase.',
  cannotTakeBack: /cannot take it back/i,
  cannotBeRecalled: /cannot be recalled/i,
  nothingWaits: 'Nothing is waiting for you',
  oneWaits: '1 passage is waiting for you',
  waits: (count) => `${count} passages are waiting for you`,
  whenAnAppAsks: 'When an app asks for a passage that needs your approval, it appears here with the exact text. That passage is not released until you say so.',
  tryAgain: 'After you release a passage, the app gets it when it asks again. If it does not ask by itself, tell it: “try again”.',
  noApp: 'No app has been added, so no app gets an answer.',
  nameIsALabel: (product) => `A name is a label: ${product} cannot check which program is really asking.`,
  recordChecked: 'The record of what left was checked when you unlocked.',
  cannotDo: (product) => `What ${product} cannot do`,
  viewWords: 'Basic shows what you use every day; Pro shows every setting. Both show the same vault.',
  nothingYet: 'Nothing has left this vault yet.',
  nothingInADay: 'Nothing left in the last 24 hours.',
  noDocuments: 'No documents yet.',
  oneDocument: '1 document, sealed in the vault.',
  documents: (count) => `${count} documents, sealed in the vault.`,
  sampleIsIn: '1 of them is the invented sample letter.',
  finishSetup: 'Finish setting up',
  done: (count) => `${count} of 4 done`,
  nextApp: 'Next: choose your AI app.',
  nextDocument: 'Next: add a document.',
  nextQuestion: 'Next: ask your AI app a first question.',
  setupHidden: 'Setup is hidden. Adding an app, adding documents and the connection steps stay on this page.',
  askMe: 'Ask me each time',
  canRead: 'Can read',
  noAccess: 'No access',
  nothingChanged: 'Nothing was changed.',
  lockedFoot: 'While locked, apps get one answer: the vault is locked. The number and sizes of the sealed files are visible without the key; what is in them is not.',
  oneWaited: '1 passage was waiting when the vault locked. It was not released. If an app still needs one, it will ask again after you unlock.',
  beforeUnlock: /^Before you unlocked (yesterday )?at \d/,
  nothingSince: /^Nothing has left since you unlocked (yesterday )?at \d/,
  notAnswering: (product) => `${product} is not answering. Is it still running in its terminal? What you see may be out of date.`,
  startAgain: 'To start it again, run this in Terminal:',
  continuesByItself: 'This page continues by itself.',
  printsNewPage: 'It prints the address of a new page. Open that one; this one can then be closed.',
  notUpToDate: /^This page could not be brought up to date: .+ Trying again\.$/,
  visit: 'You are in Pro, where every setting is. Your view stays Basic.',
  backToBasic: '← Back to Basic',
  stayInPro: 'Stay in Pro',
  setInPro: 'Parts of this vault were set up in Pro. Basic shows them and does not change them.',
  addToWhich: 'Add to which group?',
  appsInPro: 'Apps are added in Pro for this vault: it has several groups of documents, or an app that runs on this computer.',
  protocol: /MCP|Model Context Protocol|:\/\//, // what no screen of setting up shows: the protocol's name, an address
};

/** Fragments of what an AI app is told. These texts are fixed in wdva.gate.messages. */
const AGENT = { refsOnly: 'search returns refs only', withheld: 'text withheld' };

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
  'The balcony carries flower boxes on its inner side only. Barbecues with charcoal are not allowed; an electric grill is, until ten in the evening. Laundry is dried below the height of the railing, and nothing is shaken out over it.',
  'Subletting a room needs consent, which is not refused without a reason. Subletting the whole flat is allowed for at most six months and only once in three years, and the tenant stays liable for the rent and for any damage.',
  'The flat is handed back swept, with all keys, with holes in the walls filled and with the meter readings written down by both parties. Walls painted in strong colours are painted white again; pale colours may stay as they are.',
  'Parcels left with a neighbour are fetched within three days. The letter boxes are emptied daily, and a full letter box during an absence of more than two weeks is the tenant\'s matter to arrange with a neighbour or the caretaker.',
];

/** The files the owner adds in these runs. `flat-notes.txt` gives three passages, `house-rules.txt` more than five. */
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

/** The run that was begun last. */
let lastWalk = null;

/**
 * One vault, one app, one page: what a run works with. The app is started at once on an empty
 * directory; `pair` waits for it.
 */
async function begin(browser, work, name, { args = [], ownerSeconds = OWNER_SECONDS, port = 0 } = {}) {
  // A run that failed half way may have left its page open and its app running: neither belongs to the next run.
  if (lastWalk) {
    if (!lastWalk.page.isClosed()) await closePage(lastWalk.page).catch(() => {});
    await stopApp(lastWalk.app, 'SIGINT');
  }
  const store = path.join(work, name, 'vault');
  fs.mkdirSync(path.dirname(store));
  const walk = { name, store, base: '', product: '', code: '', ownerSeconds };
  lastWalk = walk;
  walk.app = startApp(options, { args: ['--store', store, ...args], ownerSeconds, port });
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
  walk.agent = (client, ...rest) => agentCall(options, store, client, ...rest);
  walk.count = (selector) => page.locator(selector).count();
  walk.text = async (selector) => trimmed(await page.locator(selector).first().textContent({ timeout: WAIT }));
  walk.address = () => new URL(page.url()).hash;
  walk.place = () => walk.address().split('?')[0];
  walk.card = (key) => page.locator(`.gate-card[data-key="${key}"]`);
  walk.sheetWith = (selector) => page.locator('dialog[open]', { has: page.locator(selector) });
  walk.noSheet = () => eventually(async () => (await walk.count('dialog[open]')) === 0, WAIT, 'a sheet stayed open');
  walk.noToast = () => eventually(async () => (await walk.count('#toasts .toast')) === 0, WAIT, 'a message stayed up');
  /** What the page sent since `mark` that was not a question: "POST /api/grants", in order. */
  walk.changesSince = (mark) => walk.sent.slice(mark).filter((entry) => entry.method !== 'GET').map((entry) => `${entry.method} ${entry.path}`);
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

/** Wait for the app, open its page and pair it with the code the terminal shows. */
async function pair(walk) {
  const lines = await started(walk.app);
  walk.base = walk.app.url;
  const found = /^pairing code: ([A-Z0-9]{4}-[A-Z0-9]{4})\s/.exec(lines[3]);
  check(found, `pairing line: ${lines[3]}`);
  walk.code = found[1];
  await walk.page.goto(walk.base);
  await walk.page.locator('#pair-code').waitFor({ state: 'visible', timeout: WAIT });
  walk.product = await walk.page.locator('meta[name="application-name"]').getAttribute('content');
  await walk.page.fill('#pair-code', walk.code);
  await walk.page.locator('.gate-card button[type="submit"]').click();
}

/** Type the new passphrase twice and press "Create the vault". What opens then is for the caller to wait for. */
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

/**
 * Stop the app with Ctrl-C and start it again on the same port, where the page that is still open
 * asks. The page is told that its session is gone and asks for the new code, which is given.
 * What opens then is the locked screen.
 */
async function restart(walk, { ownerSeconds = walk.ownerSeconds, whileDown = null } = {}) {
  const page = walk.page;
  const exit = await stopApp(walk.app, 'SIGINT');
  check(exit && exit.code === 0, `the app ended with ${JSON.stringify(exit)}`);
  check(walk.app.err === '', `standard error: ${walk.app.err}`);
  if (whileDown) await whileDown();
  const before = walk.code;
  walk.ownerSeconds = ownerSeconds;
  walk.app = startApp(options, { args: ['--store', walk.store], ownerSeconds, port: new URL(walk.base).port, home: walk.app.home });
  const lines = await started(walk.app);
  check(walk.app.url === walk.base, `the app came back at ${walk.app.url}, not at ${walk.base}`);
  const found = /^pairing code: (\S+)\s/.exec(lines[3]);
  check(found && found[1] !== before, `the new run has no code of its own: ${lines[3]}`);
  walk.code = found[1];
  const announced = expectHttp(401, '/api/state', { times: 5, optional: true });
  await page.locator('#pair-code').waitFor({ state: 'visible', timeout: 40000 });
  pageReachedApp();
  withdraw(announced);
  await page.fill('#pair-code', walk.code);
  await page.locator('.gate-card button[type="submit"]').click();
  await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
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

/**
 * Wait until the text of an element is what is wanted: a text it must equal, a pattern it must
 * match, or a test it must pass. When it never is, the step fails with what was read last.
 */
async function says(locator, wanted, about) {
  await eventually(async () => {
    const read = trimmed(await locator.textContent({ timeout: 2000 }));
    const met = typeof wanted === 'string' ? read === wanted : wanted instanceof RegExp ? wanted.test(read) : wanted(read);
    check(met, `${about}: ${read}`);
    return true;
  });
}

/** The labels of the stepper that is on screen, in order; none when it is not drawn. */
async function stepperLabels(walk) {
  return (await walk.page.locator('ol.steps .step-label').allTextContents()).map((label) => label.replace(' (done)', '').trim());
}

/** The stepper in one line: a part that carries a check as "+", the one that is on screen as ">", any other as "-". */
function stepperMarks(walk) {
  return walk.page.locator('ol.steps li.step').evaluateAll((nodes) => nodes.map((node) => (node.classList.contains('is-current') ? '>' : node.classList.contains('is-done') ? '+' : '-')).join(''));
}

/** Where an element begins on the page, whatever the window shows of it at the moment. */
function topOf(locator) {
  return locator.evaluate((node) => Math.round(node.getBoundingClientRect().top + window.scrollY));
}

/**
 * "Release once" on `card` must wait again after `cause`: it shows that it waits, and it does not
 * stop waiting sooner than a second and a half after the cause began. With `press`, the pointer
 * presses the button the moment it waits: nothing may be released by that.
 */
async function waitsAgain(walk, card, what, cause, { press = false } = {}) {
  const page = walk.page;
  const ready = card.locator('button[data-key="release"]:not(.is-waiting)');
  const waiting = card.locator('button[data-key="release"].is-waiting');
  const armed = (words) =>
    ready.waitFor({ state: 'visible', timeout: WAIT }).catch(() => {
      throw new Error(words);
    });
  await armed(`before ${what} "Release once" was still waiting`);
  const before = await walk.approvals();
  const began = Date.now();
  await cause();
  await waiting.waitFor({ state: 'visible', timeout: WAIT }).catch(() => {
    throw new Error(`after ${what} "Release once" did not wait`);
  });
  let pressed = false;
  if (press) {
    const box = await waiting.boundingBox();
    if (box) {
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      // The press counts as made in time only when the page had it well inside the wait.
      pressed = Date.now() - began < ARM_MS - 500;
    }
    if (!pressed) note(`the press on a waiting "Release once" after ${what} came too late on this machine to say anything, and was not judged`);
  }
  await armed(`after ${what} "Release once" went on waiting`);
  const took = Date.now() - began;
  check(took >= ARM_MS - 50, `after ${what} "Release once" could be pressed again after ${took} ms`);
  if (pressed) {
    const after = await walk.approvals();
    check(after.granted.length === before.granted.length && after.waiting.length === before.waiting.length, `a press on "Release once" counted while it waited after ${what}`);
  }
}

/** Check what the offered path cost against what it may cost, and say it. */
function judgeCount(walk, app) {
  const { presses, addresses, typed, dialogs } = walk.counter;
  const limit = COUNT[app];
  console.log(`count ${app}: ${presses.length} presses, ${addresses.length} addresses, ${typed.length} typed fields, ${dialogs} file dialog(s), from "Create the vault" to the first request seen on the page`);
  presses.forEach((press, index) => console.log(`      ${index + 1}. ${press}`));
  check(presses.length <= limit.presses, `the offered path took ${presses.length} presses, more than ${limit.presses}: ${presses.join(' | ')}`);
  check(JSON.stringify(addresses) === JSON.stringify(limit.addresses), `the offered path led through ${JSON.stringify(addresses)}, not through ${JSON.stringify(limit.addresses)}`);
  check(typed.length <= limit.typed, `the offered path had the owner type into ${JSON.stringify(typed)}`);
  check(dialogs <= limit.dialogs, `the offered path opened ${dialogs} file dialogs`);
  if (app === DESKTOP && presses.length > 7) note('Claude Desktop: 8 presses where the design of the first run counts 7. The eighth is the second "Copy": the one line for a terminal that would carry both the place and the text is not on the page until it has been tried against the real applications');
}

/** Press "Choose files" as a person does, and answer the dialog of the system with `files`. */
async function chooseFiles(walk, scope, files) {
  const chooser = walk.page.waitForEvent('filechooser', { timeout: WAIT });
  chooser.catch(() => {});
  await scope.locator('label.file-button').click();
  await (await chooser).setFiles(files);
}

// =====================================================================================
// Run A: the first run as offered, then Basic
// =====================================================================================

async function runA(browser, work, files) {
  const walk = await begin(browser, work, 'a', { port: options.port });
  const { page, counter } = walk;
  const key = (ref) => `ap-${DESKTOP}-${ref}`;
  let known = null; // the entry of the list of apps for Claude Desktop
  let settingsFile = ''; // where the look of this run finds the settings file of Claude Desktop
  let shownPath = ''; // that place as the page shows it
  let block = ''; // the text the page gives for that file
  let doc = ''; // the document the owner added: the first part of its references
  let firstRead = null; // the first passage that left: what the app was given

  const looks = () => walk.sent.filter((entry) => entry.method === 'POST' && entry.path === `/api/apps/${DESKTOP}/connect/check`).length;
  const stepCard = walk.card('step-4');
  const found = stepCard.locator('#ask-found');
  /** Press the button that looks at the file, and wait for the look to be answered. */
  const look = async () => {
    const before = looks();
    const kept = fs.existsSync(settingsFile) ? fs.readFileSync(settingsFile, 'utf8') : null;
    const answered = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/apps/${DESKTOP}/connect/check`, { timeout: WAIT });
    answered.catch(() => {});
    await stepCard.locator('#ask-check').click();
    const answer = await (await answered).json();
    await found.waitFor({ state: 'visible', timeout: WAIT });
    check(looks() === before + 1, `one press looked at the file ${looks() - before} times`);
    // The look reads and never writes: the file is what it was, or still is not there.
    const now = fs.existsSync(settingsFile) ? fs.readFileSync(settingsFile, 'utf8') : null;
    check(now === kept, 'the look changed the settings file');
    return answer;
  };
  const foundSays = (words) => says(found, words, 'about the file the step says');

  await step('A: the terminal shows the start-up lines, the page pairs, and "Create the vault" opens the step that adds an app', async () => {
    await pair(walk);
    if (options.port) check(walk.app.port === options.port, `the app listens on ${walk.app.port}, not on ${options.port}`);
    await page.locator('#new-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    check(JSON.stringify(await stepperLabels(walk)) === JSON.stringify(TEXT.steps), `the parts of setting up are ${JSON.stringify(await stepperLabels(walk))}`);
    await create(walk);
    await page.locator('form.gate-card[data-key="step-3"]').waitFor({ state: 'visible', timeout: 40000 });
    // From here on everything a person does on the page is counted, until the first request is seen.
    await counter.start();
    check(walk.address() === '#/setup/app', `after "Create the vault" the address is ${walk.address()}`);
    check(JSON.stringify(await stepperLabels(walk)) === JSON.stringify(TEXT.steps) && (await stepperMarks(walk)) === '+>--', `on the step the parts of setting up are ${JSON.stringify(await stepperLabels(walk))}, marked ${await stepperMarks(walk)}`);
    const now = await walk.state();
    check(now.locked === false && now.interface.view === 'basic' && now.interface.setup === 'open' && now.interface.stored === true, `a new vault starts as ${JSON.stringify(now.interface)}`);
    // Nothing exists yet but the vault: no group of documents, no app.
    check((await walk.get('/api/documents')).collections.length === 0, 'a new vault has a collection already');
    check((await walk.apps()).apps.length === 0, 'a new vault has an app already');
  });
  // Without the app there is nothing to walk.
  if (!walk.base) return;

  await step('A: the app step ticks nothing and asks for no access; choosing Claude Desktop says its name, its sentence, and gives "Ask me each time"', async () => {
    const form = page.locator('form.gate-card[data-key="step-3"]');
    const rows = form.locator('input[name="first-app-known"]');
    const add = form.locator('#first-add');
    await rows.first().waitFor({ state: 'attached', timeout: WAIT });
    const listed = (await walk.apps()).known.filter((entry) => entry.local_program);
    known = listed.find((entry) => entry.id === DESKTOP);
    check(JSON.stringify(await rows.evaluateAll((nodes) => nodes.map((node) => node.value))) === JSON.stringify(listed.map((entry) => entry.id)), 'the apps offered are not the ones that can be connected');
    check(JSON.stringify((await form.locator('.choice-label').allTextContents()).map(trimmed)) === JSON.stringify(TEXT.apps), `the rows read ${JSON.stringify(await form.locator('.choice-label').allTextContents())}`);
    check((await rows.count()) === 6 && (await form.locator('input[name="first-app-known"]:checked').count()) === 0, 'a row is ticked before the owner chose one');
    check((await walk.count('input[name="first-mode"]')) === 0, 'the first run asks what the app may do');
    check((await add.isDisabled()) && trimmed(await add.textContent()) === TEXT.chooseAnApp, `before a choice the button reads "${trimmed(await add.textContent())}"`);
    check((await walk.text('#first-sentence')) === TEXT.general, `before a choice the form says: ${await walk.text('#first-sentence')}`);
    check(trimmed(await form.locator('#first-rule strong').textContent()) === 'Ask me each time.', 'the rule does not begin with the access it gives');
    await eventually(() => focused(form.locator('#setup-title')), WAIT, 'the step does not begin with the focus on its title');
    check((await walk.count('aside.sidebar')) === 0 && (await walk.count('input[name="view"]')) === 0 && (await walk.count('a#setup-pro')) === 1, 'the step shows the sidebar or the view control, or lacks the way to Pro');
    check(!TEXT.protocol.test(await form.innerText()), 'the step names the protocol or the address of a page');
    await walk.picture('b01-setup-app', { whole: true });

    await form.locator('label.choice', { has: page.locator(`input[value="${DESKTOP}"]`) }).click();
    await eventually(() => form.locator(`input[name="first-app-known"][value="${DESKTOP}"]`).isChecked(), WAIT, 'the app was not picked');
    check((await walk.text('#first-listed')) === TEXT.listed(DESKTOP), `under the list the form says: ${await walk.text('#first-listed')}`);
    check((await walk.text('#first-sentence')) === TEXT.sentence(DESKTOP, known.destination), `about the app the form says: ${await walk.text('#first-sentence')}`);
    check(!(await add.isDisabled()) && trimmed(await add.textContent()) === TEXT.letAsk(DESKTOP), `the button that adds the app reads "${trimmed(await add.textContent())}"`);
    await walk.picture('b01b-setup-app-chosen', { whole: true });

    const mark = walk.sent.length;
    await add.click();
    await walk.card('step-2').waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/setup/documents', `after the app the address is ${walk.address()}`);
    // The press sent the three changes in this order, and the access is "Ask me each time".
    check(JSON.stringify(walk.changesSince(mark)) === JSON.stringify(['POST /api/apps', 'POST /api/collections', 'POST /api/grants']), `the press sent ${JSON.stringify(walk.changesSince(mark))}`);
    const grant = walk.sent.slice(mark).find((entry) => entry.path === '/api/grants');
    if (grant.body) check(JSON.parse(grant.body).app === DESKTOP && JSON.parse(grant.body).mode === 'approve', `the access that was asked for: ${grant.body}`);
    else note(`${options.browser} does not show a test what the page sent with a request, so the access is checked in the vault only`);
    const apps = await walk.apps();
    check(apps.apps.length === 1 && apps.apps[0].name === DESKTOP && apps.apps[0].grants.documents === 'approve', `apps: ${JSON.stringify(apps.apps.map((entry) => [entry.name, entry.grants]))}`);
    check(apps.collections.length === 1 && apps.collections[0].allow_hosted === true, 'the one group of documents was not opened to apps that send text to a provider');
  });

  await step('A: the documents step says who will be able to ask; a Word file is refused with a reason, a text file is sealed', async () => {
    const card = walk.card('step-2');
    const next = card.locator('.gate-actions .btn-primary');
    const leads = (await card.locator('.gate-lead').allTextContents()).map(trimmed);
    check(leads[0].endsWith(TEXT.cannotBeRead) && leads.includes(TEXT.canThen(DESKTOP)), `the step begins: ${JSON.stringify(leads)}`);
    check((await walk.text('#docs-sentence')) === TEXT.sentence(DESKTOP, known.destination), `where text goes is said as: ${await walk.text('#docs-sentence')}`);
    check(await next.isDisabled(), 'the step can be continued without a document');
    check((await stepperMarks(walk)) === '++>-', `the stepper of the step is marked ${await stepperMarks(walk)}`);
    check((await card.locator('label.file-button.btn-primary').count()) === 1, '"Choose files" is not the filled button of the step');
    check((await card.locator('.drop.drop-large[data-key="drop-documents"]').count()) === 1, 'the step has no drop zone of its own');

    // A file the vault cannot read is not on the path that is counted: it is handed to the page without a press.
    await counter.aside(async () => {
      await page.setInputFiles('#files-documents', files.word);
      const refused = card.locator('li.upload.upload-error .upload-note');
      await refused.waitFor({ state: 'visible', timeout: WAIT });
      check(TEXT.notAdded.test(trimmed(await refused.textContent())), `the Word file is answered: ${await refused.textContent()}`);
      check((await walk.text('#docs-refused')) === TEXT.noneAdded, `after the refusal the step says: ${await walk.text('#docs-refused')}`);
      check((await card.locator('#sample-add.btn-primary').count()) === 1 && (await card.locator('label.file-button.btn-primary').count()) === 0, 'after the refusal the sample is not the one filled button');
      check((await walk.state()).encryption.documents === 0, 'the refused file was added');
    });

    await chooseFiles(walk, card, files.flat);
    await eventually(async () => (await card.locator('li.upload.upload-done').count()) === 1, 60000, 'the file was not sealed');
    const added = (await walk.get('/api/documents')).collections[0];
    check(added.name === 'documents' && added.documents.length === 1 && added.documents[0].passages === 3, `after the first file the vault holds: ${JSON.stringify(added)}`);
    check(/\b3 passages\b/.test(await card.locator('.upload.upload-done .upload-note').textContent()), 'the page does not say how many passages were sealed');
    check((await walk.text('p.upload-who')) === TEXT.whoAsks(DESKTOP), `after the file the page says: ${await walk.text('p.upload-who')}`);
    check((await walk.count('.upload-outside')) === 1 && TEXT.notEncrypted.test(await walk.text('.upload-outside')), 'what is true of the chosen file is not said exactly once');
    check((await walk.count('#docs-refused')) === 0, 'the step still says that no file could be added');
    check(trimmed(await next.textContent()) === 'Continue with 1 document' && !(await next.isDisabled()), `the button reads "${trimmed(await next.textContent())}"`);
    await walk.picture('b02-setup-documents', { whole: true });
    await next.click();
    await stepCard.waitFor({ state: 'visible', timeout: WAIT });
    check(walk.place() === '#/setup/connect', `after the document the address is ${walk.address()}`);
  });

  await step('A: the step "First question" waits under the stored name, in three lines, and has looked at no file', async () => {
    await stepCard.locator('li.ask-step[data-key="ask-3"]').waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === `#/setup/connect?app=${DESKTOP}`, `the address does not name the app: ${walk.address()}`);
    const keys = await stepCard.locator('li.ask-step').evaluateAll((nodes) => nodes.map((node) => node.dataset.key));
    check(keys.join(',') === 'ask-1,ask-2,ask-3', `the lines of the step are ${keys.join(',')}`);
    check((await walk.text('p.ask-wait.ask-waiting')) === TEXT.waitingFor(DESKTOP), `while nothing has come the step says: ${await walk.text('p.ask-wait')}`);
    check(!TEXT.protocol.test(await stepCard.innerText()), 'the step names the protocol or the address of a page');
    const first = trimmed(await stepCard.locator('li.ask-step[data-key="ask-1"]').textContent());
    check(first.includes(TEXT.outside) && first.includes(TEXT.looksAt(walk.product)) && first.endsWith(TEXT.notTried(known.label)), `the first line reads: ${first}`);
    // An amber note at the head of the first line would mean that the command in the text cannot start.
    check((await stepCard.locator('#ask-ready').count()) === 0, `the step warns: ${first}`);
    check(looks() === 0, 'the settings file was looked at before the owner pressed for it');
    // One place for the file, the one of this computer, and nothing else of the window around the step.
    const places = await stepCard.locator('li.ask-step[data-key="ask-1"] .ask-where .path').allTextContents();
    check(places.length === 1 && places[0].startsWith('~/') && !places[0].includes('\\'), `the place of the settings file is shown as ${JSON.stringify(places)}`);
    shownPath = places[0];
    settingsFile = path.join(walk.app.home, shownPath.slice(2));
    check((await stepCard.locator('#ask-check.btn-primary').count()) === 1, '"Check the file" is not the filled button before a look');
    check((await walk.count('#ask-help')) === 1 && trimmed(await page.locator('#ask-later').textContent()) === 'Finish later', 'the step lacks "Nothing arrives?" or "Finish later"');
    check((await walk.count('aside.sidebar')) === 0 && (await walk.count('input[name="view"]')) === 0 && (await walk.count('header.topbar')) === 0, 'the step is not alone on the screen');
    check((await stepperMarks(walk)) === '+++>', `the stepper of the step is marked ${await stepperMarks(walk)}`);
    await walk.picture('b03-first-question', { whole: true });
  });

  await step('A: "Check the file" says what it found, and the text put where the page says is found by "Check again"', async () => {
    // Claude Desktop is installed in this home (its settings folder is there) and has no settings file yet.
    fs.mkdirSync(path.dirname(settingsFile), { recursive: true });
    const answer = await look();
    check(answer.state === 'no_file' && answer.path === shownPath, `the look answered ${JSON.stringify(answer)}`);
    await foundSays(TEXT.notThere);
    const abc = stepCard.locator('ol.ask-abc > li');
    check((await abc.count()) === 3, `after the look the step has ${await abc.count()} things to do`);
    check(trimmed(await abc.nth(0).locator('p').first().textContent()) === TEXT.makeIt && trimmed(await abc.nth(0).locator('.ask-path .path').textContent()) === shownPath, `first: ${trimmed(await abc.nth(0).textContent())}`);
    check(trimmed(await abc.nth(1).locator('p').first().textContent()) === TEXT.putItIn, `second: ${trimmed(await abc.nth(1).locator('p').first().textContent())}`);
    check(trimmed(await abc.nth(2).locator('#ask-check').textContent()) === 'Check again' && (await stepCard.locator('#ask-check.btn-primary').count()) === 0, 'third is not a plain "Check again"');
    block = await abc.nth(1).locator('pre.code').textContent();
    const server = Object.values(JSON.parse(block).mcpServers)[0];
    check(server.args.join(' ').includes(`gate mcp --client ${DESKTOP} --store ${walk.store}`), `the text does not name this vault: ${block}`);
    check(fs.existsSync(server.command), `the command is not a file: ${server.command}`);
    check(!block.includes(PASSPHRASE), 'the text holds the passphrase');
    check(!TEXT.protocol.test(await stepCard.locator('li.ask-step[data-key="ask-1"]').innerText()), 'the first line names the protocol or the address of a page');
    // The two things a person copies: the place of the file, and the text.
    await pressCopy(page, options, stepCard.locator('#ask-copy-path'), shownPath);
    await pressCopy(page, options, stepCard.locator('#ask-copy-text'), block);
    await walk.picture('b03b-file-not-there', { whole: true });

    // What follows is the look that says whether it worked, and looks at files a person might have:
    // neither is on the path that is counted. What the person does in the editor is done here: the file, with the text in it.
    await counter.aside(async () => {
      fs.writeFileSync(settingsFile, block);
      check((await look()).state === 'present', 'the text of the page, put where the page says, is not found');
      await foundSays(TEXT.isIn(walk.product, known.label));
      check((await stepCard.locator('li.ask-step.is-done[data-key="ask-1"]').count()) === 1, 'the first line does not carry a check once the entry is there');
      await walk.picture('b03c-file-has-the-entry', { whole: true });

      // A file that holds other settings, with entries of other tools.
      const others = { theme: 'dark', mcpServers: { calendar: { command: '/usr/bin/true', args: ['--calendar'] } } };
      fs.writeFileSync(settingsFile, `${JSON.stringify(others, null, 2)}\n`);
      const other = await look();
      check(other.state === 'other' && other.section === 'filled', `a file with other settings is answered ${JSON.stringify(other)}`);
      await foundSays(TEXT.otherSettings);
      check(trimmed(await abc.nth(1).locator('p').first().textContent()) === TEXT.typeAComma, `with other entries the step says: ${trimmed(await abc.nth(1).locator('p').first().textContent())}`);
      await walk.picture('b03d-file-other-settings', { whole: true });
      // Done as the page says, the file keeps what it held and gains the entry.
      const entry = await abc.nth(1).locator('pre.code').textContent();
      fs.writeFileSync(settingsFile, `{"theme": "dark", "mcpServers": {"calendar": {"command": "/usr/bin/true", "args": ["--calendar"]}, ${entry}}}\n`);
      check((await look()).state === 'present', 'the entry alone, put where the page says, is not found');
      check(JSON.parse(fs.readFileSync(settingsFile, 'utf8')).mcpServers.calendar.command === '/usr/bin/true', 'the walk lost the other entry');

      // A file that cannot be read as what it should be.
      fs.writeFileSync(settingsFile, '{"mcpServers": {');
      check((await look()).state === 'unreadable', 'a broken file is not told apart');
      await foundSays(TEXT.notRead(walk.product));

      // The entry is there, and it points at another vault.
      const elsewhere = JSON.parse(block);
      const slug = Object.keys(elsewhere.mcpServers)[0];
      elsewhere.mcpServers[slug].args = elsewhere.mcpServers[slug].args.map((part) => (part === walk.store ? '/somewhere/else/vault' : part));
      fs.writeFileSync(settingsFile, JSON.stringify(elsewhere));
      check((await look()).state === 'differs', 'an entry that points elsewhere is not told apart');
      await foundSays(TEXT.pointsElsewhere);
      check(trimmed(await found.textContent()).includes(`“${slug}”`), 'the step does not name the entry');
      check(trimmed(await abc.nth(1).locator('p').first().textContent()) === TEXT.replaceIt, `then the step says: ${trimmed(await abc.nth(1).locator('p').first().textContent())}`);

      // Left as a person leaves it who did what the page said.
      fs.writeFileSync(settingsFile, block);
      check((await look()).state === 'present', 'the file as the page wants it is not found');
    });
  });

  await step('A: the start of the question is copied, and a reload shows the same step at the same address', async () => {
    const line = stepCard.locator('li.ask-step[data-key="ask-2"]');
    check((await line.locator('pre.code').textContent()) === TEXT.askStart(walk.product), `the question begins: ${JSON.stringify(await line.locator('pre.code').textContent())}`);
    await pressCopy(page, options, line.locator('#ask-copy'), TEXT.askStart(walk.product));
    const before = { address: walk.address(), looks: looks() };
    await reload(page);
    await stepCard.locator('li.ask-step[data-key="ask-3"]').waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === before.address, `after a reload the address is ${walk.address()}, not ${before.address}`);
    check((await walk.text('p.ask-wait.ask-waiting')) === TEXT.waitingFor(DESKTOP), 'after a reload the step does not wait any more');
    // What a look found was true of its moment: the page keeps none of it, and looks at nothing by itself.
    check((await walk.count('#ask-found')) === 0 && (await stepCard.locator('#ask-check.btn-primary').count()) === 1, 'a reload kept what the last look found');
    check(looks() === before.looks, 'a reload looked at the settings file');
  });

  await step('A: the first request arrives on this screen under the stored name and ends setting up; the path cost no more than it may', async () => {
    const first = walk.agent(DESKTOP, 'status');
    check(first.code === 'OK' && first.text.includes(`documents (${AGENT.refsOnly}`), `status: ${first.text}`);
    const arrived = stepCard.locator('p.ask-wait.ask-arrived');
    await arrived.waitFor({ state: 'visible', timeout: WAIT });
    check(TEXT.arrived(DESKTOP).test(trimmed(await arrived.textContent())), `about the first request the step says: ${trimmed(await arrived.textContent())}`);
    // The first request is on the screen: the count ends here.
    await counter.stop();
    judgeCount(walk, DESKTOP);

    // The check beside the line is drawn in the colour of the text: green is the colour of what is sealed.
    const colours = await arrived.evaluate((node) => [getComputedStyle(node.querySelector('.ask-wait-glyph svg') || node.querySelector('.ask-wait-glyph')).color, getComputedStyle(node.querySelector('.ask-wait-text')).color]);
    check(colours[0] === colours[1], `the check beside the line has a colour of its own: ${colours.join(' / ')}`);
    // The three lines have done their work, the request stands under them, and a way on is offered.
    await eventually(async () => (await stepCard.locator('ol.ask-steps').count()) === 0, WAIT, 'the three lines are still unfolded');
    check((await page.locator('#ask-steps-toggle').getAttribute('aria-expanded')) === 'false', 'the folded lines have no button that says so');
    await stepCard.locator('ul.ask-rows .act-row').first().waitFor({ state: 'visible', timeout: WAIT });
    check((await walk.text('#ask-end-title')) === TEXT.whenItAsks, `after the first request the step says: ${await walk.text('#ask-end-title')}`);
    check((await stepCard.locator('.ask-end #ask-open.btn-primary').count()) === 1, '"Open my vault" is not the filled way on');
    await eventually(async () => (await walk.state()).interface.setup === 'done', WAIT, 'the first request did not end setting up');
    // The stepper was there when the step opened and stays: nothing above the cards comes or goes.
    check((await stepperLabels(walk)).length === 4, 'the stepper went when setting up ended');

    const search = walk.agent(DESKTOP, 'search', 'monthly rent deposit', 'documents');
    check(search.code === 'OK' && search.text.includes(AGENT.withheld), `search: ${search.text}`);
    doc = REF.exec(search.text)[1];
    await eventually(async () => (await stepCard.locator('ul.ask-rows .act-row').count()) === 2, WAIT, 'the search is not listed under the question');
    await stepCard.locator('p.setup-aside', { hasText: TEXT.searchAnswer }).waitFor({ state: 'visible', timeout: WAIT });
    // Rows on this screen open nothing: they are not buttons.
    check((await stepCard.locator('ul.ask-rows button').count()) === 0, 'a row of the step is a button');
    await walk.picture('b04-first-request', { whole: true });
  });

  await step('A: a passage waits on this screen under "Approvals", above the rows; released once, the app reads it and the step says what left', async () => {
    const ref = `${doc}.1`;
    // A second count: what the first released passage costs in addition, on the page.
    counter.reset();
    await counter.start();
    // The page itself notes when the card of this passage appears, whether its "Release once" waits then, and for how long.
    const watched = page.evaluate(
      (cardKey) =>
        new Promise((resolve) => {
          let first = null;
          let observer = null;
          const look = () => {
            const button = document.querySelector(`.approval[data-key="${cardKey}"] button[data-key="release"]`);
            if (!button) return;
            const waiting = button.classList.contains('is-waiting');
            if (!first) first = { at: performance.now(), waiting };
            if (waiting) return;
            if (observer) observer.disconnect();
            resolve({ waited: first.waiting, ms: Math.round(performance.now() - first.at) });
          };
          observer = new MutationObserver(look);
          observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
          look();
        }),
      key(ref),
    );
    watched.catch(() => {});
    const refused = walk.agent(DESKTOP, 'read', ref);
    check(refused.code === 'NEEDS_APPROVAL' && refused.text.includes('Approvals'), `read: ${JSON.stringify(refused)}`);
    const section = stepCard.locator('section[data-key="ask-approvals"]');
    const card = section.locator(`.approval-stack > .approval[data-key="${key(ref)}"]`);
    await card.waitFor({ state: 'visible', timeout: WAIT });
    // It has only just appeared: "Release once" waits from the start, and not for less than a second and a half.
    await card.locator('button[data-key="release"]:not(.is-waiting)').waitFor({ state: 'visible', timeout: WAIT });
    const wait = await watched;
    check(wait.waited, 'when the card of the first passage appeared, "Release once" could be pressed at once');
    check(wait.ms >= ARM_MS - 50, `"Release once" could be pressed ${wait.ms} ms after the card of the first passage appeared`);
    check(trimmed(await section.locator('p.basic-label').textContent()) === 'Approvals' && trimmed(await section.locator('h2.ask-heading').textContent()) === TEXT.itAsks, `above the card: ${trimmed(await section.textContent()).slice(0, 80)}`);
    const waiting = (await walk.approvals()).waiting.find((item) => item.ref === ref);
    check(waiting && waiting.mode === 'approve' && waiting.hosted === true, `the passage waits as ${JSON.stringify(waiting && { mode: waiting.mode, hosted: waiting.hosted })}`);
    check((await card.locator('pre.released').textContent()) === waiting.body && waiting.body.includes('The monthly rent is 1,180 EUR'), 'the card shows other text than would be released');
    check(TEXT.cannotBeRecalled.test(await card.locator('.approval-sentence').textContent()) && (await card.locator('.approval-head').textContent()).includes(known.destination), 'the card does not say where the text goes');
    // "Don't release" comes first, and neither answer is the loud one; nor is any other button of the screen.
    const answers = await card.locator('.approval-actions button').evaluateAll((nodes) => nodes.map((node) => node.dataset.key));
    check(answers.join(',') === 'dismiss,release', `the answers of the card are ${answers.join(',')}`);
    check((await walk.count('.btn-primary')) === 0, 'a button is filled while a passage waits');
    const [cardBox, rowsBox] = [await card.boundingBox(), await stepCard.locator('ul.ask-rows').boundingBox()];
    check(cardBox.y + cardBox.height <= rowsBox.y, `the card does not stand above the rows: ${JSON.stringify([cardBox, rowsBox])}`);
    check((await page.title()).startsWith('(1) '), `the tab is called "${await page.title()}"`);
    await walk.picture('b05-first-approval', { whole: true });

    await decideApproval(page, key(ref), 'release');
    const end = stepCard.locator('.ask-end');
    await says(end.locator('#ask-end-title'), (text) => text.startsWith(TEXT.approvedOnce(known.label)), 'after the release the step says');
    check(trimmed(await end.textContent()).includes('Waiting for it to read the passage.'), 'the step does not say that it waits for the app');
    check((await section.locator('.granted-list li').count()) === 1, 'the approval is not listed as approved and not read');
    await walk.picture('b05b-approved-once', { whole: true });

    // The owner tells the app "try again".
    firstRead = walk.agent(DESKTOP, 'read', ref);
    check(firstRead.code === 'OK' && firstRead.text.includes('The monthly rent is 1,180 EUR'), `read after approval: ${JSON.stringify(firstRead)}`);
    await says(end.locator('h2#ask-end-title'), TEXT.releasedAfter, 'after the app read the passage the step says');
    const title = (await walk.get('/api/documents')).collections[0].documents[0].title;
    check(TEXT.wentTo(title, known.destination).test(trimmed(await end.locator('p').first().textContent())), `about what left the step says: ${trimmed(await end.locator('p').first().textContent())}`);
    check((await end.locator('#ask-open.btn-primary').count()) === 1 && (await end.locator('#ask-own').count()) === 0, 'the way on is not one filled "Open my vault"');
    // How the app is opened again on another day: the command this installation was started with.
    const launch = end.locator('.launch');
    const command = await launch.locator('pre.code').textContent();
    check(command === (await walk.state()).launch.command && command.includes(' gate app') && command.includes(`--store ${walk.store}`), `the command that opens the app again: ${command}`);
    const around = (await launch.locator('p').allTextContents()).map(trimmed);
    check(around[0] === TEXT.openAgain(walk.product) && around[1] === TEXT.itPrints, `around the command the step says: ${JSON.stringify(around)}`);
    check((await page.title()) === walk.product, `with nothing waiting the tab is called "${await page.title()}"`);
    await counter.stop();
    console.log(`count ${DESKTOP}: to the first released passage, in addition: ${counter.presses.length} press(es) (${counter.presses.join(' | ')}), ${counter.addresses.length - 1} further address(es)`);
    check(counter.presses.length <= FIRST_RELEASE.presses && counter.addresses.length - 1 <= FIRST_RELEASE.screens && counter.typed.length === 0, `the first released passage took ${counter.presses.length} presses on ${JSON.stringify(counter.addresses)}: ${counter.presses.join(' | ')}`);
    await walk.picture('b05c-released', { whole: true });
  });

  // ------------------------------------------------------------------ the Basic page
  const basic = page.locator('div.basic[data-key="basic"]');
  const headline = page.locator('h1#basic-status');
  const stack = page.locator('section[data-key="basic-approvals"] .approval-stack');
  const appRow = page.locator(`div.basic-app[data-key="row-app-${DESKTOP}"]`);
  const left = page.locator('section[data-key="basic-left"]');
  const closePanel = async () => {
    await page.locator('#panel .panel-head button').click();
    await eventually(async () => (await walk.count('#panel')) === 0, WAIT, 'the details did not close');
  };
  /** Open the sheet of the app with the keyboard, as a person does who tabbed to "Change". */
  const openSheet = async () => {
    await appRow.locator(`button[data-key="change-${DESKTOP}"]`).focus();
    await page.keyboard.press('Enter');
    const sheet = walk.sheetWith('input[name="basic-mode"]');
    await sheet.waitFor({ state: 'visible', timeout: WAIT });
    return sheet;
  };
  const choose = (sheet, mode) => sheet.locator('label.choice', { has: page.locator(`input[name="basic-mode"][value="${mode}"]`) }).click();
  const grantOf = async () => (await walk.apps()).apps.find((entry) => entry.name === DESKTOP).grants.documents;

  await step('A: "Open my vault" opens the Basic page on the exact text that left, with the record\'s own figures behind one button', async () => {
    await stepCard.locator('#ask-open').click();
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/basic', `"Open my vault" leads to ${walk.address()}`);
    await eventually(async () => (await page.locator('#panel pre.released').textContent({ timeout: 2000 })) === firstRead.text, WAIT, 'the details do not show the text the app received');
    const gone = await walk.text('#panel .gone');
    check(gone.includes(known.destination) && TEXT.cannotTakeBack.test(gone), `under the text the details say: ${gone}`);
    // "Entry" and "Chain" are the record's own figures: on this page they stand behind "Record details".
    const more = page.locator('#panel #panel-more');
    check((await more.getAttribute('aria-expanded')) === 'false' && (await page.locator('#panel .facts').count()) === 0, 'the figures of the record are shown without a press');
    check(!/Entry #\d/.test(await page.locator('#panel').textContent()), 'the details name the number of the entry without a press');
    await more.click();
    await eventually(async () => (await page.locator('#panel .panel-section .facts').count()) === 2, WAIT, '"Record details" did not show the two blocks');
    check((await more.getAttribute('aria-expanded')) === 'true' && /Entry #\d/.test(await page.locator('#panel').textContent()), 'the opened details do not name the entry');
    check((await walk.count('.card-setup')) === 0 && (await walk.state()).interface.setup === 'done', 'the Basic page still asks to finish setting up');
    await walk.picture('b05d-basic-what-left', { whole: true });
    await closePanel();
  });

  await step('A: the Basic page after the first run: one headline, how to ask, what left, the document, the app under its stored name, the view at its foot', async () => {
    check((await headline.textContent()).trim() === TEXT.nothingWaits && (await walk.count('h1')) === 1, `the headline is "${await headline.textContent()}" among ${await walk.count('h1')}`);
    const approvals = page.locator('section[data-key="basic-approvals"]');
    check(trimmed(await approvals.locator('p.basic-label').textContent()) === 'Approvals' && (await walk.text('section[data-key="basic-approvals"] p.basic-none')) === TEXT.whenAnAppAsks, 'the part "Approvals" does not say what appears in it');
    check((await walk.text('p.basic-again')) === TEXT.tryAgain, `about asking again the page says: ${await walk.text('p.basic-again')}`);
    check((await walk.count('aside.sidebar')) === 0 && (await walk.count('.strip')) === 0 && (await walk.count('.nav-item')) === 0, 'the Basic page shows the sidebar or the choice between the kinds of apps');
    // How to ask, and what left: the two answers the app was given, the newest first.
    const ask = left.locator('div.basic-ask[data-key="basic-ask"]');
    check((await ask.locator('pre.code').textContent()) === TEXT.askStart(walk.product) && (await ask.locator('#basic-ask-copy').count()) === 1, 'the page does not say how to ask');
    const now = await walk.state();
    const said = trimmed(await left.locator('.section-aside').textContent());
    check(now.today.releases === 2 && said === `Last 24 hours: 2 answers, ${now.today.characters.toLocaleString('en-US')} characters`, `beside the heading the page says: ${said}`);
    const rows = left.locator('ul.act-list li.act-item');
    check((await rows.count()) === 2 && (await left.locator('li.act-divider').count()) === 0, `what left is shown in ${await rows.count()} rows`);
    check(trimmed(await rows.first().locator('.act-status').textContent()) === 'Released after approval', `the newest row is marked "${trimmed(await rows.first().locator('.act-status').textContent())}"`);
    check((await left.locator('.act-row').first().textContent()).includes(DESKTOP) && !(await left.textContent()).includes(known.label), 'a row names the program in place of the stored name');
    check((await left.locator('p.basic-hint').allTextContents()).some((text) => text.includes(TEXT.searchAnswer.slice(1))), 'the page does not say what a search answer holds');
    // No figure for calls that were refused, and nothing that was stopped: only the passage that waited, which is not a stop.
    check((await walk.count('ul.basic-stopped')) === 0 && (await walk.count('.stat')) === 0, 'the page shows stopped calls or a figure of them');
    // Documents.
    const documents = page.locator('section[data-key="basic-documents"]');
    check((await walk.text('p.basic-count')) === TEXT.oneDocument, `the page counts: ${await walk.text('p.basic-count')}`);
    check((await walk.text('section[data-key="basic-documents"] #docs-sentence')) === TEXT.sentence(DESKTOP, known.destination), 'the documents stand without the sentence about where text goes');
    check((await documents.locator('.drop[data-key="drop-documents"]').count()) === 1 && (await documents.locator('.drop-large').count()) === 0 && (await walk.count('.drop')) === 1, 'the page has not exactly one bounded place for files');
    // Apps: the stored name, where its text goes, today's label, one control.
    check((await walk.text('#basic-labels')) === TEXT.nameIsALabel(walk.product), `under "Apps" the page says: ${await walk.text('#basic-labels')}`);
    check(trimmed(await appRow.locator('.basic-app-name').textContent()) === DESKTOP && trimmed(await appRow.locator('.basic-app-access').textContent()) === TEXT.askMe, `the row of the app reads: ${trimmed(await appRow.textContent())}`);
    check((await appRow.textContent()).includes(`Sends to ${known.destination}`) && trimmed(await appRow.locator(`button[data-key="change-${DESKTOP}"]`).textContent()) === 'Change', 'the row lacks where the text goes, or "Change"');
    const app = (await walk.apps()).apps[0];
    const used = `In its last 24 hours: 2 answers, ${app.usage.chars_day.toLocaleString('en-US')} of ${app.limits.chars_per_day.toLocaleString('en-US')} characters`;
    check(app.usage.releases_day === 2 && trimmed(await appRow.locator('.basic-app-used span').first().textContent()) === used, `the second line of the row reads: ${trimmed(await appRow.locator('.basic-app-used').textContent())}`);
    check(/^Last asked .+ · says it is journey-agent 1\.0, unverified$/.test(trimmed(await appRow.locator('.basic-app-seen').textContent())), `the third line of the row reads: ${trimmed(await appRow.locator('.basic-app-seen').textContent())}`);
    check((await appRow.locator('.switch, input[type="number"]').count()) === 0 && (await appRow.locator('button').count()) === 1, 'the row has a control for pausing or for limits');
    check(trimmed(await page.locator('a.btn#basic-add-app').textContent()) === 'Add an app' && (await page.locator('a.btn#basic-add-app').getAttribute('href')) === '#/setup/app', 'the page does not offer to add an app');
    check((await walk.count('section[data-key="basic-also"]')) === 0, 'the page says that parts of this vault were set up in Pro');
    // The foot: the command, the record, what it cannot do; and the view, bottom left.
    const foot = page.locator('footer.basic-foot');
    check((await foot.locator('.launch pre.code').textContent()) === now.launch.command && (await foot.locator('#launch-copy').count()) === 1, 'the foot does not hold the command that opens the app again');
    check((await foot.textContent()).includes(TEXT.recordChecked) && trimmed(await foot.locator('#basic-cannot').textContent()) === TEXT.cannotDo(walk.product), 'the foot lacks the line about the record, or what the product cannot do');
    const bar = page.locator('footer.viewbar[data-key="viewbar"]');
    check(trimmed(await bar.locator('.view-label').textContent()) === 'View' && trimmed(await bar.locator('.viewbar-text').textContent()) === TEXT.viewWords, `the bar of the view reads: ${trimmed(await bar.textContent())}`);
    check(JSON.stringify((await bar.locator('label.segment').allTextContents()).map(trimmed)) === JSON.stringify(['Basic', 'Pro']) && (await bar.locator('input[name="view"][value="basic"]').isChecked()), 'the view control does not offer "Basic" and "Pro" with "Basic" chosen');
    const [barBox, switchBox] = [await bar.boundingBox(), await bar.locator('.segmented').boundingBox()];
    check(barBox.y + barBox.height >= page.viewportSize().height - 1 && switchBox.x < page.viewportSize().width / 2, `the view control is not in the bottom-left corner: ${JSON.stringify([barBox, switchBox])}`);
    // Setting up is over and nothing waits: no button of the page is the loud one.
    check((await basic.locator('.btn-primary').count()) === 0, 'a button of the page is filled although nothing is left to set up');
    check((await page.locator('link[rel="icon"]').getAttribute('href')) === 'favicon.svg', 'the icon of the tab carries a dot although nothing waits');
    // "What it cannot do" is the list of the Vault page, in a sheet that "Close" has the focus of.
    await foot.locator('#basic-cannot').click();
    const cannot = walk.sheetWith('ul.cannot');
    await cannot.waitFor({ state: 'visible', timeout: WAIT });
    check((await cannot.locator('ul.cannot li').count()) === 8 && (await focused(cannot.locator('.sheet-foot button'))), 'the sheet does not hold the eight statements with the focus on "Close"');
    await cannot.locator('.sheet-foot button').click();
    await walk.noSheet();
    // The documents are listed behind one button, each with a way to remove it.
    await page.locator('#basic-docs-toggle').click();
    const table = documents.locator('table.doc-table');
    await table.waitFor({ state: 'visible', timeout: WAIT });
    check((await table.locator('tbody tr').count()) === 1 && (await table.locator('tbody .icon-btn').count()) === 1, 'the table does not list the one document with its bin');
    check((await documents.locator('p.basic-hint').allTextContents()).some((text) => text.includes("a document's number, not its file name")), 'the page does not say that apps are shown a number in place of the file name');
    await page.locator('#basic-docs-toggle').click();
    await eventually(async () => (await documents.locator('table.doc-table').count()) === 0, WAIT, 'the list of documents did not fold again');
  });

  let waitingRef = '';
  await step('A: a passage waits on the Basic page: the card is first, nothing is filled, and whatever comes or goes near it makes "Release once" wait again', async () => {
    // The owner comes back to the page at its top, where what waits is shown.
    await page.evaluate(() => window.scrollTo(0, 0));
    waitingRef = `${doc}.2`;
    check(walk.agent(DESKTOP, 'read', waitingRef).code === 'NEEDS_APPROVAL', 'the second passage did not need approval');
    const card = stack.locator(`> .approval[data-key="${key(waitingRef)}"]`);
    await card.waitFor({ state: 'visible', timeout: WAIT });
    await says(headline, TEXT.oneWaits, 'with one passage waiting the headline reads');
    // Between the top bar and the card stand the label and the headline, and nothing else.
    const order = await page.locator('section[data-key="basic-approvals"]').evaluate((node) => Array.from(node.children).filter((child) => !child.classList.contains('sr-only')).map((child) => child.className.split(' ')[0]));
    check(order.slice(0, 3).join(',') === 'basic-label,basic-status,approval-stack', `under "Approvals" stand, in order: ${order.join(', ')}`);
    check((await stack.locator('> *').first().getAttribute('data-key')) === key(waitingRef), 'the card is not the first thing under the headline');
    const top = await page.locator('.basic-top').boundingBox();
    const label = await page.locator('section[data-key="basic-approvals"] p.basic-label').boundingBox();
    check(top.y + top.height <= label.y + 1 && label.y < (await headline.boundingBox()).y && (await headline.boundingBox()).y < (await card.boundingBox()).y, 'the label, the headline and the card are not in this order under the top bar');
    check((await page.locator('.basic-top').locator('.offline-bar').count()) === 0, 'a bar stands above the card although nothing is wrong');
    check((await walk.count('.btn-primary')) === 0, 'a button is filled while a passage waits');
    check((await page.title()).startsWith('(1) ') && (await page.locator('link[rel="icon"]').getAttribute('href')) === 'favicon-waiting.svg', `the tab is called "${await page.title()}" with the icon ${await page.locator('link[rel="icon"]').getAttribute('href')}`);
    check((await card.locator('pre.released').textContent()).includes('Small repairs of up to 110 EUR'), 'the card does not show the exact text of the second passage');
    await card.locator('button[data-key="release"]:not(.is-waiting)').waitFor({ state: 'visible', timeout: WAIT });
    await walk.picture('b07-basic-waiting', { whole: true });

    const tops = async () => [await topOf(headline), await topOf(card)];
    const start = await tops();
    // The pointer rests on "Release once" while things come and go.
    await card.locator('button[data-key="release"]:not(.is-waiting)').hover();

    // A list that cannot be loaded brings a bar above the card; when it loads again the bar goes.
    const documentsList = (url) => url.pathname === '/api/documents';
    const failing = expectHttp(503, '/api/documents', { times: 1000, optional: true });
    const bar = page.locator('.basic-top .offline-bar');
    let refused = true;
    await page.route(documentsList, (route) => route.fulfill({ status: 503, contentType: 'application/json', body: '{}' }));
    try {
      await waitsAgain(walk, card, 'a bar appeared above the card', async () => {
        // The page asks for its lists again when the record of the vault has a new entry: here, a search of the app.
        check(walk.agent(DESKTOP, 'search', 'caretaker laundry', 'documents').code === 'OK', 'the search of the app was not answered');
        await eventually(async () => (await bar.count()) === 1, WAIT, 'no bar said that a list could not be loaded');
      });
      check(TEXT.notUpToDate.test(trimmed(await bar.textContent())), `the bar reads: ${trimmed(await bar.textContent())}`);
      check((await page.locator('.basic-top').boundingBox()).height + (await page.locator('.basic-top').boundingBox()).y <= (await page.locator('section[data-key="basic-approvals"] p.basic-label').boundingBox()).y + 1, 'the bar lies over the page');
      await waitsAgain(walk, card, 'the bar above the card went', async () => {
        // The list can be loaded again from here on; the page tries by itself.
        await page.unroute(documentsList);
        refused = false;
        await eventually(async () => (await bar.count()) === 0, WAIT, 'the bar stayed although the list can be loaded again');
      });
    } finally {
      if (refused) await page.unroute(documentsList);
    }
    withdraw(failing);

    // The details of an entry open beside the cards, and close.
    await waitsAgain(walk, card, 'the details of an entry opened', async () => {
      await left.locator('li.act-item button.act-row').first().click({ position: { x: 24, y: 12 } });
      await eventually(async () => (await walk.count('#panel pre.released')) === 1, WAIT, 'the details of the entry did not open');
    });
    await waitsAgain(walk, card, 'the details of an entry closed', closePanel);

    // A second passage arrives. It takes its place under the first, which does not move.
    await page.evaluate(() => window.scrollTo(0, 0));
    const second = `${doc}.3`;
    await waitsAgain(walk, card, 'a second passage arrived', async () => {
      check(walk.agent(DESKTOP, 'read', second).code === 'NEEDS_APPROVAL', 'the third passage did not need approval');
      await eventually(async () => (await stack.locator(`> .approval[data-key="${key(second)}"]`).count()) === 1, WAIT, 'the second passage has no card');
    });
    check((await headline.textContent()).trim() === TEXT.waits(2) && (await page.title()).startsWith('(2) '), `with two passages waiting the headline reads "${await headline.textContent()}"`);
    check(JSON.stringify(await stack.locator('> .approval').evaluateAll((nodes) => nodes.map((node) => node.dataset.key))) === JSON.stringify([key(waitingRef), key(second)]), 'the second passage did not take its place under the first');
    check(JSON.stringify(await tops()) === JSON.stringify(start), `the headline or the first card moved: ${JSON.stringify([start, await tops()])}`);

    // A notice arrives under the cards. No real notice can be made to arrive in the middle of a session
    // of this vault, so the page is handed one of the app's own, in the app's words, with its next state.
    const notice = { code: 'synced_folder', text: 'The vault lies in a folder that looks synchronised: its sealed files and their sizes are copied to a provider and to other devices.', detail: [walk.store] };
    const stateOfTheVault = (url) => url.pathname === '/api/state';
    const warnings = page.locator('ul.warnings[data-key="warnings"]');
    await page.route(stateOfTheVault, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.warnings = (body.warnings || []).concat([notice]);
      await route.fulfill({ response, json: body });
    });
    try {
      await eventually(async () => (await warnings.count()) === 1, WAIT, 'the notice was not shown');
      const [noticeTop, lastCard] = [await topOf(warnings), await stack.locator('> .approval').last().boundingBox()];
      check(noticeTop >= (await topOf(stack.locator('> .approval').last())) + lastCard.height, 'the notice stands above a card');
      check(JSON.stringify(await tops()) === JSON.stringify(start), `a notice moved the headline or the first card: ${JSON.stringify([start, await tops()])}`);
    } finally {
      await page.unroute(stateOfTheVault);
    }
    await eventually(async () => (await warnings.count()) === 0, WAIT, 'the notice stayed');
    check(JSON.stringify(await tops()) === JSON.stringify(start), 'the headline or the first card moved when the notice went');

    // "Don't release", for each: the line that says so stays in the card's place for a moment, then nothing waits.
    await decideApproval(page, key(waitingRef), 'dismiss');
    await decideApproval(page, key(second), 'dismiss');
    await says(headline, TEXT.nothingWaits, 'after both answers the headline reads');
    await eventually(async () => (await stack.count()) === 0 || (await stack.locator('> *').count()) === 0, WAIT, 'a card or its line stayed');
    const after = await walk.approvals();
    check(after.waiting.length === 0 && after.granted.length === 0, 'after "Don\'t release" something waits or stands approved');
    await eventually(() => focused(headline), WAIT, 'after the last answer the focus is not on the headline');
    await eventually(async () => (await page.title()) === walk.product && (await page.locator('link[rel="icon"]').getAttribute('href')) === 'favicon.svg', WAIT, 'the tab still says that something waits');
    // What was not released is no stop of the day, and nothing more left the vault.
    check((await walk.state()).today.releases === 3 && (await walk.count('ul.basic-stopped')) === 0, 'a passage that was not released counts as released, or as stopped');
  });

  await step('A: "Change" opens the sheet of the app on what is in force; "Can read" is saved, said after the next file, and taken back without a question', async () => {
    await walk.noToast();
    const sheet = await openSheet();
    check(trimmed(await sheet.locator('.sheet-title').textContent()) === DESKTOP, `the sheet is called "${trimmed(await sheet.locator('.sheet-title').textContent())}"`);
    check(await focused(sheet.locator('.sheet-foot button').first()) && trimmed(await sheet.locator('.sheet-foot button').first().textContent()) === 'Cancel', 'the sheet does not open with the focus on "Cancel"');
    const modes = sheet.locator('input[name="basic-mode"]');
    check((await modes.evaluateAll((nodes) => nodes.map((node) => `${node.value}${node.checked ? '*' : ''}`))).join(',') === 'approve*,read,none', 'the three choices are not in their order with the one in force chosen');
    check(JSON.stringify((await sheet.locator('.choice-label').allTextContents()).map(trimmed)) === JSON.stringify([TEXT.askMe, TEXT.canRead, TEXT.noAccess]), 'the three choices do not carry the three labels');
    // The two figures of "Can read" are the vault's own.
    const settings = await walk.get('/api/settings');
    const share = `${Math.round(settings.per_document.max_fraction * 100)}%`;
    const most = `${settings.per_document.max_chars.toLocaleString('en-US')} characters at most`;
    const lines = trimmed(await sheet.locator('#basic-mode-read-lines').textContent());
    check(lines.includes(`up to ${share} of each document`) && lines.includes(most), `under "Can read" the sheet says: ${lines}`);
    check((await walk.text('#basic-sentence')) === TEXT.sentence(DESKTOP, known.destination) && (await sheet.textContent()).includes(TEXT.nameIsALabel(walk.product)), 'the sheet lacks the sentence about where text goes, or about names');
    check(await sheet.locator('#basic-save').isDisabled(), '"Save" can be pressed before anything was changed');
    check((await sheet.locator('#basic-steps').getAttribute('href')) === `#/setup/connect?app=${DESKTOP}` && (await sheet.locator('#basic-remove').count()) === 1, 'the sheet lacks the way to the connection steps, or "Remove this app"');
    await walk.picture('b13-app-sheet');

    const mark = walk.sent.length;
    await choose(sheet, 'read');
    await eventually(async () => !(await sheet.locator('#basic-save').isDisabled()), WAIT, '"Save" stays dimmed after another choice');
    await sheet.locator('#basic-save').click();
    await walk.noSheet();
    check(JSON.stringify(walk.changesSince(mark)) === JSON.stringify(['POST /api/grants']), `"Save" sent ${JSON.stringify(walk.changesSince(mark))}`);
    await says(appRow.locator('.basic-app-access'), TEXT.canRead, 'after "Save" the row of the app says');
    check((await grantOf()) === 'read', `the vault says: ${await grantOf()}`);
    await page.locator('#toasts .toast', { hasText: `${DESKTOP}: ${TEXT.canRead}.` }).waitFor({ state: 'visible', timeout: WAIT });

    // A second file: the line after it says that passages of it can now leave without asking.
    await page.setInputFiles('#files-documents', files.boiler);
    await eventually(async () => (await walk.count('li.upload.upload-done')) === 1, 60000, 'the second file was not sealed');
    check((await walk.text('p.upload-who')) === TEXT.whoReads(DESKTOP), `after the second file the page says: ${await walk.text('p.upload-who')}`);
    check((await walk.count('.upload-outside')) === 1, 'what is true of the chosen file is not said exactly once');
    await says(page.locator('p.basic-count'), TEXT.documents(2), 'the page counts');

    // Back to "Ask me each time". Lowering asks nothing.
    const again = await openSheet();
    check((await again.locator('input[name="basic-mode"]:checked').getAttribute('value')) === 'read', 'the sheet does not open on what is in force');
    await choose(again, 'approve');
    await again.locator('#basic-save').click();
    await walk.noSheet();
    check((await walk.count('#reauth-passphrase')) === 0 && (await grantOf()) === 'approve', 'lowering what the app may do asked for the passphrase, or did not take');
    await says(appRow.locator('.basic-app-access'), TEXT.askMe, 'after the second "Save" the row of the app says');
  });

  await step('A: locked with a passage waiting, the screen says what became of it and shows no figure of the record; "Unlock" opens the Basic page at its headline', async () => {
    check(walk.agent(DESKTOP, 'read', waitingRef).code === 'NEEDS_APPROVAL', 'the passage did not wait again');
    await stack.locator(`> .approval[data-key="${key(waitingRef)}"]`).waitFor({ state: 'visible', timeout: WAIT });
    await page.click('button.lock-now');
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    const facts = (await page.locator('dl.seal-facts dt').allTextContents()).map(trimmed);
    check(JSON.stringify(facts) === JSON.stringify(['Sealed files', 'Size']), `the locked screen shows the facts ${JSON.stringify(facts)}`);
    check((await walk.text('.gate-foot')) === TEXT.lockedFoot, `the foot of the locked screen reads: ${await walk.text('.gate-foot')}`);
    check((await walk.text('#locked-waiting')) === TEXT.oneWaited, `about what waited the locked screen says: ${await walk.text('#locked-waiting')}`);
    check((await walk.count('input[name="view"]')) === 0 && (await walk.count('#view-basic')) === 0, 'the locked screen has the view control');
    check(walk.agent(DESKTOP, 'search', 'rent').code === 'LOCKED', 'a locked vault answered a search');
    await walk.picture('b08-locked-basic');
    await unlock(walk);
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/basic', `after "Unlock" the address is ${walk.address()}`);
    await eventually(() => focused(headline), WAIT, 'after "Unlock" the focus is not on the headline');
    // What waited was not released and waits no more; the app has to ask again.
    await says(headline, TEXT.nothingWaits, 'after the unlock, with no app having asked again, the headline reads');
    check((await walk.approvals()).waiting.length === 0, 'the passage outlived the lock');
    // What left before this unlock stands under a line that says so.
    const list = left.locator('ul.act-list');
    await list.locator('li.act-divider').waitFor({ state: 'visible', timeout: WAIT });
    check(TEXT.nothingSince.test(trimmed(await list.locator('li.act-none').textContent())) && TEXT.beforeUnlock.test(trimmed(await list.locator('li.act-divider').textContent())), `the rows are split by: ${trimmed(await list.locator('li.act-divider').textContent())}`);
    check((await list.locator('li.act-divider').count()) === 1 && (await list.locator('> li').first().getAttribute('class')).includes('act-none'), 'the line that splits the rows is not there exactly once, under the line that nothing left since');

    // An ordinary day: the app asks again and gets an answer, which stands above the line.
    const search = walk.agent(DESKTOP, 'search', 'quiet hours waste', 'documents');
    check(search.code === 'OK' && search.released, `search after the unlock: ${JSON.stringify(search)}`);
    await eventually(async () => {
      const kinds = await list.locator('> li').evaluateAll((nodes) => nodes.map((node) => node.className.split(' ')[0]));
      check(kinds.join(',') === 'act-item,act-divider,act-item,act-item,act-item', `the rows of what left are ${kinds.join(',')}`);
      return true;
    });
    check((await list.locator('li.act-none').count()) === 0, 'the page still says that nothing left since the unlock');
    check((await walk.state()).record.verified === true && (await page.locator('footer.basic-foot').textContent()).includes(TEXT.recordChecked), 'the foot does not say that the record was checked at the unlock');
    await walk.picture('b06-basic', { whole: true });
  });

  let head = null;
  await step('A: the switch to Pro stores the view and says nothing; a restart opens Pro, the switch back and a restart open Basic; the record gains no entry by it', async () => {
    await walk.noToast();
    head = (await walk.state()).record;
    await switchView(page, 'pro');
    check(walk.address() === '#/overview', `"Pro" leads to ${walk.address()}`);
    await page.locator('.page-body[data-key="view-overview"]').waitFor({ state: 'attached', timeout: WAIT });
    check((await walk.state()).interface.view === 'pro', 'the switch did not keep the view');
    check((await walk.count('.visit-bar')) === 0 && (await walk.count('#toasts .toast')) === 0, 'the switch to Pro left a bar or a message');
    check(!/Basic/.test(await page.locator('#main').textContent()), 'a page of Pro speaks of Basic');
    // The control is at the foot of the sidebar, above the state of the lock, with "Pro" chosen; the six links are where they were.
    const control = page.locator('.sidebar-foot .view-switch[data-key="view"]');
    check(trimmed(await control.locator('.view-label').textContent()) === 'View' && (await control.locator('input[name="view"][value="pro"]').isChecked()), 'the view control of Pro does not show "Pro" chosen under the word "View"');
    check((await control.boundingBox()).y + (await control.boundingBox()).height <= (await page.locator('.sidebar-foot .lock-state').boundingBox()).y + 1, 'the view control is not above the state of the lock');
    check((await page.locator('aside.sidebar a.nav-item').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('href')))).join(',') === '#/overview,#/activity,#/approvals,#/documents,#/apps,#/vault', 'the six links of Pro are not the six');
    // The vault is as the first run left it: the app is named as answered, and nothing is left to get started.
    await page.locator('.hero-line').waitFor({ state: 'visible', timeout: WAIT });
    check((await walk.text('.hero-line')).includes(DESKTOP) && (await walk.count('.card-start')) === 0, 'Overview offers the list for a new vault, or does not name the app');
    const newest = (await walk.activity()).find((item) => item.app === DESKTOP);
    await page.locator(`.card[data-key="latest"] li.act-item[data-key="a${newest.seq}"]`).waitFor({ state: 'visible', timeout: WAIT });
    let now = await walk.state();
    check(now.record.head_seq === head.head_seq && now.record.head_hash === head.head_hash, 'the switch wrote an entry into the record');

    await restart(walk);
    await unlock(walk);
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: 40000 });
    check((await walk.count(FRAME.basic)) === 0 && (await walk.state()).interface.view === 'pro', 'after a restart the vault does not open in Pro');
    head = (await walk.state()).record;
    await switchView(page, 'basic');
    check(walk.address() === '#/basic' && (await walk.state()).interface.view === 'basic', `"Basic" leads to ${walk.address()}`);
    check((await walk.count('#toasts .toast')) === 0, 'the switch to Basic left a message');
    now = await walk.state();
    check(now.record.head_seq === head.head_seq && now.record.head_hash === head.head_hash, 'the switch back wrote an entry into the record');
    check(fs.readFileSync(path.join(walk.store, 'run', 'interface.json'), 'utf8') === '{"view": "basic", "setup": "done"}\n', `beside the vault the view is kept as: ${fs.readFileSync(path.join(walk.store, 'run', 'interface.json'), 'utf8')}`);

    // Once more, with owner mode as short as this run was told: the next step waits for its end.
    await restart(walk, { ownerSeconds: options.ownerSeconds });
    await unlock(walk);
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/basic' && (await walk.count(FRAME.pro)) === 0, `after the second restart the page is at ${walk.address()}`);
    check((await walk.state()).launch.port_fixed === true, 'an app that was given its port does not say so');
  });

  await step('A: after owner mode has passed, "Can read" asks for the passphrase; closing that sheet changes nothing and says so', async () => {
    const over = Date.parse((await walk.state()).owner_mode_until) + 1500;
    await eventually(() => Date.now() >= over, options.ownerSeconds * 1000 + WAIT, 'owner mode did not run out');
    await appRow.waitFor({ state: 'visible', timeout: WAIT });
    const sheet = await openSheet();
    await choose(sheet, 'read');
    expectHttp(403, '/api/grants');
    await sheet.locator('#basic-save').click();
    const again = page.locator('#reauth-passphrase');
    await again.waitFor({ state: 'visible', timeout: WAIT });
    // The owner closes the sheet that asks, without the passphrase.
    await walk.sheetWith('#reauth-passphrase').locator('.sheet-foot button', { hasText: 'Cancel' }).click();
    await eventually(async () => (await walk.count('#reauth-passphrase')) === 0, WAIT, 'the sheet that asks for the passphrase stayed');
    await sheet.locator('#basic-error').waitFor({ state: 'visible', timeout: WAIT });
    check((await walk.text('#basic-error')) === TEXT.nothingChanged, `the sheet says: ${await walk.text('#basic-error')}`);
    check((await sheet.locator('input[name="basic-mode"]:checked').getAttribute('value')) === 'approve' && (await sheet.locator('#basic-save').isDisabled()), 'the sheet does not show what is in force again');
    check((await grantOf()) === 'approve', 'something was changed although the passphrase was not given');
    await sheet.locator('.sheet-foot button').first().click();
    await walk.noSheet();
    check(trimmed(await appRow.locator('.basic-app-access').textContent()) === TEXT.askMe, 'the row does not say what is in force');
  });

  await step('A: Ctrl-C: the Basic page says that the app is not answering, with the command that starts it again', async () => {
    const command = (await walk.state()).launch.command;
    const exit = await stopApp(walk.app, 'SIGINT');
    check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
    check(walk.app.err === '', `standard error: ${walk.app.err}`);
    check(!fs.existsSync(path.join(walk.store, 'run', 'gate.sock')), 'the socket is still there');
    const bar = page.locator('.basic-top .basic-bar .offline-bar');
    await bar.waitFor({ state: 'visible', timeout: WAIT });
    const said = (await bar.locator('p').allTextContents()).map(trimmed);
    check(said[0] === TEXT.notAnswering(walk.product) && said[1] === TEXT.startAgain && said[2] === TEXT.continuesByItself, `the bar reads: ${JSON.stringify(said)}`);
    check((await bar.locator('pre.code').textContent()) === command && command.includes(`--port ${walk.app.port}`) && (await bar.locator('#bar-launch-copy').count()) === 1, `the bar holds the command: ${await bar.locator('pre.code').textContent()}`);
    // At the top of the page the bar covers nothing of it, and the top bar no longer says what the vault's state was.
    await page.evaluate(() => window.scrollTo(0, 0));
    const [barBox, labelBox] = [await bar.boundingBox(), await page.locator('section[data-key="basic-approvals"] p.basic-label').boundingBox()];
    check(barBox.y + barBox.height <= labelBox.y, `the bar lies over the page: ${JSON.stringify([barBox, labelBox])}`);
    check(trimmed(await page.locator('.topbar .lock-state.is-unknown').textContent()) === 'State unknown', 'the top bar still says that the vault is unlocked');
    await walk.picture('b14-basic-app-stopped');
  });

  await step('A: in all of this the page asked no address but the app\'s and kept nothing in the browser but its session', async () => {
    await leftNothingBehind(walk);
    await closePage(page);
  });
}

// =====================================================================================
// Run B: skipping
// =====================================================================================

async function runB(browser, work) {
  const walk = await begin(browser, work, 'b');
  const { page } = walk;
  const form = page.locator('form.gate-card[data-key="step-3"]');
  const setup = page.locator('section.card-setup[data-key="setup"]');
  /** The line about setting up, as the Basic page shows it: the count, what is next, and where its button leads. */
  const setupLine = async () => ({
    title: trimmed(await setup.locator('.card-title').textContent()),
    done: trimmed(await setup.locator('.card-aside').textContent()),
    next: trimmed(await setup.locator('p.setup-next').textContent()),
    action: trimmed(await setup.locator('#setup-continue').textContent()),
    href: await setup.locator('#setup-continue').getAttribute('href'),
  });
  const firstLine = JSON.stringify({ title: TEXT.finishSetup, done: TEXT.done(1), next: TEXT.nextApp, action: 'Continue', href: '#/setup/app' });

  await step('B: on the app step nothing is stored before the press that adds an app: a reload shows the step again, empty', async () => {
    await pair(walk);
    await create(walk);
    await form.waitFor({ state: 'visible', timeout: 40000 });
    // The products that cannot use a vault on this computer are not rows; one quiet button says why.
    const web = form.locator('#first-web');
    check((await web.getAttribute('aria-expanded')) === 'false' && (await walk.count('#first-web-note')) === 0, 'what is said about the web products is open before a press');
    await web.click();
    await page.locator('#first-web-note').waitFor({ state: 'visible', timeout: WAIT });
    check((await web.getAttribute('aria-expanded')) === 'true' && (await walk.text('#first-web-note')).startsWith('Those cannot use this vault.'), `about the web products the step says: ${await walk.text('#first-web-note')}`);
    // "Another app": its fields appear, and the button stays dimmed until they say enough.
    await form.locator('label.choice', { has: page.locator('input[value="generic"]') }).click();
    await form.locator('#first-app-name').waitFor({ state: 'visible', timeout: WAIT });
    check((await form.locator('input[name="first-app-kind"]').count()) === 2 && (await form.locator('#first-app-destination').count()) === 1, 'the fields of "Another app" are not the three');
    check((await form.locator('#first-add').isDisabled()) && (await walk.text('#first-sentence')) === TEXT.general, 'an app without a name can be added, or the sentence names one');
    check((await walk.count('input[name="first-mode"]')) === 0, 'the first run asks what the app may do');
    await reload(page);
    await form.waitFor({ state: 'visible', timeout: 40000 });
    await form.locator('input[name="first-app-known"]').first().waitFor({ state: 'attached', timeout: WAIT });
    check(walk.address() === '#/setup/app', `after a reload the address is ${walk.address()}`);
    check((await form.locator('input[name="first-app-known"]:checked').count()) === 0 && (await walk.count('#first-app-name')) === 0 && (await walk.count('#first-web-note')) === 0, 'a reload kept what was chosen on the step');
    check((await walk.apps()).apps.length === 0 && (await walk.state()).interface.setup === 'open', 'looking at the step stored something');
  });
  if (!walk.base) return;

  await step('B: locked under the app step, the vault opens on the Basic page, whose one line names the step and leads back to it', async () => {
    await lockElsewhere(page, walk.base);
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/setup/app', `a lock changed the address to ${walk.address()}`);
    check((await walk.count('#locked-waiting')) === 0, 'the locked screen speaks of passages that waited although none did');
    await unlock(walk);
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/basic', `after "Unlock" the address is ${walk.address()}`);
    await setup.waitFor({ state: 'visible', timeout: WAIT });
    check(JSON.stringify(await setupLine()) === firstLine, `the line about setting up reads ${JSON.stringify(await setupLine())}`);
    await setup.locator('#setup-continue').click();
    await form.waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/setup/app' && (await stepperMarks(walk)) === '+>--', `"Continue" leads to ${walk.address()} with the stepper ${await stepperMarks(walk)}`);
  });

  await step('B: "Set everything up yourself in Pro" stores the view and opens Overview with its list for a new vault; the switch leads back', async () => {
    const mark = walk.sent.length;
    await page.locator('a#setup-pro').click();
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/overview', `the link leads to ${walk.address()}`);
    const kept = (await walk.state()).interface;
    check(kept.view === 'pro' && kept.setup === 'open', `the link left the vault as ${JSON.stringify(kept)}`);
    check(JSON.stringify(walk.changesSince(mark)) === JSON.stringify(['POST /api/interface']), `the link sent ${JSON.stringify(walk.changesSince(mark))}`);
    // Setting up is still open, so Pro's own list for a new vault takes over, and no bar speaks of a visit.
    await page.locator('.card-start').waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('.card-start .start-step').count()) === 4 && (await walk.count('.visit-bar')) === 0, 'Overview does not show its four steps, or shows the bar of a visit');
    await switchView(page, 'basic');
    await setup.waitFor({ state: 'visible', timeout: WAIT });
    check(JSON.stringify(await setupLine()) === firstLine, `back in Basic the line about setting up reads ${JSON.stringify(await setupLine())}`);
    await setup.locator('#setup-continue').click();
    await form.waitFor({ state: 'visible', timeout: WAIT });
  });

  await step('B: "Skip" on both steps sends nothing and leads to the Basic page; a reload on the way shows the same step', async () => {
    const mark = walk.sent.length;
    await form.locator('.gate-actions .btn-quiet').click();
    const card = walk.card('step-2');
    await card.waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/setup/documents', `skipping the app step leads to ${walk.address()}`);
    // The part that was skipped keeps its number: nothing is marked as done that was not done.
    check((await stepperMarks(walk)) === '+->-', `the stepper of the step is marked ${await stepperMarks(walk)}`);
    const leads = (await card.locator('.gate-lead').allTextContents()).map(trimmed);
    check(leads.includes(TEXT.untilYouAddOne) && (await walk.count('#docs-sentence')) === 0, `without an app the step says: ${JSON.stringify(leads)}`);
    check(trimmed(await card.locator('.gate-actions .btn-primary').textContent()) === 'Continue' && (await card.locator('.gate-actions .btn-primary').isDisabled()), 'the step can be continued without a document');
    await reload(page);
    await card.waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/setup/documents', `after a reload the address is ${walk.address()}`);
    await card.locator('.gate-actions .btn-quiet').click();
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/basic', `skipping both steps leads to ${walk.address()}`);
    check(walk.changesSince(mark).length === 0, `skipping sent ${JSON.stringify(walk.changesSince(mark))}`);
    const now = await walk.state();
    check(now.interface.setup === 'open' && now.encryption.documents === 0 && (await walk.apps()).apps.length === 0, 'skipping stored something');
  });

  await step('B: the empty Basic page says one thing in each part, and its line about setting up is the one filled button', async () => {
    const empty = async () => {
      await setup.waitFor({ state: 'visible', timeout: WAIT });
      check(JSON.stringify(await setupLine()) === firstLine, `the line about setting up reads ${JSON.stringify(await setupLine())}`);
      check((await walk.text('h1#basic-status')) === TEXT.nothingWaits, `the headline reads "${await walk.text('h1#basic-status')}"`);
      check((await walk.text('section[data-key="basic-approvals"] p.basic-none')) === TEXT.noApp && (await walk.text('p.basic-again')) === TEXT.tryAgain, 'under the headline the page does not say that no app gets an answer');
      const left = page.locator('section[data-key="basic-left"]');
      await left.locator('p.basic-none').waitFor({ state: 'visible', timeout: WAIT });
      check(trimmed(await left.locator('.section-aside').textContent()) === TEXT.nothingInADay && trimmed(await left.locator('p.basic-none').textContent()) === TEXT.nothingYet, `about what left the page says: ${trimmed(await left.textContent())}`);
      check((await walk.count('div.basic-ask')) === 0, 'the page says how to ask although no app can');
      check((await walk.text('p.basic-count')) === TEXT.noDocuments && (await walk.count('.drop[data-key="drop-documents"]')) === 1 && (await walk.count('#docs-sentence')) === 0, 'the part "Documents" is not the count and the drop zone');
      const apps = page.locator('section[data-key="basic-apps"]');
      check(trimmed(await apps.locator('p.basic-none').textContent()) === TEXT.noApp && trimmed(await apps.locator('a.btn#basic-add-app').textContent()) === 'Choose your AI app', `the part "Apps" reads: ${trimmed(await apps.textContent())}`);
      check((await walk.count('#basic-labels')) === 0 && (await walk.count('.basic-app')) === 0, 'the part "Apps" shows a row');
      check((await page.locator('footer.basic-foot .launch pre.code').count()) === 1 && (await walk.count('#basic-cannot')) === 1, 'the foot lacks the command or what the product cannot do');
      // One filled button on the page, and it is the one that goes on with setting up.
      const filled = await page.locator('.btn-primary').evaluateAll((nodes) => nodes.map((node) => node.id));
      check(JSON.stringify(filled) === JSON.stringify(['setup-continue']), `the filled buttons of the page are ${JSON.stringify(filled)}`);
      // The card stands under the part "Approvals", above what left.
      check((await topOf(setup)) > (await topOf(page.locator('p.basic-again'))) && (await topOf(setup)) < (await topOf(left)), 'the line about setting up is not between "Approvals" and what left');
    };
    await empty();
    await walk.picture('b09-basic-empty', { whole: true });
    await reload(page);
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/basic', `after a reload the address is ${walk.address()}`);
    await empty();
  });

  await step('B: "Hide" ends the line about setting up for good; what it offered stays on the page', async () => {
    const mark = walk.sent.length;
    await setup.locator('#setup-hide').click();
    await eventually(async () => (await setup.count()) === 0, WAIT, 'the line about setting up stayed');
    await page.locator('#toasts .toast', { hasText: TEXT.setupHidden }).waitFor({ state: 'visible', timeout: WAIT });
    check(JSON.stringify(walk.changesSince(mark)) === JSON.stringify(['POST /api/interface']), `"Hide" sent ${JSON.stringify(walk.changesSince(mark))}`);
    const kept = (await walk.state()).interface;
    check(kept.view === 'basic' && kept.setup === 'done', `"Hide" left the vault as ${JSON.stringify(kept)}`);
    check((await walk.count('.btn-primary')) === 0, 'a button is filled although nothing is left to set up');
    await reload(page);
    await page.locator('a.btn#basic-add-app').waitFor({ state: 'visible', timeout: 40000 });
    check((await setup.count()) === 0, 'a reload brought the line about setting up back');
    check((await walk.count('.drop[data-key="drop-documents"]')) === 1 && trimmed(await page.locator('a.btn#basic-add-app').textContent()) === 'Choose your AI app', 'what the line offered is not on the page any more');
  });

  await step('B: "Choose your AI app" opens the same step without the stepper, and when the app stops under it the step says how to start it again', async () => {
    await page.locator('a.btn#basic-add-app').click();
    await form.waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/setup/app', `the button leads to ${walk.address()}`);
    await form.locator('input[name="first-app-known"]').first().waitFor({ state: 'attached', timeout: WAIT });
    // Setting up is over: no stepper, and the quiet button leads back in place of skipping on.
    check((await walk.count('ol.steps')) === 0 && trimmed(await form.locator('.gate-actions .btn-quiet').textContent()) === 'Cancel', 'the step still looks like a part of setting up');
    check((await form.locator('input[name="first-app-known"]').count()) === 6 && (await form.locator('input[name="first-app-known"]:checked').count()) === 0, 'the step does not offer the six apps with none ticked');
    const command = (await walk.state()).launch.command;
    const exit = await stopApp(walk.app, 'SIGINT');
    check(exit && exit.code === 0 && walk.app.err === '', `the app ended with ${JSON.stringify(exit)} and said: ${walk.app.err}`);
    const bar = page.locator('.gate-bar .offline-bar');
    await bar.waitFor({ state: 'visible', timeout: WAIT });
    const said = (await bar.locator('p').allTextContents()).map(trimmed);
    // This app chose its port itself, so it comes back at another address, and it opens no page by itself.
    check(said[0] === TEXT.notAnswering(walk.product) && said[1] === TEXT.startAgain && said[2] === TEXT.printsNewPage, `the bar reads: ${JSON.stringify(said)}`);
    check((await bar.locator('pre.code').textContent()) === command && !command.includes('--port') && (await bar.locator('#bar-launch-copy').count()) === 1, `the bar holds the command: ${await bar.locator('pre.code').textContent()}`);
    const [barBox, cardBox] = [await bar.boundingBox(), await form.boundingBox()];
    check(barBox.y + barBox.height <= cardBox.y, `the bar lies over the step: ${JSON.stringify([barBox, cardBox])}`);
    await walk.picture('b15-step-app-stopped', { whole: true });
  });

  await step('B: in all of this the page asked no address but the app\'s and kept nothing in the browser but its session', async () => {
    await leftNothingBehind(walk);
    await closePage(page);
  });
}

// =====================================================================================
// Run C: the sample letter
// =====================================================================================

async function runC(browser, work) {
  const walk = await begin(browser, work, 'c');
  const { page } = walk;
  const setup = page.locator('section.card-setup[data-key="setup"]');
  const docsCard = walk.card('step-2');
  const stepCard = walk.card('step-4');
  let known = null;

  await step('C: with an app added, a reload shows the documents step again, and after a lock the Basic page leads back to it', async () => {
    await pair(walk);
    await create(walk);
    const form = page.locator('form.gate-card[data-key="step-3"]');
    await form.waitFor({ state: 'visible', timeout: 40000 });
    await form.locator('label.choice', { has: page.locator(`input[value="${DESKTOP}"]`) }).click();
    await form.locator('#first-add:not([disabled])').click();
    await docsCard.waitFor({ state: 'visible', timeout: 40000 });
    known = (await walk.apps()).known.find((entry) => entry.id === DESKTOP);
    await reload(page);
    await docsCard.waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/setup/documents' && (await stepperMarks(walk)) === '++>-', `after a reload the page is at ${walk.address()} with the stepper ${await stepperMarks(walk)}`);
    await eventually(async () => (await docsCard.locator('.gate-lead').allTextContents()).map(trimmed).includes(TEXT.canThen(DESKTOP)), WAIT, 'after a reload the step does not say which app will be able to ask');
    await lockElsewhere(page, walk.base);
    await unlock(walk);
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: 40000 });
    await setup.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await setup.locator('.card-aside').textContent()) === TEXT.done(2) && trimmed(await setup.locator('p.setup-next').textContent()) === TEXT.nextDocument, `the line about setting up reads: ${trimmed(await setup.textContent())}`);
    check(trimmed(await setup.locator('#setup-continue').textContent()) === 'Continue' && (await setup.locator('#setup-continue').getAttribute('href')) === '#/setup/documents', 'the line does not lead to the documents step');
    // The app is there already, under its stored name, with what it may do.
    check(trimmed(await page.locator(`div.basic-app[data-key="row-app-${DESKTOP}"] .basic-app-access`).textContent()) === TEXT.askMe, 'the row of the app does not say what it may do');
    await setup.locator('#setup-continue').click();
    await docsCard.waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/setup/documents', `"Continue" leads to ${walk.address()}`);
  });
  if (!walk.base) return;

  await step('C: the invented sample letter is added on its own press, says that it is invented, and is not one of the files the owner chose', async () => {
    const offer = docsCard.locator('p.setup-sample');
    check(trimmed(await offer.locator('span').first().textContent()) === 'No file at hand?' && trimmed(await offer.locator('#sample-add').textContent()) === 'Add an invented sample letter', `the offer reads: ${trimmed(await offer.textContent())}`);
    check((await walk.state()).encryption.documents === 0, 'the sample is in the vault before its button was pressed');
    const mark = walk.sent.length;
    await offer.locator('#sample-add').click();
    const line = docsCard.locator('li.upload.upload-done[data-key="sample"]');
    await line.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await line.locator('.upload-note').textContent()) === TEXT.sampleAdded, `the line of the sample reads: ${trimmed(await line.textContent())}`);
    check((await walk.text('p.upload-who')) === TEXT.whoAsks(DESKTOP), `after the sample the page says: ${await walk.text('p.upload-who')}`);
    // It was sent by itself, under its own name, and no file of the owner's lies anywhere outside the vault for it.
    const sent = walk.sent.slice(mark).filter((entry) => entry.method !== 'GET');
    check(sent.length === 1 && sent[0].path === '/api/documents' && sent[0].search.includes('name=invented-sample-letter.txt') && sent[0].search.includes('collection=documents'), `the press sent ${JSON.stringify(sent.map((entry) => `${entry.method} ${entry.path}${entry.search}`))}`);
    check((await walk.count('.upload-outside')) === 0, 'the step speaks of a chosen file that is still outside the vault');
    const held = (await walk.get('/api/documents')).collections[0].documents;
    check(held.length === 1 && held[0].title === 'invented-sample-letter' && held[0].passages === 1, `the vault holds: ${JSON.stringify(held)}`);
    await eventually(async () => (await walk.count('#sample-add')) === 0, WAIT, 'the sample is offered a second time');
    const next = docsCard.locator('.gate-actions .btn-primary');
    check(trimmed(await next.textContent()) === 'Continue with 1 document' && (await docsCard.locator('.gate-actions .btn-quiet').count()) === 0, 'with a document the step still offers to skip, or does not count it');
    await walk.picture('b02b-setup-sample', { whole: true });
    await next.click();
    await stepCard.waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === `#/setup/connect?app=${DESKTOP}`, `after the sample the address is ${walk.address()}`);
  });

  await step('C: with only the sample, the question is whole; "Check the file" names a folder that is not there and leaves the home as it was', async () => {
    await stepCard.locator('li.ask-step[data-key="ask-3"]').waitFor({ state: 'visible', timeout: 40000 });
    const line = stepCard.locator('li.ask-step[data-key="ask-2"]');
    check(trimmed(await line.locator('.ask-step-title').textContent()) === 'Copy this question' && (await line.locator('pre.code').textContent()) === TEXT.askSample(walk.product), `the second line reads: ${trimmed(await line.textContent())}`);
    check(trimmed(await line.textContent()).includes(TEXT.pasteAsItIs), 'the step does not say to paste the question as it is');
    // The home of this run holds no settings of any program: the folder of the app is not there.
    const homeBefore = fs.readdirSync(walk.app.home).sort().join('\n');
    const answered = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/apps/${DESKTOP}/connect/check`, { timeout: WAIT });
    answered.catch(() => {});
    await stepCard.locator('#ask-check').click();
    const answer = await (await answered).json();
    check(answer.state === 'no_folder' && typeof answer.folder === 'string', `the look at the settings file answered ${JSON.stringify(answer)}`);
    const found = stepCard.locator('#ask-found');
    await found.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await found.textContent()) === `${TEXT.noFolder(known.label)}${answer.folder}`, `about the file the step says: ${trimmed(await found.textContent())}`);
    check(trimmed(await stepCard.locator('li.ask-step[data-key="ask-1"]').textContent()).includes(TEXT.installed(known.label)), 'the step does not ask whether the app is installed');
    check(fs.readdirSync(walk.app.home).sort().join('\n') === homeBefore, `the look changed the home of the run: ${fs.readdirSync(walk.app.home)}`);
    // The text is there without a look too, and it names this vault.
    await stepCard.locator('#ask-all').click();
    const text = await stepCard.locator('.ask-all pre.code').first().textContent();
    const server = Object.values(JSON.parse(text).mcpServers)[0];
    check(server.args.join(' ').includes(`gate mcp --client ${DESKTOP} --store ${walk.store}`) && !text.includes(PASSPHRASE), `the text does not name this vault, or holds the passphrase: ${text}`);
    // "Nothing arrives?" opens in place.
    const help = stepCard.locator('#ask-help');
    check((await help.getAttribute('aria-expanded')) === 'false' && (await walk.count('#ask-help-list')) === 0, 'the help is open before a press');
    await help.click();
    const items = (await stepCard.locator('#ask-help-list li').allTextContents()).map(trimmed);
    check(items.length === 4 && items[0].startsWith(`Was ${known.label} really quit and opened again?`) && items[3].startsWith(`${walk.product} must be running and the vault unlocked.`), `the help reads: ${JSON.stringify(items)}`);
    check((await help.getAttribute('aria-expanded')) === 'true' && !TEXT.protocol.test(items.join(' ')), 'the help names the protocol or an address');
    await walk.picture('b03e-nothing-arrives', { whole: true });
  });

  await step('C: stopped and started again under the step: the step says how, and after pairing and "Unlock" the Basic page leads back to it', async () => {
    const command = (await walk.state()).launch.command;
    await restart(walk, {
      whileDown: async () => {
        const bar = page.locator('.gate-bar .offline-bar');
        await bar.waitFor({ state: 'visible', timeout: WAIT });
        check((await bar.locator('pre.code').textContent()) === command && trimmed(await bar.textContent()).includes(TEXT.startAgain), `the bar reads: ${trimmed(await bar.textContent())}`);
      },
    });
    await unlock(walk);
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/basic', `after the restart the page is at ${walk.address()}`);
    await setup.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await setup.locator('.card-aside').textContent()) === TEXT.done(3) && trimmed(await setup.locator('p.setup-next').textContent()) === TEXT.nextQuestion, `the line about setting up reads: ${trimmed(await setup.textContent())}`);
    check(trimmed(await setup.locator('#setup-continue').textContent()) === 'Show me how' && (await setup.locator('#setup-continue').getAttribute('href')) === '#/setup/connect', 'the line does not lead to the step "First question"');
    await setup.locator('#setup-continue').click();
    await stepCard.locator('li.ask-step[data-key="ask-3"]').waitFor({ state: 'visible', timeout: 40000 });
    await eventually(() => walk.address() === `#/setup/connect?app=${DESKTOP}`, WAIT, `"Show me how" leads to ${walk.address()}`);
    check((await stepperMarks(walk)) === '+++>' && (await walk.text('p.ask-wait.ask-waiting')) === TEXT.waitingFor(DESKTOP), 'the step does not wait for the first request of the app');
  });

  await step('C: "Don\'t release" is an ending like any other: the same ways on, with "Add a document of your own" the filled one', async () => {
    check(walk.agent(DESKTOP, 'status').code === 'OK', 'the question of the app was not answered');
    await stepCard.locator('p.ask-wait.ask-arrived').waitFor({ state: 'visible', timeout: WAIT });
    const search = walk.agent(DESKTOP, 'search', 'rental agreement', 'documents');
    const found = REF.exec(search.text);
    check(search.code === 'OK' && found, `the search for the sample answered: ${JSON.stringify(search)}`);
    const ref = `${found[1]}.${found[2]}`;
    check(walk.agent(DESKTOP, 'read', ref).code === 'NEEDS_APPROVAL', 'the passage of the sample did not need approval');
    const card = stepCard.locator(`.approval[data-key="ap-${DESKTOP}-${ref}"]`);
    await card.waitFor({ state: 'visible', timeout: WAIT });
    check((await card.locator('pre.released').textContent()).startsWith('SAMPLE DOCUMENT, INVENTED.'), 'the card does not show that the text is invented');
    // While it waits, the way out of the screen is there and it is not the loud one.
    check((await walk.count('.btn-primary')) === 0 && (await stepCard.locator('.gate-actions #ask-open.btn-quiet').count()) === 1, 'while a passage waits a button is filled, or the way out is missing');
    await decideApproval(page, `ap-${DESKTOP}-${ref}`, 'dismiss');
    const end = stepCard.locator('.ask-end');
    await says(end.locator('h2#ask-end-title'), TEXT.notReleased, 'after "Don\'t release" the step says');
    check(trimmed(await end.locator('p').first().textContent()) === TEXT.nothingLeft(DESKTOP), `after "Don't release" the step says: ${trimmed(await end.locator('p').first().textContent())}`);
    await eventually(() => focused(end.locator('#ask-end-title')), WAIT, 'after the answer the focus is not on what the step says about it');
    const ways = await end.locator('.ask-actions button').evaluateAll((nodes) => nodes.map((node) => `${node.id}${node.classList.contains('btn-primary') ? '*' : ''}`));
    check(ways.join(',') === 'ask-own*,ask-open', `the ways on are ${ways.join(',')}`);
    check((await end.locator('.launch pre.code').count()) === 1, 'the step does not say how the app is opened again');
    const now = await walk.state();
    check(now.today.releases === 1 && now.interface.setup === 'done' && (await walk.approvals()).waiting.length === 0, 'something left with "Don\'t release", or setting up is still open');
    await walk.picture('b10-not-released', { whole: true });
  });

  await step('C: "Add a document of your own" opens the Basic page at "Choose files"; the sample is named there and removed on a press', async () => {
    await stepCard.locator('#ask-own').click();
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/basic', `the button leads to ${walk.address()}`);
    await eventually(() => focused(page.locator('#files-documents')), WAIT, 'the focus is not on "Choose files"');
    check((await setup.count()) === 0, 'the Basic page still asks to finish setting up');
    const sample = page.locator('p.basic-sample');
    check(trimmed(await sample.locator('span').first().textContent()) === TEXT.sampleIsIn && trimmed(await sample.locator('#sample-remove').textContent()) === 'Remove it', `about the sample the page says: ${trimmed(await sample.textContent())}`);
    check((await walk.text('p.basic-count')) === TEXT.oneDocument, `the page counts: ${await walk.text('p.basic-count')}`);
    const mark = walk.sent.length;
    await sample.locator('#sample-remove').click();
    const confirm = walk.sheetWith('.sheet-foot');
    await confirm.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await confirm.locator('.sheet-title').textContent()) === 'Remove “invented-sample-letter”?', `the question reads: ${trimmed(await confirm.locator('.sheet-title').textContent())}`);
    await confirm.locator('.sheet-foot button').last().click();
    await walk.noSheet();
    await says(page.locator('p.basic-count'), TEXT.noDocuments, 'after the sample was removed the page counts');
    check((await sample.count()) === 0, 'the sample is still named on the page');
    check(walk.changesSince(mark).length === 1 && walk.changesSince(mark)[0].startsWith('DELETE /api/documents/'), `removing sent ${JSON.stringify(walk.changesSince(mark))}`);
    check((await walk.state()).encryption.documents === 0, 'the sample is still in the vault');
  });

  await step('C: in all of this the page asked no address but the app\'s and kept nothing in the browser but its session', async () => {
    await leftNothingBehind(walk);
  });

  await step('C: at the end the app quits with status 0 and an empty standard error', async () => {
    await end(walk);
  });
}

// =====================================================================================
// Run D: a vault made in Pro, opened in Basic
// =====================================================================================

async function runD(browser, work, files) {
  const walk = await begin(browser, work, 'd', { args: ['--view', 'pro'] });
  const { page } = walk;
  const go = async (route) => {
    await page.click(`a.nav-item[href="#/${route}"]`);
    await page.locator(`.page-body[data-key="view-${route}"]`).waitFor({ state: 'attached', timeout: WAIT });
  };
  const appCard = (name) => page.locator(`.app-card[data-key="app-${name}"]`);
  const segment = (scope, value) => scope.locator('label.segment', { has: page.locator(`input[value="${value}"]`) });
  const hasClass = async (locator, name) => ((await locator.getAttribute('class')) || '').split(/\s+/).includes(name);
  /** Press the button of the sheet that says yes, and wait for the sheet to go. */
  const agree = async () => {
    const confirm = walk.sheetWith('.sheet-foot');
    await confirm.locator('.sheet-foot .btn-primary').click();
    await walk.noSheet();
  };
  /** Add one app in Pro's sheet. `pick` chooses it; the sheet that shows how to connect it is closed. */
  const addApp = async (opener, pick) => {
    await page.locator(opener).click();
    const add = walk.sheetWith('#add-app-name');
    await pick(add);
    await add.locator('button[type="submit"]').click();
    const connect = walk.sheetWith('#connect-host');
    await connect.locator('.config pre.code').first().waitFor({ state: 'visible', timeout: WAIT });
    await connect.locator('.sheet-foot .btn-primary').click();
    await walk.noSheet();
  };
  const known = (add, id) => add.locator('label.choice', { has: page.locator(`input[value="${id}"]`) }).click();
  const grant = async (app, collection, mode) => {
    const choice = segment(appCard(app).locator(`.grant-row[data-key="g-${collection}"]`), mode);
    await choice.click();
    await agree();
    await eventually(() => hasClass(choice, 'is-selected'), WAIT, `the card does not show ${mode} for ${collection}`);
  };
  const row = (name) => page.locator(`div.basic-app[data-key="row-app-${name}"]`);
  let mark = 0; // what the page had sent when the view was switched to Basic
  let limit = 0;

  await step('D: started with "--view pro", "Create the vault" opens Overview, and the vault is set up in Pro: two groups, three apps, a limit, a pause, a word', async () => {
    await pair(walk);
    await create(walk);
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/overview', `after "Create the vault" the address is ${walk.address()}`);
    const kept = (await walk.state()).interface;
    check(kept.view === 'pro' && kept.setup === 'open' && kept.stored === true, `a vault made with "--view pro" starts as ${JSON.stringify(kept)}`);
    await page.locator('.card-start').waitFor({ state: 'visible', timeout: WAIT });
    check((await walk.count('.visit-bar')) === 0 && (await walk.count('ol.steps')) === 0, 'Pro shows a step of the first run, or the bar of a visit');

    // Two groups of documents. "documents" is opened to apps that send text to a provider and shows its titles; "tax" carries a note and stays closed.
    await go('documents');
    await page.setInputFiles('#files-documents', files.flat);
    await eventually(async () => (await walk.count('.upload.upload-done')) === 1, 60000, 'the first file was not sealed');
    await page.locator('.page-actions button').click();
    const sheet = walk.sheetWith('#collection-name');
    await sheet.locator('#collection-name').fill('tax');
    await sheet.locator('#collection-note').fill('Tax notices');
    await sheet.locator('button[type="submit"]').click();
    await page.locator('.collection[data-key="c-tax"]').waitFor({ state: 'visible', timeout: WAIT });
    await walk.noSheet();
    await page.setInputFiles('#files-tax', files.boiler);
    await eventually(async () => (await page.locator('.collection[data-key="c-tax"] .upload.upload-done').count()) === 1, 60000, 'the second file was not sealed');
    await page.locator('label[for="hosted-documents"]').click();
    await agree();
    await eventually(() => page.locator('#hosted-documents').isChecked(), WAIT, '"documents" was not opened');
    await page.locator('label[for="titles-documents"]').click();
    await eventually(() => page.locator('#titles-documents').isChecked(), WAIT, 'the titles of "documents" are not shown');

    // Three apps: two that send text to a provider, one the owner declares local.
    await go('apps');
    await addApp('.card-empty .btn-primary', (add) => known(add, DESKTOP));
    await addApp('.page-actions .btn-primary', (add) => known(add, CODE));
    await addApp('.page-actions .btn-primary', async (add) => {
      await known(add, 'generic');
      await add.locator('label.radio', { has: page.locator('input[name="add-app-kind"][value="local"]') }).click();
      await add.locator('#add-app-name').fill(LOCAL);
    });
    // Different access for each group and each app.
    await grant(DESKTOP, 'documents', 'read');
    await grant(CODE, 'documents', 'approve');
    await grant(LOCAL, 'tax', 'approve');
    // One app is paused, one has a limit of its own.
    await page.locator(`label[for="answering-${CODE}"]`).click();
    await eventually(async () => (await walk.state()).serving.paused.includes(CODE), WAIT, 'the app was not paused');
    limit = (await walk.apps()).apps.find((entry) => entry.name === DESKTOP).limits.chars_per_day - 5000;
    await appCard(DESKTOP).getByRole('button', { name: 'Limits' }).click();
    const limits = walk.sheetWith('#limit-chars_per_day');
    await limits.locator('#limit-chars_per_day').fill(String(limit));
    await limits.locator('button[type="submit"]').click();
    await walk.noSheet();
    // One word that must never leave.
    await go('vault');
    await page.locator('#replace-word').fill('Sample-Account-4711');
    await page.locator('form.word-form button[type="submit"]').click();
    await eventually(async () => (await walk.get('/api/settings')).replaced_strings === 1, WAIT, 'the word was not added');
    // The fact about where the view is kept stands on this page.
    const facts = (await page.locator('.card[data-key="location"] .fact').allTextContents()).map(trimmed);
    check(facts.some((fact) => fact.includes('Kept beside the vault, not sealed') && fact.includes('Which view you use, and whether setup was finished')), `the Vault page does not say where the view is kept: ${JSON.stringify(facts)}`);

    const now = await walk.state();
    const apps = await walk.apps();
    check(now.serving.kinds === 'hosted' && now.serving.choice_needed === true, `serving: ${JSON.stringify(now.serving)}`);
    check(JSON.stringify(apps.apps.map((entry) => [entry.name, entry.grants])) === JSON.stringify([[CODE, { documents: 'approve' }], [DESKTOP, { documents: 'read' }], [LOCAL, { tax: 'approve' }]]), `apps: ${JSON.stringify(apps.apps.map((entry) => [entry.name, entry.grants]))}`);
    check(JSON.stringify(apps.collections.map((entry) => [entry.name, entry.allow_hosted])) === JSON.stringify([['documents', true], ['tax', false]]), `collections: ${JSON.stringify(apps.collections)}`);
  });
  if (!walk.base) return;

  await step('D: in Basic every app is read-only: its access per group, "Change in Pro", and the chips for a paused app and one that is not answered', async () => {
    await walk.noToast();
    mark = walk.sent.length;
    await switchView(page, 'basic');
    check(walk.address() === '#/basic' && (await walk.state()).interface.view === 'basic', `"Basic" leads to ${walk.address()}`);
    await row(LOCAL).waitFor({ state: 'visible', timeout: WAIT });
    check((await walk.count('button[data-key^="change-"]')) === 0 && (await walk.count('input[name="basic-mode"]')) === 0, 'Basic offers to change what an app may do in a vault with several groups');
    const closed = 'tax: No access (not open to apps that send text to a provider)';
    const expected = {
      [CODE]: { access: 'Different for each group', lines: ['documents: Ask me each time', closed], chips: ['Paused'], idle: true },
      [DESKTOP]: { access: 'Different for each group', lines: ['documents: Can read', closed], chips: [], idle: false },
      [LOCAL]: { access: 'Different for each group', lines: ['documents: No access', 'tax: Ask me each time'], chips: ['Not answered in this session'], idle: true },
    };
    for (const [name, wanted] of Object.entries(expected)) {
      const line = row(name);
      const shown = {
        access: trimmed(await line.locator('.basic-app-access').textContent()),
        lines: (await line.locator('ul.basic-app-groups li').allTextContents()).map(trimmed),
        // The first chip of a row says where the text goes; any other says why the app gets no answer.
        chips: (await line.locator('.basic-app-line .chip').allTextContents()).map(trimmed).slice(1),
        idle: await hasClass(line, 'is-idle'),
      };
      check(JSON.stringify(shown) === JSON.stringify(wanted), `the row of ${name} reads ${JSON.stringify(shown)}`);
      const change = line.locator(`a[data-key="change-${name}"]`);
      check(trimmed(await change.textContent()) === 'Change in Pro' && (await change.getAttribute('href')) === '#/apps', `the row of ${name} lacks "Change in Pro"`);
      check((await line.locator('button, input, select').count()) === 0, `the row of ${name} has a control`);
    }
    // The figure of a row is that app's own limit for 24 hours, and the row says that it is not the whole account.
    check(trimmed(await row(DESKTOP).locator('.basic-app-used span').first().textContent()) === `In its last 24 hours: 0 answers, 0 of ${limit.toLocaleString('en-US')} characters`, `the second line of the row reads: ${trimmed(await row(DESKTOP).locator('.basic-app-used').textContent())}`);
    check(trimmed(await row(DESKTOP).locator('.basic-app-others').textContent()) === 'Other limits apply too. They are set in Pro.' && (await row(CODE).locator('.basic-app-others').count()) === 0, 'the rows do not tell apart the app with limits of its own');
    const add = page.locator('a#basic-add-app');
    check(trimmed(await add.textContent()) === 'Add an app (in Pro)' && (await add.getAttribute('href')) === '#/apps' && !(await hasClass(add, 'btn')), `under the rows the page offers: ${trimmed(await add.textContent())}`);
    // No strip that asks which kinds of apps are answered, no switch, no field for a limit.
    check((await walk.count('.strip')) === 0 && (await walk.count('.basic .switch')) === 0 && (await walk.count('.basic input[type="number"]')) === 0, 'Basic shows a control for the kinds of apps, for pausing or for limits');
    // No app has asked yet, so setting up is still open: its line is there, and its button is the one filled button of the page.
    const setup = page.locator('section.card-setup[data-key="setup"]');
    check(trimmed(await setup.locator('.card-aside').textContent()) === TEXT.done(3) && trimmed(await setup.locator('p.setup-next').textContent()) === TEXT.nextQuestion, `the line about setting up reads: ${trimmed(await setup.textContent())}`);
    check(JSON.stringify(await page.locator('.btn-primary').evaluateAll((nodes) => nodes.map((node) => node.id))) === JSON.stringify(['setup-continue']), 'the filled buttons of the page are not the one of the line about setting up');
  });

  await step('D: "Also set for this vault, in Pro" says each thing Pro set, by the page of Pro where it is set', async () => {
    const also = page.locator('section[data-key="basic-also"]');
    await also.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await also.locator('p.basic-hint').first().textContent()) === TEXT.setInPro, `the part begins: ${trimmed(await also.locator('p.basic-hint').first().textContent())}`);
    const groups = await also.locator('.also-group').evaluateAll((nodes) =>
      nodes.map((node) => ({
        title: node.querySelector('.mini-title').textContent.trim(),
        link: `${node.querySelector('a').dataset.key} ${node.querySelector('a').getAttribute('href')} ${node.querySelector('a').textContent.trim()}`,
        lines: Array.from(node.querySelectorAll('ul.also-list li')).map((line) => line.textContent.trim()),
      })),
    );
    const wanted = [
      {
        title: 'Under Apps',
        link: 'also-apps #/apps Open in Pro',
        lines: [
          `${DESKTOP} has limits of its own.`,
          `${CODE} is paused until the vault locks.`,
          'This vault has apps that send text to a provider and apps that run on this computer. In this session only the provider apps are answered. Which kinds are answered is chosen in Pro after each unlock.',
        ],
      },
      {
        title: 'Under Documents',
        link: 'also-documents #/documents Open in Pro',
        lines: [
          'The documents are in 2 groups (collections in Pro). What an app may do is set for each group.',
          '“tax” carries a note that apps are shown.',
          'Apps are shown the titles of documents in “documents”.',
          '“tax” is not open to apps that send text to a provider.',
        ],
      },
      { title: 'Under Vault', link: 'also-vault #/vault Open in Pro', lines: ['1 word or number is replaced with [REDACTED] in everything that leaves.'] },
    ];
    check(JSON.stringify(groups) === JSON.stringify(wanted), `the part reads ${JSON.stringify(groups)}`);
    // The count names the groups, and the sentence about where text goes is the one for several apps.
    check((await walk.text('p.basic-count')) === '2 documents in 2 groups (collections in Pro): documents (1), tax (1).', `the page counts: ${await walk.text('p.basic-count')}`);
    check((await walk.text('section[data-key="basic-documents"] #docs-sentence')) === TEXT.general, `where text goes is said as: ${await walk.text('section[data-key="basic-documents"] #docs-sentence')}`);
    await page.locator('#basic-docs-toggle').click();
    const table = page.locator('table.doc-table');
    await table.waitFor({ state: 'visible', timeout: WAIT });
    check(JSON.stringify((await table.locator('tbody .doc-group').allTextContents()).map(trimmed)) === JSON.stringify(['documents', 'tax']), 'the table does not name the group of each document');
    const hints = (await page.locator('section[data-key="basic-documents"] p.basic-hint').allTextContents()).map(trimmed);
    check(JSON.stringify(hints) === JSON.stringify(["In “tax”, apps are shown a document's number, not its file name."]), `under the table the page says: ${JSON.stringify(hints)}`);
    await page.locator('#basic-docs-toggle').click();
    await walk.picture('b11-basic-set-in-pro', { whole: true });
  });

  await step('D: a chosen file is held until the owner says which group it goes to, with no group chosen for them; "Cancel" sends nothing', async () => {
    check((await walk.count('.drop[data-key="drop-basic"]')) === 1 && (await walk.count('.drop')) === 1, 'the page has not exactly one place for files');
    // The files this page added in Pro are listed under the zone, each with its group, and each group says who can ask for them.
    const listed = (await page.locator('.drop li.upload').evaluateAll((nodes) => nodes.map((node) => `${node.querySelector('.upload-name').textContent} in ${node.querySelector('.upload-group').textContent}`)));
    check(JSON.stringify(listed) === JSON.stringify(['flat-notes.txt in documents', 'boiler.md in tax']), `under the zone the page lists ${JSON.stringify(listed)}`);
    const whoCanAsk = (await page.locator('.drop p.upload-who').allTextContents()).map(trimmed);
    check(whoCanAsk.length === 2 && whoCanAsk[0].includes(`${CODE} (Ask me each time; paused)`) && whoCanAsk[0].includes(`${DESKTOP} (Can read: passages of it can leave without asking)`) && whoCanAsk[1].includes(`${LOCAL} (Ask me each time; not answered in this session)`), `after the files of each group the page says: ${JSON.stringify(whoCanAsk)}`);
    await page.setInputFiles('#files-basic', files.rules);
    const sheet = walk.sheetWith('input[name="basic-group"]');
    await sheet.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await sheet.locator('.sheet-title').textContent()) === TEXT.addToWhich && trimmed(await sheet.locator('.sheet-files').textContent()) === '1 file: house-rules.txt', `the sheet reads: ${trimmed(await sheet.locator('.sheet-title').textContent())} / ${trimmed(await sheet.locator('.sheet-files').textContent())}`);
    const groups = sheet.locator('input[name="basic-group"]');
    check((await groups.evaluateAll((nodes) => nodes.map((node) => `${node.value}${node.checked ? '*' : ''}`))).join(',') === 'documents,tax', 'a group is chosen for the owner, or the groups are not the two');
    const add = sheet.locator('.sheet-foot .btn-primary');
    check((await add.isDisabled()) && trimmed(await add.textContent()) === 'Add' && (await focused(sheet.locator('.sheet-foot button').first())), '"Add" can be pressed before a group is chosen, or the focus is not on "Cancel"');
    // Each group says who can ask for what is in it, and an app that gets no answer right now is said to be so.
    const who = (await sheet.locator('.choice-hint').allTextContents()).map(trimmed);
    check(JSON.stringify(who) === JSON.stringify([`${CODE} (Ask me each time; paused), ${DESKTOP} (Can read: passages can leave without asking)`, `${LOCAL} (Ask me each time; not answered in this session)`]), `the groups say who can ask: ${JSON.stringify(who)}`);
    check((await sheet.locator('.note-amber').count()) === 0, 'the sheet speaks of where text goes before a group is chosen');
    await sheet.locator('label.choice', { has: page.locator('input[name="basic-group"][value="documents"]') }).click();
    await eventually(async () => !(await add.isDisabled()), WAIT, '"Add" stays dimmed after a group was chosen');
    check(trimmed(await sheet.locator('.note-amber').textContent()) === TEXT.general, `with a group chosen the sheet says: ${trimmed(await sheet.locator('.note-amber').textContent())}`);
    await sheet.locator('.sheet-foot button').first().click();
    await walk.noSheet();
    check((await walk.state()).encryption.documents === 2 && (await walk.count('.drop li.upload')) === listed.length, '"Cancel" added the file');
  });

  await step('D: a link into Pro is a visit: a bar leads back, nothing is stored, and Basic has sent nothing but questions', async () => {
    await page.locator('section[data-key="basic-also"] a[data-key="also-apps"]').click();
    await page.locator('.page-body[data-key="view-apps"]').waitFor({ state: 'attached', timeout: WAIT });
    const bar = page.locator('nav.visit-bar[data-key="visit"]');
    await bar.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await bar.locator('#visit-back').textContent()) === TEXT.backToBasic && trimmed(await bar.locator('.visit-text').textContent()) === TEXT.visit && trimmed(await bar.locator('#visit-stay').textContent()) === TEXT.stayInPro, `the bar reads: ${trimmed(await bar.textContent())}`);
    // The bar stands above the head of the page, and the view control shows what is on screen.
    check((await bar.boundingBox()).y + (await bar.boundingBox()).height <= (await page.locator('.page-title').boundingBox()).y, 'the bar of the visit lies over the title of the page');
    check(await page.locator('.sidebar-foot input[name="view"][value="pro"]').isChecked(), 'during a visit the view control does not show "Pro"');
    check((await walk.state()).interface.view === 'basic', 'a link from Basic stored a view');
    // Both kinds of apps exist: Pro asks which are answered, on the page the link led to.
    await page.locator('.strip.strip-amber').waitFor({ state: 'visible', timeout: WAIT });
    await walk.picture('b12-visit-bar');
    await bar.locator('#visit-back').click();
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === '#/basic' && (await walk.state()).interface.view === 'basic', `"Back to Basic" leads to ${walk.address()}`);
    // From the switch to Basic until now: the one request that kept the view, and questions.
    check(JSON.stringify(walk.changesSince(mark)) === JSON.stringify(['POST /api/interface']), `since the switch to Basic the page sent ${JSON.stringify(walk.changesSince(mark))}`);
  });

  await step('D: on a second visit "Stay in Pro" stores the view, and the bar goes', async () => {
    const before = walk.sent.length;
    await row(DESKTOP).locator(`a[data-key="change-${DESKTOP}"]`).click();
    await page.locator('.page-body[data-key="view-apps"]').waitFor({ state: 'attached', timeout: WAIT });
    const bar = page.locator('nav.visit-bar[data-key="visit"]');
    await bar.locator('#visit-stay').click();
    await eventually(async () => (await bar.count()) === 0, WAIT, 'the bar of the visit stayed');
    check(walk.address() === '#/apps' && (await walk.state()).interface.view === 'pro', `"Stay in Pro" left the page at ${walk.address()}`);
    check(JSON.stringify(walk.changesSince(before)) === JSON.stringify(['POST /api/interface']), `"Stay in Pro" sent ${JSON.stringify(walk.changesSince(before))}`);
    check((await walk.count('#toasts .toast')) === 0, '"Stay in Pro" left a message');
    await reload(page);
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: 40000 });
    check((await walk.count('.visit-bar')) === 0, 'after a reload the bar of a visit is back');
  });

  await step('D: in all of this the page asked no address but the app\'s and kept nothing in the browser but its session', async () => {
    await leftNothingBehind(walk);
  });

  await step('D: at the end the app quits with status 0 and an empty standard error', async () => {
    await end(walk);
  });
}

// =====================================================================================
// Run E: Claude Code as offered, and a busy day
// =====================================================================================

async function runE(browser, work, files) {
  const walk = await begin(browser, work, 'e');
  const { page, counter } = walk;
  const stepCard = walk.card('step-4');
  const key = (ref) => `ap-${CODE}-${ref}`;
  let doc = '';

  await step('E: the first run for Claude Code: one line for a terminal in place of a settings file, and the path costs no more than it may', async () => {
    await pair(walk);
    await create(walk);
    const form = page.locator('form.gate-card[data-key="step-3"]');
    await form.waitFor({ state: 'visible', timeout: 40000 });
    await counter.start();
    await form.locator('label.choice', { has: page.locator(`input[value="${CODE}"]`) }).click();
    await form.locator('#first-add:not([disabled])').click();
    const docsCard = walk.card('step-2');
    await docsCard.waitFor({ state: 'visible', timeout: 40000 });
    await chooseFiles(walk, docsCard, files.rules);
    await eventually(async () => (await docsCard.locator('li.upload.upload-done').count()) === 1, 60000, 'the file was not sealed');
    check((await walk.text('p.upload-who')) === TEXT.whoAsks(CODE), `after the file the page says: ${await walk.text('p.upload-who')}`);
    await docsCard.locator('.gate-actions .btn-primary').click();
    await stepCard.locator('li.ask-step[data-key="ask-3"]').waitFor({ state: 'visible', timeout: 40000 });
    const known = (await walk.apps()).known.find((entry) => entry.id === CODE);
    const connect = await walk.get(`/api/apps/${CODE}/connect?host=${CODE}`);
    const first = stepCard.locator('li.ask-step[data-key="ask-1"]');
    // The line is the app's own first block, and nothing on this shape looks at a file.
    const line = await first.locator('.copy-line pre.code').textContent();
    check(line === connect.blocks[0].text && line.includes(`--client ${CODE} --store ${walk.store}`) && !line.includes(PASSPHRASE), `the line for the terminal reads: ${line}`);
    const terminal = connect.platform === 'macos' ? `Open a new Terminal window (⌘N; the first one is busy running ${walk.product}), paste this line and press Return:` : 'Open a new terminal window, paste this line and press Return:';
    check(trimmed(await first.locator('.ask-step-body > p').first().textContent()) === terminal, `the first line begins: ${trimmed(await first.locator('.ask-step-body > p').first().textContent())}`);
    check((await walk.count('#ask-check')) === 0 && trimmed(await first.textContent()).endsWith(TEXT.notTried(known.label)), 'the step offers to look at a file, or lacks the note about what was tried');
    check(!TEXT.protocol.test(await stepCard.innerText()), 'the step names the protocol or the address of a page');
    check(trimmed(await stepCard.locator('li.ask-step[data-key="ask-3"] .ask-step-title').textContent()) === connect.after.title, 'the third line does not carry the title the app sends for it');
    await pressCopy(page, options, first.locator('#ask-copy-line'), line);
    await pressCopy(page, options, stepCard.locator('#ask-copy'), TEXT.askStart(walk.product));
    // The other way to add it is one press away, and not on the path that is counted.
    await counter.aside(async () => {
      await first.locator('#ask-other-way').click();
      const other = first.locator('.ask-block');
      await other.waitFor({ state: 'visible', timeout: WAIT });
      check(trimmed(await other.textContent()).endsWith('Use one of the two, not both.') && (await other.locator('#ask-copy-other').count()) === 1, `the other way reads: ${trimmed(await other.textContent())}`);
    });
    check(walk.agent(CODE, 'status').code === 'OK', 'the question of the app was not answered');
    await stepCard.locator('p.ask-wait.ask-arrived').waitFor({ state: 'visible', timeout: WAIT });
    check(TEXT.arrived(CODE).test(await walk.text('p.ask-wait.ask-arrived')), `about the first request the step says: ${await walk.text('p.ask-wait.ask-arrived')}`);
    await counter.stop();
    judgeCount(walk, CODE);
  });
  if (!walk.base) return;

  await step('E: locked under the step "First question", the vault opens on the Basic page, and the sheet of the app leads back to the step', async () => {
    await lockElsewhere(page, walk.base);
    await unlock(walk);
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: 40000 });
    check(walk.address() === '#/basic', `after "Unlock" the address is ${walk.address()}`);
    // The first request ended setting up, so no line about it is left; the way back to the step is in the sheet of the app.
    const row = page.locator(`div.basic-app[data-key="row-app-${CODE}"]`);
    await row.waitFor({ state: 'visible', timeout: WAIT });
    check((await walk.count('.card-setup')) === 0 && (await walk.state()).interface.setup === 'done', 'the Basic page still asks to finish setting up');
    await row.locator(`button[data-key="change-${CODE}"]`).click();
    const sheet = walk.sheetWith('input[name="basic-mode"]');
    await sheet.locator('#basic-steps').click();
    await walk.noSheet();
    await stepCard.locator('p.ask-wait.ask-arrived').waitFor({ state: 'visible', timeout: WAIT });
    check(walk.address() === `#/setup/connect?app=${CODE}`, `"Show the connection steps" leads to ${walk.address()}`);
    // Opened from the Basic page, the step is no part of setting up: no stepper, the three lines folded, a way back.
    check((await walk.count('ol.steps')) === 0 && (await stepCard.locator('ol.ask-steps').count()) === 0, 'the step still looks like a part of setting up');
    check((await walk.text('p.ask-wait.ask-arrived')).includes(`under the name ${CODE}`) && (await stepCard.locator('.ask-end #ask-open').count()) === 1, 'the step does not say that the app has asked, or offers no way back');
    await stepCard.locator('#ask-steps-toggle').click();
    await stepCard.locator('li.ask-step[data-key="ask-1"] #ask-copy-line').waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('#ask-steps-toggle').getAttribute('aria-expanded')) === 'true', 'the button of the unfolded lines does not say that they are shown');
  });

  await step('E: five passages wait: three cards and "Show the other 2", on the step and on the Basic page, with nothing filled', async () => {
    const search = walk.agent(CODE, 'search', 'cellar balcony parcels', 'documents');
    check(search.code === 'OK' && REF.test(search.text), `search: ${JSON.stringify(search)}`);
    doc = REF.exec(search.text)[1];
    const held = (await walk.get('/api/documents')).collections[0].documents[0];
    check(held.passages >= 5, `the document has ${held.passages} passages, fewer than five`);
    for (let passage = 1; passage <= 5; passage += 1) check(walk.agent(CODE, 'read', `${doc}.${passage}`).code === 'NEEDS_APPROVAL', `passage ${passage} did not need approval`);
    const onStep = stepCard.locator('.approval-stack');
    await eventually(async () => (await onStep.locator('> .approval').count()) === 3 && (await walk.count('#ask-more-approvals')) === 1, WAIT, 'the step does not show three cards and a button for the others');
    check(trimmed(await page.locator('#ask-more-approvals').textContent()) === 'Show the other 2' && trimmed(await stepCard.locator('h2.ask-heading').textContent()) === 'It asks for 5 passages. This is the exact text of each.', `above and under the cards the step says: ${trimmed(await stepCard.locator('h2.ask-heading').textContent())} / ${trimmed(await page.locator('#ask-more-approvals').textContent())}`);
    check((await walk.count('.btn-primary')) === 0, 'a button is filled while passages wait');
    await stepCard.locator('.gate-actions #ask-open').click();
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: WAIT });
    const stack = page.locator('section[data-key="basic-approvals"] .approval-stack');
    await eventually(async () => (await stack.locator('> .approval').count()) === 3, WAIT, 'the Basic page does not show three cards');
    check((await walk.text('h1#basic-status')) === TEXT.waits(5) && (await page.title()).startsWith('(5) '), `with five passages waiting the headline reads "${await walk.text('h1#basic-status')}" and the tab "${await page.title()}"`);
    check(trimmed(await page.locator('#basic-more-approvals').textContent()) === 'Show the other 2' && (await walk.count('.btn-primary')) === 0, 'the page lacks the button for the other cards, or a button is filled');
    await stack.locator('> .approval button[data-key="release"]:not(.is-waiting)').first().waitFor({ state: 'visible', timeout: WAIT });
    await walk.picture('b16-basic-busy', { whole: true });
  });

  await step('E: "Show the other 2" makes "Release once" wait again, and a press on it while it waits releases nothing', async () => {
    const stack = page.locator('section[data-key="basic-approvals"] .approval-stack');
    const first = stack.locator(`> .approval[data-key="${key(`${doc}.1`)}"]`);
    const top = await topOf(first);
    await waitsAgain(
      walk,
      first,
      'the other cards were shown',
      async () => {
        await page.locator('#basic-more-approvals').click();
        await eventually(async () => (await stack.locator('> .approval').count()) === 5, WAIT, 'the other two cards were not shown');
      },
      { press: true },
    );
    check((await walk.count('#basic-more-approvals')) === 0 && (await topOf(first)) === top, 'the button stayed, or the first card moved');
    // One is released: it stands as approved and not read, and can be withdrawn.
    await decideApproval(page, key(`${doc}.1`), 'release');
    const granted = stack.locator('.granted-list li');
    await granted.waitFor({ state: 'visible', timeout: WAIT });
    await says(page.locator('h1#basic-status'), TEXT.waits(4), 'with one of five released the headline reads');
    check(trimmed(await granted.locator('button').textContent()) === 'Withdraw', 'the approval cannot be withdrawn');
    await granted.locator('button').click();
    await eventually(async () => (await walk.approvals()).granted.length === 0 && (await stack.locator('.granted-list').count()) === 0, WAIT, 'the approval was not withdrawn');
    // Each of the others has its own answer; nothing answers several at once.
    check((await stack.locator('> .approval button[data-key="release"]').count()) === 4 && (await page.locator('.basic-approvals button').filter({ hasText: /all/i }).count()) === 0, 'the page offers one answer for several passages');
    const now = await walk.state();
    check(now.today.approvals_waiting === 4 && now.today.releases === 1, `after the walk through the cards: ${JSON.stringify(now.today)}`);
  });

  await step('E: in all of this the page asked no address but the app\'s and kept nothing in the browser but its session', async () => {
    await leftNothingBehind(walk);
  });

  await step('E: at the end the app quits with status 0 and an empty standard error', async () => {
    await end(walk);
  });
}

// =====================================================================================

run(async () => {
  deadline(12);
  const work = shortDirectory('gb-');
  const files = writeFiles(path.join(work, 'files'));
  const browser = await openBrowser(options, 'basic-');
  if (RUNS.includes('A')) await runA(browser, work, files);
  if (RUNS.includes('B')) await runB(browser, work);
  if (RUNS.includes('C')) await runC(browser, work);
  if (RUNS.includes('D')) await runD(browser, work, files);
  if (RUNS.includes('E')) await runE(browser, work, files);
  await finish(browser);
});
