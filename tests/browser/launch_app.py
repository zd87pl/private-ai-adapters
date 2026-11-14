"""Start the real app with two things changed: how long owner mode lasts, and its home.

    python launch_app.py OWNER_MODE_SECONDS gate app --no-browser --store DIR ...
    python launch_app.py --can-connect FOLDER

Everything after the number is the command line of ``wdva``. The app that runs
is the shipped one. The constant ``OWNER_MODE`` of the service is replaced, so
that a walk-through in a browser does not have to wait ten minutes to see the
passphrase asked for again. And ``HOME`` and ``XDG_CONFIG_HOME`` name a folder
the run owns, so that what the app does outside a vault (it looks at the
settings file of an AI app, and "Connect" writes one entry into it) never meets
the settings of a program on this computer. That folder is the one
``GATE_HOME`` names; without it a folder is made under ``.run/`` beside this
file and removed again when the app ends.

The home lies under the checkout and not under the system's folder for
temporary files, and so must the vault and the Python that runs the app: the
app writes no entry that names a Python or a vault in a place that may be gone
tomorrow (a temporary folder, a folder everybody may write, one of another
account). ``--can-connect FOLDER`` asks the app's own rule about this Python and
about a folder that exists and in which the run will make its vaults, and
prints one line, ``connect: ok`` or ``connect: REFUSED ...`` with the reason; a
walk-through that is to connect asks before it starts, so that a refusal is
said once and loudly and not found step by step.

``GROK_HOME`` and ``CODEX_HOME`` move a settings file or stop a write. Those of
the computer that runs the walk-through never reach the app. A walk-through that
wants the app to see ``CODEX_HOME`` names its value in ``GATE_CODEX_HOME``.

Run by the walk-throughs in this directory (``journey_demo.js``,
``journey_real.js`` and ``journey_home.js``). It is not a test module and
pytest does not collect it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import timedelta

from wdva.cli import main as run_wdva
from wdva.gate import hostwrite
from wdva.gate.app import service

HERE = os.path.dirname(os.path.abspath(__file__))


def can_connect(folder: str) -> int:
    """Say whether the app would write an entry naming this Python and a vault in ``folder``."""
    command = [sys.executable, "-m", "wdva", "gate", "mcp", "--store", folder]
    problem = hostwrite.write_problem("claude-desktop", command)
    if problem is None:
        print("connect: ok")
        return 0
    print(
        f"connect: REFUSED ({problem}): the app writes no entry that names the Python "
        f"{sys.executable} and a vault in {folder}, because one of the two lies in a folder "
        "for temporary files, in a folder everybody may write, or in one of another account. "
        "Use a Python and a checkout in a place of your own (a virtual environment under "
        "the checkout will do)."
    )
    return 3


def main(arguments: list[str]) -> int:
    if arguments[:1] == ["--can-connect"]:
        return can_connect(arguments[1])
    service.OWNER_MODE = timedelta(seconds=float(arguments[0]))
    given = os.environ.get("GATE_HOME", "")
    made = ""
    if not given:
        os.makedirs(os.path.join(HERE, ".run"), exist_ok=True)
        made = tempfile.mkdtemp(prefix="home-", dir=os.path.join(HERE, ".run"))
    home = given or made
    os.environ["HOME"] = home
    os.environ["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    os.environ.pop("GROK_HOME", None)
    os.environ.pop("CODEX_HOME", None)
    moved = os.environ.get("GATE_CODEX_HOME", "")
    if moved:
        os.environ["CODEX_HOME"] = moved
    try:
        return run_wdva(arguments[1:])
    finally:
        if made:
            shutil.rmtree(made, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
