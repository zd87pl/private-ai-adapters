// A new vault, set up and used in Pro, walked in a browser against the real app.
//
// Run it from the top of the repository (after "npm install --no-save playwright@1.63.0"
// and "npx playwright install chromium"):
//
//   GATE_PYTHON=.venv/bin/python node tests/browser/journey_real.js
//
// Options (environment), all optional:
//   GATE_PYTHON         the Python that has wdva installed (default: python3)
//   GATE_BROWSER        chromium (default), webkit or firefox
//   GATE_SHOTS          where the screenshots go (default: shots/ next to this file)
//   GATE_SCALE          device scale factor of the screenshots (default: 1)
//   GATE_PORT           port of the interface (default: the app picks a free one)
//   GATE_OWNER_SECONDS  how long owner mode lasts in this run (default: 100; the app's own value is 600)
//
// The app is started WITHOUT --demo on an empty directory with a short path under
// /tmp, through launch_app.py, which changes one constant (how long owner mode
// lasts) and gives the app an empty home of its own. Calls of an agent application
// are made by agent_call.py through RemoteGate, the path the program an application
// starts uses.
//
// The walk pairs, creates the vault, skips the two steps of the first run that can be
// skipped, and switches from the Basic page, where a new vault opens, to Pro. From
// there on it is a walk through the six pages of Pro: documents, apps, grants, an
// agent's calls, approvals, the passphrase asked for again, lock and unlock, Ctrl-C
// and a restart. The first run as the page offers it, and the Basic page, are walked
// by journey_basic.js.
//
// Every step prints "ok" or "FAIL". Exit status 1 when a step failed, the page
// logged an error, a request failed, or an HTTP error came that no step announced.

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
  newPage,
  note,
  openBrowser,
  pageReachedApp,
  pressCopy,
  readOptions,
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

const options = readOptions({ ownerSeconds: 100 });
const PASSPHRASE = 'correct horse battery staple';
const HOSTED = 'claude-desktop'; // a known application: its text goes to a provider
const LOCAL = 'lm-studio'; // an app the owner declares local
const TAX_LINE = 'The tax refund for 2025 is 386 EUR and arrives within four weeks.';
const REF = /ref=(d-[0-9a-f]{12}\.\d+)/;
/** Where a row of the activity list is pressed: near its start, which the details beside the list never cover. */
const AT_ITS_START = { position: { x: 24, y: 12 } };

/** The few words of the interface this walk reads. Everything else is found by role, id or class. */
const TEXT = {
  question: 'Question', // the chip of a call that asked what the app may use
  afterApproval: 'Released after approval', // the chip of a read the owner approved
  connect: 'Connect', // two buttons on an app's card
  limits: 'Limits',
  terminal: /terminal/i, // where the pairing page sends the owner for the code
  noRecovery: /no recovery/i, // beside the new passphrase
  notEncrypted: /not encrypted/i, // about the files the owner chose, after adding them
  cannotBeRecalled: /cannot be recalled/i, // wherever text is given to a hosted app
  cannotCheck: /cannot check/i, // about an app declared local
  runningAsYou: /any program running as you/i, // before both kinds of apps are answered together
  cannotTakeBack: /cannot take it back/i, // under text that has left
  unverified: /unverified/i, // beside the name an app gives itself
  although: /although/i, // why a passage waits although the app can read the collection
  originalsKept: /originals? kept/i, // what the vault screen must not say
  steps: ['Passphrase', 'App', 'Documents', 'First question'], // the four parts of setting up
};

/** Fragments of what an agent is told. These texts are fixed in wdva.gate.messages. */
const AGENT = {
  withSnippets: 'search with snippets',
  refsOnly: 'search returns refs only',
  withheld: 'text withheld',
  noCollections: 'Collections: none.',
};

function tinyPdf(line) {
  const stream = `BT /F1 12 Tf 72 720 Td (${line}) Tj ET`;
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let out = '%PDF-1.4\n';
  const offsets = [];
  objects.forEach((body, index) => {
    offsets.push(out.length);
    out += `${index + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) out += `${String(offset).padStart(10, '0')} 00000 n \n`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}

/** The three files the owner adds. `flat-notes.txt` is long enough for three passages. */
function writeFiles(directory) {
  const flat = [
    'Notes about the flat',
    'The monthly rent is 1,180 EUR and is due on the third working day of each month. It is paid by standing order to the cooperative. The deposit was 2,360 EUR and lies in a separate account until the flat is handed back.',
    'The caretaker is reached on weekday mornings. Meter readings are announced a week ahead on the board in the staircase. The laundry room is booked on the list next to its door, and the bicycle space in the yard is for one bicycle.',
    'Quiet hours are from ten in the evening until seven in the morning. Waste is sorted into the bins in the yard. The staircase is kept free of furniture, prams and shoes, and smoking is not allowed in the staircase or in the cellar.',
    'Small repairs of up to 110 EUR each are paid by the tenant, at most 350 EUR in one year. Damage is reported without delay. Shelves, lamps and curtain rails may be put up without asking; changes to walls, floors, pipes or wiring need consent.',
    'Either party may end the lease with three months of written notice, counted from the first day of the month after the notice was received. Notice is given by letter; a message on the notice board or by telephone does not count.',
    'The service charges are settled each spring. The tenant pays 95 EUR a month in advance towards lighting, cleaning, the lift, the garden and the insurance of the building, and may look at the invoices in the office on any Tuesday afternoon.',
    'Liability insurance is held for the whole term. The landlord insures the building; the belongings of the tenant are not covered by that insurance. A lost key is reported at once, and further keys are ordered through the cooperative only.',
  ];
  fs.mkdirSync(directory);
  const files = {
    flat: path.join(directory, 'flat-notes.txt'),
    boiler: path.join(directory, 'boiler.md'),
    tax: path.join(directory, 'tax-2025.pdf'),
    picture: path.join(directory, 'picture.png'),
  };
  fs.writeFileSync(files.flat, `${flat.join('\n\n')}\n`);
  fs.writeFileSync(files.boiler, '# Boiler\n\nThe boiler was serviced in March. The next service is due in March of next year.\nThe heating engineer left a card in the kitchen drawer.\n');
  fs.writeFileSync(files.tax, tinyPdf(TAX_LINE));
  fs.writeFileSync(files.picture, Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  return files;
}

run(async () => {
  deadline(10);
  const work = shortDirectory('gj-');
  const store = path.join(work, 'vault');
  const files = writeFiles(path.join(work, 'files'));

  const browser = await openBrowser(options, '');
  let app = startApp(options, { args: ['--store', store] });
  let page = await newPage(browser, options);
  let base = '';
  let product = '';
  let code = '';

  const get = (pathname) => apiGet(page, base, pathname);
  const state = () => get('/api/state');
  const activity = async () => (await get('/api/activity?limit=200')).items;
  const hostedApp = async () => (await get('/api/apps')).apps.find((entry) => entry.name === HOSTED);
  const agent = (directory, client, ...rest) => agentCall(options, directory, client, ...rest);
  const picture = (name) => shot(page, name);
  const count = (selector) => page.locator(selector).count();
  const go = async (route) => {
    await page.click(`a.nav-item[href="#/${route}"]`);
    await page.locator(`.page-body[data-key="view-${route}"]`).waitFor({ state: 'attached', timeout: WAIT });
  };
  /** The open sheet that holds the element `selector` names. */
  const sheetWith = (selector) => page.locator('dialog[open]', { has: page.locator(selector) });
  const noSheet = () => eventually(async () => (await count('dialog[open]')) === 0, WAIT, 'a sheet stayed open');
  const appCard = (name) => page.locator(`.app-card[data-key="app-${name}"]`);
  const row = (seq) => page.locator(`li.act-item[data-key="a${seq}"]`);
  const segment = (scope, value) => scope.locator('label.segment', { has: page.locator(`input[value="${value}"]`) });
  const hasClass = async (locator, name) => ((await locator.getAttribute('class')) || '').split(/\s+/).includes(name);
  /** Close the details beside the activity list: with the Escape key, or with the button in their corner. */
  const closePanel = async (how = 'button') => {
    if (how === 'Escape') await page.keyboard.press('Escape');
    else await page.locator('#panel .panel-head button').click();
    await eventually(async () => (await count('#panel')) === 0, WAIT, how === 'Escape' ? 'the Escape key did not close the details' : 'the details did not close');
  };
  /** The step of the getting-started list that is next, counted from 1, and where its button leads. */
  const nextStart = async () => {
    const steps = page.locator('.card-start .start-step');
    const total = await steps.count();
    for (let index = 0; index < total; index += 1) {
      if (await hasClass(steps.nth(index), 'is-next')) return { number: index + 1, total, href: await steps.nth(index).locator('a.btn').getAttribute('href') };
    }
    return { number: 0, total, href: null };
  };
  const startIsAt = (number, href) =>
    eventually(async () => {
      const next = await nextStart();
      check(next.total === 4 && next.number === number && next.href === href, `the getting-started list is at ${JSON.stringify(next)}, not at step ${number} with ${href}`);
      return true;
    });

  /**
   * A change that lets more leave. On a slow machine owner mode may have run out by
   * now: the app's answer to the request says so, and then the sheet that asks for
   * the passphrase is answered. `press` sends the change; `pathname` is where it goes.
   */
  const widening = async (pathname, press) => {
    const isIt = (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === pathname;
    const announced = expectHttp(403, pathname, { optional: true });
    const first = page.waitForResponse(isIt, { timeout: WAIT });
    first.catch(() => {});
    await press();
    const answer = await first;
    if (answer.status() === 403) {
      const again = page.locator('#reauth-passphrase');
      await again.waitFor({ state: 'visible', timeout: WAIT });
      note(`owner mode had run out before the change at ${pathname}: the passphrase was asked for again and given`);
      const second = page.waitForResponse(isIt, { timeout: WAIT });
      second.catch(() => {});
      await again.fill(PASSPHRASE);
      await sheetWith('#reauth-passphrase').locator('button[type="submit"]').click();
      check((await second).status() < 400, `the change at ${pathname} was refused after the passphrase too`);
    } else {
      check(answer.status() < 400, `the change at ${pathname} was answered with ${answer.status()}`);
    }
    withdraw(announced);
  };

  // ------------------------------------------------------------------ start and pair
  await step('the terminal shows the start-up lines of a real vault', async () => {
    const lines = await started(app);
    base = app.url;
    check(lines.length === 5, `expected five lines, got ${JSON.stringify(lines)}`);
    check(lines[1] === `store: ${store}`, `store line: ${lines[1]}`);
    const found = /^pairing code: ([A-Z0-9]{4}-[A-Z0-9]{4})\s/.exec(lines[3]);
    check(found, `pairing line: ${lines[3]}`);
    code = found[1];
    check(!/passphrase/i.test(app.out), 'a real vault prints something about a passphrase');
    if (options.port) check(app.port === options.port, `the app listens on ${app.port}, not on ${options.port}`);
  });
  // Without the app there is nothing to walk.
  if (!base) await finish(browser);

  await step('the page asks for the pairing code and says where to find it', async () => {
    await page.goto(base);
    await page.locator('#pair-code').waitFor({ state: 'visible', timeout: WAIT });
    product = await page.locator('meta[name="application-name"]').getAttribute('content');
    const session = await get('/api/session');
    check(session.paired === false && session.pairing === 'code', `session: ${JSON.stringify(session)}`);
    check(product && session.name === product && (await page.title()) === product, `the page is called "${await page.title()}", the app "${session.name}"`);
    check(app.lines[0] === `${product} ${session.version}`, `the first line is not the name and the version: ${app.lines[0]}`);
    const lead = await page.locator('#pair-lead').textContent();
    check(lead.includes(product) && TEXT.terminal.test(lead), `the page does not say where the code is: ${lead}`);
    await picture('01-pair');
  });

  await step('a wrong code is refused in plain words, the right one pairs', async () => {
    const pair = page.locator('.gate-card button[type="submit"]');
    expectHttp(403, '/api/session/pair');
    await page.fill('#pair-code', code === 'AAAA-AAAA' ? 'BBBB-BBBB' : 'AAAA-AAAA');
    await pair.click();
    const refusal = page.locator('#pair-code-error');
    await refusal.waitFor({ state: 'visible', timeout: WAIT });
    const words = (await refusal.textContent()).trim();
    check(/\s/.test(words) && !/wrong_code|403/.test(words), `the refusal is not a sentence: ${words}`);
    // Typed the way a person might: lower case, a blank for the hyphen.
    await page.fill('#pair-code', code.toLowerCase().replace('-', ' '));
    await pair.click();
    await page.locator('#new-passphrase').waitFor({ state: 'visible', timeout: WAIT });
  });

  // ------------------------------------------------------------------ create the vault
  await step('creating the vault: the rules of the passphrase are said, the place is shown', async () => {
    const create = page.locator('.gate-card button[type="submit"]');
    check(TEXT.noRecovery.test(await page.locator('.gate-card .note-amber').textContent()), 'the page does not say that a lost passphrase cannot be recovered');
    check((await page.locator('.gate-where').textContent()).includes(store), `the page does not show where the vault is created: ${await page.locator('.gate-where').textContent()}`);
    await page.fill('#new-passphrase', 'too short');
    await eventually(async () => /\b9\b/.test(await page.locator('#new-passphrase-hint').textContent()), WAIT, 'the hint does not count the nine characters typed');
    check(await create.isDisabled(), 'a short passphrase can be sent');
    await page.fill('#new-passphrase', PASSPHRASE);
    await page.fill('#new-passphrase-again', `${PASSPHRASE}x`);
    await page.locator('#new-passphrase-again-error').waitFor({ state: 'visible', timeout: WAIT });
    check(await create.isDisabled(), 'two different entries can be sent');
    await page.fill('#new-passphrase-again', PASSPHRASE);
    await eventually(async () => (await count('#new-passphrase-again-error')) === 0, WAIT, 'the two entries still count as different');
    // The parts of setting up are named before the vault exists: four, and pairing is not one of them.
    const labels = (await page.locator('ol.steps .step-label').allTextContents()).map((label) => label.trim());
    check(JSON.stringify(labels) === JSON.stringify(TEXT.steps), `the parts of setting up are ${JSON.stringify(labels)}`);
    await picture('02-create');
    await create.click();
    // The step that follows is an address, and it is the one that adds an app.
    await page.locator('form.gate-card[data-key="step-3"]').waitFor({ state: 'visible', timeout: 40000 });
    check(new URL(page.url()).hash === '#/setup/app', `after "Create the vault" the address is ${page.url()}`);
    const now = await state();
    check(now.locked === false && now.store.exists === true, 'the vault is not unlocked after it was created');
    check(!('demo_passphrase' in now), 'a real vault sends a passphrase');
    check(now.interface.view === 'basic' && now.interface.setup === 'open', `a new vault starts as ${JSON.stringify(now.interface)}`);
  });

  await step('the first run can be skipped, and the empty overview says what to do next, in order', async () => {
    await picture('03-first-run-app');
    await page.locator('form.gate-card[data-key="step-3"] .gate-actions .btn-quiet').click();
    const second = page.locator('section.gate-card[data-key="step-2"]');
    await second.waitFor({ state: 'visible', timeout: WAIT });
    check(new URL(page.url()).hash === '#/setup/documents', `skipping the app step leads to ${page.url()}`);
    await picture('04-first-run-documents');
    await second.locator('.gate-actions .btn-quiet').click();
    // A new vault opens in the Basic view, where one line says what setting up still lacks. Skipping
    // stored nothing: no app, no document, and setting up still open.
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: WAIT });
    check(new URL(page.url()).hash === '#/basic', `skipping both steps leads to ${page.url()}`);
    const skipped = await state();
    check(skipped.interface.view === 'basic' && skipped.interface.setup === 'open', `after skipping: ${JSON.stringify(skipped.interface)}`);
    check(skipped.encryption.documents === 0 && (await get('/api/apps')).apps.length === 0, 'skipping added something to the vault');
    const next = page.locator('.card-setup[data-key="setup"] #setup-continue');
    await next.waitFor({ state: 'visible', timeout: WAIT });
    check((await next.getAttribute('href')) === '#/setup/app', `the line about setting up leads to ${await next.getAttribute('href')}`);
    // The walk from here on is through Pro: the view is switched once, without a message, and the
    // six pages are where they were.
    await switchView(page, 'pro');
    check((await state()).interface.view === 'pro', 'the switch did not keep the view');
    check((await count('.visit-bar')) === 0 && (await count('#toasts .toast')) === 0, 'the switch to Pro left a bar or a message');
    await page.locator('.card-start').waitFor({ state: 'visible', timeout: WAIT });
    await startIsAt(1, '#/documents');
    check((await count('.start-step.is-next a.btn')) === 1, 'more than one next step is offered');
    // No app yet: the card of what was sent offers to add one, and nothing waits.
    await page.locator('.card[data-key="left"] .empty a[href="#/apps"]').waitFor({ state: 'visible', timeout: WAIT });
    await page.locator('.card[data-key="waiting"] .quiet-line').waitFor({ state: 'visible', timeout: WAIT });
    await picture('05-overview-empty');
  });

  await step('the other screens of an empty vault each say one thing to do', async () => {
    await go('activity');
    // The record already has the owner's own entries, and nobody else's.
    await page.locator('.act-row').first().waitFor({ state: 'visible', timeout: WAIT });
    const items = await activity();
    check(items.length >= 1 && items.every((item) => item.app === null), `the record of a new vault: ${JSON.stringify(items.map((item) => item.summary))}`);
    await go('approvals');
    await page.locator('.card-empty .empty-text').waitFor({ state: 'visible', timeout: WAIT });
    await go('apps');
    await page.locator('.card-empty .btn-primary').waitFor({ state: 'visible', timeout: WAIT });
    check((await count('.card-empty .btn')) === 1, 'the empty Apps screen offers more than one thing to do');
    await go('vault');
    const location = page.locator('.card[data-key="location"]');
    await location.waitFor({ state: 'visible', timeout: WAIT });
    check((await location.textContent()).includes(store), 'the vault screen does not show where the vault is');
    check(!TEXT.originalsKept.test(await page.locator('#main').textContent()), 'the vault screen speaks of originals kept');
    await picture('06-vault-empty');
    await go('documents');
    await page.locator('.card-empty .drop').waitFor({ state: 'visible', timeout: WAIT });
    await picture('07-documents-empty');
  });

  // ------------------------------------------------------------------ documents
  await step('adding a .txt and a .md into "documents", and being told where the chosen files are', async () => {
    await page.setInputFiles('#files-documents', [files.flat, files.boiler]);
    await eventually(async () => (await count('.upload.upload-done')) === 2, 60000, 'the two files were not sealed');
    const card = page.locator('.collection[data-key="c-documents"]');
    await eventually(async () => (await card.locator('.doc-name').allTextContents()).sort().join(',') === 'boiler,flat-notes', WAIT, 'the two documents are not listed in their collection');
    check((await count('.upload-outside')) === 1, 'what is true of the chosen files is not said exactly once');
    check(TEXT.notEncrypted.test(await page.locator('.upload-outside').textContent()), `about the chosen files the page says: ${await page.locator('.upload-outside').textContent()}`);
    await picture('08-documents-added');
  });

  await step('a second collection, and a PDF into it', async () => {
    await page.locator('.page-actions button').click();
    const sheet = sheetWith('#collection-name');
    await sheet.locator('#collection-name').fill('tax');
    await sheet.locator('#collection-note').fill('Tax notices');
    await sheet.locator('button[type="submit"]').click();
    const card = page.locator('.collection[data-key="c-tax"]');
    await card.waitFor({ state: 'visible', timeout: WAIT });
    await noSheet();
    await page.setInputFiles('#files-tax', files.tax);
    // A PDF is read in a process of its own, which takes a moment to start.
    await eventually(async () => (await card.locator('.upload.upload-done').count()) === 1, 90000, 'the PDF was not sealed');
    await eventually(async () => (await card.locator('.doc-name').allTextContents()).join(',') === 'tax-2025', WAIT, 'the PDF is not listed in its collection');
    check((await card.locator('.upload-outside').count()) === 1, 'what is true of the chosen file is not said for the PDF');
    const now = await state();
    check(now.encryption.documents === 3 && now.encryption.collections === 2, `documents: ${JSON.stringify(now.encryption)}`);
    await picture('09-documents-two-collections');
  });

  await step('a file that cannot be read and a duplicate are refused with a reason', async () => {
    const card = page.locator('.collection[data-key="c-tax"]');
    expectHttp(409, '/api/documents');
    await page.setInputFiles('#files-tax', [files.picture, files.tax]);
    const refused = card.locator('.upload.upload-error');
    await eventually(async () => (await refused.count()) === 2, 90000, 'the two files were not both refused');
    const reasons = (await refused.locator('.upload-note').allTextContents()).map((text) => text.trim());
    check(reasons.every((reason) => /\s/.test(reason)) && reasons[0] !== reasons[1], `the reasons given: ${JSON.stringify(reasons)}`);
    check((await state()).encryption.documents === 3, 'a refused file was added');
  });

  await step('the overview now points at step 2', async () => {
    await go('overview');
    await startIsAt(2, '#/apps');
  });

  // ------------------------------------------------------------------ apps
  await step('adding a hosted app shows where its text goes, then how to connect it', async () => {
    await go('apps');
    await page.locator('.card-empty .btn-primary').click();
    const add = sheetWith('#add-app-name');
    await add.locator('label.choice', { has: page.locator(`input[value="${HOSTED}"]`) }).click();
    await eventually(() => add.locator(`input[name="add-app-known"][value="${HOSTED}"]`).isChecked(), WAIT, 'the app was not picked');
    const known = (await get('/api/apps')).known.find((entry) => entry.id === HOSTED);
    const goes = await add.locator('.goes-to').textContent();
    check(goes.includes(known.destination) && TEXT.cannotBeRecalled.test(goes), `where the text goes is not said: ${goes}`);
    await widening('/api/apps', () => add.locator('button[type="submit"]').click());
    const connect = sheetWith('#connect-host');
    const block = connect.locator('.config pre.code').first();
    await block.waitFor({ state: 'visible', timeout: WAIT });
    check((await connect.locator('.sheet-title').textContent()).includes(known.label), `the sheet is not about ${known.label}`);
    // The sheet can show the text for every program the app knows: ten of them.
    const hosts = await connect.locator('#connect-host option').evaluateAll((nodes) => nodes.map((node) => node.value));
    check(hosts.length === 10 && JSON.stringify(hosts) === JSON.stringify((await get('/api/apps')).known.map((entry) => entry.id)), `the sheet offers the text for ${JSON.stringify(hosts)}`);
    const text = await block.textContent();
    const server = Object.values(JSON.parse(text).mcpServers)[0];
    check(server.args.join(' ').includes(`gate mcp --client ${HOSTED} --store ${store}`), `the configuration does not name this vault: ${text}`);
    check(fs.existsSync(server.command), `the command is not a file: ${server.command}`);
    check(!text.includes(PASSPHRASE), 'the configuration holds the passphrase');
    check((await connect.locator('.config-where').count()) >= 1, 'the sheet does not say where to put the configuration');
    // "Copy" puts the text of the block on the clipboard, and nothing else.
    await pressCopy(page, options, connect.locator('.config-head button').first(), text);
    await picture('10-connect-sheet');
    await connect.locator('.sheet-foot .btn-primary').click();
    await noSheet();
    await appCard(HOSTED).waitFor({ state: 'visible', timeout: WAIT });
    check((await hostedApp()).last_seen === null, 'a new app counts as having called');
  });

  await step('adding a local app, and the choice the owner then has to make', async () => {
    await page.locator('.page-actions .btn-primary').click();
    const add = sheetWith('#add-app-name');
    await add.locator('label.choice', { has: page.locator('input[value="generic"]') }).click();
    await add.locator('label.radio', { has: page.locator('input[name="add-app-kind"][value="local"]') }).click();
    await add.locator('#add-app-name').fill(LOCAL);
    await eventually(async () => TEXT.cannotCheck.test(await add.locator('.goes-to').textContent()), WAIT, 'the sheet does not say that "local" cannot be checked');
    await widening('/api/apps', () => add.locator('button[type="submit"]').click());
    const connect = sheetWith('#connect-host');
    await connect.locator('.config pre.code').first().waitFor({ state: 'visible', timeout: WAIT });
    await connect.locator('.sheet-foot .btn-primary').click();
    await noSheet();
    // Both kinds of apps exist now. Until the owner chooses, the second kind is not answered.
    await page.locator('.strip.strip-amber').waitFor({ state: 'visible', timeout: WAIT });
    const now = await state();
    check(now.serving.choice_needed === true && now.serving.kinds === 'hosted', `serving: ${JSON.stringify(now.serving)}`);
    await eventually(() => hasClass(appCard(LOCAL), 'is-idle'), WAIT, 'the local app is not marked as not answered');
    check(await page.locator(`#answering-${LOCAL}`).isDisabled(), 'the local app can be switched on before the choice is made');
    await picture('11-apps-choice-needed');
    await go('overview');
    await page.locator('.strip.strip-amber').waitFor({ state: 'visible', timeout: WAIT });
    await page.locator(`.left-item.is-idle[data-key="${LOCAL}"]`).waitFor({ state: 'visible', timeout: WAIT });
    await go('apps');
  });

  await step('choosing both kinds asks first, in the sentence about programs running as you', async () => {
    await segment(page.locator('.strip'), 'both').click();
    const confirm = sheetWith('.sheet-foot');
    const sentence = confirm.locator('.note');
    await sentence.waitFor({ state: 'visible', timeout: WAIT });
    check(TEXT.runningAsYou.test(await sentence.textContent()), `before both kinds are answered the sheet says: ${await sentence.textContent()}`);
    await picture('12-both-kinds-sheet');
    await widening('/api/serving', () => confirm.locator('.sheet-foot .btn-primary').click());
    await eventually(async () => {
      const serving = (await state()).serving;
      check(serving.kinds === 'both' && serving.choice_needed === false && serving.local.includes(LOCAL), `serving: ${JSON.stringify(serving)}`);
      return true;
    });
    await noSheet();
    await eventually(async () => (await count('.strip.strip-amber')) === 0, WAIT, 'the page still asks for the choice');
  });

  // ------------------------------------------------------------------ grants
  await step('a hosted app cannot be given a collection until the collection is opened to hosted apps', async () => {
    const closed = appCard(HOSTED).locator('.grant-row[data-key="g-documents"]');
    await closed.locator('.grant-closed').waitFor({ state: 'visible', timeout: WAIT });
    check((await closed.locator('.segmented').count()) === 0, 'a closed collection can be granted');
    await closed.locator('.grant-closed a').click();
    await page.locator('.page-body[data-key="view-documents"]').waitFor({ state: 'attached', timeout: WAIT });
    for (const name of ['documents', 'tax']) {
      await page.locator(`label[for="hosted-${name}"]`).click();
      const confirm = sheetWith('.sheet-foot');
      await confirm.locator('.note').waitFor({ state: 'visible', timeout: WAIT });
      check((await confirm.locator('.sheet-title').textContent()).includes(name), `the question is not about ${name}`);
      await widening('/api/collections', () => confirm.locator('.sheet-foot .btn-primary').click());
      await eventually(() => page.locator(`#hosted-${name}`).isChecked(), WAIT, `the switch of ${name} did not go on`);
      await noSheet();
    }
    const collections = (await get('/api/apps')).collections;
    check(collections.length === 2 && collections.every((entry) => entry.allow_hosted === true), `collections: ${JSON.stringify(collections)}`);
  });

  await step('granting "documents" for reading and "tax" as ask me each time, each with its sentence', async () => {
    await go('apps');
    const destination = (await hostedApp()).destination;
    const grant = async (collection, mode) => {
      const choice = segment(appCard(HOSTED).locator(`.grant-row[data-key="g-${collection}"]`), mode);
      await choice.click();
      const confirm = sheetWith('.sheet-foot');
      const sentence = confirm.locator('.note');
      await sentence.waitFor({ state: 'visible', timeout: WAIT });
      const words = await sentence.textContent();
      check(words.includes(collection) && words.includes(destination) && TEXT.cannotBeRecalled.test(words), `granting ${collection} the sheet says: ${words}`);
      if (collection === 'documents') await picture('13-grant-sheet');
      await widening('/api/grants', () => confirm.locator('.sheet-foot .btn-primary').click());
      await eventually(() => hasClass(choice, 'is-selected'), WAIT, `the card does not show the new choice for ${collection}`);
      await noSheet();
    };
    await grant('documents', 'read');
    await grant('tax', 'approve');
    const grants = (await hostedApp()).grants;
    check(grants.documents === 'read' && grants.tax === 'approve', `grants: ${JSON.stringify(grants)}`);
    await picture('14-apps-granted');
    await go('overview');
    await startIsAt(4, '#/apps');
    await picture('15-overview-step-4');
  });

  await step('the screen that shows how to connect the app can be found again', async () => {
    await go('apps');
    await appCard(HOSTED).getByRole('button', { name: TEXT.connect }).click();
    const connect = sheetWith('#connect-host');
    await connect.locator('.config pre.code').first().waitFor({ state: 'visible', timeout: WAIT });
    check((await connect.locator('.config-where').count()) >= 1, 'the sheet does not say where to put the configuration');
    // An amber note here means the command in the block would not start (no SDK, an old Python).
    check((await connect.locator('.note-amber').count()) === 0, `the connect sheet warns: ${await connect.locator('.connect-body').textContent()}`);
    await connect.locator('.sheet-foot .btn-primary').click();
    await noSheet();
  });

  // ------------------------------------------------------------------ calls of the app
  const calls = {};
  const seen = {};
  await step('the app asks what it may use: a question, not a release', async () => {
    calls.status = agent(store, HOSTED, 'status');
    check(calls.status.code === 'OK' && calls.status.released === false, `status: ${JSON.stringify(calls.status)}`);
    check(calls.status.text.includes(`documents (${AGENT.withSnippets}`) && calls.status.text.includes(`tax (${AGENT.refsOnly}`), `status text: ${calls.status.text}`);
    await go('activity');
    const question = await eventually(async () => (await activity()).find((item) => item.kind === 'question' && item.app === HOSTED), WAIT, 'the question is not in the record');
    await row(question.seq).waitFor({ state: 'visible', timeout: WAIT });
    check((await row(question.seq).locator('.act-status').textContent()).trim() === TEXT.question, `chip: ${await row(question.seq).locator('.act-status').textContent()}`);
    const now = await state();
    check(now.today.releases === 0 && now.today.questions === 1 && now.today.characters === 0, `today: ${JSON.stringify(now.today)}`);
    await row(question.seq).locator('.act-row').click(AT_ITS_START);
    await page.locator('#panel .note').waitFor({ state: 'visible', timeout: WAIT });
    check((await count('#panel pre.released')) === 0, 'the details of a question show released text');
    await picture('16-activity-question');
    await closePanel('Escape');
  });

  await step('a search and a read appear in the activity list with the exact text that left', async () => {
    calls.search = agent(store, HOSTED, 'search', 'monthly rent deposit', 'documents');
    check(calls.search.code === 'OK' && calls.search.released, `search: ${JSON.stringify(calls.search)}`);
    const ref = REF.exec(calls.search.text)[1];
    calls.read = agent(store, HOSTED, 'read', ref);
    check(calls.read.code === 'OK' && calls.read.text.includes('The monthly rent is 1,180 EUR'), `read: ${JSON.stringify(calls.read)}`);
    const found = await eventually(async () => {
      const items = await activity();
      const search = items.find((item) => item.kind === 'release' && item.tool === 'search');
      const read = items.find((item) => item.kind === 'release' && item.tool === 'read' && item.ref === ref);
      return search && read ? { search, read } : null;
    }, WAIT, 'the search and the read are not in the record');
    seen.read = found.read.seq;
    const released = page.locator('#panel pre.released');
    const shows = (text) =>
      eventually(async () => {
        const shown = await released.textContent({ timeout: 2000 });
        check(shown === text, `the details show other text than the app received:\n${shown}\n---\n${text}`);
        return true;
      });
    await row(found.read.seq).locator('.act-row').click(AT_ITS_START);
    await shows(calls.read.text);
    const destination = (await hostedApp()).destination;
    const gone = await page.locator('#panel .gone').textContent();
    check(gone.includes(destination) && TEXT.cannotTakeBack.test(gone), `under the text the details say: ${gone}`);
    const who = await page.locator('#panel .panel-who').textContent();
    check(who.includes('journey-agent 1.0') && TEXT.unverified.test(who), `about the caller the details say: ${who}`);
    await picture('17-activity-detail');
    await row(found.search.seq).locator('.act-row').click(AT_ITS_START);
    await shows(calls.search.text);
    check((await page.locator('#panel').textContent()).includes('monthly rent deposit'), 'the query is not shown');
    await closePanel();
    const now = await state();
    check(now.today.releases === 2 && now.today.questions === 1, `today: ${JSON.stringify(now.today)}`);
    check(now.today.characters === calls.search.text.length + calls.read.text.length, `characters: ${now.today.characters}`);
    await go('overview');
    // The app has called: the getting-started list has done its work and goes.
    await page.locator('.card-start').waitFor({ state: 'detached', timeout: WAIT });
    await page.locator('.stats-aside').waitFor({ state: 'visible', timeout: WAIT });
    await picture('18-overview-after-calls');
  });

  await step('a passage of "tax" waits for the owner, with its exact text, and is released once', async () => {
    calls.taxSearch = agent(store, HOSTED, 'search', 'tax refund', 'tax');
    check(calls.taxSearch.code === 'OK' && calls.taxSearch.text.includes(AGENT.withheld), `tax search: ${calls.taxSearch.text}`);
    const ref = REF.exec(calls.taxSearch.text)[1];
    calls.refused = agent(store, HOSTED, 'read', ref);
    check(calls.refused.code === 'NEEDS_APPROVAL' && !calls.refused.released, `read: ${JSON.stringify(calls.refused)}`);
    check(calls.refused.text.includes(product) && calls.refused.text.includes(ref) && calls.refused.text.includes(HOSTED), `the refusal does not send the owner to the app: ${calls.refused.text}`);
    // The overview shows that something waits, and leads to it.
    const review = page.locator('.card[data-key="waiting"] .waiting-item a.btn');
    await review.waitFor({ state: 'visible', timeout: WAIT });
    await review.click();
    const key = `ap-${HOSTED}-${ref}`;
    const card = page.locator(`.approval[data-key="${key}"]`);
    await card.waitFor({ state: 'visible', timeout: WAIT });
    const waiting = (await get('/api/approvals')).waiting.find((item) => item.ref === ref);
    check(waiting && waiting.mode === 'approve' && waiting.collection === 'tax', `the passage waits as: ${JSON.stringify(waiting && { mode: waiting.mode, collection: waiting.collection })}`);
    const body = await card.locator('pre.released').textContent();
    check(body === TAX_LINE, `the card shows: ${body}`);
    const sentence = await card.locator('.approval-sentence').textContent();
    check(sentence.includes(waiting.destination) && TEXT.cannotBeRecalled.test(sentence), `before the release the card says: ${sentence}`);
    await picture('19-approval-card');
    await decideApproval(page, key, 'release');
    calls.approved = agent(store, HOSTED, 'read', ref);
    check(calls.approved.code === 'OK' && calls.approved.text.includes(body), `read after approval: ${JSON.stringify(calls.approved)}`);
    calls.again = agent(store, HOSTED, 'read', ref);
    check(calls.again.code === 'NEEDS_APPROVAL', `an approval was used twice: ${calls.again.code}`);
    await go('activity');
    const items = await activity();
    const read = items.find((item) => item.kind === 'release' && item.approved === true && item.ref === ref);
    check(read && items.some((item) => item.kind === 'approval'), 'the approval and the read it allowed are not both in the record');
    await row(read.seq).waitFor({ state: 'visible', timeout: WAIT });
    check((await row(read.seq).locator('.act-status').textContent()).trim() === TEXT.afterApproval, 'the read is not marked as released after approval');
    await row(read.seq).locator('.act-row').click(AT_ITS_START);
    await eventually(async () => (await page.locator('#panel pre.released').textContent({ timeout: 2000 })) === calls.approved.text, WAIT, 'the details show other text than the app received');
    await picture('20-activity-after-approval');
    await closePanel();
    // The app asked once more. This time the owner says no.
    await go('approvals');
    await card.waitFor({ state: 'visible', timeout: WAIT });
    await decideApproval(page, key, 'dismiss');
    await page.locator('.card-empty').waitFor({ state: 'visible', timeout: WAIT });
    check((await get('/api/approvals')).waiting.length === 0, 'the passage still waits after "Don\'t release"');
  });

  await step('a short document: its one passage waits although the app can read, and the card says why', async () => {
    const found = agent(store, HOSTED, 'search', 'boiler serviced', 'documents');
    const ref = REF.exec(found.text)[1];
    const refused = agent(store, HOSTED, 'read', ref);
    check(refused.code === 'NEEDS_APPROVAL', `read of a one-passage document: ${refused.code}`);
    await go('approvals');
    const key = `ap-${HOSTED}-${ref}`;
    const card = page.locator(`.approval[data-key="${key}"]`);
    await card.waitFor({ state: 'visible', timeout: WAIT });
    const waiting = (await get('/api/approvals')).waiting.find((item) => item.ref === ref);
    check(waiting && waiting.mode === 'read' && waiting.inside_free_allowance === false, `the passage waits as: ${JSON.stringify(waiting && { mode: waiting.mode, inside: waiting.inside_free_allowance })}`);
    const why = await card.locator('.approval-counts').textContent();
    check(TEXT.although.test(why) && why.includes('documents'), `the card does not say why a passage waits that the app may read: ${why}`);
    await picture('20b-approval-past-allowance');
    await decideApproval(page, key, 'dismiss');
    await page.locator('.card-empty').waitFor({ state: 'visible', timeout: WAIT });
  });

  await step('a call under a name nobody added, and one by the local app', async () => {
    const stranger = agent(store, 'someone-else', 'search', 'rent');
    check(stranger.code === 'NOT_ALLOWED', `stranger: ${stranger.code}`);
    const local = agent(store, LOCAL, 'status');
    check(local.code === 'OK' && local.text.includes(AGENT.noCollections), `local status: ${local.text}`);
    await go('activity');
    const refusal = await eventually(async () => (await activity()).find((item) => item.kind === 'refusal' && item.code === 'NOT_ALLOWED'), WAIT, 'the refused call is not in the record');
    await row(refusal.seq).locator('.act-status .chip-red').waitFor({ state: 'visible', timeout: WAIT });
  });

  // ------------------------------------------------------------------ owner mode
  let limit = 0;
  await step('raising a limit after owner mode has passed asks for the passphrase again', async () => {
    // The state names the moment; a change sent after it is refused until the passphrase was typed again.
    const over = Date.parse((await state()).owner_mode_until) + 1500;
    await eventually(() => Date.now() >= over, options.ownerSeconds * 1000 + WAIT, 'owner mode did not run out');
    await go('apps');
    limit = (await hostedApp()).limits.chars_per_day + 10000;
    await appCard(HOSTED).getByRole('button', { name: TEXT.limits }).click();
    const limits = sheetWith('#limit-chars_per_day');
    await limits.locator('#limit-chars_per_day').fill(String(limit));
    expectHttp(403, `/api/apps/${HOSTED}/limits`);
    await limits.locator('button[type="submit"]').click();
    const again = page.locator('#reauth-passphrase');
    await again.waitFor({ state: 'visible', timeout: WAIT });
    const sheet = sheetWith('#reauth-passphrase');
    check((await count('.demo-pass')) === 0, 'a real vault shows a passphrase');
    await picture('21-reauth-sheet');
    expectHttp(403, '/api/reauth');
    await again.fill('not the passphrase');
    await sheet.locator('button[type="submit"]').click();
    await page.locator('#reauth-passphrase-error').waitFor({ state: 'visible', timeout: WAIT });
    await again.fill(PASSPHRASE);
    await sheet.locator('button[type="submit"]').click();
    await eventually(async () => (await hostedApp()).limits.chars_per_day === limit, WAIT, 'the limit was not raised');
    await noSheet();
    await appCard(HOSTED).locator(`.meter[aria-valuemax="${limit}"]`).first().waitFor({ state: 'visible', timeout: WAIT });
    check(Date.parse((await state()).owner_mode_until) > Date.now(), 'owner mode was not renewed');
  });

  await step('lowering a limit needs no passphrase', async () => {
    limit -= 5000;
    await appCard(HOSTED).getByRole('button', { name: TEXT.limits }).click();
    const limits = sheetWith('#limit-chars_per_day');
    await limits.locator('#limit-chars_per_day').fill(String(limit));
    await limits.locator('button[type="submit"]').click();
    await eventually(async () => (await hostedApp()).limits.chars_per_day === limit, WAIT, 'the limit was not lowered');
    await noSheet();
    await appCard(HOSTED).locator(`.meter[aria-valuemax="${limit}"]`).first().waitFor({ state: 'visible', timeout: WAIT });
  });

  // ------------------------------------------------------------------ lock, unlock
  await step('locking: the page shows the locked vault and the app is told so', async () => {
    await page.click('button.lock-now');
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('.vault-path').textContent()) === store, `the locked screen shows: ${await page.locator('.vault-path').textContent()}`);
    check((await count('.demo-pass')) === 0, 'a real vault shows a passphrase on the locked screen');
    await eventually(async () => (await page.title()) !== product && (await page.title()).includes(product), WAIT, `the title does not say that the vault is locked: ${await page.title()}`);
    const locked = agent(store, HOSTED, 'search', 'rent');
    check(locked.code === 'LOCKED' && locked.text.includes(product), `locked: ${JSON.stringify(locked)}`);
    await picture('22-locked');
  });

  await step('unlocking: a wrong passphrase is refused, the right one brings everything back', async () => {
    const unlock = page.locator('.gate-card button[type="submit"]');
    expectHttp(403, '/api/unlock');
    await page.fill('#unlock-passphrase', 'not the passphrase');
    await unlock.click();
    await page.locator('#unlock-passphrase-error').waitFor({ state: 'visible', timeout: WAIT });
    await page.fill('#unlock-passphrase', PASSPHRASE);
    await unlock.click();
    await page.locator('aside.sidebar').waitFor({ state: 'visible', timeout: 40000 });
    await go('overview');
    await page.locator('.left-list .left-item').first().waitFor({ state: 'visible', timeout: WAIT });
    const now = await state();
    check(now.locked === false && now.record.previous_session === 'clean', `state: ${JSON.stringify(now.record)}`);
    // The choice between the kinds of apps was for one session: it is asked for again.
    check(now.serving.choice_needed === true, 'the choice of kinds outlived the session');
    await page.locator('.strip.strip-amber').waitFor({ state: 'visible', timeout: WAIT });
    const after = agent(store, HOSTED, 'status');
    check(after.code === 'OK', `status after unlock: ${after.code}`);
    await go('activity');
    const sessions = (await activity()).filter((item) => item.kind === 'session' && item.seq > seen.read);
    check(sessions.length === 2, `the lock and the unlock are not both in the record: ${JSON.stringify(sessions.map((item) => item.summary))}`);
    for (const item of sessions) await row(item.seq).waitFor({ state: 'visible', timeout: WAIT });
    await row(seen.read).waitFor({ state: 'visible', timeout: WAIT });
    await picture('23-activity-after-unlock');
  });

  // ------------------------------------------------------------------ Ctrl-C
  await step('Ctrl-C locks the vault and quits with status 0 and nothing more on the terminal', async () => {
    const before = app.out;
    const exit = await stopApp(app, 'SIGINT');
    check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
    check(app.out === before, `more was printed: ${app.out.slice(before.length)}`);
    check(app.err === '', `standard error: ${app.err}`);
    check(!fs.existsSync(path.join(store, 'run', 'gate.sock')), 'the socket is still there');
    const gone = agent(store, HOSTED, 'status');
    check(gone.code === 'LOCKED', `after the app stopped: ${gone.code}`);
    // The page notices by itself and says which program it misses.
    const bar = page.locator('.offline-bar');
    await bar.waitFor({ state: 'visible', timeout: WAIT });
    check((await bar.textContent()).includes(product), `the page says: ${await bar.textContent()}`);
    // The bar stands above the page's title and covers none of it, and the sidebar no longer
    // says what the vault's state was when the app last answered.
    const [barBox, titleBox] = [await bar.boundingBox(), await page.locator('.page-title').boundingBox()];
    check(barBox.y + barBox.height <= titleBox.y, `the bar lies over the title: ${JSON.stringify([barBox, titleBox])}`);
    await page.locator('.lock-state.is-unknown').waitFor({ state: 'visible', timeout: WAIT });
    await picture('24-app-stopped');
  });

  await step('started again: the page pairs anew, and the record says how the last session ended', async () => {
    // On the same port, because the page that is still open asks there.
    app = startApp(options, { args: ['--store', store], port: new URL(base).port });
    const lines = await started(app);
    check(app.url === base, `the app came back at ${app.url}, not at ${base}`);
    const again = /^pairing code: (\S+)\s/.exec(lines[3]);
    check(again && again[1] !== code, `the second run has no code of its own: ${lines[3]}`);
    // The old session is gone: the page is told so at its next question and asks for the new code.
    expectHttp(401, '/api/state', { times: 5, optional: true });
    await page.locator('#pair-code').waitFor({ state: 'visible', timeout: 40000 });
    pageReachedApp();
    await page.fill('#pair-code', again[1]);
    await page.locator('.gate-card button[type="submit"]').click();
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    await page.fill('#unlock-passphrase', PASSPHRASE);
    await page.locator('.gate-card button[type="submit"]').click();
    await page.locator('aside.sidebar').waitFor({ state: 'visible', timeout: 40000 });
    await go('activity');
    const now = await state();
    check(now.record.previous_session === 'clean' && now.warnings.length === 0, `state: ${JSON.stringify([now.record, now.warnings])}`);
    const stopped = (await activity()).find((item) => item.kind === 'session' && item.summary.includes(product));
    check(stopped, 'the record does not say that the app was stopped');
    await row(stopped.seq).waitFor({ state: 'visible', timeout: WAIT });
    await picture('25-after-restart');
  });

  await step('stopped once more, the app again quits with status 0 and an empty standard error', async () => {
    await closePage(page);
    const exit = await stopApp(app, 'SIGINT');
    check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
    check(app.err === '', `standard error: ${app.err}`);
  });

  await finish(browser);
});
