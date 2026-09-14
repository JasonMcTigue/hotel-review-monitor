# Hotel Review Monitor

Automatically monitors Google and TripAdvisor reviews for **** and sends email alerts when new reviews are posted.

## How it works

- Runs every 30 minutes via GitHub Actions
- Checks Google Places and TripAdvisor for new reviews
- Sends an HTML email alert with review details
- Highlights 1-2 star reviews with a red border for quick visibility

## Setup

### 1. GitHub Secrets

Add the following secrets to your repository (**Settings → Secrets and variables → Actions**):

| Secret | Description |
|---|---|
| `GOOGLE_PLACES_API_KEY` | Google Places key — needs both the legacy Places API and Places API (New) |
| `TRIPADVISOR_API_KE` | TripAdvisor Content API key |
| `YAHOO_APP_PASSWORD` | Yahoo Mail app password |

### 2. Google Cloud Console

Enable **both** of these for your project at [console.cloud.google.com](https://console.cloud.google.com):

- **Places API (New)** — used to look up the hotel's place ID
- **Places API** (legacy) — used to fetch reviews, because only the legacy Place Details endpoint supports `reviews_sort=newest`

If the legacy API is disabled, review fetching fails with `REQUEST_DENIED` and the run logs Google's `error_message` explaining why.

### 3. First run

On first run the script records all existing reviews without sending an email. From the second run onwards it will only alert on new reviews.

## Manual test

Go to **Actions → Check Hotel Reviews → Run workflow** and enable the **"Send a test email"** toggle. This sends the latest review from each platform to confirm everything is working.

## When a feed breaks

If either API refuses a request, that platform's error is printed and the run **fails** (exit 1), so GitHub emails you about the failed workflow. The other platform is still checked and can still alert — one dead feed doesn't take the other down.

This matters: previously a refused API returned an empty list, which was indistinguishable from "no new reviews", so the run went green. Google alerts were dead for six weeks before anyone noticed.

## Keepalive

GitHub disables scheduled workflows after 60 days with no repository activity — this is what stopped the monitor in September 2026. `keepalive.yml` pushes an empty commit on the 1st and 21st of each month to reset that timer, and re-enables `check-reviews.yml` via the API if it was disabled anyway.

## Email format

- One card per new review showing author, star rating, title, and text
- Green left border for positive reviews (3-5 stars)
- Red left border for negative reviews (1-2 stars)
- Grouped by platform (Google / TripAdvisor)
