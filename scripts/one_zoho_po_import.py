"""
One-time import of open (not-yet-supplied) Purchase Orders from Zoho Books
into this ERP.

Source: Zoho Books org "Logika Systems India Pvt. Limited" (org id
60006374366), fetched 2026-08-14. Scope, as agreed with the user before
this was written:

  - Every PO dated in the last 6 months (2026-02-14 through 2026-08-14)
    whose Zoho `order_status` is "open" -- this INCLUDES partially-billed
    POs, since Zoho's own goods-receipt tracking is switched off in this
    account (every PO, even ones billed months ago, shows
    quantity_received = 0), so "order_status: open" is the only usable
    stand-in for "not fully supplied yet" that this account's data
    actually supports. Fully billed ("closed") and cancelled POs are
    excluded.
  - Vendors are matched by exact (case-insensitive, trimmed) name against
    the local `vendors` table; if no match, a new Vendor row is created
    from the Zoho contact/address data embedded below.
  - Items are matched the same way against `items.name`. If EVERY line on
    a PO matches an existing Item, the whole PO is imported. If ANY line's
    item can't be found locally, the ENTIRE PO is skipped (not partially
    imported) and reported in the "missing items" summary at the end --
    per the user's explicit instruction, this script never creates Items.
  - POs from the intercompany vendor "Logika Technologies INC ( Canada)
    (Creditor)" are invoiced in USD. This required adding real
    multi-currency support to Purchase Orders first (see
    PurchaseOrder.currency/exchange_rate in models.py and migration
    d4f7a9c21b3e) -- amounts for those POs are stored in USD, not
    force-converted to INR.

Safe to re-run: before creating a PurchaseOrder, this script checks
whether one already exists whose notes mention the same Zoho PO number
(e.g. "LSIPL/26-27/PO/036") and skips it if so. Commits one PO at a time,
so a re-run after a partial failure only imports what's left.

Run from the project root, AFTER applying migration d4f7a9c21b3e (alembic
upgrade head):
    python scripts\\one_zoho_po_import.py
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import SessionLocal
from app.models import Vendor, Item, PurchaseOrder, PurchaseOrderItem

# Same IST convention as app/formatting.py -- Zoho's `date` field is a
# plain calendar date with no time attached, so it's stored as IST
# midnight (converted back to the UTC instant that displays as that date
# once format_dt adds IST_OFFSET back on), not literal UTC midnight.
IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_midnight_to_utc(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d") - IST_OFFSET


# ---------------------------------------------------------------------------
# Source data -- the 9 open POs fetched from Zoho Books, last 6 months.
# ---------------------------------------------------------------------------
PURCHASE_ORDERS = [
    {
        "po_number": "LSIPL/26-27/PO/036",
        "date": "2026-08-14",
        "currency": "INR",
        "exchange_rate": 1,
        "vendor": {
            "name": "Hydrocons",
            "gstin": "20ANDPU1355J1Z4",
            "contact_person": "",
            "email": "hdrocons@outlook.com",
            "phone": "9334746678",
            "address": "NS - 5, Phase - II, Industrial Area, Aditypur, Jamshedpur, Jharkhand 832109, India",
        },
        "notes": "Imported from Zoho PO LSIPL/26-27/PO/036. Payment Terms: Net 90 Days. Price Basis: EXW, Canada. Duties & Taxes: Extra.",
        "lines": [
            {"item_name": "Steel braided hose, G 5/16, 1.5m", "qty": 20, "rate": 720},
            {"item_name": "Steel braided hose, G1/2, 5m", "qty": 20, "rate": 2200},
            {"item_name": "LS22XXMF50031A : Steel braided hose, G1/2, 1.5m", "qty": 20, "rate": 1220},
        ],
    },
    {
        "po_number": "LSIPL/26-27/PO/035",
        "date": "2026-08-12",
        "currency": "INR",
        "exchange_rate": 1,
        "vendor": {
            "name": "ARJUNLAL BANWARILAL & CO. PVT. LTD.",
            "gstin": "20AACCA7396N1ZE",
            "contact_person": "Ashish Mohanka",
            "email": "info@arjunlalbanwarilal.co.in",
            "phone": "06572291546",
            "address": "CHOWK, JUGSALAI, Jamshedpur, Jharkhand 831006, India",
        },
        "notes": "Imported from Zoho PO LSIPL/26-27/PO/035. Payment Terms: 100% on Delivery.",
        "lines": [
            {"item_name": "7C x 0.75sqmm PVC copper flexible cable", "qty": 100, "rate": 243.3},
            {"item_name": "4C x 0.5sqmm PVC copper flexible cable", "qty": 100, "rate": 70},
        ],
    },
    {
        "po_number": "LSIPL/26-27/PO/034",
        "date": "2026-08-10",
        "currency": "USD",
        "exchange_rate": 97.2,
        "vendor": {
            "name": "Logika Technologies INC ( Canada) (Creditor)",
            "gstin": "",
            "contact_person": "Phil DiBello",
            "email": "phild@logikaglobal.com",
            "phone": "",
            "address": "10 - 30 Mural Street, Richmond Hill, Ontario L4B 1B5, Canada",
        },
        "notes": "Imported from Zoho PO LSIPL/26-27/PO/034. Payment Terms: Net 90 Days. Price Basis: EXW, Canada.",
        "lines": [
            {"item_name": "LP01143 : Hot Metal Sensor, Static, 24V, Green, LT", "qty": 8, "rate": 250},
            {"item_name": "LP00099 : Loop Position Sensor, 30°, 24V, N, 20H-L", "qty": 17, "rate": 1040},
            {"item_name": "LP00361 : Infrared Sensor, 30S14, 24V,", "qty": 14, "rate": 780},
        ],
    },
    {
        "po_number": "LSIPL/26-27/PO/033",
        "date": "2026-08-05",
        "currency": "INR",
        "exchange_rate": 1,
        "vendor": {
            "name": "SP Enterprise",
            "gstin": "19BKFPM8109J1ZS",
            "contact_person": "Sirsendu Mahanta",
            "email": "sirsendu.mahanta@outlook.com",
            "phone": "9831992996",
            "address": "105/46 DUMDUM Road, Sil Colony, Kolkata, West Bengal 700074, India",
        },
        "notes": "Imported from Zoho PO LSIPL/26-27/PO/033. Payment Terms: On Delivery after Quality Check.",
        "lines": [
            {"item_name": "LP01034 : Sleeve, Lens Tube, 0680-00, 310SS", "qty": 5, "rate": 20830},
            {"item_name": "51070 : Custom Flange for JSPL Raigarh", "qty": 1, "rate": 13500},
        ],
    },
    {
        "po_number": "LSIPL/26-27/PO/032",
        "date": "2026-08-05",
        "currency": "USD",
        "exchange_rate": 97.2,
        "vendor": {
            "name": "Logika Technologies INC ( Canada) (Creditor)",
            "gstin": "",
            "contact_person": "Phil DiBello",
            "email": "phild@logikaglobal.com",
            "phone": "",
            "address": "10 - 30 Mural Street, Richmond Hill, Ontario L4B 1B5, Canada",
        },
        "notes": "Imported from Zoho PO LSIPL/26-27/PO/032. Payment Terms: Net 90 Days. Price Basis: EXW, Canada.",
        "lines": [
            {"item_name": "LP01143-5m : Hot Metal Sensor, Static, 24V, GRN, LT, 5m Cable", "qty": 2, "rate": 250},
            {"item_name": "LP00045-5m : Hot Metal Sensor, Scan, 10°, 24V, 5 m Cable", "qty": 3, "rate": 690},
            {"item_name": "LP00099-5m : Loop Position Sensor, 30°, 24V, N, 20H-L, 5 m Cable", "qty": 2, "rate": 1040},
            {"item_name": "LP00380-5m : Infrared Sensor, 616, 24V, 5 m Cable", "qty": 2, "rate": 1680},
        ],
    },
    {
        "po_number": "LSIPL/26-27/PO/027",
        "date": "2026-07-09",
        "currency": "INR",
        "exchange_rate": 1,
        "vendor": {
            "name": "APPLIED  SOLUTIONS",
            "gstin": "20AFVPD7276F1ZD",
            "contact_person": "Bablu Pradhan",
            "email": "appliedsolutions.jsr@gmail.com",
            "phone": "+91-9608016733",
            "address": "30, Ground Floor, Ashiana Trade Centre Adityapur, Jamshedpur, Jharkhand 831013, India",
        },
        "notes": "Imported from Zoho PO LSIPL/26-27/PO/027 (partially billed in Zoho as of import). Payment Terms: 100% Against PI.",
        "lines": [
            {"item_name": "AC40-X8E55 - Filter Assembly", "qty": 10, "rate": 16735},
            {"item_name": "LP00832 : Air Filter Element, 5 micron", "qty": 30, "rate": 630},
            {"item_name": "LP00833 : Mist Separator Filter Element, 0.3 micron", "qty": 30, "rate": 1390},
            {"item_name": "Elbow Shape fitting for Air control box", "qty": 20, "rate": 175},
            {"item_name": "Straight Adaptor for Pneumatic Control Box", "qty": 20, "rate": 82},
            {"item_name": "Straight Adaptor for Pneumatic Control Box (KQ2H10-01AS)", "qty": 20, "rate": 100},
            {"item_name": "TEE Joint for Pneumatic Control box (KQ2Y10-04AS)", "qty": 20, "rate": 264},
        ],
    },
    {
        "po_number": "LSIPL/26-27/PO/026",
        "date": "2026-07-07",
        "currency": "USD",
        "exchange_rate": 95.15,
        "vendor": {
            "name": "Logika Technologies INC ( Canada) (Creditor)",
            "gstin": "",
            "contact_person": "Phil DiBello",
            "email": "phild@logikaglobal.com",
            "phone": "",
            "address": "10 - 30 Mural Street, Richmond Hill, Ontario L4B 1B5, Canada",
        },
        "notes": "Imported from Zoho PO LSIPL/26-27/PO/026. Payment Terms: Net 90 Days. Price Basis: EXW, Canada.",
        "lines": [
            {"item_name": "LP00024 : Lens, 0680-00, Hi-Temp, with RTD", "qty": 7, "rate": 1200},
            {"item_name": "LP01266 : Lens Hood Assembly, Straight, 680 Lens", "qty": 8, "rate": 325},
            {"item_name": "LP01034 : Sleeve, Lens Tube, 0680-00, 310SS", "qty": 2, "rate": 450},
            {"item_name": "LP01672 : Lens Hood Assembly, Straight, 480 Lens", "qty": 2, "rate": 325},
            {"item_name": "LP01626 : Lens, 0480-00-082, Hi-Temp, with RTD", "qty": 2, "rate": 1200},
        ],
    },
    {
        "po_number": "LSIPL/26-27/PO/024",
        "date": "2026-07-06",
        "currency": "USD",
        "exchange_rate": 95.15,
        "vendor": {
            "name": "Logika Technologies INC ( Canada) (Creditor)",
            "gstin": "",
            "contact_person": "Phil DiBello",
            "email": "phild@logikaglobal.com",
            "phone": "",
            "address": "10 - 30 Mural Street, Richmond Hill, Ontario L4B 1B5, Canada",
        },
        "notes": "Imported from Zoho PO LSIPL/26-27/PO/024. Payment Terms: Net 90 Days. Price Basis: EXW, Canada.",
        "lines": [
            {"item_name": "LP01702 : Loop Position Sensor, P1385, 24V, 2 m Cable", "qty": 15, "rate": 950},
        ],
    },
    {
        "po_number": "LSIPL/25-26/PO/113",
        "date": "2026-03-02",
        "currency": "INR",
        "exchange_rate": 1,
        "vendor": {
            "name": "SHARMA ENGINEERING WORKS",
            "gstin": "20FDZPS2819Q1ZJ",
            "contact_person": "Manoj Kumar Sharma",
            "email": "sharmaengineering39@gmail.com",
            "phone": "9504715102",
            "address": "D/4 New Development Area, Gomluri, Jamshedpur, Jharkhand 831003, India",
        },
        "notes": "Imported from Zoho PO LSIPL/25-26/PO/113 (partially billed in Zoho as of import, ref LSIPL/24-25/PO/044).",
        "lines": [
            {"item_name": "LS02XXPL50010A : Air Junction Box", "qty": 5, "rate": 17200},
            {"item_name": "LS02XXPL50009A : Electronics Box", "qty": 5, "rate": 17200},
        ],
    },
]


def norm(s: str) -> str:
    return (s or "").strip().lower()


def already_imported(db, po_number: str) -> bool:
    return (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.notes.like(f"%{po_number}%"))
        .first()
        is not None
    )


def next_order_no(db, offset: int) -> str:
    count = db.query(PurchaseOrder).count()
    return f"PO-{count + 1 + offset:05d}"


def main():
    db = SessionLocal()
    vendors_created = []
    pos_imported = []
    pos_skipped_duplicate = []
    pos_skipped_missing_items = []  # (po_number, [missing item names])
    order_no_offset = 0

    # Pre-load once and keep in-memory, keyed by normalized name -- cheap
    # for this app's table sizes, and avoids re-querying the whole table
    # on every line/PO. vendors_by_name is updated in place as new
    # vendors get created below, so a later PO from the same new vendor
    # (e.g. two Hydrocons POs) reuses it instead of creating a duplicate.
    vendors_by_name = {norm(v.name): v for v in db.query(Vendor).all()}
    items_by_name = {norm(i.name): i for i in db.query(Item).all()}

    try:
        for po in PURCHASE_ORDERS:
            po_number = po["po_number"]

            if already_imported(db, po_number):
                pos_skipped_duplicate.append(po_number)
                continue

            missing = [line["item_name"] for line in po["lines"] if norm(line["item_name"]) not in items_by_name]
            if missing:
                pos_skipped_missing_items.append((po_number, missing))
                continue

            vendor = vendors_by_name.get(norm(po["vendor"]["name"]))
            if vendor is None:
                v = po["vendor"]
                vendor = Vendor(
                    name=v["name"],
                    contact_person=v["contact_person"] or None,
                    email=v["email"] or None,
                    phone=v["phone"] or None,
                    address=v["address"] or None,
                    gstin=v["gstin"] or None,
                )
                db.add(vendor)
                db.flush()  # get vendor.id without committing yet
                vendors_by_name[norm(vendor.name)] = vendor
                vendors_created.append(vendor.name)

            order = PurchaseOrder(
                order_no=next_order_no(db, order_no_offset),
                vendor_id=vendor.id,
                order_date=ist_midnight_to_utc(po["date"]),
                status="ordered",  # already placed with the vendor in Zoho, just not received here yet
                currency=po["currency"],
                exchange_rate=po["exchange_rate"],
                notes=po["notes"],
            )
            db.add(order)

            total = 0.0
            for line in po["lines"]:
                item = items_by_name[norm(line["item_name"])]
                qty = float(line["qty"])
                price = float(line["rate"])
                line_total = qty * price
                total += line_total
                order.items.append(PurchaseOrderItem(
                    item_id=item.id, quantity=qty, unit_price=price, total=line_total,
                ))
            order.total_amount = total

            db.commit()
            order_no_offset += 1
            pos_imported.append((order.order_no, po_number, po["currency"], total))

        print("=" * 70)
        print("Zoho PO import complete")
        print("=" * 70)

        if vendors_created:
            print(f"\nVendors created ({len(vendors_created)}):")
            for name in vendors_created:
                print(f"  - {name}")
        else:
            print("\nNo new vendors needed -- all matched existing ones.")

        if pos_imported:
            print(f"\nPurchase Orders imported ({len(pos_imported)}):")
            for order_no, zoho_no, currency, total in pos_imported:
                print(f"  - {order_no}  (Zoho {zoho_no})  {currency} {total:,.2f}")
        else:
            print("\nNo purchase orders imported.")

        if pos_skipped_duplicate:
            print(f"\nAlready imported previously, skipped ({len(pos_skipped_duplicate)}):")
            for zoho_no in pos_skipped_duplicate:
                print(f"  - {zoho_no}")

        if pos_skipped_missing_items:
            print(f"\nSKIPPED -- missing item(s) not found locally ({len(pos_skipped_missing_items)} PO(s)):")
            all_missing = set()
            for zoho_no, missing in pos_skipped_missing_items:
                print(f"  - {zoho_no}:")
                for name in missing:
                    print(f"      * {name}")
                    all_missing.add(name)
            print(f"\n  {len(all_missing)} distinct item(s) need to be created (or matched by renaming) before these POs can be imported:")
            for name in sorted(all_missing):
                print(f"    - {name}")
            print("\n  Re-run this script after adding them -- already-imported POs above will be skipped automatically.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
