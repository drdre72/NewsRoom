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

## Path B — Upload API for Shorts + long-form (built, gated)

Rendered MP4s + manifests are already on disk (`output/shorts`,
`output/longform`, `output/manifests` — manifests carry titles,
`youtube_chapters`, fact IDs, and gate verdicts). The publish module
is built (`src/newsroom/publish/youtube.py`): OAuth device flow,
resumable upload, title/description/chapters from manifest, and a
"Best of the Day" playlist — all via raw HTTP, no Google client library.

### Setup (one-time)

1. Google Cloud Console → enable YouTube Data API v3 → create OAuth
   client ID (Desktop app) → put in `.env`:
   ```
   NEWSROOM_YT_CLIENT_ID=xxx.apps.googleusercontent.com
   NEWSROOM_YT_CLIENT_SECRET=xxx
   ```
2. Run `python main.py youtube auth` → visit the URL, enter the code,
   copy the refresh token to `.env`:
   ```
   NEWSROOM_YT_REFRESH_TOKEN=xxx
   ```

### Usage

```bash
python main.py youtube upload --dry-run    # see what would upload
python main.py youtube upload              # upload all gate-passed
python main.py youtube playlist --dry-run  # see best-of-day selection
python main.py youtube playlist            # create the playlist
```

### What uploads

Only assets where `publication_gate.verdict == "pass"` in the manifest.
Shorts (vertical 1080x1920) get `#Shorts` in the description;
long-form gets YouTube chapters from the manifest. Titles strip the
outlet prefix; descriptions carry source attribution + event ID +
revision + chapters + a link back to the live channel.

### Quota (June 2026)

`videos.insert` has its own bucket: 100 uploads/day at 1 unit each,
independent of the 10,000-unit shared pool. No scarcity-driven curation
needed — upload everything that passes the gate.

### Best of the Day playlist

`select_best_of_day` ranks stories by `impact_score × (1 + air_count/10)`
over the last 24h, excluding replays. A FLASH that aired once beats a
NORMAL that aired 50 times. The playlist drives engagement back to
the live broadcast.

## Recommendation

Start with Path A (zero build, real live presence) and add Path B for
Shorts/long-form once the live channel has run cleanly for a week.
Both stay OFF until you say go.
