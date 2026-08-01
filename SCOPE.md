# Label Agent — Scope Document

**Project:** Automated shipping-label printing for Elaine's Poshmark & Vinted shops
**Status:** Draft v0.1 — pre-implementation
**Date:** 2026-08-01

## 1. Problem statement

When Elaine sells an item on Poshmark or Vinted, the platform emails her a prepaid
USPS shipping label as a PDF attachment. Today she has to open the email on a
laptop, download the attachment, fix the page size/orientation so it prints
correctly on the label printer, and print it. This is manual, error-prone, and
requires a laptop.

The Label Agent watches her inbox for these emails, extracts the label PDF,
normalizes it to the printer's label size (crop / rotate / scale as needed), and
prints it — automatically, or on demand from a phone/tablet/laptop via a small
web app on the home network.

## 2. Goals

1. **Automatic ingestion** — connect to Gmail and detect new Poshmark/Vinted
   label emails without user action.
2. **Label normalization** — produce a correctly sized, print-ready 4×6 label
   from whatever the platform attaches:
   - Poshmark: attachment is already ~4×6 (292×436 pt) → pass through / normalize.
   - Vinted: attachment is a full US-Letter landscape page with the label
     occupying part of it → detect label bounding box, crop, rotate, scale.
3. **Automatic printing** — send the normalized label to the household printer
   (auto-print mode), with safe handling when the printer is off/unreachable.
4. **Control panel (local web app)** — reachable from Elaine's iPhone, tablet,
   and laptop on the home network:
   - Pause / resume the agent.
   - Run manually: "check email now", print/reprint an individual label.
   - View pending queue and label previews before printing.
5. **Metrics & history** — small local database recording every label and error:
   - "What printed today?" with day-by-day history.
   - Errors surfaced clearly (fetch, parse, crop, print), per stage.
6. **Cheap to run** — deterministic code wherever possible; small/cheap LLM
   agents (e.g. Claude Haiku) only for fuzzy steps (email classification,
   crop sanity-check). Target well under ~1¢ per label on average.

## 3. Non-goals (v1)

- No interaction with Poshmark/Vinted websites or APIs (email is the only source).
- No packing slips, pick lists, or inventory management.
- No public-internet access to the web app (LAN only); no multi-user accounts.
- No support for platforms beyond Poshmark and Vinted (design leaves room to add).
- No QR-code / "print at USPS" flows.
- No push notifications (possible v2: notify on error or on label printed).

## 4. Users

- **Elaine** — primary user. Non-technical use: opens the web page on whatever
  device is handy, glances at status, pauses/resumes, taps reprint.
- **Brian** — admin. Sets up host machine, Gmail credentials, printer, and
  maintains the service.

## 5. What we know from the example data

| | Poshmark | Vinted |
|---|---|---|
| Sender | `orders@poshmark.com` | `no-reply@vinted.com` |
| Label delivery | PDF attachment (`pre-paid mailing label 4x6.pdf`) | PDF attachment (`Vinted-Label-<txn>.pdf`) |
| Label page size | 292×436 pt ≈ 4.06"×6.06" (portrait, full-bleed) | 612×792 pt letter **landscape**; label occupies left portion |
| Crop needed | No (normalize only) | Yes (bounding-box crop + rotate to portrait + scale) |
| Parseable metadata | item title, order ID, tracking #, buyer | item title, transaction ID, tracking #, ship-by deadline |

The example emails in this folder were **manually forwarded** from Elaine's
Gmail; the originals were delivered to `gelaine@umich.edu`. The agent must
recognize both direct platform emails and forwarded copies (`Fwd:` subjects,
different From header, attachment preserved).

## 6. Decisions (confirmed 2026-08-01)

- **Mailbox:** platform emails are addressed to `gelaine@umich.edu` but are
  **pulled into Elaine's personal Gmail** (`elaineamy.g2010@gmail.com`), so
  the agent authenticates to her personal Gmail only. Auth method: **IMAP +
  Google app password** (see SPEC §3.0 for why this beats Gmail-API OAuth for
  a personal always-on agent). Requires 2-Step Verification enabled on her
  Google account to generate the app password.
- **Printer:** Canon PIXMA **TS9521C-series ("TS9521Ta") inkjet on WiFi**,
  printing on 4×6 stock fed from the rear tray — not a thermal label printer.
  TS9500-series supports AirPrint, i.e. driverless IPP that CUPS speaks
  natively — no vendor driver needed. Print submission selects 4×6 media +
  rear tray; exact option names discovered during Phase 1 calibration.
- **Host:** Elaine has a **macOS laptop and a Windows laptop; the macOS laptop
  hosts v1** — CUPS and launchd are built in, so the WiFi printer and service
  wrapper work with zero extra infrastructure (Windows would need Docker/WSL
  for the same). Planned migration to a future home server and possibly cloud
  hosting: the design must be portable (self-contained data directory,
  containerizable), and printing must be separable from the "brain" so a cloud
  deployment can still reach the home printer via a small LAN print relay.
- **Stack:** Python + FastAPI, per SPEC.

## 7. Assumptions

- A. Home network is trusted; the web app needs no login in v1 (a PIN gate is a
  cheap add; real auth becomes required only if/when cloud-hosted).
- B. Volume is low — a few labels per day at most.
- C. While hosted on the laptop, "always on" is not guaranteed: the agent
  catches up whenever the laptop is awake; nothing is lost while it sleeps.
- D. Defaults unless Brian objects: **pause = keep ingesting & queueing, stop
  printing**; **auto-print ON** by default with a UI toggle.

## 8. Open items (none block Phase 1)

1. **App password setup** — confirm 2-Step Verification is on for
   `elaineamy.g2010@gmail.com` and generate a Google app password for the
   agent (setup-time task with Elaine, not a design question).
2. **CUPS option names for the TS9521Ta** — media/tray/borderless values come
   from `lpoptions -l` against the real printer during Phase 1 calibration.
3. **Duplicates** — platforms occasionally re-send labels (upgrades,
   re-issues). Default plan: dedupe on tracking number, surface re-sends as
   "duplicate — print anyway?" in the UI.

## 9. Risks

- **Email format drift** — Poshmark/Vinted change templates. Mitigation: LLM
  classifier is tolerant of wording changes; detection keyed on sender domain +
  PDF attachment presence, not brittle text matching.
- **Crop failure on unseen layouts** — Mitigation: deterministic crop first,
  cheap vision-model sanity check after; on low confidence, don't print — queue
  the original with an error for manual review in the UI.
- **Printer offline** — Mitigation: retry with backoff, mark job "waiting for
  printer", surface in UI; never silently drop a label.
- **Credential safety** — Gmail token and Anthropic API key stored on the host.
  Mitigation: file permissions, LAN-only service, secrets never in the repo.
- **Wrong-label incidents** — printing is cheap and labels are prepaid, so the
  cost of a false positive is one wasted sheet; bias toward printing with clear
  audit trail rather than blocking.

## 10. Success criteria

- A new Poshmark or Vinted sale results in a correctly sized label coming off
  the printer within ~5 minutes, with zero taps (auto mode).
- Elaine can pause, resume, and reprint from her phone in under 10 seconds.
- The dashboard answers "did anything print today / this week, and did anything
  fail?" at a glance.
- Running cost is negligible (pennies per month of LLM usage).

## 11. Phases

- **Phase 1 — Pipeline core:** parse the two example emails end-to-end offline;
  crop/normalize both labels; print via CUPS. CLI only.
- **Phase 2 — Gmail ingestion:** OAuth/IMAP connection, polling loop,
  classification, dedupe, state in SQLite.
- **Phase 3 — Web app:** pause/resume, queue, manual run, previews, reprint,
  daily metrics + history, error views. Mobile-friendly.
- **Phase 4 — Hardening:** printer-offline handling, retries, service install
  (systemd), backup/restore of DB, docs for Elaine.
