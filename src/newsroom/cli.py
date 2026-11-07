"""newsroom CLI — entry point for main.py and the `newsroom` script.

    python main.py doctor
    python main.py status
    python main.py simulate <scenario> [--dry-run]
    python main.py run [--once|--poll]
    python main.py render <event_id>
    python main.py events [--type earthquake]
    python main.py story <event_id>
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .logging import configure as configure_logging, get_logger, log_event

log = get_logger("cli")


def _conn_and_settings(args):
    from .config import load_settings
    from .paths import ensure_runtime_dirs
    from .storage.database import connect, integrity_check, migrate, recover_jobs
    settings = load_settings()
    ensure_runtime_dirs()
    conn = connect()
    migrate(conn)
    recovered = recover_jobs(conn)
    if recovered:
        log_event(log, 30, "requeued jobs after unclean shutdown",
              count=recovered)
    return conn, settings


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    return run_doctor(json_out=args.json)


def cmd_status(args) -> int:
    from .intelligence.freshness import feed_status
    from .storage.database import integrity_check
    from .storage import repositories as repo
    conn, settings = _conn_and_settings(args)
    status = {
        "db": integrity_check(conn),
        "jobs": repo.queue_depth(conn),
        "feeds": [feed_status(conn, s) for s in ("usgs", "nws", "eonet", "gdacs")],
        "events": [
            {"event_id": e.event_id, "type": e.event_type,
             "revision": e.current_revision, "impact": e.impact_level,
             "impact_score": e.impact_score,
             "confidence": e.confidence_score, "updated": e.last_updated}
            for e in repo.list_events(conn, limit=10)
        ],
    }
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        db = status["db"]
        print(f"db: integrity={db['integrity']} wal={db['wal']} "
              f"events={db['count_events']} facts={db['count_facts']} "
              f"obs={db['count_observations']}")
        print(f"jobs: {status['jobs']}")
        for f in status["feeds"]:
            age = f["age_seconds"]
            age_s = "never" if age is None else f"{int(age)}s ago"
            print(f"feed {f['source']:<6} {f['status']:<8} last={age_s}")
        for e in status["events"]:
            print(f"{e['event_id']:<32} r{e['revision']:<3} "
                  f"{e['impact']:<10} impact={e['impact_score']:<5} "
                  f"conf={e['confidence']:<5} {e['updated']}")
    return 0


def cmd_simulate(args) -> int:
    from .sim.runner import list_scenarios, run_scenario
    conn, settings = _conn_and_settings(args)
    if args.name == "list":
        for name, spec in list_scenarios().items():
            print(f"  {name:<34} {spec.get('description','')}")
        return 0
    result = run_scenario(conn, settings, args.name, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_run(args) -> int:
    from .ingestion.polling import poll_once
    from .pipeline import ingest_observation
    conn, settings = _conn_and_settings(args)
    interval = int(settings.get("polling.interval_seconds", 60))
    once = args.once or not args.poll
    while True:
        report = poll_once(conn, settings, ingest_observation)
        if args.json or once:
            print(json.dumps(report, indent=2))
        if once:
            return 0 if not report["errors"] else 1
        log_event(log, 20, "sleeping", seconds=interval)
        time.sleep(interval)


def cmd_render(args) -> int:
    from .render import render_short
    conn, settings = _conn_and_settings(args)
    manifest = render_short(conn, settings, args.event_id, dry_run=args.dry_run)
    print(json.dumps(manifest, indent=2))
    return 0 if manifest["publication_gate"]["verdict"] == "pass" else 1


def cmd_events(args) -> int:
    from .storage import repositories as repo
    conn, _ = _conn_and_settings(args)
    for e in repo.list_events(conn, event_type=args.type, limit=args.limit):
        print(f"{e.event_id:<34} r{e.current_revision:<3} {e.impact_level:<10}"
              f" conf={e.confidence_score:<5} {(e.title or '')[:60]}")
    return 0


def cmd_story(args) -> int:
    from .story.newsroom_memory import revision_timeline, what_changed
    conn, _ = _conn_and_settings(args)
    out = {"event_id": args.event_id,
           "changed": what_changed(conn, args.event_id),
           "timeline": revision_timeline(conn, args.event_id)}
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newsroom")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--dry-run", action="store_true",
                        help="no external publishing/streaming (§52)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor")
    p_sim = sub.add_parser("simulate")
    p_sim.add_argument("name", help="scenario name or 'list'")
    p_run = sub.add_parser("run")
    p_run.add_argument("--once", action="store_true")
    p_run.add_argument("--poll", action="store_true")
    p_ren = sub.add_parser("render")
    p_ren.add_argument("event_id")
    p_ev = sub.add_parser("events")
    p_ev.add_argument("--type", default=None)
    p_ev.add_argument("--limit", type=int, default=20)
    p_st = sub.add_parser("story")
    p_st.add_argument("event_id")
    sub.add_parser("status")

    args = parser.parse_args(argv)
    configure_logging()
    return {
        "doctor": cmd_doctor, "status": cmd_status, "simulate": cmd_simulate,
        "run": cmd_run, "render": cmd_render, "events": cmd_events,
        "story": cmd_story,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
