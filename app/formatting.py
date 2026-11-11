"""
Small display-formatting helpers shared across routers' Jinja environments.

Quantity-bearing columns (Item.current_stock, Item.reorder_level,
BomComponent.quantity, *OrderItem.quantity, ProductionOrder*.quantity*,
StockTransaction.quantity) are all Numeric(14, 4) so fractional amounts
(e.g. 0.2459 of a cable reel) can be stored precisely. Numeric columns come
back from SQLAlchemy as Decimal, which would otherwise render in templates
as "3.0000" instead of "3" for a plain whole-number quantity. `format_qty`
rounds to at most 2 decimal places and strips trailing zeros for display;
the underlying stored value (full 4-decimal precision) is untouched — this
is a display-only rounding, same as format_inr's 2-decimal currency
rounding below. The one place this rounding is visible beyond a read-only
display is items/form.html's Reorder Level input, which is pre-filled via
this filter — re-saving that field carries the rounded value forward, so a
reorder level entered with 3+ decimals will get rounded to 2 the next time
that item is edited and saved, even if the field itself isn't touched.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_DOWN

# Every `datetime.utcnow()` default in models.py (created_at, order_date,
# transaction_date, uploaded_at, received_at, ...) stores a real UTC instant.
# This app has a single office, in India, so a fixed +5:30 offset is used to
# display those in IST rather than pulling in a timezone library — India
# doesn't observe DST, so there's no DST transition to get wrong.
IST_OFFSET = timedelta(hours=5, minutes=30)


def format_dt(value, fmt: str = "%d-%b-%Y %H:%M") -> str:
    """IST display for a column that captures a real moment in time
    (created_at, order_date, transaction_date, uploaded_at/received_at).
    Pass fmt='%d-%b-%Y' for a date-only display (order_date etc.) — still
    goes through the IST shift first since it's a genuine captured instant,
    just formatted without the time portion, so an order created just after
    midnight UTC (which is already the next day in IST) shows the correct
    local date.

    Do NOT use this for a plain user-typed date with no time/timezone
    attached — SalesOrder.customer_po_date/expected_shipment_date and
    SalesOrderPaymentTerm.due_date/bg_expiry_date/received_date are parsed
    from a bare <input type=date> via sales.py's _parse_date() as a naive
    midnight datetime; there's no UTC instant behind them to convert, and
    shifting would silently change the calendar date the user actually
    typed. Those keep using a plain .strftime() in the template.
    """
    if value is None:
        return "-"
    return (value + IST_OFFSET).strftime(fmt)


def format_qty(value) -> str:
    if value is None:
        return "0"
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # never show more than 2 decimal places
    s = format(d, "f")  # avoid scientific notation for very small/large values
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def format_price(value, currency: str = "INR") -> str:
    """Unit price for display: normal 2-decimal money format, plus the extra
    3rd/4th decimals only when the stored price really has them (e.g. 36.0656
    for 11,000 / 305 m). Display only."""
    if value is None:
        return format_money(0, currency)
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    d = d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    base = format_money(d.quantize(Decimal("0.01"), rounding=ROUND_DOWN), currency)
    extra = format(d, "f").split(".")[1][2:].rstrip("0") if "." in format(d, "f") else ""
    return base + extra


def format_inr(value) -> str:
    """
    Format a currency amount Indian-style: comma grouping by lakh/crore
    (last 3 digits, then groups of 2 going left — 1,00,000.00 not
    100,000.00) with exactly 2 decimal places. Used for every currency
    display across the app (Sales, Purchase, Items, Vendors, Customers,
    Dashboard) — display only. Never apply this to the value attribute of
    an editable <input type="number">; browsers reject comma-formatted
    numeric input, so those stay plain (see the `inr` JS helper in
    base.html for the client-side equivalent, used the same way — on
    display spans only, never on input values).
    """
    if value is None:
        return "0.00"
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    negative = d < 0
    d = abs(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    int_part, dec_part = format(d, "f").split(".")
    if len(int_part) > 3:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        int_part = ",".join(groups) + "," + last3
    return ("-" if negative else "") + int_part + "." + dec_part


# Currency code -> display symbol, for format_money below. Not exhaustive —
# just the currencies this app's vendors actually invoice in. Anything not
# listed here falls back to showing the 3-letter code itself (e.g. "JPY
# 1,234.00"), which is always unambiguous even if less pretty.
CURRENCY_SYMBOLS = {
    "INR": "Rs.",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "CAD": "CA$",
    "AED": "AED ",
    "SGD": "S$",
}


def format_money(value, currency: str = "INR") -> str:
    """Currency-aware amount formatting for PurchaseOrder.total_amount /
    PurchaseOrderItem.unit_price, which (since the multi-currency addition)
    can be in the vendor's own invoicing currency rather than always INR —
    see PurchaseOrder.currency/exchange_rate in models.py. INR keeps the
    existing lakh/crore grouping via format_inr unchanged (everything else
    in the app is still INR-only — Sales, Items, Vendors, Customers,
    Dashboard — so this filter is purchase-order-specific for now). Any
    other currency uses plain international 3-digit grouping, since lakh
    grouping is a rupee convention, not a general one.

    Like format_inr, this is display-only — the stored value is untouched.
    Never apply to the value of an editable <input type="number"> (see
    format_inr's docstring); use the `formatMoney` JS helper in base.html
    for the client-side equivalent instead.
    """
    if not currency or currency == "INR":
        return format_inr(value)
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ")
    if value is None:
        return symbol + "0.00"
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    negative = d < 0
    d = abs(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return ("-" if negative else "") + symbol + format(d, ",.2f")


# Freight on Sales/Purchase orders is always taxed at this flat GST rate.
FREIGHT_GST_PERCENT = 18


def parse_freight(value) -> float:
    """Freight amount from a form field: blank/invalid/negative -> 0."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(amount, 2) if amount > 0 else 0.0


def parse_round_off(value) -> float:
    """Manual round-off from a form field (may be negative); blank/invalid -> 0."""
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def order_totals(lines_total, lines_gst, freight, round_off):
    """(total_amount, gst_amount, round_off) for a sales/purchase order.

    total_amount includes freight and the manually-entered round_off (can be
    negative); gst_amount is left untouched by the round-off.
    """
    round_off = round(round_off, 2)
    return round(lines_total + freight + round_off, 2), round(lines_gst, 2), round_off
