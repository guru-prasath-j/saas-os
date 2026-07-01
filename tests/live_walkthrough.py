"""
End-to-end live walkthrough using TestClient.
Run: python tests/live_walkthrough.py
"""
import csv
import datetime as _dt
import io
import json
import os
import sys
import tempfile

os.environ["AMY_SAAS_DATA"] = tempfile.mkdtemp(prefix="amy_live_")

from fastapi.testclient import TestClient
from amy.saas.app import app
from amy.saas.db import init_db

init_db()

client = TestClient(app, raise_server_exceptions=True)

SEP = "-" * 70

def pp(label, r):
    print(f"\n{SEP}")
    print(f"  {label}  [{r.status_code}]")
    print(SEP)
    try:
        body = r.json()
        print(json.dumps(body, indent=2, ensure_ascii=True)[:1200])
    except Exception:
        print(r.text[:800])

# ── 1. Sign up ──────────────────────────────────────────────────────────────
r = client.post("/auth/signup", json={"email": "demo@example.com", "password": "Demo1234!"})
assert r.status_code == 200, r.text
uid   = r.json()["user"]["id"]
token = r.json()["token"]
H     = {"Authorization": f"Bearer {token}"}
pp("1. SIGN UP", r)

# ── 2. Add income source ─────────────────────────────────────────────────────
r = client.post("/api/finance/income", json={"name": "Salary", "amount": 80000}, headers=H)
pp("2. ADD INCOME  (Rs.80k/month)", r)

# ── 3. Add HDFC bank account ─────────────────────────────────────────────────
r = client.post("/api/finance/accounts",
                json={"nickname": "HDFC Main", "bank_name": "HDFC", "account_type": "savings"},
                headers=H)
assert r.status_code == 200, r.text
aid = r.json()["id"]
pp("3. CREATE ACCOUNT  (HDFC Main)", r)

# ── 4. Upload HDFC CSV ──────────────────────────────────────────────────────
today = _dt.date.today()
rows = [
    {"Date": (today - _dt.timedelta(days=2)).strftime("%d/%m/%y"),
     "Narration": "SWIGGY FOOD ORDER", "Value Dt": "",
     "Withdrawal Amt.": "850.00", "Deposit Amt.": "", "Closing Balance": "79150.00"},
    {"Date": (today - _dt.timedelta(days=5)).strftime("%d/%m/%y"),
     "Narration": "AMAZON RETAIL", "Value Dt": "",
     "Withdrawal Amt.": "2999.00", "Deposit Amt.": "", "Closing Balance": "82149.00"},
    {"Date": (today - _dt.timedelta(days=6)).strftime("%d/%m/%y"),
     "Narration": "IRCTC TICKET BOOKING", "Value Dt": "",
     "Withdrawal Amt.": "1200.00", "Deposit Amt.": "", "Closing Balance": "85148.00"},
    {"Date": (today - _dt.timedelta(days=3)).strftime("%d/%m/%y"),
     "Narration": "SALARY CREDIT INFOSYS", "Value Dt": "",
     "Withdrawal Amt.": "", "Deposit Amt.": "80000.00", "Closing Balance": "86348.00"},
    {"Date": (today - _dt.timedelta(days=1)).strftime("%d/%m/%y"),
     "Narration": "ZOMATO FOOD APP", "Value Dt": "",
     "Withdrawal Amt.": "650.00", "Deposit Amt.": "", "Closing Balance": "78500.00"},
]
headers = ["Date", "Narration", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]
buf = io.StringIO()
w = csv.DictWriter(buf, fieldnames=headers)
w.writeheader()
for row in rows:
    w.writerow(row)
csv_bytes = buf.getvalue().encode()

r = client.post(f"/api/finance/accounts/{aid}/upload/csv",
                files={"file": ("statement.csv", csv_bytes, "text/csv")},
                headers=H)
pp("4. UPLOAD HDFC CSV  (5 transactions — preset auto-detected)", r)

# ── 5. Check transactions ─────────────────────────────────────────────────────
r = client.get("/api/finance/transactions?limit=10", headers=H)
pp("5. LIST TRANSACTIONS  (all imported)", r)

# ── 6. Set budget for Food & Dining (we'll overspend it) ─────────────────────
r = client.post("/api/finance/budgets",
                json={"category": "Food & Dining", "monthly_limit": 500},
                headers=H)
pp("6. SET BUDGET  Food & Dining = Rs.500/month (will be overshot)", r)

# ── 7. Add a few more food transactions directly to trigger budget overage ────
for amt, desc in [(900, "Swiggy"), (700, "Zomato")]:
    client.post("/api/finance/transactions",
                json={"amount": -amt, "category": "Food & Dining",
                      "merchant": desc, "date": today.isoformat()},
                headers=H)

# ── 8. Add subscriptions (one will be "used", one "unused") ──────────────────
client.post("/api/finance/subscriptions",
            json={"name": "Netflix", "monthly_cost": 499, "status": "active",
                  "renewal_date": (today + _dt.timedelta(days=3)).isoformat()},
            headers=H)
client.post("/api/finance/subscriptions",
            json={"name": "SomePlatform", "monthly_cost": 299, "status": "active",
                  "renewal_date": (today + _dt.timedelta(days=10)).isoformat()},
            headers=H)
print("\n  [added Netflix (bill due 3 days) + SomePlatform (10 days, no transactions)]")

# ── 9. Afford engine ─────────────────────────────────────────────────────────
r = client.post("/api/finance/afford",
                json={"amount": 15000, "description": "New mechanical keyboard"},
                headers=H)
pp("9. AFFORD ENGINE  — 'Can I afford Rs.15,000 keyboard?'", r)

# ── 10. Finance overview / dashboard ─────────────────────────────────────────
r = client.get("/api/finance/overview", headers=H)
pp("10. FINANCE OVERVIEW  (dashboard data)", r)

# ── 11. Cash-flow forecast ────────────────────────────────────────────────────
r = client.get("/api/finance/forecast/cashflow", headers=H)
pp("11. CASH-FLOW FORECAST", r)

# ── 12. Run digest manually to trigger notifications ─────────────────────────
from amy.saas import paths
from amy.collab.db import CollabDB
from amy.events.scheduler import generate_and_store

cdb = CollabDB(str(paths.index_dir(uid) / "collab.db"))
generate_and_store(cdb,
                   finance_db_path=str(paths.index_dir(uid) / "finance.db"))
cdb.close()
print(f"\n  [digest ran — notifications generated from finance conditions]")

# ── 13. Check notifications ───────────────────────────────────────────────────
r = client.get("/api/notifications", headers=H)
pp("13. NOTIFICATIONS  (all)", r)

# ── 14. AA toggle ─────────────────────────────────────────────────────────────
r = client.get("/api/me", headers=H)
pp("14. GET /api/me  (shows aa_enabled)", r)

print(f"\n{SEP}")
print("  WALKTHROUGH COMPLETE")
print(SEP)
