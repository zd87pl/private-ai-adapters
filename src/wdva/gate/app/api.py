# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""What the interface server asks of the app: one method per endpoint.

The HTTP server in :mod:`wdva.gate.app.httpd` knows about requests, sessions and
static files and nothing about stores. Everything else it hands to an object
with the methods of :class:`Service`. Each method takes the decoded fields of
one request as keyword arguments, returns a JSON object as a plain ``dict``,
and raises :class:`ServiceError` to answer with an error instead.

What the server guarantees before it calls a method:

* the request passed its own checks (address, session, same-site, size);
* every keyword argument the method declares is passed; a field that is
  optional and absent (or ``null``) arrives as ``None``;
* a value has the basic type its annotation says (text, a number, true or
  false, a list of texts, an object). Nothing more: lengths, ranges, names and
  the contents of objects are for the method to check.

Passphrases arrive as arguments like any other field. A method must never put
one into a :class:`ServiceError` message or a returned value.

One method belongs to no endpoint of its own: :meth:`Service.language`. The
server asks it for the answer it gives a page before pairing, so the request
behind it has shown no session. It returns one word or ``None`` instead of a
JSON object, and it must not fail.
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["JSONObject", "Service", "ServiceError"]

JSONObject = dict[str, Any]
"""A JSON object as Python holds it: text keys, values that ``json`` can write."""


class ServiceError(Exception):
    """An answer that is an error: an HTTP status, a short code and one sentence for a person.

    The server sends ``{"error": {"code": code, "message": message}}`` with the
    given status. The message is shown to the owner as it is, so it must not
    contain a passphrase or text from a document.

    ``reason`` is one more word for the page, where one code stands for several
    things the page says differently: why an uploaded file was not taken. The
    server sends it as a third member, ``"reason"``, and only when it is a word
    of lower-case letters, digits and underscores.
    """

    def __init__(self, status: int, code: str, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.reason = reason


class Service(Protocol):
    """The app behind the interface. Every method but one may raise :class:`ServiceError`."""

    # -- state ------------------------------------------------------------------

    def state(self) -> JSONObject:
        """What the interface shows at a glance. Answers also when locked or without a store."""
        ...

    def create_store(self, *, passphrase: str, language: str | None = None) -> JSONObject:
        """Create the store and return the state; keep ``language`` for the page if one is given."""
        ...

    def unlock(self, *, passphrase: str, hours: float | None = None) -> JSONObject:
        """Unlock the store, for ``hours`` or the app's default, and return the state."""
        ...

    def lock(self) -> JSONObject:
        """Lock the store and return the state."""
        ...

    def reauth(self, *, passphrase: str, scope: str | None = None) -> JSONObject:
        """Check the passphrase again; what needs it is then allowed for a while.

        Without ``scope``: actions that widen access, a write into another
        program's settings file, and trial questions. With ``scope="trial"``:
        trial questions only.
        """
        ...

    def set_serving(
        self, *, kinds: str | None = None, paused: list[str] | None = None
    ) -> JSONObject:
        """Choose which kinds of apps are served and which apps are paused; return the state."""
        ...

    def verify(self) -> JSONObject:
        """Verify the record chain and say what was found."""
        ...

    def set_interface(
        self, *, view: str | None = None, setup: str | None = None, language: str | None = None
    ) -> JSONObject:
        """Keep the view the owner uses, that setup is done, or the page's language; the state."""
        ...

    def language(self) -> str | None:
        """The language kept for the owner's page, or ``None`` when none is kept.

        Asked for a request that showed no session, and answered to it: the
        word must be nothing that is secret, and the method must not raise.
        """
        ...

    # -- activity ---------------------------------------------------------------

    def activity(
        self,
        *,
        limit: int | None = None,
        before: int | None = None,
        app: str | None = None,
        kind: str | None = None,
    ) -> JSONObject:
        """A page of the record, newest first."""
        ...

    def activity_detail(self, *, seq: int) -> JSONObject:
        """One record in full, with the exact text of a release."""
        ...

    # -- approvals --------------------------------------------------------------

    def approvals(self) -> JSONObject:
        """The passages waiting for the owner and the approvals not yet used."""
        ...

    def release(self, *, app: str, ref: str, body_sha256: str) -> JSONObject:
        """Approve one passage for one app, once, if its text is still the one shown."""
        ...

    def dismiss(self, *, app: str, ref: str) -> JSONObject:
        """Take one waiting passage off the list without approving it."""
        ...

    # -- the trial --------------------------------------------------------------

    def try_question(self, *, question: str) -> JSONObject:
        """The one passage an app would be given for the owner's question; records nothing."""
        ...

    # -- documents --------------------------------------------------------------

    def documents(self) -> JSONObject:
        """The collections with their documents, and what can be uploaded."""
        ...

    def add_document(self, *, collection: str, name: str, data: bytes) -> JSONObject:
        """Add one uploaded file, given as its name and its bytes, to a collection."""
        ...

    def remove_document(self, *, id: str) -> JSONObject:
        """Remove one document."""
        ...

    def set_collection(
        self,
        *,
        name: str,
        note: str | None = None,
        allow_hosted: bool | None = None,
        show_titles: bool | None = None,
    ) -> JSONObject:
        """Create a collection or change the fields that are given."""
        ...

    def remove_collection(self, *, name: str) -> JSONObject:
        """Remove an empty collection."""
        ...

    # -- apps -------------------------------------------------------------------

    def apps(self) -> JSONObject:
        """The registered apps with their grants, limits and use, and the tiles to connect one."""
        ...

    def add_app(self, *, name: str, hosted: bool, destination: str | None = None) -> JSONObject:
        """Register an app under a name."""
        ...

    def remove_app(self, *, name: str) -> JSONObject:
        """Remove an app and its grants."""
        ...

    def set_grant(self, *, app: str, collection: str, mode: str) -> JSONObject:
        """Set what one app may do with one collection."""
        ...

    def set_app_limits(self, *, name: str, limits: JSONObject) -> JSONObject:
        """Change the limits of one app; ``limits`` is the request's whole JSON object."""
        ...

    def connect(self, *, name: str, host: str | None = None) -> JSONObject:
        """The configuration that connects one agent application under this app's name."""
        ...

    # -- the settings file of an agent application ---------------------------------

    def connect_look(self, *, host: str) -> JSONObject:
        """What one application's settings file holds of this vault's entry, in fixed words.

        Reads that file and changes nothing, in the vault or outside.
        """
        ...

    def connect_write(self, *, host: str, expect: str) -> JSONObject:
        """Register one application and write the vault's entry into its settings file.

        The one entry, into the one file of that application, and only while
        the file is in the state ``expect`` that the owner was shown. It
        always needs owner mode: it writes a file of another program.
        """
        ...

    def connect_remove(self, *, host: str, expect: str) -> JSONObject:
        """Stop answering one application and take the vault's entry out of its settings file.

        The file is changed only where ``expect`` says that the entry is this
        vault's; then, and where removing the app makes other apps answered,
        it needs owner mode. Otherwise it only takes away.
        """
        ...

    # -- settings ---------------------------------------------------------------

    def settings(self) -> JSONObject:
        """The limits and other settings of the vault."""
        ...

    def set_settings(
        self,
        *,
        defaults: JSONObject | None = None,
        groups: JSONObject | None = None,
        per_document: JSONObject | None = None,
        approval: JSONObject | None = None,
    ) -> JSONObject:
        """Change the settings that are given and return all of them."""
        ...

    def add_replaced_string(self, *, text: str) -> JSONObject:
        """Add one word or number that must never leave; returns only the new count."""
        ...

    # -- the demo ---------------------------------------------------------------

    def demo_again(self) -> JSONObject:
        """In a demo: have the played app ask once more for the passage it waits for."""
        ...
