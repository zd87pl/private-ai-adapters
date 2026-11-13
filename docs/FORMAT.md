# WDVA vault format, version 1

This is the normative description of a `.wdva` file. It is complete enough to
write an independent reader: `tests/test_vault.py::reference_unseal` is one,
written only from this document and checked against the implementation and
fixed test vectors on every CI run.

A vault is a single authenticated ciphertext with a small self-describing
header. It can hold any bytes; WDVA uses it for adapter bundles
([see below](#adapter-payload)).

## Container layout

All integers are unsigned and big-endian.

| Offset  | Size | Field          | Value                                     |
| ------- | ---- | -------------- | ----------------------------------------- |
| 0       | 4    | magic          | `57 44 56 41` (ASCII `WDVA`)              |
| 4       | 1    | format version | `01`                                      |
| 5       | 4    | header length  | `H`, with `1 <= H <= 65536`               |
| 9       | `H`  | header         | JSON object, see below                    |
| 9 + `H` | `N`  | ciphertext     | same length as the plaintext payload      |
| 9+`H`+`N` | 16 | tag            | Poly1305 authentication tag               |

The total overhead is `9 + H + 16` bytes; `H` is about 240 bytes for a
key-locked vault with no public metadata.

A reader MUST reject a file whose magic, version or header length is not as
above, or that is shorter than `9 + H + 16` bytes.

## Header

The header is a JSON object. Writers MUST emit it in canonical form: keys sorted,
no insignificant whitespace, ASCII only (non-ASCII characters escaped as
`\uXXXX`). Readers authenticate the header **bytes as stored**, so canonical
form matters for reproducibility, not for security.

| Key      | Required | Type   | Meaning                                                        |
| -------- | -------- | ------ | -------------------------------------------------------------- |
| `alg`    | yes      | string | AEAD. Always `"XChaCha20-Poly1305"`.                           |
| `kdf`    | yes      | string | Subkey derivation. Always `"HKDF-SHA256"`.                     |
| `salt`   | yes      | string | Base64 of 32 random bytes: the HKDF salt.                      |
| `nonce`  | yes      | string | Base64 of 24 random bytes: the XChaCha20 nonce.                |
| `commit` | yes      | string | Base64 of the 32-byte key commitment.                          |
| `type`   | yes      | string | Media type of the payload, e.g. `application/octet-stream`.    |
| `pw`     | no       | object | Present if and only if the vault is locked with a passphrase.  |
| `meta`   | no       | object | Caller-supplied public metadata. Omitted when empty.           |

Base64 is the standard alphabet with padding (RFC 4648 section 4). `type` is two
RFC 6838 tokens joined by a slash: each starts with a letter or digit and
continues with letters, digits or any of `!#$&^_.+-`; the whole value is at most
127 characters. (So it is always printable ASCII, and safe to display from an
unverified file.)

A reader MUST reject a header:

* that is not UTF-8, or is not a JSON object;
* in which **any object contains the same key twice**, at any depth. Two readers
  that resolved a duplicate differently could otherwise derive different keys
  and nonces from the same authentic file;
* that contains `NaN`, `Infinity` or `-Infinity`, a floating-point number too
  large to be a finite double, or an integer outside the range `-(2^53 - 1)` to
  `2^53 - 1`. The first are not JSON. The last cannot be held exactly by a
  reader whose numbers are doubles, so two readers could disagree about its
  value. Writers MUST NOT produce such a header;
* with unknown keys or missing required keys. Any new field therefore requires a
  new format version;
* with a value of the wrong type. `null` is not a substitute for an absent
  optional key, and where an integer is required a boolean or a float is wrong
  even if it compares equal (`true`, `8.0`);
* whose binary fields do not decode to exactly the stated length;
* in which objects and arrays nest more than **64 levels deep**, counting the
  header object itself as level 1. Writers MUST NOT produce such a header. The
  limit is explicit because JSON libraries differ in how deep they can go
  before failing, and a reader must fail cleanly, not crash, on a hostile file.

`pw`, when present, has exactly these keys:

| Key    | Type    | Meaning                                                            |
| ------ | ------- | ------------------------------------------------------------------ |
| `kdf`  | string  | Always `"scrypt"`.                                                 |
| `n`    | integer | CPU/memory cost: a power of two with `2^14 <= n <= 2^20`.          |
| `r`    | integer | Always `8`.                                                        |
| `p`    | integer | Always `1`.                                                        |
| `salt` | string  | Base64 of 16 random bytes.                                         |

The bounds on `n` are enforced by readers **before** running scrypt. The header
is attacker-controlled until authenticated, and an unbounded cost parameter
would let a hostile file exhaust the reader's memory. scrypt needs about
`128 * r * n` bytes: 128 MiB at the default `n = 2^17`, and 1 GiB at the maximum
`n = 2^20`, which a reader must therefore be prepared to allocate.

**Everything in the header is public.** It is authenticated, so it cannot be
changed undetected, but it is not encrypted. `meta` in particular is readable by
anyone who can read the file.

## Key schedule

```
master   = key                                    (32 bytes, key-locked vault)
         | scrypt(passphrase', pw.salt, N=pw.n, r=8, p=1, dkLen=32)

enc_key  = HKDF-SHA256(IKM=master, salt=header.salt, info="wdva/v1/encryption-key", L=32)
commit   = HKDF-SHA256(IKM=master, salt=header.salt, info="wdva/v1/key-commitment", L=32)
```

* `passphrase'` is the passphrase normalised to Unicode NFKC and encoded as
  UTF-8. An empty passphrase is invalid.
* HKDF is RFC 5869 (extract-then-expand). The `info` strings are ASCII with no
  terminator.
* Each vault has a fresh random `salt`, so each vault has its own `enc_key` even
  when many vaults share one `master`.

### Why a key commitment

XChaCha20-Poly1305, like most AEADs, is not *key-committing*: a ciphertext can
be crafted that authenticates under more than one key. Storing a value derived
from the key by a collision-resistant function closes that gap, and it has a
practical benefit: a reader can distinguish *"this is the wrong key"* from
*"this file is damaged"*.

`commit` reveals nothing useful about `master` or `enc_key`: it is a PRF output
under a secret key. For passphrase-locked vaults it gives an offline attacker a
way to test guesses, but so does the AEAD tag; in both cases every guess costs
one scrypt evaluation.

## Encryption

```
AAD        = bytes 0 .. 9+H-1 of the file   (magic, version, header length, header)
ciphertext, tag = XChaCha20-Poly1305-Encrypt(key=enc_key, nonce=header.nonce, aad=AAD, payload)
```

XChaCha20-Poly1305 is as specified in `draft-irtf-cfrg-xchacha-03`: the 24-byte
nonce variant of RFC 8439.

## Reading a vault

1. Check the magic, version and header length; reject if the file is too short.
2. Parse and validate the header as described above.
3. Obtain `master` from the key, or by running scrypt with the header's `pw`
   parameters. A key offered for a passphrase-locked vault (or the reverse) is
   an error.
4. Derive `enc_key` and `commit`. Compare `commit` with the header's value **in
   constant time**. On mismatch, stop: the credential is wrong (or the header's
   `salt`/`commit` were altered).
5. Decrypt and verify with the AAD above. On failure, stop: the file is
   corrupted or was tampered with. **No plaintext may be released.**

## Adapter payload

When `type` is `application/vnd.wdva.adapter.v1`, the plaintext payload is:

| Offset  | Size | Field           | Value                                        |
| ------- | ---- | --------------- | -------------------------------------------- |
| 0       | 4    | manifest length | `M`, big-endian, `M <= 16 MiB`               |
| 4       | `M`  | manifest        | UTF-8 JSON object                            |
| 4 + `M` | rest | weights         | a complete `.safetensors` file               |

The manifest has these keys:

| Key              | Type   | Meaning                                                     |
| ---------------- | ------ | ----------------------------------------------------------- |
| `format`         | string | Always `"application/vnd.wdva.adapter.v1"`.                 |
| `adapter_config` | object | The contents of PEFT's `adapter_config.json`.               |
| `info`           | object | Free-form private notes: base model, training statistics.   |

The manifest is parsed under the same rules as the header: no duplicate keys, no
`NaN` or `Infinity`, no number outside the ranges given there, at most 64 levels
of nesting.

The weights use the same tensor names PEFT writes to
`adapter_model.safetensors`, so a bundle converts losslessly to and from a
standard PEFT adapter directory. They must be a well-formed safetensors file:
known dtypes, integer shapes whose byte size matches the stated offsets, and
tensor data that covers the buffer exactly, with no overlaps, gaps or trailing
bytes.

Because the manifest is inside the ciphertext, the adapter's base model, rank,
tensor names, document names and training statistics are **not** visible to
someone who only holds the file. What *is* visible: that the file is a WDVA
adapter vault, how it is unlocked, and its size, from which the adapter's
parameter count can be estimated.

## Test vectors

Both vectors are checked byte-for-byte in `tests/test_vault.py`
(`KAT_KEYED`, `KAT_PASSPHRASE`).

**Key-locked**

```
key      000102…1f            (bytes 0x00 through 0x1f)
salt     202122…3f            (bytes 0x20 through 0x3f)
nonce    404142…57            (bytes 0x40 through 0x57)
type     application/octet-stream
meta     {"note":"kat"}
payload  "weight-delta vault adapters"   (27 bytes, ASCII)

vault    309 bytes
tag      41fe7f33519b4eb6b7b997609600a8a0
sha256   aa7d8c04f989ca3125afe0718f0d494f6e0fd61eac9fe8baa77c4cb6af921e2e   (of the whole vault)
```

**Passphrase-locked**

```
passphrase   "correct horse battery staple"
pw.salt      000102…0f        (bytes 0x00 through 0x0f)
pw.n         16384
salt         000102…1f
nonce        000102…17
type         application/octet-stream
payload      "passphrase vault"          (16 bytes, ASCII)

vault        354 bytes
tag          34be7e009cc9cde2345d423e813b3a2f
sha256       1a84ecd45620db6e4873001169ee738a7cffaa6a97f8a9931b61f5e35d9690ab   (of the whole vault)
```

## Limits and non-goals

* **Whole-file.** A vault is encrypted and decrypted in one piece, in memory.
  That suits adapters (megabytes), not multi-gigabyte checkpoints. There is no
  streaming or random access.
* **One recipient.** A vault has one key or one passphrase. There is no
  multi-recipient or public-key mode.
* **No compression.** Trained floating-point weights barely compress, and
  leaving it out removes a class of decompression-bomb and length-leak problems.
* **Size is not hidden.** Ciphertext length equals plaintext length.
* **No rollback protection.** An attacker who can write to your storage can swap
  a vault for an older vault sealed under the same key; both authenticate. If
  that matters, put a version in `meta` and check it after unsealing.

## Versioning

The version byte changes whenever a conforming version-1 reader would
misinterpret a file. Readers reject versions they do not know. JSON-based
`.wdva` files from pre-0.1 prototypes of this project have no magic bytes and
are not supported; no working release ever produced one.
