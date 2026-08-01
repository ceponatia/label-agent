# Label Agent — Starting Specification

**Status:** Draft v0.1 — accompanies [SCOPE.md](SCOPE.md). Open questions there
may change details here; sections marked *(assumption)* depend on them.

## 1. System overview

```
                ┌────────────────── host machine (Elaine's laptop → home server later) ──────────┐
                │                                                                                │
 Gmail ────────▶│  Ingest worker ──▶ Label pipeline ──▶ Print worker ──▶ CUPS ──▶ Canon PIXMA    │
 (poll)         │                                                                (4×6 rear tray) │
                │       │                  │                 │                                   │
                │       └──────────────────┴────────┬────────┘                                   │
                │                                   ▼                                            │
                │                              SQLite DB                                         │
                │                                   ▲                                            │
                │                    Web app (FastAPI, LAN only)                                 │
                └───────────────────────────────────│────────────────────────────────────────────┘
                                                    ▼
                              Elaine's iPhone / iPad / laptop browser
```

One long-running service, three logical parts sharing one SQLite database:

1. **Ingest worker** — polls Gmail on an interval, finds candidate emails,
   downloads PDF attachments, records a `label` row per new shipment.
2. **Label pipeline** — normalizes each label PDF to print-ready 4×6, with an
   LLM sanity check.
3. **Web app + print worker** — control panel, metrics, and the actual
   `lp` submission with retry handling.

## 2. Tech stack (Python + FastAPI confirmed)

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Best PDF/imaging libraries; simple service code |
| Web framework | FastAPI + Jinja2 templates + htmx | One process, server-rendered, trivially mobile-responsive; no SPA build step |
| Scheduler | APScheduler (in-process) | Poll interval, retries |
| DB | SQLite (WAL mode) | Single-writer, tiny footprint, file backup |
| PDF handling | PyMuPDF (fitz) | Crop boxes, rendering, page-size normalization |
| Image analysis | OpenCV (contour detect) for bounding box; only if PDF geometry insufficient | Deterministic crop |
| Email | IMAP + app password (`imap-tools`), using Gmail IMAP extensions | No OAuth-app maintenance; stable message IDs + labels for idempotency (see §3.0) |
| Printing | CUPS via `lp` / `pycups` | Standard Linux print path |
| LLM | Anthropic API, `claude-haiku-4-5` | Cheap classification + vision verification |
| Service | launchd agent on the macOS laptop (v1); systemd unit for the future home server; optional Docker Compose as the portable path | Auto-start, restart on crash, easy migration |

## 3. Ingestion

### 3.0 Mailbox (confirmed)

Platform emails are addressed to `gelaine@umich.edu` but are **pulled into
Elaine's personal Gmail** (`elaineamy.g2010@gmail.com`), so the agent
authenticates only to her personal Gmail.

**Auth: IMAP + Google app password** (requires 2-Step Verification on her
account). Chosen over Gmail-API OAuth deliberately: a personal (unverified)
OAuth app using restricted Gmail scopes gets refresh tokens that expire every
7 days while in "testing" status, which is unusable for an always-on agent,
and full verification is overkill for one household. An app password never
expires unless revoked.

IMAP still gives us everything we need via Gmail's IMAP extensions:
`X-GM-MSGID` for stable message IDs, `X-GM-LABELS` to tag processed messages
with a `label-agent/processed` Gmail label, and `X-GM-RAW` for Gmail-syntax
searches. The mailbox is opened read-only except for label writes; nothing is
ever marked read, moved, or deleted.

### 3.1 Detection

Poll every N minutes (default 3, configurable). A message is a **candidate** if:

- From (or embedded forwarded-From) matches `poshmark.com` or `vinted.com`, **and**
- it has at least one PDF attachment.

Candidates go to the **classifier agent** (Haiku, text-only, ~1k tokens):

> Input: sender, subject, plain-text body (truncated), attachment filenames.
> Output (JSON): `{is_label_email, platform, item_title, order_id,
> tracking_number, ship_by_deadline, confidence}`

Rationale: survives template drift and both direct + forwarded (`Fwd:`) forms
without brittle regexes. Regex fallback extracts tracking/order IDs if the
model omits them (both platforms print them in plain text).

### 3.2 Idempotency & dedupe

- Processed messages are recorded by `X-GM-MSGID` in the DB and tagged with
  the Gmail label `label-agent/processed` via IMAP, so a reinstall or DB loss
  still can't double-print history.
- A shipment's natural key is its **tracking number**. A second email carrying
  an already-seen tracking number becomes a `duplicate` row: not auto-printed,
  shown in the UI with a "print anyway" action.

### 3.3 Attachment handling

PDFs are saved under `data/labels/{label_id}/original.pdf`. Non-PDF label
formats (PNG/JPG) are out of scope for v1 but the storage layout allows them.

## 4. Label pipeline

Target output: single-page PDF, exactly **288×432 pt (4×6 in)**, portrait,
label content full-bleed. Stored as `data/labels/{label_id}/print.pdf`.

Stages (each records success/failure + timing in the DB):

1. **Measure** — read page size of the original PDF.
2. **Route:**
   - Page ≈ 4×6 within 5% tolerance (Poshmark case, 292×436 pt): scale to
     exactly 288×432, done.
   - Otherwise (Vinted case: 792×612 letter-landscape): continue.
3. **Bounding-box detection** — deterministic, two attempts in order:
   a. **Vector:** largest rectangle among the page's drawn paths (the label's
      printed border) via PyMuPDF drawings.
   b. **Raster:** render at 150 dpi, OpenCV threshold + largest-contour
      bounding rect.
   Add small margin, clamp to page.
4. **Crop & orient** — set the crop box; rotate so height > width; scale to
   288×432 preserving aspect (letterbox with white if aspect ≠ 2:3).
5. **Verify (vision agent)** — render `print.pdf` at ~100 dpi, send to Haiku:
   > "Is this a complete, printable USPS shipping label? Are the tracking
   > barcode, ship-to address, and postage block fully visible and uncut?
   > JSON: {ok, problems[]}"
   - `ok: true` → mark **ready**.
   - `ok: false` → mark **needs_review**; do not print; UI shows original +
     cropped previews side by side and offers "print original on letter" /
     "print crop anyway".
6. **Queue for print** — if agent is running and auto-print is on.

Estimated LLM cost: one small text call + one small vision call per label —
fractions of a cent.

## 5. Printing

Target device: **Canon PIXMA TS9521Ta on WiFi, 4×6 stock in the rear tray**.
The TS9500 series supports AirPrint (driverless IPP), which CUPS — built into
macOS — speaks natively, so no vendor driver is needed; the printer is
addressed by its network name from any host on the LAN.

- Submit via CUPS, e.g.
  `lp -d <printer> -o media=na_index-4x6_4x6in -o media-source=rear print.pdf`
  — exact `media`/`media-source`/borderless values discovered from
  `lpoptions -l` for the actual model during Phase 1 and stored in settings.
  A **test-print button** in Settings prints a ruler/calibration page so tray
  and margins can be verified without burning a real label.
- Because this is an inkjet (not full-bleed thermal), default to
  fit-with-margins rather than borderless; barcode integrity is checked by the
  verify stage either way.
- **Printer offline / job stuck:** an inkjet that's powered off is the normal
  case here, not an error. Job state polled via CUPS; after timeout the label
  is marked `waiting_for_printer` (not failed) and retried on a backoff
  schedule and whenever the printer reappears. UI banner: "Printer unreachable
  since 14:32".
- **Reprint** is always available per label from the UI (increments a copy
  counter, records who/when).
- The print worker is an internal interface (`submit(pdf) → job status`) so it
  can later run as a **standalone LAN print relay** if the brain moves to the
  cloud (see §9).

## 6. Data model (SQLite)

```sql
labels (
  id INTEGER PK,
  platform TEXT CHECK(platform IN ('poshmark','vinted')),
  item_title TEXT,
  order_ref TEXT,            -- Poshmark order id / Vinted transaction id
  tracking_number TEXT,      -- natural dedupe key (indexed, non-unique)
  ship_by TEXT,              -- ISO date, Vinted deadline if present
  gmail_message_id TEXT UNIQUE,
  email_received_at TEXT,
  status TEXT CHECK(status IN (
    'ingested','processing','ready','queued','printing',
    'printed','needs_review','waiting_for_printer','failed','duplicate')),
  status_detail TEXT,        -- human-readable last error / note
  original_path TEXT, print_path TEXT,
  print_count INTEGER DEFAULT 0,
  created_at TEXT, printed_at TEXT
)

events (                     -- audit trail + error log + metrics source
  id INTEGER PK,
  label_id INTEGER NULL REFERENCES labels(id),
  stage TEXT,                -- ingest | classify | crop | verify | print | system
  level TEXT,                -- info | warn | error
  message TEXT,
  created_at TEXT
)

settings (key TEXT PK, value TEXT)
-- keys: agent_state ('running'|'paused'), auto_print ('on'|'off'),
--       poll_interval_min, printer_name, last_poll_at, ...
```

Daily metrics are computed by query (`GROUP BY date(printed_at)`), no rollup
table needed at this volume.

## 7. Web app

LAN-only, bound to `0.0.0.0:8080` on the host *(assumption: trusted network,
no auth in v1; PIN gate is a cheap add if wanted)*. Server-rendered, responsive
layout (single column on phone). Optional PWA manifest so it can be added to
the iPhone home screen.

### Pages

1. **Dashboard `/`**
   - Big status card: Running / Paused, printer status, last email check time.
   - Primary actions: **Pause/Resume**, **Check now**, auto-print toggle.
   - "Today": labels printed, pending, needs-review, errors.
   - Pending/needs-review list with thumbnail, item title, one-tap **Print**.
2. **History `/history?date=YYYY-MM-DD`**
   - Day picker (defaults today, arrows for prev/next, calendar input).
   - Per-day list: time, platform, item, status, reprint button.
   - Small 14-day sparkline of print counts.
3. **Label detail `/labels/{id}`**
   - Original vs. cropped preview images, full metadata, event timeline,
     Print / Reprint / Mark resolved.
4. **Errors `/errors`** — reverse-chron error events, filterable by stage.
5. **Settings `/settings`** — poll interval, printer selection (from CUPS
   list), auto-print default, test print button.

### HTTP API (used by the pages; also curl-able)

```
GET  /api/status                 → agent state, printer state, today's counts
POST /api/agent/pause | /resume
POST /api/agent/check-now        → trigger immediate poll
POST /api/labels/{id}/print
GET  /api/labels?date=&status=
GET  /api/labels/{id}/preview.png | original.pdf | print.pdf
GET  /api/metrics/daily?from=&to=
POST /api/settings
```

### Pause semantics (default, per SCOPE §7)

Paused = ingestion continues (email is still read, labels are still cropped
and queued) but **nothing is sent to the printer**. Resuming, or tapping Print
on a queued label, releases jobs. This means the printer can stay off with the
agent "paused" and nothing is lost.

## 8. Agents & cost profile

| Agent | Model | Modality | When | Est. tokens |
|---|---|---|---|---|
| Email classifier | claude-haiku-4-5 | text | per candidate email | ~1–2k in / 100 out |
| Crop verifier | claude-haiku-4-5 | vision | per label | ~1.5k in / 100 out |

Everything else (polling, download, crop, print, metrics) is deterministic
code. No agent is on the critical path for pause/resume/reprint. API key in an
env file readable only by the service user.

## 9. Configuration, deployment & migration path

- `config.toml` (non-secret) + `.env` (secrets: Anthropic key, Gmail
  credentials path). Neither committed.
- Gmail credentials: a Google **app password** for
  `elaineamy.g2010@gmail.com` in `.env` (one-time setup with Elaine: enable
  2-Step Verification, generate app password). No OAuth flow, nothing expires.
- **All state lives in one `data/` directory** (SQLite DB, PDFs, tokens) —
  migrating hosts = install + copy `data/`. Backup = copy `data/`. Retention:
  keep label PDFs 90 days (configurable), DB rows forever.
- **Host 1 (now): Elaine's macOS laptop.** launchd agent (`KeepAlive`), CUPS
  built in, WiFi printer added by network name. Laptop sleep is fine: on wake,
  the next poll catches up on anything missed; nothing prints while it's
  closed. (Her Windows laptop is not used as a host — it would need
  Docker/WSL for CUPS; the web app is of course reachable from it like any
  browser.) Optional Dockerfile + compose as the OS-independent path.
- **Host 2 (planned): home server** — same artifact, copy `data/`, done.
- **Host 3 (possible): cloud** — the ingest/pipeline/web parts run anywhere,
  but the printer is on the home LAN, so the print worker splits into a small
  **print relay** process at home (polls the cloud brain for ready jobs over
  HTTPS — outbound only, no port forwarding). Cloud hosting also requires
  adding real login to the web app. Out of scope for v1; the print-worker
  interface (§5) is the seam that keeps it cheap later.

## 10. Error taxonomy (what the UI reports)

| Stage | Example | Label status | Auto-recovery |
|---|---|---|---|
| ingest | Gmail unreachable, token expired | — (system banner) | retry each poll; banner until fixed |
| classify | model unsure / API down | `needs_review` | manual classify buttons in UI |
| crop | no bounding box found | `needs_review` | print-original fallback offered |
| verify | barcode cut off | `needs_review` | side-by-side review UI |
| print | CUPS error, printer off | `waiting_for_printer` | backoff retry + reappearance trigger |
| print | job cancelled or aborted at the printer | `failed` | none — a human stopped this job; Print reprints it |

Rule: **an error never deletes or skips a label silently** — every shipment
email that passes classification exists as a row Elaine can see and act on.

## 11. Testing

- **Golden files:** the two example emails/labels in this repo become fixture
  tests — full pipeline offline (mock Gmail, mock `lp`), asserting output page
  size, orientation, and successful barcode detection (decode the Code128 with
  `pyzbar` as an objective crop check).
- **Crop fuzz:** synthetic pages with the label at random positions/rotations.
- **Duplicate handling:** same tracking number twice → second is `duplicate`.
- **Printer-off simulation:** CUPS queue disabled → status transitions +
  recovery verified.
- Manual acceptance: print both example labels on the real printer; scan
  barcodes with a phone to confirm integrity.

## 12. Milestone acceptance (maps to SCOPE phases)

- **P1 done:** `python -m labelagent process fixtures/*.pdf` produces two valid
  4×6 PDFs; both print correctly on the real printer via CLI.
- **P2 done:** a label email sent to the monitored inbox prints hands-free
  within one poll interval; restart-safe (no double prints).
- **P3 done:** Elaine pauses, resumes, reprints, and reads yesterday's history
  from her iPhone.
- **P4 done:** survives host reboot and a full day with the printer off, then
  prints everything queued when it returns.
