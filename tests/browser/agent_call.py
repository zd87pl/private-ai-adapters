"""One call of an agent application, made the way the program it starts makes it.

    python agent_call.py STORE CLIENT status
    python agent_call.py STORE CLIENT search QUERY [COLLECTION]
    python agent_call.py STORE CLIENT read REF

Uses ``RemoteGate``, which is what ``wdva gate mcp --client CLIENT`` uses: it has
no key and reaches the app through the vault's local socket. Prints the reply
as one JSON object.

Run by ``journey_real.js`` and ``journey_basic.js`` in this directory. It is not
a test module and pytest does not collect it.
"""

from __future__ import annotations

import json
import sys

from wdva.gate.link import RemoteGate
from wdva.gate.types import Caller


def main(arguments: list[str]) -> int:
    store, client, operation, *rest = arguments
    gate = RemoteGate(store, client)
    caller = Caller(client, "journey-agent", "1.0", "2025-11-25", "stdio")
    if operation == "status":
        reply = gate.status(caller)
    elif operation == "search":
        reply = gate.search(caller, rest[0], rest[1] if len(rest) > 1 else "")
    elif operation == "read":
        reply = gate.read(caller, rest[0])
    else:
        sys.stderr.write(f"unknown operation: {operation}\n")
        return 2
    print(json.dumps({"code": reply.code, "text": reply.text, "released": reply.released}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
