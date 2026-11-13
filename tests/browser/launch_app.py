"""Start the real app with one thing changed: how long owner mode lasts.

    python launch_app.py OWNER_MODE_SECONDS gate app --no-browser --store DIR ...

Everything after the number is the command line of ``wdva``. The app that runs
is the shipped one; only the constant ``OWNER_MODE`` of the service is replaced,
so that a walk-through in a browser does not have to wait ten minutes to see the
passphrase asked for again.

Run by the walk-throughs in this directory (``journey_demo.js`` and
``journey_real.js``). It is not a test module and pytest does not collect it.
"""

from __future__ import annotations

import sys
from datetime import timedelta

from wdva.cli import main as run_wdva
from wdva.gate.app import service


def main(arguments: list[str]) -> int:
    service.OWNER_MODE = timedelta(seconds=float(arguments[0]))
    return run_wdva(arguments[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
