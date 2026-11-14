// SPDX-License-Identifier: Apache-2.0
// Copyright 2025 Enclave
//
// The texts of the page in English: one flat table, a key for each text. A value is
// one text, or the plural forms of one (`one` and `other`). A place for a
// value is its name in braces, and the product's name is the place {product}. This
// file imports nothing, and only texts.js reads it.

export const TEXT = Object.freeze({
  // Every screen
  "common.label.language": "Language",
  "common.lang.en": "English",
  "common.lang.pl": "Polski",
  "common.chip.demo": "Demo",
  "common.button.cancel": "Cancel",
  "common.button.remove": "Remove",
  "common.status.justnow": "just now",
  "common.error.unknown": "Something went wrong.",
  "common.error.cancelled": "Nothing was changed.",
  "common.error.busy": "The vault is open in another program; lock it there first.",
  "common.error.unavailable": "The vault cannot answer right now.",
  "nav.result.locked": "The vault is locked.",
  "nav.tab.locked": "{product} – locked",

  // Pairing, and a page without a session
  "pair.title": "Pair this browser",
  "pair.say.1": "Type the pairing code from the terminal where {product} runs.",
  "pair.say.2": "Anyone who can read that terminal can pair.",
  "pair.label.code": "Pairing code",
  "pair.button.pair": "Pair",
  "pair.button.busy": "Pairing…",
  "pair.error.wrong": "That is not the code. Check the terminal and try again.",
  "pair.error.expired": "The session ended. Pair again.",
  "pair.error.disabled": "Pairing is off after five wrong codes. Quit {product} in its terminal and start it again.",
  "stuck.title": "This page has no session",
  "stuck.say.1": "Reload the page, and if that does not help, quit {product} in its terminal and start it again.",
  "stuck.button.reload": "Reload",

  // The passphrase of a new vault
  "create.title": "Choose a passphrase",
  "create.label.passphrase": "Passphrase",
  "create.label.again": "Once more",
  "create.button.show": "Show",
  "create.button.hide": "Hide",
  "create.label.show": "Show what is typed",
  "create.label.hide": "Hide what is typed",
  "create.status.short": "At least 12 characters; {n} so far",
  "create.status.long": {
    one: "{n} character: long enough",
    other: "{n} characters: long enough",
  },
  "create.error.mismatch": "The two passphrases do not match.",
  "create.say.1": "It locks your documents on this computer.",
  "create.say.2": "Nobody can reset it: a lost passphrase is a lost vault.",
  "create.button.create": "Create my vault",
  "create.button.busy": "Creating the vault…",
  "create.status.where": "It will be created at {path}",

  // The approval card
  "card.title": "{app} asks for a passage of “{title}”",
  "card.chip.hosted": "Would be sent to {destination}",
  "card.chip.local": "Stays here, if the app really is local",
  "card.status.claimed": "Says it is {claimed}, unverified",
  "card.status.asked": "Asked {ago}",
  "card.status.asked.times": {
    one: "Asked {n} time, first {ago}",
    other: "Asked {n} times, first {ago}",
  },
  "card.label.text": "The exact text that would be released",
  "card.status.meta": {
    one: "{n} character · passage {passage} of {passages}",
    other: "{n} characters · passage {passage} of {passages}",
  },
  "card.say.1": "Once released, this text is sent to {destination} and cannot be recalled.",
  "card.say.1.local": "Once released, this text is with the app, and stays on this computer only if the app really is local.",
  "card.say.2": "The name “{app}” is a label: {product} cannot check which program is asking.",
  "card.button.no": "Don’t release",
  "card.button.yes": "Release once",
  "card.label.wait": "Read the text first",
  "card.button.details": "Details",
  "card.status.document": "This document: {released} of {characters} characters already released; {after} after this.",
  "card.status.today": "This app, last 24 hours: {used} of {limit} characters.",
  "card.details.say.1.each": "It waits because this app asks you each time.",
  "card.details.say.1.allowance": "It waits because it goes past what the app may read of this document without asking.",
  "card.say.3.nofit": "The app’s limits would refuse this passage right now, even with your yes.",
  "card.result.yes": "Released once for {app}, until {time}. Tell it: “try again”.",
  "card.result.no": "Not released. If {app} asks again, the passage comes back here.",
  "card.result.changed": "The text changed since it was shown, so nothing was released. Read it again.",

  // The zone that takes files, and its lines
  "docs.say.1": "A sealed copy goes into the vault, and the file you add stays where it was, not encrypted.",
  "docs.button.add": "Add documents",
  "docs.label.drop": "Drop files here",
  "docs.label.dropnow": "Drop to add",
  "docs.status.types": "PDF or text, up to {size} each",
  "docs.status.sealing": "Sealing…",
  "docs.status.waiting": "Queued",
  "docs.result.sealed": {
    one: "Sealed: {n} passage",
    other: "Sealed: {n} passages",
  },
  "docs.result.who": "Apps that can ask for it from now on: {apps}.",
  "docs.result.nobody": "No app can ask for it yet.",
  "docs.error.type": "Not added: only PDF and text files can be read. Save a Word file as PDF and add that.",
  "docs.error.pdf": "Not added: this installation cannot read PDFs.",
  "docs.error.large": "Not added: larger than {size}.",
  "docs.error.empty": "Not added: the file is empty.",
  "docs.error.duplicate": "Not added: this document is already in the vault.",
  "docs.error.other": "Not added: {reason}",
  "docs.button.clear": "Clear this list",
  "docs.result.added": {
    one: "{n} document added.",
    other: "{n} documents added.",
  },
  "docs.result.mixed": "{added} added, {failed} not added.",
  "docs.result.noneadded": "No document was added.",

  // The sheet that asks before a document is removed
  "remove.title": "Remove “{title}”?",
  "remove.say.1": "Apps can no longer search or read it, and what was released earlier stays under Activity.",
  "remove.say.2": "Its files are deleted, not shredded, and backups keep what they have.",
  "remove.result.done": "Removed “{title}”.",

  // The details of an entry
  "panel.label.close": "Close the details",
  "panel.label.text": "The exact text that left",
  "panel.say.1": "This text is with {destination} now.",
  "panel.say.2": "{product} cannot take it back.",
  "panel.say.1.local": "This text is with the app now, and stays on this computer only if the app really is local.",
  "panel.label.query": "What it searched for",
  "panel.button.record": "Record details",
  "panel.label.entry": "Entry #{seq}",

  // The sheet that asks for the passphrase again, and the demo's note
  "reauth.title": "Enter your passphrase again",
  "reauth.say.1": "This needs your passphrase because you last typed it more than 10 minutes ago.",
  "reauth.label.passphrase": "Passphrase",
  "reauth.button.continue": "Continue",
  "reauth.button.busy": "Checking…",
  "reauth.error.wrong": "That passphrase is not right.",
  "demo.say.1": "This is the demo, and the passphrase of its vault is {passphrase}.",
  "demo.say.2": "The passphrase of a real vault is never shown.",
  "demo.button.fill": "Fill it in",

  // The locked vault
  "locked.title": "The vault is locked",
  "locked.label.demo": "Demo vault",
  "locked.label.passphrase": "Passphrase",
  "locked.button.unlock": "Unlock",
  "locked.button.busy": "Unlocking…",
  "locked.error.wrong": "That passphrase did not open the vault.",
  "locked.status.waited": {
    one: "{n} passage was waiting when the vault locked; it was not released.",
    other: "{n} passages were waiting when the vault locked; none was released.",
  },
  "locked.say.1": "While locked, apps get one answer: the vault is locked.",
  "locked.say.2": "The number and sizes of the sealed files are visible without the key, and what is in them is not.",
  "locked.label.files": "Sealed files",
  "locked.label.size": "Size",
  "locked.label.last": "Last entry",

  // The app does not answer
  "offline.title": "{product} is not answering",
  "offline.say.1": "Start {product} again in a terminal, then open the address it prints.",
  "offline.say.2": "When {product} stops, the vault is locked.",
  "offline.button.try": "Try now",

  // What the product cannot do
  "how.cannot.1": "Take text back. What an app received is with that app and, for an app that sends text to a provider, with that provider.",
  "how.cannot.2": "Tell who is calling. App names are labels: any program running as you can call under a name that is answered.",
  "how.cannot.3": "Notice instructions hidden in a document. Limits bound what can be lost; one released passage is enough to leak that passage.",
  "how.cannot.4": "Keep a whole document in. Search answers show which passages hold the words asked for, and passages add up, one by one.",
  "how.cannot.5": "Replace more than the words you listed. Other forms of the same name or number pass.",
  "how.cannot.6": "Protect your passphrase from other programs running as you, or bring back a lost one.",
  "how.cannot.7": "Prove anything with its record. It is a log of what was released; whoever can delete files can roll it back.",
  "how.cannot.8": "Shred. Removing a document deletes files, and backups keep what they have.",
  "how.version": "{product} {version} · a preview: it has not yet been tried against the AI apps it lists, and it does not run on Windows.",
});
