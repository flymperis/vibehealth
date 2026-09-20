# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Open a **private security advisory** (GitHub
"Report a vulnerability" under the Security tab) and include what you found, how to reproduce it and the
version. You will get an answer as soon as the maintainers can give one.

Maintainers: private vulnerability reporting must be enabled on the repository (Settings > Code security >
Private vulnerability reporting) before the project is published.

If advisories are not available, open a public issue that only asks for a private channel, **without any
vulnerability details**, and wait for a reply before sending them.

## Supported versions

Only the latest release (or the latest commit on the main branch) is supported.

## Threat model

VibeHealth stores medical records for one person or household and is meant for a trusted network
(loopback, a LAN or a VPN such as Tailscale), not for the public internet. It defends against other
people and other websites reaching that network: a password is required (a fresh install needs a
one-time setup code), sessions are signed, state-changing requests are checked for origin, the Host
header is checked against an allowlist, login attempts are throttled, saved secrets are encrypted
and write-only, and uploaded files are opened only in a short-lived, resource-limited child process.
It does **not** defend against someone with access to the machine or its disk (the data folder is not
encrypted at rest; use disk encryption), it does not securely erase deleted files, and it does not scan
uploads for malware. Run it behind a TLS reverse proxy if it must be reached over an untrusted network,
and never expose it to the internet directly.

Files that come from Paperless (for reading and for previews) are parsed in the server process, not in the
sandbox: only uploaded files are parsed in the sandbox. A compromised or malicious Paperless server could
therefore feed the PDF and image parsers hostile files. Use Paperless only if you trust it; moving that parsing
into the sandbox is a known follow-up (see docs/DESIGN.md).

A new installation is unusable until a password is set. `VIBEHEALTH_LEGACY_OPEN=1` keeps an installation without
a password open (the whole API, no login); it is not recommended.
