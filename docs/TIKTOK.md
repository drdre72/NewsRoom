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

## Phase B (optional): upload-to-drafts API

If manual posting gets tedious, the compliant automation ceiling is
TikTok's `video.upload` scope (MEDIA_UPLOAD post_mode): videos land in
the @justsystemnews inbox as drafts and you tap publish — it preserves
the human-review gate. Requires registering a TikTok developer app +
OAuth; `draft.json` fields already map onto the API's post_info shape.
Direct-to-public posting is not a realistic target for a self-tool.
