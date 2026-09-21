"""SQLite storage for the banking voice agent demo.

Holds the customer records used to contextualise a call, a log of calls, the
transcript of each call, and the runtime-editable agent settings.

All data here is fictional demo data.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bank.db"),
)

# Serialises writes from the Flask request threads and the WebSocket monitor
# threads. Writes are tiny, so a single lock is simpler than a pool.
_write_lock = threading.Lock()

CUSTOMER_FIELDS = [
    "full_name_en",
    "full_name_ar",
    "mobile_e164",
    "preferred_language",
    "account_type",
    "balance",
    "currency",
    "segment",
    "last_txn_amount",
    "last_txn_merchant",
    "last_txn_date",
    "open_case",
    "card_status",
    "relationship_manager",
    "branch",
    "kyc_status",
    "kyc_expiry",
    "loan_summary",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name_en         TEXT NOT NULL,
    full_name_ar         TEXT NOT NULL DEFAULT '',
    mobile_e164          TEXT NOT NULL,
    mobile_digits        TEXT NOT NULL,
    preferred_language   TEXT NOT NULL DEFAULT 'en',
    account_type         TEXT,
    balance              REAL,
    currency             TEXT DEFAULT 'QAR',
    segment              TEXT,
    last_txn_amount      REAL,
    last_txn_merchant    TEXT,
    last_txn_date        TEXT,
    open_case            TEXT,
    card_status          TEXT,
    relationship_manager TEXT,
    branch               TEXT,
    kyc_status           TEXT,
    kyc_expiry           TEXT,
    loan_summary         TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_digits ON customers(mobile_digits);

CREATE TABLE IF NOT EXISTS calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT NOT NULL UNIQUE,
    from_number  TEXT,
    customer_id  INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    matched      INTEGER NOT NULL DEFAULT 0,
    language     TEXT,
    status       TEXT NOT NULL DEFAULT 'ringing',
    started_at   TEXT NOT NULL,
    ended_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at DESC);

CREATE TABLE IF NOT EXISTS transcript_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcript_call ON transcript_lines(call_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digits_only(value):
    """Reduces any phone-number format to bare digits for matching."""
    return "".join(ch for ch in (value or "") if ch.isdigit())


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def list_customers():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM customers ORDER BY full_name_en COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def get_customer(customer_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return dict(row) if row else None


def find_customer_by_number(number):
    """Matches a caller number against stored customers.

    Trunks differ in how much of the number they send: some deliver full E.164,
    others strip the country code. So try the exact digits first, then
    progressively shorter suffixes, and only accept a suffix match when it is
    unambiguous.
    """
    target = digits_only(number)
    if len(target) < 6:
        return None

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE mobile_digits = ?", (target,)
        ).fetchone()
        if row:
            return dict(row)

        for length in (11, 10, 9, 8, 7):
            if len(target) < length:
                continue
            suffix = target[-length:]
            rows = conn.execute(
                "SELECT * FROM customers WHERE substr(mobile_digits, -?) = ?",
                (length, suffix),
            ).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
            if len(rows) > 1:
                # Ambiguous at this length, and shorter suffixes can only be
                # more ambiguous, so stop rather than guess.
                return None
    return None


def create_customer(data):
    values = {field: data.get(field) for field in CUSTOMER_FIELDS}
    values["full_name_ar"] = values.get("full_name_ar") or ""
    values["preferred_language"] = values.get("preferred_language") or "en"
    values["mobile_digits"] = digits_only(values.get("mobile_e164"))
    values["created_at"] = values["updated_at"] = now_iso()

    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    with _write_lock, connect() as conn:
        cursor = conn.execute(
            f"INSERT INTO customers ({columns}) VALUES ({placeholders})",
            list(values.values()),
        )
        return cursor.lastrowid


def update_customer(customer_id, data):
    values = {field: data[field] for field in CUSTOMER_FIELDS if field in data}
    if not values:
        return False
    if "mobile_e164" in values:
        values["mobile_digits"] = digits_only(values["mobile_e164"])
    values["updated_at"] = now_iso()

    assignments = ", ".join(f"{column} = ?" for column in values)
    with _write_lock, connect() as conn:
        cursor = conn.execute(
            f"UPDATE customers SET {assignments} WHERE id = ?",
            list(values.values()) + [customer_id],
        )
        return cursor.rowcount > 0


def delete_customer(customer_id):
    with _write_lock, connect() as conn:
        cursor = conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Calls and transcripts
# ---------------------------------------------------------------------------


def start_call(call_id, from_number, customer_id, language):
    with _write_lock, connect() as conn:
        conn.execute(
            """INSERT INTO calls (call_id, from_number, customer_id, matched, language,
                                  status, started_at)
               VALUES (?, ?, ?, ?, ?, 'in_progress', ?)
               ON CONFLICT(call_id) DO UPDATE SET
                   from_number = excluded.from_number,
                   customer_id = excluded.customer_id,
                   matched     = excluded.matched,
                   language    = excluded.language,
                   status      = 'in_progress'""",
            (call_id, from_number, customer_id, 1 if customer_id else 0, language, now_iso()),
        )


def end_call(call_id, status="completed"):
    with _write_lock, connect() as conn:
        conn.execute(
            "UPDATE calls SET status = ?, ended_at = ? WHERE call_id = ?",
            (status, now_iso(), call_id),
        )


def list_calls(limit=50):
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.*, cu.full_name_en, cu.full_name_ar
               FROM calls c
               LEFT JOIN customers cu ON cu.id = c.customer_id
               ORDER BY c.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_transcript_line(call_id, role, text):
    if not (text or "").strip():
        return
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO transcript_lines (call_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (call_id, role, text.strip(), now_iso()),
        )


def get_transcript(call_id, after_id=0):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transcript_lines WHERE call_id = ? AND id > ? ORDER BY id",
            (call_id, after_id),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_setting(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def get_settings():
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_settings(values):
    with _write_lock, connect() as conn:
        for key, value in values.items():
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )


# ---------------------------------------------------------------------------
# Seed data (fictional)
# ---------------------------------------------------------------------------

SEED_CUSTOMERS = [
    {
        "full_name_en": "Ahmed Al-Mansouri",
        "full_name_ar": "أحمد المنصوري",
        "mobile_e164": "+97455512345",
        "preferred_language": "en",
        "account_type": "Current",
        "balance": 48320.55,
        "currency": "QAR",
        "segment": "Premium",
        "last_txn_amount": 1250.00,
        "last_txn_merchant": "Lulu Hypermarket",
        "last_txn_date": "2026-09-14",
        "open_case": "Disputed charge QAR 899.00, ref CS-40281, status In review",
        "card_status": "Active",
        "relationship_manager": "Noura Al-Kuwari",
        "branch": "West Bay",
        "kyc_status": "Valid",
        "kyc_expiry": "2027-03-01",
        "loan_summary": "Auto loan, QAR 62,000 outstanding, 18 instalments left",
    },
    {
        "full_name_en": "Fatima Al-Kuwari",
        "full_name_ar": "فاطمة الكواري",
        "mobile_e164": "+97455598761",
        "preferred_language": "ar",
        "account_type": "Savings",
        "balance": 156980.20,
        "currency": "QAR",
        "segment": "Private",
        "last_txn_amount": 4300.00,
        "last_txn_merchant": "Qatar Airways",
        "last_txn_date": "2026-09-18",
        "open_case": None,
        "card_status": "Active",
        "relationship_manager": "Hassan Al-Ansari",
        "branch": "Al Sadd",
        "kyc_status": "Valid",
        "kyc_expiry": "2028-01-15",
        "loan_summary": None,
    },
    {
        "full_name_en": "Mohammed Al-Thani",
        "full_name_ar": "محمد آل ثاني",
        "mobile_e164": "+97433344556",
        "preferred_language": "ar",
        "account_type": "Current",
        "balance": 9875.40,
        "currency": "QAR",
        "segment": "Retail",
        "last_txn_amount": 320.75,
        "last_txn_merchant": "Woqod Petrol Station",
        "last_txn_date": "2026-09-19",
        "open_case": "Card replacement requested, ref CS-40315, status Pending dispatch",
        "card_status": "Blocked",
        "relationship_manager": None,
        "branch": "Al Rayyan",
        "kyc_status": "Expiring soon",
        "kyc_expiry": "2026-11-30",
        "loan_summary": None,
    },
    {
        "full_name_en": "Noura Al-Sulaiti",
        "full_name_ar": "نورة السليطي",
        "mobile_e164": "+97466677889",
        "preferred_language": "en",
        "account_type": "Joint",
        "balance": 73410.00,
        "currency": "QAR",
        "segment": "Premium",
        "last_txn_amount": 15200.00,
        "last_txn_merchant": "Qatar Insurance Company",
        "last_txn_date": "2026-09-10",
        "open_case": None,
        "card_status": "Active",
        "relationship_manager": "Noura Al-Kuwari",
        "branch": "The Pearl",
        "kyc_status": "Valid",
        "kyc_expiry": "2027-08-22",
        "loan_summary": "Mortgage, QAR 1,180,000 outstanding, 212 instalments left",
    },
    {
        "full_name_en": "Khalid Al-Emadi",
        "full_name_ar": "خالد العمادي",
        "mobile_e164": "+97477712398",
        "preferred_language": "ar",
        "account_type": "Current",
        "balance": 2310.15,
        "currency": "QAR",
        "segment": "Retail",
        "last_txn_amount": 89.50,
        "last_txn_merchant": "Talabat",
        "last_txn_date": "2026-09-20",
        "open_case": "Salary not credited, ref CS-40342, status Under investigation",
        "card_status": "Active",
        "relationship_manager": None,
        "branch": "Industrial Area",
        "kyc_status": "Valid",
        "kyc_expiry": "2027-05-09",
        "loan_summary": "Personal loan, QAR 18,500 outstanding, 24 instalments left",
    },
    {
        "full_name_en": "Sara Al-Naimi",
        "full_name_ar": "سارة النعيمي",
        "mobile_e164": "+97455423377",
        "preferred_language": "en",
        "account_type": "Savings",
        "balance": 25640.90,
        "currency": "QAR",
        "segment": "Retail",
        "last_txn_amount": 640.00,
        "last_txn_merchant": "Virgin Megastore",
        "last_txn_date": "2026-09-17",
        "open_case": None,
        "card_status": "Expiring this month",
        "relationship_manager": None,
        "branch": "Villaggio",
        "kyc_status": "Valid",
        "kyc_expiry": "2029-02-11",
        "loan_summary": None,
    },
    {
        "full_name_en": "Youssef Al-Obaidli",
        "full_name_ar": "يوسف العبيدلي",
        "mobile_e164": "+97430098120",
        "preferred_language": "ar",
        "account_type": "Current",
        "balance": 412300.00,
        "currency": "QAR",
        "segment": "Private",
        "last_txn_amount": 98000.00,
        "last_txn_merchant": "Interbank transfer",
        "last_txn_date": "2026-09-16",
        "open_case": None,
        "card_status": "Active",
        "relationship_manager": "Hassan Al-Ansari",
        "branch": "West Bay",
        "kyc_status": "Valid",
        "kyc_expiry": "2028-06-30",
        "loan_summary": "Business facility, QAR 2,400,000 outstanding, revolving",
    },
    {
        "full_name_en": "Maryam Al-Dosari",
        "full_name_ar": "مريم الدوسري",
        "mobile_e164": "+97466512044",
        "preferred_language": "en",
        "account_type": "Savings",
        "balance": 8125.75,
        "currency": "QAR",
        "segment": "Retail",
        "last_txn_amount": 210.25,
        "last_txn_merchant": "Carrefour City Center",
        "last_txn_date": "2026-09-19",
        "open_case": "Requested cheque book, ref CS-40350, status Ready for collection",
        "card_status": "Active",
        "relationship_manager": None,
        "branch": "Al Khor",
        "kyc_status": "Valid",
        "kyc_expiry": "2027-12-04",
        "loan_summary": None,
    },
]


def seed(force=False):
    """Populates the demo customers. Returns the number inserted.

    With force=True the customer table is cleared first, which is what the
    "reseed" button in the UI does.
    """
    init_db()

    with connect() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]

    if existing and not force:
        return 0

    if force:
        with _write_lock, connect() as conn:
            conn.execute("DELETE FROM customers")

    # Lets the operator put their own handset into the demo without editing code.
    demo_number = (os.environ.get("DEMO_CUSTOMER_MSISDN") or "").strip()
    records = [dict(record) for record in SEED_CUSTOMERS]
    if demo_number:
        records[0]["mobile_e164"] = demo_number

    for record in records:
        create_customer(record)

    return len(records)
