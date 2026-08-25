import csv
import io
import json
import os
import sqlite3
import logging
from datetime import date
from functools import wraps

from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

import constants
from database_service import (
    write_audit_log, get_audit_logs, count_audit_logs,
    get_recent_sales_with_totals, get_today_sales_total_cents, get_sales_by_method_for_date_and_store,
    get_assigned_store_ids, get_breakages,
    get_open_cash_session, get_open_cash_session_for_cashier, get_open_sessions_for_store,
    get_cash_sessions_for_store, get_cash_session_by_id,
    insert_denomination_counts, get_denomination_counts,
    get_payments_total_cents, get_sale_inventory_value_cents, get_breakage_total_cents,
    get_previous_daily_closing, get_daily_closing_for_date, get_recent_daily_closings,
)
from money import InvalidMoneyError, parse_to_cents, cents_to_decimal, format_cents
import business_day

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
# Database path is configurable so tests (and any future deployment) can point
# at an isolated file instead of silently writing into the dev/production DB.
# Bug found in Phase 1 audit: db()/init_db() previously read a module-level
# DATABASE constant and ignored app.config entirely, so the test suite's
# app.config["DATABASE"] = "test_pos.db" had no effect and tests were writing
# into pos.db. Fixed by resolving the path through app.config at call time.
app.config["DATABASE"] = os.environ.get("DATABASE", os.path.join(BASE_DIR, "pos.db"))

# SECURITY: Fail fast if SECRET_KEY not provided
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    raise RuntimeError("SECRET_KEY environment variable must be set before running the application")
app.config["SECRET_KEY"] = secret_key

# Enable CSRF protection
csrf = CSRFProtect(app)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','store_manager','cashier','warehouse_operator','accountant')), active INTEGER NOT NULL DEFAULT 1);
-- timezone drives Feature 3's business-day boundaries (business_day.py) —
-- never UTC or the server's local time.
CREATE TABLE IF NOT EXISTS stores (id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT DEFAULT '', timezone TEXT NOT NULL DEFAULT 'UTC', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS warehouses (id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS user_stores (user_id INTEGER NOT NULL, store_id INTEGER NOT NULL, PRIMARY KEY(user_id, store_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(store_id) REFERENCES stores(id));
CREATE TABLE IF NOT EXISTS user_warehouses (user_id INTEGER NOT NULL, warehouse_id INTEGER NOT NULL, PRIMARY KEY(user_id, warehouse_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(warehouse_id) REFERENCES warehouses(id));
-- Money: integer cents everywhere (Phase 2 fix — REAL/float was the original
-- bug that made exact reconciliation to 0 essentially impossible). cost_price
-- and sale_price are separate per the target Product model: cost_price feeds
-- COGS/loss valuation, sale_price is what POS charges and what breakages are
-- valued at per the confirmed BREAKAGE_VALUATION_METHOD="sale_price".
CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, cost_price_cents INTEGER NOT NULL DEFAULT 0, sale_price_cents INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS stock (product_id INTEGER NOT NULL, location_type TEXT NOT NULL CHECK(location_type IN ('store','warehouse')), location_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(product_id, location_type, location_id), FOREIGN KEY(product_id) REFERENCES products(id));
CREATE TABLE IF NOT EXISTS transfers (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, source_type TEXT NOT NULL, source_id INTEGER NOT NULL, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, quantity INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(product_id) REFERENCES products(id), FOREIGN KEY(user_id) REFERENCES users(id));
-- InventoryMovement (kept the existing stock_movements name/table rather than
-- a parallel duplicate). movement_type covers manual counts (ENTRY/EXIT, the
-- pre-existing mechanism) plus the target model's SALE/TRANSFER_IN/TRANSFER_OUT/
-- BREAKAGE. unit_value_cents + reference_id let a movement point back at the
-- sale/transfer/breakage that generated it.
CREATE TABLE IF NOT EXISTS stock_movements (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, location_type TEXT NOT NULL CHECK(location_type IN ('store','warehouse')), location_id INTEGER NOT NULL, movement_type TEXT NOT NULL CHECK(movement_type IN ('ENTRY','EXIT','SALE','TRANSFER_IN','TRANSFER_OUT','BREAKAGE')), quantity INTEGER NOT NULL, unit_value_cents INTEGER, reference_id INTEGER, reason TEXT DEFAULT '', user_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(product_id) REFERENCES products(id), FOREIGN KEY(user_id) REFERENCES users(id));
-- Sale no longer carries total/payment_method directly: total is SUM(sale_items),
-- tender breakdown lives in the new payments table (supports split tender,
-- and lets cash_sales_total / transfer_sales_total be computed per Feature 3).
-- cash_session_id links a sale to the cashier's register session it was rung
-- up under (Feature 3: sessions close individually before the store's daily
-- closing aggregates them). No FK constraint on this column — see the Phase 4
-- migration note in _migrate_add_columns for why.
CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL, user_id INTEGER NOT NULL, cash_session_id INTEGER, status TEXT NOT NULL CHECK(status IN ('COMPLETED','VOIDED')) DEFAULT 'COMPLETED', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(store_id) REFERENCES stores(id), FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS sale_items (id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL, subtotal_cents INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE, FOREIGN KEY(product_id) REFERENCES products(id));
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL, method TEXT NOT NULL CHECK(method IN ('CASH','TRANSFER','CARD')), amount_cents INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE);
-- cash_closures: superseded by daily_closings (Phase 4 / Feature 3). Left in
-- place, unused, rather than dropped — it held 0 rows at migration time, but
-- dropping a table outright is exactly the kind of destructive, hard-to-undo
-- change this audit is supposed to avoid making silently.
CREATE TABLE IF NOT EXISTS cash_closures (id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL, closure_date TEXT NOT NULL, cash_total REAL NOT NULL, card_total REAL NOT NULL, other_total REAL NOT NULL, notes TEXT DEFAULT '', user_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(store_id, closure_date), FOREIGN KEY(store_id) REFERENCES stores(id), FOREIGN KEY(user_id) REFERENCES users(id));

-- CashRegisterSession: a cashier's shift on one store's register. Sales ring
-- up under whichever session is OPEN for that (store, cashier) pair — the
-- /sales endpoint auto-opens one (opening_amount_cents=0) if the cashier
-- didn't explicitly declare a starting float first, so Phase 2's POS flow
-- keeps working without forcing an "open register" step in front of every
-- sale; a cashier can still declare a real opening float via /cash-sessions.
CREATE TABLE IF NOT EXISTS cash_sessions (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    cashier_id INTEGER NOT NULL,
    opening_amount_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')) DEFAULT 'OPEN',
    opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    FOREIGN KEY(store_id) REFERENCES stores(id),
    FOREIGN KEY(cashier_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_cash_sessions_store ON cash_sessions(store_id);
CREATE INDEX IF NOT EXISTS idx_cash_sessions_cashier ON cash_sessions(cashier_id);
CREATE INDEX IF NOT EXISTS idx_cash_sessions_status ON cash_sessions(status);

-- DailyClosing: per-store, per-business-day reconciliation (Feature 3's core).
-- UNIQUE(store_id, business_date) is the idempotency guard against a
-- double-submit creating two closings for the same day. Immutable once
-- inserted: no UPDATE/DELETE route is exposed, and the triggers below block
-- both at the DB layer too, same pattern as audit_logs. Corrections are new
-- rows in daily_closing_adjustments referencing this row, never an edit here.
CREATE TABLE IF NOT EXISTS daily_closings (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    business_date TEXT NOT NULL,
    previous_closing_id INTEGER,
    opening_float_cents INTEGER NOT NULL,
    cash_sales_total_cents INTEGER NOT NULL,
    transfer_sales_total_cents INTEGER NOT NULL,
    breakage_total_cents INTEGER NOT NULL,
    cash_counted_total_cents INTEGER NOT NULL,
    cash_variance_cents INTEGER NOT NULL,
    inventory_withdrawn_value_cents INTEGER NOT NULL,
    reconciliation_difference_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('FINALIZED','FINALIZED_WITH_VARIANCE')),
    justification_note TEXT,
    finalized_by INTEGER NOT NULL,
    finalized_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(store_id, business_date),
    FOREIGN KEY(store_id) REFERENCES stores(id),
    FOREIGN KEY(previous_closing_id) REFERENCES daily_closings(id),
    FOREIGN KEY(finalized_by) REFERENCES users(id)
);
CREATE TRIGGER IF NOT EXISTS daily_closings_no_update BEFORE UPDATE ON daily_closings
BEGIN SELECT RAISE(ABORT, 'daily_closings is immutable once finalized: UPDATE not allowed'); END;
CREATE TRIGGER IF NOT EXISTS daily_closings_no_delete BEFORE DELETE ON daily_closings
BEGIN SELECT RAISE(ABORT, 'daily_closings is immutable once finalized: DELETE not allowed'); END;
CREATE INDEX IF NOT EXISTS idx_daily_closings_store_date ON daily_closings(store_id, business_date);
CREATE INDEX IF NOT EXISTS idx_daily_closings_status ON daily_closings(status);

-- Corrections to a finalized closing are new rows here, referencing it —
-- never an in-place edit (see the immutability triggers above).
CREATE TABLE IF NOT EXISTS daily_closing_adjustments (
    id INTEGER PRIMARY KEY,
    daily_closing_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(daily_closing_id) REFERENCES daily_closings(id),
    FOREIGN KEY(created_by) REFERENCES users(id)
);

-- CashDenominationCount: exactly one of cash_session_id / daily_closing_id is
-- set — a session's own close-out count, or the day's final consolidated
-- count. denomination_value_cents is one of constants.CASH_DENOMINATIONS_CENTS.
CREATE TABLE IF NOT EXISTS cash_denomination_counts (
    id INTEGER PRIMARY KEY,
    cash_session_id INTEGER,
    daily_closing_id INTEGER,
    denomination_value_cents INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    CHECK ((cash_session_id IS NOT NULL) + (daily_closing_id IS NOT NULL) = 1),
    FOREIGN KEY(cash_session_id) REFERENCES cash_sessions(id),
    FOREIGN KEY(daily_closing_id) REFERENCES daily_closings(id)
);
CREATE INDEX IF NOT EXISTS idx_cash_denom_session ON cash_denomination_counts(cash_session_id);
CREATE INDEX IF NOT EXISTS idx_cash_denom_closing ON cash_denomination_counts(daily_closing_id);

-- Breakage/Rotura workflow (Feature 4). Reporting never touches stock —
-- only approval does (see /breakages/<id>/approve). value_cents/valuation_method
-- are computed and frozen at approval time, from the confirmed
-- BREAKAGE_VALUATION_METHOD="sale_price" (constants.py), so a later change to
-- that setting doesn't retroactively change already-approved historical rows.
CREATE TABLE IF NOT EXISTS breakages (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('damaged','expired','lost','other')),
    notes TEXT DEFAULT '',
    valuation_method TEXT,
    value_cents INTEGER,
    reported_by INTEGER NOT NULL,
    approved_by INTEGER,
    status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED')) DEFAULT 'PENDING',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT,
    FOREIGN KEY(store_id) REFERENCES stores(id),
    FOREIGN KEY(product_id) REFERENCES products(id),
    FOREIGN KEY(reported_by) REFERENCES users(id),
    FOREIGN KEY(approved_by) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_breakages_store ON breakages(store_id);
CREATE INDEX IF NOT EXISTS idx_breakages_status ON breakages(status);
CREATE INDEX IF NOT EXISTS idx_breakages_created ON breakages(created_at);

-- AuditLog: append-only. INSERT is done exclusively via write_audit_log() in
-- database_service.py; no update/delete helper is exposed at the application
-- layer, and the triggers below reject UPDATE/DELETE at the DB layer too as
-- defense in depth (belt-and-suspenders, since SQLite has no column/row grants).
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    role_at_time TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    before_json TEXT,
    after_json TEXT,
    store_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ip_address TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(store_id) REFERENCES stores(id)
);
CREATE TRIGGER IF NOT EXISTS audit_logs_no_update BEFORE UPDATE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE not allowed'); END;
CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete BEFORE DELETE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE not allowed'); END;

CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_store ON audit_logs(store_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);

CREATE INDEX IF NOT EXISTS idx_stock_product_id ON stock(product_id);
CREATE INDEX IF NOT EXISTS idx_stock_location ON stock(location_type, location_id);
CREATE INDEX IF NOT EXISTS idx_sales_store_id ON sales(store_id);
CREATE INDEX IF NOT EXISTS idx_sales_created_at ON sales(created_at);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale_id ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_items_product_id ON sale_items(product_id);
CREATE INDEX IF NOT EXISTS idx_payments_sale_id ON payments(sale_id);
CREATE INDEX IF NOT EXISTS idx_payments_method ON payments(method);
CREATE INDEX IF NOT EXISTS idx_stock_movements_reference ON stock_movements(reference_id);
CREATE INDEX IF NOT EXISTS idx_stock_movements_product ON stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_stock_movements_location ON stock_movements(location_type, location_id);
CREATE INDEX IF NOT EXISTS idx_stock_movements_date ON stock_movements(created_at);
CREATE INDEX IF NOT EXISTS idx_transfers_product ON transfers(product_id);
CREATE INDEX IF NOT EXISTS idx_transfers_date ON transfers(created_at);
CREATE INDEX IF NOT EXISTS idx_closures_store_date ON cash_closures(store_id, closure_date);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_user_stores ON user_stores(user_id, store_id);
CREATE INDEX IF NOT EXISTS idx_user_warehouses ON user_warehouses(user_id, warehouse_id);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # Without this, a second connection hitting a locked writer (e.g. a
        # concurrent BEGIN IMMEDIATE stock deduction, see Feature 2) fails
        # immediately with "database is locked" instead of queuing. This is
        # what makes SQLite's single-writer model behave like row locking for
        # our purposes: the second transaction blocks and retries internally
        # until the timeout instead of racing past a half-checked stock read.
        g.db.execute("PRAGMA busy_timeout = 5000")
        if app.config.get("TESTING"):
            # Test runs recreate the whole DB file per test and commit
            # repeatedly; fsync-per-commit makes that painfully slow on some
            # filesystems. synchronous=OFF only risks losing data on an OS
            # crash, which is irrelevant for an ephemeral test database.
            g.db.execute("PRAGMA synchronous = OFF")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection:
        connection.close()


def hash_password(value):
    """Hash password using Werkzeug's secure hashing (pbkdf2:sha256)"""
    return generate_password_hash(value, method="pbkdf2:sha256")


def validate_password(stored_hash, password):
    """Validate password against stored hash"""
    return check_password_hash(stored_hash, password)


def validate_input(value, field_name, min_length=1, max_length=255, required=True):
    """Validate string input: length, required status"""
    if required and not value:
        return False, f"{field_name} is required"
    if value and (len(str(value)) < min_length or len(str(value)) > max_length):
        return False, f"{field_name} must be between {min_length} and {max_length} characters"
    return True, None


def validate_price(value):
    """Validate price is non-negative and within range"""
    try:
        price = float(value)
        if price < 0:
            return False, "Price cannot be negative"
        if price > 1_000_000:
            return False, "Price exceeds maximum allowed"
        return True, None
    except (ValueError, TypeError):
        return False, "Invalid price format"


def validate_quantity(value):
    """Validate quantity is positive integer"""
    try:
        qty = int(value)
        if qty <= 0:
            return False, "Quantity must be positive"
        if qty > 1_000_000:
            return False, "Quantity exceeds maximum"
        return True, None
    except (ValueError, TypeError):
        return False, "Invalid quantity format"


def migrate_legacy_roles(connection):
    """One-time migration: the users.role CHECK constraint used to allow the
    old 3-role model ('admin_almacen','vendedor'). SQLite can't ALTER a CHECK
    constraint in place, so if any row still has a legacy role value we rebuild
    the table with the new constraint and remap the data. Idempotent — a no-op
    once every row already uses a current role name."""
    legacy_roles = tuple(constants.ROLE_MIGRATION_MAP.keys())
    placeholders = ",".join("?" * len(legacy_roles))
    has_legacy = connection.execute(
        f"SELECT 1 FROM users WHERE role IN ({placeholders}) LIMIT 1", legacy_roles
    ).fetchone()
    if not has_legacy:
        return
    logger.info("Migrating legacy user roles to the expanded 5-role model")
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("ALTER TABLE users RENAME TO users_legacy")
        connection.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, "
            "role TEXT NOT NULL CHECK(role IN ('admin','store_manager','cashier','warehouse_operator','accountant')), "
            "active INTEGER NOT NULL DEFAULT 1)"
        )
        rows = connection.execute("SELECT id, username, password, role, active FROM users_legacy").fetchall()
        for row in rows:
            new_role = constants.ROLE_MIGRATION_MAP.get(row[3], row[3])
            connection.execute(
                "INSERT INTO users(id, username, password, role, active) VALUES (?,?,?,?,?)",
                (row[0], row[1], row[2], new_role, row[4]),
            )
        connection.execute("DROP TABLE users_legacy")
        connection.commit()
        logger.info(f"Migrated {len(rows)} user row(s) to new role names")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _table_has_column(connection, table, column):
    return column in [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _migrate_products_to_cents(connection):
    if _table_has_column(connection, "products", "cost_price_cents"):
        return
    logger.info("Migrating products.price (REAL) to cost_price_cents/sale_price_cents (INTEGER)")
    connection.execute("ALTER TABLE products RENAME TO products_legacy")
    connection.execute(
        "CREATE TABLE products (id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, "
        "cost_price_cents INTEGER NOT NULL DEFAULT 0, sale_price_cents INTEGER NOT NULL DEFAULT 0, "
        "active INTEGER NOT NULL DEFAULT 1)"
    )
    rows = connection.execute("SELECT id, sku, name, price, active FROM products_legacy").fetchall()
    for row in rows:
        price_cents = parse_to_cents(row[3])
        # No historical cost_price existed before this migration; default it to
        # sale_price so nothing divides by zero, but this reports 0 margin until
        # an admin corrects real cost via /catalog — flagged in the Phase 2 summary.
        connection.execute(
            "INSERT INTO products(id, sku, name, cost_price_cents, sale_price_cents, active) VALUES (?,?,?,?,?,?)",
            (row[0], row[1], row[2], price_cents, price_cents, row[4]),
        )
    connection.execute("DROP TABLE products_legacy")
    logger.info(f"Migrated {len(rows)} product row(s) to cents-based pricing")


def _migrate_stock_movements_types(connection):
    if _table_has_column(connection, "stock_movements", "unit_value_cents"):
        return
    logger.info("Migrating stock_movements to the expanded movement_type enum")
    connection.execute("ALTER TABLE stock_movements RENAME TO stock_movements_legacy")
    connection.execute(
        "CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, "
        "location_type TEXT NOT NULL CHECK(location_type IN ('store','warehouse')), location_id INTEGER NOT NULL, "
        "movement_type TEXT NOT NULL CHECK(movement_type IN ('ENTRY','EXIT','SALE','TRANSFER_IN','TRANSFER_OUT','BREAKAGE')), "
        "quantity INTEGER NOT NULL, unit_value_cents INTEGER, reference_id INTEGER, reason TEXT DEFAULT '', "
        "user_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "FOREIGN KEY(product_id) REFERENCES products(id), FOREIGN KEY(user_id) REFERENCES users(id))"
    )
    rows = connection.execute(
        "SELECT id, product_id, location_type, location_id, movement_type, quantity, reason, user_id, created_at "
        "FROM stock_movements_legacy"
    ).fetchall()
    for row in rows:
        connection.execute(
            "INSERT INTO stock_movements(id, product_id, location_type, location_id, movement_type, quantity, "
            "unit_value_cents, reference_id, reason, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row[0], row[1], row[2], row[3], row[4].upper(), row[5], None, None, row[6], row[7], row[8]),
        )
    connection.execute("DROP TABLE stock_movements_legacy")
    logger.info(f"Migrated {len(rows)} stock_movement row(s) to the new movement_type enum")


def _migrate_sales_to_items_and_payments(connection):
    if not _table_has_column(connection, "sales", "total"):
        return
    logger.info("Migrating sales/sale_items to cents + separate payments table")
    connection.execute("ALTER TABLE sales RENAME TO sales_legacy")
    connection.execute(
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
        "status TEXT NOT NULL CHECK(status IN ('COMPLETED','VOIDED')) DEFAULT 'COMPLETED', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(store_id) REFERENCES stores(id), "
        "FOREIGN KEY(user_id) REFERENCES users(id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL, "
        "method TEXT NOT NULL CHECK(method IN ('CASH','TRANSFER','CARD')), amount_cents INTEGER NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE)"
    )
    # Legacy free-form Spanish payment_method -> canonical enum. "Otro" has no
    # clean equivalent; mapped to TRANSFER as the least-wrong bucket for
    # historical rows (flagged in the Phase 2 summary — there were 0 such rows
    # in the live pos.db at migration time).
    legacy_method_map = {"Efectivo": constants.PAYMENT_CASH, "Tarjeta": constants.PAYMENT_CARD, "Otro": constants.PAYMENT_TRANSFER}
    old_sales = connection.execute("SELECT id, store_id, total, payment_method, user_id, created_at FROM sales_legacy").fetchall()
    for row in old_sales:
        connection.execute(
            "INSERT INTO sales(id, store_id, user_id, status, created_at) VALUES (?,?,?,?,?)",
            (row[0], row[1], row[4], "COMPLETED", row[5]),
        )
        connection.execute(
            "INSERT INTO payments(sale_id, method, amount_cents, created_at) VALUES (?,?,?,?)",
            (row[0], legacy_method_map.get(row[3], constants.PAYMENT_TRANSFER), parse_to_cents(row[2]), row[5]),
        )
    connection.execute("DROP TABLE sales_legacy")

    if _table_has_column(connection, "sale_items", "unit_price"):
        connection.execute("ALTER TABLE sale_items RENAME TO sale_items_legacy")
        connection.execute(
            "CREATE TABLE sale_items (id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL, product_id INTEGER NOT NULL, "
            "quantity INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL, subtotal_cents INTEGER NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE, "
            "FOREIGN KEY(product_id) REFERENCES products(id))"
        )
        old_items = connection.execute(
            "SELECT id, sale_id, product_id, quantity, unit_price, line_total, created_at FROM sale_items_legacy"
        ).fetchall()
        for row in old_items:
            connection.execute(
                "INSERT INTO sale_items(id, sale_id, product_id, quantity, unit_price_cents, subtotal_cents, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (row[0], row[1], row[2], row[3], parse_to_cents(row[4]), parse_to_cents(row[5]), row[6]),
            )
        connection.execute("DROP TABLE sale_items_legacy")
    logger.info(f"Migrated {len(old_sales)} sale row(s) to cents + payments table")


def migrate_money_schema(connection):
    """One-time migration to integer-cents money (Phase 2 non-functional
    requirement). Each sub-step is independently idempotent, so this is a
    no-op once every table already matches the current schema."""
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _migrate_products_to_cents(connection)
        _migrate_stock_movements_types(connection)
        _migrate_sales_to_items_and_payments(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def migrate_add_columns(connection):
    """Phase 4: stores.timezone and sales.cash_session_id are pure additions
    (nullable/defaulted, no CHECK constraint involved), so a plain ALTER TABLE
    ADD COLUMN is sufficient here — no table rebuild needed like the CHECK
    constraint changes elsewhere in this file. sales.cash_session_id has no FK
    constraint declared (SQLite can't add one via ALTER ADD COLUMN without a
    rebuild); it's an application-level reference only."""
    if not _table_has_column(connection, "stores", "timezone"):
        logger.info("Adding stores.timezone column")
        connection.execute(f"ALTER TABLE stores ADD COLUMN timezone TEXT NOT NULL DEFAULT '{constants.DEFAULT_STORE_TIMEZONE}'")
        connection.commit()
    if not _table_has_column(connection, "sales", "cash_session_id"):
        logger.info("Adding sales.cash_session_id column")
        connection.execute("ALTER TABLE sales ADD COLUMN cash_session_id INTEGER")
        connection.commit()


def init_db():
    connection = sqlite3.connect(app.config["DATABASE"])
    if app.config.get("TESTING"):
        connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(SCHEMA)
    migrate_legacy_roles(connection)
    migrate_money_schema(connection)
    migrate_add_columns(connection)
    if not connection.execute("SELECT id FROM users LIMIT 1").fetchone():
        connection.execute("INSERT INTO users(username,password,role) VALUES (?,?,?)", ("admin", hash_password("admin123"), "admin"))
        connection.execute("INSERT INTO stores(name,address) VALUES (?,?)", ("Tienda principal", "Pendiente de configurar"))
        connection.execute("INSERT INTO warehouses(name,address) VALUES (?,?)", ("Almacen central", "Pendiente de configurar"))
        connection.execute(
            "INSERT INTO products(sku,name,cost_price_cents,sale_price_cents) VALUES (?,?,?,?)",
            ("DEMO-001", "Producto demo", 700, 1000),
        )
    connection.commit()
    connection.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def permission_required(*permissions):
    """RBAC guard enforced server-side against constants.ROLE_PERMISSIONS —
    never rely on templates hiding a nav link as the only access control.
    Grants access if the user's role holds ANY of the listed permissions."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user:
                return redirect(url_for("login"))
            if not any(constants.has_permission(g.user["role"], perm) for perm in permissions):
                flash("No tienes permisos para esta operación.", "error")
                logger.warning(f"Permission denied: user {g.user['id']} ({g.user['role']}) lacks {permissions}")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def assigned(location_type, location_id):
    if g.user["role"] == "admin":
        return True
    table = "user_stores" if location_type == "store" else "user_warehouses"
    column = "store_id" if location_type == "store" else "warehouse_id"
    return db().execute(f"SELECT 1 FROM {table} WHERE user_id=? AND {column}=?", (g.user["id"], location_id)).fetchone() is not None


def parse_location_value(value):
    """Parse a combined '<type>:<id>' <select> value (transfer.html /
    inventory.html) into (location_type, location_id_int).

    QA finding: this used to be two independent form fields — a "tipo" select
    and a location-name select — that weren't linked to each other. A user
    could pick a location by name while the paired type select silently stayed
    on its default, submitting a (type, id) pair that didn't describe the
    location they'd actually picked. Store ids and warehouse ids are separate
    sequences that collide constantly (both routinely have id=1, id=2, ...),
    so the mismatch was easy to trigger by accident and, worse, mostly silent —
    it only surfaced as an error in the one case where the mismatched pair
    happened to hit a nonexistent row and trip the audit_logs FK constraint;
    every other mismatch just moved stock to/from the wrong location with no
    error at all. One combined select removes the possibility of mismatch by
    construction; location_exists() below is the actual validation, not the
    accidental FK side effect."""
    location_type, _, location_id_str = value.partition(":")
    if location_type not in constants.VALID_LOCATION_TYPES or not location_id_str:
        raise ValueError(f"Invalid location value: {value!r}")
    return location_type, int(location_id_str)


def location_exists(connection, location_type, location_id):
    table = "stores" if location_type == "store" else "warehouses"
    return connection.execute(f"SELECT 1 FROM {table} WHERE id=? AND active=1", (location_id,)).fetchone() is not None


@app.before_request
def load_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = db().execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()


@app.context_processor
def globals_for_templates():
    return {"current_user": g.user, "has_permission": constants.has_permission, "constants": constants}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        # Validate input
        valid, error = validate_input(username, "Username", min_length=1, max_length=100)
        if not valid:
            flash(error, "error")
            return render_template("login.html")
        
        if not password:
            flash("Password is required.", "error")
            return render_template("login.html")
        
        user = db().execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        if user and validate_password(user["password"], password):
            session["user_id"] = user["id"]
            logger.info(f"User {username} logged in successfully")
            return redirect(url_for("dashboard"))
        
        logger.warning(f"Failed login attempt for username: {username}")
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    connection = db()
    stats = {
        "stores": connection.execute("SELECT COUNT(*) FROM stores WHERE active=1").fetchone()[0],
        "warehouses": connection.execute("SELECT COUNT(*) FROM warehouses WHERE active=1").fetchone()[0],
        "products": connection.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0],
        "today_sales": cents_to_decimal(get_today_sales_total_cents(connection)),
    }
    recent = get_recent_sales_with_totals(connection, 8)
    return render_template("dashboard.html", stats=stats, recent=recent)


@app.route("/catalog", methods=["GET", "POST"])
@permission_required(constants.PERM_CATALOG_MANAGE)
def catalog():
    if request.method == "POST":
        kind = request.form.get("kind", "").strip()
        
        # SECURITY: Whitelist kind to prevent SQL injection
        if kind not in ("store", "warehouse", "product"):
            flash("Invalid item type.", "error")
            logger.warning(f"Attempted invalid catalog kind: {kind}")
            return redirect(url_for("catalog"))
        
        try:
            if kind == "product":
                sku = request.form.get("sku", "").strip()
                name = request.form.get("name", "").strip()
                cost_price = request.form.get("cost_price", "")
                sale_price = request.form.get("sale_price", "")

                # Validate inputs
                valid, error = validate_input(sku, "SKU", min_length=1, max_length=50)
                if not valid:
                    flash(error, "error")
                    return redirect(url_for("catalog"))

                valid, error = validate_input(name, "Product name", min_length=1, max_length=255)
                if not valid:
                    flash(error, "error")
                    return redirect(url_for("catalog"))

                try:
                    cost_price_cents = parse_to_cents(cost_price)
                    sale_price_cents = parse_to_cents(sale_price)
                except InvalidMoneyError as e:
                    flash(f"Precio: {e}", "error")
                    return redirect(url_for("catalog"))
                if cost_price_cents > 100_000_000 or sale_price_cents > 100_000_000:
                    flash("Price exceeds maximum allowed", "error")
                    return redirect(url_for("catalog"))

                cursor = db().execute(
                    "INSERT INTO products(sku,name,cost_price_cents,sale_price_cents) VALUES (?,?,?,?)",
                    (sku, name, cost_price_cents, sale_price_cents),
                )
                entity_id = cursor.lastrowid
                after_state = {"sku": sku, "name": name, "cost_price_cents": cost_price_cents,
                               "sale_price_cents": sale_price_cents}
            else:
                name = request.form.get("name", "").strip()
                address = request.form.get("address", "").strip()
                
                # Validate inputs
                valid, error = validate_input(name, "Name", min_length=1, max_length=255)
                if not valid:
                    flash(error, "error")
                    return redirect(url_for("catalog"))
                
                valid, error = validate_input(address, "Address", min_length=0, max_length=500, required=False)
                if not valid:
                    flash(error, "error")
                    return redirect(url_for("catalog"))
                
                # Use parameterized approach instead of f-string
                table = "stores" if kind == "store" else "warehouses"
                if table == "stores":
                    timezone_name = request.form.get("timezone", "").strip() or constants.DEFAULT_STORE_TIMEZONE
                    if not business_day.is_valid_timezone(timezone_name):
                        flash(f"Invalid timezone: {timezone_name}", "error")
                        return redirect(url_for("catalog"))
                    cursor = db().execute(
                        "INSERT INTO stores(name,address,timezone) VALUES (?,?,?)", (name, address, timezone_name)
                    )
                    after_state = {"name": name, "address": address, "timezone": timezone_name}
                else:
                    cursor = db().execute("INSERT INTO warehouses(name,address) VALUES (?,?)", (name, address))
                    after_state = {"name": name, "address": address}
                entity_id = cursor.lastrowid

            write_audit_log(db(), user=g.user, action=constants.AUDIT_ACTION_CREATE, entity_type=kind,
                             entity_id=entity_id, after=after_state, ip_address=request.remote_addr)
            db().commit()
            flash("Registro creado.", "success")
            logger.info(f"Created new {kind}: {request.form.get('name', 'N/A')}")
        except sqlite3.IntegrityError as e:
            flash("Duplicate entry or invalid data.", "error")
            logger.error(f"Integrity error in catalog: {e}")
        except Exception as e:
            flash("Error creating record.", "error")
            logger.error(f"Error in catalog: {e}")
    
    return render_template("catalog.html", 
                         stores=db().execute("SELECT * FROM stores").fetchall(), 
                         warehouses=db().execute("SELECT * FROM warehouses").fetchall(), 
                         products=db().execute("SELECT * FROM products").fetchall())


@app.route("/users", methods=["GET", "POST"])
@permission_required(constants.PERM_USERS_MANAGE)
def users():
    connection = db()
    if request.method == "POST":
        try:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "").strip()
            
            # Validate inputs
            valid, error = validate_input(username, "Username", min_length=3, max_length=100)
            if not valid:
                flash(error, "error")
                return redirect(url_for("users"))
            
            if not password or len(password) < 6:
                flash("Password must be at least 6 characters.", "error")
                return redirect(url_for("users"))
            
            if role not in constants.VALID_ROLES:
                flash("Invalid role.", "error")
                logger.warning(f"Attempted to create user with invalid role: {role}")
                return redirect(url_for("users"))
            
            cursor = connection.execute("INSERT INTO users(username,password,role) VALUES (?,?,?)", 
                                       (username, hash_password(password), role))
            user_id = cursor.lastrowid
            
            # Assign stores
            for store_id in request.form.getlist("store_ids"):
                try:
                    store_id = int(store_id)
                    connection.execute("INSERT INTO user_stores(user_id,store_id) VALUES (?,?)", 
                                     (user_id, store_id))
                except ValueError:
                    continue
            
            # Assign warehouses
            for warehouse_id in request.form.getlist("warehouse_ids"):
                try:
                    warehouse_id = int(warehouse_id)
                    connection.execute("INSERT INTO user_warehouses(user_id,warehouse_id) VALUES (?,?)", 
                                     (user_id, warehouse_id))
                except ValueError:
                    continue

            write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_CREATE, entity_type="user",
                             entity_id=user_id, after={"username": username, "role": role,
                             "store_ids": request.form.getlist("store_ids"),
                             "warehouse_ids": request.form.getlist("warehouse_ids")},
                             ip_address=request.remote_addr)
            connection.commit()
            logger.info(f"User created: {username} with role {role}")
            flash("Usuario creado y asignaciones guardadas.", "success")
        except sqlite3.IntegrityError:
            flash("El usuario ya existe o la asignación es inválida.", "error")
            logger.warning(f"Duplicate username attempt: {username}")
        except Exception as e:
            flash("Error creating user.", "error")
            logger.error(f"Error in users endpoint: {e}")
    
    user_rows = connection.execute("SELECT id,username,role,active FROM users ORDER BY username").fetchall()
    return render_template("users.html", 
                         users=user_rows, 
                         stores=connection.execute("SELECT * FROM stores WHERE active=1").fetchall(), 
                         warehouses=connection.execute("SELECT * FROM warehouses WHERE active=1").fetchall())


@app.route("/transfer", methods=["GET", "POST"])
@permission_required(constants.PERM_TRANSFER_MANAGE)
def transfer():
    connection = db()
    if request.method == "POST":
        try:
            # Validate and parse inputs
            product_id = int(request.form.get("product_id", 0))
            quantity_str = request.form.get("quantity", "")
            try:
                source_type, source_id = parse_location_value(request.form.get("source", ""))
                target_type, target_id = parse_location_value(request.form.get("target", ""))
            except ValueError:
                flash("Invalid source/target location.", "error")
                return redirect(url_for("transfer"))

            # Validate quantity
            valid, error = validate_quantity(quantity_str)
            if not valid:
                flash(error, "error")
                return redirect(url_for("transfer"))

            quantity = int(quantity_str)

            # QA fix: the location select no longer lets a mismatched (type, id)
            # pair be constructed at all (see parse_location_value), but a
            # non-browser client could still POST a bogus id directly, and
            # admin's assigned() bypass below wouldn't catch it — so verify
            # both locations are real rows, not just well-formed values.
            if not location_exists(connection, source_type, source_id):
                flash("El origen seleccionado no existe.", "error")
                return redirect(url_for("transfer"))
            if not location_exists(connection, target_type, target_id):
                flash("El destino seleccionado no existe.", "error")
                return redirect(url_for("transfer"))

            # SECURITY FIX: Prevent self-transfer
            if source_type == target_type and source_id == target_id:
                flash("Source and target locations cannot be the same.", "error")
                return redirect(url_for("transfer"))

            # Check permissions
            if not assigned(source_type, source_id) or not assigned(target_type, target_id):
                flash("No tienes asignado el origen o destino seleccionado.", "error")
                logger.warning(f"Unauthorized transfer attempt by user {g.user['id']}")
                return redirect(url_for("transfer"))

            # Check product exists
            product = connection.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
            if not product:
                flash("Invalid product.", "error")
                return redirect(url_for("transfer"))

            # Same locking pattern as Feature 2's sale flow: BEGIN IMMEDIATE
            # takes the write lock before the stock check, so the check and the
            # deduction below can't race against a concurrent transfer/sale on
            # the same product+location.
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT quantity FROM stock WHERE product_id=? AND location_type=? AND location_id=?",
                    (product_id, source_type, source_id)
                ).fetchone()
                current_quantity = current["quantity"] if current else 0
                if current_quantity < quantity:
                    connection.rollback()
                    flash(f"Stock insuficiente. Disponible: {current_quantity}", "error")
                    return redirect(url_for("transfer"))

                # Deduct from source
                connection.execute(
                    "INSERT INTO stock(product_id,location_type,location_id,quantity) VALUES (?,?,?,?) ON CONFLICT(product_id,location_type,location_id) DO UPDATE SET quantity=quantity+excluded.quantity",
                    (product_id, source_type, source_id, -quantity)
                )

                # Add to target
                connection.execute(
                    "INSERT INTO stock(product_id,location_type,location_id,quantity) VALUES (?,?,?,?) ON CONFLICT(product_id,location_type,location_id) DO UPDATE SET quantity=quantity+excluded.quantity",
                    (product_id, target_type, target_id, quantity)
                )

                # Record transfer
                cursor = connection.execute(
                    "INSERT INTO transfers(product_id,source_type,source_id,target_type,target_id,quantity,user_id) VALUES (?,?,?,?,?,?,?)",
                    (product_id, source_type, source_id, target_type, target_id, quantity, g.user["id"])
                )
                transfer_id = cursor.lastrowid

                # InventoryMovement pair for the transfer (target model's
                # TRANSFER_OUT/TRANSFER_IN), referencing the transfers row.
                connection.execute(
                    "INSERT INTO stock_movements(product_id, location_type, location_id, movement_type, quantity, "
                    "unit_value_cents, reference_id, reason, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (product_id, source_type, source_id, constants.MOVEMENT_TYPE_TRANSFER_OUT, quantity,
                     product["cost_price_cents"], transfer_id, f"Transfer #{transfer_id}", g.user["id"]),
                )
                connection.execute(
                    "INSERT INTO stock_movements(product_id, location_type, location_id, movement_type, quantity, "
                    "unit_value_cents, reference_id, reason, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (product_id, target_type, target_id, constants.MOVEMENT_TYPE_TRANSFER_IN, quantity,
                     product["cost_price_cents"], transfer_id, f"Transfer #{transfer_id}", g.user["id"]),
                )

                write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_CREATE, entity_type="transfer",
                                 entity_id=transfer_id,
                                 after={"product_id": product_id, "source_type": source_type, "source_id": source_id,
                                        "target_type": target_type, "target_id": target_id, "quantity": quantity},
                                 store_id=source_id if source_type == "store" else (target_id if target_type == "store" else None),
                                 ip_address=request.remote_addr)
                connection.commit()
                logger.info(f"Transfer: {quantity} units of product {product_id} from {source_type} {source_id} to {target_type} {target_id} by user {g.user['id']}")
                flash("Traspaso realizado.", "success")
            except Exception as e:
                connection.rollback()
                flash("Error executing transfer. Transaction rolled back.", "error")
                logger.error(f"Transfer error: {e}")
        
        except (ValueError, TypeError):
            flash("Invalid input values.", "error")
            logger.warning(f"Invalid transfer input from user {g.user['id']}")
    
    return render_template("transfer.html", 
                         products=connection.execute("SELECT * FROM products WHERE active=1").fetchall(), 
                         stores=connection.execute("SELECT * FROM stores WHERE active=1").fetchall(), 
                         warehouses=connection.execute("SELECT * FROM warehouses WHERE active=1").fetchall())


@app.route("/inventory", methods=["GET", "POST"])
@permission_required(constants.PERM_INVENTORY_MANAGE)
def inventory():
    connection = db()
    if request.method == "POST":
        try:
            # Validate and parse inputs
            product_id = int(request.form.get("product_id", 0))
            try:
                location_type, location_id = parse_location_value(request.form.get("location", ""))
            except ValueError:
                flash("Invalid location.", "error")
                return redirect(url_for("inventory"))
            movement_type = request.form.get("movement_type", "").strip()
            quantity_str = request.form.get("quantity", "")
            reason = request.form.get("reason", "").strip()

            # Validate quantity
            valid, error = validate_quantity(quantity_str)
            if not valid:
                flash(error, "error")
                return redirect(url_for("inventory"))

            quantity = int(quantity_str)

            if movement_type not in constants.VALID_MANUAL_MOVEMENT_TYPES:
                flash("Invalid movement type.", "error")
                return redirect(url_for("inventory"))

            # Validate reason length
            valid, error = validate_input(reason, "Reason", min_length=0, max_length=255, required=False)
            if not valid:
                flash(error, "error")
                return redirect(url_for("inventory"))

            # QA fix: same root cause as /transfer — verify the location is a
            # real row, not just a well-formed (type, id) pair (see
            # parse_location_value's docstring for the full story).
            if not location_exists(connection, location_type, location_id):
                flash("La ubicación seleccionada no existe.", "error")
                return redirect(url_for("inventory"))

            # Check permissions
            if not assigned(location_type, location_id):
                flash("Ubicación no asignada.", "error")
                logger.warning(f"Unauthorized inventory access by user {g.user['id']}")
                return redirect(url_for("inventory"))

            # Check product exists
            product = connection.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
            if not product:
                flash("Invalid product.", "error")
                return redirect(url_for("inventory"))

            # Same locking pattern as Feature 2: BEGIN IMMEDIATE before reading
            # current stock, so the read-then-write below can't race a
            # concurrent sale/transfer/movement on the same product+location.
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT quantity FROM stock WHERE product_id=? AND location_type=? AND location_id=?",
                    (product_id, location_type, location_id)
                ).fetchone()
                current_quantity = current["quantity"] if current else 0
                new_quantity = (current_quantity + quantity if movement_type == constants.MOVEMENT_TYPE_ENTRY
                                 else current_quantity - quantity)
                if new_quantity < 0:
                    connection.rollback()
                    flash(f"La salida supera el stock disponible. Actual: {current_quantity}", "error")
                    return redirect(url_for("inventory"))

                # Update or insert stock
                connection.execute(
                    "INSERT INTO stock(product_id,location_type,location_id,quantity) VALUES (?,?,?,?) ON CONFLICT(product_id,location_type,location_id) DO UPDATE SET quantity=excluded.quantity",
                    (product_id, location_type, location_id, new_quantity)
                )

                # Record movement
                cursor = connection.execute(
                    "INSERT INTO stock_movements(product_id,location_type,location_id,movement_type,quantity,"
                    "unit_value_cents,reason,user_id) VALUES (?,?,?,?,?,?,?,?)",
                    (product_id, location_type, location_id, movement_type, quantity,
                     product["cost_price_cents"], reason, g.user["id"])
                )

                write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_CREATE, entity_type="stock_movement",
                                 entity_id=cursor.lastrowid,
                                 before={"quantity": current_quantity}, after={"quantity": new_quantity, "movement_type": movement_type},
                                 store_id=location_id if location_type == "store" else None,
                                 ip_address=request.remote_addr)
                connection.commit()
                logger.info(f"Stock movement: {movement_type} of {quantity} units for product {product_id} at {location_type} {location_id}")
                flash("Movimiento de almacén guardado.", "success")
            except Exception as e:
                connection.rollback()
                flash("Error recording inventory movement. Transaction rolled back.", "error")
                logger.error(f"Inventory movement error: {e}")
        
        except (ValueError, TypeError):
            flash("Invalid input values.", "error")
            logger.warning(f"Invalid inventory input from user {g.user['id']}")
    
    stock_rows = connection.execute(
        """SELECT stock.quantity, products.sku, products.name, stock.location_type, stock.location_id, 
           COALESCE(stores.name, warehouses.name) location_name 
           FROM stock 
           JOIN products ON products.id=stock.product_id 
           LEFT JOIN stores ON stock.location_type='store' AND stores.id=stock.location_id 
           LEFT JOIN warehouses ON stock.location_type='warehouse' AND warehouses.id=stock.location_id 
           ORDER BY stock.location_type, location_name, products.name"""
    ).fetchall()
    
    movement_rows = connection.execute(
        """SELECT stock_movements.*, products.sku, products.name, 
           COALESCE(stores.name, warehouses.name) location_name 
           FROM stock_movements 
           JOIN products ON products.id=stock_movements.product_id 
           LEFT JOIN stores ON stock_movements.location_type='store' AND stores.id=stock_movements.location_id 
           LEFT JOIN warehouses ON stock_movements.location_type='warehouse' AND warehouses.id=stock_movements.location_id 
           ORDER BY stock_movements.id DESC LIMIT 20"""
    ).fetchall()
    
    return render_template("inventory.html", 
                         products=connection.execute("SELECT * FROM products WHERE active=1").fetchall(), 
                         stores=connection.execute("SELECT * FROM stores WHERE active=1").fetchall(), 
                         warehouses=connection.execute("SELECT * FROM warehouses WHERE active=1").fetchall(), 
                         stock_rows=stock_rows, 
                         movement_rows=movement_rows)


def _parse_sale_form(form):
    """Parse the static multi-row sale form into (items, payments, errors).
    Blank rows are silently skipped; a row with only one of its two fields
    filled in is an error. No JS in this app (see Phase 2 summary), so the
    form is a fixed number of rows rather than a dynamic cart."""
    errors = []
    items = []
    for i in range(1, constants.MAX_SALE_ITEM_ROWS + 1):
        product_id_str = form.get(f"item_product_id_{i}", "").strip()
        quantity_str = form.get(f"item_quantity_{i}", "").strip()
        if not product_id_str and not quantity_str:
            continue
        try:
            product_id = int(product_id_str)
        except (ValueError, TypeError):
            errors.append(f"Fila {i}: producto inválido.")
            continue
        valid, error = validate_quantity(quantity_str)
        if not valid:
            errors.append(f"Fila {i}: {error}")
            continue
        items.append({"product_id": product_id, "quantity": int(quantity_str)})

    payments = []
    for i in range(1, constants.MAX_SALE_PAYMENT_ROWS + 1):
        method = form.get(f"payment_method_{i}", "").strip()
        amount_str = form.get(f"payment_amount_{i}", "").strip()
        if not method and not amount_str:
            continue
        if method not in constants.VALID_PAYMENT_METHODS:
            errors.append(f"Pago {i}: método inválido.")
            continue
        try:
            amount_cents = parse_to_cents(amount_str)
        except InvalidMoneyError as e:
            errors.append(f"Pago {i}: {e}")
            continue
        if amount_cents <= 0:
            errors.append(f"Pago {i}: el monto debe ser mayor que 0.")
            continue
        payments.append({"method": method, "amount_cents": amount_cents})

    if not items:
        errors.append("Agrega al menos un producto a la venta.")
    if not payments:
        errors.append("Agrega al menos un pago.")
    return items, payments, errors


@app.route("/sales", methods=["GET", "POST"])
@permission_required(constants.PERM_SALES_CREATE)
def sales():
    connection = db()
    stores = connection.execute("SELECT * FROM stores WHERE active=1").fetchall()

    if request.method == "POST":
        try:
            store_id = int(request.form.get("store_id", 0))
        except (ValueError, TypeError):
            flash("Invalid store ID.", "error")
            return redirect(url_for("sales"))

        # QA fix (same class of bug as /transfer's location mismatch):
        # assigned() returns True unconditionally for admin regardless of
        # whether store_id is real, and sales.store_id has a FK — a bogus id
        # would 500 on an unhandled IntegrityError instead of failing cleanly.
        if not location_exists(connection, "store", store_id):
            flash("La tienda seleccionada no existe.", "error")
            return redirect(url_for("sales"))

        # FEATURE 2: a sale can only deduct stock from its own store — enforced
        # server-side (not just by which stores appear in the UI dropdown).
        if not assigned("store", store_id):
            flash("No tienes acceso a esa tienda.", "error")
            logger.warning(f"Unauthorized sale attempt for store {store_id} by user {g.user['id']}")
            return redirect(url_for("sales"))

        items, payments, errors = _parse_sale_form(request.form)
        if errors:
            for error in errors:
                flash(error, "error")
            return redirect(url_for("sales"))

        # FEATURE 2, step 1: open the transaction with BEGIN IMMEDIATE (not a
        # plain BEGIN) so SQLite grabs the write lock *before* the stock check
        # below runs, not at the first write. That's what makes the
        # check-then-act stock validation safe under concurrency: a second
        # terminal's BEGIN IMMEDIATE blocks (up to busy_timeout) until this
        # transaction commits or rolls back, instead of racing past a stock
        # read that's about to go stale.
        connection.execute("BEGIN IMMEDIATE")
        try:
            merged_items = {}
            for item in items:
                merged_items[item["product_id"]] = merged_items.get(item["product_id"], 0) + item["quantity"]

            products_by_id = {}
            insufficient = []
            for product_id, quantity in merged_items.items():
                product = connection.execute(
                    "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
                ).fetchone()
                if not product:
                    insufficient.append(f"Producto {product_id} no existe o está inactivo.")
                    continue
                products_by_id[product_id] = product
                stock_row = connection.execute(
                    "SELECT quantity FROM stock WHERE product_id=? AND location_type='store' AND location_id=?",
                    (product_id, store_id),
                ).fetchone()
                available = stock_row["quantity"] if stock_row else 0
                if available < quantity:
                    insufficient.append(f"{product['name']}: stock disponible {available}, solicitado {quantity}.")

            if insufficient:
                # FEATURE 2, step 3: abort without any partial deduction.
                connection.rollback()
                for message in insufficient:
                    flash(message, "error")
                return redirect(url_for("sales"))

            sale_total_cents = sum(
                products_by_id[pid]["sale_price_cents"] * qty for pid, qty in merged_items.items()
            )
            payments_total_cents = sum(p["amount_cents"] for p in payments)
            if payments_total_cents != sale_total_cents:
                connection.rollback()
                flash(
                    f"El total de pagos (${format_cents(payments_total_cents)}) no coincide con el total "
                    f"de la venta (${format_cents(sale_total_cents)}).", "error"
                )
                return redirect(url_for("sales"))

            # FEATURE 3: ring the sale up under the cashier's open register
            # session for this store, auto-opening one (opening_amount=0) if
            # they haven't explicitly declared a starting float via
            # /cash-sessions — see the cash_sessions schema note.
            session_row = get_open_cash_session(connection, g.user["id"], store_id)
            if session_row:
                cash_session_id = session_row["id"]
            else:
                session_cursor = connection.execute(
                    "INSERT INTO cash_sessions(store_id, cashier_id, opening_amount_cents) VALUES (?,?,0)",
                    (store_id, g.user["id"]),
                )
                cash_session_id = session_cursor.lastrowid

            # FEATURE 2, steps 4-6: sale + items + payments + one InventoryMovement
            # per line + stock decrement, all inside the same locked transaction.
            cursor = connection.execute(
                "INSERT INTO sales(store_id, user_id, cash_session_id, status) VALUES (?,?,?,'COMPLETED')",
                (store_id, g.user["id"], cash_session_id),
            )
            sale_id = cursor.lastrowid

            item_audit = []
            for product_id, quantity in merged_items.items():
                unit_price_cents = products_by_id[product_id]["sale_price_cents"]
                subtotal_cents = unit_price_cents * quantity
                connection.execute(
                    "INSERT INTO sale_items(sale_id, product_id, quantity, unit_price_cents, subtotal_cents) "
                    "VALUES (?,?,?,?,?)",
                    (sale_id, product_id, quantity, unit_price_cents, subtotal_cents),
                )
                connection.execute(
                    "UPDATE stock SET quantity = quantity - ? WHERE product_id=? AND location_type='store' AND location_id=?",
                    (quantity, product_id, store_id),
                )
                connection.execute(
                    "INSERT INTO stock_movements(product_id, location_type, location_id, movement_type, quantity, "
                    "unit_value_cents, reference_id, reason, user_id) VALUES (?,'store',?,?,?,?,?,?,?)",
                    (product_id, store_id, constants.MOVEMENT_TYPE_SALE, quantity, unit_price_cents, sale_id,
                     f"Sale #{sale_id}", g.user["id"]),
                )
                item_audit.append({"product_id": product_id, "quantity": quantity, "unit_price_cents": unit_price_cents})

            for payment in payments:
                connection.execute(
                    "INSERT INTO payments(sale_id, method, amount_cents) VALUES (?,?,?)",
                    (sale_id, payment["method"], payment["amount_cents"]),
                )

            write_audit_log(
                connection, user=g.user, action=constants.AUDIT_ACTION_CREATE, entity_type="sale",
                entity_id=sale_id,
                after={"store_id": store_id, "items": item_audit, "payments": payments,
                       "total_cents": sale_total_cents},
                store_id=store_id, ip_address=request.remote_addr,
            )
            connection.commit()
            logger.info(f"Sale {sale_id} created for store {store_id} by user {g.user['id']}: ${format_cents(sale_total_cents)}")
            flash("Venta registrada.", "success")
        except Exception as e:
            connection.rollback()
            flash("Error registering sale. Transaction rolled back.", "error")
            logger.error(f"Error in sales endpoint: {e}")

    sales_data = get_recent_sales_with_totals(connection, constants.MAX_RECENT_RECORDS)
    products = connection.execute("SELECT * FROM products WHERE active=1").fetchall()
    return render_template("sales.html", stores=stores, sales=sales_data, products=products,
                            item_rows=range(1, constants.MAX_SALE_ITEM_ROWS + 1),
                            payment_rows=range(1, constants.MAX_SALE_PAYMENT_ROWS + 1))


def _parse_denomination_form(form):
    """Parse denom_<value_cents> quantity inputs into [(value_cents, qty), ...]
    (zero/blank rows dropped) plus the summed total in cents."""
    counts = []
    total_cents = 0
    for value_cents in constants.CASH_DENOMINATIONS_CENTS:
        raw = form.get(f"denom_{value_cents}", "").strip()
        if not raw:
            continue
        try:
            quantity = int(raw)
        except (ValueError, TypeError):
            continue
        if quantity <= 0:
            continue
        counts.append((value_cents, quantity))
        total_cents += value_cents * quantity
    return counts, total_cents


# ============ CASH REGISTER SESSIONS (Feature 3) ============

@app.route("/cash-sessions", methods=["GET", "POST"])
@permission_required(constants.PERM_SALES_CREATE, constants.PERM_CLOSURES_MANAGE)
def cash_sessions():
    connection = db()
    if request.method == "POST":
        if not constants.has_permission(g.user["role"], constants.PERM_SALES_CREATE):
            flash("No tienes permisos para esta operación.", "error")
            return redirect(url_for("cash_sessions"))

        try:
            store_id = int(request.form.get("store_id", 0))
        except (ValueError, TypeError):
            flash("Invalid store ID.", "error")
            return redirect(url_for("cash_sessions"))
        try:
            opening_amount_cents = parse_to_cents(request.form.get("opening_amount", "0"))
        except InvalidMoneyError as e:
            flash(f"Monto inicial: {e}", "error")
            return redirect(url_for("cash_sessions"))

        # QA fix (same class of bug as /transfer's location mismatch):
        # cash_sessions.store_id has a FK — verify it's real before admin's
        # assigned() bypass lets a bogus id through to an unhandled 500.
        if not location_exists(connection, "store", store_id):
            flash("La tienda seleccionada no existe.", "error")
            return redirect(url_for("cash_sessions"))
        if not assigned("store", store_id):
            flash("No tienes acceso a esa tienda.", "error")
            return redirect(url_for("cash_sessions"))
        if get_open_cash_session_for_cashier(connection, g.user["id"]):
            flash("Ya tienes una caja abierta. Ciérrala antes de abrir otra.", "error")
            return redirect(url_for("cash_sessions"))

        cursor = connection.execute(
            "INSERT INTO cash_sessions(store_id, cashier_id, opening_amount_cents) VALUES (?,?,?)",
            (store_id, g.user["id"], opening_amount_cents),
        )
        session_id = cursor.lastrowid
        write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_CREATE, entity_type="cash_session",
                         entity_id=session_id, after={"store_id": store_id, "opening_amount_cents": opening_amount_cents},
                         store_id=store_id, ip_address=request.remote_addr)
        connection.commit()
        flash("Caja abierta.", "success")
        return redirect(url_for("cash_sessions"))

    store_ids = None if g.user["role"] == constants.ROLE_ADMIN else get_assigned_store_ids(connection, g.user)
    if store_ids is None:
        open_sessions = connection.execute(
            "SELECT cash_sessions.*, users.username cashier_username, stores.name store_name FROM cash_sessions "
            "JOIN users ON users.id=cash_sessions.cashier_id JOIN stores ON stores.id=cash_sessions.store_id "
            "WHERE cash_sessions.status='OPEN' ORDER BY cash_sessions.id DESC"
        ).fetchall()
    elif store_ids:
        placeholders = ",".join("?" * len(store_ids))
        open_sessions = connection.execute(
            f"SELECT cash_sessions.*, users.username cashier_username, stores.name store_name FROM cash_sessions "
            f"JOIN users ON users.id=cash_sessions.cashier_id JOIN stores ON stores.id=cash_sessions.store_id "
            f"WHERE cash_sessions.status='OPEN' AND cash_sessions.store_id IN ({placeholders}) "
            f"ORDER BY cash_sessions.id DESC",
            store_ids
        ).fetchall()
    else:
        open_sessions = []

    my_open_session = get_open_cash_session_for_cashier(connection, g.user["id"])
    can_close_any = constants.has_permission(g.user["role"], constants.PERM_CLOSURES_MANAGE)
    return render_template(
        "cash_sessions.html",
        stores=connection.execute("SELECT * FROM stores WHERE active=1").fetchall(),
        open_sessions=open_sessions, my_open_session=my_open_session, can_close_any=can_close_any,
        denominations=constants.CASH_DENOMINATIONS_CENTS,
    )


@app.post("/cash-sessions/<int:session_id>/close")
@permission_required(constants.PERM_SALES_CREATE, constants.PERM_CLOSURES_MANAGE)
def close_cash_session(session_id):
    connection = db()
    session_row = get_cash_session_by_id(connection, session_id)
    if not session_row:
        flash("Caja no encontrada.", "error")
        return redirect(url_for("cash_sessions"))
    if session_row["status"] != constants.CASH_SESSION_STATUS_OPEN:
        flash("Esta caja ya está cerrada.", "error")
        return redirect(url_for("cash_sessions"))
    is_owner = session_row["cashier_id"] == g.user["id"]
    can_close_any = constants.has_permission(g.user["role"], constants.PERM_CLOSURES_MANAGE)
    if not (is_owner or can_close_any):
        flash("No tienes permisos para esta operación.", "error")
        return redirect(url_for("cash_sessions"))

    counts, counted_total_cents = _parse_denomination_form(request.form)
    try:
        connection.execute(
            "UPDATE cash_sessions SET status='CLOSED', closed_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,)
        )
        insert_denomination_counts(connection, counts, cash_session_id=session_id)
        write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_UPDATE, entity_type="cash_session",
                         entity_id=session_id, before={"status": "OPEN"},
                         after={"status": "CLOSED", "counted_total_cents": counted_total_cents},
                         store_id=session_row["store_id"], ip_address=request.remote_addr)
        connection.commit()
        flash(f"Caja cerrada. Efectivo contado: ${format_cents(counted_total_cents)}.", "success")
    except Exception as e:
        connection.rollback()
        flash("Error al cerrar la caja.", "error")
        logger.error(f"Cash session close error: {e}")
    return redirect(url_for("cash_sessions"))


# ============ DAILY CLOSING (Feature 3 core) ============

def compute_daily_closing_preview(connection, store, business_date):
    """Compute (never persist) the full Feature 3 breakdown for one store/day.
    Returns a dict with every intermediate value in cents, plus whether a
    previous closing exists (opening_float must be manually entered+confirmed
    if not — see the docstring on the /daily-closing route)."""
    start_utc, end_utc = business_day.business_date_bounds_utc(business_date, store["timezone"])
    previous = get_previous_daily_closing(connection, store["id"], business_date)

    cash_sales_total_cents = get_payments_total_cents(connection, store["id"], start_utc, end_utc, [constants.PAYMENT_CASH])
    transfer_sales_total_cents = get_payments_total_cents(
        connection, store["id"], start_utc, end_utc, [constants.PAYMENT_TRANSFER, constants.PAYMENT_CARD]
    )
    breakage_total_cents = get_breakage_total_cents(connection, store["id"], start_utc, end_utc)
    sale_inventory_value_cents = get_sale_inventory_value_cents(connection, store["id"], start_utc, end_utc)
    inventory_withdrawn_value_cents = sale_inventory_value_cents + breakage_total_cents

    open_sessions = get_open_sessions_for_store(connection, store["id"])

    return {
        "business_date": str(business_date), "start_utc": start_utc, "end_utc": end_utc,
        "previous_closing": previous,
        "opening_float_cents": previous["cash_counted_total_cents"] if previous else None,
        "cash_sales_total_cents": cash_sales_total_cents,
        "transfer_sales_total_cents": transfer_sales_total_cents,
        "breakage_total_cents": breakage_total_cents,
        "inventory_withdrawn_value_cents": inventory_withdrawn_value_cents,
        "open_sessions": open_sessions,
    }


@app.route("/daily-closing", methods=["GET", "POST"])
@permission_required(constants.PERM_CLOSURES_MANAGE)
def daily_closing():
    """FEATURE 3: the real Daily Accounting Closing. GET previews the full
    breakdown (nothing persisted yet); POST finalizes it as one immutable row.

      expected_cash_in_drawer = opening_float + cash_sales_total
      cash_variance            = cash_counted_total - expected_cash_in_drawer
      recorded_revenue         = cash_sales_total + transfer_sales_total
      reconciliation_difference = recorded_revenue - inventory_withdrawn_value

    Both are expected to be exactly 0 (tolerance confirmed with the product
    owner as RECONCILIATION_VARIANCE_THRESHOLD_CENTS=0). A non-zero value does
    NOT block finalization, but requires a mandatory justification_note and
    sets status=FINALIZED_WITH_VARIANCE.
    """
    connection = db()
    store_ids = None if g.user["role"] == constants.ROLE_ADMIN else get_assigned_store_ids(connection, g.user)
    stores = (connection.execute("SELECT * FROM stores WHERE active=1").fetchall() if store_ids is None
              else connection.execute(
                  f"SELECT * FROM stores WHERE active=1 AND id IN ({','.join('?' * len(store_ids))})", store_ids
              ).fetchall() if store_ids else [])

    try:
        store_id = int(request.values.get("store_id") or (stores[0]["id"] if stores else 0))
    except (ValueError, TypeError):
        store_id = stores[0]["id"] if stores else 0
    business_date_str = request.values.get("business_date", "").strip()
    store = connection.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()

    if not store or (store_ids is not None and store["id"] not in store_ids):
        flash("No tienes acceso a esa tienda.", "error")
        return redirect(url_for("dashboard"))

    business_date = (date.fromisoformat(business_date_str) if business_date_str
                      else business_day.current_business_date(store["timezone"]))
    already_closed = get_daily_closing_for_date(connection, store_id, business_date)
    if request.method == "POST" and already_closed:
        # FEATURE 3 idempotency: a double-submit (double-click/retry) lands
        # here and is a no-op, not a second row — same guard as the
        # UNIQUE(store_id, business_date) constraint below, just short-circuited
        # before doing any work.
        flash("Ya existe un cierre finalizado para esta tienda y fecha.", "error")
        return redirect(url_for("daily_closing", store_id=store_id, business_date=business_date))

    if request.method == "POST" and not already_closed:
        preview = compute_daily_closing_preview(connection, store, business_date)
        counts, cash_counted_total_cents = _parse_denomination_form(request.form)

        if preview["open_sessions"]:
            flash(f"Cierra primero las {len(preview['open_sessions'])} caja(s) abiertas de esta tienda.", "error")
            return redirect(url_for("daily_closing", store_id=store_id, business_date=business_date))

        if preview["opening_float_cents"] is None:
            # No previous closing exists — never silently default to 0.
            try:
                opening_float_cents = parse_to_cents(request.form.get("opening_float_confirm", ""))
            except InvalidMoneyError:
                flash("No existe un cierre anterior para esta tienda: debes ingresar y confirmar el saldo inicial (opening_float).", "error")
                return redirect(url_for("daily_closing", store_id=store_id, business_date=business_date))
        else:
            opening_float_cents = preview["opening_float_cents"]

        justification_note = request.form.get("justification_note", "").strip()

        expected_cash_in_drawer_cents = opening_float_cents + preview["cash_sales_total_cents"]
        cash_variance_cents = cash_counted_total_cents - expected_cash_in_drawer_cents
        recorded_revenue_cents = preview["cash_sales_total_cents"] + preview["transfer_sales_total_cents"]
        reconciliation_difference_cents = recorded_revenue_cents - preview["inventory_withdrawn_value_cents"]

        has_variance = (abs(cash_variance_cents) > constants.RECONCILIATION_VARIANCE_THRESHOLD_CENTS or
                         abs(reconciliation_difference_cents) > constants.RECONCILIATION_VARIANCE_THRESHOLD_CENTS)
        if has_variance and not justification_note:
            flash("Hay una variación en el cierre (cash_variance o reconciliation_difference distinto de 0). "
                  "Se requiere una nota de justificación para finalizar.", "error")
            return redirect(url_for("daily_closing", store_id=store_id, business_date=business_date))

        status = constants.DAILY_CLOSING_STATUS_FINALIZED_WITH_VARIANCE if has_variance else constants.DAILY_CLOSING_STATUS_FINALIZED

        try:
            cursor = connection.execute(
                "INSERT INTO daily_closings(store_id, business_date, previous_closing_id, opening_float_cents, "
                "cash_sales_total_cents, transfer_sales_total_cents, breakage_total_cents, cash_counted_total_cents, "
                "cash_variance_cents, inventory_withdrawn_value_cents, reconciliation_difference_cents, status, "
                "justification_note, finalized_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (store_id, str(business_date), preview["previous_closing"]["id"] if preview["previous_closing"] else None,
                 opening_float_cents, preview["cash_sales_total_cents"], preview["transfer_sales_total_cents"],
                 preview["breakage_total_cents"], cash_counted_total_cents, cash_variance_cents,
                 preview["inventory_withdrawn_value_cents"], reconciliation_difference_cents, status,
                 justification_note or None, g.user["id"]),
            )
            closing_id = cursor.lastrowid
            insert_denomination_counts(connection, counts, daily_closing_id=closing_id)
            write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_FINALIZE, entity_type="daily_closing",
                             entity_id=closing_id,
                             after={"business_date": str(business_date), "status": status,
                                    "cash_variance_cents": cash_variance_cents,
                                    "reconciliation_difference_cents": reconciliation_difference_cents},
                             store_id=store_id, ip_address=request.remote_addr)
            connection.commit()
            logger.info(f"Daily closing {closing_id} finalized for store {store_id} on {business_date}: status={status}")
            if status == constants.DAILY_CLOSING_STATUS_FINALIZED_WITH_VARIANCE:
                flash(f"Cierre finalizado CON VARIACIÓN. cash_variance=${format_cents(cash_variance_cents)}, "
                      f"reconciliation_difference=${format_cents(reconciliation_difference_cents)}.", "warning")
            else:
                flash("Cierre finalizado sin variaciones.", "success")
            return redirect(url_for("daily_closing", store_id=store_id, business_date=business_date))
        except sqlite3.IntegrityError:
            # FEATURE 3: idempotency — the UNIQUE(store_id, business_date)
            # constraint rejects a double-submit (double-click/retry) outright.
            connection.rollback()
            flash("Ya existe un cierre finalizado para esta tienda y fecha.", "error")
            return redirect(url_for("daily_closing", store_id=store_id, business_date=business_date))

    preview = None if already_closed else compute_daily_closing_preview(connection, store, business_date)
    closing_denominations = get_denomination_counts(connection, daily_closing_id=already_closed["id"]) if already_closed else []
    recent_closings = get_recent_daily_closings(connection, store_ids=store_ids, limit=15)

    return render_template(
        "daily_closing.html",
        stores=stores, store=store, business_date=str(business_date),
        already_closed=already_closed, preview=preview,
        closing_denominations=closing_denominations,
        denominations=constants.CASH_DENOMINATIONS_CENTS,
        recent_closings=recent_closings,
        variance_threshold_cents=constants.RECONCILIATION_VARIANCE_THRESHOLD_CENTS,
    )


@app.get("/closures")
@login_required
def closures():
    """The old manual cash/card/other closure form is fully superseded by
    /daily-closing (Feature 3). Kept as a redirect for any old bookmarks."""
    return redirect(url_for("daily_closing"))


def _parse_breakage_report_form(form):
    errors = []
    try:
        store_id = int(form.get("store_id", 0))
    except (ValueError, TypeError):
        store_id = None
        errors.append("Invalid store.")
    try:
        product_id = int(form.get("product_id", 0))
    except (ValueError, TypeError):
        product_id = None
        errors.append("Invalid product.")
    quantity_str = form.get("quantity", "")
    valid, error = validate_quantity(quantity_str)
    if not valid:
        errors.append(error)
    reason = form.get("reason", "").strip()
    if reason not in constants.VALID_BREAKAGE_REASONS:
        errors.append("Invalid reason.")
    notes = form.get("notes", "").strip()
    valid, error = validate_input(notes, "Notes", min_length=0, max_length=500, required=False)
    if not valid:
        errors.append(error)
    return store_id, product_id, quantity_str, reason, notes, errors


@app.route("/breakages", methods=["GET", "POST"])
@permission_required(constants.PERM_BREAKAGE_REPORT, constants.PERM_BREAKAGE_APPROVE)
def breakages():
    """FEATURE 4: dedicated report -> approve/reject workflow. Reporting never
    touches stock; only approval does (see approve_breakage below)."""
    connection = db()
    if request.method == "POST":
        if not constants.has_permission(g.user["role"], constants.PERM_BREAKAGE_REPORT):
            flash("No tienes permisos para esta operación.", "error")
            return redirect(url_for("breakages"))

        store_id, product_id, quantity_str, reason, notes, errors = _parse_breakage_report_form(request.form)
        if errors:
            for error in errors:
                flash(error, "error")
            return redirect(url_for("breakages"))
        quantity = int(quantity_str)

        # QA fix (same class of bug as /transfer's location mismatch): verify
        # the store is real before the FK-backed INSERT below, rather than
        # letting a bogus id 500 on an unhandled IntegrityError.
        if not location_exists(connection, "store", store_id):
            flash("La tienda seleccionada no existe.", "error")
            return redirect(url_for("breakages"))

        if not assigned("store", store_id):
            flash("No tienes acceso a esa tienda.", "error")
            logger.warning(f"Unauthorized breakage report for store {store_id} by user {g.user['id']}")
            return redirect(url_for("breakages"))

        product = connection.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not product:
            flash("Invalid product.", "error")
            return redirect(url_for("breakages"))

        # Advisory only — stock may still move between report and approval,
        # so the authoritative check happens again under lock at approval time.
        stock_row = connection.execute(
            "SELECT quantity FROM stock WHERE product_id=? AND location_type='store' AND location_id=?",
            (product_id, store_id)
        ).fetchone()
        available = stock_row["quantity"] if stock_row else 0
        if quantity > available:
            flash(f"Aviso: la cantidad reportada ({quantity}) supera el stock actual ({available}). "
                  f"Se revalidará al momento de aprobar.", "warning")

        cursor = connection.execute(
            "INSERT INTO breakages(store_id, product_id, quantity, reason, notes, reported_by) VALUES (?,?,?,?,?,?)",
            (store_id, product_id, quantity, reason, notes, g.user["id"])
        )
        breakage_id = cursor.lastrowid
        write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_CREATE, entity_type="breakage",
                         entity_id=breakage_id,
                         after={"store_id": store_id, "product_id": product_id, "quantity": quantity,
                                "reason": reason, "notes": notes, "status": constants.BREAKAGE_STATUS_PENDING},
                         store_id=store_id, ip_address=request.remote_addr)
        connection.commit()
        logger.info(f"Breakage {breakage_id} reported by user {g.user['id']} for product {product_id} at store {store_id}")
        flash("Rotura reportada. Pendiente de aprobación.", "success")
        return redirect(url_for("breakages"))

    store_ids = None if g.user["role"] == constants.ROLE_ADMIN else get_assigned_store_ids(connection, g.user)
    breakages_list = get_breakages(connection, store_ids=store_ids, limit=50)
    return render_template(
        "breakages.html",
        stores=connection.execute("SELECT * FROM stores WHERE active=1").fetchall(),
        products=connection.execute("SELECT * FROM products WHERE active=1").fetchall(),
        breakages=breakages_list,
        can_approve=constants.has_permission(g.user["role"], constants.PERM_BREAKAGE_APPROVE),
    )


@app.post("/breakages/<int:breakage_id>/approve")
@permission_required(constants.PERM_BREAKAGE_APPROVE)
def approve_breakage(breakage_id):
    connection = db()
    # Same BEGIN IMMEDIATE locking pattern as Feature 2: stock must be
    # re-validated under lock at approval time, since it may have moved (sold,
    # transferred) since the breakage was reported.
    connection.execute("BEGIN IMMEDIATE")
    try:
        breakage = connection.execute("SELECT * FROM breakages WHERE id=?", (breakage_id,)).fetchone()
        if not breakage:
            connection.rollback()
            flash("Rotura no encontrada.", "error")
            return redirect(url_for("breakages"))
        if breakage["status"] != constants.BREAKAGE_STATUS_PENDING:
            connection.rollback()
            flash("Esta rotura ya fue procesada.", "error")
            return redirect(url_for("breakages"))

        product = connection.execute("SELECT * FROM products WHERE id=?", (breakage["product_id"],)).fetchone()
        stock_row = connection.execute(
            "SELECT quantity FROM stock WHERE product_id=? AND location_type='store' AND location_id=?",
            (breakage["product_id"], breakage["store_id"])
        ).fetchone()
        available = stock_row["quantity"] if stock_row else 0
        if available < breakage["quantity"]:
            connection.rollback()
            flash(f"No se puede aprobar: stock disponible {available}, cantidad reportada {breakage['quantity']}.", "error")
            return redirect(url_for("breakages"))

        # FEATURE 4: value uses the confirmed BREAKAGE_VALUATION_METHOD (sale_price).
        unit_value_cents = (product["sale_price_cents"] if constants.BREAKAGE_VALUATION_METHOD == "sale_price"
                             else product["cost_price_cents"])
        value_cents = unit_value_cents * breakage["quantity"]

        connection.execute(
            "UPDATE stock SET quantity = quantity - ? WHERE product_id=? AND location_type='store' AND location_id=?",
            (breakage["quantity"], breakage["product_id"], breakage["store_id"])
        )
        connection.execute(
            "INSERT INTO stock_movements(product_id, location_type, location_id, movement_type, quantity, "
            "unit_value_cents, reference_id, reason, user_id) VALUES (?,'store',?,?,?,?,?,?,?)",
            (breakage["product_id"], breakage["store_id"], constants.MOVEMENT_TYPE_BREAKAGE, breakage["quantity"],
             unit_value_cents, breakage_id, f"Breakage #{breakage_id}: {breakage['reason']}", g.user["id"])
        )
        connection.execute(
            "UPDATE breakages SET status=?, approved_by=?, valuation_method=?, value_cents=?, "
            "decided_at=CURRENT_TIMESTAMP WHERE id=?",
            (constants.BREAKAGE_STATUS_APPROVED, g.user["id"], constants.BREAKAGE_VALUATION_METHOD, value_cents, breakage_id)
        )
        write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_APPROVE, entity_type="breakage",
                         entity_id=breakage_id, before={"status": constants.BREAKAGE_STATUS_PENDING},
                         after={"status": constants.BREAKAGE_STATUS_APPROVED, "value_cents": value_cents,
                                "valuation_method": constants.BREAKAGE_VALUATION_METHOD},
                         store_id=breakage["store_id"], ip_address=request.remote_addr)
        connection.commit()
        logger.info(f"Breakage {breakage_id} approved by user {g.user['id']}: value ${format_cents(value_cents)}")
        flash("Rotura aprobada y stock ajustado.", "success")
    except Exception as e:
        connection.rollback()
        flash("Error al aprobar la rotura.", "error")
        logger.error(f"Breakage approval error: {e}")
    return redirect(url_for("breakages"))


@app.post("/breakages/<int:breakage_id>/reject")
@permission_required(constants.PERM_BREAKAGE_APPROVE)
def reject_breakage(breakage_id):
    connection = db()
    try:
        breakage = connection.execute("SELECT * FROM breakages WHERE id=?", (breakage_id,)).fetchone()
        if not breakage:
            flash("Rotura no encontrada.", "error")
            return redirect(url_for("breakages"))
        if breakage["status"] != constants.BREAKAGE_STATUS_PENDING:
            flash("Esta rotura ya fue procesada.", "error")
            return redirect(url_for("breakages"))
        connection.execute(
            "UPDATE breakages SET status=?, approved_by=?, decided_at=CURRENT_TIMESTAMP WHERE id=?",
            (constants.BREAKAGE_STATUS_REJECTED, g.user["id"], breakage_id)
        )
        write_audit_log(connection, user=g.user, action=constants.AUDIT_ACTION_REJECT, entity_type="breakage",
                         entity_id=breakage_id, before={"status": constants.BREAKAGE_STATUS_PENDING},
                         after={"status": constants.BREAKAGE_STATUS_REJECTED},
                         store_id=breakage["store_id"], ip_address=request.remote_addr)
        connection.commit()
        logger.info(f"Breakage {breakage_id} rejected by user {g.user['id']}")
        flash("Rotura rechazada.", "success")
    except Exception as e:
        connection.rollback()
        flash("Error al rechazar la rotura.", "error")
        logger.error(f"Breakage rejection error: {e}")
    return redirect(url_for("breakages"))


@app.get("/audit-trail")
@permission_required(constants.PERM_AUDIT_VIEW)
def audit_trail():
    """Admin-only audit trail: filterable, paginated, CSV-exportable view over
    the append-only audit_logs table. Filters: user, store, date range,
    entity type, action type."""
    connection = db()
    filters = {
        "user_id": request.args.get("user_id", "").strip(),
        "store_id": request.args.get("store_id", "").strip(),
        "entity_type": request.args.get("entity_type", "").strip(),
        "action": request.args.get("action", "").strip(),
        "date_from": request.args.get("date_from", "").strip(),
        "date_to": request.args.get("date_to", "").strip(),
    }

    if request.args.get("format") == "csv":
        rows = get_audit_logs(connection, filters, page=1, page_size=1_000_000)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "created_at", "user", "role_at_time", "action", "entity_type",
                          "entity_id", "store_id", "ip_address", "before_json", "after_json"])
        for row in rows:
            writer.writerow([row["id"], row["created_at"], row["username"] or "", row["role_at_time"] or "",
                              row["action"], row["entity_type"], row["entity_id"], row["store_id"] or "",
                              row["ip_address"] or "", row["before_json"] or "", row["after_json"] or ""])
        logger.info(f"Audit trail CSV export by user {g.user['id']}")
        return Response(buffer.getvalue(), mimetype="text/csv",
                         headers={"Content-Disposition": "attachment; filename=audit_trail.csv"})

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    total = count_audit_logs(connection, filters)
    rows = get_audit_logs(connection, filters, page=page, page_size=constants.AUDIT_PAGE_SIZE)
    total_pages = max(1, (total + constants.AUDIT_PAGE_SIZE - 1) // constants.AUDIT_PAGE_SIZE)

    return render_template(
        "audit_trail.html",
        logs=rows, filters=filters, page=page, total_pages=total_pages, total=total,
        entity_types=["store", "warehouse", "product", "user", "transfer", "stock_movement", "sale", "cash_closure"],
        actions=[constants.AUDIT_ACTION_CREATE, constants.AUDIT_ACTION_UPDATE, constants.AUDIT_ACTION_DELETE,
                 constants.AUDIT_ACTION_APPROVE, constants.AUDIT_ACTION_REJECT, constants.AUDIT_ACTION_FINALIZE],
        stores=connection.execute("SELECT * FROM stores WHERE active=1").fetchall(),
        users=connection.execute("SELECT id, username FROM users ORDER BY username").fetchall(),
    )


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True)
