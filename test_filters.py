import scraper as S

# (text, should_pass, label)
CASES = [
    # --- must be REJECTED ---
    ("Genossenschaftswohnung mit Balkon in Strasshof", False, "Genossenschaftswohnung"),
    ("Schöne Wohnung der Baugenossenschaft Alpenland", False, "Baugenossenschaft"),
    ("Wohnungsgenossenschaft - 3 Zimmer", False, "Wohnungsgenossenschaft"),
    ("Gemeindewohnung 60m2 mit Garten", False, "Gemeindewohnung"),
    ("Gemeindebau, ruhige Lage", False, "Gemeindebau"),
    ("Gemeindebauwohnung zu vermieten", False, "Gemeindebauwohnung"),
    ("Wohnung, Finanzierungsbeitrag EUR 12.000", False, "Finanzierungsbeitrag"),
    ("Baukostenbeitrag: 8.500 Euro", False, "Baukostenbeitrag"),
    ("Nutzungsvertrag unbefristet", False, "Nutzungsvertrag"),
    ("Wohnberechtigungsschein erforderlich", False, "WBS required"),
    ("GEDESAG Neubauprojekt Mietwohnung", False, "GEDESAG"),
    ("Angebot der Sozialbau AG", False, "Sozialbau"),
    ("Wohnung befristet auf 3 Jahre", False, "befristet"),
    ("WG-Zimmer in netter Runde", False, "WG-Zimmer"),
    ("Untermiete für 6 Monate", False, "Untermiete"),
    ("gemeinnützige Bauvereinigung", False, "gemeinnuetzige Bauvereinigung"),

    # --- must PASS (the false-positive traps) ---
    ("Mietwohnung in der Marktgemeinde Strasshof mit Terrasse", True, "Marktgemeinde"),
    ("Wohnung im Gemeindegebiet Gänserndorf", True, "Gemeindegebiet"),
    ("Ruhige Lage, Gemeindeamt in der Nähe", True, "Gemeindeamt"),
    ("Erstbezug, unbefristeter Mietvertrag", True, "unbefristet"),
    ("Wohnung unbefristet zu vermieten", True, "unbefristet 2"),
    ("Kein Finanzierungsbeitrag, keine Ablöse", True, "kein Finanzierungsbeitrag"),
    ("ohne Baukostenbeitrag", True, "ohne Baukostenbeitrag"),
    ("Keine Genossenschaft, privat vermietet", True, "keine Genossenschaft"),
    ("Privatvermietung, 65m2, Balkon, Garage", True, "clean private"),
    ("Schöne 3-Zimmer Wohnung mit Küche und Kellerabteil", True, "kueche/keller"),
    ("Neubau mit Fußbodenheizung und Wärmepumpe", True, "umlaut folding"),
    ("Haustiere erlaubt, Garten vorhanden", True, "Haustiere erlaubt"),
]


def run():
    fails = 0
    for text, want, label in CASES:
        l = {"text": S.fold(text), "price": 700, "area": 60}
        ok, reasons = S.text_verdict(l)
        mark = "ok " if ok == want else "FAIL"
        if ok != want:
            fails += 1
        why = f"  <- {reasons}" if reasons else ""
        print(f"{mark}  {'pass' if ok else 'reject'}  {label}{why}")
    print(f"\n{len(CASES) - fails}/{len(CASES)} correct")
    return fails


def show_scores():
    print("\n--- scoring sanity ---")
    samples = [
        ("Erstbezug 70m2 unbefristet mit Garage, provisionsfrei", 750, 70),
        ("3 Zimmer Wohnung mit Balkon", 800, 55),
        ("Nette Wohnung mit Terrasse und Ablöse EUR 3000", 700, 60),
    ]
    for text, price, area in samples:
        l = {"text": S.fold(text), "price": price, "area": area}
        s = S.score(l)
        print(f"  {s:>7}  {price}EUR/{area}m2  ({price/area:.1f}/m2)  {text}")
        print(f"           matched: {', '.join(l['matched']) or '-'}")


if __name__ == "__main__":
    f = run()
    show_scores()
    raise SystemExit(1 if f else 0)
