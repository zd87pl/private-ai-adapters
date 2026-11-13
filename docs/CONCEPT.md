# Concept

This document explains where WDVA started, why that idea was not enough, and the design it is moving toward. Everything below the "What exists today" section is a plan.

## Where it started

The original idea was to fine-tune a small adapter (LoRA/DoRA) on your documents and encrypt it. The encrypted adapter could then be stored anywhere and decrypted in memory only when you query it. Deleting the key was meant to delete the model.

Taking that idea seriously turned up four problems:

- **Fine-tuning is a poor way to store facts.** Retrieval (RAG) tends to beat fine-tuning at answering questions about specific documents. For example, [Ovadia et al. 2023](https://arxiv.org/abs/2312.05934) found RAG consistently ahead of unsupervised fine-tuning on knowledge-injection tasks. That paper fine-tuned whole models; adapters, which update far fewer weights, are not expected to do better. Adapters are good at style, format and vocabulary, not at remembering what page 14 of your lease says.
- **An adapter trained on private text is private text.** Models can memorize and regurgitate training data, so an adapter needs the same protection as the documents. Encrypting it protects it at rest only.
- **Most of the privacy risk is elsewhere.** The risk lies in the extracted text, search index, embeddings, chat transcripts, and above all whatever gets sent to a cloud model. Encrypting one adapter file leaves all of that untouched.
- **"Delete the key, delete the model" depends on the key really being gone.** That means every copy, including backups, password managers, and synced keychains. A library cannot promise that on its own.

## What exists today

- `wdva.crypto` encrypts adapter weights with XChaCha20-Poly1305, using a key derived per file with HKDF-SHA256. It authenticates the header, rejects tampering and wrong keys, and is tested, including against libsodium as an independent implementation.
- `wdva.inference` runs a small MLX base model locally.
- Training is not implemented. Decrypted adapters are not applied to the model.

## Where it is going: a local-first privacy firewall

The working name is *Sealgate*. It is a tool for one person on an Apple Silicon Mac who wants AI help with sensitive documents: medical, legal, financial, immigration, tax. They want that help without handing those documents to a cloud service by default.

### Vault

Documents and **everything derived from them** are stored encrypted:

- OCR text
- chunks
- the search index and embeddings (embeddings can be inverted, so they count as plaintext)
- pseudonym maps
- chat transcripts
- any future adapters

Each collection (for example "medical", or one legal matter) gets its own random key, wrapped by a key derived from a passphrase and by a macOS Keychain item. Destroying a collection's key wraps "crypto-shreds" it and produces a signed record of what this tool destroyed. Keys stay on this device: no iCloud Keychain sync, and the vault refuses to live in synced folders.

That still doesn't stop Time Machine or APFS snapshots from keeping an old passphrase-wrapped key. The vault will need to be excluded from backups, and the deletion record cannot prove that no copy survives elsewhere.

### Local answers by default

A local MLX model answers from the vault, with citations to the page. By default nothing leaves the Mac. The first thing to measure is how often local answers are good enough. If they aren't, people will paste documents into a chat app and every other protection is moot.

### One gated path to the cloud

When a task needs a stronger model, a single egress module is the only code allowed to reach the network or hold API keys. It sends the smallest request that does the job, in one of two forms:

- **Question rewriting:** the local model turns your question into a general one that doesn't need your documents. Research such as [PAPILLON](https://arxiv.org/abs/2410.17127) reports most of the quality of sending raw data, with far less leakage.
- **Pseudonymized excerpts:** a few short passages with names and identifiers replaced by consistent stand-ins, under a hard size limit.

You see the exact bytes before they leave. Real names are put back only on your machine. Each send is written to an encrypted, signed ledger that records which model received what, and what that provider says it does with the data.

### Routing tiers

| Tier | Where data may go | Default for |
|---|---|---|
| Local only | Nowhere | Medical, legal, financial, identity and personal collections |
| Verified secure hardware | Open-weight models in attestable enclaves (later) | — |
| Cloud, minimized | Claude's commercial API with your own key; no file uploads, tools or connectors | Explicitly approved tasks |
| Cloud, standard | Same API | Public material only |

The strictest label on anything retrieved for a request decides the tier. A local model may make a label stricter, never looser. Consumer chat apps and free tiers that train on your data are never used for vault content.

### Where adapters fit later

Adapters stay out of the first version. Two later uses are reasonable:

- A small detector adapter trained on your own corrections, to spot identifier formats specific to you.
- Style adapters trained only on pseudonymized text, with a record of what each was trained on.

Both would live in the vault and be encrypted with `wdva.crypto`.

## Principles

- **Claims ship with tests.** "Plaintext never reaches disk" is a CI test that plants canary strings and scans the vault, temp dirs, caches and logs. "Nothing unapproved leaves" is a test against a recording mock server.
- **No backend.** There is no telemetry, account, relay or sync. You bring your own API key, so this project is never a processor of your data.
- **Say what it can't do.** Pseudonymized is not anonymous. A gateway is not a network firewall. See [THREAT_MODEL.md](THREAT_MODEL.md).
- **Few dependencies, to be pinned and license-checked.** LangChain and the PyTorch inference backend were removed to keep the list small. Pinning by hash and a license check are not done yet.
