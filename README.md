# VibeHealth

A private, self-hosted place for your health records. It reads lab values out of PDF reports and
photos with **local AI models running on your own machine** (through [Ollama](https://ollama.com)),
lets you check and approve every value, and keeps the history. Documents come from an optional
[Paperless-ngx](https://docs.paperless-ngx.com) server or from files you upload.

- Everything stays on your machine: no cloud, no accounts, no telemetry.
- Nothing is trusted blindly: two readers read each page, and values they do not agree on wait for your review.
- One person's records per instance. English and Greek interface.

> **Not a medical device. Not medical advice.** VibeHealth is a personal record-keeping tool. The AI
> readers make mistakes: always compare a value with the original report before you rely on it, and talk to
> a doctor about your health. Do not use it to diagnose, treat or make medical decisions.

## Requirements

- Docker (with Compose) or Podman.
- [Ollama](https://ollama.com) (the bundled compose service is recommended) with two models: `qwen3.5:4b` (reader A) and `glm-ocr` (reader B).
- Hardware for the models: a few GB of VRAM (or RAM). A GPU reads a page in roughly seconds; CPU-only works but is much slower.

## Quick start

```sh
git clone https://github.com/flymperis/vibehealth.git && cd vibehealth
docker compose up -d --build
docker compose logs vibehealth        # the one-time setup code is printed here
```

Open <http://127.0.0.1:5001> and follow the setup guide: choose a password (it asks for the setup code once),
pick where documents come from, and check that Ollama is ready. The code is also in the container at
`/data/.setup-code` (`docker compose exec vibehealth cat /data/.setup-code`). A new installation is unusable until a
password is set (passwords need at least 10 characters).

> Do not paste the logs anywhere (issues, forums, chats) before a password is set: they contain the setup code.

With Podman, use `podman compose` the same way, or run it as a systemd service: see [deploy/README.md](deploy/README.md).

## Ollama

**Recommended: the bundled Ollama service.** It runs next to VibeHealth on the compose network and is not published
to the host or your LAN:

```sh
docker compose --profile ollama up -d
docker compose exec ollama ollama pull qwen3.5:4b
docker compose exec ollama ollama pull glm-ocr
```

Then set `OLLAMA_URL=http://ollama:11434` (in `.env`, see `.env.example`, or in Settings). GPU notes are in `compose.yaml`.

**Ollama already running on the host.** The container reaches the host through `host.docker.internal` (already set up in
`compose.yaml`; it is also the default Ollama address in the app). **Ollama has no authentication at all**: anything that
can reach its port can run models on your machine and read what it has loaded, so never bind it to `0.0.0.0` or to a
LAN address. On Linux the container comes from the Docker bridge, so bind Ollama to the bridge (host-gateway) address
only, not to everything:

```sh
ip -4 addr show docker0                       # usually 172.17.0.1
sudo systemctl edit ollama                    # add the two lines below, then: sudo systemctl restart ollama
#   [Service]
#   Environment="OLLAMA_HOST=172.17.0.1:11434"
```

Then set the app's Ollama address to `http://172.17.0.1:11434` (or use `host.docker.internal` if it resolves to that
address). If you must let Ollama listen more widely, add a firewall rule that only allows the container network to reach
its port, for example with ufw:

```sh
sudo ufw allow from 172.17.0.0/16 to any port 11434 proto tcp
sudo ufw deny 11434/tcp
```

(These commands are examples for Linux with the default Docker bridge; a compose project may use another subnet, see
`docker network inspect vibehealth_default`. With rootless Podman the host address is different again.) Ollama for Mac
and Windows already reaches Docker Desktop containers through `host.docker.internal` without listening beyond loopback.

## Choosing models

The project measured these, on Greek lab reports:

| Model | Role | Result |
| --- | --- | --- |
| `qwen3.5:4b` | reader A (default) | 128 of 129 values correct at 150 dpi on two labs; about 12 s per results page on an RTX 2070, fully on the GPU |
| `glm-ocr` | reader B (default) | a good second reader; needs its retry rule for cut-off pages (built in) |
| `qwen3-vl` (4b instruct) | not recommended | invented more rows |
| PaddleOCR-VL | not recommended | rows shifted silently, and it needs a lot of memory |

Any other Ollama vision model that supports structured JSON output can be tried as reader A, but it is **untested**:
Settings shows a badge (tested, untested, not recommended, not a vision model) next to each installed model. To find
out whether a model works on your machine, use **Settings > Model check**: send a page you know (optionally with the
values printed on it, one per line as `Name = value unit` or `CODE: value`) and it reports how many values it read
correctly, how many rows look invented, the seconds per page and how much of the model sits in GPU memory. The sample
file is deleted when the run ends and only counts are kept. The check needs a password, like uploads.

Reader B is tied to `glm-ocr`: it reads its "Text Recognition:" output with a parser written for it, so other models
produce text the parser cannot use. You can switch reader B off and run with reader A alone; values are then verified
less strongly (more of them are left for you to review).

## Adding documents

- **Upload**: add a PDF, JPEG, PNG or WebP in the app. The original is kept in the data folder. Needs a password.
- **Paperless-ngx** (optional): enter its address and an API token in the setup guide or Settings. VibeHealth
  syncs the documents with the chosen type or tags and never copies the files out of Paperless.

Then open a document and press Read. Verified values can be approved in one step; the rest need a look.

**Reports are not read for values.** A document of kind imaging, medical opinion or prescription has findings and a
conclusion, not a table of lab values. It is not sent to the two lab readers: reader A transcribes its pages, and one
more call to the same local model writes a short **automatic summary** (the conclusion and up to eight key findings).
The document shows the summary (labelled as automatic; the original prevails), the findings and the full text per page.
If the summary fails the text is kept and Read again retries it. A page that looks like lab results inside a report gets
a notice with a button to read the whole document as a blood test. Blood tests and documents of kind "other" are read
for values as before; which kinds are which is one constant, `LAB_KINDS` in `backend/app/models.py`.

## Configuration

Everything can be set in the app. Environment variables are optional defaults: copy `.env.example` to `.env` and
uncomment `env_file` in `compose.yaml`. Settings saved in the app win over the environment.

## Backups

Back up the **whole data folder** (`/data`): `vibehealth.db*` (the database uses WAL mode, so **never copy only
`vibehealth.db`**), `uploads/` (your original files) and `.keys/` (the key that decrypts saved secrets). With the
default named volume (the archive is made readable by you only):

```sh
docker compose stop vibehealth
( umask 077 && docker run --rm -v vibehealth_vibehealth-data:/data:ro -v "$PWD":/backup alpine \
    sh -c 'umask 077 && tar czf /backup/vibehealth-backup.tar.gz -C /data .' )
chmod 600 vibehealth-backup.tar.gz
docker compose start vibehealth
```

Backups contain medical records: store them as carefully as the app. **Keeping `.keys/` in the same archive as the
database defeats the encryption of the saved secrets** (the Paperless token): anyone who has the archive has the key
too. Either set `SECRET_KEY` in the environment (then no key file is stored in the data folder; keep the value in
your password manager, not in the backup), or take `.keys/` out of the archive (`--exclude=./.keys` in the `tar`
command) and store it somewhere else. To restore, extract the archive into an empty volume (or folder) with the app
stopped, and put the key back (or set the same `SECRET_KEY`).

## Upgrading

```sh
git pull
docker compose up -d --build
```

The database is migrated on start (a checked backup is written to `backups/` first). Back up before upgrading.
Going back to an older version means restoring a backup.

## Uninstalling

```sh
docker compose down            # keeps your data
docker compose down -v         # also deletes the data volume: your records are gone
```

## Exposing safely

The app speaks plain HTTP and is meant for this machine, your LAN or a VPN such as Tailscale. **Never expose it
to the internet.** To reach it from other devices, publish it on that network address only (set `VIBEHEALTH_BIND`,
see the `ports` comment in `compose.yaml`). If you put a TLS reverse proxy (Caddy, Traefik, nginx) in front:

- set `VIBEHEALTH_TRUST_PROXY=1` so cookies are `Secure` and client addresses are right,
- list the public name in `VIBEHEALTH_ALLOWED_HOSTS` and its origin in `TRUSTED_ORIGINS`,
- keep the app itself reachable only from the proxy.

The log warns at start if the app is reachable beyond loopback with no password or no trusted proxy. Compose publishes
the port on `VIBEHEALTH_BIND` (default `127.0.0.1`) and tells the app the same address, so the warnings appear exactly
when you publish it on something else.

## Security in short

A password is required: a new installation is unusable until you set one with the setup code (`VIBEHEALTH_LEGACY_OPEN=1`
keeps an installation without a password open; not recommended). Saved secrets such
as the Paperless token are encrypted and write-only. Uploaded files are opened only in a short-lived,
resource-limited child process. The container runs as a non-root user with a read-only file system and no
capabilities. The data folder is not encrypted at rest and **deleted files are not securely erased**: use
disk encryption if that matters. Locked out? `docker compose exec vibehealth python -m app.security reset-password`.
See [SECURITY.md](SECURITY.md) to report a problem.

## Known limitations

- Reading quality was measured on Greek lab reports from two labs. Other languages, other labs and phone photos may read worse.
- Reader B works with `glm-ocr` only. Models other than `qwen3.5:4b` and `glm-ocr` are untested.
- No unit conversion yet; units are kept as printed.
- One person's records per instance; there is no per-document access control.
- The reading queue lives in memory: a restart drops waiting documents.

## Development and tests

```sh
cd backend
pip install -r requirements-dev.txt
pytest
```

Frontend: `cd frontend && npm ci && npm run dev` (proxies `/api` to `http://127.0.0.1:5001`; set `VITE_API_TARGET` to change it).
Design notes are in [docs/DESIGN.md](docs/DESIGN.md).

## Licence

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0). Copyright VibeHealth.
