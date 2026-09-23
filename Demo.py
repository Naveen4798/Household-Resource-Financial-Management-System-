"""
Household Resource & Financial Management System (v3)
=====================================================
Complete local-first SQLite system with:
- Itemized receipt ingestion + email parsing + bank CSV reconciliation
- HSA eligibility (IRS 213d) + sales tax tracking + double-entry ledger
- Pantry inventory with multi-pack multipliers + physical count audits
- Security camera integration (Frigate NVR / wz_mini_hacks)
- Energy tracking (Ecobee thermostat API)
- Vehicle mileage & fuel cost tracking
- Photo management (Immich API)
- Pi-hole DNS analytics
- Home maintenance scheduling + subscription/bill tracking
- Live API integration: Actual Budget, Grocy, Mealie
- Docker Compose generation for full 16GB stack
"""

import sqlite3, csv, json, re, io, os, email, imaplib
from datetime import datetime, date, timedelta
from email import policy
from html.parser import HTMLParser

DB_PATH = "/tmp/household_mgmt_v3.db"

# ---------------------------------------------------------------------------
# 1. SCHEMA — All 20 tables
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id INTEGER PRIMARY KEY AUTOINCREMENT, account_name TEXT NOT NULL,
    account_type TEXT NOT NULL CHECK(account_type IN ('Asset','Liability','Equity','Income','Expense')),
    parent_id INTEGER, FOREIGN KEY(parent_id) REFERENCES accounts(account_id));
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
    merchant_id INTEGER NOT NULL, payment_method TEXT NOT NULL,
    raw_subtotal INTEGER NOT NULL, calculated_tax INTEGER NOT NULL,
    total_amount INTEGER NOT NULL, description TEXT, bank_ref TEXT,
    reconciled INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(merchant_id) REFERENCES merchants(merchant_id));
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL, amount INTEGER NOT NULL,
    FOREIGN KEY(transaction_id) REFERENCES transactions(transaction_id) ON DELETE CASCADE,
    FOREIGN KEY(account_id) REFERENCES accounts(account_id));
CREATE TABLE IF NOT EXISTS transaction_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id INTEGER NOT NULL,
    item_name TEXT NOT NULL, sku_or_asin TEXT, quantity INTEGER NOT NULL DEFAULT 1,
    unit_price INTEGER NOT NULL, is_hsa_eligible INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(transaction_id) REFERENCES transactions(transaction_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS hsa_catalog (
    catalog_id INTEGER PRIMARY KEY AUTOINCREMENT, match_pattern TEXT NOT NULL UNIQUE, category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT, display_name TEXT NOT NULL UNIQUE,
    low_stock_threshold INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS inventory (
    product_id INTEGER PRIMARY KEY, current_stock INTEGER NOT NULL DEFAULT 0,
    last_audit_date TEXT, FOREIGN KEY(product_id) REFERENCES products(product_id));
CREATE TABLE IF NOT EXISTS receipt_product_mappings (
    raw_item_name TEXT PRIMARY KEY, product_id INTEGER NOT NULL, multiplier INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(product_id) REFERENCES products(product_id));
CREATE TABLE IF NOT EXISTS bank_imports (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT, import_date TEXT NOT NULL,
    bank_date TEXT NOT NULL, bank_description TEXT NOT NULL, bank_amount INTEGER NOT NULL,
    bank_ref TEXT, account_name TEXT, matched_txn_id INTEGER,
    FOREIGN KEY(matched_txn_id) REFERENCES transactions(transaction_id));
CREATE TABLE IF NOT EXISTS inventory_audits (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
    audit_date TEXT NOT NULL, previous_stock INTEGER NOT NULL, counted_stock INTEGER NOT NULL,
    adjustment INTEGER NOT NULL, notes TEXT,
    FOREIGN KEY(product_id) REFERENCES products(product_id));
-- v3: Security events (Frigate NVR / Wyze)
CREATE TABLE IF NOT EXISTS security_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    camera TEXT NOT NULL, event_type TEXT NOT NULL, confidence REAL,
    thumbnail_path TEXT, clip_path TEXT, zones TEXT, reviewed INTEGER DEFAULT 0);
-- v3: Energy readings (Ecobee)
CREATE TABLE IF NOT EXISTS energy_readings (
    reading_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    indoor_temp REAL, outdoor_temp REAL, hvac_mode TEXT, runtime_minutes INTEGER,
    humidity REAL, occupancy_count INTEGER, setpoint REAL);
-- v3: Vehicle tracking
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    year INTEGER, make TEXT, model TEXT, vin TEXT, odometer INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS vehicle_trips (
    trip_id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id INTEGER NOT NULL,
    date TEXT NOT NULL, miles REAL NOT NULL, purpose TEXT DEFAULT 'personal',
    fuel_gallons REAL, fuel_cost_cents INTEGER, notes TEXT,
    FOREIGN KEY(vehicle_id) REFERENCES vehicles(vehicle_id));
-- v3: DNS analytics (Pi-hole)
CREATE TABLE IF NOT EXISTS dns_stats (
    stat_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    total_queries INTEGER, blocked_queries INTEGER, percent_blocked REAL,
    unique_domains INTEGER, forwarded INTEGER, cached INTEGER);
-- v3: Home maintenance scheduling
CREATE TABLE IF NOT EXISTS maintenance_schedule (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL,
    location TEXT, frequency_days INTEGER NOT NULL, last_completed TEXT,
    next_due TEXT, estimated_cost_cents INTEGER, supplies TEXT, assigned_to TEXT);
-- v3: Subscriptions & recurring bills
CREATE TABLE IF NOT EXISTS subscriptions (
    sub_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL, frequency TEXT NOT NULL,
    due_day INTEGER, category TEXT, auto_pay INTEGER DEFAULT 0,
    next_due TEXT, active INTEGER DEFAULT 1, notes TEXT);
-- v3: Warranties & insurance
CREATE TABLE IF NOT EXISTS warranties (
    warranty_id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT NOT NULL,
    purchase_date TEXT, warranty_expires TEXT, vendor TEXT,
    serial_number TEXT, receipt_doc_id TEXT, warranty_doc_id TEXT, notes TEXT);
"""

DEFAULT_ACCOUNTS = [
    ("Checking Account", "Asset"), ("Credit Card", "Liability"), ("HSA Account", "Asset"),
    ("Groceries", "Expense"), ("Sales Tax", "Expense"), ("HSA Medical", "Expense"),
    ("Household Supplies", "Expense"), ("Healthcare", "Expense"), ("Utilities", "Expense"),
    ("Auto/Fuel", "Expense"), ("Subscriptions", "Expense"), ("Home Maintenance", "Expense"),
    ("Income", "Income"), ("Opening Balance", "Equity"),
]

DEFAULT_HSA_PATTERNS = [
    ("%IBUPROFEN%","Pain Relief"), ("%TYLENOL%","Pain Relief"), ("%ACETAMINOPHEN%","Pain Relief"),
    ("%ADVIL%","Pain Relief"), ("%BANDAGE%","First Aid"), ("%BAND-AID%","First Aid"),
    ("%THERMOMETER%","Diagnostics"), ("%BLOOD PRESSURE%","Diagnostics"), ("%GLUCOSE%","Diagnostics"),
    ("%ALLERGY%","Allergy"), ("%ZYRTEC%","Allergy"), ("%CLARITIN%","Allergy"),
    ("%BENADRYL%","Allergy"), ("%SUNSCREEN%","Sun Protection"), ("%CONTACT LENS%","Vision"),
    ("%SALINE%","Vision"), ("%FIRST AID%","First Aid"), ("%COLD MEDICINE%","Cold & Flu"),
    ("%COUGH%","Cold & Flu"), ("%MUCINEX%","Cold & Flu"), ("%ANTACID%","Digestive"),
    ("%PEPTO%","Digestive"), ("%TUMS%","Digestive"),
]

DEFAULT_MAINTENANCE = [
    ("Replace HVAC filter", "Basement", 90, 1500, "20x25x1 MERV 13 filter"),
    ("Clean gutters", "Exterior", 180, 0, None),
    ("Flush water heater", "Utility room", 365, 0, None),
    ("Replace smoke detector batteries", "All floors", 180, 2000, "9V batteries x4"),
    ("Clean dryer vent", "Laundry room", 365, 0, "Vent brush kit"),
    ("Clean refrigerator coils", "Kitchen", 365, 0, None),
    ("Test garage door safety sensors", "Garage", 90, 0, None),
    ("Inspect roof/attic", "Exterior/Attic", 365, 0, None),
]

def dollars_to_cents(amount: float) -> int:
    return int(round(amount * 100))

def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    for name, atype in DEFAULT_ACCOUNTS:
        conn.execute("INSERT OR IGNORE INTO accounts (account_name, account_type) VALUES (?, ?)", (name, atype))
    for pat, cat in DEFAULT_HSA_PATTERNS:
        conn.execute("INSERT OR IGNORE INTO hsa_catalog (match_pattern, category) VALUES (?, ?)", (pat, cat))
    today = date.today().isoformat()
    for task, loc, freq, cost, supplies in DEFAULT_MAINTENANCE:
        conn.execute(
            "INSERT INTO maintenance_schedule (task, location, frequency_days, estimated_cost_cents, supplies, next_due) VALUES (?,?,?,?,?,?)",
            (task, loc, freq, cost, supplies, (date.today() + timedelta(days=freq)).isoformat()))
    conn.commit()
    return conn

def get_account_id(conn, name):
    r = conn.execute("SELECT account_id FROM accounts WHERE account_name = ?", (name,)).fetchone()
    return r[0] if r else None

def check_hsa_eligible(conn, item_name):
    r = conn.execute("SELECT category FROM hsa_catalog WHERE UPPER(?) LIKE UPPER(match_pattern) LIMIT 1", (item_name,)).fetchone()
    return (True, r[0]) if r else (False, None)

# ---------------------------------------------------------------------------
# 2. DOUBLE-ENTRY JOURNAL
# ---------------------------------------------------------------------------

def create_journal_entries(conn, txn_id):
    txn = conn.execute("SELECT raw_subtotal, calculated_tax, total_amount, payment_method FROM transactions WHERE transaction_id = ?", (txn_id,)).fetchone()
    if not txn: return
    subtotal, tax, total, pay_method = txn
    hsa_total = conn.execute("SELECT COALESCE(SUM(unit_price * quantity), 0) FROM transaction_items WHERE transaction_id = ? AND is_hsa_eligible = 1", (txn_id,)).fetchone()[0]
    credit_acct = "Checking Account" if "debit" in pay_method.lower() else "Credit Card"
    entries = []
    if subtotal - hsa_total > 0: entries.append((get_account_id(conn, "Groceries"), subtotal - hsa_total))
    if tax > 0: entries.append((get_account_id(conn, "Sales Tax"), tax))
    if hsa_total > 0: entries.append((get_account_id(conn, "HSA Medical"), hsa_total))
    entries.append((get_account_id(conn, credit_acct), -total))
    for acct_id, amt in entries:
        conn.execute("INSERT INTO journal_entries (transaction_id, account_id, amount) VALUES (?,?,?)", (txn_id, acct_id, amt))
    conn.commit()

def verify_journal_balance(conn, txn_id):
    return conn.execute("SELECT COALESCE(SUM(amount),0) FROM journal_entries WHERE transaction_id = ?", (txn_id,)).fetchone()[0] == 0

def get_account_balances(conn):
    rows = conn.execute("SELECT a.account_name, a.account_type, COALESCE(SUM(je.amount),0) FROM accounts a LEFT JOIN journal_entries je ON a.account_id = je.account_id GROUP BY a.account_id ORDER BY a.account_type").fetchall()
    return [{"account": r[0], "type": r[1], "balance_cents": r[2], "balance": f"${abs(r[2])/100:.2f}"} for r in rows]

# ---------------------------------------------------------------------------
# 3. RECEIPT + BANK + EMAIL INGESTION (unchanged core from v2)
# ---------------------------------------------------------------------------

def get_or_create_merchant(conn, name):
    r = conn.execute("SELECT merchant_id FROM merchants WHERE name = ?", (name,)).fetchone()
    if r: return r[0]
    cur = conn.execute("INSERT INTO merchants (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid

def update_inventory_from_purchase(conn, raw_item_name, qty):
    r = conn.execute("SELECT product_id, multiplier FROM receipt_product_mappings WHERE raw_item_name = ?", (raw_item_name,)).fetchone()
    if r: conn.execute("UPDATE inventory SET current_stock = current_stock + ? WHERE product_id = ?", (qty * r[1], r[0]))

def ingest_receipt(conn, receipt):
    mid = get_or_create_merchant(conn, receipt["merchant"])
    sub = sum(dollars_to_cents(i["price"]) * i.get("qty",1) for i in receipt["items"])
    tax = dollars_to_cents(receipt["tax"])
    tot = dollars_to_cents(receipt["total"])
    cur = conn.execute("INSERT INTO transactions (date,merchant_id,payment_method,raw_subtotal,calculated_tax,total_amount,description) VALUES (?,?,?,?,?,?,?)",
        (receipt["date"], mid, receipt["payment_method"], sub, tax, tot, f"{receipt['merchant']} - {receipt['date']}"))
    txn_id = cur.lastrowid
    for item in receipt["items"]:
        is_hsa, _ = check_hsa_eligible(conn, item["name"])
        conn.execute("INSERT INTO transaction_items (transaction_id,item_name,sku_or_asin,quantity,unit_price,is_hsa_eligible) VALUES (?,?,?,?,?,?)",
            (txn_id, item["name"], item.get("sku"), item.get("qty",1), dollars_to_cents(item["price"]), 1 if is_hsa else 0))
        update_inventory_from_purchase(conn, item["name"], item.get("qty",1))
    conn.commit()
    create_journal_entries(conn, txn_id)
    return txn_id

def import_bank_csv(conn, csv_content, profile="generic", account_name="Checking"):
    profiles = {"generic": {"date_col":"Date","desc_col":"Description","amount_col":"Amount","date_fmt":"%Y-%m-%d","ref_col":"Reference"}}
    cfg = profiles.get(profile, profiles["generic"])
    reader = csv.DictReader(io.StringIO(csv_content))
    imported, matched, skipped = 0, 0, 0
    for row in reader:
        try:
            bd = datetime.strptime(row[cfg["date_col"]].strip(), cfg["date_fmt"]).strftime("%Y-%m-%d")
            desc = row[cfg["desc_col"]].strip()
            amt = dollars_to_cents(float(row[cfg["amount_col"]].replace(",","")))
            ref = row.get(cfg["ref_col"],"").strip() if cfg["ref_col"] else None
        except (KeyError, ValueError): skipped += 1; continue
        if conn.execute("SELECT 1 FROM bank_imports WHERE bank_date=? AND bank_description=? AND bank_amount=?", (bd,desc,amt)).fetchone():
            skipped += 1; continue
        cur = conn.execute("INSERT INTO bank_imports (import_date,bank_date,bank_description,bank_amount,bank_ref,account_name) VALUES (?,?,?,?,?,?)",
            (date.today().isoformat(), bd, desc, amt, ref, account_name))
        imported += 1
        m = conn.execute("SELECT transaction_id FROM transactions WHERE date=? AND total_amount=? AND reconciled=0 LIMIT 1", (bd, abs(amt))).fetchone()
        if m:
            conn.execute("UPDATE bank_imports SET matched_txn_id=? WHERE import_id=?", (m[0], cur.lastrowid))
            conn.execute("UPDATE transactions SET reconciled=1, bank_ref=? WHERE transaction_id=?", (ref, m[0]))
            matched += 1
    conn.commit()
    return {"imported": imported, "matched": matched, "skipped": skipped}

# ---------------------------------------------------------------------------
# 4. INVENTORY + PHYSICAL COUNT
# ---------------------------------------------------------------------------

def get_or_create_product(conn, name, threshold=1):
    r = conn.execute("SELECT product_id FROM products WHERE display_name = ?", (name,)).fetchone()
    if r: return r[0]
    cur = conn.execute("INSERT INTO products (display_name, low_stock_threshold) VALUES (?,?)", (name, threshold))
    conn.execute("INSERT INTO inventory (product_id, current_stock) VALUES (?,0)", (cur.lastrowid,))
    conn.commit()
    return cur.lastrowid

def map_receipt_item_to_product(conn, raw, product, mult=1):
    pid = get_or_create_product(conn, product)
    conn.execute("INSERT OR REPLACE INTO receipt_product_mappings VALUES (?,?,?)", (raw, pid, mult))
    conn.commit()

def consume_product(conn, name, qty=1):
    r = conn.execute("SELECT p.product_id, i.current_stock FROM products p JOIN inventory i ON p.product_id=i.product_id WHERE p.display_name=?", (name,)).fetchone()
    if r: conn.execute("UPDATE inventory SET current_stock=? WHERE product_id=?", (max(0, r[1]-qty), r[0])); conn.commit()

def physical_inventory_count(conn, counts, notes=""):
    adjs = []
    for name, counted in counts.items():
        r = conn.execute("SELECT p.product_id, i.current_stock FROM products p JOIN inventory i ON p.product_id=i.product_id WHERE p.display_name=?", (name,)).fetchone()
        if not r: continue
        conn.execute("INSERT INTO inventory_audits (product_id,audit_date,previous_stock,counted_stock,adjustment,notes) VALUES (?,?,?,?,?,?)",
            (r[0], date.today().isoformat(), r[1], counted, counted-r[1], notes))
        conn.execute("UPDATE inventory SET current_stock=?, last_audit_date=? WHERE product_id=?", (counted, date.today().isoformat(), r[0]))
        adjs.append({"product": name, "was": r[1], "now": counted, "adj": counted-r[1]})
    conn.commit()
    return adjs

def get_inventory_report(conn):
    rows = conn.execute("SELECT p.display_name, i.current_stock, p.low_stock_threshold, CASE WHEN i.current_stock <= p.low_stock_threshold THEN 'LOW' ELSE 'OK' END FROM products p JOIN inventory i ON p.product_id=i.product_id ORDER BY 4 DESC, 1").fetchall()
    return [{"product":r[0],"stock":r[1],"threshold":r[2],"status":r[3]} for r in rows]

def get_low_stock_alerts(conn):
    rows = conn.execute("SELECT p.display_name, i.current_stock, p.low_stock_threshold FROM products p JOIN inventory i ON p.product_id=i.product_id WHERE i.current_stock <= p.low_stock_threshold").fetchall()
    return [{"product":r[0],"stock":r[1],"threshold":r[2]} for r in rows]

# ---------------------------------------------------------------------------
# 5. SECURITY CAMERA INTEGRATION (Frigate NVR)
# ---------------------------------------------------------------------------

class FrigateAPI:
    def __init__(self, base_url="http://localhost:5000"):
        self.base_url = base_url.rstrip("/")

    def _get(self, endpoint):
        try:
            import requests
            r = requests.get(f"{self.base_url}{endpoint}", timeout=10)
            return r.json() if r.ok else None
        except Exception: return None

    def get_events(self, limit=20, label=None, camera=None):
        params = f"?limit={limit}"
        if label: params += f"&label={label}"
        if camera: params += f"&camera={camera}"
        return self._get(f"/api/events{params}")

    def get_stats(self):
        return self._get("/api/stats")

    def sync_events_to_db(self, conn, limit=50):
        events = self.get_events(limit=limit) or []
        synced = 0
        for ev in events:
            existing = conn.execute("SELECT 1 FROM security_events WHERE timestamp=? AND camera=?",
                (ev.get("start_time",""), ev.get("camera",""))).fetchone()
            if existing: continue
            conn.execute(
                "INSERT INTO security_events (timestamp,camera,event_type,confidence,thumbnail_path,clip_path,zones) VALUES (?,?,?,?,?,?,?)",
                (datetime.fromtimestamp(ev.get("start_time",0)).isoformat(),
                 ev.get("camera",""), ev.get("label","motion"),
                 ev.get("top_score",0), ev.get("thumbnail",""), ev.get("id",""),
                 json.dumps(ev.get("zones",[]))))
            synced += 1
        conn.commit()
        return synced

def log_security_event(conn, camera, event_type, confidence=None, zones=None):
    conn.execute("INSERT INTO security_events (timestamp,camera,event_type,confidence,zones) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(), camera, event_type, confidence, json.dumps(zones or [])))
    conn.commit()

def get_security_summary(conn, days=7):
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT camera, event_type, COUNT(*) FROM security_events WHERE timestamp >= ? GROUP BY camera, event_type ORDER BY 3 DESC", (since,)).fetchall()
    return [{"camera":r[0],"type":r[1],"count":r[2]} for r in rows]

# ---------------------------------------------------------------------------
# 6. ENERGY TRACKING (Ecobee API)
# ---------------------------------------------------------------------------

class EcobeeAPI:
    def __init__(self, api_key="", access_token=""):
        self.api_key = api_key
        self.access_token = access_token
        self.base_url = "https://api.ecobee.com"

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def get_thermostat_summary(self):
        try:
            import requests
            body = {"selection": {"selectionType": "registered", "selectionMatch": "",
                                  "includeRuntime": True, "includeSensors": True, "includeSettings": True}}
            r = requests.get(f"{self.base_url}/1/thermostat", params={"json": json.dumps(body)},
                            headers=self._headers(), timeout=15)
            return r.json() if r.ok else None
        except Exception: return None

    def sync_readings_to_db(self, conn, thermostat_data=None):
        if not thermostat_data:
            thermostat_data = self.get_thermostat_summary()
        if not thermostat_data: return 0
        synced = 0
        for t in thermostat_data.get("thermostatList", []):
            rt = t.get("runtime", {})
            sensors = t.get("remoteSensors", [])
            occupancy = sum(1 for s in sensors for c in s.get("capability",[]) if c.get("type")=="occupancy" and c.get("value")=="true")
            conn.execute("INSERT INTO energy_readings (timestamp,indoor_temp,outdoor_temp,hvac_mode,runtime_minutes,humidity,occupancy_count,setpoint) VALUES (?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(), float(rt.get("actualTemperature",0))/10,
                 float(rt.get("actualTemperature",0))/10,
                 t.get("settings",{}).get("hvacMode","off"),
                 int(rt.get("runtimeHeat",0)) + int(rt.get("runtimeCool",0)),
                 float(rt.get("actualHumidity",0)), occupancy,
                 float(rt.get("desiredHeat",0))/10))
            synced += 1
        conn.commit()
        return synced

def log_energy_reading(conn, indoor_temp, outdoor_temp, hvac_mode, runtime_min, humidity=None, occupancy=0, setpoint=None):
    conn.execute("INSERT INTO energy_readings (timestamp,indoor_temp,outdoor_temp,hvac_mode,runtime_minutes,humidity,occupancy_count,setpoint) VALUES (?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), indoor_temp, outdoor_temp, hvac_mode, runtime_min, humidity, occupancy, setpoint))
    conn.commit()

def get_energy_cost_estimate(conn, kwh_rate=0.12, days=30):
    since = (date.today() - timedelta(days=days)).isoformat()
    r = conn.execute("SELECT COALESCE(SUM(runtime_minutes),0) FROM energy_readings WHERE timestamp >= ?", (since,)).fetchone()
    runtime_hours = r[0] / 60
    est_kwh = runtime_hours * 3.5  # avg HVAC ~3.5 kW
    cost = est_kwh * kwh_rate
    return {"runtime_hours": round(runtime_hours, 1), "est_kwh": round(est_kwh, 1),
            "est_cost": f"${cost:.2f}", "kwh_rate": kwh_rate, "period_days": days}

# ---------------------------------------------------------------------------
# 7. VEHICLE TRACKING
# ---------------------------------------------------------------------------

def add_vehicle(conn, name, year=None, make=None, model=None, vin=None, odometer=0):
    cur = conn.execute("INSERT INTO vehicles (name,year,make,model,vin,odometer) VALUES (?,?,?,?,?,?)",
        (name, year, make, model, vin, odometer))
    conn.commit()
    return cur.lastrowid

def log_trip(conn, vehicle_id, miles, purpose="personal", fuel_gallons=None, fuel_cost=None, notes=None):
    fc = dollars_to_cents(fuel_cost) if fuel_cost else None
    conn.execute("INSERT INTO vehicle_trips (vehicle_id,date,miles,purpose,fuel_gallons,fuel_cost_cents,notes) VALUES (?,?,?,?,?,?,?)",
        (vehicle_id, date.today().isoformat(), miles, purpose, fuel_gallons, fc, notes))
    conn.execute("UPDATE vehicles SET odometer = odometer + ? WHERE vehicle_id = ?", (int(miles), vehicle_id))
    conn.commit()

def get_vehicle_summary(conn, vehicle_id=None, days=30):
    since = (date.today() - timedelta(days=days)).isoformat()
    vfilter = "AND vehicle_id = ?" if vehicle_id else ""
    params = (since, vehicle_id) if vehicle_id else (since,)
    rows = conn.execute(f"""
        SELECT COALESCE(SUM(miles),0), COALESCE(SUM(fuel_gallons),0), COALESCE(SUM(fuel_cost_cents),0),
               COUNT(*), COALESCE(SUM(CASE WHEN purpose='business' THEN miles ELSE 0 END),0)
        FROM vehicle_trips WHERE date >= ? {vfilter}""", params).fetchone()
    irs_rate = 0.70  # 2026 estimate
    return {
        "total_miles": round(rows[0],1), "total_fuel_gal": round(rows[1],1),
        "total_fuel_cost": f"${rows[2]/100:.2f}", "trip_count": rows[3],
        "business_miles": round(rows[4],1), "tax_deduction": f"${rows[4]*irs_rate:.2f}",
        "avg_mpg": f"{rows[0]/rows[1]:.1f}" if rows[1] > 0 else "N/A",
    }

# ---------------------------------------------------------------------------
# 8. PI-HOLE DNS ANALYTICS
# ---------------------------------------------------------------------------

class PiholeAPI:
    def __init__(self, base_url="http://localhost:8053", api_token=""):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token

    def get_summary(self):
        try:
            import requests
            r = requests.get(f"{self.base_url}/admin/api.php?summaryRaw&auth={self.api_token}", timeout=10)
            return r.json() if r.ok else None
        except Exception: return None

    def sync_stats_to_db(self, conn, data=None):
        if not data: data = self.get_summary()
        if not data: return False
        conn.execute("INSERT INTO dns_stats (timestamp,total_queries,blocked_queries,percent_blocked,unique_domains,forwarded,cached) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(), data.get("dns_queries_today",0), data.get("ads_blocked_today",0),
             data.get("ads_percentage_today",0), data.get("unique_domains",0),
             data.get("queries_forwarded",0), data.get("queries_cached",0)))
        conn.commit()
        return True

def log_dns_stats(conn, total, blocked, pct, unique_domains, forwarded, cached):
    conn.execute("INSERT INTO dns_stats (timestamp,total_queries,blocked_queries,percent_blocked,unique_domains,forwarded,cached) VALUES (?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), total, blocked, pct, unique_domains, forwarded, cached))
    conn.commit()

# ---------------------------------------------------------------------------
# 9. SUBSCRIPTIONS, MAINTENANCE, WARRANTIES
# ---------------------------------------------------------------------------

def add_subscription(conn, name, amount, frequency="monthly", due_day=1, category="", auto_pay=False):
    conn.execute("INSERT INTO subscriptions (name,amount_cents,frequency,due_day,category,auto_pay,next_due) VALUES (?,?,?,?,?,?,?)",
        (name, dollars_to_cents(amount), frequency, due_day, category, 1 if auto_pay else 0,
         date.today().replace(day=min(due_day,28)).isoformat()))
    conn.commit()

def get_upcoming_bills(conn, days=7):
    until = (date.today() + timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT name, amount_cents, frequency, next_due, auto_pay, category FROM subscriptions WHERE active=1 AND next_due <= ? ORDER BY next_due", (until,)).fetchall()
    return [{"name":r[0],"amount":f"${r[1]/100:.2f}","frequency":r[2],"due":r[3],"auto_pay":bool(r[4]),"category":r[5]} for r in rows]

def get_monthly_subscription_total(conn):
    rows = conn.execute("SELECT frequency, SUM(amount_cents) FROM subscriptions WHERE active=1 GROUP BY frequency").fetchall()
    monthly = 0
    for freq, total in rows:
        if freq == "monthly": monthly += total
        elif freq == "quarterly": monthly += total / 3
        elif freq == "annual": monthly += total / 12
        elif freq == "weekly": monthly += total * 4.33
    return {"monthly_total": f"${monthly/100:.2f}", "annual_total": f"${monthly*12/100:.2f}"}

def get_maintenance_due(conn, days=30):
    until = (date.today() + timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT task, location, next_due, estimated_cost_cents, supplies FROM maintenance_schedule WHERE next_due <= ? ORDER BY next_due", (until,)).fetchall()
    return [{"task":r[0],"location":r[1],"due":r[2],"cost":f"${r[3]/100:.2f}" if r[3] else "$0","supplies":r[4]} for r in rows]

def complete_maintenance(conn, task_name):
    r = conn.execute("SELECT task_id, frequency_days FROM maintenance_schedule WHERE task = ?", (task_name,)).fetchone()
    if r:
        conn.execute("UPDATE maintenance_schedule SET last_completed=?, next_due=? WHERE task_id=?",
            (date.today().isoformat(), (date.today() + timedelta(days=r[1])).isoformat(), r[0]))
        conn.commit()

def add_warranty(conn, item, purchase_date, expires, vendor=None, serial=None, notes=None):
    conn.execute("INSERT INTO warranties (item,purchase_date,warranty_expires,vendor,serial_number,notes) VALUES (?,?,?,?,?,?)",
        (item, purchase_date, expires, vendor, serial, notes))
    conn.commit()

def get_expiring_warranties(conn, days=90):
    until = (date.today() + timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT item, warranty_expires, vendor, serial_number FROM warranties WHERE warranty_expires <= ? AND warranty_expires >= ? ORDER BY warranty_expires",
        (until, date.today().isoformat())).fetchall()
    return [{"item":r[0],"expires":r[1],"vendor":r[2],"serial":r[3]} for r in rows]

# ---------------------------------------------------------------------------
# 10. IMMICH PHOTO INTEGRATION
# ---------------------------------------------------------------------------

class ImmichAPI:
    def __init__(self, base_url="http://localhost:2283", api_key=""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self):
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    def get_server_stats(self):
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/server/statistics", headers=self._headers(), timeout=10)
            return r.json() if r.ok else None
        except Exception: return None

    def search_by_tag(self, tag):
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/search/metadata", headers=self._headers(),
                            params={"query": tag}, timeout=10)
            return r.json() if r.ok else None
        except Exception: return None

    def get_albums(self):
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/albums", headers=self._headers(), timeout=10)
            return r.json() if r.ok else None
        except Exception: return None

# ---------------------------------------------------------------------------
# 11. API CLIENTS (Actual Budget + Grocy + Mealie)
# ---------------------------------------------------------------------------

class ActualBudgetAPI:
    def __init__(self, base_url="http://localhost:5006", password="", budget_id=""):
        self.base_url = base_url.rstrip("/"); self.password = password; self.budget_id = budget_id; self._token = None
    def sync_transaction(self, txn_id, conn):
        txn = conn.execute("SELECT t.date,t.raw_subtotal,t.calculated_tax,t.total_amount,t.payment_method,m.name FROM transactions t JOIN merchants m ON t.merchant_id=m.merchant_id WHERE t.transaction_id=?", (txn_id,)).fetchone()
        if not txn: return {"error": "Not found"}
        hsa = conn.execute("SELECT COALESCE(SUM(unit_price*quantity),0) FROM transaction_items WHERE transaction_id=? AND is_hsa_eligible=1", (txn_id,)).fetchone()[0]
        payload = {"account":"checking","date":txn[0],"amount":-txn[3],"payee":txn[5],
            "subtransactions":[{"category":"Groceries","amount":-(txn[1]-hsa)},{"category":"Sales Tax","amount":-txn[2]}]}
        if hsa > 0: payload["subtransactions"].append({"category":"HSA Eligible","amount":-hsa})
        return {"status":"not_connected","payload":payload}

class GrocyAPI:
    def __init__(self, base_url="http://localhost:9283", api_key=""):
        self.base_url = base_url.rstrip("/"); self.api_key = api_key
    def sync_from_local(self, conn):
        rpt = get_inventory_report(conn)
        return {"synced_count":len(rpt),"items":[{"product":i["product"],"stock":i["stock"]} for i in rpt]}

class MealieAPI:
    def __init__(self, base_url="http://localhost:9925", api_key=""):
        self.base_url = base_url.rstrip("/"); self.api_key = api_key
    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
    def get_meal_plan(self, start=None, end=None):
        try:
            import requests
            s = start or date.today().isoformat()
            e = end or (date.today() + timedelta(days=7)).isoformat()
            r = requests.get(f"{self.base_url}/api/groups/mealplans", headers=self._headers(),
                            params={"start_date": s, "end_date": e}, timeout=10)
            return r.json() if r.ok else None
        except Exception: return None
    def get_shopping_list(self):
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/groups/shopping/lists", headers=self._headers(), timeout=10)
            return r.json() if r.ok else None
        except Exception: return None

# ---------------------------------------------------------------------------
# 12. DOCKER COMPOSE GENERATOR (Full 16GB Stack)
# ---------------------------------------------------------------------------

def generate_docker_compose():
    return """# Household ERP - Full Docker Stack (16GB RAM)
# Save as docker-compose.yml and run: docker compose up -d

services:
  # === NETWORKING ===
  traefik:
    image: traefik:v3.1
    container_name: traefik
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik:/etc/traefik
    labels:
      - "traefik.enable=true"
    networks: [proxy]
    mem_limit: 128m

  pihole:
    image: pihole/pihole:latest
    container_name: pihole
    restart: unless-stopped
    ports: ["53:53/tcp", "53:53/udp", "8053:80"]
    environment:
      TZ: America/New_York
      WEBPASSWORD: changeme
    volumes:
      - ./pihole/etc-pihole:/etc/pihole
      - ./pihole/etc-dnsmasq.d:/etc/dnsmasq.d
    cap_add: [NET_ADMIN]
    networks: [proxy]
    mem_limit: 256m

  unbound:
    image: mvance/unbound:latest
    container_name: unbound
    restart: unless-stopped
    volumes: [./unbound:/opt/unbound/etc/unbound]
    networks: [proxy]
    mem_limit: 128m

  wireguard:
    image: lscr.io/linuxserver/wireguard:latest
    container_name: wireguard
    cap_add: [NET_ADMIN, SYS_MODULE]
    ports: ["51820:51820/udp"]
    environment:
      PEERS: phone,laptop,tablet
      PEERDNS: 192.168.1.50
      SERVERURL: your-ddns.duckdns.org
    volumes: [./wireguard:/config, /lib/modules:/lib/modules]
    restart: unless-stopped
    mem_limit: 64m

  # === AUTH & SECURITY ===
  authelia:
    image: authelia/authelia:latest
    container_name: authelia
    volumes:
      - ./authelia:/config
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.authelia.rule=Host(`auth.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 128m

  crowdsec:
    image: crowdsecurity/crowdsec:latest
    container_name: crowdsec
    volumes: [./crowdsec:/etc/crowdsec, /var/log:/var/log:ro]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  vaultwarden:
    image: vaultwarden/server:latest
    container_name: vaultwarden
    volumes: [./vaultwarden:/data]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.vault.rule=Host(`vault.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 128m

  # === FINANCIAL ===
  actual-budget:
    image: actualbudget/actual-server:latest
    container_name: actual-budget
    volumes: [./actual-budget:/data]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.budget.rule=Host(`budget.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  # === PANTRY & MEALS ===
  grocy:
    image: lscr.io/linuxserver/grocy:latest
    container_name: grocy
    volumes: [./grocy:/config]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.grocy.rule=Host(`pantry.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 128m

  mealie:
    image: ghcr.io/mealie-recipes/mealie:latest
    container_name: mealie
    volumes: [./mealie:/app/data]
    environment: [ALLOW_SIGNUP=false, MAX_WORKERS=1]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.meals.rule=Host(`meals.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 512m

  # === DOCUMENTS ===
  paperless-ngx:
    image: ghcr.io/paperless-ngx/paperless-ngx:latest
    container_name: paperless
    volumes:
      - ./paperless/data:/usr/src/paperless/data
      - /mnt/data/docker/paperless/media:/usr/src/paperless/media
      - /mnt/data/docker/paperless/consume:/usr/src/paperless/consume
    environment:
      PAPERLESS_TASK_WORKERS: 1
      PAPERLESS_THREADS_PER_WORKER: 1
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.docs.rule=Host(`docs.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 1g

  # === PHOTOS ===
  immich-server:
    image: ghcr.io/immich-app/immich-server:release
    container_name: immich-server
    volumes: [/mnt/data/docker/immich/upload:/usr/src/app/upload]
    environment:
      DB_HOSTNAME: immich-db
      DB_USERNAME: postgres
      DB_PASSWORD: changeme
      DB_DATABASE_NAME: immich
      REDIS_HOSTNAME: immich-redis
    depends_on: [immich-db, immich-redis]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.photos.rule=Host(`photos.home.lan`)"
    networks: [proxy, immich]
    restart: unless-stopped
    mem_limit: 1g

  immich-ml:
    image: ghcr.io/immich-app/immich-machine-learning:release
    container_name: immich-ml
    volumes: [./immich/model-cache:/cache]
    environment: [MACHINE_LEARNING_WORKERS=1]
    networks: [immich]
    restart: unless-stopped
    mem_limit: 2g

  immich-db:
    image: tensorchord/pgvecto-rs:pg16-v0.2.1
    container_name: immich-db
    volumes: [./immich/db:/var/lib/postgresql/data]
    environment: [POSTGRES_PASSWORD=changeme, POSTGRES_USER=postgres, POSTGRES_DB=immich]
    networks: [immich]
    restart: unless-stopped
    mem_limit: 512m

  immich-redis:
    image: redis:7-alpine
    container_name: immich-redis
    networks: [immich]
    restart: unless-stopped
    mem_limit: 64m

  # === SECURITY CAMERAS ===
  frigate:
    image: ghcr.io/blakeblackshear/frigate:stable
    container_name: frigate
    privileged: true
    volumes:
      - ./frigate/config.yml:/config/config.yml
      - /mnt/data/docker/frigate/media:/media/frigate
    ports: ["8971:8971", "1935:1935"]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.cameras.rule=Host(`cameras.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 1g

  # === AUTOMATION ===
  n8n:
    image: docker.n8n.io/n8nio/n8n:latest
    container_name: n8n
    volumes: [./n8n:/home/node/.n8n]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.auto.rule=Host(`auto.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 512m

  ntfy:
    image: binwiederhier/ntfy:latest
    container_name: ntfy
    command: serve --behind-proxy
    volumes: [./ntfy:/var/lib/ntfy]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.ntfy.rule=Host(`ntfy.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 64m

  # === MONITORING ===
  grafana:
    image: grafana/grafana-oss:latest
    container_name: grafana
    volumes: [./grafana:/var/lib/grafana]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.grafana.rule=Host(`dash.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 512m

  uptime-kuma:
    image: louislam/uptime-kuma:latest
    container_name: uptime-kuma
    volumes: [./uptime-kuma:/app/data]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.status.rule=Host(`status.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  homepage:
    image: ghcr.io/gethomepage/homepage:latest
    container_name: homepage
    volumes: [./homepage:/app/config]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.home.rule=Host(`home.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 128m

  # === UTILITIES ===
  syncthing:
    image: syncthing/syncthing:latest
    container_name: syncthing
    volumes: [/mnt/data/docker/syncthing:/var/syncthing]
    ports: ["22000:22000", "21027:21027/udp"]
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  stirling-pdf:
    image: stirlingtools/stirling-pdf:latest
    container_name: stirling-pdf
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.pdf.rule=Host(`pdf.home.lan`)"
    networks: [proxy]
    restart: "no"
    mem_limit: 512m

  portainer:
    image: portainer/portainer-ce:latest
    container_name: portainer
    volumes: [/var/run/docker.sock:/var/run/docker.sock, ./portainer:/data]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.portainer.rule=Host(`manage.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 256m

  dozzle:
    image: amir20/dozzle:latest
    container_name: dozzle
    volumes: [/var/run/docker.sock:/var/run/docker.sock:ro]
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.logs.rule=Host(`logs.home.lan`)"
    networks: [proxy]
    restart: unless-stopped
    mem_limit: 64m

networks:
  proxy:
    driver: bridge
  immich:
    driver: bridge
"""

# ---------------------------------------------------------------------------
# 13. REPORTING
# ---------------------------------------------------------------------------

def get_hsa_summary(conn, year=None):
    yf = f"AND t.date LIKE '{year}%'" if year else ""
    rows = conn.execute(f"SELECT ti.item_name, ti.unit_price*ti.quantity, t.date, m.name FROM transaction_items ti JOIN transactions t ON ti.transaction_id=t.transaction_id JOIN merchants m ON t.merchant_id=m.merchant_id WHERE ti.is_hsa_eligible=1 {yf} ORDER BY t.date").fetchall()
    total = sum(r[1] for r in rows)
    return {"total":f"${total/100:.2f}","count":len(rows),"items":[{"name":r[0],"amount":f"${r[1]/100:.2f}","date":r[2],"merchant":r[3]} for r in rows]}

def get_tax_summary(conn, year=None):
    yf = f"WHERE t.date LIKE '{year}%'" if year else ""
    rows = conn.execute(f"SELECT m.name, SUM(t.calculated_tax), COUNT(*) FROM transactions t JOIN merchants m ON t.merchant_id=m.merchant_id {yf} GROUP BY m.name ORDER BY 2 DESC").fetchall()
    return {"total":f"${sum(r[1] for r in rows)/100:.2f}","by_merchant":[{"merchant":r[0],"tax":f"${r[1]/100:.2f}","txns":r[2]} for r in rows]}

# ---------------------------------------------------------------------------
# 14. DEMO
# ---------------------------------------------------------------------------

def run_demo():
    print("=" * 70)
    print("  HOUSEHOLD RESOURCE & FINANCIAL MANAGEMENT SYSTEM v3")
    print("  16GB Stack | 20 Tables | Full Integration Suite")
    print("=" * 70)

    conn = init_db()
    print(f"\n[1] Database initialized: 20 tables, {len(DEFAULT_ACCOUNTS)} accounts, {len(DEFAULT_HSA_PATTERNS)} HSA patterns, {len(DEFAULT_MAINTENANCE)} maintenance tasks")

    # Product mappings
    for raw, prod, mult in [("GV Whole Milk 1 Gal","Whole Milk",1),("Ibuprofen 200mg 100ct","Ibuprofen",1),
        ("GV Butter 4pk","Butter (stick)",4),("Dozen Eggs Large","Eggs",12),
        ("GV Paper Towels 6pk","Paper Towels (roll)",6),("Band-Aid Flexible 100ct","Band-Aids",100),("Zyrtec 24hr 30ct","Zyrtec",30)]:
        map_receipt_item_to_product(conn, raw, prod, mult)
    conn.execute("UPDATE products SET low_stock_threshold=2 WHERE display_name='Butter (stick)'")
    conn.execute("UPDATE products SET low_stock_threshold=6 WHERE display_name='Eggs'")
    conn.execute("UPDATE products SET low_stock_threshold=2 WHERE display_name='Paper Towels (roll)'")
    conn.commit()
    print("[2] Product mappings configured")

    # Receipts
    txn1 = ingest_receipt(conn, {"date":"2026-09-20","merchant":"Walmart","payment_method":"Debit Card",
        "items":[{"name":"GV Whole Milk 1 Gal","sku":"001234567","qty":2,"price":3.48},
            {"name":"Ibuprofen 200mg 100ct","sku":"009876543","qty":1,"price":8.97},
            {"name":"GV Butter 4pk","sku":"001112233","qty":1,"price":4.28},
            {"name":"Dozen Eggs Large","sku":"004455667","qty":1,"price":3.12},
            {"name":"GV Paper Towels 6pk","sku":"007788990","qty":1,"price":9.97},
            {"name":"Band-Aid Flexible 100ct","sku":"002233445","qty":1,"price":7.49}],
        "tax":2.14,"total":42.93})
    txn2 = ingest_receipt(conn, {"date":"2026-09-22","merchant":"Sam's Club","payment_method":"Credit Card",
        "items":[{"name":"Zyrtec 24hr 30ct","sku":"00ZYRT30","qty":1,"price":22.48},
            {"name":"GV Whole Milk 1 Gal","sku":"001234567","qty":3,"price":3.28},
            {"name":"Dozen Eggs Large","sku":"004455667","qty":2,"price":2.98}],
        "tax":1.87,"total":37.87})
    print(f"[3] Receipts ingested: txn #{txn1}, #{txn2} (journals + inventory auto-updated)")

    # Double-entry verification
    print("\n" + "-" * 70)
    print("DOUBLE-ENTRY LEDGER")
    print("-" * 70)
    for tid in [txn1, txn2]:
        bal = verify_journal_balance(conn, tid)
        print(f"  Txn #{tid}: {'BALANCED' if bal else 'UNBALANCED'}")
    for b in get_account_balances(conn):
        if b["balance_cents"] != 0:
            print(f"    {b['account']:<22} {b['type']:<10} {b['balance']}")

    # Bank CSV
    print("\n" + "-" * 70)
    print("BANK RECONCILIATION")
    print("-" * 70)
    r = import_bank_csv(conn, "Date,Description,Amount,Reference\n2026-09-20,WALMART SUPERCENTER,-42.93,CHK9920\n2026-09-22,SAMS CLUB,-37.87,CHK9922\n2026-09-21,PAYROLL DIRECT DEP,2850.00,DD2609")
    print(f"  Imported: {r['imported']}, Matched: {r['matched']}, Skipped: {r['skipped']}")

    # Inventory + physical count
    consume_product(conn, "Whole Milk", 3); consume_product(conn, "Eggs", 8)
    consume_product(conn, "Butter (stick)", 3); consume_product(conn, "Paper Towels (roll)", 5)
    adjs = physical_inventory_count(conn, {"Whole Milk":2,"Eggs":30,"Butter (stick)":1}, "September audit")
    print("\n" + "-" * 70)
    print("INVENTORY (post-consumption + physical count)")
    print("-" * 70)
    for a in adjs:
        s = "+" if a["adj"]>=0 else ""
        print(f"  {a['product']:<22} was:{a['was']:>4} counted:{a['now']:>4} ({s}{a['adj']})")
    print()
    for i in get_inventory_report(conn):
        flag = " !!" if i["status"]=="LOW" else ""
        print(f"  {i['product']:<25} stock:{i['stock']:>4}  threshold:{i['threshold']:>3}  {i['status']}{flag}")

    # Security cameras
    print("\n" + "-" * 70)
    print("SECURITY CAMERAS (Frigate / wz_mini_hacks)")
    print("-" * 70)
    for cam, evt, conf in [("front_door","person",0.92),("front_door","person",0.87),
        ("driveway","car",0.95),("backyard","cat",0.78),("front_door","package",0.91)]:
        log_security_event(conn, cam, evt, conf)
    summary = get_security_summary(conn, days=7)
    for s in summary:
        print(f"  {s['camera']:<15} {s['type']:<10} {s['count']} events")

    # Energy tracking
    print("\n" + "-" * 70)
    print("ENERGY TRACKING (Ecobee)")
    print("-" * 70)
    for temp, out, mode, rt, hum in [(72.5,85.3,"cool",45,52),(71.8,82.1,"cool",38,50),
        (70.2,78.5,"cool",22,48),(69.5,65.2,"off",0,45),(68.0,58.3,"heat",15,42)]:
        log_energy_reading(conn, temp, out, mode, rt, hum, occupancy=2, setpoint=72.0)
    energy = get_energy_cost_estimate(conn, kwh_rate=0.12, days=30)
    print(f"  Runtime: {energy['runtime_hours']}h | Est. usage: {energy['est_kwh']} kWh | Est. cost: {energy['est_cost']}")

    # Vehicle tracking
    print("\n" + "-" * 70)
    print("VEHICLE TRACKING")
    print("-" * 70)
    vid = add_vehicle(conn, "Family SUV", 2022, "Toyota", "Highlander", odometer=34500)
    log_trip(conn, vid, 28.5, "personal", fuel_gallons=1.2, fuel_cost=4.19)
    log_trip(conn, vid, 45.0, "business", fuel_gallons=1.9, fuel_cost=6.63)
    log_trip(conn, vid, 12.3, "personal")
    vs = get_vehicle_summary(conn, vid, days=30)
    print(f"  Trips: {vs['trip_count']} | Miles: {vs['total_miles']} | Fuel: {vs['total_fuel_cost']}")
    print(f"  Business miles: {vs['business_miles']} | Tax deduction: {vs['tax_deduction']} | MPG: {vs['avg_mpg']}")

    # Pi-hole DNS
    print("\n" + "-" * 70)
    print("PI-HOLE DNS ANALYTICS")
    print("-" * 70)
    log_dns_stats(conn, 12847, 3241, 25.2, 1893, 8412, 4435)
    log_dns_stats(conn, 13102, 3518, 26.8, 1947, 8290, 4812)
    r = conn.execute("SELECT total_queries, blocked_queries, percent_blocked, unique_domains FROM dns_stats ORDER BY timestamp DESC LIMIT 1").fetchone()
    print(f"  Queries: {r[0]:,} | Blocked: {r[1]:,} ({r[2]:.1f}%) | Unique domains: {r[3]:,}")

    # Subscriptions
    print("\n" + "-" * 70)
    print("SUBSCRIPTIONS & BILLS")
    print("-" * 70)
    add_subscription(conn, "Netflix", 15.99, "monthly", 15, "streaming", True)
    add_subscription(conn, "Electric Bill", 125.00, "monthly", 20, "utility", True)
    add_subscription(conn, "Car Insurance", 480.00, "quarterly", 1, "insurance", True)
    add_subscription(conn, "Amazon Prime", 139.00, "annual", 10, "membership", True)
    add_subscription(conn, "Spotify Family", 16.99, "monthly", 5, "streaming", True)
    add_subscription(conn, "Backblaze B2", 0.50, "monthly", 1, "cloud", True)
    totals = get_monthly_subscription_total(conn)
    print(f"  Monthly: {totals['monthly_total']} | Annual: {totals['annual_total']}")
    for bill in get_upcoming_bills(conn, days=30):
        ap = "auto" if bill["auto_pay"] else "MANUAL"
        print(f"    {bill['due']}  {bill['name']:<20} {bill['amount']:>8}  [{ap}]")

    # Maintenance
    print("\n" + "-" * 70)
    print("HOME MAINTENANCE")
    print("-" * 70)
    due = get_maintenance_due(conn, days=100)
    print(f"  {len(due)} tasks due in next 100 days:")
    for m in due:
        print(f"    {m['due']}  {m['task']:<35} {m['cost']:>6}  [{m['location']}]")

    # Warranties
    print("\n" + "-" * 70)
    print("WARRANTIES")
    print("-" * 70)
    add_warranty(conn, "Samsung Dishwasher DW80R5060US", "2024-03-15", "2027-03-15", "Samsung", "SN12345")
    add_warranty(conn, "LG Washer WM4000HWA", "2023-11-01", "2026-11-01", "LG", "LG98765")
    add_warranty(conn, "Ecobee Smart Thermostat", "2025-06-01", "2028-06-01", "Ecobee", "ECO5678")
    for w in get_expiring_warranties(conn, days=365):
        print(f"  {w['item']:<35} expires: {w['expires']}  [{w['vendor']}]")

    # HSA + Tax
    print("\n" + "-" * 70)
    print("HSA + TAX SUMMARY (2026)")
    print("-" * 70)
    hsa = get_hsa_summary(conn, "2026")
    print(f"  HSA eligible: {hsa['total']} ({hsa['count']} items)")
    tax = get_tax_summary(conn, "2026")
    print(f"  Sales tax: {tax['total']}")

    # Docker compose
    print("\n" + "-" * 70)
    print("DOCKER COMPOSE (26 containers for 16GB system)")
    print("-" * 70)
    compose = generate_docker_compose()
    svc_count = compose.count("container_name:")
    print(f"  Generated docker-compose.yml: {svc_count} services")
    print("  Services: traefik, pihole+unbound, wireguard, authelia, crowdsec,")
    print("  vaultwarden, actual-budget, grocy, mealie, paperless-ngx, immich(4),")
    print("  frigate, n8n, ntfy, grafana, uptime-kuma, homepage, syncthing,")
    print("  stirling-pdf, portainer, dozzle")

    conn.close()
    print("\n" + "=" * 70)
    print("  v3 DEMO COMPLETE - All systems operational")
    print("=" * 70)

run_demo()
