"""Seeds ~2 weeks of realistic claim history so the dashboard has meaningful trends.

Every seeded claim goes through the REAL pipeline (duplicate check, validation, policy engine, risk scoring,
routing and audit trail); only the AI step is replaced by pre-defined receipt data and the timestamps are
spread over the past 14 days. Seeded employees and vendors are fictional.

    python tools/seed_demo_data.py            # add history
    python tools/seed_demo_data.py --reset    # wipe all claims first (clean demo)
"""
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_sample_receipts as gen  # noqa: E402
from app import config, db, pipeline  # noqa: E402
from app.models import ReceiptData  # noqa: E402

random.seed(42)
EMPLOYEES = ["Aarav Mehta", "Priya Sharma", "Rohan Iyer", "Sneha Kulkarni", "Kabir Singh", "Ananya Rao"]
PURPOSES = ["🤝 Client meeting", "✈️ Business travel", "🎤 Conference", "🍽️ Team event", "📦 Office purchase"]
VENDORS = {
    "meals": [("Tandoor Tales", 400, 1400), ("Cafe Brewster", 250, 700), ("Biryani Junction", 500, 1300),
              ("The Konkan Kitchen", 900, 2400)],
    "travel": [("Swift Cabs", 250, 900), ("Indigo Air (agent)", 4200, 9500), ("RailYatri Tickets", 600, 2200)],
    "lodging": [("Hotel Lotus Residency", 3500, 7800), ("Grand Orchid Inn", 4800, 9800)],
    "fuel": [("HP Fuel Station", 1200, 2800), ("Indian Oil COCO", 900, 2600)],
    "office_supplies": [("Paper Plaza Stationers", 300, 1800), ("TechKart Electronics", 1500, 6500)],
}
WEIGHTS = {"meals": 40, "travel": 25, "lodging": 12, "fuel": 10, "office_supplies": 13}


def fake_extractor(data: ReceiptData):
    return lambda image_bytes, mime, log=None: (data, data.model_dump(mode="json"), 1, random.randint(3800, 8800))


def make_image(name, vendor, d, items, seed):
    gen.OUT = config.RECEIPTS_DIR / "_seed"
    gen.OUT.mkdir(parents=True, exist_ok=True)
    gen.receipt(name, vendor.upper(), ["Pune"], [("Bill No:", f"B-{seed:05d}"), ("Date:", d.strftime("%d/%m/%Y"))],
                items, tax_rate=0, seed=seed, angle=random.uniform(-2, 2))
    return (gen.OUT / name).read_bytes()


def backdate(claim_id: str, when: datetime):
    c = db.get_claim(claim_id)
    proc = random.randint(3900, 9500)
    with db.connect() as conn:
        conn.execute("UPDATE claims SET created_at=?, updated_at=?, processing_ms=?, ai_latency_ms=? WHERE id=?",
                     (when.isoformat(timespec="seconds"), (when + timedelta(seconds=proc / 1000)).isoformat(timespec="seconds"),
                      proc if c["status"] != "DUPLICATE" else random.randint(15, 60), proc - 300, claim_id))
        rows = conn.execute("SELECT id FROM audit_log WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
        for i, r in enumerate(rows):
            conn.execute("UPDATE audit_log SET ts=? WHERE id=?",
                         ((when + timedelta(seconds=i * proc / 1000 / max(len(rows), 1))).isoformat(timespec="seconds"), r[0]))
        # API metrics (with the occasional free-tier rate limit and failover)
        if c["status"] != "DUPLICATE":
            t = when.isoformat(timespec="seconds")
            if random.random() < 0.12:
                conn.execute("INSERT INTO api_metrics (ts, service, success, latency_ms, error, model) VALUES (?,?,?,?,?,?)",
                             (t, "gemini", 0, random.randint(300, 900), "429 RESOURCE_EXHAUSTED: free-tier rate limit",
                              "gemini-2.5-flash"))
                model = "gemini-3.1-flash-lite"
            else:
                model = "gemini-2.5-flash"
            conn.execute("INSERT INTO api_metrics (ts, service, success, latency_ms, error, model) VALUES (?,?,?,?,?,?)",
                         (t, "gemini", 1, proc - 300, None, model))


def manager_act(claim_id: str, when: datetime):
    approve = random.random() < 0.7
    pipeline.manager_decision(claim_id, approve, random.choice(["Neha Joshi (Manager)", "Vikram Desai (Manager)"]),
                              "" if approve else random.choice(["Not covered by policy", "Please attach itinerary"]),
                              notify=False)
    later = when + timedelta(minutes=random.randint(8, 300))
    with db.connect() as conn:
        conn.execute("UPDATE claims SET updated_at=? WHERE id=?", (later.isoformat(timespec="seconds"), claim_id))
        last = conn.execute("SELECT MAX(id) FROM audit_log WHERE claim_id=?", (claim_id,)).fetchone()[0]
        conn.execute("UPDATE audit_log SET ts=? WHERE id=?", (later.isoformat(timespec="seconds"), last))


def main():
    db.init_db()
    if "--reset" in sys.argv:
        with db.connect() as conn:
            for t in ("claims", "audit_log", "api_metrics"):
                conn.execute(f"DELETE FROM {t}")
        print("Database cleared.")
    now = datetime.now()
    made = []
    for i in range(46):
        cat = random.choices(list(WEIGHTS), weights=list(WEIGHTS.values()))[0]
        vendor, lo, hi = random.choice(VENDORS[cat])
        when = now - timedelta(days=random.uniform(0.2, 14), hours=0)
        when = when.replace(hour=random.randint(9, 20), minute=random.randint(0, 59))
        rdate = (when - timedelta(days=random.choice([0, 0, 1, 2, 3]))).date()
        amount = round(random.uniform(lo, hi), 2)
        items = [("Item / service", amount)]
        conf = round(random.uniform(0.86, 1.0), 2)
        roll = random.random()
        if roll < 0.06:
            rdate = rdate - timedelta(days=random.randint(35, 70))          # too old
        elif roll < 0.12:
            items = [("Main course", round(amount * 0.6, 2)), ("Beer x2", round(amount * 0.4, 2))]  # alcohol
            cat = "meals"
        elif roll < 0.18:
            conf = round(random.uniform(0.3, 0.6), 2)                        # blurry
        elif roll < 0.21:
            amount = float(random.choice([1500, 2000, 2500, 3000]))           # round amount
            items = [("Services", amount)]
        data = ReceiptData(is_receipt=True, vendor=vendor, receipt_date=rdate, total_amount=amount, currency="INR",
                           category=cat, line_items=[{"description": d, "amount": a} for d, a in items],
                           invoice_number=f"INV-{random.randint(10000, 99999)}", confidence=conf)
        img = make_image(f"seed_{i:02d}.png", vendor, rdate, items, seed=1000 + i)
        r = pipeline.submit_claim(img, chat_id=0, employee_name=random.choice(EMPLOYEES),
                                  purpose=random.choice(PURPOSES), mime_type="image/png",
                                  extractor=fake_extractor(data), notify=False)
        backdate(r.claim_id, when)
        made.append((r.claim_id, img, when))
        # someone re-submits the same receipt a little later
        if random.random() < 0.08:
            d = pipeline.submit_claim(img, chat_id=0, employee_name=db.get_claim(r.claim_id)["employee_name"],
                                      purpose="Re-submitted", mime_type="image/png",
                                      extractor=fake_extractor(data), notify=False)
            backdate(d.claim_id, when + timedelta(hours=random.randint(2, 30)))
    # managers have acted on most older pending claims; leave the recent ones in the inbox
    pending = db.claims_by_status("PENDING_APPROVAL", "NEEDS_REVIEW")
    for c in pending:
        when = datetime.fromisoformat(c["created_at"])
        if when < now - timedelta(days=1) and random.random() < 0.85:
            manager_act(c["id"], when)
    with db.connect() as conn:
        summary = conn.execute("SELECT status, COUNT(*) FROM claims GROUP BY status").fetchall()
    print("Seeded claims by status:", {s: n for s, n in summary})


if __name__ == "__main__":
    main()
