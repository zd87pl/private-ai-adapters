// The demo of the app, walked in a browser: nothing in it may be a dead end.
//
// Run it from the top of the repository (after "npm install --no-save playwright@1.63.0"
// and "npx playwright install chromium"):
//
//   GATE_PYTHON=.venv/bin/python node tests/browser/journey_demo.js
//
// Options (environment), all optional:
//   GATE_PYTHON         the Python that has wdva installed (default: python3)
//   GATE_BROWSER        chromium (default), webkit or firefox
//   GATE_SHOTS          where the screenshots go (default: shots/ next to this file)
//   GATE_SCALE          device scale factor of the screenshots (default: 1)
//   GATE_PORT           port of the interface (default: the app picks a free one)
//   GATE_OWNER_SECONDS  how long owner mode lasts in this run (default: 30; the app's own value is 600)
//
// The app is started with --demo through launch_app.py, which only shortens how
// long owner mode lasts. The walk first follows the demo's card on Home: the played
// app asks for a passage, "Don't release" is not undone by a clock, and after either
// answer the played app asks again only when the walk presses the button that tells
// it to. On the way it asks the box on Home a question of its own, which sends
// nothing anywhere, and presses a tile, which in a demo connects nothing and looks at
// no file. Then it waits until owner mode is over: "Ask" asks for the passphrase,
// and so, on the six pages under Settings, does a raised limit (the passphrase is
// shown, one click fills it in). It locks the vault, unlocks it with the passphrase
// the page shows, and sees the played agent go on.
//
// Every step prints "ok" or "FAIL". Exit status 1 when a step failed, the page
// logged an error, a request failed, or an HTTP error came that no step announced.

'use strict';

const fs = require('fs');
const { FRAME, WAIT, apiGet, check, deadline, decideApproval, eventually, expectHttp, finish, newPage, note, openAllSettings, openBrowser, openPage, readOptions, run, shot, sleep, startApp, started, step, stopApp, textTable } = require('./common');

const options = readOptions({ ownerSeconds: 30 });

/** The app the demo's agent plays honestly: it may read one collection and must ask for another. */
const HONEST = 'claude-desktop';
/** How long the locked vault is watched to see that nothing is recorded; the played agent calls every 2 to 4 seconds. */
const LOCKED_WATCH_MS = 6000;
/** How long the page is watched after "Don't release" to see that the passage does not come back by itself. */
const KEPT_WATCH_MS = 15000;
/** The played app asks a light question every 30 seconds once its passage was read; the script may still be playing before that. */
const LIGHT_QUESTION_WAIT = 120000;

/** The few labels of the interface this walk reads. Everything else is found by role, id or class. */
const TEXT = {
  question: 'Question', // the chip of a call that asked what the app may use
  released: /^Released/, // the chip of a call that handed out text
  afterApproval: 'Released after approval',
  limits: 'Limits', // the button on an app's card
  copies: /cop(y|ies) of the files/i, // the fact on the vault screen about what was kept of added files
  none: 'None',
  originalsKept: /originals? kept/i, // what that fact must not be called
  ownVault: 'To start with your own documents: ', // what the line printed when a demo stops begins with
  // (The words of Home, of its card for the demo and of its sheets are read from the page's own table.)
};

run(async () => {
  deadline(6);
  const browser = await openBrowser(options, 'demo-');
  const app = startApp(options, { args: ['--demo'] });
  const page = await newPage(browser, options);

  let base = '';
  let passphrase = '';
  let storePath = '';
  /** The passage the played app asked for and, after the guide was followed, read: its reference. */
  let decidedRef = '';
  /** The newest entry of the record while the vault was locked. */
  let lockedHead = 0;
  const state = () => apiGet(page, base, '/api/state');
  const apps = () => apiGet(page, base, '/api/apps');
  const approvals = () => apiGet(page, base, '/api/approvals');
  const activity = async () => (await apiGet(page, base, '/api/activity?limit=200')).items;
  /** A picture of the page once the messages in its corner have gone, so that they cover nothing of it. */
  const picture = async (name) => {
    await eventually(async () => (await page.locator('#toasts .toast').count()) === 0, WAIT).catch(() => {});
    await shot(page, `demo-${name}`);
  };
  /** Open one of the six pages with every setting: from one of them by its entry, from a simple page through Settings. */
  const go = async (route) => {
    if ((await page.locator(FRAME.pro).count()) === 0) await openAllSettings(page, route);
    else await page.click(`a.nav-item[href="#/pro/${route}"]`);
    await page.locator(`.page-body[data-key="view-pro/${route}"]`).waitFor({ state: 'attached', timeout: WAIT });
  };
  /** The open sheet that holds the element `selector` names. */
  const sheetWith = (selector) => page.locator('dialog[open]', { has: page.locator(selector) });
  const noSheet = () => eventually(async () => (await page.locator('dialog[open]').count()) === 0, WAIT, 'a sheet stayed open');
  const kindFilter = (value) => page.locator('.filters label.segment', { has: page.locator(`input[value="${value}"]`) });
  const chips = async () => (await page.locator('.act-row .act-status').allTextContents()).map((text) => text.trim());
  /** The English table of the page, read from the app as the page reads it. */
  let en = {};
  /** Every request the page sent that was not a question: "POST /api/try", in order. */
  const changes = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET') changes.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  const trimmed = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  // The demo's card, on Home.
  const guide = page.locator('section.guide[data-key="demo-guide"]');
  /** The title of the card, once it has found its place from the lists of the page. */
  const guideTitle = async () => ((await guide.locator('#guide-title').textContent()) || '').trim();
  /** Wait until the card shows the moment `beat` names: its title and its one sentence, from the table. */
  const guideSays = (beat, timeout = WAIT) =>
    eventually(
      async () => {
        const title = await guideTitle();
        check(title === en[`guide.${beat}.title`], `the card of the demo says "${title}", not "${en[`guide.${beat}.title`]}"`);
        const said = trimmed(await guide.locator('#guide-say').textContent());
        check(said === en[`guide.${beat}.say.1`].replace('{product}', product), `the card of the demo says: ${said}`);
        return true;
      },
      timeout,
    );
  let product = '';
  const stack = page.locator('.approval-stack > .approval');
  /** The read of one passage that followed an approval, in the record. */
  const approvedRead = async (ref) => (await activity()).find((item) => item.kind === 'release' && item.approved === true && item.ref === ref);

  await step('the terminal shows the demo passphrase as one more start-up line', async () => {
    const lines = await started(app);
    base = app.url;
    check(lines.length === 5, `expected five lines, got ${JSON.stringify(lines)}`);
    check(/^store: [^/]/.test(lines[1]), `the store line names a path: ${lines[1]}`);
    const found = /^demo passphrase: (\S+)\s/.exec(lines[3]);
    check(found, `line 4 does not give the demo's passphrase: ${lines[3]}`);
    passphrase = found[1];
    check(!app.out.includes('pairing code'), 'a demo prints a pairing code');
    if (options.port) check(app.port === options.port, `the app listens on ${app.port}, not on ${options.port}`);
  });
  // Without the app there is nothing to walk.
  if (!base) await finish(browser);

  await step('the demo opens unlocked on Home, in the frame of four entries, with its badge and the demo\'s card; Home is not drawn as the three cards', async () => {
    await page.goto(base);
    await page.locator(FRAME.simple).waitFor({ state: 'visible', timeout: WAIT });
    en = await textTable(page, base, 'en');
    product = await page.locator('meta[name="application-name"]').getAttribute('content');
    check((await page.locator('.side-brand .chip').textContent()).trim() === en['common.chip.demo'], 'the sidebar does not carry the badge of the demo');
    check((await page.locator(`${FRAME.simple} a`).count()) === 4, 'the sidebar does not have four entries');
    check((await page.locator(`${FRAME.simple} a[aria-current="page"]`).getAttribute('href')) === '#/home', 'the entry of Home is not the current one');
    const now = await state();
    check(now.demo === true && now.locked === false, `the demo is not unlocked: ${JSON.stringify({ demo: now.demo, locked: now.locked })}`);
    check(now.demo_passphrase === passphrase, 'the state does not carry the passphrase the terminal shows');
    storePath = now.store.path;
    // The card stands under "Waiting for you" and above the box, with the badge at its head.
    await guide.waitFor({ state: 'visible', timeout: WAIT });
    check((await guide.locator('.guide-head .chip').textContent()).trim() === en['common.chip.demo'], 'the card does not carry the badge of the demo');
    const before = (await approvals()).waiting.length;
    const title = await eventually(async () => ((await guide.locator('#guide-say').count()) > 0 ? guideTitle() : ''), WAIT, 'the card of the demo did not find its place');
    const after = (await approvals()).waiting.length;
    // The played app asks for its passage ten to twenty seconds after the start. A page that came up later than that is told so.
    if (before === 0 && after === 0) check(title === en['guide.watch.title'], `while nothing waits the card says: ${title}`);
    else note('the played app had asked for its passage before the page was up, so the first words of the demo\'s card were not seen');
    check((await guide.locator('.guide-own').count()) === 0, 'the way to a vault of one\'s own is shown before anything was decided');
    check((await guide.locator('#demo-again').count()) === 0, 'the card offers to ask again before anything was decided');
    // Home is not drawn as the three cards of a new vault: its parts stand there once, under their own headings.
    check((await page.locator('#first-title, .first-card').count()) === 0, 'the demo shows the three cards of a new vault');
    await page.locator('#tile-claude').waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('#ask-input').count()) === 1 && (await page.locator('.tile').count()) === 4, 'the box or the four tiles are missing, or drawn twice');
    check((await page.locator('#home-ask-title').textContent()).trim() === en['home.ask.title'] && (await page.locator('#home-connect-title').textContent()).trim() === en['home.connect.title'], 'the parts of Home are not under their headings');
    // The demo's vault holds documents, so "Ask" may be the one filled button; nothing else is.
    const filled = (await page.locator('.btn-primary').allTextContents()).map(trimmed);
    check(filled.every((text) => text === en['ask.button.ask']) && filled.length <= 1, `the filled buttons are ${JSON.stringify(filled)}`);
    await picture('00-guide');
  });

  let key = '';
  const card = () => page.locator(`.approval[data-key="${key}"]`);

  await step('the played app asks: the whole card is in the window, "Don\'t release" stays, and nothing asks again by itself', async () => {
    await stack.first().waitFor({ state: 'visible', timeout: 90000 });
    const waiting = (await approvals()).waiting.find((item) => item.app === HONEST);
    check(waiting && waiting.mode === 'approve' && waiting.hosted === true, `the passage waits for another reason: ${JSON.stringify(waiting && { mode: waiting.mode, hosted: waiting.hosted })}`);
    decidedRef = waiting.ref;
    key = `ap-${waiting.app}-${waiting.ref}`;
    await card().waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('#home-wait-title').textContent()).trim() === en['home.wait.title'], 'the card does not stand under "Waiting for you"');
    check((await card().locator('pre.released').textContent()) === waiting.body, 'the card shows other text than would be released');
    check((await card().locator('.approval-head').textContent()).includes(waiting.destination), `the card does not say where the text goes (${waiting.destination})`);
    // Its two sentences and its two answers are the texts of the table.
    const read = trimmed(await card().textContent());
    for (const text of [en['card.say.1'].replace('{app}', waiting.app).replace('{destination}', waiting.destination), en['card.button.no'], en['card.button.yes']]) check(read.includes(text.split('{')[0].trim()), `the card does not say: ${text}`);
    // The card is whole in the window, with both answers, in the one column beside the sidebar: nothing has to be scrolled.
    const size = page.viewportSize();
    const side = await page.locator('aside.side').boundingBox();
    const box = await card().boundingBox();
    check(box.y >= 0 && box.y + box.height <= size.height && box.x >= side.x + side.width && box.x + box.width <= size.width, `the card is not whole in the window: ${JSON.stringify({ card: box, side, size })}`);
    check(box.width <= 640, `the card is wider than the column: ${box.width}`);
    for (const answer of ['dismiss', 'release']) {
      const button = await card().locator(`button[data-key="${answer}"]`).boundingBox();
      check(button.y >= box.y && button.y + button.height <= size.height && button.x >= 0 && button.x + button.width <= size.width, `"${answer}" is not in the window`);
    }
    await guideSays('turn');
    check(en['guide.turn.say.1'].includes('either answer is fine'), 'the card of the demo does not say that either answer is fine');
    await eventually(async () => (await page.locator('.btn-primary').count()) === 0, WAIT, 'a button is filled while a passage waits');
    check((await card().boundingBox()).y < (await guide.boundingBox()).y, 'the card of the demo stands above the passage that waits');
    // The picture shows the card as it is once "Release once" can be pressed, a moment after it appeared.
    await card().locator('button[data-key="release"]:not(.is-waiting)').waitFor({ state: 'visible', timeout: WAIT });
    await picture('00b-your-turn');

    // "Don't release". The played app is not told, and no clock makes it ask again.
    await decideApproval(page, key, 'dismiss');
    await guideSays('kept');
    check((await guide.locator('#demo-again').count()) === 1, 'after "Don\'t release" the card does not offer to ask again');
    check((await guide.locator('.guide-own').count()) === 1, 'after an answer the way to a vault of one\'s own is not shown');
    const asked = (await activity()).filter((item) => item.app === HONEST && item.tool === 'read').reduce((sum, item) => sum + item.count, 0);
    // Not a wait for the page: for this long nothing may happen.
    const until = Date.now() + KEPT_WATCH_MS;
    while (Date.now() < until) {
      check((await stack.count()) === 0, 'the card came back by itself');
      check((await approvals()).waiting.length === 0, 'the passage waits again although nobody asked');
      await sleep(500);
    }
    const askedSince = (await activity()).filter((item) => item.app === HONEST && item.tool === 'read').reduce((sum, item) => sum + item.count, 0);
    check(askedSince === asked, `the played app asked for a passage again by itself: ${askedSince - asked} read(s)`);
    check((await guideTitle()) === en['guide.kept.title'], `after the watch the card says: ${await guideTitle()}`);
  });

  await step('"Ask" in the box asks for the passphrase once its time is over, and what is typed there opens the box and nothing else; the box shows a passage of an invented document and the words it was found by; neither button sends a request; nothing but calls of the played apps is recorded; while a passage waits the answer has no buttons', async () => {
    const names = (await apps()).apps.map((entry) => entry.name);
    const seen = new Set((await activity()).map((item) => item.seq));
    // The box answers without the passphrase for as long as owner mode lasts, which in this walk is short: by now
    // that time is over. The state names the moment.
    const over = Date.parse((await state()).trial.until) + 1500;
    await eventually(() => Date.now() >= over, options.ownerSeconds * 1000 + WAIT, 'the time of the box did not run out');
    const mark = changes.length;
    await page.fill('#ask-input', 'When is the rent due?');
    expectHttp(403, '/api/try');
    await page.locator('#ask-button').click();
    const asked = sheetWith('#reauth-passphrase');
    await asked.locator('#reauth-passphrase').waitFor({ state: 'visible', timeout: WAIT });
    check((await asked.locator('.demo-pass-word').textContent()) === passphrase, 'the sheet of the box shows another passphrase than the terminal');
    await asked.locator('#demo-pass-fill').click();
    await asked.locator('button[type="submit"]').click();
    await page.locator('#trial').waitFor({ state: 'visible', timeout: WAIT });
    await noSheet();
    check(JSON.stringify(changes.slice(mark)) === JSON.stringify(['POST /api/try', 'POST /api/reauth', 'POST /api/try']), `"Ask" sent ${changes.slice(mark).join(', ')}`);
    // What was typed there opened the box and nothing else: a change that lets more leave still asks (see below).
    const renewed = await state();
    check(Date.parse(renewed.trial.until) > Date.now() && Date.parse(renewed.owner_mode_until) < Date.now(), `the passphrase of the box renewed more than the box: ${JSON.stringify({ trial: renewed.trial.until, owner: renewed.owner_mode_until })}`);
    const questions = renewed.trial.questions - 1;
    const titles = (await apiGet(page, base, '/api/documents')).collections.flatMap((group) => group.documents.map((one) => one.title));
    const meta = trimmed(await page.locator('#trial-meta').textContent());
    check(titles.some((one) => meta.includes(one)), `the answer names no document of the demo: ${meta}`);
    check(trimmed(await page.locator('#trial-words').textContent()).startsWith(en['ask.status.words'].replace('{words}', '')), 'the answer does not say by which words the passage was found');
    check(trimmed(await page.locator('#trial-say-2').textContent()) === en['ask.say.2'], 'the answer does not say that nothing is sent and nothing recorded');
    await picture('00d-trial');
    // Neither button sends anything: each says what would have happened.
    await page.locator('#trial-yes').click();
    await eventually(async () => trimmed(await page.locator('#trial-result').textContent()) === en['ask.result.yes'], WAIT, '"Release once" under the answer does not say what would have happened');
    await page.locator('#trial-again').click();
    await page.fill('#ask-input', 'When is the rent due?');
    await page.locator('#ask-button').click();
    await page.locator('#trial-no').click();
    await eventually(async () => trimmed(await page.locator('#trial-result').textContent()) === en['ask.result.no'], WAIT, '"Don\'t release" under the answer does not say what would have happened');
    check(JSON.stringify(changes.slice(mark)) === JSON.stringify(['POST /api/try', 'POST /api/reauth', 'POST /api/try', 'POST /api/try']), `the box and its buttons sent ${changes.slice(mark).join(', ')}`);
    check((await state()).trial.questions === questions + 2, 'the app does not count the two questions');
    // In a demo the record grows by itself: the played apps call all the time. So the head of the record says
    // nothing here (journey_home.js compares it in a vault where nobody else calls). What can be seen is that
    // every entry since the first question is a call of a played app, and that nothing waits or was released for the owner.
    const since = (await activity()).filter((item) => !seen.has(item.seq));
    check(since.every((item) => names.includes(item.app) && ['release', 'question', 'refusal'].includes(item.kind)), `since the question the record gained: ${JSON.stringify(since.filter((item) => !names.includes(item.app)).map((item) => [item.kind, item.app]))}`);
    check((await approvals()).waiting.length === 0 && (await approvals()).granted.length === 0, 'a question in the box made a passage wait, or released one');
    // The played app asks again, on a press: while its passage waits, the answer of the box has no buttons, and says why.
    await page.locator('#trial-again').click();
    await page.fill('#ask-input', 'When is the rent due?');
    await page.locator('#ask-button').click();
    await page.locator('#trial-yes').waitFor({ state: 'visible', timeout: WAIT });
    await guide.locator('#demo-again').click();
    await card().waitFor({ state: 'visible', timeout: WAIT });
    await guideSays('turn');
    check((await approvals()).waiting.length === 1, 'the passage does not wait again');
    await eventually(async () => (await page.locator('#trial-real').count()) === 1 && (await page.locator('#trial-yes, #trial-no').count()) === 0, WAIT, 'the answer of the box keeps its buttons while a real passage waits');
    check(trimmed(await page.locator('#trial-real').textContent()) === en['ask.status.real'], 'the answer does not say that a real request waits');
  });

  await step('"Release once": nothing is read until the played app is told to ask again; then the card of the demo shows the way to a vault of one\'s own, and "Hide" takes it away', async () => {
    await decideApproval(page, key, 'release');
    await guideSays('released');
    check((await approvals()).granted.some((item) => item.app === HONEST && item.ref === decidedRef), 'the approval is not listed as released and not read');
    check(!(await approvedRead(decidedRef)), 'the passage was read before the played app was told to ask again');
    check((await guide.locator('#demo-again').count()) === 1, 'after "Release once" the card does not offer the same button');
    await guide.locator('#demo-again').click();
    const read = await eventually(() => approvedRead(decidedRef), WAIT, 'the released passage was not read');
    await guideSays('read');
    check((await guide.locator('#demo-again').count()) === 0, 'the card still offers to ask again after the passage was read');
    check((await approvals()).waiting.length === 0 && (await approvals()).granted.length === 0, 'something still waits or stands released');
    // The played apps go on calling, so the row may have left the three newest on Home: the page Activity has it.
    await openPage(page, 'activity');
    const row = page.locator(`#main li.row-item[data-key="a${read.seq}"]`);
    await row.waitFor({ state: 'visible', timeout: WAIT });
    check(trimmed(await row.locator('.chip').first().textContent()) === en['row.chip.approved'], `the read is not marked as given after a yes: ${await row.textContent()}`);
    await openPage(page, 'home');
    await guide.waitFor({ state: 'visible', timeout: WAIT });
    await guideSays('read');

    // The end of the card: the command for a vault of one's own, which is not the demo's.
    const own = guide.locator('.guide-own');
    const command = (await own.locator('pre.code').textContent()).trim();
    check(command === (await state()).launch.command, `the card shows another command than the app sent: ${command}`);
    check(command.includes(' gate app') && !command.includes('--demo') && !command.includes(storePath), `the command starts the demo again, or names its store: ${command}`);
    check((await own.locator('#guide-copy').count()) === 1, 'the command has no "Copy"');
    check(trimmed(await own.locator('#guide-own').textContent()) === en['guide.say.2'], 'the card does not say what the command is for');
    await picture('00c-own-vault');

    // "Hide" takes the card away; the parts of Home stay where they were.
    await guide.locator('#guide-hide').click();
    await eventually(async () => (await guide.count()) === 0, WAIT, 'the card of the demo did not go');
    check((await page.locator('#ask-input').count()) === 1 && (await page.locator('.tile').count()) === 4, 'a part of Home went with the card');
  });

  await step('a tile in the demo says that nothing can be connected, and asks for no look', async () => {
    const mark = changes.length;
    check((await apps()).tiles.every((tile) => tile.path === null), 'the app names the place of a settings file in a demo');
    for (const word of ['claude', 'chatgpt', 'grok']) {
      await page.locator(`#tile-${word}`).click();
      const sheet = page.locator('dialog[open] .connect[data-state="refused"][data-word="demo"]');
      await sheet.waitFor({ state: 'visible', timeout: WAIT });
      check(trimmed(await sheet.locator('#connect-say-1').textContent()) === en['connect.refused.say.1.demo'], `the sheet says: ${await sheet.locator('#connect-say-1').textContent()}`);
      check((await sheet.locator('#connect-go, #connect-byhand, #connect-look').count()) === 0, 'the sheet offers to connect, to paste or to look');
      const read = await page.locator('dialog[open]').textContent();
      for (const part of ['claude_desktop_config', '.codex', '.grok', 'config.toml', 'Application Support']) check(!read.includes(part), `the sheet names the place of a settings file: ${part}`);
      // What it offers is the command that starts a vault of one's own.
      check((await sheet.locator('pre.code').textContent()).trim() === (await state()).launch.command, 'the sheet does not show the command for a vault of one\'s own');
      if (word === 'claude') await picture('00e-tile-in-demo');
      await sheet.locator('#connect-close').click();
      await noSheet();
    }
    check(changes.slice(mark).every((sent) => !sent.includes('/api/connect/')), `a press on a tile asked for a look: ${changes.slice(mark).join(', ')}`);
  });

  await step('two presses, "Settings" and "Overview", show the same demo on the six pages, without anything to set up', async () => {
    await page.locator(`${FRAME.simple} a[href="#/settings"]`).click();
    await page.locator('.link-rows').waitFor({ state: 'visible', timeout: WAIT });
    // A demo is not opened again by the command the app names: Settings does not show it.
    check((await page.locator('#settings-launch-copy').count()) === 0, 'Settings says how to open the demo\'s vault again');
    check((await page.locator('.link-rows a.row-link').count()) === 6, 'Settings does not list six pages');
    await page.locator('a.row-link[href="#/pro/overview"]').click();
    await page.locator(FRAME.pro).waitFor({ state: 'visible', timeout: WAIT });
    check(new URL(page.url()).hash === '#/pro/overview', `the address is ${new URL(page.url()).hash}`);
    // Looking at the six pages keeps nothing: the app still opens on Home.
    check((await state()).interface.view === 'basic', 'a look at the six pages changed where the app opens');
    const product = await page.locator('meta[name="application-name"]').getAttribute('content');
    check(product && (await page.title()).includes(product), `the title does not carry the name of the app: ${await page.title()}`);
    await page.locator('.sidebar .brand .chip').waitFor({ state: 'visible', timeout: WAIT });
    // Once the apps are listed the page knows enough to offer the list for a new vault, and must not.
    await page.locator('.left-list .left-item').first().waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('.card-start').count()) === 0, 'the demo shows the list for a new vault');
    check((await page.locator('.card-guide').count()) === 0, 'the guide of Home stands on one of the six pages');
    check((await page.locator('#pro-home').count()) === 1, 'the sidebar of the six pages has no way back to Home');
    check(app.lines[0] === `${product} ${(await state()).version}`, `the first line is not the name and the version: ${app.lines[0]}`);
  });

  await step('a status call is listed as a question, not as a release', async () => {
    const question = await eventually(async () => (await activity()).find((item) => item.kind === 'question'), 60000, 'the played agent has not asked what it may use');
    check(question.characters === 0, `a question released ${question.characters} characters`);
    await go('activity');
    const row = page.locator(`li.act-item[data-key="a${question.seq}"]`);
    await row.waitFor({ state: 'visible', timeout: WAIT });
    check((await row.locator('.act-status').textContent()).trim() === TEXT.question, 'the status call has another chip');
    await kindFilter('question').click();
    await eventually(async () => {
      const shown = await chips();
      check(shown.length >= 1 && shown.every((chip) => chip === TEXT.question), `the filter for questions shows: ${shown.join(' / ') || 'nothing'}`);
      return true;
    });
    await picture('01-questions');
    await kindFilter('release').click();
    await eventually(async () => {
      const shown = await chips();
      check(shown.length >= 1 && shown.every((chip) => TEXT.released.test(chip)), `the filter for releases shows: ${shown.join(' / ') || 'nothing'}`);
      return true;
    });
    await kindFilter('').click();
    await row.waitFor({ state: 'visible', timeout: WAIT });
  });

  await step('the vault screen says nothing about originals it cannot know about', async () => {
    await go('vault');
    const fact = page.locator('.card[data-key="location"] .fact', { hasText: TEXT.copies });
    await fact.waitFor({ state: 'visible', timeout: WAIT });
    check(!TEXT.originalsKept.test(await page.locator('#main').textContent()), 'the vault screen speaks of originals kept');
    const kept = (await state()).encryption.originals_kept;
    check(kept === 0, `the demo, which added no files, keeps ${kept} copies`);
    check((await fact.locator('dd').textContent()).trim() === TEXT.none, `the demo, which added no files, says: ${await fact.locator('dd').textContent()}`);
  });

  await step('after owner mode has passed, a change asks for the passphrase although it was typed for the box, shows it, and one click fills it in', async () => {
    // The state names the moment; a change sent after it is refused until the passphrase was typed again.
    const over = Date.parse((await state()).owner_mode_until) + 1500;
    await eventually(() => Date.now() >= over, options.ownerSeconds * 1000 + WAIT, 'owner mode did not run out');
    await go('apps');
    const before = (await apps()).apps.find((entry) => entry.name === HONEST).limits.chars_per_day;
    const raised = before + 3000;
    const card = page.locator(`.app-card[data-key="app-${HONEST}"]`);
    await card.getByRole('button', { name: TEXT.limits }).click();
    const limits = sheetWith('#limit-chars_per_day');
    await limits.locator('#limit-chars_per_day').fill(String(raised));
    expectHttp(403, `/api/apps/${HONEST}/limits`);
    await limits.locator('button[type="submit"]').click();
    const again = page.locator('#reauth-passphrase');
    await again.waitFor({ state: 'visible', timeout: WAIT });
    const sheet = sheetWith('#reauth-passphrase');
    check((await sheet.locator('.demo-pass-word').textContent()) === passphrase, 'the sheet shows another passphrase than the terminal');
    await picture('02-reauth-with-passphrase');
    await sheet.locator('#demo-pass-fill').click();
    await eventually(async () => (await again.inputValue()) === passphrase, WAIT, 'the button did not fill the field');
    await sheet.locator('button[type="submit"]').click();
    await eventually(async () => (await apps()).apps.find((entry) => entry.name === HONEST).limits.chars_per_day === raised, WAIT, 'the limit was not raised');
    await noSheet();
    await card.locator(`.meter[aria-valuemax="${raised}"]`).first().waitFor({ state: 'visible', timeout: WAIT });
  });

  await step('"Lock now" is not the end: the locked screen shows the passphrase and one click fills it in', async () => {
    await page.click('button.lock-now');
    const field = page.locator('#unlock-passphrase');
    await field.waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('.demo-pass-word').textContent()) === passphrase, 'the locked screen shows another passphrase');
    await picture('03-locked-with-passphrase');
    const before = await state();
    check(before.locked === true, 'the vault is not locked');
    // Not a wait for the page: for this long nothing may happen. The played agent would have called twice.
    await sleep(LOCKED_WATCH_MS);
    const after = await state();
    check(after.record.head_seq === before.record.head_seq, 'records were written while the vault was locked');
    lockedHead = after.record.head_seq;
    await page.click('#demo-pass-fill');
    await eventually(async () => (await field.inputValue()) === passphrase, WAIT, 'the button did not fill the field');
    await page.locator('.gate-card button[type="submit"]').click();
    // The demo opens on Home, also after it was locked on one of the six pages.
    await page.locator(FRAME.simple).waitFor({ state: 'visible', timeout: 40000 });
    check(new URL(page.url()).hash === '#/home', `after "Unlock" the address is ${new URL(page.url()).hash}`);
    check((await state()).locked === false, 'the vault did not unlock');
  });

  await step('unlocked again, the played agent goes on, and what was decided before the lock is in the record', async () => {
    // The passage was decided on Home. The played app, which has read it, asks a light question from time to time.
    const question = await eventually(
      async () => (await activity()).find((item) => item.app === HONEST && item.kind === 'release' && item.tool === 'search' && item.seq > lockedHead),
      LIGHT_QUESTION_WAIT,
      'the played agent did not go on after the unlock',
    );
    check((await approvals()).waiting.length === 0, 'a passage waits again although nobody told the played app to ask');
    const read = await approvedRead(decidedRef);
    check(read, 'the read that followed the approval is no longer in the record');
    await go('activity');
    await page.locator(`li.act-item[data-key="a${question.seq}"]`).waitFor({ state: 'visible', timeout: WAIT });
    const row = page.locator(`li.act-item[data-key="a${read.seq}"]`);
    await row.waitFor({ state: 'visible', timeout: WAIT });
    check((await row.locator('.act-status').textContent()).trim() === TEXT.afterApproval, `the read is not marked as released after approval: ${await row.locator('.act-status').textContent()}`);
    check(!(await activity()).some((item) => item.code === 'LOCKED'), 'a call to the locked vault was recorded');
    await picture('04-activity-after-unlock');
  });

  await step('the limits of the demo show: the second app is stopped by its daily limit', async () => {
    await eventually(
      async () => {
        const items = await activity();
        check(items.some((item) => item.code === 'OVER_LIMIT' && item.variant === 'client-day'), 'no call was refused by a daily limit yet');
        check(items.some((item) => item.kind === 'refusal' && item.code === 'NOT_ALLOWED'), 'no call under a name nobody added yet');
        return true;
      },
      150000,
    );
    await go('overview');
    const refused = page.locator('.stats .stat').nth(2).locator('.stat-value');
    await eventually(async () => Number((await refused.textContent()).replace(/\D/g, '')) >= 2, WAIT, 'the overview does not count the refused calls');
    await picture('05-overview');
  });

  await step('Ctrl-C ends the demo with status 0, says how to start for real, and removes its store', async () => {
    const exit = await stopApp(app, 'SIGINT');
    check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
    check(app.err === '', `standard error: ${app.err}`);
    // The five start-up lines, and one more when a demo stops: the command for a vault of one's own.
    const printed = app.out.trimEnd().split('\n');
    check(printed.length === 6, `expected the start-up lines and one more, got ${JSON.stringify(printed)}`);
    const last = printed[5];
    check(last.startsWith(TEXT.ownVault), `the last line is not the one about a vault of one's own: ${last}`);
    check(last.includes(' gate app') && !last.includes('--demo') && !last.includes(storePath), `the command starts the demo again, or names its store: ${last}`);
    check(storePath && !fs.existsSync(storePath), `the demo store is still there: ${storePath}`);
  });

  await stopApp(app, 'SIGINT');
  await finish(browser);
});
