#!/usr/bin/env python3
"""
Ticket-booking watcher.

Polls a URL (a BookMyShow / District showtimes page, or an internal API
request you grabbed from your browser's DevTools) and sends a Telegram
message the moment a given theatre appears with booking open.

State is tracked in state.json so you get alerted on the *transition*
to "available" instead of on every run.

Everything is driven by config.json (and/or environment variables), so
nothing site-specific is hardcoded -- if BookMyShow/District change their
markup you only edit config, not code.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

# Look like a real Chrome on Windows -- BMS rejects obvious bots.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH, default={}) or {}

    # Environment variables override the file (used by GitHub Actions secrets).
    env_map = {
        "TARGET_URL": "target_url",
        "THEATRE": "theatre",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "NTFY_TOPIC": "ntfy_topic",
        "NTFY_SERVER": "ntfy_server",
    }
    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    if os.environ.get("HEADERS_JSON"):
        cfg["headers"] = json.loads(os.environ["HEADERS_JSON"])

    # The BMS date is embedded in the URL, so build the URL from the template
    # and the (possibly overridden) requested_date. Set REQUESTED_DATE=20260717
    # to point everything at the 17th for a live end-to-end test.
    if cfg.get("url_template") and cfg.get("requested_date"):
        cfg["target_url"] = cfg["url_template"].format(date=cfg["requested_date"])

    required = ["target_url"]
    detector = cfg.get("detector")
    if detector in ("bms_date", "venue_date", "any_venue_date", "venue_dates"):
        required.append("requested_date")
    elif detector not in ("venue_date", "venue_dates"):
        required.append("theatre")
    if detector in ("venue_date", "venue_dates") and not (
            cfg.get("venue_code") or cfg.get("venue_codes")):
        sys.exit(f"{detector} detector needs 'venue_code' or 'venue_codes'")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}")

    # At least one notification channel has to be usable, otherwise the poller
    # would happily detect the opening and tell nobody.
    has_telegram = cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")
    if not (cfg.get("ntfy_topic") or has_telegram):
        sys.exit(
            "No notification channel configured. Set NTFY_TOPIC "
            "(recommended) and/or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID."
        )
    return cfg


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=30,
    )
    resp.raise_for_status()


def send_ntfy(cfg, text):
    """
    Push to the user's phone via ntfy.sh.

    ntfy needs no account and no API key: you pick an unguessable topic
    name, subscribe to it in the ntfy app, and anyone who POSTs to
    https://ntfy.sh/<topic> makes your phone buzz. That's the whole setup,
    which is why it beats Telegram bots or WhatsApp relays here.

    Because the public server has no auth, the topic name IS the secret --
    keep it random and don't paste it anywhere public.
    """
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": cfg.get("ntfy_title", "Tickets are open"),
        "Priority": "urgent",
        "Tags": "clapper,tickets",
    }
    if cfg.get("target_url"):
        headers["Click"] = cfg["target_url"]
    resp = requests.post(
        f"{server}/{cfg['ntfy_topic']}",
        data=text.encode("utf-8"),
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()


def notify(cfg, text):
    """
    Fan the alert out to every channel that's configured.

    Returns True if at least one channel accepted the message. The caller
    only persists the "already alerted" state on True, so a total delivery
    failure means the next run tries again instead of silently swallowing
    the one alert you cared about.
    """
    channels = []
    if cfg.get("ntfy_topic"):
        channels.append(("ntfy", lambda: send_ntfy(cfg, text)))
    if cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
        channels.append(
            ("telegram", lambda: send_telegram(
                cfg["telegram_bot_token"], cfg["telegram_chat_id"], text))
        )

    delivered = False
    for name, send in channels:
        for attempt in range(1, 4):
            try:
                send()
                print(f"  -> {name}: sent")
                delivered = True
                break
            except Exception as exc:  # noqa: BLE001 - one channel must not kill the rest
                print(f"  -> {name}: attempt {attempt}/3 failed: {exc}")
                if attempt < 3:
                    time.sleep(5 * attempt)
    return delivered


def fetch(cfg):
    """
    Fetch the target URL from an India egress when configured.

    BookMyShow blocks non-India / datacenter IPs (e.g. GitHub's US runners),
    so a plain request from CI gets a 403. Two ways to route through India:

    * SCRAPERAPI_KEY  -- routes via ScraperAPI with country_code=in and solves
                         anti-bot. Easiest for CI. Set it as a repo secret.
    * PROXY_URL       -- a standard http(s) proxy string, e.g.
                         "http://user:pass@in-proxy-host:port".

    With neither set, it makes a direct request with browser headers plus a
    cookie warm-up -- enough only when running from an India IP.
    """
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers", {}))

    scraper_key = os.environ.get("SCRAPERAPI_KEY")
    if scraper_key:
        api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
            {"api_key": scraper_key, "country_code": "in", "url": cfg["target_url"]}
        )
        resp = requests.get(api_url, timeout=90)
        resp.raise_for_status()
        return resp.text

    proxy = os.environ.get("PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    session = requests.Session()
    session.headers.update(headers)

    # Warm-up: hit the homepage first to pick up cookies (helps soft bot checks).
    try:
        session.get("https://in.bookmyshow.com/", timeout=30, proxies=proxies)
    except requests.RequestException:
        pass

    resp = session.get(
        cfg["target_url"],
        timeout=30,
        proxies=proxies,
        headers={"Referer": "https://in.bookmyshow.com/explore/movies-chennai"},
    )
    resp.raise_for_status()
    return resp.text


def is_available_bms_date(page_text, cfg):
    """
    BookMyShow-specific detector for "a given date has opened for booking".

    BMS only renders showtimes for the date currently being displayed, and it
    silently falls back to the nearest available date when you request a date
    that hasn't opened yet. So the requested date (e.g. 20260720) sits at a
    low ~3 count (just the date-strip navigation) until it opens, at which
    point its showtimes render and it becomes the *dominant* date token.

    Rule: open when the requested date is the most-referenced date token on
    the page and it clears a small floor (well above strip-only noise).
    """
    requested = cfg["requested_date"]  # e.g. "20260720"
    floor = cfg.get("min_references", 10)

    tokens = re.findall(r"20\d{6}", page_text)
    if not tokens:
        return False

    counts = Counter(tokens)
    top_date, _ = counts.most_common(1)[0]
    requested_count = counts.get(requested, 0)

    return top_date == requested and requested_count >= floor


def is_available_venue_date(page_text, cfg):
    """
    Theatre-specific detector: is a given venue bookable on a given date?

    BMS renders a per-venue booking link like
        /cinemas/chennai/<slug>/buytickets/<venueCode>/<date>
    only when that venue has live shows for that exact date. Because the date
    is baked into the link, it can't be confused with the silent fallback
    (a fallback page carries /<code>/<fallbackDate>, not /<code>/<ourDate>).

    Set venue_code (one) or venue_codes (list). With a list, it's open when
    ANY of them is bookable for the date.
    """
    date = cfg["requested_date"]
    codes = cfg.get("venue_codes") or [cfg["venue_code"]]
    return any("/{}/{}".format(code, date) in page_text for code in codes)


def is_available_any_venue_date(page_text, cfg):
    """
    "Booking opened at ANY theatre in this city, for this exact date."

    BMS renders a per-venue booking link like
        /cinemas/<city>/<slug>/buytickets/<venueCode>/<date>
    only for venues that actually have live shows on that exact date. Before
    booking opens there are zero such links; the moment the city goes live
    there are dozens.

    This is immune to the silent date-fallback (a fallback page carries
    /<code>/<fallbackDate>, never /<code>/<ourDate>) and, unlike the
    date-token-frequency heuristic, it doesn't false-positive on a
    pre-release page that merely echoes the requested date in its URL,
    canonical tag and date strip.

    `min_venues` (default 1) is how many distinct theatres must be live.
    """
    date = cfg["requested_date"]
    codes = set(re.findall(r"/buytickets/([A-Z0-9]{4})/" + re.escape(date), page_text))
    return len(codes) >= cfg.get("min_venues", 1)


def is_available_venue_dates(page_text, cfg):
    """
    Watch specific theatres across one or more dates -- in a single request.

    Two BMS behaviours make this work:

    * A per-venue link /cinemas/<city>/<slug>/buytickets/<CODE>/<date> is
      rendered only when that venue has live shows on that exact date.
    * When you ask for a date that hasn't opened, BMS silently falls back to
      the nearest date that HAS opened, and renders that date's links.

    So requesting the release date also surfaces an earlier premiere date if
    that's what opened first. That matters for Telugu releases, where paid
    premieres on the eve routinely open before the release day itself -- and
    it costs no extra requests, which keeps us inside ScraperAPI's free tier.

    Records every (venue, date) hit in cfg["_hits"] so the alert can say
    which theatre and which date actually opened.
    """
    codes = cfg.get("venue_codes") or [cfg["venue_code"]]
    dates = set(cfg.get("watch_dates") or [cfg["requested_date"]])

    hits = []
    for code in codes:
        for m in re.finditer(r"/buytickets/%s/(\d{8})" % re.escape(code), page_text):
            if m.group(1) in dates:
                hit = (code, m.group(1))
                if hit not in hits:
                    hits.append(hit)
    cfg["_hits"] = hits
    return bool(hits)


def is_available(page_text, cfg):
    detector = cfg.get("detector")
    if detector == "venue_dates":
        return is_available_venue_dates(page_text, cfg)
    if detector == "any_venue_date":
        return is_available_any_venue_date(page_text, cfg)
    if detector == "venue_date":
        return is_available_venue_date(page_text, cfg)
    if detector == "bms_date":
        return is_available_bms_date(page_text, cfg)
    return is_available_generic(page_text, cfg)


def is_available_generic(page_text, cfg):
    """
    Booking is considered OPEN for the target theatre when the theatre name
    is present AND at least one 'booking is live' signal is present.

    Matching is case-insensitive and ignores extra whitespace so small
    formatting differences don't cause misses.
    """
    haystack = re.sub(r"\s+", " ", page_text).lower()

    theatre = re.sub(r"\s+", " ", cfg["theatre"]).lower().strip()
    if theatre not in haystack:
        return False

    # If the movie name is configured, require it too (avoids false hits when
    # the theatre is listed for other movies).
    movie = cfg.get("movie")
    if movie:
        if re.sub(r"\s+", " ", movie).lower().strip() not in haystack:
            return False

    # Signals that booking is actually live rather than "coming soon".
    open_signals = cfg.get(
        "open_signals",
        ["book tickets", "book now", '"showtimes"', "showtime", "select seats"],
    )
    # Signals that it's NOT yet open -- if present near-exclusively, treat as closed.
    closed_signals = cfg.get("closed_signals", ["notify me", "coming soon"])

    has_open = any(s.lower() in haystack for s in open_signals)
    only_closed = any(s.lower() in haystack for s in closed_signals) and not has_open

    return has_open and not only_closed


def main():
    cfg = load_config()
    state = load_json(STATE_PATH, default={"available": False}) or {"available": False}

    target_desc = cfg.get("theatre") or cfg.get("requested_date", "target")
    label = f"{cfg.get('movie', 'movie')} @ {target_desc}"

    try:
        page = fetch(cfg)
    except requests.RequestException as exc:
        # Transient network/blocking errors shouldn't crash the workflow.
        print(f"[{label}] fetch failed: {exc}")
        return 0

    available = is_available(page, cfg)
    print(f"[{label}] available={available} (was {state.get('available')})")

    if available and not state.get("available"):
        if cfg.get("detector") == "venue_dates":
            names = cfg.get("venue_names", {})
            lines = []
            for code, d in cfg.get("_hits", []):
                pretty = f"{d[6:8]}-{d[4:6]}-{d[0:4]}"
                lines.append(f"- {names.get(code, code)} - {pretty}")
            msg = (
                f"BOOKING OPEN: {cfg.get('movie', 'Movie')}\n\n"
                + "\n".join(lines)
                + f"\n\nBook: {cfg['target_url']}"
            )
        elif cfg.get("detector") in ("bms_date", "venue_date", "any_venue_date"):
            rd = cfg["requested_date"]
            pretty = f"{rd[6:8]}-{rd[4:6]}-{rd[0:4]}"
            venue = cfg.get("venue_label") or cfg.get("venue_code") or ""
            venue_line = f"Theatre: {venue}\n" if venue else ""
            msg = (
                f"🎬 Booking just OPENED!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"{venue_line}"
                f"Date: {pretty}\n\n"
                f"Book here: {cfg['target_url']}"
            )
        else:
            msg = (
                f"🎬 Booking is OPEN!\n\n"
                f"{cfg.get('movie', 'Movie')}\n"
                f"Theatre: {cfg['theatre']}\n\n"
                f"Book here: {cfg['target_url']}"
            )
        if not notify(cfg, msg):
            # Nothing got through. Leave state untouched so the next run
            # re-attempts the alert rather than losing it.
            print(f"[{label}] ALL notification channels failed -- not "
                  f"persisting state, will retry next run")
            return 1
        print(f"[{label}] notification sent")

    # Persist current state so we don't re-alert every run.
    if available != state.get("available"):
        state["available"] = available
        state["checked_at"] = int(time.time())
        save_json(STATE_PATH, state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
