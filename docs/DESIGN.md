# VibeHealth - design notes

How VibeHealth works and why. For installing and running it, see the [README](../README.md); for
reporting a security problem, see [SECURITY.md](../SECURITY.md).

VibeHealth is a private, self-hosted record of one person's lab results. Documents come from an optional
Paperless-ngx server or from uploaded files. Two local AI models (through Ollama) read the values out of each
document, the values are checked against each other, and a person approves them. Only approved values count as
data.

## Architecture

- **Backend**: FastAPI, SQLModel, SQLite (WAL mode), asyncio background loops (sync, reading queue), pypdfium2 and
  Pillow for page images, a local Ollama server for the models. One process: see "One worker".
- **Frontend**: React, TypeScript, Vite, Tailwind, TanStack Query; installable as a PWA; English and Greek.
  The built files are served by the backend.
- **Packaging**: one container, port 5001, non-root user, read-only file system, no capabilities
  (`Dockerfile`, `compose.yaml`; a Podman Quadlet example is in `deploy/`).
- **Data folder** (`/data`, `DATA_DIR`): `vibehealth.db*`, `uploads/` (originals of uploaded files), `cache/`
  (thumbnails, rebuildable), `backups/` (pre-migration copies), `.keys/` (master key), `.setup-code`.

A Paperless file is never copied: it stays in Paperless, and a document being read is downloaded into memory and
dropped afterwards. An **uploaded** file is the opposite: the original is kept in the data folder.

## Data model

Schema versions are tracked in `PRAGMA user_version` (see "Migrations").

- `documents`: `source` (`paperless` or `upload`), `paperless_id` (unique; null for an upload), title, kind,
  doc_date, ignored, timestamps. For uploads also `original_filename` (cleaned, display only), `stored_path`
  (relative to `uploads/`), `mime_type`, `size_bytes` and `sha256` (unique among uploads: the same file is stored once).
- `app_settings`: every setting saved in the app, as `<section>.<name>`. Values are JSON, except secrets
  (`enc:v1:<token>`).
- `extraction_runs`: one reading of a document: status (running, done, error, interrupted, cleared), timing, pages,
  page errors, the settings used, counts.
- `extracted_values`: document, run, test code (nullable), printed name, value, unit, reference range, flag, status
  (`verified`, `needs_review`, `approved`, `rejected`), reason, page, and what each reader read. A partial unique
  index allows **one approved value per test per document**.

## The reading pipeline

For one document (`reading.py`):

1. **Load** (`sources.load_pages`). Paperless: the original file, downloaded into memory (the archived PDF when the
   original is neither a PDF nor an image). Upload: nothing is parsed in the server process; a sandbox child draws
   each page from the file on disk (see "Uploads").
2. **Render** each page with pypdfium2 at the configured dpi (default 150). No page is drawn above 3500 px on its
   long side, whatever the dpi setting says. Photos are used as they are, capped at 3000 px (an uploaded photo is
   first made upright, RGB and at most 2200 px).
3. **Reader A** (default `qwen3.5:4b`): one Ollama `/api/chat` call per page with `think: false`, a JSON-schema
   `format`, `temperature` 0 and a fixed prompt. A cut-off answer, an empty answer or invalid JSON is a page error.
4. **Reader B** (default `glm-ocr`): its native "Text Recognition:" prompt; `glm_parser.py` turns the text into rows.
   When the text ends on a dotted leader line (the model stopped mid-table), the page is read again at the fallback
   dpis (100, then 200). If every attempt is cut short, the first text is kept and the page is noted.
5. **Paperless text** (optional third opinion): the document's OCR `content` in Paperless. An upload has none.
6. **Normalise** (`catalog.py`): printed names are mapped to test codes (Greek, Latin and Cyrillic look-alikes, accents,
   ordered patterns so that `CHOL/HDL` never becomes `HDL`, a rule that tells urine tests from blood tests by value,
   absolute white-cell counts). Garbled names fall back to a fuzzy match against the catalogue. Only the **first
   occurrence** of a test in a document counts, because later pages repeat history.
7. **Verify** (`verify.py`), the verification rule:
   - `verified`: A and B read the same value, or one reader's value is on a Paperless text line that itself maps to
     the same test. A text value only counts on a line with no other digits. When A and B differ and the text backs
     exactly one of them, that one is kept.
   - `needs_review` otherwise, with a short reason (readers differ, only one reader found it, unknown test name).
   - The H/L flag is computed from the printed range. Units are kept as printed (no conversion yet).
8. **Save**: the document's non-approved values are replaced. Approved values stay, and a test that already has an
   approved value gets no new row.

Ollama calls run one at a time. Before a reading starts the installed models are checked, so the person sees "Ollama
is not reachable" or "model X is not installed" rather than a generic failure. A model that rejects `think` is retried
once without it.

**Background work** (`worker.py`). A queue reads one document at a time; progress (document, stage, page x/y) is shown
in the app. The queue lives in memory: a restart drops waiting documents and marks the running one `interrupted`. A
sync loop asks Paperless for documents (by document type and/or tags) at start, on an interval and on demand; it
touches only `source = 'paperless'` rows. "Read new documents after sync" is off by default.

## Settings and configuration

A setting resolves as **saved in the app > environment variable > built-in default**, and every setting reports its
source. Sections: `paperless`, `general`, `uploads`, `auth`, `setup`, `reading`. A saved value that no longer
validates is skipped, never a crash. The log names each setting's source at start, never a value. Everything is
optional; `.env.example` lists all variables.

| Setting | Default | Environment variable |
|---|---|---|
| Paperless address / public address / token | empty (not connected) | `PAPERLESS_URL` / `PAPERLESS_PUBLIC_URL` / `PAPERLESS_TOKEN` |
| Paperless document type / tags | `Medical` / four medical tags | `PAPERLESS_DOCUMENT_TYPE` / `PAPERLESS_TAGS` |
| Sync interval | 240 min (0: manual only) | `SYNC_INTERVAL_MINUTES` |
| Reading enabled | on | `READING_ENABLED` |
| Ollama address | `http://host.docker.internal:11434` | `OLLAMA_URL` |
| Reader A model | `qwen3.5:4b` | `READER_A_MODEL` |
| Reader B on / model | on / `glm-ocr` | `READER_B_ENABLED` / `READER_B_MODEL` |
| Page dpi / fallback dpis | 150 / 100, 200 | `READING_DPI` / `READING_FALLBACK_DPIS` |
| `num_ctx` / timeout per page / `keep_alive` | 8192 / 300 s / `5m` | `OLLAMA_NUM_CTX` / `OLLAMA_TIMEOUT_SECONDS` / `OLLAMA_KEEP_ALIVE` |
| Use Paperless OCR text | on | `USE_PAPERLESS_TEXT` |
| Read new documents after sync | off | `AUTO_READ_AFTER_SYNC` |
| Uploads: enabled / max file / max total | on / 50 MB / 10 GB | app setting only |
| Interface language | `en` | `DEFAULT_LANGUAGE` |
| Session length | 30 days | `SESSION_DAYS` |
| Root secret | random key in `.keys/master.key` | `SECRET_KEY` |
| Password hash | set in the setup guide | `APP_PASSWORD_HASH` |
| Extra allowed hosts / trusted origins | none | `VIBEHEALTH_ALLOWED_HOSTS` / `TRUSTED_ORIGINS` |
| Trust a reverse proxy | off | `VIBEHEALTH_TRUST_PROXY` |
| Sandbox memory limit | 2048 MB | `VIBEHEALTH_SANDBOX_MEMORY_MB` |
| Serve `/docs` (development) | off | `VIBEHEALTH_DEV_DOCS` |

Values are validated on the server. Paperless addresses must be http or https with a host, and no user info, query or
fragment. If the effective Paperless address changes, the token must be sent in the same request (or be cleared): a
token is never sent to an address it was not saved for.

## Security model

VibeHealth is meant for a trusted network (loopback, a LAN, a VPN) and defends against other people and other websites
reaching that network. It is not hardened for the public internet and does not encrypt the data folder at rest.

- **Password.** scrypt (N=2^15, r=8, p=3; at most 2 checks at a time), at least 10 characters for a new one (hashes made
  earlier still verify). A new installation is unusable until a password is set (see "Setup wizard"), and the first
  password needs a **setup code**: 8 unambiguous characters made at each start while there is no password, printed in
  the log and saved in `.setup-code` (mode 0600, removed once a password is set). Anything saved under `auth` that
  cannot be read (the hash, the session epoch, the revoked-session list) locks the app instead of opening it or reviving
  ended sessions. `python -m app.security reset-password` recovers a locked-out install (it prints where the new code
  file is, never the code) and leaves the install gated until a new password is set. `VIBEHEALTH_LEGACY_OPEN=1` keeps an
  installation without a password open; not recommended (see "Setup wizard").
- **Sessions.** Signed cookie: `HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS or behind a trusted proxy. Changing
  the password ends every session; logout revokes that token; "log out everywhere" bumps an epoch.
- **Login throttle** (`throttle.py`, in memory). Every guess (login, password confirmation, setup code) counts as a
  failure before it is checked and is taken back on success. Per client (IPv4 address, IPv6 /64, or the last valid
  `X-Forwarded-For` hop behind a trusted proxy): a lock from the 5th failure, 30 s doubling up to 15 min (429 with
  `Retry-After`). Across all clients a back-off (never a lock) slows guessing from many addresses.
- **Other-site requests.** A non-GET request carrying an `Origin` must name this host or a trusted origin;
  `Sec-Fetch-Site: cross-site` is refused.
- **Host allowlist** (DNS-rebinding defence). Only these `Host` values are answered: an IP address, `localhost`, a
  single-label name, a name ending in `.local`, `.lan`, `.home.arpa`, `.internal` or `.ts.net`, and anything listed in
  `VIBEHEALTH_ALLOWED_HOSTS`, the `general.allowed_hosts` setting or a trusted origin. Anything else is 400.
- **Secrets at rest.** The Paperless token is stored with Fernet (`enc:v1:`), keyed by HKDF from `SECRET_KEY` or the
  master key file. Secrets are write-only through the API (a GET shows only "set" and the last four characters of long
  secrets). Log records mask registered secrets. Losing the key makes saved secrets unreadable and ends sessions.
- **Settings that decide where the server connects or whom it trusts** (Paperless and Ollama addresses, trusted
  origins, allowed hosts) cannot be changed through the API while no password is set (403); with a password they
  need `current_password` in the request. The same goes for testing an address other than the saved one (Paperless
  test, Ollama test-connection). The connection-test helpers answer only with fixed, classified messages, never the
  other end's text, an address or the token; the same holds for Ollama errors shown in the app or stored with a
  reading (unreachable, timeout, model missing, bad request, server error, bad answer: the other end's own error text is
  neither shown nor kept). A Paperless preview or thumbnail is served only as PDF, JPEG, PNG or WebP, `nosniff` and
  `private, no-store`.
- **HTTP hardening.** `nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, a strict CSP,
  `Cache-Control: private, no-store` on the API. Request bodies are capped at 1 MiB (uploads use their own cap).
  `/docs`, `/redoc` and `/openapi.json` are off unless `VIBEHEALTH_DEV_DOCS=1`.
- **Upload sandbox** (below), a private data folder (umask 077, folders 0700, files 0600 on POSIX) and a container
  that runs as a non-root user without capabilities.
- **One worker.** The queue, the throttle, the settings cache and the sandbox gate live in memory, so the app refuses
  to start with more than one worker.
- **Exposing the app.** The app speaks plain HTTP. Behind a TLS reverse proxy set `VIBEHEALTH_TRUST_PROXY=1`, list the public
  name in `VIBEHEALTH_ALLOWED_HOSTS` and its origin in `TRUSTED_ORIGINS`. At start the log warns (it only warns) if the
  app can be reached beyond loopback with no password or without a trusted proxy. `VIBEHEALTH_PUBLISHED_ON` says which
  host address the container port is published on (`compose.yaml` derives it from `VIBEHEALTH_BIND`, the Quadlet sets it
  next to `PublishPort`); the warnings are skipped only when it is a loopback address (127.0.0.0/8, `::1`, `localhost`).
- **Files from Paperless are parsed in the server process** (PDF and image rendering for reading and previews), unlike
  uploads, which are opened only in the sandbox. A compromised Paperless could therefore feed the parsers hostile files;
  run Paperless only if you trust it. Routing these files through the sandbox is a possible follow-up.
- **Dependencies** are pinned to exact versions in `requirements.txt` but without hashes. A hashed lock file
  (`pip install --require-hashes`, and image digests in the Dockerfile and `compose.yaml`) is a known follow-up. CI runs
  `pip-audit` and `npm audit`, and Dependabot proposes updates.

## Setup wizard

An installation is `ready` only when a password exists (saved in the app or `APP_PASSWORD_HASH`; a saved hash that
cannot be read counts, the app is then locked) or when the operator explicitly set `VIBEHEALTH_LEGACY_OPEN=1`. Nothing
is inferred from a Paperless token, from documents or from a stored flag. Otherwise it is `needs_setup`: every `/api/*`
request except health, auth status, `/api/setup/*` and the first password change is answered `403 setup_required`, so a
new installation is unusable until a password is set (and so is one after `reset-password`). `VIBEHEALTH_LEGACY_OPEN=1`
keeps a password-less installation open (`/api/auth/status` reports `mode: open_legacy`); not recommended, and it has no
effect once a password exists. The state is derived at runtime (`setup_state.py`); there is no migration. The background
sync and reading workers are internal and unaffected, but nothing they hold is reachable through the API while the
install is gated.

`GET /api/setup/status` is public, so it returns only `state`, `password_set`, `needs_setup_code` and `wizard_pending`
(true without a password unless legacy-open, and with a password until the guide was finished or skipped once): nothing
about Paperless, uploads, Ollama or whether documents exist.

The guide (`/setup`) steps: language, password with the setup code, sources (uploads and/or Paperless; at least one),
Ollama (address, detection, connection test, a readiness checklist with copyable `ollama pull` commands, "skip for
now"), done. It is resumable and can be re-run from Settings without resetting anything. Detection probes a fixed list
of addresses (`host.docker.internal`, `localhost`, `127.0.0.1`, the Docker bridge gateway) on port 11434 only, so it
cannot be used to scan other hosts.

## Uploads

A PDF, JPEG, PNG or WebP can be added without Paperless. Uploads need a password and the `uploads.enabled` setting.

- **Before a byte is read**: at most 3 uploads in flight (429 otherwise); enough free disk (2 x the size cap, plus one
  cap per other upload in flight); the folder under its quota (507 otherwise).
- **Streaming**: the body is parsed as a stream and written to `uploads/.tmp` while a SHA-256 and a byte counter run.
  Past the size cap it stops with 413. A body that stalls or crawls is cut with 408.
- **Type** comes from the magic bytes; the client's Content-Type and file name are ignored (415).
- **Opened for real in the sandbox**, never in the server. PDF: no password to open, 1 to 40 pages, pages at most 2000
  pt on a side. Image: header first (at most 48 megapixels and 16384 px on a side, so a decompression bomb is never
  decoded), then a full decode of the first frame. Failures are 422 with a plain message.
- **Duplicates** (same SHA-256): 409, nothing stored.
- **Storage**: `uploads/<xx>/<random>.<ext>` (the name is generated, never from the client), modes 0600 in 0700
  folders, the resolved path checked to be inside `uploads/`. Thumbnails are cached as `cache/thumbs/<sha256>.jpg`.
- **Serving**: the stored media type, `nosniff`, `private, no-store`, no file name.
- **Editing and deleting** (uploads only): title, kind and date can be changed. Deleting removes the values and runs, the
  row, then the file; the intent to remove the file is written first, so a crash is finished at the next start. A file that
  cannot be removed is retried at start. Files that no row points at are never removed automatically.

**The sandbox** (`sandbox.py`, `sandbox_child.py`). PDFs and images from uploads are parsed by native code, so each job
(check and thumbnail, page count, draw one page) runs in a fresh short-lived `python` child: a wall-clock timeout with a
kill, on POSIX limits on address space (default 2 GB), CPU time, core dumps and open files, a scrubbed environment (no
secrets), stderr dropped, and a length-prefixed reply of bounded size (nothing is unpickled). At most 2 children run at
a time. A failure of any kind is a clean error, never a crash. It is **not** a full sandbox: the child runs as the same
user with the same file system and network. A namespace or seccomp layer would be the next step.

## Model check

**Settings > Model check** tells a person whether a model works on their machine. The registry in `model_check.py`
records what the project measured (`qwen3.5:4b`, `glm-ocr`, `qwen3-vl`, PaddleOCR-VL); every other model is `untested`,
and Ollama's `capabilities` mark a model without `vision` as unusable. Each installed model gets a badge in Settings.

The check (password required, uploads turned on; 409 while a document is being read) takes a page you know, optionally with the values printed
on it (`Name = value unit` or `CODE: value`; at most 400 lines of 200 characters, longer ones are skipped). It runs reader A on at most 5 pages (and reader B when asked), and reports the
values read correctly, wrong values, invented rows, seconds per page and the share of the model in GPU memory. It goes through
the upload code and the sandbox like any upload, creates no document or value, and deletes the file at the end. Only counts are
kept (the last 10 models); never the file, its name, the expected list or what was read.

## Migrations, backups and upgrading

- Each migration runs in its own transaction with its version bump. Before a migration that changes a database with data,
  a copy is written to `backups/pre-v<N>-<timestamp>.db` with `VACUUM INTO` and **verified** (integrity check and row counts
  per table); otherwise the copy is deleted and the start is refused. The newest few verified backups are kept.
- A failed migration is rolled back completely and the app refuses to start. A database newer than the app is refused.
- Version 2 added uploads (a rebuild of `documents` with foreign keys off, checked against a fresh schema in the tests).
- **Backing up**: the whole data folder. The database uses WAL, so copying `vibehealth.db` alone copies an empty shell: take
  `vibehealth.db*` together (or use `VACUUM INTO`), `uploads/` and `.keys/` (not needed if `SECRET_KEY` is set). `cache/`
  and `backups/` are optional. A database restored without `uploads/` still lists its documents, marked as having no file.
- **Upgrading**: pull, rebuild, start; the database is migrated on start. Going back to an older release means stopping
  the app and restoring a backup (an older release refuses a newer database).

## Benchmark method and headline results

The reading pipeline follows an offline benchmark that ran on Greek laboratory reports from two laboratories. Each
candidate model read the report pages, its rows were mapped to test codes with the catalogue, and the values were compared with
the known ground-truth values of those reports. The measures are the ones the Model check reports: values correct, wrong values,
invented rows, seconds per page, GPU share. The reports are real medical data and are not
part of this repository; the figures below are small-sample results, not a guarantee.

| Model | Role | Result |
|---|---|---|
| `qwen3.5:4b` | reader A | 128 of 129 values correct at 150 dpi on two laboratories; about 12 s per results page on an RTX 2070, fully on the GPU |
| `glm-ocr` | reader B | a good second reader; needs the retry rule for cut-off pages |
| `qwen3-vl` (4b instruct) | not recommended | invented more rows |
| PaddleOCR-VL | not recommended | rows shifted silently; needs a lot of memory |

The verification rule (two readers agreeing, or one backed by the text layer) is what turns these per-model results into a
per-value confidence: what the readers disagree on goes to review.

## Known limits

- Measured on Greek lab reports from two laboratories only. Other languages, laboratories and phone photos may read worse.
  Reader A's prompt is the benchmarked one.
- No unit conversion yet (units are kept as printed; the same test can be printed in different units by different laboratories).
- Reader B works with `glm-ocr` only (a parser is written for its output).
- The reading queue lives in memory. One person's records per instance, no per-document access control.
- The data folder is unencrypted at rest, deleted files are not securely erased, and uploads are not scanned for malware.
- Approved values are not shown anywhere else yet (no charts or per-test history).

## Tests

`backend/tests` (pytest, synthetic data only, no network, no real documents) covers the parser and catalogue, the verification
rule, the pipeline with fake Paperless and Ollama servers, settings validation, authentication, throttling, the host and origin
checks, information disclosure in error messages, the setup wizard, migrations (against a committed v1 schema), uploads (spoofed
types, hostile names, size limits, bombs, EXIF, duplicates, deletion), the sandbox and the model check.

```sh
cd backend && pip install -r requirements-dev.txt && pytest
sh verify-pinned.sh        # the same suite against the pinned versions, in a throw-away virtualenv
```
