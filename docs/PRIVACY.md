# Privacy Policy — NewsRoom

NewsRoom is a local-first, owner-operated application
(source: https://github.com/drdre72/NewsRoom). It does not operate a public
service and does not collect data from third-party users.

- **Credentials** (TikTok client key/secret, OAuth refresh tokens) are
  stored only in the operator's local `.env` file (mode 0600) and are never
  transmitted anywhere except TikTok's official API endpoints
  (open.tiktokapis.com, www.tiktok.com).
- **Scopes**: the app requests `user.info.basic` (to identify the
  authorizing account) and `video.upload` (to deliver rendered videos to
  the operator's own TikTok inbox as drafts). It requests no other data.
- **Data**: no TikTok user data is stored beyond the OAuth tokens and the
  `open_id`/`publish_id` values needed to operate and audit uploads.
- **Sharing**: nothing is shared with or sold to any third party.
- **Retention**: upload journals and render metadata live under the
  operator's local `output/` directory under their own retention policy.

Contact: via the repository's issue tracker.
