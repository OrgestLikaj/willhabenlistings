"""
willhaben.at rental apartment watcher.

Fetches a filtered willhaben search, applies text rules to the listing title,
advertiser name and (optionally) the full description from the detail page,
scores what survives, and sends a Telegram message for each new match.

Local usage (Windows cmd):
    set DEBUG=1
    python scraper.py

With notifications:
    set TG_TOKEN=8942496497:AAF...
    set TG_CHAT_ID=7530443269
    python scraper.py
"""

import json
import os
import re
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import requests

# ================================================================ SEARCH URL

# Copied verbatim from the browser address bar after clicking "Suchen".
# Building this URL from individual params does NOT work - willhaben resolves
# the area filter from the sfId, so a hand-built URL silently returns the wrong
# region (Vienna listings instead of Lower Austria). To change filters: redo
# the search in the browser and paste the new URL here, or override it with the
# WILLHABEN_URL env var / GitHub repo variable.
#
# Current filters: 11 municipalities, 2 rooms, Terrasse, max 800 EUR, min 40 m2.
DEFAULT_URL = (
    "https://www.willhaben.at/iad/immobilien/mietwohnungen/mietwohnung-angebote"
    "?sfId=aa8f37b4-dcd7-42eb-a49b-57d5c7de3819&isNavigation=true"
    "&areaId=117223&areaId=117224&areaId=117225&areaId=117226&areaId=117227"
    "&areaId=117228&areaId=117229&areaId=117230&areaId=117231&areaId=117242"
    "&areaId=117244"
    "&rows=100"
    "&NO_OF_ROOMS_BUCKET=2X2"
    "&FREE_AREA/FREE_AREA_TYPE=20"
    "&PRICE_TO=800"
    "&ESTATE_SIZE/LIVING_AREA_FROM=40"
)

SEARCH_URL = os.environ.get("WILLHABEN_URL", DEFAULT_URL)

SEEN_FILE = "seen.json"
STATE_FILE = "state.json"     # remembers when the previous run finished
TZ_NAME = "Europe/Vienna"

# Notification policy. Every new listing is reported - nothing is silently
# dropped. Below MAX_INDIVIDUAL they get one rich message each; above that they
# are batched into digests of DIGEST_CHUNK so a big backlog does not turn into
# 90 separate phone buzzes.
MAX_INDIVIDUAL = 15
DIGEST_CHUNK = 10

# Send a "no new listings" message covering the window since the last run.
HEARTBEAT = True
# Heartbeats arrive silently (no sound/vibration); real listings do not.
HEARTBEAT_SILENT = True

# --- email (second channel, sent in addition to Telegram) ---
# One email per run containing every new listing, not one email per listing.
EMAIL_ENABLED = True
EMAIL_TO = "orgest.likaj23@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465                     # 465 = implicit SSL
# Credentials come from the environment / GitHub secrets:
#   SMTP_USER = the sending Gmail address
#   SMTP_PASS = a Google App Password (NOT your account password)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ============================================================ NUMERIC FILTERS

MAX_PRICE = 1000        # keep in sync with PRICE_TO in the search URL
MIN_AREA = 40           # keep in sync with LIVING_AREA_FROM in the URL
MIN_ROOMS = 0            # 0 = no room filter

# Postal codes you will actually consider. Empty list = no location check.
# This is a safety net: if the search URL's sfId expires or points at the wrong
# region, everything gets rejected on location instead of quietly alerting you
# about apartments in a city you are not looking in.
#
# Vienna:              1010, 1020, 1030 ... 1230 (districts 1-23)
# Gaenserndorf area:   2231, 2232, 2241, 2244, 2251, 2262, 2273, 2285, 2295
#
# Uncomment ONE of these, or leave the empty list to disable the check.
ALLOWED_POSTCODES = []
# ALLOWED_POSTCODES = [1010, 1020, 1030, 1040, 1050, 1060, 1070, 1080, 1090,
#                      1200, 1220]
# ALLOWED_POSTCODES = [2231, 2232, 2241, 2244, 2251, 2262, 2273, 2285, 2295]

# =============================================================== TEXT FILTERS
#
# All patterns are regexes matched case-insensitively against FOLDED text,
# where umlauts are normalised (ü->ue, ö->oe, ä->ae, ß->ss). Write patterns in
# the folded form: "kueche", not "küche".
#
# Plain words match as substrings, so "garage" also matches "Garagenplatz".
# Use \b...\b when you need a whole word, e.g. r"\bgbv\b".

# --- hard rejects ---------------------------------------------------------
EXCLUDE = [
    # not eligible: Gemeindebau / Genossenschaft
    # NOTE: never match bare "gemeinde" - "Marktgemeinde Strasshof" is normal
    #       location text and would reject almost everything.
    r"(?<!keine )(?<!kein )gemeindebau",
    r"(?<!keine )gemeindewohnung",
    r"gemeinde[- ]?bau[- ]?wohnung",
    r"wiener wohnen",
    r"(?<!keine )(?<!kein )genossenschaft",   # -swohnung, -sbau, Bau-, Wohnungs-
    r"genossenschaftlich",
    r"\bgbv\b",
    r"\bwgg\b",
    r"wohnungsgemeinnuetzigkeitsgesetz",
    r"gemeinnuetzige? (bauvereinigung|bautraeger|wohnbau)",
    r"siedlungsgenossenschaft|siedlungsverein|wohnbauvereinigung",

    # financial fingerprints of the co-op sector
    r"(?<!kein )(?<!keine )(?<!ohne )finanzierungsbeitrag",
    r"(?<!kein )(?<!keine )(?<!ohne )baukostenbeitrag",
    r"(?<!kein )(?<!keine )(?<!ohne )eigenmittelanteil",
    r"nutzungsvertrag",       # co-ops issue this instead of a Mietvertrag
    r"wohnberechtigungsschein|vormerkschein|anspruchsberechtigung",

    # known non-profit landlords / developers in Lower Austria
    r"\bebg\b|gedesag|alpenland|\bwet\b|neue heimat|sozialbau|heimat oesterreich",

    # wrong product entirely
    r"(?<!un)befristet",
    r"untermiete",
    r"wg[- ]?zimmer",
    r"garconniere",
    r"zimmer zu vermieten",
    r"nur an studenten",
]

# Set True if you also want subsidised (geförderte) apartments filtered out.
# These are a SEPARATE category from Genossenschaft - income limits plus a
# Wohnbauförderung requirement. Some people qualify for these but not co-ops,
# so this is off by default.
EXCLUDE_GEFOERDERT = False

GEFOERDERT_PATTERNS = [
    r"(?<!nicht )gefoerdert",
    r"wohnbaufoerderung|wohnungsfoerderung",
    r"einkommensgrenze|einkommensnachweis erforderlich",
    r"foerderungswuerdig|foerderzusicherung",
]

# --- optional requirements (empty = not enforced) -------------------------
REQUIRE_ALL = [
    # every pattern must appear, e.g. r"unbefristet"
]

REQUIRE_ANY = [
    # at least one must appear, e.g. r"garage|stellplatz|carport|parkplatz"
]

# --- scoring bonuses (negative values are penalties) ---------------------
BONUS = {
    r"erstbezug|neubau": 10,
    r"unbefristet": 10,
    r"garage|stellplatz|carport|parkplatz": 8,
    r"provisionsfrei|keine provision|ohne provision": 8,
    r"(?<!kein )(?<!keine )keller|kellerabteil|abstellraum": 5,
    r"kueche": 4,
    r"bahnhof|\bbahn\b|\bbus\b": 4,
    r"(?<!keine )haustiere?( sind)? erlaubt|hunde erlaubt|katzen erlaubt": 6,
    r"fussbodenheizung|waermepumpe|photovoltaik": 5,
    r"ablöse|abloese": -15,
    r"sanierungsbedarf|renovierungsbedarf": -10,
}

# --- detail page fetching -------------------------------------------------
# The search results only give a short headline. Most of what the text rules
# above look for lives in the full description on each listing's own page.
FETCH_DETAILS = True
DETAIL_DELAY = 2.0       # seconds between detail requests - do not lower much

# Verification aid: when True, EXCLUDE matches are NOT rejected but scored at
# -200 instead, so they still appear in the console log with the reason. Run
# this way for a day if you want to confirm nothing legitimate is being eaten.
SOFT_EXCLUDE = False


# ================================================================== HELPERS

def now_local():
    """Timezone-aware current time in TZ_NAME, falling back to UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return datetime.now(timezone.utc)


def fmt_dt(dt):
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "?"


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            print(f"{STATE_FILE} is unreadable - starting fresh")
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def parse_dt(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def fold(s):
    """Lowercase and normalise German umlauts so patterns match either spelling."""
    s = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return s


def hit(text, pattern):
    """True if the folded pattern matches the (already folded) text."""
    try:
        return bool(re.search(fold(pattern), text))
    except re.error as e:
        print(f"  bad regex {pattern!r}: {e}")
        return False


def active_excludes():
    pats = list(EXCLUDE)
    if EXCLUDE_GEFOERDERT:
        pats += GEFOERDERT_PATTERNS
    return pats


# ================================================================== FETCHING

def _get(url):
    r = requests.get(url, headers={
        "User-Agent": UA,
        "Accept-Language": "de-AT,de;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }, timeout=30)
    r.raise_for_status()
    return r.text


def _next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


def fetch_listings(url):
    html = _get(url)
    data = _next_data(html)
    if data is None:
        raise RuntimeError("No __NEXT_DATA__ found - blocked or page layout changed")

    if os.environ.get("DEBUG"):
        with open("debug.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print("wrote debug.json")

    print(f"willhaben reports {_find_key(data, 'rowsFound')} total matches")
    return _find_ads(data)


def fetch_detail_text(url):
    """Full description text from a listing's detail page, folded."""
    try:
        html = _get(url)
        data = _next_data(html)
        if data:
            parts = []
            for key in ("BODY_DYN", "description", "PROPERTY_DESCRIPTION"):
                val = _find_key(data, key)
                if val:
                    parts.append(str(val))
            if parts:
                return fold(re.sub(r"<[^>]+>", " ", " ".join(parts)))
        return fold(re.sub(r"<[^>]+>", " ", html))
    except Exception as e:
        print(f"  detail fetch failed for {url}: {e}")
        return ""


def _find_key(obj, key):
    """First scalar value for `key` anywhere in the tree."""
    if isinstance(obj, dict):
        if key in obj and not isinstance(obj[key], (dict, list)):
            return obj[key]
        for v in obj.values():
            got = _find_key(v, key)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_key(v, key)
            if got is not None:
                return got
    return None


def _find_ads(obj):
    """Locate advertSummaryList regardless of how deeply it is nested."""
    if isinstance(obj, dict):
        if "advertSummaryList" in obj and isinstance(obj["advertSummaryList"], dict):
            return obj["advertSummaryList"].get("advertSummary", [])
        for v in obj.values():
            found = _find_ads(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_ads(v)
            if found:
                return found
    return []


# =================================================================== PARSING

def attrs(ad):
    """Flatten willhaben's attribute list into a plain dict."""
    out = {}
    for a in ad.get("attributes", {}).get("attribute", []):
        vals = a.get("values", [])
        out[a["name"]] = vals[0] if len(vals) == 1 else vals
    return out


def to_float(v):
    if v is None or isinstance(v, list):
        return None
    try:
        return float(str(v).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


def parse(ad):
    a = attrs(ad)
    seo = a.get("SEO_URL", "")
    org = (ad.get("organisationDetails") or {}).get("name") or a.get("ORGNAME") or ""
    return {
        "id": str(ad.get("id") or ""),
        "title": (ad.get("description") or a.get("HEADING") or "").strip(),
        "org": str(org),
        "price": to_float(a.get("PRICE")),
        "area": to_float(a.get("ESTATE_SIZE/LIVING_AREA") or a.get("ESTATE_SIZE")),
        "rooms": to_float(a.get("NUMBER_OF_ROOMS")),
        "district": a.get("DISTRICT") or a.get("LOCATION") or "",
        "postcode": to_int(a.get("POSTCODE") or a.get("POSTALCODE")),
        "url": "https://www.willhaben.at/iad/" + seo if seo else "",
    }


# ================================================================= FILTERING

def passes_numeric(l):
    """Cheap checks, run before spending a request on the detail page."""
    if ALLOWED_POSTCODES and l["postcode"] not in ALLOWED_POSTCODES:
        return False
    if l["price"] and l["price"] > MAX_PRICE:
        return False
    if l["area"] and l["area"] < MIN_AREA:
        return False
    if MIN_ROOMS and l["rooms"] and l["rooms"] < MIN_ROOMS:
        return False
    return True


def text_verdict(l):
    """Returns (ok, reasons). ok=False means reject."""
    t = l["text"]
    bad = [p for p in active_excludes() if hit(t, p)]
    if bad and not SOFT_EXCLUDE:
        return False, bad
    if REQUIRE_ALL:
        missing = [p for p in REQUIRE_ALL if not hit(t, p)]
        if missing:
            return False, [f"missing: {p}" for p in missing]
    if REQUIRE_ANY and not any(hit(t, p) for p in REQUIRE_ANY):
        return False, ["none of REQUIRE_ANY matched"]
    return True, bad


def score(l, excluded_patterns=()):
    """Keyword-based only. Price does not influence the score - listings are
    kept in willhaben's own newest-first order (see main)."""
    s = 0.0
    if l["area"]:
        s += min(l["area"], 120) / 10

    matched = []
    for pat, pts in BONUS.items():
        if hit(l["text"], pat):
            s += pts
            matched.append(f"{pat.split('|')[0]}{'' if pts > 0 else f' ({pts})'}")

    if excluded_patterns:
        s -= 200
        matched += [f"EXCLUDED: {p}" for p in excluded_patterns]

    l["matched"] = matched
    return round(s, 1)


# ================================================================ SEEN STATE

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, ValueError):
            print(f"{SEEN_FILE} is unreadable - starting fresh")
    return set()


def save_seen(ids):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=0)


# ============================================================== NOTIFICATION

def _send(text, silent=False):
    """Single Telegram send. Returns True on success."""
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not (token and chat):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False,
                  "disable_notification": bool(silent)},
            timeout=20,
        )
        if not r.ok:
            print(f"  Telegram error {r.status_code}: {r.text}")
            return False
        return True
    except requests.RequestException as e:
        print(f"  Telegram request failed: {e}")
        return False


def have_telegram():
    return bool(os.environ.get("TG_TOKEN") and os.environ.get("TG_CHAT_ID"))


def ppm(l):
    """Rent per square metre, or None."""
    if l.get("price") and l.get("area"):
        return l["price"] / l["area"]
    return None


def fmt_num(v, suffix=""):
    return f"{v:,.0f}{suffix}".replace(",", " ") if v is not None else "-"


def place(l):
    bits = [str(l["postcode"])] if l.get("postcode") else []
    if l.get("district"):
        bits.append(l["district"])
    return " ".join(bits) or "location unknown"


def spec_line(l, sep=" · "):
    """e.g. "890 € · 64 m² · 2 rooms · 13.9 €/m²" """
    parts = [f"{fmt_num(l.get('price'))} €", f"{fmt_num(l.get('area'))} m²"]
    if l.get("rooms"):
        n = int(l["rooms"])
        parts.append(f"{n} room" + ("s" if n != 1 else ""))
    p = ppm(l)
    if p:
        parts.append(f"{p:.1f} €/m²")
    return sep.join(parts)


def duration(a, b):
    mins = max(0, int((b - a).total_seconds() // 60))
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m"


def listing_message(l):
    msg = (f"🏠 <b>{l['title']}</b>\n"
           f"{spec_line(l)}\n"
           f"📍 {place(l)}")
    if l.get("matched"):
        msg += f"\n✨ {', '.join(l['matched'])[:200]}"
    return msg + f"\n\n<a href=\"{l['url']}\">View on willhaben →</a>"


def have_email():
    return bool(EMAIL_ENABLED and EMAIL_TO
                and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))


def _send_email(subject, html, text_fallback=""):
    """Send one HTML email. Returns True on success."""
    if not have_email():
        return False
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = EMAIL_TO
    msg.set_content(text_fallback or re.sub(r"<[^>]+>", "", html))
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        print(f"emailed: {subject}")
        return True
    except Exception as e:
        print(f"  email failed: {type(e).__name__}: {e}")
        return False


def _email_style():
    return ("font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
            "font-size:15px;color:#1a1a1a;line-height:1.5")


def listings_email_html(new, window_from, window_to):
    rows = []
    for i, l in enumerate(new, 1):
        tags = ""
        if l.get("matched"):
            chips = "".join(
                f'<span style="display:inline-block;background:#e7f5ec;'
                f'color:#0a7d2c;border-radius:10px;padding:2px 8px;'
                f'font-size:12px;margin:2px 4px 2px 0">{m}</span>'
                for m in l["matched"])
            tags = f'<div style="margin-top:6px">{chips}</div>'
        rows.append(
            f'<tr><td style="padding:16px 0;border-bottom:1px solid #eaeaea">'
            f'<div style="color:#999;font-size:12px">#{i}</div>'
            f'<a href="{l["url"]}" style="font-weight:600;color:#0b57d0;'
            f'text-decoration:none;font-size:17px">{l["title"]}</a>'
            f'<div style="color:#222;margin-top:6px;font-size:15px">'
            f'{spec_line(l, sep=" &nbsp;·&nbsp; ")}</div>'
            f'<div style="color:#666;margin-top:2px;font-size:14px">'
            f'📍 {place(l)}</div>'
            f'{tags}'
            f'<div style="margin-top:8px">'
            f'<a href="{l["url"]}" style="color:#0b57d0;font-size:13px;'
            f'text-decoration:none">View on willhaben &rarr;</a></div>'
            f'</td></tr>')
    n = len(new)
    return (f'<div style="{_email_style()};max-width:640px">'
            f'<h2 style="margin:0 0 2px;font-size:21px">'
            f'{n} new listing{"s" if n != 1 else ""}</h2>'
            f'<div style="color:#666;font-size:13px;margin-bottom:6px">'
            f'Since last check: {fmt_dt(window_from)} &rarr; {fmt_dt(window_to)} '
            f'({duration(window_from, window_to)})</div>'
            f'<table style="width:100%;border-collapse:collapse">'
            f'{"".join(rows)}</table>'
            f'<div style="color:#999;font-size:12px;margin-top:14px">'
            f'Newest first. Sent by your willhaben watcher.</div>'
            f'</div>')


def listings_email_text(new, window_from, window_to):
    n = len(new)
    lines = [f"{n} new listing{'s' if n != 1 else ''} on willhaben",
             f"Since last check: {fmt_dt(window_from)} -> {fmt_dt(window_to)}"
             f" ({duration(window_from, window_to)})",
             "", "-" * 60, ""]
    for i, l in enumerate(new, 1):
        lines += [f"{i}. {l['title']}",
                  f"   {spec_line(l, sep='  |  ')}",
                  f"   {place(l)}"]
        if l.get("matched"):
            lines.append(f"   Matches: {', '.join(l['matched'])}")
        lines += [f"   {l['url']}", ""]
    lines += ["-" * 60,
              "Newest first. Sent by your willhaben watcher."]
    return "\n".join(lines)


def heartbeat_email_text(window_from, window_to, stats):
    lines = [
        "No new listings",
        f"Checked {fmt_dt(window_from)} -> {fmt_dt(window_to)}"
        f" ({duration(window_from, window_to)})",
        "",
        f"  {stats.get('total', '?')} listings matched your willhaben search",
        f"  {stats.get('eligible', 0)} met your price, size and location limits",
        f"  {stats.get('new', 0)} were new since the last check",
    ]
    if stats.get("rejected"):
        lines.append(f"  {stats['rejected']} filtered out by your keyword rules")
    lines += ["", "Nothing to do. Next check in about an hour."]
    return "\n".join(lines)


def heartbeat_email_html(window_from, window_to, stats):
    def row(label, value):
        return (f'<tr><td style="padding:5px 14px 5px 0;color:#666;'
                f'font-size:14px">{label}</td>'
                f'<td style="padding:5px 0;font-weight:600;font-size:14px">'
                f'{value}</td></tr>')
    extra = ""
    if stats.get("rejected"):
        extra = (f'<div style="color:#666;font-size:13px;margin-top:12px">'
                 f'{stats["rejected"]} new listing'
                 f'{"s were" if stats["rejected"] != 1 else " was"} filtered out '
                 f'by your keyword rules.</div>')
    return (f'<div style="{_email_style()};max-width:640px">'
            f'<h2 style="margin:0 0 2px;font-size:21px">No new listings</h2>'
            f'<div style="color:#666;font-size:13px;margin-bottom:14px">'
            f'Checked {fmt_dt(window_from)} &rarr; {fmt_dt(window_to)} '
            f'({duration(window_from, window_to)})</div>'
            f'<table style="border-collapse:collapse">'
            f'{row("Matched your search", stats.get("total", "?"))}'
            f'{row("Within price / size / location", stats.get("eligible", 0))}'
            f'{row("New since last check", stats.get("new", 0))}'
            f'</table>{extra}'
            f'<div style="color:#999;font-size:12px;margin-top:16px">'
            f'Nothing to do. Next check in about an hour.</div>'
            f'</div>')


def notify_listings(new, window_from, window_to):
    """Send every new listing. Individually if few, batched into digests if many."""
    _send_email(f"[willhaben] {len(new)} new listing{'s' if len(new) != 1 else ''}",
                listings_email_html(new, window_from, window_to),
                listings_email_text(new, window_from, window_to))
    if not have_email():
        print("No SMTP credentials set - skipping email")

    if not have_telegram():
        print("No Telegram credentials set - skipping notifications")
        return

    n = len(new)
    header = (f"🔔 <b>{n} new listing{'s' if n != 1 else ''}</b>\n"
              f"Since last check: {fmt_dt(window_from)} → {fmt_dt(window_to)}")

    if len(new) <= MAX_INDIVIDUAL:
        _send(header)
        time.sleep(1.0)
        for l in new:
            _send(listing_message(l))
            time.sleep(1.5)      # stay under the per-chat rate limit
        print(f"sent {len(new)} individual messages")
        return

    # too many for one-per-message: batch them so nothing is dropped
    chunks = [new[i:i + DIGEST_CHUNK] for i in range(0, len(new), DIGEST_CHUNK)]
    _send(header + f"\nSent in {len(chunks)} parts to keep things readable.")
    time.sleep(1.0)
    for idx, chunk in enumerate(chunks, 1):
        lines = [f"<b>Part {idx}/{len(chunks)}</b>"]
        for l in chunk:
            lines.append(
                f"• <a href=\"{l['url']}\">{l['title'][:70]}</a>\n"
                f"  {spec_line(l)} — {place(l)}")
        _send("\n".join(lines))
        time.sleep(1.5)
    print(f"sent {len(new)} listings in {len(chunks)} digest messages")


def notify_heartbeat(window_from, window_to, stats):
    """Tell the user nothing new turned up in this window."""
    _send_email("[willhaben] no new listings",
                heartbeat_email_html(window_from, window_to, stats),
                heartbeat_email_text(window_from, window_to, stats))

    if not have_telegram():
        print("No Telegram credentials set - skipping heartbeat")
        return
    msg = (f"🕰 <b>No new listings</b>\n"
           f"Checked {fmt_dt(window_from)} → {fmt_dt(window_to)} "
           f"({duration(window_from, window_to)})\n"
           f"\n"
           f"{stats.get('total', '?')} listings matched your willhaben search, "
           f"{stats.get('eligible', 0)} met your price, size and location limits, "
           f"and none of them were new since the last check.")
    if stats.get("rejected"):
        msg += (f"\n\n{stats['rejected']} new one"
                f"{'s were' if stats['rejected'] != 1 else ' was'} filtered out "
                f"by your keyword rules (Gemeindebau / Genossenschaft etc).")
    _send(msg, silent=HEARTBEAT_SILENT)
    print("sent heartbeat")


# ====================================================================== MAIN

def main():
    started = now_local()
    state = load_state()
    prev_run = parse_dt(state.get("last_run")) or started
    print(f"run started {fmt_dt(started)} (previous run: "
          f"{fmt_dt(parse_dt(state.get('last_run')))})")

    print(f"Fetching: {SEARCH_URL}")
    ads = fetch_listings(SEARCH_URL)
    print(f"Fetched {len(ads)} raw listings")

    listings = [parse(a) for a in ads]
    if not ALLOWED_POSTCODES:
        codes = sorted({l["postcode"] for l in listings if l["postcode"]})
        print(f"postcode check OFF - results are in: {codes}")
    total_raw = len(listings)
    listings = [l for l in listings if l["id"] and passes_numeric(l)]
    print(f"{len(listings)} passed price/size/location checks")

    seen = load_seen()
    candidates = [l for l in listings if l["id"] not in seen]
    print(f"{len(candidates)} are new - applying text rules"
          f"{' (SOFT_EXCLUDE on)' if SOFT_EXCLUDE else ''}")

    kept, rejected = [], 0
    for l in candidates:
        l["text"] = fold(l["title"] + " " + l["org"])
        if FETCH_DETAILS and l["url"]:
            l["text"] += " " + fetch_detail_text(l["url"])
            time.sleep(DETAIL_DELAY)

        ok, reasons = text_verdict(l)
        if ok:
            l["score"] = score(l, reasons if SOFT_EXCLUDE else ())
            kept.append(l)
        else:
            rejected += 1
            print(f"  rejected [{', '.join(reasons)}]: {l['title']}")

    # willhaben returns newest first; a fresher listing beats a higher score.
    # Uncomment to rank by keyword score instead:
    # kept.sort(key=lambda x: -x["score"])
    print(f"{len(kept)} passed everything (newest first)")
    for l in kept:
        print(f"  [{l['score']:>7}] {l['price']} EUR  {l['area']} m2  "
              f"{l['postcode'] or '?'} {l['district']} - {l['title']}")
        print(f"            matched: {', '.join(l['matched']) or '-'}")
        print(f"            {l['url']}")

    finished = now_local()

    if kept:
        notify_listings(kept, prev_run, finished)
    elif HEARTBEAT:
        notify_heartbeat(prev_run, finished, {
            "total": total_raw,
            "eligible": len(listings),
            "new": len(candidates),
            "rejected": rejected,
        })

    # Mark every eligible listing as seen, including text rejects, so their
    # detail pages are not fetched again next hour.
    save_seen(seen | {l["id"] for l in listings})

    state["last_run"] = finished.isoformat()
    state["last_result"] = {
        "raw": total_raw,
        "eligible": len(listings),
        "new": len(candidates),
        "notified": len(kept),
        "rejected": rejected,
    }
    save_state(state)
    print(f"run finished {fmt_dt(finished)} "
          f"({(finished - started).total_seconds():.0f}s)")


if __name__ == "__main__":
    main()
