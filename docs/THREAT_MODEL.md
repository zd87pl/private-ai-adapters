# Threat model

This is a skeleton and will grow with the code. Each claim is labelled **Today** (implemented and tested in this repo) or **Planned** (design intent, not built). If a claim has no label, treat it as Planned.

## Who and what this is for

- **User:** one person on their own Apple Silicon Mac, managing sensitive personal or professional documents.
- **Assets:**
  - the documents
  - everything derived from them (extracted text, embeddings, pseudonym maps, transcripts, adapters)
  - the keys
  - the record of what was sent to whom
- **Goal:** keep these on the user's machine by default. When a cloud model is used, send as little as possible, with the user's approval and a record.

## What it protects against

| Threat | Protection | State |
|---|---|---|
| Someone reads or copies an encrypted adapter file (stolen laptop backup, shared drive, cloud storage) | XChaCha20-Poly1305 with a 256-bit key; per-file HKDF-SHA256 subkey with a random salt; 192-bit random nonce. Without the key, tensor names and values can't be read. | **Today** |
| Someone edits the ciphertext, tag, salt, nonce or header metadata of an encrypted adapter file, or swaps ciphertexts between files | The Poly1305 tag covers the ciphertext and the header metadata, and the salt and nonce feed the key and cipher, so changing any of them fails decryption. Unsupported algorithm or KDF headers are rejected. **Not detected:** replacing a whole file with another file encrypted under the same key (for example an older version), and extra JSON fields, which are ignored. | **Today** |
| Decrypted adapter weights written to disk by WDVA | Decryption happens in memory. A test checks that decrypting and querying create no files in the temp or working directory. WDVA cannot stop the OS from swapping memory to disk. | **Today** |
| Plaintext documents or derivatives at rest | Everything in an encrypted vault; per-collection keys; canary-residue CI test that scans vault, temp, caches and logs | Planned |
| Being unable to delete a collection | Per-collection random keys; destroying the wraps crypto-shreds the collection; a signed record of what was destroyed (not proof that no copy survives); keys kept device-only | Planned |
| Sending raw documents to a cloud model by accident | Single egress module; default local-only; approval screen with exact bytes; deterministic routing tiers | Planned |
| Not knowing what a provider has seen | Encrypted, signed ledger of every outbound request, with the provider terms that applied | Planned |
| Prompt injection in an untrusted document causing data to leave | Untrusted documents read by a quarantined local model with no tools; no cloud call without approval | Planned |

## What it does NOT protect against

These limits are permanent unless a later section says otherwise.

- **A compromised Mac.** Malware, a malicious app with Full Disk Access, or anyone with root or physical access while the vault is unlocked can read whatever the user can. Python also cannot reliably wipe keys or plaintext from memory.
- **Keys the user copies.** Today the caller holds the key (`EncryptedAdapter.generate_key()`, `WDVA.export_key()`). Any copy in a password manager, a synced keychain, a backup, shell history or a screenshot can decrypt the file. Deleting a key only "crypto-shreds" data when **every** copy is gone. `WDVA.delete()` only drops one Python reference.
- **Backups and snapshots.** Time Machine, APFS snapshots, iCloud Drive and synced keychains can keep old keys or plaintext after deletion. The planned vault keeps keys device-only and refuses synced locations, but cannot reach copies made outside it.
- **Metadata in the adapter file.** The `.wdva` header (format version, tensor count, plaintext size, compression flag, and any metadata you pass, such as a base model name) is readable by anyone with the file. It is authenticated, not encrypted. The file size also reveals the approximate adapter size.
- **What a cloud provider keeps.** Once text is sent, the provider's terms govern it, not this tool. According to Anthropic's documentation as checked on 2026-11-01:
  - **Commercial API:** not used for training unless you give express permission. For most models, inputs and outputs are deleted within 30 days by default. Covered Models (currently Claude Fable 5/5.1 and Mythos 5/5.1) are kept for *at least* 30 days and can't be used under zero data retention. Content flagged by safety classifiers can be kept for up to 2 years, and legal holds can extend retention. ([API data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention), [organization data](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data), [Covered Models](https://support.claude.com/en/articles/15425695))
  - **Zero data retention:** a contractual arrangement that Anthropic's sales team enables per organization, not a setting you can turn on yourself. It covers only some endpoints and features (not, for example, the Files API, Batch API or code execution), and no Covered Models. Even under it, flagged content can be kept up to 2 years and legal holds still apply. ([API data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention))
  - **Consumer apps (Free, Pro, Max):** since the August 2025 terms update, whether chats are used for training is a user setting. With training on, data can be kept in training pipelines for up to 5 years. With it off, chats stay in your history until you delete them, and are removed from back-end storage within 30 days after that. Either way, flagged content can be kept up to 2 years, and feedback you submit for 5 years. ([consumer terms update](https://www.anthropic.com/news/updates-to-our-consumer-terms), [consumer retention](https://privacy.claude.com/en/articles/10023548-how-long-do-you-store-my-data))
  - **Other providers:** terms differ, and all of them change. Check the source before relying on any of this.
- **Courts and governments.** Deletion promises can be overridden by legal process, at the provider or on the user's own machine.
- **Re-identification.** Pseudonymized or rewritten text is not anonymous. Language models can infer personal attributes (location, age, income, health) from text with names removed, and can link pseudonymized writing back to people. Minimization reduces exposure; it does not remove it.
- **Detector misses.** Planned PII detection will miss things, especially in clinical text, non-Latin scripts, and identifier formats it has not seen. The approval screen is the backstop, and only if the user reads it.
- **Approval fatigue.** If the user approves every request without reading, the gate protects nothing.
- **Network metadata.** The provider still sees the user's account, IP address, timing and request sizes. This is not an anonymity tool.
- **Model and package supply chain.** Models come from Hugging Face. The first load downloads them, and every later load contacts the Hub to check for a newer revision, revealing which model is used, when, and from which IP address. Setting `HF_HUB_OFFLINE=1` or passing a local path avoids this. Dependencies come from PyPI. Neither models nor dependencies are pinned by hash yet.
- **Side channels and hardware attacks** on the Mac, or on any "confidential computing" service. Attestable secure-hardware inference exists for some open-weight models, and published physical attacks have broken TEE protections. As of 2026-11-01 we found no Claude model offered with client-verifiable attestation.

## Trust boundaries (planned)

```
documents ──▶ [ ingest ] ──▶ [ encrypted vault ] ◀──▶ [ local MLX model ]
                                     │
                                     ▼
                           [ minimizer + approval ]
                                     │   (only path to the network)
                                     ▼
                              [ egress module ] ──▶ cloud API (your key)
                                     │
                                     ▼
                              [ encrypted ledger ]
```

Only the egress module may import an HTTP client or hold API keys. CI will check this.

## Reporting a problem

Open an issue at https://github.com/zd87pl/private-ai-adapters/issues. For anything that could hurt users before a fix exists, don't post details publicly. Private vulnerability reporting is not enabled on the repository yet, so open an issue that only says you have a security report and asks for a private channel.
