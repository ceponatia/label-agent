import argparse
import sys
from pathlib import Path

from .config import load_config
from .db import Database
from .pipeline import process_label_pdf
from .printing import make_printer
from .scheduler import build_scheduler
from .service import AgentService
from .verify import verify_print_pdf
from .web.app import create_app


def cmd_db_init(args) -> int:
    config = load_config(args.config)
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    db = Database(config.db_path)
    db.init()
    db.close()
    print(f"initialized {config.db_path}")
    return 0


def cmd_process(args) -> int:
    config = load_config(args.config)
    out_dir = Path(args.out_dir) if args.out_dir else Path(config.data_dir) / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    printer = make_printer(config) if args.print else None

    all_ok = True
    for source in (Path(p) for p in args.pdfs):
        destination = out_dir / f"{source.stem}-4x6.pdf"
        result = process_label_pdf(source, destination)
        if not result.ok:
            all_ok = False
            print(f"{source.name}: failed ({'; '.join(result.problems)})")
            continue

        verdict = verify_print_pdf(destination, config=config)
        problems = list(dict.fromkeys(result.problems + verdict.problems))
        ok = not problems and not result.needs_review
        all_ok = all_ok and ok
        print(
            f"{source.name}: {result.method} "
            f"{'ok' if ok else 'problems: ' + '; '.join(problems)} -> {destination}"
        )
        if printer is not None and ok:
            try:
                print(f"  submitted as job {printer.submit(str(destination))}")
            except Exception as exc:
                all_ok = False
                print(f"  print failed: {exc}", file=sys.stderr)

    return 0 if all_ok else 1


def cmd_serve(args) -> int:
    import uvicorn

    config = load_config(args.config)
    host = args.host or config.web_host
    port = args.port or config.web_port
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    db = Database(config.db_path)
    db.init()

    service = AgentService(db, config)
    scheduler = build_scheduler(service, config)
    scheduler.start()
    print(f"label-agent serving on http://{host}:{port}")
    try:
        uvicorn.run(create_app(db, config, service), host=host, port=port)
    finally:
        # The jobs write to this database, so let an in-flight cycle land before
        # the connection under it goes away.
        scheduler.shutdown(wait=True)
        service.close()
        db.close()
    return 0


def cmd_check_now(args) -> int:
    config = load_config(args.config)
    if not config.imap_user:
        print(
            "no mailbox configured: set LABELAGENT_IMAP_USER and "
            "LABELAGENT_IMAP_PASSWORD in .env",
            file=sys.stderr,
        )
        return 2

    db = Database(config.db_path)
    db.init()
    service = AgentService(db, config)
    try:
        summary = service.check_now()
    finally:
        service.close()
        db.close()

    for key, value in summary.items():
        print(f"{key}: {value}")
    return 1 if summary["errors"] else 0


def cmd_backfill_buyers(args) -> int:
    config = load_config(args.config)
    db = Database(config.db_path)
    db.init()
    service = AgentService(db, config)
    try:
        result = service.backfill_buyer_names(day=args.date)
    finally:
        db.close()
    print(result["detail"])
    for problem in result["problems"]:
        print(f"  {problem}")
    return 0 if not result["problems"] else 1


def cmd_test_print(args) -> int:
    config = load_config(args.config)
    db = Database(config.db_path)
    db.init()
    service = AgentService(db, config)
    try:
        result = service.test_print()
    finally:
        db.close()
    print(result["detail"])
    return 0 if result["ok"] else 1


def _add_process(sub):
    p = sub.add_parser("process", help="normalize and verify one or more label PDFs")
    p.add_argument("pdfs", nargs="+")
    p.add_argument("-o", "--out-dir", help="where to write the 4x6 PDFs")
    p.add_argument(
        "--print", action="store_true", help="also send each result to the printer"
    )
    return p


def _add_serve(sub):
    p = sub.add_parser("serve", help="run the web app and background workers")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    return p


def _add_check_now(sub):
    return sub.add_parser("check-now", help="poll Gmail once and exit")


def _add_test_print(sub):
    return sub.add_parser("test-print", help="print a 4x6 calibration page")


def _add_backfill_buyers(sub):
    p = sub.add_parser(
        "backfill-buyers",
        help="fill missing buyer names for a day's labels by re-reading "
        "their PDFs; prints nothing",
    )
    p.add_argument("--date", help="day to backfill as YYYY-MM-DD (default today)")
    return p


def _add_db_init(sub):
    return sub.add_parser("db-init", help="create the data dir and SQLite schema")


COMMANDS = {
    "process": (_add_process, cmd_process),
    "serve": (_add_serve, cmd_serve),
    "check-now": (_add_check_now, cmd_check_now),
    "test-print": (_add_test_print, cmd_test_print),
    "backfill-buyers": (_add_backfill_buyers, cmd_backfill_buyers),
    "db-init": (_add_db_init, cmd_db_init),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="label-agent")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    for add_parser, _ in COMMANDS.values():
        add_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command][1](args)


if __name__ == "__main__":
    sys.exit(main())
