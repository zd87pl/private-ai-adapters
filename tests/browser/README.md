# Browser walk-throughs

Three Node scripts drive the owner's interface in a real browser, against the real `wdva gate app`.

- `journey_demo.js` walks the demo: its guide on the Basic page (a passage that is not released and does not come back by itself, the played app asking again only when a button tells it to, the passage released and read), then in Pro the passphrase asked for again, lock and unlock.
- `journey_real.js` walks a new vault through Pro, from pairing to Ctrl-C: the two steps of the first run skipped, then documents, apps, grants, an agent's calls, approvals, the passphrase asked for again, lock and unlock, a restart.
- `journey_basic.js` walks the first run as the page offers it, and the Basic view, in five runs with a new vault each: the app, a document, the first question with the look at the settings file, the first request and the first approval; the Basic page empty, on an ordinary day, with a passage waiting, with five waiting, locked, and for a vault that was set up in Pro; the switch between the two views across restarts; a reload, a lock and a restart in the middle of the first run; the app stopped.

To run them, from the top of the repository:

    python -m pip install -e ".[gate]"
    npm install --no-save playwright@1.63.0 && npx playwright install chromium
    node tests/browser/journey_demo.js && node tests/browser/journey_real.js && node tests/browser/journey_basic.js

All three start the app through `launch_app.py`, which changes how long owner mode lasts and gives the app an empty home of its own (`HOME`, `XDG_CONFIG_HOME`), so that "Check the file" never reads the settings of a program on this computer. Where `journey_basic.js` has the page look at the settings file of an AI app, the file is one the script put into that home.

Each step prints `ok` or `FAIL`; the exit status is 1 when a step failed, the page logged an error, a request failed, or an HTTP error came that no step announced. The head of each script lists its options (`GATE_PYTHON`, `GATE_BROWSER`, `GATE_SHOTS`, `GATE_SCALE`, `GATE_PORT`, `GATE_OWNER_SECONDS`; for `journey_basic.js` also `GATE_RUNS`, which walks only the runs it names, for example `GATE_RUNS=A,D`). Screenshots go to `shots/` in this directory: `demo-…` from the demo, `01-…` to `25-…` from the walk through Pro, `b01-…` to `b16-…` from the first run and the Basic view. A step that failed leaves a picture named `…FAILED-step-NN`.

`journey_basic.js` also counts what the offered first run costs a person, from "Create the vault" to the first request of their own AI app seen on the page: presses, the addresses the page was at, fields typed into, file dialogs. It prints the count and fails above its limits: 8 presses, 3 addresses, no typed field and 1 file dialog for Claude Desktop; 6 presses for Claude Code. For Claude Desktop the design counts 7, with one line for a terminal that opens the settings file and holds the text; that line is on the page only after it has been tried against the real applications, and until then the place of the file and the text are two things to copy. Each of its five runs ends by looking at what the page did in the browser: it asked no address but the app's, and it keeps nothing there but its session.

The scripts keep to what Chromium, WebKit and Firefox all do: no launch options, a wait is a wait for a condition, the app's state is read over its own API, and what a "Copy" button copied is read back from the clipboard only in Chromium, the one engine that lets a test do that; everywhere the text is compared with what the page shows beside the button.

On a development machine the three take about one, two and two minutes in Chromium. CI runs all three on every push (job "Browser" in `.github/workflows/tests.yml`): on Linux in Chromium, and on macOS in WebKit, the engine of Safari.
