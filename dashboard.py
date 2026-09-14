#!/usr/bin/env python3
"""Generate the review dashboard from reviews.json.

Emits a standalone page for GitHub Pages (docs/index.html) and, with
--artifact, the same page without the document wrapper so it can be published
as an Artifact. Reads only stored history, so it costs no API calls.
"""
import html
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

HOTEL_NAME = "The Grace Westport Estate"
HISTORY_FILE = "reviews.json"
OUTPUT_FILE = os.path.join("docs", "index.html")
MONTHS_SHOWN = 6


def rating_int(value):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def parse_day(published):
    return (published or "")[:10]


def month_key(published):
    return (published or "")[:7]


def month_label(key):
    try:
        return datetime.strptime(key, "%Y-%m").strftime("%b")
    except ValueError:
        return key


def summarise(history):
    reviews = [r for r in history.get("reviews", []) if r.get("published")]
    reviews.sort(key=lambda r: r["published"], reverse=True)
    now = datetime.now(timezone.utc)

    def age_days(r):
        try:
            t = datetime.fromisoformat(r["published"].replace("Z", "+00:00"))
        except ValueError:
            return 9999
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).total_seconds() / 86400

    rated = [rating_int(r["rating"]) for r in reviews if rating_int(r["rating"])]
    last_30 = [r for r in reviews if age_days(r) <= 30]
    rated_30 = [rating_int(r["rating"]) for r in last_30 if rating_int(r["rating"])]

    # Months are built from the review dates present, newest MONTHS_SHOWN, so a
    # sparse history shows the months it has rather than a run of empty bars.
    by_month = defaultdict(lambda: {"Google": 0, "TripAdvisor": 0})
    for r in reviews:
        by_month[month_key(r["published"])][r.get("platform", "Google")] += 1
    months = sorted(by_month)[-MONTHS_SHOWN:]

    distribution = Counter(rating_int(r["rating"]) for r in reviews)

    return {
        "hotel": HOTEL_NAME,
        "generated": now.strftime("%d %b %Y, %H:%M UTC"),
        "tracking_since": parse_day(reviews[-1]["published"]) if reviews else "",
        "average": round(sum(rated) / len(rated), 1) if rated else 0,
        "average_30": round(sum(rated_30) / len(rated_30), 1) if rated_30 else 0,
        "total": len(reviews),
        "count_30": len(last_30),
        "days_since": int(age_days(reviews[0])) if reviews else None,
        "negatives": sum(1 for r in reviews if 0 < rating_int(r["rating"]) <= 2),
        "platforms": dict(Counter(r.get("platform", "Google") for r in reviews)),
        "months": [
            {"key": m, "label": month_label(m),
             "Google": by_month[m]["Google"], "TripAdvisor": by_month[m]["TripAdvisor"]}
            for m in months
        ],
        "distribution": [{"stars": s, "count": distribution.get(s, 0)} for s in range(5, 0, -1)],
        "reviews": [
            {
                "platform": r.get("platform", ""),
                "author": r.get("author", "Anonymous"),
                "rating": rating_int(r["rating"]),
                "title": r.get("title", ""),
                "text": r.get("text", ""),
                "date": parse_day(r["published"]),
                "url": r.get("url", ""),
            }
            for r in reviews[:20]
        ],
    }


STYLE = """
<style>
  :root {
    color-scheme: light;
    --page:        #f7f8f6;
    --surface:     #fcfcfb;
    --ink:         #0b0b0b;
    --ink-2:       #52514e;
    --muted:       #898781;
    --rule:        #e1e0d9;
    --baseline:    #c3c2b7;
    --accent:      #1f4d3d;
    --series-1:    #2a78d6;
    --series-2:    #eb6834;
    --critical:    #d03b3b;
    --good:        #0ca30c;
    --ring:        rgba(11,11,11,0.10);
    --shadow:      0 1px 2px rgba(11,11,11,0.05);
    --display: "Fraunces", Georgia, "Times New Roman", serif;
    --ui: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page:     #0d0d0d;
      --surface:  #1a1a19;
      --ink:      #ffffff;
      --ink-2:    #c3c2b7;
      --muted:    #898781;
      --rule:     #2c2c2a;
      --baseline: #383835;
      --accent:   #7fbfa4;
      --series-1: #3987e5;
      --series-2: #d95926;
      --critical: #d03b3b;
      --good:     #0ca30c;
      --ring:     rgba(255,255,255,0.10);
      --shadow:   none;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page:     #0d0d0d;
    --surface:  #1a1a19;
    --ink:      #ffffff;
    --ink-2:    #c3c2b7;
    --muted:    #898781;
    --rule:     #2c2c2a;
    --baseline: #383835;
    --accent:   #7fbfa4;
    --series-1: #3987e5;
    --series-2: #d95926;
    --critical: #d03b3b;
    --good:     #0ca30c;
    --ring:     rgba(255,255,255,0.10);
    --shadow:   none;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--ink);
    font-family: var(--ui);
    font-size: 15px;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }
  .wrap {
    max-width: 1040px;
    margin: 0 auto;
    padding-inline: 20px;
    padding-block: 32px 56px;
    display: flex;
    flex-direction: column;
    gap: 28px;
  }
  a { color: var(--accent); }
  a:focus-visible, button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* masthead */
  .masthead { display: flex; flex-wrap: wrap; gap: 16px 24px; align-items: baseline;
              justify-content: space-between; border-bottom: 2px solid var(--accent);
              padding-bottom: 14px; }
  .masthead h1 { font-family: var(--display); font-weight: 600; font-size: 27px;
                 margin: 0; letter-spacing: -0.01em; text-wrap: balance; }
  .eyebrow { font-size: 11px; letter-spacing: 0.13em; text-transform: uppercase;
             color: var(--muted); margin: 0 0 2px; }
  .feeds { display: flex; gap: 8px; flex-wrap: wrap; }
  .feed { display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
          color: var(--ink-2); border: 1px solid var(--rule); border-radius: 999px;
          padding: 3px 10px; background: var(--surface); }
  .dot { width: 7px; height: 7px; border-radius: 50%; }

  /* stat tiles */
  .tiles { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 6px;
          padding: 14px 16px; box-shadow: var(--shadow); }
  .tile .label { font-size: 11px; letter-spacing: 0.09em; text-transform: uppercase;
                 color: var(--muted); }
  .tile .value { font-size: 30px; font-weight: 600; line-height: 1.15; margin-top: 4px;
                 font-variant-numeric: tabular-nums; }
  .tile .value.hero { font-size: 44px; }
  .tile .note { font-size: 12px; color: var(--ink-2); }

  /* cards */
  .grid2 { display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
  .card { background: var(--surface); border: 1px solid var(--ring); border-radius: 6px;
          padding: 18px 18px 14px; box-shadow: var(--shadow); }
  .card h2 { font-family: var(--display); font-size: 16px; font-weight: 600; margin: 0 0 2px; }
  .card .sub { font-size: 12px; color: var(--muted); margin: 0 0 14px; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px;
            color: var(--ink-2); margin-bottom: 10px; }
  .swatch { width: 9px; height: 9px; border-radius: 2px; display: inline-block;
            margin-right: 5px; vertical-align: middle; }
  .empty { font-size: 13px; color: var(--ink-2); padding: 22px 0; text-align: center; }
  figure { margin: 0; }
  svg { display: block; width: 100%; height: auto; overflow: visible; }
  .tick { font-size: 11px; fill: var(--muted); font-family: var(--ui); }
  .datalabel { font-size: 11px; fill: var(--ink-2); font-family: var(--ui);
               font-variant-numeric: tabular-nums; }

  .toggle { background: none; border: 1px solid var(--rule); border-radius: 4px;
            color: var(--ink-2); font: inherit; font-size: 11px; padding: 3px 9px;
            cursor: pointer; margin-top: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
  th, td { text-align: left; padding: 5px 8px 5px 0; border-bottom: 1px solid var(--rule);
           font-variant-numeric: tabular-nums; }
  th { font-size: 11px; letter-spacing: 0.07em; text-transform: uppercase; color: var(--muted);
       font-weight: 500; }

  /* review list */
  .reviews { display: flex; flex-direction: column; gap: 10px; }
  .review { background: var(--surface); border: 1px solid var(--ring);
            border-left: 3px solid var(--baseline); border-radius: 5px; padding: 13px 16px; }
  .review.low { border-left-color: var(--critical); }
  .review.high { border-left-color: var(--good); }
  .review .head { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; }
  .review .who { font-weight: 600; }
  .review .stars { color: var(--series-2); letter-spacing: 1px; font-size: 13px; }
  .review .meta { font-size: 12px; color: var(--muted); margin-left: auto;
                  font-variant-numeric: tabular-nums; }
  .chip { font-size: 11px; border: 1px solid var(--rule); border-radius: 3px;
          padding: 1px 6px; color: var(--ink-2); }
  .chip.warn { border-color: var(--critical); color: var(--critical); }
  .review .quote { margin: 7px 0 0; color: var(--ink-2); }
  .review .quote strong { color: var(--ink); font-weight: 600; }

  footer { border-top: 1px solid var(--rule); padding-top: 14px; font-size: 12px;
           color: var(--muted); display: flex; flex-direction: column; gap: 4px; }

  #tip { position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
         background: var(--ink); color: var(--page); font-size: 12px; line-height: 1.4;
         padding: 6px 9px; border-radius: 4px; z-index: 10; max-width: 220px; }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
"""


def esc(s):
    return html.escape(str(s or ""))


def stars(n):
    return "★" * n + "☆" * (5 - n)


HEAD = f"""<title>Grace Westport Reviews</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
{STYLE}"""


def render_content(d):
    feeds = "".join(
        f'<span class="feed"><span class="dot" style="background:{color}"></span>'
        f'{esc(name)} · {d["platforms"].get(name, 0)}</span>'
        for name, color in (("Google", "var(--series-1)"), ("TripAdvisor", "var(--series-2)"))
    )

    since = d["days_since"]
    since_text = "today" if since == 0 else ("1 day ago" if since == 1 else f"{since} days ago")

    tiles = [
        ("Average rating", f'{d["average"]:.1f}' if d["average"] else "—", "hero",
         f'across {d["total"]} reviews'),
        ("Last 30 days", str(d["count_30"]), "",
         f'averaging {d["average_30"]:.1f} ★' if d["average_30"] else "no reviews yet"),
        ("Most recent", since_text if since is not None else "—", "",
         d["reviews"][0]["date"] if d["reviews"] else ""),
        ("1–2 star reviews", str(d["negatives"]), "",
         "none on record" if not d["negatives"] else "escalated by email"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="label">{esc(label)}</div>'
        f'<div class="value {cls}">{esc(value)}</div>'
        f'<div class="note">{esc(note)}</div></div>'
        for label, value, cls, note in tiles
    )

    review_html = "".join(
        f'<article class="review {"low" if r["rating"] <= 2 else "high" if r["rating"] >= 4 else ""}">'
        f'<div class="head"><span class="who">{esc(r["author"])}</span>'
        f'<span class="stars" aria-label="{r["rating"]} out of 5">{stars(r["rating"])}</span>'
        f'<span class="chip">{esc(r["platform"])}</span>'
        + ('<span class="chip warn">Needs a reply</span>' if r["rating"] <= 2 else "")
        + f'<span class="meta">{esc(r["date"])}</span></div>'
        + (f'<p class="quote"><strong>{esc(r["title"])}</strong> — {esc(r["text"][:260])}'
           f'{"…" if len(r["text"]) > 260 else ""}</p>'
           if r["title"] else
           f'<p class="quote">{esc(r["text"][:260])}{"…" if len(r["text"]) > 260 else ""}</p>')
        + (f'<p class="quote"><a href="{esc(r["url"])}">Read on {esc(r["platform"])}</a></p>'
           if r["url"] else "")
        + "</article>"
        for r in d["reviews"]
    )

    return f"""<div class="wrap">
  <header class="masthead">
    <div>
      <p class="eyebrow">Review monitor</p>
      <h1>{esc(d["hotel"])}</h1>
    </div>
    <div class="feeds">{feeds}</div>
  </header>

  <section class="tiles">{tile_html}</section>

  <section class="grid2">
    <div class="card">
      <h2>Rating mix</h2>
      <p class="sub">Every review on record, by star rating</p>
      <figure id="dist"></figure>
    </div>
    <div class="card">
      <h2>Reviews per month</h2>
      <p class="sub">Volume by platform, last {MONTHS_SHOWN} months with activity</p>
      <div class="legend">
        <span><span class="swatch" style="background:var(--series-1)"></span>Google</span>
        <span><span class="swatch" style="background:var(--series-2)"></span>TripAdvisor</span>
      </div>
      <figure id="months"></figure>
      <button class="toggle" id="tableBtn" type="button" aria-expanded="false">Show data table</button>
      <div id="monthTable" hidden></div>
    </div>
  </section>

  <section>
    <h2 style="font-family:var(--display);font-size:17px;margin:0 0 12px;">Latest reviews</h2>
    <div class="reviews">{review_html or '<p class="empty">No reviews recorded yet.</p>'}</div>
  </section>

  <footer>
    <span>Updated {esc(d["generated"])} · tracking since {esc(d["tracking_since"])}</span>
    <span>History builds forward from the first run: Google exposes only its 5 newest reviews
          and Tripadvisor's Discover tier only 3, so earlier reviews cannot be backfilled.</span>
    <span>Reviews and bubble ratings provided by Tripadvisor.</span>
  </footer>
</div>
<div id="tip" role="status"></div>
<script>
const DATA = {json.dumps({k: d[k] for k in ("distribution", "months")})};
const tip = document.getElementById("tip");
const showTip = (evt, html) => {{
  tip.innerHTML = html;
  tip.style.opacity = 1;
  const pad = 12;
  tip.style.left = Math.min(evt.clientX + pad, innerWidth - tip.offsetWidth - pad) + "px";
  tip.style.top = Math.max(evt.clientY - tip.offsetHeight - pad, pad) + "px";
}};
const hideTip = () => {{ tip.style.opacity = 0; }};

function distChart(el, rows) {{
  const total = rows.reduce((s, r) => s + r.count, 0);
  if (!total) {{ el.innerHTML = '<p class="empty">No ratings recorded yet.</p>'; return; }}
  const max = Math.max(...rows.map(r => r.count));
  const rowH = 30, labelW = 34, valueW = 30, w = 320;
  const h = rows.length * rowH;
  const track = w - labelW - valueW;
  const bars = rows.map((r, i) => {{
    const y = i * rowH + 6;
    const len = max ? Math.max(r.count / max * track, r.count ? 3 : 0) : 0;
    return `<g class="bar" data-t="${{r.stars}} star · ${{r.count}} review${{r.count === 1 ? "" : "s"}}">
      <text class="tick" x="0" y="${{y + 13}}">${{r.stars}}★</text>
      <rect x="${{labelW}}" y="${{y}}" width="${{track}}" height="18" rx="3" fill="var(--rule)"></rect>
      <rect x="${{labelW}}" y="${{y}}" width="${{len}}" height="18" rx="3" fill="var(--series-1)"></rect>
      <text class="datalabel" x="${{labelW + track + 6}}" y="${{y + 13}}">${{r.count}}</text>
    </g>`;
  }}).join("");
  el.innerHTML = `<svg viewBox="0 0 ${{w}} ${{h}}" role="img"
    aria-label="Rating distribution: ${{rows.map(r => r.stars + " star, " + r.count).join("; ")}}">${{bars}}</svg>`;
}}

function monthChart(el, rows) {{
  if (rows.length < 2) {{
    el.innerHTML = '<p class="empty">Building history — a month-by-month trend appears once there are two months of data.</p>';
    return;
  }}
  const w = 340, h = 170, padL = 22, padB = 24, padT = 8;
  const max = Math.max(...rows.map(r => r.Google + r.TripAdvisor), 1);
  const band = (w - padL) / rows.length;
  const barW = Math.min(band * 0.55, 44);
  const scale = v => (h - padB - padT) * (v / max);
  const ticks = [0, Math.ceil(max / 2), max].filter((v, i, a) => a.indexOf(v) === i);
  const grid = ticks.map(t => {{
    const y = h - padB - scale(t);
    return `<line x1="${{padL}}" x2="${{w}}" y1="${{y}}" y2="${{y}}" stroke="var(--rule)" stroke-width="1"></line>
            <text class="tick" x="0" y="${{y + 4}}">${{t}}</text>`;
  }}).join("");
  const bars = rows.map((r, i) => {{
    const x = padL + band * i + (band - barW) / 2;
    const gH = scale(r.Google), tH = scale(r.TripAdvisor);
    const gY = h - padB - gH;
    const tY = gY - tH - (gH && tH ? 2 : 0);
    return `<g class="bar" data-t="<b>${{r.label}}</b><br>Google ${{r.Google}} · TripAdvisor ${{r.TripAdvisor}}">
      <rect x="${{x}}" y="${{gY}}" width="${{barW}}" height="${{gH}}" rx="3" fill="var(--series-1)"></rect>
      <rect x="${{x}}" y="${{tY}}" width="${{barW}}" height="${{tH}}" rx="3" fill="var(--series-2)"></rect>
      <text class="tick" x="${{x + barW / 2}}" y="${{h - 8}}" text-anchor="middle">${{r.label}}</text>
    </g>`;
  }}).join("");
  el.innerHTML = `<svg viewBox="0 0 ${{w}} ${{h}}" role="img"
    aria-label="Reviews per month: ${{rows.map(r => r.label + ", " + (r.Google + r.TripAdvisor)).join("; ")}}">${{grid}}${{bars}}</svg>`;
}}

distChart(document.getElementById("dist"), DATA.distribution);
monthChart(document.getElementById("months"), DATA.months);

document.querySelectorAll(".bar").forEach(g => {{
  g.addEventListener("mousemove", e => showTip(e, g.dataset.t));
  g.addEventListener("mouseleave", hideTip);
}});

const btn = document.getElementById("tableBtn"), box = document.getElementById("monthTable");
box.innerHTML = `<table><thead><tr><th>Month</th><th>Google</th><th>TripAdvisor</th><th>Total</th></tr></thead>
  <tbody>${{DATA.months.map(r => `<tr><td>${{r.label}}</td><td>${{r.Google}}</td>
  <td>${{r.TripAdvisor}}</td><td>${{r.Google + r.TripAdvisor}}</td></tr>`).join("")}}</tbody></table>`;
btn.addEventListener("click", () => {{
  const open = box.hidden;
  box.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  btn.textContent = open ? "Hide data table" : "Show data table";
}});
</script>
"""


def full_page(content):
    """Standalone document for GitHub Pages.

    The Artifact variant omits this wrapper entirely — the Artifact runtime
    supplies its own doctype, head and body, so the published file is just
    HEAD + content.
    """
    return ('<!doctype html>\n<html lang="en">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'{HEAD}\n</head>\n<body>\n{content}\n</body>\n</html>\n')


def main():
    with open(HISTORY_FILE) as f:
        history = json.load(f)
    data = summarise(history)
    content = render_content(data)

    if "--artifact" in sys.argv:
        path = sys.argv[sys.argv.index("--artifact") + 1]
        with open(path, "w") as f:
            f.write(HEAD + "\n" + content)
        print(f"Wrote artifact page: {path}")
        return

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(full_page(content))
    print(f"Wrote {OUTPUT_FILE} — {data['total']} reviews, "
          f"{len(data['months'])} months, average {data['average']}")


if __name__ == "__main__":
    main()
