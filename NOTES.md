# Ops notes — scheduling & delivery reliability

Written 2026-07-03 after investigating missing WhatsApp notifications.

## Findings (2026-07-03)

1. **CallMeBot quota exhausted (main reason no messages arrived).**
   On 2026-07-02 CallMeBot answered every send with HTTP 200 but the body said
   *"You have **0** messages left. I need your support..."* — messages were
   accepted but not delivered. The day before it warned *"Up to 16 messages per
   240 minutes"* (HTTP 210). The old code only logged the HTTP status, so every
   run stayed green. `gmail_digest.py` now validates the response body and the
   workflow fails loudly when CallMeBot does not confirm queuing.
   → **Manual action**: check remaining quota / supporter status at callmebot.com,
   and if sends keep failing, re-send `I allow callmebot to send me messages`
   to the CallMeBot WhatsApp bot (+34 644 59 78 23) to refresh the API key.

2. **GitHub's native `schedule` fires 3-4h late** on this repo (e.g. cron
   `0 5 * * *` firing at 08:13, 08:50, 08:32 UTC). This is GitHub's documented
   best-effort behavior for scheduled workflows and cannot be fixed in-repo.

3. **An external cron-job.org job is still active** and dispatches this
   workflow at exactly 05:00 UTC daily (runs show `workflow_dispatch` at
   05:00:47, actor = florianingenhousz). That is the *reliable, on-time*
   trigger — keep it. The native cron is now a fallback at 05:30 UTC that
   skips itself if a successful run already happened today (prevents the
   double daily send that was burning CallMeBot quota).

## External scheduler setup (primary trigger)

The workflow should be driven by cron-job.org (or any external cron) at the
exact desired time. Current job appears active; to (re)create it:

- **URL**: `https://api.github.com/repos/florianingenhousz/gmail-digest/actions/workflows/digest.yml/dispatches`
- **Method**: `POST`
- **Headers**:
  - `Authorization: Bearer <PAT>` — a GitHub personal access token with `repo` + `workflow` scope
  - `Accept: application/vnd.github+json`
- **Body**: `{"ref":"master"}`
- **Schedule**: 05:00 UTC daily (= 07:00 Paris in summer/CEST; note this shifts
  to 06:00 Paris in winter unless you adjust the external job).

A successful dispatch returns HTTP 204 with an empty body.

## Layers of defense

| Layer | Time (UTC) | Purpose |
|---|---|---|
| cron-job.org → workflow_dispatch | 05:00 | primary, on time |
| native `schedule` cron | 05:30 (best effort, often hours late) | fallback, self-skips if a run already succeeded today |
| `keepalive.yml` (weekly) | Sun 04:00 | prevents GitHub auto-disabling the schedule after 60 days without pushes |
