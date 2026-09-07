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
import time

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
    "&PRICE_TO=1000"
    "&ESTATE_SIZE/LIVING_AREA_FROM=40"
)

SEARCH_URL = os.environ.get("WILLHABEN_URL", DEFAULT_URL)

SEEN_FILE = "seen.json"
MAX_NOTIFY = 10          # never send more than this many messages in one run

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ============================================================ NUMERIC FILTERS

MAX_PRICE = 800
MIN_AREA = 40
MIN_ROOMS = 0            # 0 = no room filter
PRICE_PER_SQM_TARGET = 16.0   # €/m² below which a listing starts scoring well

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
        "url": "https://www.willhaben.at/iad/" + seo if seo else "",
    }


# ================================================================= FILTERING

def passes_numeric(l):
    """Cheap checks, run before spending a request on the detail page."""
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
    s = 0.0
    if l["price"] and l["area"]:
        ppm = l["price"] / l["area"]
        s += max(0, PRICE_PER_SQM_TARGET - ppm) * 8
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

def notify_telegram(new):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not (token and chat):
        print("No Telegram credentials set - skipping notifications")
        return

    batch = new[:MAX_NOTIFY]
    if len(new) > MAX_NOTIFY:
        print(f"Capping notifications at {MAX_NOTIFY} (of {len(new)})")

    for l in batch:
        msg = (f"🏠 <b>{l['title']}</b>\n"
               f"{l['price'] or '?'} € · {l['area'] or '?'} m² · "
               f"{l['rooms'] or '?'} Zi · {l['district']}\n"
               f"Score: {l['score']}")
        if l.get("matched"):
            msg += f"\n✅ {', '.join(l['matched'])[:200]}"
        msg += f"\n{l['url']}"
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
                timeout=20,
            )
            if not r.ok:
                print(f"Telegram error {r.status_code}: {r.text}")
        except requests.RequestException as e:
            print(f"Telegram request failed: {e}")
        time.sleep(1.5)      # stay under the per-chat rate limit


# ====================================================================== MAIN

def main():
    print(f"Fetching: {SEARCH_URL}")
    ads = fetch_listings(SEARCH_URL)
    print(f"Fetched {len(ads)} raw listings")

    listings = [parse(a) for a in ads]
    listings = [l for l in listings if l["id"] and passes_numeric(l)]
    print(f"{len(listings)} passed price/size checks")

    seen = load_seen()
    candidates = [l for l in listings if l["id"] not in seen]
    print(f"{len(candidates)} are new - applying text rules"
          f"{' (SOFT_EXCLUDE on)' if SOFT_EXCLUDE else ''}")

    kept = []
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
            print(f"  rejected [{', '.join(reasons)}]: {l['title']}")

    kept.sort(key=lambda x: -x["score"])
    print(f"{len(kept)} passed everything")
    for l in kept:
        print(f"  [{l['score']:>7}] {l['price']} EUR  {l['area']} m2  "
              f"{l['district']} - {l['title']}")
        print(f"            matched: {', '.join(l['matched']) or '-'}")
        print(f"            {l['url']}")

    if kept:
        notify_telegram(kept)

    # Mark every numerically-eligible listing as seen, including text rejects,
    # so their detail pages are not fetched again next hour.
    save_seen(seen | {l["id"] for l in listings})


if __name__ == "__main__":
    main()
