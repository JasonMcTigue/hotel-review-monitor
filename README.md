# Hotel Review Monitor

Automatically monitors Google and TripAdvisor reviews for **** and sends email alerts when new reviews are posted.

**Dashboard: https://jasonmctigue.github.io/hotel-review-monitor/**

## How it works

- Runs every 6 hours via GitHub Actions
- Checks Google Places and Tripadvisor Terra for new reviews
- Emails an HTML alert with review details; 1–2 star reviews get their own escalated email
- Records every review it sees in `reviews.json` and rebuilds a dashboard from it
- Emails a digest each Monday and shouts if the monitor itself goes quiet

| Workflow | Schedule | Does |
|---|---|---|
| `check-reviews.yml` | every 6 hours | fetch reviews, alert, update history + dashboard |
| `maintenance.yml` | daily 08:00 UTC | heartbeat check; weekly digest on Mondays |
| `keepalive.yml` | 1st & 21st | keep the schedules from being disabled for inactivity |

## Setup

### 1. GitHub Secrets

Add the following secrets to your repository (**Settings → Secrets and variables → Actions**):

| Secret | Description |
|---|---|
| `GOOGLE_PLACES_API_KEY` | Google Places key — needs both the legacy Places API and Places API (New) |
| `TRIPADVISOR_API_KEY` | Tripadvisor **Terra** API key, from [tripadvisor.com/developers](https://www.tripadvisor.com/developers) |
| `YAHOO_APP_PASSWORD` | Yahoo Mail app password |
| `DASHBOARD_PASSWORD` | Passphrase that unlocks the published dashboard |

### 2. Google Cloud Console

Enable **both** of these for your project at [console.cloud.google.com](https://console.cloud.google.com):

- **Places API (New)** — used to look up the hotel's place ID
- **Places API** (legacy) — used to fetch reviews, because only the legacy Place Details endpoint supports `reviews_sort=newest`

If the legacy API is disabled, review fetching fails with `REQUEST_DENIED` and the run logs Google's `error_message` explaining why.

### 3. First run

On first run the script records all existing reviews without sending an email. From the second run onwards it will only alert on new reviews.

## Manual test

Go to **Actions → Check Hotel Reviews → Run workflow** and enable the **"Send a test email"** toggle. This sends the latest review from each platform to confirm everything is working.

## API call budget

Both feeds are metered, which is why the schedule is every 6 hours (4 runs/day, ~120 calls per platform per month) rather than every 30 minutes. Reviews are only alerted on within `MAX_AGE_DAYS` (7) of publication, so a 6-hour gap between runs has no chance of missing one.

| | Rate | Free allowance |
|---|---|---|
| Google Place Details (Enterprise + Atmosphere — the `reviews` field) | $25 / 1,000 | 1,000 per month |
| Tripadvisor Terra | shown at signup | set by your plan |

Google needs active billing on the Cloud project even to use its free tier — if billing lapses, every call returns `REQUEST_DENIED`. Worth setting a daily quota cap on the Places API and a budget alert.

## Tripadvisor: Terra, not the Content API

The legacy Tripadvisor Content API was sunset on **31 August 2026** and now returns `403` to every key, regardless of account standing. This monitor uses its replacement, [Terra](https://docs.terra.tripadvisor.com):

- `GET https://terra.tripadvisor.com/api/locations/{id}/reviews`, authenticated with an `X-API-Key` header
- `sort_by=MOST_RECENT` gives true date ordering, which the legacy endpoint never supported
- `title` and `text` come back as arrays of translations; the entry flagged `primary` is the original language
- Terra's display requirements mean review alerts must show Tripadvisor's own bubble rating image, the review date, a link back to the review, and a credit line — all of which the email template does

## When a feed breaks

See [INCIDENT.md](INCIDENT.md) for the September 2026 write-up: three stacked failures over six weeks, none of which produced a single failed run.

If either API refuses a request, that platform's error is printed and the run **fails** (exit 1), so GitHub emails you about the failed workflow. The other platform is still checked and can still alert — one dead feed doesn't take the other down.

This matters: previously a refused API returned an empty list, which was indistinguishable from "no new reviews", so the run went green. Google alerts were dead for six weeks before anyone noticed.

## Keepalive

GitHub disables scheduled workflows after 60 days with no repository activity — this is what stopped the monitor in September 2026. `keepalive.yml` pushes an empty commit on the 1st and 21st of each month to reset that timer, and re-enables `check-reviews.yml` via the API if it was disabled anyway.

## Review history

`reviews.json` is the single source of truth — "have we seen this review" is derived from it, so there is no separate state file to drift out of sync. It lives in git rather than the Actions cache because caches are evicted after 7 days unused, which would silently destroy the history the dashboard and digest are built on.

History builds **forward from the first run**. Neither platform can be backfilled: Google returns only its 5 newest reviews, and Tripadvisor's Discover tier returns only 3 per location no matter what `size` you ask for.

## Dashboard

`dashboard.py` renders `docs/index.html` from `reviews.json` on every run — no API calls. It shows average rating, 30-day volume, rating mix, reviews per month by platform, and the latest reviews with 1–2 star ones striped for attention.

```
python dashboard.py                      # plain page, for local viewing
python dashboard.py --encrypt            # passphrase-gated, what CI publishes
python dashboard.py --artifact page.html # no document wrapper
```

### Why it's encrypted

GitHub Pages can't restrict access on a personal account — that needs Enterprise Cloud, and a Pages site built from a *private* repo is still public. So the protection lives inside the file: the page is encrypted with AES-GCM under a PBKDF2-SHA256 key (600,000 iterations), and the published file contains only ciphertext plus an unlock form. The passphrase is the `DASHBOARD_PASSWORD` secret; `--encrypt` refuses to run without it rather than publishing in the clear.

What this does and doesn't buy you:

- The lock screen names neither the hotel nor anyone else, and carries `noindex` — that applies to its script too, so the `localStorage` key is deliberately generic
- The logo spells out the hotel's name, so it is inlined into the stylesheet from `assets/logo-mask.png` rather than shipped to `docs/` as its own file, where it would sit in the clear beside the lock screen
- The ciphertext is world-downloadable, so security rests entirely on passphrase strength — it's a strong random passphrase, and the iteration count makes offline guessing expensive
- **This repo is public, so `reviews.json` already publishes every review in the clear.** The gate protects the rendered dashboard, not the underlying data — making the repo private is what would close that gap
- To rotate: update the secret and re-run the workflow. GitHub never reads a secret back, so keep a copy somewhere you can recover it
- Encryption uses a fresh salt and IV each build, so the page is only rewritten when what it contains actually changes (data, markup, stylesheet, logo or gate — tracked in `docs/.content-hash`) — otherwise every run would commit a new file
- A rotation changes none of that, so the build also checks whether the published page still opens with the current passphrase and re-encrypts when it doesn't. Without that, rotating would look like it worked while leaving the page sealed under the old key

## Weekly digest and heartbeat

- **Digest** (Mondays): counts, averages for the week and last 30 days, anything rated 3 or below, and the week's reviews. Built from stored history, so it costs nothing.
- **Heartbeat** (daily): checks when `check-reviews.yml` last succeeded and emails if that was over 24 hours ago. This catches *silence* — a schedule disabled for inactivity produces no failed run to notice, which is exactly how six weeks of missing Google alerts went unspotted.

## Email format

- One card per new review showing author, star rating, title, and text
- Green left border for positive reviews (3-5 stars)
- Red left border for negative reviews (1-2 stars)
- 1–2 star reviews are split into their own **⚠️ Negative review** email so they are never buried in a batch of praise
- Grouped by platform (Google / TripAdvisor)
