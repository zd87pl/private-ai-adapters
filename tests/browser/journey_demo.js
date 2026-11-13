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
// long owner mode lasts. The walk waits until owner mode is over, raises a limit
// (the passphrase is asked for and shown, one click fills it in), locks the vault,
// unlocks it with the passphrase the page shows, and sees the played agent go on.
//
// Every step prints "ok" or "FAIL". Exit status 1 when a step failed, the page
// logged an error, a request failed, or an HTTP error came that no step announced.

'use strict';

const fs = require('fs');
const { WAIT, apiGet, check, deadline, decideApproval, eventually, expectHttp, finish, newPage, openBrowser, readOptions, run, shot, sleep, startApp, started, step, stopApp } = require('./common');

const options = readOptions({ ownerSeconds: 30 });

/** The app the demo's agent plays honestly: it may read one collection and must ask for another. */
const HONEST = 'claude-desktop';
/** How long the locked vault is watched to see that nothing is recorded; the played agent calls every 2 to 4 seconds. */
const LOCKED_WATCH_MS = 6000;

/** The few labels of the interface this walk reads. Everything else is found by role, id or class. */
const TEXT = {
  question: 'Question', // the chip of a call that asked what the app may use
  released: /^Released/, // the chip of a call that handed out text
  afterApproval: 'Released after approval',
  limits: 'Limits', // the button on an app's card
  copies: /cop(y|ies) of the files/i, // the fact on the vault screen about what was kept of added files
  none: 'None',
  originalsKept: /originals? kept/i, // what that fact must not be called
};

run(async () => {
  deadline(6);
  const browser = await openBrowser(options, 'demo-');
  const app = startApp(options, { args: ['--demo'] });
  const page = await newPage(browser, options);

  let base = '';
  let passphrase = '';
  let storePath = '';
  const state = () => apiGet(page, base, '/api/state');
  const apps = () => apiGet(page, base, '/api/apps');
  const activity = async () => (await apiGet(page, base, '/api/activity?limit=200')).items;
  const picture = (name) => shot(page, `demo-${name}`);
  const go = async (route) => {
    await page.click(`a.nav-item[href="#/${route}"]`);
    await page.locator(`.page-body[data-key="view-${route}"]`).waitFor({ state: 'attached', timeout: WAIT });
  };
  /** The open sheet that holds the element `selector` names. */
  const sheetWith = (selector) => page.locator('dialog[open]', { has: page.locator(selector) });
  const noSheet = () => eventually(async () => (await page.locator('dialog[open]').count()) === 0, WAIT, 'a sheet stayed open');
  const kindFilter = (value) => page.locator('.filters label.segment', { has: page.locator(`input[value="${value}"]`) });
  const chips = async () => (await page.locator('.act-row .act-status').allTextContents()).map((text) => text.trim());

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

  await step('the demo opens unlocked, with its badge, and without anything to set up', async () => {
    await page.goto(base);
    await page.locator('aside.sidebar').waitFor({ state: 'visible', timeout: WAIT });
    const product = await page.locator('meta[name="application-name"]').getAttribute('content');
    check(product && (await page.title()).includes(product), `the title does not carry the name of the app: ${await page.title()}`);
    await page.locator('.sidebar .brand .chip').waitFor({ state: 'visible', timeout: WAIT });
    // Once the apps are listed the page knows enough to offer the list for a new vault, and must not.
    await page.locator('.left-list .left-item').first().waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('.card-start').count()) === 0, 'the demo shows the list for a new vault');
    const now = await state();
    check(now.demo === true && now.locked === false, `the demo is not unlocked: ${JSON.stringify({ demo: now.demo, locked: now.locked })}`);
    check(now.demo_passphrase === passphrase, 'the state does not carry the passphrase the terminal shows');
    check(app.lines[0] === `${product} ${now.version}`, `the first line is not the name and the version: ${app.lines[0]}`);
    storePath = now.store.path;
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

  await step('after owner mode has passed, a change asks for the passphrase, shows it, and one click fills it in', async () => {
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
    await page.click('#demo-pass-fill');
    await eventually(async () => (await field.inputValue()) === passphrase, WAIT, 'the button did not fill the field');
    await page.locator('.gate-card button[type="submit"]').click();
    await page.locator('aside.sidebar').waitFor({ state: 'visible', timeout: 40000 });
    check((await state()).locked === false, 'the vault did not unlock');
  });

  await step('unlocked again, the played agent goes on and the waiting passage can be released', async () => {
    await go('approvals');
    // The agent asks again for the passage it was told to wait for, every ten seconds.
    const waiting = await eventually(async () => (await apiGet(page, base, '/api/approvals')).waiting.find((item) => item.app === HONEST), 90000, 'the played agent did not ask again for its passage');
    check(waiting.mode === 'approve' && waiting.hosted === true, `the passage waits for another reason: ${JSON.stringify({ mode: waiting.mode, hosted: waiting.hosted })}`);
    const key = `ap-${waiting.app}-${waiting.ref}`;
    const card = page.locator(`.approval[data-key="${key}"]`);
    await card.waitFor({ state: 'visible', timeout: WAIT });
    check((await card.locator('pre.released').textContent()) === waiting.body, 'the card shows other text than would be released');
    check((await card.locator('.approval-head').textContent()).includes(waiting.destination), `the card does not say where the text goes (${waiting.destination})`);
    await decideApproval(page, key, 'release');
    // The agent's next call for that passage succeeds and is listed as released after approval.
    const read = await eventually(async () => (await activity()).find((item) => item.kind === 'release' && item.approved === true && item.ref === waiting.ref), 60000, 'the approved passage was not read');
    await go('activity');
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

  await step('Ctrl-C ends the demo with status 0 and removes its store', async () => {
    const exit = await stopApp(app, 'SIGINT');
    check(exit && exit.code === 0, `exit: ${JSON.stringify(exit)}`);
    check(app.err === '', `standard error: ${app.err}`);
    check(app.out.trimEnd().split('\n').length === 5, 'more was printed than the start-up lines');
    check(storePath && !fs.existsSync(storePath), `the demo store is still there: ${storePath}`);
  });

  await stopApp(app, 'SIGINT');
  await finish(browser);
});
