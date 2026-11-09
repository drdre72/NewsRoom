# YouTube go-live paths

Two independent ways to put the newsroom on YouTube. They can coexist.

## Path A — 24/7 live stream via RTMP relay (built, gated)

The Broadcast Director already encodes H.264/AAC into a continuously
updated `output/preview/live.m3u8`. A dedicated agent —
`python main.py rtmp-relay` (`com.newsroom.rtmp`) — runs **one** persistent
ffmpeg that follows that live playlist and remuxes it to YouTube with
`-c copy` (no re-encode). Do NOT tee RTMP off the per-program encodes:
each program's ffmpeg exits at its boundary, and the connect/disconnect
churn makes YouTube ingest flag "not receiving enough video". The relay
keeps a single TCP session across program joins and Director restarts,
and pauses itself when the HLS window goes stale instead of re-airing
the tail.

1. YouTube Studio → Go Live → copy the **stream key** for a persistent
   ("reuseable") stream. New channels must wait 24 h after enabling live.
2. Put the full URL in the environment, never in config/git:

   ```bash
   echo 'NEWSROOM_RTMP_URL=rtmp://a.rtmp.youtube.com/live2/<key>' >> .env
   ```
3. Flip `outputs.rtmp.enabled: true` in `config/broadcast.yaml` and
   `publishing.automatic: true` in `config/settings.yaml`.
4. `python main.py services install` — loads `com.newsroom.rtmp` too.

What airs: whatever the Director airs — stories, replays, standby slates.
The stream is continuous; YouTube sees one long broadcast.

Caveats: you can't title/tag individual segments, no chapters mid-stream,
YouTube may throttle/re-encode; VOD archives accumulate automatically
(up to the archive limit) but aren't curated.

## Path B — Upload API for Shorts + long-form (needs building)

Rendered MP4s + manifests are already on disk (`output/shorts`,
`output/longform`, `output/manifests` — manifests carry titles,
`youtube_chapters`, fact IDs, and gate verdicts).

Build required (~1-2 sessions):
- Google Cloud project → enable YouTube Data API v3 → OAuth consent
  screen (external, testing) → client_secret.json (stays out of git).
- One-time OAuth device/console flow → refresh token in `.env`
  (`NEWSROOM_YT_REFRESH_TOKEN`); access tokens minted per upload.
- `src/newsroom/publish/youtube.py`: resumable upload, title/description
  from manifest, `#Shorts` tagging for verticals, chapters for long-form.
- Upload policy in config: min impact level, per-day cap
  (default quota is 10,000 units/day; one upload costs ~1,600 → ~6/day),
  dry-run mode that emits the request without sending.
- Gate: only `publication_gate.verdict == "pass"` assets upload;
  `publishing.automatic` still required as the master switch.

Caveats: quota-limited to ~6 uploads/day on a free project — pick the
best stories, not everything. OAuth consent screen in "testing" mode
requires re-auth weekly unless the app is verified.

## Recommendation

Start with Path A (zero build, real live presence) and add Path B for
Shorts/long-form once the live channel has run cleanly for a week.
Both stay OFF until you say go.
