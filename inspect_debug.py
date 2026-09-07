"""
Reads debug.json (written by scraper.py when DEBUG=1) and shows exactly what
the numeric filters are doing to each listing. No network needed.

    python inspect_debug.py
"""

import json
import scraper as S

data = json.load(open("debug.json", encoding="utf-8"))
ads = S._find_ads(data)
print(f"{len(ads)} raw listings in debug.json\n")

rows = []
for ad in ads:
    l = S.parse(ad)
    reasons = []
    if S.ALLOWED_POSTCODES and l["postcode"] not in S.ALLOWED_POSTCODES:
        reasons.append(f"postcode {l['postcode']}")
    if l["price"] and l["price"] > S.MAX_PRICE:
        reasons.append(f"price {l['price']:.0f} > {S.MAX_PRICE}")
    if l["area"] and l["area"] < S.MIN_AREA:
        reasons.append(f"area {l['area']:.0f} < {S.MIN_AREA}")
    if S.MIN_ROOMS and l["rooms"] and l["rooms"] < S.MIN_ROOMS:
        reasons.append(f"rooms {l['rooms']:.0f} < {S.MIN_ROOMS}")
    if not l["id"]:
        reasons.append("no id")
    rows.append((l, reasons))

kept = [r for r in rows if not r[1]]
print(f"{len(kept)} pass, {len(rows) - len(kept)} rejected\n")

# --- why were they rejected? ---
from collections import Counter
tally = Counter(r.split()[0] for _, rs in rows for r in rs)
print("rejection reasons by type:")
for k, v in tally.most_common():
    print(f"  {k:12} {v}")

# --- are the fields even parsing? ---
print("\nmissing/unparsed fields:")
for field in ("price", "area", "rooms", "postcode"):
    n = sum(1 for l, _ in rows if l[field] is None)
    print(f"  {field:10} None for {n}/{len(rows)}")

# --- value ranges, to sanity-check the parsing ---
print("\nvalue ranges:")
for field in ("price", "area", "rooms"):
    vals = sorted(l[field] for l, _ in rows if l[field] is not None)
    if vals:
        print(f"  {field:10} min {vals[0]:>8.0f}  median {vals[len(vals)//2]:>8.0f}"
              f"  max {vals[-1]:>8.0f}")

print("\npostcodes seen:",
      sorted({l["postcode"] for l, _ in rows if l["postcode"]}))

# --- first 15 rejected, in detail ---
print("\n--- sample of rejected listings ---")
for l, rs in [r for r in rows if r[1]][:15]:
    print(f"  {l['price']} EUR / {l['area']} m2 / {l['rooms']} Zi / "
          f"{l['postcode']} | {', '.join(rs)}")
    print(f"      {l['title'][:80]}")

print("\n--- what passes ---")
for l, _ in kept[:15]:
    print(f"  {l['price']} EUR / {l['area']} m2 / {l['postcode']} | {l['title'][:70]}")

# --- raw attribute names on the first ad, for when a field is always None ---
if ads:
    print("\nattribute names available on the first listing:")
    print(" ", sorted(S.attrs(ads[0]).keys()))
