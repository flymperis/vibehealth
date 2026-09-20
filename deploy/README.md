# Running VibeHealth with rootless Podman and systemd (Quadlet)

`vibehealth.container` is an example unit. It needs Podman 4.4 or newer (Quadlet) and a systemd user session.

1. Build the image from the repository root (`--format docker` keeps the image's health check):

   ```sh
   podman build --format docker -t localhost/vibehealth:latest .
   ```

2. Install the unit and start it:

   ```sh
   mkdir -p ~/.config/containers/systemd
   cp deploy/vibehealth.container ~/.config/containers/systemd/
   systemctl --user daemon-reload
   systemctl --user start vibehealth.service
   ```

   `[Install] WantedBy=default.target` in the file is what starts it at login. Quadlet units are
   generated, so there is no `systemctl enable`. To have it start at boot without you logging in, run
   `loginctl enable-linger "$USER"` once.

3. Find the setup code and open `http://127.0.0.1:5001`:

   ```sh
   journalctl --user -u vibehealth.service | grep "Setup code"
   ```

The data lives in `~/vibehealth-data` (the `Volume=` line). Back up that whole folder.

Settings and secrets: put them in a private env file (see `.env.example`) and uncomment
`EnvironmentFile=` in the unit. Never write secrets inline in the unit.

Check the unit without starting it: `/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun`.

Update: rebuild the image, then `systemctl --user restart vibehealth.service`.
