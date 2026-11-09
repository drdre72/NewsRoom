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


def _conn_and_settings(args, recover: bool = False):
    from .config import load_settings
    from .paths import ensure_runtime_dirs
    from .storage.database import connect, integrity_check, migrate, recover_jobs
    settings = load_settings()
    ensure_runtime_dirs()
    conn = connect()
    migrate(conn)
    recovered = recover_jobs(conn) if recover else 0
    if recovered:
        log_event(log, 30, "requeued jobs after unclean shutdown",
              count=recovered)
    return conn, settings


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    return run_doctor(json_out=args.json)


def cmd_status(args) -> int:
    from .intelligence.source_health import source_statuses
    from .storage.retention import disk_health, retention_audit
    from .storage.database import integrity_check
    from .storage import repositories as repo
    conn, settings = _conn_and_settings(args)
    status = {
        "db": integrity_check(conn),
        "jobs": repo.queue_depth(conn),
        "feeds": source_statuses(settings),
        "disk": disk_health(settings),
        "retention": retention_audit(settings),
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
        print(f"disk: free={status['disk']['free_gb']} GiB "
              f"min={status['disk']['min_free_gb']} GiB")
        for f in status["feeds"]:
            age = f["age_seconds"]
            age_s = "never" if age is None else f"{int(age)}s ago"
            print(f"feed {f['source']:<10} {f['status']:<12} last={age_s}")
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
    from .service import ServiceLoop
    once = args.once or not args.poll
    conn, settings = _conn_and_settings(args, recover=not once)
    if once:
        report = poll_once(conn, settings, ingest_observation)
        print(json.dumps(report, indent=2))
        return 0 if not report["errors"] else 1
    interval = int(settings.get("polling.interval_seconds", 60))
    ServiceLoop(conn, settings).run(interval)
    return 0


def cmd_render(args) -> int:
    from .render import render_short
    conn, settings = _conn_and_settings(args)
    manifest = render_short(conn, settings, args.event_id, dry_run=args.dry_run)
    print(json.dumps(manifest, indent=2))
    return 0 if manifest["publication_gate"]["verdict"] == "pass" else 1


def cmd_render_longform(args) -> int:
    from .render import render_longform
    conn, settings = _conn_and_settings(args)
    manifest = render_longform(conn, settings, args.event_id,
                               dry_run=args.dry_run)
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


def cmd_broadcast(args) -> int:
    """Run the Broadcast Director — local HLS preview playout (§31-§35)."""
    from .broadcast.director import BroadcastDirector
    conn, settings = _conn_and_settings(args)
    BroadcastDirector(conn, settings).run()
    return 0


def cmd_rtmp_relay(args) -> int:
    """Persistent YouTube RTMP push: one ffmpeg follows live.m3u8 and
    remuxes it out — the connection survives program boundaries (§34)."""
    from .broadcast.rtmp_relay import run
    _, settings = _conn_and_settings(args)
    return run(settings)


def cmd_youtube(args) -> int:
    """YouTube upload API: auth, upload Shorts/long-form, best-of-day playlist."""
    from .publish.youtube import (
        authorize_desktop, save_refresh_token, refresh_access_token,
        upload_video, reviewed_metadata, select_best_of_day, upload_eligible,
    )
    from .storage import repositories as repo
    conn, settings = _conn_and_settings(args)

    if args.action == "auth":
        from pathlib import Path
        env_file = Path(__file__).resolve().parents[2] / ".env"
        try:
            token = authorize_desktop()
            save_refresh_token(token, env_file)
        except (RuntimeError, TimeoutError, OSError) as exc:
            print(f"Authorization failed: {exc}")
            return 1
        print("Authorized; refresh token saved privately to .env.")
        return 0

    if args.action == "upload":
        if args.execute and (not args.asset or args.dry_run):
            print("Use --execute --asset <asset_id> for one reviewed upload; "
                  "do not combine --execute and --dry-run.")
            return 1
        eligible = upload_eligible(conn, settings,
                                   max_per_day=args.max_per_day,
                                   asset_id=args.asset)
        if not eligible:
            print("No eligible videos to upload.")
            return 1 if args.asset else 0
        if args.execute and len(eligible) != 1:
            print("Expected exactly one reviewed asset; no upload attempted.")
            return 1
        reviewed = None
        if args.asset:
            from pathlib import Path
            draft_path = (Path(__file__).resolve().parents[2] /
                          "docs/YOUTUBE_CANDIDATE_DRAFT.json")
            try:
                reviewed = reviewed_metadata(conn, eligible[0], draft_path)
            except (ValueError, KeyError, OSError) as exc:
                print(f"Draft validation failed: {exc}")
                return 1
        print(f"Found {len(eligible)} eligible videos.")
        if not args.execute:
            if reviewed:
                print(f"  Privacy: {reviewed['privacy']}")
                print(f"  Title: {reviewed['title']}")
                print(f"  Tags: {', '.join(reviewed['tags'])}")
                print(f"  Description:\n{reviewed['description']}")
                print(f"  Video: {eligible[0]['path']}")
                return 0
            for e in eligible:
                ev = repo.get_event(conn, e["event_id"])
                print(f"  [dry-run] {e['event_id']} r{e['revision']} "
                      f"-> '{ev.title[:60] if ev else ''}'")
            return 0
        if not (settings.get("publishing.automatic", False) and
                settings.get("publishing.youtube.uploads_enabled", False)):
            print("YouTube uploads are disabled; review the preview and "
                  "enable publishing.youtube.uploads_enabled explicitly.")
            return 1
        token = refresh_access_token()
        uploaded = 0
        for e in eligible:
            ev = repo.get_event(conn, e["event_id"])
            result = upload_video(e["manifest"], ev, token,
                                 asset_id=e["asset_id"], reviewed=reviewed)
            if result.get("ok") and result.get("video_id"):
                repo.patch_asset_meta(conn, e["asset_id"], {
                    "youtube_id": result["video_id"],
                    "youtube_uploaded_at": repo.utcnow(),
                })
                uploaded += 1
                print(f"  uploaded: {result.get('video_id', '?')} "
                      f"'{result.get('title', '')[:60]}'")
            else:
                print(f"  failed: {e['event_id']} - {result.get('error','')}")
        print(f"Uploaded {uploaded}/{len(eligible)} videos.")
        return 0 if uploaded == len(eligible) else 1

    if args.action == "playlist":
        best = select_best_of_day(conn, settings, top_n=args.top_n)
        if not best:
            print("No stories aired in the last 24h.")
            return 0
        print(f"Best of the Day ({len(best)} stories):")
        for i, s in enumerate(best, 1):
            print(f"  {i}. [{s['impact_level']}] {s['title'][:60]} "
                  f"(aired {s['air_count']}x, score {s['impact_score']:.2f})")
        if not args.execute:
            return 0
        print("Playlist publishing is not implemented yet: selection has no "
              "verified uploaded video IDs. No playlist was created.")
        return 1

    return 1


def cmd_preview(args) -> int:
    """Serve the local broadcast preview over HTTP (§34 dry-run surface)."""
    from .broadcast.preview import serve
    from .paths import resolve
    conn, settings = _conn_and_settings(args)
    hls_dir = resolve(settings.get("playout.hls.dir",
                                   "output/preview"))
    hls_dir.mkdir(parents=True, exist_ok=True)
    port = int(args.port or settings.get("playout.port", 8788))
    print(f"preview → http://localhost:{port}/  (serving {hls_dir})")
    serve(conn, settings, hls_dir, port)
    return 0


def cmd_retention(args) -> int:
    """Eligibility ledger: which videos could convert to recipes, which
    are protected, and why — report-only, never deletes."""
    from .storage.recipes import retention_report
    conn, settings = _conn_and_settings(args)
    report = retention_report(conn, settings)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"retention: {report['effective_retention_days']}d window "
              f"(disk free {report['disk']['free_gb']} GiB)")
        for status, n in sorted(report["counts"].items()):
            print(f"  {status:<14} {n}")
        print(f"eligible: {len(report['eligible'])} assets, "
              f"{report['eligible_bytes'] / 1e6:.0f} MB")
        for e in report["eligible"][:20]:
            print(f"  {e['asset_id']:<18} {e['event_id'][:34]:<34} "
                  f"{e['reason']}")
        print(f"recipes: {report['recipes']}")
        if not args.execute:
            print("plan only — run `dehydrate --execute` to apply")
    return 0


def cmd_dehydrate(args) -> int:
    """Convert eligible videos to verified rehydration recipes.
    Without --execute this prints the plan and changes nothing."""
    from .storage.recipes import dehydrate
    conn, settings = _conn_and_settings(args)
    result = dehydrate(conn, settings, execute=args.execute,
                       verify=not args.no_verify, budget=args.budget,
                       asset_ids=args.asset)
    print(json.dumps(result, indent=2))
    return 0


def cmd_restore(args) -> int:
    """Rebuild a retired video from its recipe — the only step needed."""
    from .storage.recipes import restore
    conn, settings = _conn_and_settings(args)
    manifest = restore(conn, settings, args.ident)
    print(json.dumps(manifest, indent=2))
    return 0 if manifest["publication_gate"]["verdict"] == "pass" else 1


def cmd_archive(args) -> int:
    """Copy completed masters to iCloud Drive, sha256-verified, budgeted."""
    from .storage.archive import archive_report, run_archive
    conn, settings = _conn_and_settings(args)
    if args.execute:
        result = run_archive(conn, settings, max_gb=args.max_gb)
    else:
        result = archive_report(conn, settings)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 1


def cmd_services(args) -> int:
    from .macos.services import install, status, uninstall
    return {"install": lambda: install(print_only=args.print_only),
            "uninstall": uninstall,
            "status": status}[args.action]()


def cmd_health(args) -> int:
    """Alias for preview — the health/status surface is the same server."""
    return cmd_preview(args)


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
    p_rlf = sub.add_parser("render-long")
    p_rlf.add_argument("event_id")
    p_ev = sub.add_parser("events")
    p_ev.add_argument("--type", default=None)
    p_ev.add_argument("--limit", type=int, default=20)
    p_st = sub.add_parser("story")
    p_st.add_argument("event_id")
    sub.add_parser("status")
    sub.add_parser("broadcast")
    sub.add_parser("rtmp-relay")
    p_pv = sub.add_parser("preview")
    p_pv.add_argument("--port", type=int, default=None)
    p_hl = sub.add_parser("health")
    p_hl.add_argument("--port", type=int, default=None)
    p_sv = sub.add_parser("services")
    p_sv.add_argument("action", choices=("install", "uninstall", "status"))
    p_sv.add_argument("--print", dest="print_only", action="store_true")
    p_ret = sub.add_parser("retention")
    p_ret.add_argument("--execute", action="store_true",
                       help="accepted for symmetry; retention never deletes")
    p_dh = sub.add_parser("dehydrate")
    p_dh.add_argument("--execute", action="store_true",
                      help="apply the plan (default: report only)")
    p_dh.add_argument("--no-verify", action="store_true",
                      help="skip the restore-verification render (recipes "
                           "stay 'written'; MP4s are NOT removed)")
    p_dh.add_argument("--budget", type=int, default=None,
                      help="max assets this run")
    p_dh.add_argument("--asset", action="append", default=None,
                      help="restrict to asset_id(s); repeatable")
    p_res = sub.add_parser("restore")
    p_res.add_argument("ident", help="recipe_id, asset_id, or event_id")
    p_ar = sub.add_parser("archive")
    p_ar.add_argument("--execute", action="store_true",
                      help="perform copies (default: report only)")
    p_ar.add_argument("--max-gb", type=float, default=None)
    p_yt = sub.add_parser("youtube")
    p_yt.add_argument("action", choices=("auth", "upload", "playlist"))
    p_yt.add_argument("--dry-run", action="store_true",
                      help="show what would upload without sending")
    p_yt.add_argument("--execute", action="store_true",
                      help="upload one reviewed private video (requires --asset and uploads_enabled)")
    p_yt.add_argument("--asset", default=None,
                      help="restrict upload preview/execution to one reviewed asset ID")
    p_yt.add_argument("--max-per-day", type=int, default=100,
                      help="daily upload cap (quota is 100)")
    p_yt.add_argument("--top-n", type=int, default=10,
                      help="playlist size for 'playlist' action")

    args = parser.parse_args(argv)
    configure_logging()
    return {
        "doctor": cmd_doctor, "status": cmd_status, "simulate": cmd_simulate,
        "run": cmd_run, "render": cmd_render,
        "render-long": cmd_render_longform, "events": cmd_events,
        "story": cmd_story, "broadcast": cmd_broadcast,
        "rtmp-relay": cmd_rtmp_relay,
        "preview": cmd_preview, "health": cmd_health,
        "services": cmd_services, "retention": cmd_retention,
        "dehydrate": cmd_dehydrate, "restore": cmd_restore,
        "archive": cmd_archive,
        "youtube": cmd_youtube,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
