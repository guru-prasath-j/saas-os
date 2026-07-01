"""Rule-based + LLM transaction categorizer for Indian bank transactions.

Keyword matching runs first (instant, no API cost).
LLM is used only for merchants that don't match any rule.
"""
from __future__ import annotations
import re

# ---------------------------------------------------------------------------
# Rule table — (keywords, category)
# Each keyword matched as case-insensitive substring of merchant/description.
# ---------------------------------------------------------------------------

_RULES: list[tuple[set[str], str]] = [
    # Food & Dining
    ({"swiggy", "zomato", "bigbasket", "blinkit", "dunzo", "zepto", "grofers",
      "restaurant", "cafe", "coffee", "bakery", "dhaba", "liquor", "beer", "wine",
      "dineout", "eatsure", "freshmenu", "box8", "faasos", "mcdonalds", "mcd",
      "kfc", "dominos", "pizza", "burger", "subway", "starbucks", "chaayos",
      "chai point", "haldirams", "barbeque", "biryani", "madhuloka",
      "dining", "food", "kitchen", "canteen", "mess", "tiffin", "snacks",
      "eat", "hotel restaurant", "juice", "bakehouse"}, "Food"),

    # Transport
    ({"uber", "ola", "rapido", "irctc", "indigo", "spicejet", "air india",
      "goair", "vistara", "petrol", "fuel", "parking", "bmtc", "ksrtc",
      "msrtc", "cab", "taxi", "yulu", "bounce", "vogo", "airways",
      "airline", "railway", "toll", "fastag", "metro card"}, "Transport"),

    # Utilities
    ({"electricity", "bescom", "tangedco", "tneb", "cesc", "bses", "msedcl",
      "water board", "piped gas", "bsnl", "airtel", "jio", "vodafone", "vi-",
      "recharge", "broadband", "dth", "tataplay", "suncable", "hathway",
      "act fibernet", "postpaid", "bill payment", "ebill", "utility"}, "Utilities"),

    # Entertainment
    ({"netflix", "amazon prime", "hotstar", "disney+", "spotify", "wynk",
      "gaana", "youtube premium", "pvr", "inox", "cinemas", "bookmyshow",
      "steam", "playstation", "xbox", "zee5", "sonyliv", "jiocinema",
      "mxplayer", "voot", "altbalaji", "lionsgate", "apple tv"}, "Entertainment"),

    # Health & Medical
    ({"pharmacy", "hospital", "doctor", "clinic", "apollo", "medplus", "1mg",
      "netmeds", "practo", "pharmeasy", "fortis", "manipal", "narayana",
      "diagnostic", "lab test", "chemist", "medical store", "dental",
      "optician", "ayurveda", "cult.fit", "gym", "fitness", "wellness"}, "Health"),

    # Shopping & Retail
    ({"amazon", "flipkart", "myntra", "ajio", "meesho", "reliance retail",
      "dmart", "big bazaar", "spencers", "croma", "vijay sales", "ikea",
      "decathlon", "pepperfry", "urban ladder", "nykaa", "purplle",
      "lenskart", "firstcry", "snapdeal", "shopsy", "jiomart",
      "supermarket", "hypermarket", "general store", "kirana"}, "Shopping"),

    # Investment & Savings
    ({"sip-", " sip ", "mutual fund", "ppf", " nps", "nps-", "fd-", " fd ",
      "fixed deposit", "hdfc securities", "zerodha", "groww", "kuvera",
      "paytm money", "scripbox", "investment", "demat", "ipo-", "dividend",
      "smallcase", "angel broking", "5paisa", "upstox"}, "Investment"),

    # Rent & Housing
    ({"rent", "maintenance charge", "society fee", "apartment", " pg ",
      "nobroker", "nestaway", "stanza", "colive", "zolo",
      "housing loan emi", "home loan"}, "Rent"),

    # Education
    ({"school fee", "college fee", "tuition", "udemy", "coursera", "unacademy",
      "byju", "vedantu", "toppr", "simplilearn", "upgrad", "exam fee",
      "admission fee", "coaching", "whiteboard"}, "Education"),

    # Travel & Hotels
    ({"makemytrip", "cleartrip", "goibibo", "yatra", "oyo rooms", "airbnb",
      "booking.com", "treebo", "fabhotels", "itc hotel", "marriott",
      "hilton", "taj hotel", "club mahindra", "thomas cook"}, "Travel"),

    # Insurance
    ({"insurance", "lic ", "hdfc life", "icici pru", "bajaj allianz",
      "star health", "niva bupa", "religare", "care health",
      "term plan", "ulip", "policy premium"}, "Insurance"),

    # EMI / Loan repayment
    ({"emi", "equated monthly", "loan repay", "loan emi", "home loan emi",
      "car loan", "personal loan", "cc emi", "bnpl", "bajaj finance",
      "home credit", "credit card payment", "outstanding due"}, "EMI/Loan"),

    # Transfers / UPI (categorize last — many merchant names contain "upi")
    ({"phonepe", "google pay", "gpay", "paytm p2p", "bhim upi",
      "self transfer", "own account", "sweep", "account transfer",
      "fund transfer", "neft transfer", "imps transfer", "rtgs transfer"}, "Transfer"),

    # Income (salary credits, refunds, inbound NEFT — positive amounts)
    ({"salary", "payroll", "stipend", "refund", "cashback", "reward credit",
      "interest credit", "reversal",
      "credited to your a/c", "credited to your account",
      "sent by:", "neft received", "amount credited"}, "Income"),
]

_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile("|".join(re.escape(k) for k in kws), re.IGNORECASE), cat)
    for kws, cat in _RULES
]

# --- Extra regex patterns for things keywords can't cleanly handle ----------

# UPI personal name transfer: "UPI-MR GURUPRASATH-" / "UPI-MRS UMADEVI-"
_UPI_PERSON_TITLE_RE = re.compile(
    r"\bUPI-(?:MR|MRS|MS|DR|SHRI|SRI|PROF|ER|MASTER)\s+[A-Z]", re.IGNORECASE
)
# Personal UPI ID patterns (no business keyword in merchant name)
# Covers: Q-number, phone numbers, name-based IDs, CV.NAME style
# EXCLUDES @OKBIZAXIS (merchant UPI handle)
_UPI_PERSONAL_ID_RE = re.compile(
    r"UPI-[A-Z][A-Z .'-]{1,35}-"
    r"(?:"
      r"[QR]\d{6,}"                           # Q/R-number personal ID
      r"|[6-9]\d{9}"                           # 10-digit phone starting 6-9
      r"|\d{10}"                               # 10-digit generic
      r"|CV\.[A-Z]+(?:-\d+)?"                # CV.JOEVISHAL-2 style
      r"|[A-Z]{3,}[A-Z0-9]{2,}(?:-\d+)?"    # JEEVARA2002-1 / BHARATRAVIRAJA-2
      r"|[A-Z]{3,}\.[A-Z]{1,5}(?:-\d+)?"    # SHATHISH.MV style dot-separated
    r")"
    r"(?:-\d+)?@"
    r"(?:OKSBI|OKAXIS|OKICICI|OKHDFCBANK|YBL|SBIN|ICIC|HDFC|IBL|KVB|CNRB|"
    r"NAVIAXIS|AXL|AIRTEL|PAYTM|AIRP|BKID|IOBA|PUNB|UPI|BARB|IDIB|AUBL)",
    re.IGNORECASE,
)
# POS / card fee entries (bank markup, decline charges)
_POS_FEE_RE = re.compile(
    r"\b(pos.?decchg|intl.?pos|pos.?txn.?markup|pos.?markup|"
    r"dc.?intl|forex.?markup|card.?fee|annual.?fee|renewal.?fee)\b", re.IGNORECASE
)
# UPI to own name (self/fund transfer)
_SELF_TRANSFER_RE = re.compile(
    r"(self.?transfer|own.?account|neft.*to.*self|"
    r"fund.?transfer|credited.?to.?beneficiary|transfer.?to)", re.IGNORECASE
)
# Credit card bill payment
_CC_BILL_RE = re.compile(r"\b(billpay|bill.?pay|cc.?payment|credit.?card.?pay|ib.?bill)\b", re.IGNORECASE)
# ATM cash withdrawal
_ATM_RE = re.compile(r"\b(ATW[-\s]|atm.?with|cash.?with)\b", re.IGNORECASE)
# South Indian food brands & generic food patterns
# Note: no \b on some terms to catch concatenated UPI names like HOTELSHREESARAVANABA
_FOOD_EXTRA_RE = re.compile(
    r"(ambur|dum.{0,4}biry|biryani|briyani|saravana|sreesaravana|"
    r"five.?star|shreedhar|annamess|shreehotel|hotelshr|"
    r"\bmeals\b|\bfood\b|\bfood.zone\b|\btiffin\b|\bidly\b|\bdosa\b|"
    r"\bparotta\b|\bbhavan\b|\banna.mess\b|\bfuller.bite\b|\bjuice\b|"
    r"\btea.stall\b|\bwines\b|\bliquor\b|\bchicken\b|\bmutton\b|"
    r"\bmess\b|\bcanteen\b|\beatery\b|\bsnack\b|\bhot.dum\b)", re.IGNORECASE
)
# Fuel / service stations
_FUEL_RE = re.compile(
    r"\b(service.?station|fuel.?station|petrol.?bunk|filling.?station|"
    r"hp.?petrol|indian.?oil|bharat.?petroleum|essar.?fuel|reliance.?petro|"
    r"vriddhii.?fuel|rk.?service|fuels)\b", re.IGNORECASE
)
# Generic retail / mart patterns
_RETAIL_RE = re.compile(
    r"\b(mart|supermark|hypermark|general.?store|provision|traders|wholesale|"
    r"agencies|enterprises|stationery|hardware|book.?shop|vyapar|"
    r"xerox|print.?shop|textiles|garment|cloth)\b", re.IGNORECASE
)
# BharatPe / PoS / QR terminal (small merchant → Shopping)
_BHARATPE_RE = re.compile(r"bharatpe|paytmqr|wlpos\.|bhqr\.", re.IGNORECASE)
# Insurance / government schemes
_INSURANCE_EXTRA_RE = re.compile(
    r"\b(pradhan.?mantri|pmby|pmjjby|pmsby|bima|jeevan|suraksha|term.?plan)\b",
    re.IGNORECASE,
)
# Technology / SaaS subscriptions
_TECH_RE = re.compile(
    r"\b(github|aws|azure|google.?cloud|digitalocean|cloudflare|"
    r"vercel|heroku|notion|figma|canva|adobe|microsoft.?365|office.?365|"
    r"slack|zoom|dropbox|github.?inc)\b", re.IGNORECASE,
)
# SBI MOPS / government payment gateway
_GOVT_PAY_RE = re.compile(r"\b(sbimops|mopsup|sbipay|krishnanagar|municipality|"
                           r"municipal|corporation|govt|revenue.?dept)\b", re.IGNORECASE)


def categorize(merchant: str, amount: float = 0.0, notes: str = "") -> str:
    """Return a category for one transaction using rules. Never raises."""
    text = f"{merchant} {notes}".strip()
    if not text:
        return "Income" if amount > 0 else "Uncategorized"

    # 1. Keyword rules (fast path)
    for pattern, cat in _COMPILED:
        if pattern.search(text):
            return cat

    # 2. Credit card bill payment
    if _CC_BILL_RE.search(text):
        return "EMI/Loan"

    # 2b. POS / card fee
    if _POS_FEE_RE.search(text):
        return "EMI/Loan"

    # 3. ATM withdrawal
    if _ATM_RE.search(text):
        return "Transfer"

    # 4. Personal UPI transfer (title prefix like MR/MRS)
    if _UPI_PERSON_TITLE_RE.search(text):
        return "Transfer"

    # 5. Personal UPI transfer (Q-number / name-based UPI ID pattern)
    if _UPI_PERSONAL_ID_RE.search(text) and not re.search(
        r"\b(store|mart|hotel|restaurant|shop|agency|service|centre|"
        r"medical|clinic|pharmacy|fuel|station|foods?|cafe|wines|liquor)\b",
        text, re.IGNORECASE
    ):
        return "Transfer"

    # 6. Fund / self transfer
    if _SELF_TRANSFER_RE.search(text):
        return "Transfer"

    # 7. Insurance / govt schemes
    if _INSURANCE_EXTRA_RE.search(text):
        return "Insurance"

    # 8. Government payment portals
    if _GOVT_PAY_RE.search(text):
        return "Utilities"

    # 9. Tech / SaaS subscriptions
    if _TECH_RE.search(text):
        return "Entertainment"

    # 10. South Indian food brands & generic food terms
    if _FOOD_EXTRA_RE.search(text):
        return "Food"

    # 11. Fuel / service station
    if _FUEL_RE.search(text):
        return "Transport"

    # 12. Generic retail patterns
    if _RETAIL_RE.search(text):
        return "Shopping"

    # 13. BharatPe / PoS / QR terminal → Shopping
    if _BHARATPE_RE.search(text):
        return "Shopping"

    # 14. Unmatched positive amount → Income
    if amount > 0:
        return "Income"

    return "Uncategorized"


def auto_categorize_all(engine) -> int:
    """Categorize every 'Uncategorized' transaction in the DB. Returns count updated."""
    rows = engine.conn.execute(
        "SELECT id, merchant, amount, notes FROM transactions"
        " WHERE category IS NULL OR category='Uncategorized'"
    ).fetchall()
    updated = 0
    for row in rows:
        tid, merchant, amount, notes = row
        cat = categorize(merchant or "", float(amount or 0), notes or "")
        if cat != "Uncategorized":
            engine.conn.execute(
                "UPDATE transactions SET category=? WHERE id=?", (cat, tid)
            )
            updated += 1
    engine.conn.commit()
    return updated
