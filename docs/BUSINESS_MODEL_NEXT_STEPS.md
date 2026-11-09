# Rights-cleared channel rollout

## Now: protect the commercial boundary

- Google News RSS polling is disabled. Historical observations, facts,
  revisions and media remain in the ledger, but Google-sourced events
  are excluded from the rundown, interrupts, replays, queued renders,
  YouTube upload eligibility and Best-of-the-Day selection.
- DOJ's documented press-release API is the first direct crime source.
  It uses official release titles, dates and source links, not embedded
  article media. Releases older than 48 hours are ignored. DOJ says its
  website content is generally public domain unless otherwise marked;
  verify any exceptions and keep criminal allegations attributed.
  API: https://www.justice.gov/developer/api-documentation/api_v1
  Rights: https://www.justice.gov/legalpolicies
- BBC and other publisher feed terms still require individual review
  before automated monetized uploads. A linked RSS feed alone does not
  grant a license to reproduce an article or its images.

## Next: make the channel worth watching

1. Press fallback now uses a short, complete, attributed feed-summary
   sentence when it adds distinct ledger-backed detail; otherwise it
   stops at the attributed headline rather than padding with a date and
   generic close. This is not independent verification or a full
   explainer: pursue structured official-source detail and a reviewed
   explanatory format before scaling. Review quotation/reuse rights per
   publisher; no automatic publication of unreviewed crime allegations
   or graphic material.
2. The core now records obsolete queued renders as `cancelled` (with a
   reason, without deleting jobs or facts): superseded revisions, restricted
   sources and ticker-only weather. New ordinary weather revisions no longer
   enqueue video renders; FLASH/BREAKING weather still does. Within each
   priority band, newer jobs run first. Monitor whether current eligible
   stories actually premiere rather than relying on replay volume, and
   check the weather/restricted-source mix daily.
3. Weather lower third now scrolls: the Director generates a transparent
   ticker strip (PIL, same palette/font) per program and the playout overlays
   it with a looping x-offset expression (`playout.ticker_speed`, 110 px/s).
   Stories, plain pillarbox and standby slate all carry it. Verified via
   pixel-diff across encoded frames. Urgent weather interrupts unchanged.
4. Add one rights-reviewed original Short per day, one deeper weekly
   explainer and a YouTube Studio Related Video link from each Short to
   the live stream (Shorts description URLs are not clickable).
5. Track 7- and 28-day Shorts retention, related-video clicks, public
   live watch hours, returning viewers, and corrections before increasing
   volume. Review publisher rights and YouTube ad suitability separately.

Do not confuse the API project's 100 `videos.insert` calls/day with a
channel upload allowance or a viable publishing cadence. Uploads remain
off by default; the previously approved BBC video is the only recorded
YouTube upload.
