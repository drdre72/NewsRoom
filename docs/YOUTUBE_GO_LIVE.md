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

## Path B — Upload API for Shorts + long-form (preview only)

Rendered MP4s + manifests are already on disk (`output/shorts`,
`output/longform`, `output/manifests` — manifests carry titles,
`youtube_chapters`, fact IDs, and gate verdicts). The publish module
contains desktop loopback OAuth and resumable upload. Uploads are
separately disabled pending an operator-approved publication review;
the "Best of the Day" command currently previews selection only.

### Setup (one-time)

1. Google Cloud Console → enable YouTube Data API v3 → create OAuth
   client ID (Desktop app) → put in `.env`:
   ```
   NEWSROOM_YT_CLIENT_ID=xxx.apps.googleusercontent.com
   NEWSROOM_YT_CLIENT_SECRET=xxx
   ```
2. Run `python main.py youtube auth` on the Mac, open the short
   `http://127.0.0.1:<port>/start` URL it prints in a browser on that
   same Mac, and approve the requested YouTube scopes. Leave the command
   running until the browser redirects back to localhost.
   The localhost callback stores the refresh token privately in `.env`;
   it is never printed. Desktop clients cannot use Google's device-code
   flow.

### Usage

```bash
python main.py youtube upload --max-per-day 3  # read-only preview
python main.py youtube upload --asset asset_87e9c5628cdb  # reviewed BBC metadata
python main.py youtube playlist                # read-only selection
```

`youtube upload --execute` requires both `publishing.automatic` and
`publishing.youtube.uploads_enabled` (default **false**). Do not enable
uploads until the channel, metadata, and a test upload are reviewed.
Live uploads require `--asset <asset_id>` and matching reviewed metadata
in `docs/YOUTUBE_CANDIDATE_DRAFT.json`; the first test is private-only.
The draft is bound to the current revision, video SHA-256 and linked
source fact. Its description is used verbatim instead of the generic
placeholder channel URL. Do not use `--execute` until the operator
explicitly authorizes the specific upload.
Playlist publishing is blocked until it can use verified uploaded video IDs.
The first reviewed BBC Short was uploaded as a **private** test
(`JmapP03nQa4`); YouTube confirmed it processed and remains private.
The upload switch is off again. This does not authorize public publishing.
The upload client saves resumable sessions in the private
`output/youtube_sessions/` directory (0700, journal files 0600). If a
connection drops, rerunning the command queries YouTube for the committed
byte range or completed video ID instead of starting another upload.
An expired or unqueryable session **fails closed**: check the channel
manually before attempting a new session. Do not remove the journal.

### Upload selection

Only current-revision, non-fixture, non-weather assets where
`publication_gate.verdict == "pass"` in the manifest and the video hash
matches the file. The CLI cap and remaining daily quota both apply;
an event already uploaded is skipped.
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
over the last 24h, excluding replays. This is a preview only until
upload records can link selected stories to verified YouTube video IDs.

## Recommendation

Start with Path A (zero build, real live presence) and add Path B for
Shorts/long-form once the live channel has run cleanly for a week.
The live RTMP path is already on; upload publishing remains off.
