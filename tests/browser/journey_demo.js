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
// long owner mode lasts. The walk first follows the guide of the demo on the Basic
// page: the played app asks for a passage, "Don't release" is not undone by a clock,
// and after either answer the played app asks again only when the walk presses the
// button that tells it to. Then, in Pro, it waits until owner mode is over, raises a
// limit (the passphrase is asked for and shown, one click fills it in), locks the
// vault, unlocks it with the passphrase the page shows, and sees the played agent go on.
//
// Every step prints "ok" or "FAIL". Exit status 1 when a step failed, the page
// logged an error, a request failed, or an HTTP error came that no step announced.

'use strict';

const fs = require('fs');
const { FRAME, WAIT, apiGet, check, deadline, decideApproval, eventually, expectHttp, finish, newPage, note, openBrowser, readOptions, run, shot, sleep, startApp, started, step, stopApp, switchView } = require('./common');

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
  demo: 'Demo', // the badge beside the name, and at the head of the guide
  // The guide of the demo: its title at each moment, and two of its sentences.
  watch: 'Watch an app ask for documents',
  turn: 'Your turn',
  eitherAnswer: 'Either answer is fine.',
  kept: 'You did not release it',
  releasedOnce: 'You released it once',
  read: 'It asked again and got the passage',
  haveReady: 'Have ready: a passphrase you can type from memory (it cannot be recovered), one document, and the AI app you use.',
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
  // The guide of the demo, on the Basic page.
  const guide = page.locator('section.card-guide[data-key="demo-guide"]');
  /** The title of the guide, once it has found its place from the lists of the page. */
  const guideTitle = async () => ((await guide.locator('#guide-title').textContent()) || '').trim();
  const guideSays = (title, timeout = WAIT) => eventually(async () => (await guideTitle()) === title, timeout, `the guide does not say "${title}"`);
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

  await step('the demo opens unlocked on the Basic page, with its badge and its guide, and nothing to set up', async () => {
    await page.goto(base);
    await page.locator(FRAME.basic).waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('.topbar .brand .chip').textContent()).trim() === TEXT.demo, 'the top bar does not carry the badge of the demo');
    const now = await state();
    check(now.demo === true && now.locked === false, `the demo is not unlocked: ${JSON.stringify({ demo: now.demo, locked: now.locked })}`);
    check(now.demo_passphrase === passphrase, 'the state does not carry the passphrase the terminal shows');
    storePath = now.store.path;
    // The guide stands under "Approvals", with the badge at its head.
    await guide.waitFor({ state: 'visible', timeout: WAIT });
    check((await guide.locator('.card-head .chip').textContent()).trim() === TEXT.demo, 'the guide does not carry the badge of the demo');
    const before = (await approvals()).waiting.length;
    const title = await eventually(async () => ((await guide.locator('.guide-text').count()) > 0 ? guideTitle() : ''), WAIT, 'the guide did not find its place');
    const after = (await approvals()).waiting.length;
    // The played app asks for its passage ten to twenty seconds after the start. A page that came up later than that is told so.
    if (before === 0 && after === 0) check(title === TEXT.watch, `while nothing waits the guide says: ${title}`);
    else note('the played app had asked for its passage before the page was up, so the first words of the guide were not seen');
    check((await page.locator('#guide-own').getAttribute('aria-expanded')) === 'false', 'the way to a vault of one\'s own is open before anything was decided');
    check((await guide.locator('.guide-own').count()) === 0, 'the last part of the guide is shown before anything was decided');
    check((await guide.locator('#demo-again').count()) === 0, 'the guide offers to ask again before anything was decided');
    // Nothing of a vault that is being set up, no choice above the guide, and the lower parts folded to their headings.
    check((await page.locator('.card-setup').count()) === 0, 'the demo shows the card of a vault that is being set up');
    check((await page.locator('.strip').count()) === 0, 'the Basic page shows the choice between the kinds of apps');
    check((await page.locator('.basic-foot .launch').count()) === 0, 'the foot of the demo says how to open the demo\'s vault again');
    await page.locator('#fold-also').waitFor({ state: 'visible', timeout: WAIT });
    for (const part of ['documents', 'apps', 'also']) {
      check((await page.locator(`#fold-${part}`).getAttribute('aria-expanded')) === 'false', `"${part}" is not folded while the guide is open`);
    }
    check((await page.locator('.basic .drop, .basic .basic-app, .basic .also-list').count()) === 0, 'a folded part of the page shows what is in it');
    check((await page.locator('.btn-primary').count()) === 0, 'a button of the demo\'s page is filled');
    await picture('00-guide');
    // A folded part opens and closes on its own button.
    await page.locator('#fold-apps').click();
    await page.locator('.basic .basic-app').first().waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('#fold-apps').getAttribute('aria-expanded')) === 'true', 'the button of an open part does not say that it is open');
    await page.locator('#fold-apps').click();
    await eventually(async () => (await page.locator('.basic .basic-app').count()) === 0, WAIT, '"Apps" did not fold again');
  });

  await step('the played app asks: the whole card is in the window, "Don\'t release" stays, and it asks again only on a press', async () => {
    await stack.first().waitFor({ state: 'visible', timeout: 90000 });
    const waiting = (await approvals()).waiting.find((item) => item.app === HONEST);
    check(waiting && waiting.mode === 'approve' && waiting.hosted === true, `the passage waits for another reason: ${JSON.stringify(waiting && { mode: waiting.mode, hosted: waiting.hosted })}`);
    decidedRef = waiting.ref;
    const key = `ap-${waiting.app}-${waiting.ref}`;
    const card = page.locator(`.approval[data-key="${key}"]`);
    await card.waitFor({ state: 'visible', timeout: WAIT });
    check((await card.locator('pre.released').textContent()) === waiting.body, 'the card shows other text than would be released');
    check((await card.locator('.approval-head').textContent()).includes(waiting.destination), `the card does not say where the text goes (${waiting.destination})`);
    // The card is whole, with both answers, between the top bar and the bar with the view: nothing has to be scrolled.
    const size = page.viewportSize();
    const top = await page.locator('.basic-top').boundingBox();
    const foot = await page.locator('footer.viewbar').boundingBox();
    const box = await card.boundingBox();
    check(box.y >= top.y + top.height && box.y + box.height <= foot.y && foot.y + foot.height <= size.height + 1, `the card is not whole in the window: ${JSON.stringify({ card: box, top, foot, size })}`);
    for (const answer of ['dismiss', 'release']) {
      const button = await card.locator(`button[data-key="${answer}"]`).boundingBox();
      check(button.y >= box.y && button.y + button.height <= foot.y && button.x >= 0 && button.x + button.width <= size.width, `"${answer}" is not in the window`);
    }
    await guideSays(TEXT.turn);
    check((await guide.locator('.guide-text').first().textContent()).includes(TEXT.eitherAnswer), 'the guide does not say that either answer is fine');
    check((await guide.locator('.guide-text').first().textContent()).includes(`under the name ${HONEST},`), 'the guide does not name the app by its stored name');
    check((await page.locator('.btn-primary').count()) === 0, 'a button is filled while a passage waits');
    check((await card.boundingBox()).y < (await guide.boundingBox()).y, 'the guide stands above the card');
    await picture('00b-your-turn');

    // "Don't release". The played app is not told, and no clock makes it ask again.
    await decideApproval(page, key, 'dismiss');
    await guideSays(TEXT.kept);
    check((await guide.locator('#demo-again').count()) === 1, 'after "Don\'t release" the guide does not offer to ask again');
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
    check((await guideTitle()) === TEXT.kept, `after the watch the guide says: ${await guideTitle()}`);

    // "Have the played app ask again": the passage waits again, as with a real app that is told "try again".
    await guide.locator('#demo-again').click();
    await card.waitFor({ state: 'visible', timeout: WAIT });
    await guideSays(TEXT.turn);
    check((await approvals()).waiting.length === 1, 'the passage does not wait again');

    // "Release once". Nothing is read until the played app is told to ask again.
    await decideApproval(page, key, 'release');
    await guideSays(TEXT.releasedOnce);
    check((await approvals()).granted.some((item) => item.app === HONEST && item.ref === decidedRef), 'the approval is not listed as approved and not read');
    check(!(await approvedRead(decidedRef)), 'the passage was read before the played app was told to ask again');
    check((await guide.locator('#demo-again').count()) === 1, 'after "Release once" the guide does not offer the same button');
    await guide.locator('#demo-again').click();
    const read = await eventually(() => approvedRead(decidedRef), WAIT, 'the approved passage was not read');
    const row = page.locator(`.act-list li.act-item[data-key="a${read.seq}"]`);
    await row.waitFor({ state: 'visible', timeout: WAIT });
    check((await row.locator('.act-status').textContent()).trim() === TEXT.afterApproval, `the read is not marked as released after approval: ${await row.locator('.act-status').textContent()}`);
    await guideSays(TEXT.read);
    check((await guide.locator('#demo-again').count()) === 0, 'the guide still offers to ask again after the passage was read');
    check((await approvals()).waiting.length === 0 && (await approvals()).granted.length === 0, 'something still waits or stands approved');

    // The end of the guide: the command for a vault of one's own, which is not the demo's.
    const own = guide.locator('.guide-own');
    const command = (await own.locator('pre.code').textContent()).trim();
    check(command === (await state()).launch.command, `the guide shows another command than the app sent: ${command}`);
    check(command.includes(' gate app') && !command.includes('--demo') && !command.includes(storePath), `the command starts the demo again, or names its store: ${command}`);
    check((await own.locator('#guide-copy').count()) === 1, 'the command has no "Copy"');
    check((await own.textContent()).includes(TEXT.haveReady), 'the guide does not say what to have ready');
    check((await page.locator('.btn-primary').count()) === 0, 'a button of the demo\'s page is filled');
    await picture('00c-own-vault');

    // "Hide" takes the guide away and unfolds what was folded while it was open.
    await guide.locator('#guide-hide').click();
    await eventually(async () => (await guide.count()) === 0, WAIT, 'the guide did not go');
    await page.locator('.basic .basic-app').first().waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('#fold-documents, #fold-apps, #fold-also').count()) === 0, 'a part of the page is still folded after the guide was hidden');
  });

  await step('one press shows the same demo in Pro, without anything to set up', async () => {
    await switchView(page, 'pro');
    const product = await page.locator('meta[name="application-name"]').getAttribute('content');
    check(product && (await page.title()).includes(product), `the title does not carry the name of the app: ${await page.title()}`);
    await page.locator('.sidebar .brand .chip').waitFor({ state: 'visible', timeout: WAIT });
    // Once the apps are listed the page knows enough to offer the list for a new vault, and must not.
    await page.locator('.left-list .left-item').first().waitFor({ state: 'visible', timeout: WAIT });
    check((await page.locator('.card-start').count()) === 0, 'the demo shows the list for a new vault');
    check((await page.locator('.card-guide').count()) === 0, 'the guide of the Basic page stands on a page of Pro');
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
    lockedHead = after.record.head_seq;
    await page.click('#demo-pass-fill');
    await eventually(async () => (await field.inputValue()) === passphrase, WAIT, 'the button did not fill the field');
    await page.locator('.gate-card button[type="submit"]').click();
    await page.locator('aside.sidebar').waitFor({ state: 'visible', timeout: 40000 });
    check((await state()).locked === false, 'the vault did not unlock');
  });

  await step('unlocked again, the played agent goes on, and what was decided before the lock is in the record', async () => {
    // The passage was decided on the Basic page. The played app, which has read it, asks a light question from time to time.
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
