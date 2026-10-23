"""Generates demo receipt images (fictional vendors) for each test scenario.

Dates are relative to today, so re-run this right before recording the demo:
    python tools/make_sample_receipts.py
Output: sample_receipts/*.png
"""
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "sample_receipts"
OUT.mkdir(exist_ok=True)


def font(size, bold=False):
    for name in (["consolab.ttf", "courbd.ttf"] if bold else ["consola.ttf", "cour.ttf"]) + ["DejaVuSansMono.ttf"]:
        for base in ("C:/Windows/Fonts/", "/usr/share/fonts/truetype/dejavu/", ""):
            try:
                return ImageFont.truetype(base + name, size)
            except OSError:
                continue
    return ImageFont.load_default()


def weekday(days_ago):
    d = date.today() - timedelta(days=days_ago)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def receipt(filename, header, address, meta, items, tax_rate=0.05, footer="Thank you! Visit again", blur=0,
            angle=0.0, seed=1, extra_lines=()):
    random.seed(seed)
    W, line_h = 460, 26
    sub = sum(a for _, a in items)
    tax = round(sub * tax_rate, 2)
    total = round(sub + tax, 2)
    rows = len(meta) + len(items) + len(extra_lines) + 16
    H = rows * line_h + 60
    paper = Image.new("RGB", (W, H), (252, 251, 246))
    d = ImageDraw.Draw(paper)
    f, fb, fh = font(19), font(20, True), font(26, True)
    y = 24

    def center(text, fnt, yy):
        w = d.textlength(text, font=fnt)
        d.text(((W - w) / 2, yy), text, font=fnt, fill=(25, 25, 25))

    def lr(left, right, fnt=f):
        nonlocal y
        d.text((24, y), left, font=fnt, fill=(25, 25, 25))
        if right:
            d.text((W - 24 - d.textlength(right, font=fnt), y), right, font=fnt, fill=(25, 25, 25))
        y += line_h

    def dash():
        nonlocal y
        d.text((24, y), "-" * 38, font=f, fill=(90, 90, 90))
        y += line_h

    center(header, fh, y); y += 36
    for a in address:
        center(a, f, y); y += line_h
    dash()
    for k, v in meta:
        lr(k, v)
    dash()
    lr("ITEM", "AMOUNT", fb)
    for name, amt in items:
        lr(name, f"{amt:,.2f}")
    dash()
    lr("Sub Total", f"{sub:,.2f}")
    if tax_rate:
        lr(f"GST {tax_rate * 100:.0f}%", f"{tax:,.2f}")
    lr("GRAND TOTAL Rs.", f"{total:,.2f}", fb)
    for ln in extra_lines:
        lr(ln, "")
    dash()
    center(footer, f, y)

    # make it look like a phone photo: background, slight rotation, noise
    bg = Image.new("RGB", (W + 120, H + 120), (120 + random.randint(-15, 15), 105, 90))
    bg.paste(paper, (60, 60))
    img = bg.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=(110, 98, 85))
    px = img.load()
    for _ in range(4000):
        x, yy = random.randrange(img.width), random.randrange(img.height)
        r, g, b = px[x, yy]
        n = random.randint(-25, 25)
        px[x, yy] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    img.save(OUT / filename)
    print(f"  {filename:<28} total Rs. {total:,.2f}")
    return total


def not_a_receipt(filename):
    img = Image.new("RGB", (600, 450), (135, 190, 235))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 300, 600, 450], fill=(80, 160, 70))
    d.ellipse([440, 40, 540, 140], fill=(255, 215, 0))
    d.polygon([(80, 300), (230, 120), (380, 300)], fill=(120, 110, 100))
    d.polygon([(260, 300), (400, 160), (540, 300)], fill=(100, 95, 90))
    d.rectangle([60, 250, 140, 330], fill=(170, 60, 40))
    d.polygon([(50, 250), (100, 210), (150, 250)], fill=(120, 40, 30))
    img.save(OUT / filename)
    print(f"  {filename:<28} (landscape picture)")


if __name__ == "__main__":
    print(f"Writing sample receipts to {OUT}")
    d1 = weekday(2)
    meal_meta = [("Bill No:", "SG-20431"), ("Date:", d1.strftime("%d/%m/%Y")), ("Table:", "7   Pax: 3")]
    meal_items = [("Paneer Butter Masala", 320.0), ("Butter Naan x4", 160.0), ("Veg Biryani", 260.0),
                  ("Fresh Lime Soda x2", 70.0)]
    receipt("01_meal_auto_approve.png", "SPICE GARDEN", ["Multi-Cuisine Restaurant", "FC Road, Pune 411004",
            "GSTIN: 27ABCDE1234F1Z5"], meal_meta, meal_items, seed=1, angle=1.2)

    receipt("02_hotel_manager_approval.png", "HOTEL SEA BREEZE", ["Juhu Tara Road, Mumbai 400049",
            "GSTIN: 27HSBPL5678K1Z2"],
            [("Invoice:", "HSB/24/0912"), ("Check-in:", weekday(4).strftime("%d/%m/%Y")),
             ("Check-out:", weekday(3).strftime("%d/%m/%Y")), ("Guest:", "Dev Gandhi")],
            [("Deluxe Room x1 night", 6200.0), ("Breakfast buffet", 450.0)], tax_rate=0.12, seed=2, angle=-0.8)

    receipt("03_bar_alcohol_flagged.png", "SKYLINE BAR & GRILL", ["Koregaon Park, Pune 411001"],
            [("Bill No:", "SK-7781"), ("Date:", weekday(1).strftime("%d/%m/%Y"))],
            [("Paneer Tikka", 380.0), ("Chicken Wings", 420.0), ("Kingfisher Beer x2", 520.0),
             ("Whisky (Large)", 340.0)], tax_rate=0.0, seed=3, angle=0.6)

    old = date.today() - timedelta(days=62)
    receipt("04_old_taxi_auto_reject.png", "METRO CABS", ["Ride receipt", "Bengaluru"],
            [("Trip ID:", "MC-55120"), ("Date:", old.strftime("%d/%m/%Y")), ("From:", "Airport T1"),
             ("To:", "Whitefield")], [("Base fare", 1150.0), ("Toll", 120.0), ("Airport fee", 80.0)],
            tax_rate=0.05, seed=4, angle=-1.5)

    receipt("05_blurry_manual_review.png", "CAFE MOCHA", ["MG Road, Bengaluru"],
            [("Bill No:", "CM-118"), ("Date:", weekday(2).strftime("%d/%m/%Y"))],
            [("Cappuccino x2", 380.0), ("Club Sandwich", 290.0)], seed=5, blur=6, angle=4)

    receipt("06_meal_duplicate_rephoto.png", "SPICE GARDEN", ["Multi-Cuisine Restaurant", "FC Road, Pune 411004",
            "GSTIN: 27ABCDE1234F1Z5"], meal_meta, meal_items, seed=99, angle=-2.3)

    not_a_receipt("07_not_a_receipt.png")

    receipt("08_cab_ride_auto_approve.png", "METRO CABS", ["Ride receipt", "Pune"],
            [("Trip ID:", "MC-60871"), ("Date:", weekday(1).strftime("%d/%m/%Y")), ("From:", "Hinjewadi Ph 1"),
             ("To:", "Baner")], [("Base fare", 310.0), ("Distance 9.2 km", 95.0)], seed=8, angle=0.9)
    print("Done. 01 re-sent as-is = exact duplicate demo; 06 = different photo of 01 (fuzzy duplicate).")
