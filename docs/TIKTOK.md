# TikTok post desk — @justsystemnews

The TikTok leg of the newsroom is a **manual post desk**, not an
uploader. TikTok's Content Posting API audit explicitly rejects tools
whose purpose is uploading to the developer's own account, and Direct
Post requires an audited client intended for a broad audience. So the
newsroom renders and packages; a human publishes.

```
python main.py tiktok list                     # events worth a post + status
python main.py render-tiktok <event_id>        # vertical explainer render
python main.py tiktok draft <event_id>         # build the posting package
python main.py tiktok posted <event_id> <url>  # record the URL (audit trail)

# Upload-to-drafts API (video.upload scope):
python main.py tiktok auth                     # one-time OAuth (browser)
python main.py tiktok push <event_id>          # -> TikTok inbox as a draft
python main.py tiktok status <publish_id>      # delivery status
```

`draft` writes `output/tiktok/<slug>_r<rev>/`:

| file | purpose |
|---|---|
| `video.mp4` | the rendered 1080x1920 explainer |
| `caption.txt` | paste-ready description (attribution, source domain, AI disclosure, CTA, ≤5 hashtags, no bare URLs) |
| `checklist.md` | posting steps incl. the mandatory "Disclose AI-generated content" toggle |
| `draft.json` | provenance: event/revision/asset, video hash, gate verdict, fact IDs, rewards flag |

The same rules as the YouTube path apply: publication gate must pass,
restricted sources (Google News-derived) never become drafts, stale
revisions and weather alerts are excluded, `tiktok_draft_at` /
`tiktok_url` stamp the asset ledger so nothing drafts or posts twice.

## The format: the vertical explainer

`render-tiktok` reuses the multi-chapter explainer script (`plan_longform`
+ `generate_longform_script`: lede → what we know → context → revisions →
corroboration → impact → background → what to watch → close) rendered at
1080x1920. Duration is narration-driven (~60–150s), which lands above the
60-second Creator Rewards floor. `draft.json` flags `rewards_eligible`
from the actual video duration — never padded.

## Business model — how TikTok differs from YouTube

- **No 24/7 live.** US LIVE Studio/stream-key access needs ~10k followers,
  a 30-day-old account, recent LIVE history, and (since July 2025) ~50%
  gaming content for third-party RTMP. The broadcast stays on YouTube;
  TikTok is a clips operation.
- **Monetization = Creator Rewards**, not YPP: personal (not business)
  account, 10k followers, 100k views/30d, and only videos **over 1
  minute** with 1k+ For-You-feed views earn. Shorts-style clips are reach,
  explainers are revenue.
- **AIGC labeling is mandatory.** Every video is fully synthetic (Kokoro
  voice + generated cards). The checklist makes the "Disclose AI-generated
  content" toggle a required step on every post.
- **The funnel inverts.** No clickable links in captions; discovery is
  pure FYP. The CTA is a follow, not a livestream URL. Cadence target:
  1–3 quality posts/day (`publishing.tiktok.max_drafts_per_day`).
- **Rights policy carries over.** No Google News-derived stories, claims
  attributed to their outlet, unreviewed crime allegations stay off.

## Upload-to-drafts API (built)

`tiktok push` uses TikTok's `video.upload` scope: the video lands in the
@justsystemnews inbox as a draft and you tap publish — the human-review
gate is preserved by design. Direct-to-public posting is not a realistic
target for a self-tool (TikTok's audit rejects it), so this is the
automation ceiling.

Setup (one time, in developers.tiktok.com):
1. App with the **Content Posting API** product and **`video.upload`** scope.
2. Redirect URI registered exactly: `http://127.0.0.1:8789/callback`.
3. If the app is unaudited, add the @justsystemnews account under
   **Target users / test users** in the portal — unaudited apps can only
   authorize listed accounts.
4. `.env`: `NEWSROOM_TIKTOK_CLIENT_KEY` + `NEWSROOM_TIKTOK_CLIENT_SECRET`.
5. `python main.py tiktok auth`, open the printed `127.0.0.1:8789/start`
   link, approve. Refresh token saves privately to `.env`.

Then `tiktok push <event_id>` sends the render to the inbox.
`tiktok posted` still records the final URL for the audit trail.
Access tokens last 24h and are refreshed automatically (the rotating
refresh token is re-saved to `.env` on each use). `push` is journaled
per-asset — a crash mid-upload reconciles by publish_id status instead
of re-sending; worst case is a duplicate inbox draft, never a public
post.
