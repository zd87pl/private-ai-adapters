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
// The app is started WITHOUT --demo and with "--view pro" on an empty directory with a
// short path under /tmp, through launch_app.py, which changes one constant (how long
// owner mode lasts) and gives the app an empty home of its own. Calls of an agent
// application are made by agent_call.py through RemoteGate, the path the program an
// application starts uses.
//
// The walk pairs and creates the vault, which opens on Overview with its list for a new
// vault: there is no step to skip. From there on it is a walk through the six pages
// with every setting (Pro, under "#/pro/…"): documents, apps, grants, an agent's calls,
// approvals, the passphrase asked for again, lock and unlock, Ctrl-C and a restart,
// and at its end the addresses those pages, Home and the three steps of an earlier
// first run had before. Home, the sheets of its tiles and the plain pages are walked by
// journey_home.js.
//
// On its way it chooses Polish four times: on the pairing screen, on the passphrase
// screen, for an approval card and on the locked screen. Each time everything that can
// be read there must be a text of the Polish table or a name, and a reload keeps the
// language once the vault exists. The rest of the walk is in English.
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
  chooseLanguage,
  closePage,
  deadline,
  decideApproval,
  eventually,
  expectHttp,
  expectOldSessionRefused,
  finish,
  newPage,
  notOfTable,
  note,
  openBrowser,
  openOn,
  openPage,
  pageReachedApp,
  pressCopy,
  readOptions,
  reload,
  run,
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
  nobodyResets: /Nobody can reset it/, // beside the new passphrase
  notEncrypted: /not encrypted/i, // about the files the owner chose, after adding them
  cannotBeRecalled: /cannot be recalled/i, // wherever text is given to a hosted app
  cannotCheck: /cannot check/i, // about an app declared local
  runningAsYou: /any program running as you/i, // before both kinds of apps are answered together
  cannotTakeBack: /cannot take it back/i, // under text that has left
  unverified: /unverified/i, // beside the name an app gives itself
  isALabel: /is a label/i, // on a card: what the name of the app is
  pastAllowance: /goes past what the app may read of this document without asking/, // why a passage waits although the app can read the collection
  originalsKept: /originals? kept/i, // what the vault screen must not say
};

/** What stands alone on a screen and is no text of a table: the two letters of each language, a figure, a size. */
const CODES = ['EN', 'PL'];
const FIGURE = /^[\d\s.,]+( (bajt\w*|bytes?|kB|MB|GB))?$/;

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
  let app = startApp(options, { args: ['--store', store, '--view', 'pro'] });
  let page = await newPage(browser, options);
  let base = '';
  let product = '';
  let code = '';
  /** The Polish table of the page, as the app serves it. */
  let polish = null;

  const get = (pathname) => apiGet(page, base, pathname);
  const state = () => get('/api/state');
  const activity = async () => (await get('/api/activity?limit=200')).items;
  const hostedApp = async () => (await get('/api/apps')).apps.find((entry) => entry.name === HOSTED);
  const agent = (directory, client, ...rest) => agentCall(options, directory, client, ...rest);
  const picture = (name) => shot(page, name);
  const count = (selector) => page.locator(selector).count();
  /** Open one of the six pages by its entry in their sidebar. Their addresses begin with "#/pro/". */
  const go = async (route) => {
    await page.click(`a.nav-item[href="#/pro/${route}"]`);
    await page.locator(`.page-body[data-key="view-pro/${route}"]`).waitFor({ state: 'attached', timeout: WAIT });
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
  /**
   * Everything that can be read inside `selector` is a text of the Polish table or one of
   * `names`, and each of the keys in `shown` is among it: the screen is in Polish, and it
   * is the screen that was meant.
   */
  const allPolish = async (selector, names, shown) => {
    const read = await textsIn(page, selector);
    const foreign = notOfTable(read, polish, [product, ...CODES, ...names]);
    check(foreign.length === 0, `in Polish, ${selector} still shows: ${JSON.stringify(foreign)}`);
    for (const key of shown) {
      check(notOfTable(read, { [key]: polish[key] }).length < read.length, `in Polish, ${selector} does not show the text of ${key}`);
    }
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
    // An English browser, and no vault to keep another choice: the page starts in English.
    check(session.language === null && (await page.locator('html').getAttribute('lang')) === 'en', `the page starts in "${await page.locator('html').getAttribute('lang')}", the app keeps ${JSON.stringify(session.language)}`);
    check((await page.locator('.gate-card button[type="submit"]').isDisabled()) === true, '"Pair" can be pressed before a code is typed');
    await picture('01-pair');
  });

  await step('Polish chosen on the pairing screen: every text is the Polish table\'s, and nothing is sent', async () => {
    polish = await textTable(page, base, 'pl');
    const asked = [];
    const listen = (request) => {
      if (request.method() !== 'GET') asked.push(`${request.method()} ${new URL(request.url()).pathname}`);
    };
    page.on('request', listen);
    await chooseLanguage(page, '.gate-lang', 'pl');
    await allPolish('#app', [], ['pair.title', 'pair.say.1', 'pair.say.2', 'pair.label.code', 'pair.button.pair']);
    check((await page.locator('.gate-lang .lang').getAttribute('aria-label')) === polish['common.label.language'], 'the switch is not named in Polish');
    // The switch stays top right, also in a window as narrow as a phone, where the card fills the width.
    await page.setViewportSize({ width: 400, height: 800 });
    const [lang, card] = [await page.locator('.gate-lang').boundingBox(), await page.locator('.gate-card').boundingBox()];
    check(lang.x + lang.width <= 400 && lang.x > 200 && lang.y + lang.height <= card.y, `at 400 px the switch is at ${JSON.stringify(lang)}, the card at ${JSON.stringify(card)}`);
    check(Math.round(card.x) === 16 && Math.round(card.width) === 368, `at 400 px the card does not fill the width with 16 px at each side: ${JSON.stringify(card)}`);
    check((await page.evaluate(() => document.documentElement.scrollWidth)) <= 400, 'at 400 px the pairing screen scrolls sideways');
    await page.setViewportSize({ width: 1280, height: 900 });
    await picture('01b-pair-polish');
    // There is no vault to keep the choice beside: the press stays in the page's memory.
    page.off('request', listen);
    check(asked.length === 0, `choosing a language before a vault exists sent: ${asked.join(', ')}`);
  });

  await step('a wrong code is refused in plain words; the eighth character of the right one pairs, without a press', async () => {
    const pair = page.locator('.gate-card button[type="submit"]');
    const wrong = code === 'AAAA-AAAA' ? 'BBBB-BBBB' : 'AAAA-AAAA';
    expectHttp(403, '/api/session/pair');
    // The eighth character sends the code. No press on "Pair" is on the way in.
    await page.fill('#pair-code', wrong);
    const refusal = page.locator('#pair-code-error');
    await refusal.waitFor({ state: 'visible', timeout: WAIT });
    // The page keeps a Polish word of one letter on the line of the word after it, with a no-break space: compared as a space.
    const words = (await refusal.textContent()).replace(/\u00a0/g, ' ').trim();
    check(words === polish['pair.error.wrong'] && /\s/.test(words) && !/wrong_code|403/.test(words), `the refusal is not the sentence of the table: ${words}`);
    // Nothing was emptied, and the code that was refused cannot be sent again as it stands.
    check((await page.inputValue('#pair-code')) === wrong, `after the refusal the field holds "${await page.inputValue('#pair-code')}"`);
    check(await pair.isDisabled(), '"Pair" would send the refused code a second time');
    // Typed the way a person might: lower case, a blank for the hyphen.
    await page.fill('#pair-code', code.toLowerCase().replace('-', ' '));
    await page.locator('#new-passphrase').waitFor({ state: 'visible', timeout: WAIT });
  });

  // ------------------------------------------------------------------ create the vault
  await step('creating the vault: the rules of the passphrase are said in English and in Polish, the place is shown', async () => {
    const create = page.locator('.gate-card button[type="submit"]');
    // The language chosen on the pairing screen is still the one shown: every text here is Polish.
    check((await page.locator('html').getAttribute('lang')) === 'pl', 'the language chosen before pairing was lost by pairing');
    await allPolish('#app', [], ['create.title', 'create.label.passphrase', 'create.label.again', 'create.say.1', 'create.say.2', 'create.button.create', 'create.status.where', 'create.status.short']);
    await picture('02b-create-polish');
    await chooseLanguage(page, '.gate-lang', 'en');
    check(TEXT.nobodyResets.test(await page.locator('.gate-card .note-amber').textContent()), 'the page does not say that nobody can reset a lost passphrase');
    check((await page.locator('.gate-where').textContent()).includes(store), `the page does not show where the vault is created: ${await page.locator('.gate-where').textContent()}`);
    // One card of 420 px, and no row of steps: the passphrase is the one thing to do here.
    check(Math.round((await page.locator('.gate-card').boundingBox()).width) === 420 && (await count('ol.steps')) === 0, 'the passphrase screen is not one card of 420 px without a row of steps');
    await page.fill('#new-passphrase', 'too short');
    await eventually(async () => /\b9\b/.test(await page.locator('#new-passphrase-hint').textContent()), WAIT, 'the hint does not count the nine characters typed');
    check(await create.isDisabled(), 'a short passphrase can be sent');
    await page.fill('#new-passphrase', PASSPHRASE);
    await page.fill('#new-passphrase-again', `${PASSPHRASE}x`);
    await page.locator('#new-passphrase-again-error').waitFor({ state: 'visible', timeout: WAIT });
    check(await create.isDisabled(), 'two different entries can be sent');
    await page.fill('#new-passphrase-again', PASSPHRASE);
    await eventually(async () => (await count('#new-passphrase-again-error')) === 0, WAIT, 'the two entries still count as different');
    await picture('02-create');
    // The language that is pressed last goes along with the passphrase: Polish.
    await chooseLanguage(page, '.gate-lang', 'pl');
    await create.click();
    // No step follows: a vault that was started with "--view pro" opens on Overview.
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: 40000 });
    check(new URL(page.url()).hash === '#/pro/overview', `after "Create my vault" the address is ${page.url()}`);
    const now = await state();
    check(now.locked === false && now.store.exists === true, 'the vault is not unlocked after it was created');
    check(!('demo_passphrase' in now), 'a real vault sends a passphrase');
    check(now.interface.view === 'pro' && now.interface.language === 'pl', `a new vault starts as ${JSON.stringify(now.interface)}`);
  });

  await step('the app keeps the language beside the vault: a reload shows Polish again, and one press changes it back', async () => {
    check((await get('/api/session')).language === 'pl', 'the session\'s answer does not name the kept language');
    await reload(page);
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('html').getAttribute('lang')) === 'pl', `after a reload the page is in "${await page.locator('html').getAttribute('lang')}"`);
    check((await page.locator('.sidebar-foot button[data-language="pl"]').getAttribute('aria-pressed')) === 'true', 'after a reload the switch does not mark Polish');
    // A press now is kept by the app at once: the vault exists.
    const kept = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/interface', { timeout: WAIT });
    await chooseLanguage(page, '.sidebar-foot', 'en');
    check((await kept).status() === 200, 'the app did not keep the language');
    await eventually(async () => (await state()).interface.language === 'en', WAIT, 'the state does not name the language that was pressed');
    check((await count('#toasts .toast')) === 0, 'choosing a language left a message');
  });

  await step('no step follows the passphrase: Overview is open with its list for a new vault, which says what to do next, in order; Settings shows where the app opens', async () => {
    // Nothing was added on the way, and the app keeps that this vault opens on the six pages.
    const fresh = await state();
    check(fresh.interface.view === 'pro', `after "Create my vault": ${JSON.stringify(fresh.interface)}`);
    check(fresh.encryption.documents === 0 && (await get('/api/apps')).apps.length === 0, 'creating the vault added something to it');
    check((await count('ol.steps')) === 0 && (await count('.gate-card')) === 0, 'a step of setting up is on the screen');
    // "Open on" under Settings shows it, and Overview is one of six rows there.
    await page.locator('#pro-home').click();
    await page.locator(FRAME.simple).waitFor({ state: 'visible', timeout: WAIT });
    await openPage(page, 'settings');
    check((await page.locator('input[name="start"]:checked').getAttribute('value')) === 'pro', '"Open on" does not show that this vault opens on the six pages');
    await openOn(page, 'basic');
    await eventually(async () => (await state()).interface.view === 'basic', WAIT, '"Open on" did not keep the choice');
    await openOn(page, 'pro');
    await eventually(async () => (await state()).interface.view === 'pro', WAIT, '"Open on" did not keep the choice');
    check((await count('#toasts .toast')) === 0, 'choosing where the app opens left a message');
    check(new URL(page.url()).hash === '#/settings', `choosing where the app opens moved the page to ${page.url()}`);
    await page.locator('a.row-link[href="#/pro/overview"]').click();
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: WAIT });
    check(new URL(page.url()).hash === '#/pro/overview', `the row "Overview" leads to ${page.url()}`);
    // The six pages have one entry above their six, which leads back to Home, and no switch of a view.
    check((await page.locator('aside.sidebar nav a').count()) === 7 && (await page.locator('aside.sidebar nav a').first().getAttribute('href')) === '#/home', 'the sidebar of the six pages does not begin with the way back to Home');
    check((await count('input[name="view"]')) === 0 && (await count('.visit-bar')) === 0, 'a switch of the view or the bar of a visit is still there');
    await page.locator('.card-start').waitFor({ state: 'visible', timeout: WAIT });
    await startIsAt(1, '#/pro/documents');
    check((await count('.start-step.is-next a.btn')) === 1, 'more than one next step is offered');
    // No app yet: the card of what was sent offers to add one, and nothing waits.
    await page.locator('.card[data-key="left"] .empty a[href="#/pro/apps"]').waitFor({ state: 'visible', timeout: WAIT });
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
    await startIsAt(2, '#/pro/apps');
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
    // The sheet can show the text for every program the app knows: eleven of them.
    const hosts = await connect.locator('#connect-host option').evaluateAll((nodes) => nodes.map((node) => node.value));
    check(hosts.length === 11 && JSON.stringify(hosts) === JSON.stringify((await get('/api/apps')).known.map((entry) => entry.id)), `the sheet offers the text for ${JSON.stringify(hosts)}`);
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
    await page.locator('.page-body[data-key="view-pro/documents"]').waitFor({ state: 'attached', timeout: WAIT });
    check(new URL(page.url()).hash === '#/pro/documents', `the link of a closed collection leads to ${page.url()}`);
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
    await startIsAt(4, '#/pro/apps');
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
    // This sheet shows text to copy. It wrote nothing: the home of the app is as empty as it was made.
    check(fs.readdirSync(app.home).length === 0, `the home of the app holds ${JSON.stringify(fs.readdirSync(app.home))}`);
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

  await step('a passage of "tax" waits for the owner, with its exact text, in English and in Polish, and is released once', async () => {
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
    // The card names the app by the name it has in the vault, and says what that name is.
    check((await card.locator('.approval-title').textContent()) === `${HOSTED} asks for a passage of \u201ctax-2025\u201d`, `the card is titled: ${await card.locator('.approval-title').textContent()}`);
    const label = await card.locator('.approval-label').textContent();
    check(label.includes(HOSTED) && label.includes(product) && TEXT.isALabel.test(label), `about the name the card says: ${label}`);
    check(TEXT.unverified.test(await card.locator('.approval-meta').textContent()) && (await card.locator('.approval-meta').textContent()).includes(waiting.destination), `the line of facts reads: ${await card.locator('.approval-meta').textContent()}`);
    // The two answers: the careful one first, neither filled.
    const answers = await card.locator('.approval-actions button').evaluateAll((nodes) => nodes.map((node) => `${node.dataset.key} ${node.classList.contains('btn-primary')}`));
    check(JSON.stringify(answers) === JSON.stringify(['dismiss false', 'release false']), `the answers of the card are ${JSON.stringify(answers)}`);
    await picture('19-approval-card');
    // In Polish, every text of the card is the Polish table's; what is in the box is the owner's own text.
    await chooseLanguage(page, '.sidebar-foot', 'pl');
    // A document of one passage says "the whole document" in place of "passage 1 of 1".
    const figures = waiting.passages === 1 ? 'card.status.meta.whole' : 'card.status.meta';
    await allPolish(`.approval[data-key="${key}"]`, [TAX_LINE], ['card.title', 'card.chip.hosted', 'card.status.claimed', 'card.label.text', figures, 'card.say.1', 'card.say.2', 'card.button.no', 'card.button.yes', 'card.button.details']);
    // "Details" unfolds three lines, and "Release once" waits again: the cards below would have moved.
    await card.locator('button[data-key="more"]').click();
    await eventually(async () => (await card.locator('.approval-details li').count()) === 3, WAIT, '"Details" did not unfold three lines');
    check(await hasClass(card.locator('button[data-key="release"]'), 'is-waiting'), '"Release once" stayed ready while the card unfolded');
    await allPolish(`.approval[data-key="${key}"]`, [TAX_LINE], ['card.status.document', 'card.status.today', 'card.details.say.1.each']);
    await picture('19b-approval-card-polish');
    await chooseLanguage(page, '.sidebar-foot', 'en');
    await card.locator('button[data-key="more"]').click();
    await eventually(async () => (await card.locator('.approval-details').count()) === 0, WAIT, '"Details" did not fold again');
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
    // Why it waits is behind "Details", with the two lines of figures.
    await card.locator('button[data-key="more"]').click();
    const why = card.locator('.approval-details');
    await why.waitFor({ state: 'visible', timeout: WAIT });
    check(TEXT.pastAllowance.test(await why.textContent()), `the card does not say why a passage waits that the app may read: ${await why.textContent()}`);
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
  await step('locking: the page shows the locked vault and the app is told so; in Polish every text of it is the Polish table\'s, and a reload keeps the language', async () => {
    await page.click('button.lock-now');
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('.vault-path').textContent()) === store, `the locked screen shows: ${await page.locator('.vault-path').textContent()}`);
    check((await count('.demo-pass')) === 0, 'a real vault shows a passphrase on the locked screen');
    await eventually(async () => (await page.title()) !== product && (await page.title()).includes(product), WAIT, `the title does not say that the vault is locked: ${await page.title()}`);
    const locked = agent(store, HOSTED, 'search', 'rent');
    check(locked.code === 'LOCKED' && locked.text.includes(product), `locked: ${JSON.stringify(locked)}`);
    // The view that is kept is Pro: the three figures stand under the place, and the foot has both sentences.
    check((await page.locator('dl.seal-facts dt').count()) === 3 && (await page.locator('.gate-foot span').count()) === 2, 'the locked screen of a vault whose view is Pro lacks a figure or a sentence');
    await picture('22-locked');
    // In Polish, every text of the locked screen is the Polish table's. The app keeps the word
    // also while the vault is locked, so a reload shows the same screen in the same language.
    await chooseLanguage(page, '.gate-lang', 'pl');
    const names = ['vault', /^[^/]*\/$/, /^#\d+ [0-9a-f]+$/, FIGURE];
    const shown = ['locked.title', 'locked.label.files', 'locked.label.size', 'locked.label.last', 'locked.label.passphrase', 'locked.button.unlock', 'locked.say.1', 'locked.say.2'];
    await allPolish('#app', names, shown);
    await eventually(async () => (await state()).interface.language === 'pl', WAIT, 'the app did not keep the language of a locked vault');
    await eventually(async () => (await page.title()) === polish['nav.tab.locked'].replace('{product}', product), WAIT, `in Polish the tab is called "${await page.title()}"`);
    await reload(page);
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('html').getAttribute('lang')) === 'pl', 'a reload of the locked screen lost the language');
    await allPolish('#app', names, shown);
    await picture('22b-locked-polish');
    await chooseLanguage(page, '.gate-lang', 'en');
    await eventually(async () => (await state()).interface.language === 'en', WAIT, 'the app did not keep the language that was chosen back');
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
    const refused = expectOldSessionRefused();
    await page.locator('#pair-code').waitFor({ state: 'visible', timeout: 40000 });
    pageReachedApp();
    // The eighth character of the code pairs.
    await page.fill('#pair-code', again[1]);
    await page.locator('#unlock-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    withdraw(refused);
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

  await step('the addresses from before lead where their pages are now, and the short ones are the simple pages', async () => {
    const opens = async (typed, address, shown) => {
      await page.goto(`${base}${typed}`);
      await page.locator(shown).first().waitFor({ state: 'visible', timeout: WAIT });
      await eventually(async () => new URL(page.url()).hash === address, WAIT, `"${typed}" does not become "${address}": ${page.url()}`);
    };
    // Four of the six pages keep their word, under "#/pro/".
    await opens('#/overview', '#/pro/overview', '.page-body[data-key="view-pro/overview"]');
    await opens('#/apps', '#/pro/apps', '.page-body[data-key="view-pro/apps"]');
    await opens('#/vault', '#/pro/vault', '.page-body[data-key="view-pro/vault"]');
    await opens('#/approvals', '#/pro/approvals', '.page-body[data-key="view-pro/approvals"]');
    // "#/documents" and "#/activity" meant two of the six pages and are the simple pages now.
    await opens('#/documents', '#/documents', '.simple .page-body[data-key="view-documents"]');
    await opens('#/activity', '#/activity', '.simple .page-body[data-key="view-activity"]');
    check((await count(FRAME.pro)) === 0, 'a simple page stands in the frame of the six pages');
    // The six pages of the same words are one press away: the link at the foot of the simple page.
    await page.locator('#activity-full').click();
    await page.locator('.page-body[data-key="view-pro/activity"]').waitFor({ state: 'attached', timeout: WAIT });
    // The page that was called Basic is Home, and so is every step of the first run that is gone.
    await opens('#/basic', '#/home', '.simple .page-body[data-key="view-home"]');
    await opens('#/setup/app', '#/home', '.simple .page-body[data-key="view-home"]');
    await opens('#/setup', '#/home', '.simple .page-body[data-key="view-home"]');
    // An address that names no page is decided by what the app keeps: this vault opens on the six pages.
    await opens('#/nothing-of-that-name', '#/nothing-of-that-name', '.page-body[data-key="view-pro/overview"]');
  });

  await step('stopped once more, the app again quits with status 0 and an empty standard error', async () => {
    await closePage(page);
    const exit = await stopApp(app, 'SIGINT');
    check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
    check(app.err === '', `standard error: ${app.err}`);
  });

  await finish(browser);
});
