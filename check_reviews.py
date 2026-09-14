#!/usr/bin/env python3
import json
import os
import smtplib
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

TRIPADVISOR_LOCATION_ID = "34251217"
HOTEL_NAME = "The Grace Westport Estate"
HOTEL_LAT = 53.8009718
HOTEL_LNG = -9.5285384

GOOGLE_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
TRIPADVISOR_API_KEY = os.environ.get("TRIPADVISOR_API_KEY", "")
DASHBOARD_URL = "https://jasonmctigue.github.io/hotel-review-monitor/"
SENDER_EMAIL = "c_newport26@yahoo.com"
SENDER_APP_PASSWORD = os.environ.get("YAHOO_APP_PASSWORD", "")
RECIPIENT_EMAILS = ["jasonmctigue@live.ie", "creidy@thegrace.ie"]

# Full review history, committed to the repo. This is the single source of
# truth: "have we seen this review" is derived from it, so there is no separate
# state file to fall out of sync. It lives in git rather than the Actions cache
# because caches are evicted after 7 days unused — which would silently destroy
# the history the dashboard and digest are built on.
HISTORY_FILE = "reviews.json"

# A review only counts as "new" if it was published within this window. Reviews
# older than this are pre-existing ones rotating into the API's relevance-ranked
# window (the APIs return only a handful of reviews), not genuinely new reviews —
# alerting on them would be a false alarm. Kept tight so only genuinely recent
# reviews alert, while still leaving a few days' slack for a review delayed by
# moderation to appear before the window closes.
MAX_AGE_DAYS = 7

# A 1-2 star review is escalated into its own email rather than being buried in
# a batch with the good ones.
NEGATIVE_RATING = 2


class ReviewFetchError(Exception):
    """A review API refused or failed the request.

    Raised instead of returning an empty list, because an empty list is
    indistinguishable from "no new reviews" — which is how a dead Google feed
    went unnoticed for six weeks while every run stayed green.
    """


# ---------------------------------------------------------------- history ---


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {"initialized": False, "google_place_id": None, "reviews": []}


def save_history(history):
    history["reviews"].sort(key=lambda r: r.get("published") or "", reverse=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_ts(s):
    """Parse an RFC3339 timestamp into a comparable datetime, or None."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    # Truncate over-long fractional seconds (Google can return nanoseconds).
    if "." in s:
        head, frac = s.split(".", 1)
        tz = ""
        for sep in ("+", "-"):
            if sep in frac:
                idx = frac.index(sep)
                tz, frac = frac[idx:], frac[:idx]
                break
        s = f"{head}.{frac[:6]}{tz}"
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def is_recent(ts, days=MAX_AGE_DAYS):
    """True if a review was published within `days` of now.

    Used to tell a genuinely new review apart from an old one that has merely
    rotated into the API's window with an ID we hadn't recorded yet. Unlike a
    high-water mark, this never suppresses a new review just because an
    even-newer one was seen first. An unparseable or missing timestamp errs
    toward alerting rather than silently dropping.
    """
    t = parse_ts(ts)
    if t is None:
        return True
    return datetime.now(timezone.utc) - t <= timedelta(days=days)


def rating_int(value):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


# ----------------------------------------------------------------- google ---


def get_google_place_id():
    resp = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        json={"textQuery": HOTEL_NAME, "locationBias": {"circle": {"center": {"latitude": HOTEL_LAT, "longitude": HOTEL_LNG}, "radius": 1000.0}}},
        headers={"X-Goog-Api-Key": GOOGLE_API_KEY, "X-Goog-FieldMask": "places.id"},
        timeout=10,
    )
    data = resp.json()
    places = data.get("places", [])
    if resp.status_code != 200 or not places:
        raise ReviewFetchError(
            f"Places search failed: HTTP {resp.status_code} — "
            f"{data.get('error', {}).get('message') or data}"
        )
    print(f"Google Places search: OK — place {places[0]['id']}")
    return places[0]["id"]


def get_google_reviews(place_id):
    # Use the legacy Place Details endpoint with reviews_sort=newest. The Places
    # API (New) only returns ~5 reviews ranked by relevance with no newest sort,
    # so once the hotel had enough reviews, new ones stopped making the cut and
    # Google alerts silently died. The legacy endpoint returns the 5 *newest*.
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={"place_id": place_id, "fields": "reviews",
                "reviews_sort": "newest", "key": GOOGLE_API_KEY},
        timeout=10,
    )
    data = resp.json()
    status = data.get("status")
    # The legacy endpoint answers HTTP 200 even when it refuses the request, so
    # the real verdict is in `status`; `error_message` says *why* it refused
    # (billing disabled, API not enabled, key restriction).
    if resp.status_code != 200 or status not in ("OK", "ZERO_RESULTS"):
        raise ReviewFetchError(
            f"Place Details failed: HTTP {resp.status_code} {status} — "
            f"{data.get('error_message', 'no error_message returned')}"
        )
    reviews = []
    for r in data.get("result", {}).get("reviews", []):
        # Legacy reviews have no stable resource id; synthesise one from the
        # author and unix review time so dedup still works.
        t = r.get("time")
        published = (datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
                     if isinstance(t, (int, float)) else "")
        reviews.append({
            "platform": "Google",
            "id": f"google:{r.get('author_name', '')}:{t}",
            "author": r.get("author_name", "Anonymous"),
            "rating": rating_int(r.get("rating")),
            "title": "",
            "text": (r.get("text") or "")[:1000],
            "published": published,
            "date": published[:10],
            "url": r.get("author_url", ""),
            "rating_icon": "",
            "owner_response": False,
        })
    print(f"Google Details API: {status} — {len(reviews)} reviews")
    return reviews


# ------------------------------------------------------------ tripadvisor ---


def primary_translation(items):
    """Terra returns title and text as a list of translations, one per language.

    The entry flagged `primary` is the language the review was written in.
    """
    if not items:
        return ""
    primary = next((i for i in items if i.get("primary")), items[0])
    return primary.get("value", "")


def get_tripadvisor_reviews(size=25):
    # Terra, not the legacy Content API: that was sunset on 31 Aug 2026 and now
    # returns 403 to every key, which is what killed TripAdvisor alerts on
    # 1 Sep. Terra sorts by date natively — the legacy endpoint could not — so
    # MAX_AGE_DAYS is now a backstop rather than the only thing stopping old
    # reviews from rotating into view and alerting.
    #
    # The Discover (entry) tier returns only the 3 most recent reviews per
    # location no matter what `size` asks for, so there is no history to
    # backfill and the run cadence must stay well under 3 new reviews apart.
    resp = requests.get(
        f"https://terra.tripadvisor.com/api/locations/{TRIPADVISOR_LOCATION_ID}/reviews",
        params={"sort_by": "MOST_RECENT", "size": size, "language": "en"},
        headers={"X-API-Key": TRIPADVISOR_API_KEY},
        timeout=10,
    )
    data = resp.json()
    if resp.status_code != 200:
        raise ReviewFetchError(f"HTTP {resp.status_code} — {data}")
    reviews = []
    for r in data.get("data", []):
        published = r.get("publish_ts", "") or ""
        reviews.append({
            "platform": "TripAdvisor",
            "id": str(r.get("id", "")),
            "author": (r.get("user") or {}).get("username", "Anonymous"),
            "rating": rating_int(r.get("rating")),
            "title": primary_translation(r.get("title")),
            "text": primary_translation(r.get("text"))[:1000],
            "published": published,
            "date": published[:10],
            "url": r.get("url", ""),
            # Terra's display requirements: show Tripadvisor's own bubble
            # rating image and link back to the review on Tripadvisor.
            "rating_icon": (r.get("rating_icon_url") or {}).get("url", ""),
            # Absent on the Discover tier, but recorded so a plan upgrade
            # lights up response tracking without a data migration.
            "owner_response": bool(r.get("owner_response")),
        })
    print(f"TripAdvisor API: OK — {len(reviews)} reviews")
    return reviews


# ------------------------------------------------------------------ email ---


def star_rating(rating):
    n = rating_int(rating)
    return "★" * n + "☆" * (5 - n) if n else str(rating)


def review_card(r):
    title_html = (
        f"<div style='font-style:italic;margin-bottom:6px;'>{r['title']}</div>"
        if r.get("title") else ""
    )
    border_color = "#e74c3c" if rating_int(r["rating"]) <= NEGATIVE_RATING else "#4CAF50"
    # Tripadvisor's terms require its own bubble rating image rather than a
    # substitute; Google reviews keep the star glyphs.
    rating_html = (
        f"<img src='{r['rating_icon']}' alt='{r['rating']} of 5 bubbles' "
        f"style='height:18px;margin:6px 0;display:block;'>"
        if r.get("rating_icon")
        else f"<div style='color:#f5a623;font-size:18px;margin:4px 0;'>{star_rating(r['rating'])}</div>"
    )
    link_html = (
        f"<a href='{r['url']}' style='color:#00aa6c;font-size:12px;'>Read on {r['platform']}</a>"
        if r.get("url") else ""
    )
    return f"""
            <div style="background:#f9f9f9;border-left:4px solid {border_color};
                        padding:12px 16px;margin:10px 0;border-radius:4px;">
              <div style="font-weight:bold;font-size:15px;">{r['author']}</div>
              {rating_html}
              {title_html}
              <div style="color:#333;">{r['text']}</div>
              <div style="color:#999;font-size:12px;margin-top:8px;">{r['date']} {link_html}</div>
            </div>"""


def send_html(subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Hotel Review Monitor <{SENDER_EMAIL}>"
    msg["To"] = ", ".join(RECIPIENT_EMAILS)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.mail.yahoo.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAILS, msg.as_string())

    print(f"Email sent: {subject}")


def send_email(new_reviews, negative=False):
    total = sum(len(v) for v in new_reviews.values())
    if negative:
        subject = f"⚠️ Negative review — {HOTEL_NAME} ({total})"
        heading = "Negative Review Alert"
        intro = ("A review of <strong>2 stars or fewer</strong> was posted for "
                 f"<strong>{HOTEL_NAME}</strong>. This one is worth a reply:")
    else:
        subject = f"New Review — {HOTEL_NAME} ({total} new)"
        heading = "New Review Alert"
        intro = (f"New {'review' if total == 1 else 'reviews'} posted for "
                 f"<strong>{HOTEL_NAME}</strong>:")

    sections = []
    for platform, reviews in new_reviews.items():
        if not reviews:
            continue
        icon = "🔍" if platform == "Google" else "✈️"
        cards = "".join(review_card(r) for r in reviews)
        # Terra's display requirements: review content must be credited.
        credit = (
            "<p style='color:#999;font-size:11px;margin:4px 0 0;'>"
            "Reviews and bubble ratings provided by Tripadvisor.</p>"
            if platform == "TripAdvisor" else ""
        )
        sections.append(
            f"<h3 style='color:#2c3e50;border-bottom:2px solid #eee;padding-bottom:6px;'>"
            f"{icon} {platform}</h3>" + cards + credit
        )

    send_html(subject, f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:{'#c0392b' if negative else '#2c3e50'};">{heading}</h2>
      <p style="color:#666;">{intro}</p>
      {"".join(sections)}
      <hr style="border:none;border-top:1px solid #eee;margin-top:24px;">
      <p style="color:#aaa;font-size:11px;"><a href="{DASHBOARD_URL}" style="color:#1f4d3d;">View the review dashboard</a> · Monitored by hotel-review-monitor</p>
    </body></html>""")


# ------------------------------------------------------------------- main ---


def collect(history, fetch, platform, new_reviews, errors):
    """Fetch one platform, record anything unseen, and queue genuinely new reviews."""
    seen = {(r["platform"], r["id"]) for r in history["reviews"]}
    now = datetime.now(timezone.utc).isoformat()
    try:
        for r in fetch():
            if not r["id"] or (r["platform"], r["id"]) in seen:
                continue
            r["first_seen"] = now
            history["reviews"].append(r)
            seen.add((r["platform"], r["id"]))
            if history["initialized"] and is_recent(r["published"]):
                new_reviews[platform].append(r)
    except (ReviewFetchError, requests.RequestException) as e:
        errors.append(f"{platform}: {e}")


def main():
    history = load_history()
    new_reviews = {"Google": [], "TripAdvisor": []}
    # Each platform is fetched independently so one dead feed still lets the
    # other alert, but any failure is collected and exits non-zero at the end —
    # a broken feed must show up as a red run, not as "No new reviews found."
    errors = []

    def google():
        place_id = history.get("google_place_id") or get_google_place_id()
        history["google_place_id"] = place_id
        return get_google_reviews(place_id)

    collect(history, google, "Google", new_reviews, errors)
    collect(history, get_tripadvisor_reviews, "TripAdvisor", new_reviews, errors)

    if not history["initialized"]:
        # Only claim a baseline once both feeds have actually answered —
        # otherwise the failing platform's existing reviews would all look new
        # the first time it recovers.
        if errors:
            print("First run incomplete — baseline not recorded while a feed is failing.")
        else:
            history["initialized"] = True
            print(f"Baseline recorded: {len(history['reviews'])} existing reviews. "
                  "Will alert on new reviews from now on.")
    else:
        # Split negatives into their own email so a 1-2 star review is never
        # buried in a batch of praise.
        negatives = {p: [r for r in rs if rating_int(r["rating"]) <= NEGATIVE_RATING]
                     for p, rs in new_reviews.items()}
        positives = {p: [r for r in rs if rating_int(r["rating"]) > NEGATIVE_RATING]
                     for p, rs in new_reviews.items()}
        if any(negatives.values()):
            send_email(negatives, negative=True)
        if any(positives.values()):
            send_email(positives)
        if not any(new_reviews.values()) and not errors:
            print("No new reviews found.")

    save_history(history)

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        sys.exit(1)


# ----------------------------------------------------------------- digest ---


def weekly_digest():
    """Monday summary built entirely from stored history — costs no API calls."""
    history = load_history()
    reviews = history["reviews"]
    if not reviews:
        print("No history yet — nothing to summarise.")
        return

    week = [r for r in reviews if is_recent(r["published"], days=7)]
    month = [r for r in reviews if is_recent(r["published"], days=30)]

    def average(rs):
        rated = [rating_int(r["rating"]) for r in rs if rating_int(r["rating"])]
        return sum(rated) / len(rated) if rated else 0

    rows = "".join(
        f"<tr><td style='padding:6px 12px 6px 0;color:#666;'>{label}</td>"
        f"<td style='padding:6px 0;font-weight:bold;'>{value}</td></tr>"
        for label, value in [
            ("Reviews this week", len(week)),
            ("Average this week", f"{average(week):.1f} ★" if week else "—"),
            ("Reviews last 30 days", len(month)),
            ("Average last 30 days", f"{average(month):.1f} ★" if month else "—"),
            ("All-time tracked", len(reviews)),
            ("All-time average", f"{average(reviews):.1f} ★"),
        ]
    )

    worst = min(week, key=lambda r: rating_int(r["rating"]), default=None)
    lowlight = ""
    if worst and rating_int(worst["rating"]) <= 3:
        lowlight = ("<h3 style='color:#c0392b;'>Needs attention</h3>"
                    + review_card(worst))

    cards = "".join(review_card(r) for r in sorted(
        week, key=lambda r: r["published"], reverse=True)[:5])
    body = cards or "<p style='color:#666;'>No new reviews this week.</p>"

    send_html(
        f"Weekly review digest — {HOTEL_NAME}",
        f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#2c3e50;">Weekly Review Digest</h2>
      <p style="color:#666;">{HOTEL_NAME}, week ending
         {datetime.now(timezone.utc).strftime('%d %B %Y')}</p>
      <table style="border-collapse:collapse;margin:16px 0;">{rows}</table>
      {lowlight}
      <h3 style="color:#2c3e50;">This week's reviews</h3>
      {body}
      <p style='color:#999;font-size:11px;'>Reviews and bubble ratings provided by Tripadvisor.</p>
      <hr style="border:none;border-top:1px solid #eee;margin-top:24px;">
      <p style="color:#aaa;font-size:11px;"><a href="{DASHBOARD_URL}" style="color:#1f4d3d;">View the review dashboard</a> · Monitored by hotel-review-monitor</p>
    </body></html>""")


# -------------------------------------------------------------- heartbeat ---


def heartbeat(max_age_hours=24):
    """Alert if the review checker has not succeeded recently.

    The failure this catches is silence: a schedule disabled for inactivity, a
    workflow erroring before it can report, or Actions being down. None of those
    produce a failed run to notice — the monitor simply stops, which is exactly
    how six weeks of Google alerts went missing.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/workflows/check-reviews.yml/runs",
        params={"status": "success", "per_page": 1},
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise ReviewFetchError(f"GitHub API: HTTP {resp.status_code} — {resp.text[:200]}")

    runs = resp.json().get("workflow_runs", [])
    if not runs:
        print("No successful runs on record yet.")
        return

    last = parse_ts(runs[0]["created_at"])
    age = datetime.now(timezone.utc) - last
    hours = age.total_seconds() / 3600
    print(f"Last successful check: {runs[0]['created_at']} ({hours:.1f}h ago)")
    if hours <= max_age_hours:
        return

    send_html(
        f"⚠️ Review monitor is silent — {HOTEL_NAME}",
        f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#c0392b;">Review monitor has stopped reporting</h2>
      <p style="color:#333;">The last successful review check was
         <strong>{hours:.0f} hours ago</strong>
         ({runs[0]['created_at'][:16].replace('T', ' ')} UTC).</p>
      <p style="color:#666;">New reviews for {HOTEL_NAME} may be going unnoticed.
         Common causes: the schedule was disabled for repository inactivity, an
         API key expired, or Google Cloud billing lapsed.</p>
      <p><a href="https://github.com/{repo}/actions/workflows/check-reviews.yml">
         Open the workflow</a></p>
    </body></html>""")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_reviews = {"Google": [], "TripAdvisor": []}
        errors = []
        try:
            reviews = get_google_reviews(get_google_place_id())
            test_reviews["Google"] = reviews[:1]
        except (ReviewFetchError, requests.RequestException) as e:
            errors.append(f"Google: {e}")
        try:
            ta = get_tripadvisor_reviews()
            test_reviews["TripAdvisor"] = ta[:1]
            low = next((r for r in ta if rating_int(r["rating"]) <= NEGATIVE_RATING), None)
            if low and low not in test_reviews["TripAdvisor"]:
                test_reviews["TripAdvisor"].append(low)
        except (ReviewFetchError, requests.RequestException) as e:
            errors.append(f"TripAdvisor: {e}")

        if any(test_reviews.values()):
            send_email(test_reviews)
            print("Test email sent with real latest reviews.")
        else:
            print("No reviews found to send.")
        for e in errors:
            print(f"ERROR: {e}")
        if errors:
            sys.exit(1)

    elif "--debug" in sys.argv:
        # Read-only: dump exactly what each API returns, newest first. Sends no
        # email and does not touch history.
        history = load_history()
        errors = []
        print(f"\n=== GOOGLE (place {history.get('google_place_id')}) ===")
        try:
            place_id = history.get("google_place_id") or get_google_place_id()
            g = get_google_reviews(place_id)
            for r in sorted(g, key=lambda r: r["published"], reverse=True):
                print(f"  {r['published']}  {r['rating']}★  {r['author']}  | {r['text'][:60]!r}")
            print(f"  ({len(g)} Google reviews returned)")
        except (ReviewFetchError, requests.RequestException) as e:
            errors.append(f"Google: {e}")

        print("\n=== TRIPADVISOR ===")
        try:
            for r in sorted(get_tripadvisor_reviews(), key=lambda r: r["published"], reverse=True):
                print(f"  {r['published']}  {r['rating']}★  {r['author']}  | {r['title']!r}")
        except (ReviewFetchError, requests.RequestException) as e:
            errors.append(f"TripAdvisor: {e}")

        for e in errors:
            print(f"ERROR: {e}")
        if errors:
            sys.exit(1)

    elif "--digest" in sys.argv:
        weekly_digest()

    elif "--heartbeat" in sys.argv:
        heartbeat()

    else:
        main()
