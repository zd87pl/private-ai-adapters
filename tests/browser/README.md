# Browser walk-throughs

Two Node scripts drive the owner's interface in a real browser, against the real `wdva gate app`. `journey_demo.js` walks the demo: the played agent, the passphrase asked for again, lock and unlock, one approval. `journey_real.js` walks a new vault from pairing to Ctrl-C: passphrase, documents, apps, grants, an agent's calls, approvals, a restart, and the first run as offered.

    python -m pip install -e ".[gate]"
    npm install --no-save playwright@1.63.0 && npx playwright install chromium
    node tests/browser/journey_demo.js && node tests/browser/journey_real.js

Each step prints `ok` or `FAIL`; the exit status is 1 when a step failed, the page logged an error, a request failed, or an HTTP error came that no step announced. The head of each script lists its options (`GATE_PYTHON`, `GATE_BROWSER`, `GATE_SHOTS`, `GATE_SCALE`, `GATE_PORT`); screenshots go to `shots/` in this directory.
CI runs both on every push (job "Browser" in `.github/workflows/tests.yml`): on Linux in Chromium, and on macOS in WebKit, the engine of Safari.
