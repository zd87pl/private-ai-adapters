"""Start the real app with two things changed: how long owner mode lasts, and its home.

    python launch_app.py OWNER_MODE_SECONDS gate app --no-browser --store DIR ...

Everything after the number is the command line of ``wdva``. The app that runs
is the shipped one. The constant ``OWNER_MODE`` of the service is replaced, so
that a walk-through in a browser does not have to wait ten minutes to see the
passphrase asked for again. And ``HOME`` and ``XDG_CONFIG_HOME`` name a folder
the run owns, so that the one look the app takes outside a vault ("Check the
file", at the settings file of an AI app) never meets the settings of a
program on this computer. That folder is the one ``GATE_HOME`` names; without
it a temporary folder is made and removed again when the app ends.

Run by the walk-throughs in this directory (``journey_demo.js`` and
``journey_real.js``). It is not a test module and pytest does not collect it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import timedelta

from wdva.cli import main as run_wdva
from wdva.gate.app import service


def main(arguments: list[str]) -> int:
    service.OWNER_MODE = timedelta(seconds=float(arguments[0]))
    given = os.environ.get("GATE_HOME", "")
    home = given or tempfile.mkdtemp(prefix="gate-home-")
    os.environ["HOME"] = home
    os.environ["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
    try:
        return run_wdva(arguments[1:])
    finally:
        if not given:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
