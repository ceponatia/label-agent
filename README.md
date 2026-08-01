# Label Agent

Watches a Gmail inbox for Poshmark and Vinted shipping-label emails, turns the
attached PDF into a print-ready 4×6 label, and prints it — automatically, or on
demand from a phone via a small web app on the home network. Everything runs in
one process on one machine and keeps its state in a single `data/` directory.

Background and design: [SCOPE.md](SCOPE.md) (what and why),
[SPEC.md](SPEC.md) (how).

## How it works

1. Every few minutes it polls Gmail over IMAP for unprocessed messages from
   `poshmark.com` / `vinted.com` that carry a PDF attachment (direct or
   forwarded copies both count).
2. A cheap Haiku call classifies the email and pulls out item, order/transaction
   id, tracking number and ship-by date; regexes fill in anything the model
   omits, and the tracking number is always the regex's. Same tracking number
   twice → the second one is a `duplicate` and is not auto-printed.
3. The PDF is normalized to exactly 288×432 pt: Poshmark labels are already
   ~4×6 and are scaled; Vinted labels sit on a letter-landscape page and get
   cropped (vector border first, OpenCV contours as fallback), rotated and
   scaled.
4. The result is verified — page size, ink coverage, and a `pyzbar` decode of
   the tracking barcode, plus an optional vision check. A label that fails
   verification becomes `needs_review` and is never printed silently.
5. Good labels are submitted to CUPS. If the printer is off, the label parks in
   `waiting_for_printer` and is retried every two minutes until it comes back.

## Quickstart

```sh
uv venv
uv pip install -e ".[dev]"

cp config.example.toml config.toml     # non-secret settings
cp .env.example .env                   # Gmail app password, Anthropic key

.venv/bin/python -m labelagent db-init
```

Try the pipeline on the two example labels without touching email or the
printer:

```sh
.venv/bin/python -m labelagent process \
    tests/fixtures/poshmark-label.pdf tests/fixtures/vinted-label.pdf -o /tmp/labels
# poshmark-label.pdf: passthrough ok -> /tmp/labels/poshmark-label-4x6.pdf
# vinted-label.pdf: raster-crop ok -> /tmp/labels/vinted-label-4x6.pdf
```

Then run the whole thing:

```sh
.venv/bin/python -m labelagent serve
```

`label-agent` is also installed as a console script, so `label-agent serve`
works once the venv is active.

## Configuration

Two files, neither committed:

- **`config.toml`** — non-secret settings (poll interval, printer name and media
  options, auto-print default, web host/port, retention). Every key is
  documented in `config.example.toml`, and every key can be overridden by an
  environment variable.
- **`.env`** — secrets: `LABELAGENT_IMAP_USER`, `LABELAGENT_IMAP_PASSWORD`,
  `ANTHROPIC_API_KEY`. See `.env.example`.

Leaving `printer_name` empty (or `"file"`) selects the built-in fake printer,
which copies each PDF into `data/printed/` instead of printing. Useful for
setting things up before the printer is ready.

A few settings can also be changed from the web app's Settings page (poll
interval, auto-print, printer name); those are stored in the database and win
over `config.toml`. Changing the printer name takes effect when the service
restarts.

### Gmail app password

The agent authenticates only to Elaine's personal Gmail
(`elaineamy.g2010@gmail.com`), where the platform mail lands. It uses IMAP with
a Google **app password** — no OAuth app to maintain, and nothing expires unless
the password is revoked.

1. Sign in to that Google account and open
   [myaccount.google.com/security](https://myaccount.google.com/security).
2. Turn on **2-Step Verification** if it isn't already — app passwords are only
   available on accounts that have it.
3. Go to **App passwords** (search "app passwords" in the account settings),
   create one named `label-agent`, and copy the 16-character value.
4. Put it in `.env`:

   ```sh
   LABELAGENT_IMAP_USER=elaineamy.g2010@gmail.com
   LABELAGENT_IMAP_PASSWORD=abcdefghijklmnop
   ```

5. Check it works: `.venv/bin/python -m labelagent check-now`.

The mailbox is opened read-only apart from one thing: processed messages get the
Gmail label `label-agent/processed`, which is how a reinstall or a lost database
still can't cause double prints. Nothing is ever marked read, moved or deleted.

### Printer setup (macOS)

The Canon PIXMA TS9521Ta speaks AirPrint, which the CUPS built into macOS
handles without a vendor driver.

1. **System Settings → Printers & Scanners → Add Printer**, pick the TS9521 off
   the network. Load 4×6 stock in the rear tray.
2. Find the CUPS queue name (the short name, not the display name):

   ```sh
   lpstat -p
   # printer Canon_TS9521a_series is idle.  enabled since ...
   ```

3. Find the real names of the media and tray options for this printer:

   ```sh
   lpoptions -p Canon_TS9521a_series -l
   # PageSize/Media Size: ... *na_index-4x6_4x6in ...
   # InputSlot/Paper Source: ... rear ...
   ```

4. Put all three in `config.toml`:

   ```toml
   printer_name = "Canon_TS9521a_series"
   print_media = "na_index-4x6_4x6in"
   print_media_source = "rear"
   ```

5. Print the calibration page and check the tray, orientation and margins
   without burning a real label:

   ```sh
   .venv/bin/python -m labelagent test-print
   ```

   It prints a 4×6 page with a border 4 pt inside the edge, corner registration
   marks and half-inch ticks. If the border is cut off or the sheet comes out
   sideways, the media/tray options are wrong. The same page is behind the
   **Test print** button in the web app's Settings.

Because this is an inkjet rather than a full-bleed thermal printer, labels are
fitted with small margins rather than printed borderless; the barcode is checked
either way.

## Running as a service

### macOS (launchd) — the current host

```sh
mkdir -p data/logs
# edit the three /Users/CHANGEME/label-agent paths first
cp deploy/launchd/com.snarebox.label-agent.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.snarebox.label-agent.plist

launchctl list | grep label-agent      # is it running?
tail -f data/logs/label-agent.err.log  # what is it doing?
launchctl unload ~/Library/LaunchAgents/com.snarebox.label-agent.plist  # stop
```

`KeepAlive` restarts it after a crash and `RunAtLoad` starts it at login. Laptop
sleep is fine: nothing prints while the lid is closed, and the next poll after
wake catches up on everything that arrived meanwhile.

### Linux (systemd) — the future home server

`deploy/systemd/label-agent.service`; edit `User`, `WorkingDirectory` and
`ExecStart`, then `sudo systemctl enable --now label-agent`.

### Docker — the portable path

`deploy/Dockerfile` and `deploy/docker-compose.yml`. The container uses
`network_mode: host` because the printer is discovered on the LAN, so the web
app is on the host's port 8080 directly.

```sh
docker compose -f deploy/docker-compose.yml up -d --build
```

## Using it

Open the web app from any device on the home network:

```
http://<laptop-name>.local:8080
```

`<laptop-name>` is what `scutil --get LocalHostName` prints on the host (e.g.
`http://elaines-macbook.local:8080`). On an iPhone, Share → **Add to Home
Screen** installs it as an icon; it is a PWA and behaves like an app.

- **Dashboard** — running/paused, printer status, last check, today's counts,
  and anything needing attention with a one-tap Print.
- **History** — day by day, with a 14-day sparkline and reprint buttons.
- **Label detail** — original vs. cropped previews, metadata, full event
  timeline.
- **Errors** — recent warnings and errors, filterable by stage.
- **Settings** — poll interval, auto-print, printer name, test print.

**Pause means "stop printing", not "stop working".** A paused agent still reads
email, still crops and verifies labels, and still queues them; it just doesn't
send anything to the printer. So the printer can stay off all day with the agent
paused and nothing is lost — resume (or tap Print on a single label) and the
queue goes out. Auto-print off is the same idea for the printing step only:
labels reach `ready` and wait for a tap.

There is no login. The app is meant for a trusted home network and should not be
exposed to the internet.

### CLI

```
label-agent db-init                      create data/ and the SQLite schema
label-agent process <pdf...> [-o DIR]    normalize + verify PDFs; --print to print them
label-agent check-now                    one poll cycle, print the summary, exit
label-agent test-print                   print the 4×6 calibration page
label-agent serve                        web app + background scheduler
```

All of them accept `--config path/to/config.toml`.

## Where the data lives

Everything is under `data/`:

```
data/
  labelagent.db          SQLite: labels, events, settings
  labels/<id>/original.pdf, print.pdf, *-preview.png
  printed/               only when using the fake file printer
  logs/                  launchd stdout/stderr
```

**Backup:** copy `data/`. **Move to another machine:** install the app there,
copy `data/` over, done — the database and every label PDF travel together.
Stop the service first so SQLite isn't mid-write, or copy from a snapshot.

Database rows are kept forever, so history and metrics survive. The PDFs behind
them are deleted once a label is finished with (printed, failed or duplicate)
and older than `label_retention_days` — 90 by default, `0` to keep everything.
A daily job does this; labels in `needs_review` or `waiting_for_printer` keep
their files however old they are, since they still have somewhere to go.

## Tests

```sh
.venv/bin/python -m pytest -q
```

The suite is offline and hermetic — no network, no real printer, no API key
needed. It runs the real pipeline against the committed example labels, decodes
the printed barcodes with `pyzbar` to prove the crop is intact, and drives the
whole agent end to end (six fixture emails in → two printed labels, two
duplicates, two ignored) including the web app on top of the real service.
