#!/usr/bin/env python3
import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

TRIPADVISOR_LOCATION_ID = "34251217"
HOTEL_NAME = "The Grace Westport Estate"
HOTEL_LAT = 53.8009718
HOTEL_LNG = -9.5285384

GOOGLE_API_KEY = os.environ["GOOGLE_PLACES_API_KEY"]
TRIPADVISOR_API_KEY = os.environ["TRIPADVISOR_API_KEY"]
SENDER_EMAIL = "c_newport26@yahoo.com"
SENDER_APP_PASSWORD = os.environ["YAHOO_APP_PASSWORD"]
MANAGER_EMAIL = "jasonmctigue@live.ie"
STATE_FILE = "seen_reviews.json"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "initialized": False,
        "tripadvisor_ids": [],
        "google_times": [],
        "google_place_id": None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_google_place_id():
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
        params={
            "input": HOTEL_NAME,
            "inputtype": "textquery",
            "locationbias": f"point:{HOTEL_LAT},{HOTEL_LNG}",
            "fields": "place_id",
            "key": GOOGLE_API_KEY,
        },
        timeout=10,
    )
    candidates = resp.json().get("candidates", [])
    return candidates[0]["place_id"] if candidates else None


def get_google_reviews(place_id):
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={
            "place_id": place_id,
            "fields": "reviews",
            "reviews_sort": "newest",
            "key": GOOGLE_API_KEY,
        },
        timeout=10,
    )
    return resp.json().get("result", {}).get("reviews", [])


def get_tripadvisor_reviews():
    resp = requests.get(
        f"https://api.content.tripadvisor.com/api/v1/location/{TRIPADVISOR_LOCATION_ID}/reviews",
        params={"key": TRIPADVISOR_API_KEY, "language": "en"},
        timeout=10,
    )
    reviews = []
    for r in resp.json().get("data", []):
        reviews.append({
            "id": str(r.get("id", "")),
            "author": r.get("user", {}).get("username", "Anonymous"),
            "rating": str(r.get("rating", "?")),
            "title": r.get("title", ""),
            "text": r.get("text", "")[:1000],
            "date": r.get("published_date", "")[:10],
        })
    return reviews


def star_rating(rating_str):
    try:
        n = int(float(rating_str))
        return "★" * n + "☆" * (5 - n)
    except (ValueError, TypeError):
        return rating_str


def send_email(new_reviews):
    total = sum(len(v) for v in new_reviews.values())
    subject = f"New Review — {HOTEL_NAME} ({total} new)"

    sections = []
    for platform, reviews in new_reviews.items():
        if not reviews:
            continue
        icon = "🔍" if platform == "Google" else "✈️"
        cards = []
        for r in reviews:
            title_html = (
                f"<div style='font-style:italic;margin-bottom:6px;'>{r['title']}</div>"
                if r.get("title")
                else ""
            )
            cards.append(f"""
            <div style="background:#f9f9f9;border-left:4px solid #4CAF50;
                        padding:12px 16px;margin:10px 0;border-radius:4px;">
              <div style="font-weight:bold;font-size:15px;">{r['author']}</div>
              <div style="color:#f5a623;font-size:18px;margin:4px 0;">{star_rating(r['rating'])}</div>
              {title_html}
              <div style="color:#333;">{r['text']}</div>
              <div style="color:#999;font-size:12px;margin-top:8px;">{r['date']}</div>
            </div>""")

        sections.append(
            f"<h3 style='color:#2c3e50;border-bottom:2px solid #eee;padding-bottom:6px;'>"
            f"{icon} {platform}</h3>" + "".join(cards)
        )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#2c3e50;">New Review Alert</h2>
      <p style="color:#666;">New {"review" if total == 1 else "reviews"} posted for
         <strong>{HOTEL_NAME}</strong>:</p>
      {"".join(sections)}
      <hr style="border:none;border-top:1px solid #eee;margin-top:24px;">
      <p style="color:#aaa;font-size:11px;">Monitored by hotel-review-monitor</p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Hotel Review Monitor <{SENDER_EMAIL}>"
    msg["To"] = MANAGER_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.mail.yahoo.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, MANAGER_EMAIL, msg.as_string())

    print(f"Email sent: {subject}")


def main():
    state = load_state()
    new_reviews = {"Google": [], "TripAdvisor": []}

    # Google Reviews
    place_id = state.get("google_place_id") or get_google_place_id()
    if place_id:
        state["google_place_id"] = place_id
        seen_times = set(str(t) for t in state.get("google_times", []))
        for r in get_google_reviews(place_id):
            t = str(r.get("time", ""))
            if t and t not in seen_times:
                if state["initialized"]:
                    new_reviews["Google"].append({
                        "author": r.get("author_name", "Anonymous"),
                        "rating": str(r.get("rating", "?")),
                        "text": r.get("text", "")[:1000],
                        "date": r.get("relative_time_description", ""),
                        "title": "",
                    })
                seen_times.add(t)
        state["google_times"] = list(seen_times)
    else:
        print("Warning: Could not find Google Place ID — check your API key")

    # TripAdvisor Reviews
    seen_ids = set(str(i) for i in state.get("tripadvisor_ids", []))
    for r in get_tripadvisor_reviews():
        rid = str(r["id"])
        if rid not in seen_ids:
            if state["initialized"]:
                new_reviews["TripAdvisor"].append(r)
            seen_ids.add(rid)
    state["tripadvisor_ids"] = list(seen_ids)

    if not state["initialized"]:
        state["initialized"] = True
        print("First run complete — existing reviews recorded. Will alert on new reviews from now on.")
    else:
        total = sum(len(v) for v in new_reviews.values())
        if total > 0:
            send_email(new_reviews)
        else:
            print("No new reviews found.")

    save_state(state)


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        test_reviews = {"Google": [], "TripAdvisor": []}

        place_id = get_google_place_id()
        print(f"Google Place ID: {place_id}")
        if place_id:
            reviews = get_google_reviews(place_id)
            print(f"Google reviews fetched: {len(reviews)}")
            if reviews:
                r = reviews[0]
                test_reviews["Google"].append({
                    "author": r.get("author_name", "Anonymous"),
                    "rating": str(r.get("rating", "?")),
                    "text": r.get("text", "")[:1000],
                    "date": r.get("relative_time_description", ""),
                    "title": "",
                })
        else:
            print("Warning: Could not find Google Place ID — check your API key")

        ta_reviews = get_tripadvisor_reviews()
        print(f"TripAdvisor reviews fetched: {len(ta_reviews)}")
        if ta_reviews:
            test_reviews["TripAdvisor"].append(ta_reviews[0])

        if any(test_reviews.values()):
            send_email(test_reviews)
            print("Test email sent with real latest reviews.")
        else:
            print("No reviews found to send.")
    else:
        main()
