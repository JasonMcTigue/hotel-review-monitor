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

# How recently a review must have been published to be treated as news rather
# than as history. This sorts alerts into the right email; it never decides
# whether one is sent. Anything the monitor has not recorded before is always
# reported, because the check that stops a review being emailed twice is its
# ID being present in the history — not its age.
#
# Reviews older than this are usually pre-existing ones rotating into the API's
# window (the feeds return only a handful each), so announcing them as new
# would be a false alarm. But they can also be genuinely new to us and merely
# slow to appear: Tripadvisor's publish_ts is the guest's submission time and a
# disputed review can sit in moderation for weeks. Those used to be recorded
# and then silently never alerted, so both cases now get their own email.
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
            history = json.load(f)
        # `scores` was added after the first histories were written.
        history.setdefault("scores", {})
        return history
    return {"initialized": False, "google_place_id": None, "scores": {}, "reviews": []}


def record_score(history, platform, score):
    """Store a platform's own headline rating, replacing the previous figure.

    This is the score the platform publishes across its whole review history,
    which is a different thing from averaging the reviews in `reviews` — the
    feeds only ever hand back a handful of those, so their average describes
    the sample rather than the property.
    """
    if not score or not score.get("rating"):
        return
    history.setdefault("scores", {})[platform] = dict(
        score, fetched=datetime.now(timezone.utc).isoformat()
    )


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


def surfaced_after(r):
    """Whole days between a review being published and the monitor seeing it.

    Tripadvisor holds reviews for moderation before they go live and its
    `publish_ts` is the guest's submission time, so a couple of days' gap is
    routine there and none at all is normal on Google. A gap far wider than
    that is the interesting case: it means the review was invisible to the
    feeds until long after it was written.
    """
    published, first_seen = parse_ts(r.get("published", "")), parse_ts(r.get("first_seen", ""))
    if published is None or first_seen is None:
        return 0
    return max(0, (first_seen - published).days)


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
    """Return (the 5 newest reviews, Google's own headline score).

    Use the legacy Place Details endpoint with reviews_sort=newest. The Places
    API (New) only returns ~5 reviews ranked by relevance with no newest sort,
    so once the hotel had enough reviews, new ones stopped making the cut and
    Google alerts silently died. The legacy endpoint returns the 5 *newest*.

    `rating` and `user_ratings_total` come along for free: they sit in the same
    Atmosphere billing category as `reviews`, and a request is charged once at
    the highest category it asks for — so adding them to a call that already
    wants `reviews` costs nothing and saves a second call.
    """
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={"place_id": place_id, "fields": "rating,user_ratings_total,reviews",
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
    result = data.get("result", {})
    score = (
        {"rating": float(result["rating"]),
         "count": int(result.get("user_ratings_total") or 0)}
        if result.get("rating") else {}
    )
    reviews = []
    for r in result.get("reviews", []):
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
    scored = f", score {score['rating']} from {score['count']}" if score else ""
    print(f"Google Details API: {status} — {len(reviews)} reviews{scored}")
    return reviews, score


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


def get_tripadvisor_score():
    """The property's own headline rating on Tripadvisor, across every review.

    A separate call from the reviews one, and metered the same way, which is
    why it runs daily from maintenance.yml rather than every 6 hours: the
    figure barely moves, so a day-old score is no worse than a 6-hour-old one
    and it costs ~30 calls a month instead of ~120.
    """
    resp = requests.get(
        "https://terra.tripadvisor.com/api/locations",
        params={"id": [TRIPADVISOR_LOCATION_ID]},
        headers={"X-API-Key": TRIPADVISOR_API_KEY},
        timeout=10,
    )
    data = resp.json()
    if resp.status_code != 200:
        raise ReviewFetchError(f"HTTP {resp.status_code} — {data}")
    locations = data.get("data") or []
    if not locations:
        raise ReviewFetchError(f"no location returned for id {TRIPADVISOR_LOCATION_ID}")
    overall = ((locations[0].get("traveler_ratings") or {}).get("overall")) or {}
    if not overall.get("rating"):
        # Loud rather than silent: a score that quietly becomes None would
        # leave a stale figure on the dashboard with nothing to say why.
        raise ReviewFetchError(
            f"no overall rating in response — got keys {sorted(locations[0])}")
    print(f"TripAdvisor score: {overall['rating']} from {overall.get('count')} reviews")
    return {"rating": float(overall["rating"]),
            "count": int(overall.get("count") or 0),
            "icon_url": overall.get("icon_url") or ""}


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
    # Only worth saying when the gap is wide enough to be surprising — past the
    # point where the review would once have been dropped without an email —
    # otherwise it would annotate every Tripadvisor review with its routine
    # couple of days in moderation. Worded without reference to "today",
    # because the weekly digest renders these same cards days after the fact.
    gap = surfaced_after(r)
    delay_html = (
        f"<div style='color:#8a6d3b;background:#fcf8e3;font-size:12px;"
        f"padding:6px 8px;margin-top:8px;border-radius:3px;'>"
        f"Written {r['date']}, but {r['platform']} only made it visible "
        f"{gap} days later.</div>"
        if gap > MAX_AGE_DAYS else ""
    )
    return f"""
            <div style="background:#f9f9f9;border-left:4px solid {border_color};
                        padding:12px 16px;margin:10px 0;border-radius:4px;">
              <div style="font-weight:bold;font-size:15px;">{r['author']}</div>
              {rating_html}
              {title_html}
              <div style="color:#333;">{r['text']}</div>
              <div style="color:#999;font-size:12px;margin-top:8px;">{r['date']} {link_html}</div>
              {delay_html}
            </div>"""


def send_html(subject, html, recipients=None):
    """Send to RECIPIENT_EMAILS, or to `recipients` if given.

    The override exists for test runs: verifying the mail path should not put
    an alert about an already-seen review into the hotel's inbox.
    """
    to = recipients or RECIPIENT_EMAILS
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Hotel Review Monitor <{SENDER_EMAIL}>"
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.mail.yahoo.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, to, msg.as_string())

    print(f"Email sent to {', '.join(to)}: {subject}")


def send_email(new_reviews, negative=False, backdated=False, recipients=None):
    total = sum(len(v) for v in new_reviews.values())
    if negative:
        subject = f"⚠️ Negative review — {HOTEL_NAME} ({total})"
        heading = "Negative Review Alert"
        intro = ("A review of <strong>2 stars or fewer</strong> was posted for "
                 f"<strong>{HOTEL_NAME}</strong>. This one is worth a reply:")
    elif backdated:
        # Deliberately flat wording. These are reviews the monitor has genuinely
        # never reported, but they were written a while ago — so they should be
        # easy to skim past without reading like something needing attention
        # today.
        subject = f"Backdated {'review' if total == 1 else 'reviews'} — {HOTEL_NAME} ({total})"
        heading = "Backdated Reviews"
        intro = (f"{'A review' if total == 1 else 'Reviews'} for "
                 f"<strong>{HOTEL_NAME}</strong> that "
                 f"{'has' if total == 1 else 'have'} not been reported before, "
                 f"but {'was' if total == 1 else 'were'} published more than "
                 f"{MAX_AGE_DAYS} days ago. Usually this means a long spell in "
                 "moderation, or a feed serving older reviews:")
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
    </body></html>""", recipients)


# ------------------------------------------------------------------- main ---


def collect(history, fetch, platform, new_reviews, errors):
    """Fetch one platform, record anything unseen, and queue it for alerting.

    The `(platform, id)` check below is the only thing that decides whether a
    review can be emailed, and it is derived from the whole stored history — so
    a review that has already been recorded never alerts again, however many
    times the feeds keep handing it back. Everything unseen is queued
    unconditionally; `main` decides which email it belongs in. Age is not
    consulted here, because a review's age must never determine whether it is
    reported, only how.
    """
    seen = {(r["platform"], r["id"]) for r in history["reviews"]}
    now = datetime.now(timezone.utc).isoformat()
    try:
        for r in fetch():
            if not r["id"] or (r["platform"], r["id"]) in seen:
                continue
            r["first_seen"] = now
            history["reviews"].append(r)
            seen.add((r["platform"], r["id"]))
            if history["initialized"]:
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
        reviews, score = get_google_reviews(place_id)
        # Free with the call above, so it refreshes on every run.
        record_score(history, "Google", score)
        return reviews

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
        # Three routes, so that age only ever changes which email a review
        # arrives in — never whether it arrives at all.
        #
        # Negatives are escalated whatever their age: a 1-2 star review that
        # spent three weeks in moderation is the single most worth knowing
        # about, so it must not be filed away as old news. Praise is split by
        # age instead: recent reviews are the normal alert, while anything
        # published longer ago than MAX_AGE_DAYS gets its own clearly labelled
        # email. That way a feed serving stale content, or a history rebuilt
        # from scratch, reads as what it is rather than as breaking news.
        def bucket(pred):
            return {p: [r for r in rs if pred(r)] for p, rs in new_reviews.items()}

        def negative(r):
            return rating_int(r["rating"]) <= NEGATIVE_RATING

        negatives = bucket(negative)
        positives = bucket(lambda r: not negative(r) and is_recent(r["published"]))
        backdated = bucket(lambda r: not negative(r) and not is_recent(r["published"]))

        if any(negatives.values()):
            send_email(negatives, negative=True)
        if any(positives.values()):
            send_email(positives)
        if any(backdated.values()):
            # Logged as well as emailed: this is the path that used to drop a
            # review on the floor without a word, so it should be visible in
            # the run output even if the email fails to send.
            for p, rs in backdated.items():
                for r in rs:
                    print(f"Backdated {p} review: published {r['date']}, "
                          f"first seen today ({surfaced_after(r)} days)")
            send_email(backdated, backdated=True)
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

    def latest_success():
        # Deliberately unfiltered. Asking the API for `status=success` serves
        # the answer from a secondary index that lags and is not reliably
        # ordered: on 17 Sep and 22 Sep 2026 it returned a run days old while
        # the checker was passing every 6 hours, and both times this function
        # emailed the hotel a false "monitor is silent" alarm. The plain run
        # list is newest-first, so filtering client-side is accurate. A page
        # of 30 covers a week of 6-hourly runs — plenty to find a success in.
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/actions/workflows/check-reviews.yml/runs",
            params={"per_page": 30},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise ReviewFetchError(f"GitHub API: HTTP {resp.status_code} — {resp.text[:200]}")
        runs = resp.json().get("workflow_runs", [])
        return max((r for r in runs if r.get("conclusion") == "success"),
                   key=lambda r: r["created_at"], default=None)

    def age_hours(run):
        return (datetime.now(timezone.utc) - parse_ts(run["created_at"])).total_seconds() / 3600

    run = latest_success()
    if run is None:
        print("No successful runs on record yet.")
        return

    print(f"Last successful check: {run['created_at']} ({age_hours(run):.1f}h ago)")
    if age_hours(run) <= max_age_hours:
        return

    # One stale answer must not reach the hotel's inbox. Re-ask before
    # alerting, and believe whichever response is more recent.
    retry = latest_success()
    if retry is not None and retry["created_at"] > run["created_at"]:
        run = retry
        print(f"Re-checked: {run['created_at']} ({age_hours(run):.1f}h ago)")
        if age_hours(run) <= max_age_hours:
            return
    hours = age_hours(run)

    send_html(
        f"⚠️ Review monitor is silent — {HOTEL_NAME}",
        f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#c0392b;">Review monitor has stopped reporting</h2>
      <p style="color:#333;">The last successful review check was
         <strong>{hours:.0f} hours ago</strong>
         ({run['created_at'][:16].replace('T', ' ')} UTC).</p>
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
            reviews, _ = get_google_reviews(get_google_place_id())
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
            # TEST_RECIPIENT keeps a test run out of the hotel's inbox: the
            # email is an ordinary "New Review" alert about reviews they have
            # already seen, which is confusing to receive unannounced.
            only = os.environ.get("TEST_RECIPIENT", "").strip()
            send_email(test_reviews, recipients=[only] if only else None)
            print("Test email sent with real latest reviews"
                  + (f" to {only} only." if only else " to all recipients."))
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
            g, score = get_google_reviews(place_id)
            for r in sorted(g, key=lambda r: r["published"], reverse=True):
                print(f"  {r['published']}  {r['rating']}★  {r['author']}  | {r['text'][:60]!r}")
            print(f"  ({len(g)} Google reviews returned, score {score or 'none'})")
        except (ReviewFetchError, requests.RequestException) as e:
            errors.append(f"Google: {e}")

        print("\n=== TRIPADVISOR ===")
        try:
            for r in sorted(get_tripadvisor_reviews(), key=lambda r: r["published"], reverse=True):
                print(f"  {r['published']}  {r['rating']}★  {r['author']}  | {r['title']!r}")
        except (ReviewFetchError, requests.RequestException) as e:
            errors.append(f"TripAdvisor: {e}")

        print("\n=== TRIPADVISOR SCORE ===")
        try:
            print(f"  {get_tripadvisor_score()}")
        except (ReviewFetchError, requests.RequestException) as e:
            errors.append(f"TripAdvisor score: {e}")

        for e in errors:
            print(f"ERROR: {e}")
        if errors:
            sys.exit(1)

    elif "--scores" in sys.argv:
        # Refreshes only the platform-published scores, never the reviews, so
        # it cannot alert and cannot alter review history. Google's score is
        # already free with the 6-hourly reviews call; this is here for the
        # Tripadvisor one, which costs a call of its own.
        history = load_history()
        try:
            record_score(history, "TripAdvisor", get_tripadvisor_score())
        except (ReviewFetchError, requests.RequestException) as e:
            print(f"ERROR: TripAdvisor score: {e}")
            sys.exit(1)
        save_history(history)

    elif "--digest" in sys.argv:
        weekly_digest()

    elif "--heartbeat" in sys.argv:
        heartbeat()

    else:
        main()
