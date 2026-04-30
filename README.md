# Hotel Review Monitor

Automatically monitors Google and TripAdvisor reviews for The Grace Westport Estate and sends email alerts when new reviews are posted.

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
| `GOOGLE_PLACES_API_KEY` | Google Places API (New) key |
| `TRIPADVISOR_API_KE` | TripAdvisor Content API key |
| `YAHOO_APP_PASSWORD` | Yahoo Mail app password |

### 2. Google Cloud Console

Enable the **Places API (New)** for your project at [console.cloud.google.com](https://console.cloud.google.com).

### 3. First run

On first run the script records all existing reviews without sending an email. From the second run onwards it will only alert on new reviews.

## Manual test

Go to **Actions → Check Hotel Reviews → Run workflow** and enable the **"Send a test email"** toggle. This sends the latest review from each platform to confirm everything is working.

## Email format

- One card per new review showing author, star rating, title, and text
- Green left border for positive reviews (3-5 stars)
- Red left border for negative reviews (1-2 stars)
- Grouped by platform (Google / TripAdvisor)
