# Six weeks of silence — what happened, September 2026

The monitor stopped alerting and nobody noticed, because it never once reported
a failure. This is the write-up: what broke, why it stayed hidden, and what
changed so the next failure is loud.

## Timeline

| When (UTC) | What broke | Cause |
|---|---|---|
| **29 Jul 2026**, between 12:25 and 14:36 | Google reviews | Billing lapsed on Cloud project `280336349375`; every Place Details call returned `REQUEST_DENIED` |
| **1 Sep 2026**, between 10:35 and 15:15 | TripAdvisor reviews | Tripadvisor sunset the legacy Content API on 31 Aug 2026; all keys now return `403` |
| **5 Sep 2026**, 17:24 | Everything | GitHub auto-disabled the schedule after 60 days with no repository activity — exactly 60 days after the last commit (`fb15115`, 7 Jul) |

Last genuine alert email: **1 Sep 2026**. Last Google alert: **29 Jul 2026**.
By 14 Sep the monitor had been fully dead for 9 days.

## Why it stayed hidden

Both fetchers caught API errors, printed them, and returned an empty list:

```python
except Exception as e:
    print(f"error: {e}")
    return []          # indistinguishable from "no new reviews"
```

So the script logged `No new reviews found.`, exited 0, and the run went green.
Over 300 consecutive successful runs while both feeds were dead. The workflow
was doing exactly what it was told; it was never told that fetching nothing is
different from finding nothing.

The third failure was worse in kind: a disabled schedule produces **no run at
all**. There is no red X to notice, no email, nothing — just an absence. Nothing
in the system was watching for absence.

## What changed

| Fix | Commit |
|---|---|
| A refused API raises instead of returning `[]`; each platform fetched independently; any failure exits non-zero. Google's `error_message` is logged, which is what identified billing as the cause | `5c707df` |
| State saves even when a feed fails — an `if:` without a status function carries an implicit `success()`, so the cache step was being skipped and reviews would have re-alerted every run | `3fe5007` |
| Migrated TripAdvisor to Terra; secret renamed `TRIPADVISOR_API_KEY` (the missing `Y` dated back to `cb9fa91`) | `fa5aa91` |
| Schedule cut from 30 minutes to 6 hours — both APIs now meter calls | `2694a80` |
| Review history, dashboard, negative escalation, weekly digest, daily heartbeat | `a9d04c9` |
| Dashboard published on GitHub Pages | `e7c7148` |

`keepalive.yml` pushes an empty commit twice a month to reset the inactivity
timer, and re-enables the checker via the API if it is ever disabled anyway.

## The three failure modes, and what now catches each

- **An API refuses** → the run exits non-zero and goes red, so GitHub emails you.
- **The monitor goes silent** → the daily heartbeat notices no successful run in
  24 hours and emails you. This is the one that was missing.
- **The schedule is disabled for inactivity** → the keepalive commits keep the
  clock from ever reaching 60 days, and re-enable the workflow if it does.

## Postscript: the watchdog cried wolf (17 and 22 Sep 2026)

The heartbeat sent two false "monitor is silent" emails while the checker was
passing every 6 hours. It asked GitHub for the latest run with `status=success`,
and that filtered query is served from a lagging index that is not reliably
ordered — it answered with runs from 14 Aug and 19 Sep respectively. The
heartbeat now reads the unfiltered run list, which is newest-first, picks the
most recent success itself, and re-queries once before alerting.

## Lessons worth keeping

1. **An empty result is not a negative result.** Any fetcher that can fail
   silently will eventually fail silently, and a monitor that cannot fail
   loudly is not a monitor.
2. **Monitor the monitor.** Every alerting system needs something watching for
   its silence, because its own failure mode is producing no output — which
   looks exactly like nothing happening.
3. **Vendor timelines are part of your uptime.** The Tripadvisor outage was a
   published deprecation with a date on it. Nothing in the project tracked that
   date, so it arrived as an outage rather than a migration.
4. **Free tiers have preconditions.** Google's free allowance still requires an
   active billing account; the free tier does not mean no billing setup.
