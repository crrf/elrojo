"""
Nexo POS - Database Service Layer
Centralized database query functions to reduce duplication and improve maintainability
"""

import json

from flask import current_app, g
from constants import LOCATION_STORE, LOCATION_WAREHOUSE, VALID_LOCATION_TYPES


def db():
    """Get database connection (Flask context-aware). Reads the path from
    app.config["DATABASE"] so tests pointed at an isolated file actually use
    it (see the note in app.py about the bug this fixes)."""
    if "db" not in g:
        import sqlite3
        import os

        database_path = current_app.config.get("DATABASE")
        if not database_path:
            BASE_DIR = os.path.dirname(os.path.abspath(__file__))
            database_path = os.path.join(BASE_DIR, "pos.db")

        g.db = sqlite3.connect(database_path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


# ============ ACTIVE RECORDS QUERIES ============

def get_active_stores():
    """Get all active stores"""
    return db().execute("SELECT * FROM stores WHERE active=1").fetchall()


def get_active_warehouses():
    """Get all active warehouses"""
    return db().execute("SELECT * FROM warehouses WHERE active=1").fetchall()


def get_active_products():
    """Get all active products"""
    return db().execute("SELECT * FROM products WHERE active=1").fetchall()


def get_all_users():
    """Get all users ordered by username"""
    return db().execute("SELECT id,username,role,active FROM users ORDER BY username").fetchall()


# ============ LOCATION QUERIES ============

def get_location_name(location_type, location_id):
    """Get location name (store or warehouse)"""
    if location_type == LOCATION_STORE:
        result = db().execute("SELECT name FROM stores WHERE id=?", (location_id,)).fetchone()
    elif location_type == LOCATION_WAREHOUSE:
        result = db().execute("SELECT name FROM warehouses WHERE id=?", (location_id,)).fetchone()
    else:
        return None
    return result["name"] if result else None


# ============ STOCK QUERIES ============

def get_stock_at_location(product_id, location_type, location_id):
    """Get current stock quantity at a specific location"""
    result = db().execute(
        "SELECT quantity FROM stock WHERE product_id=? AND location_type=? AND location_id=?",
        (product_id, location_type, location_id)
    ).fetchone()
    return result["quantity"] if result else 0


def get_all_stock():
    """Get all stock with location and product information"""
    return db().execute(
        """SELECT stock.quantity, products.sku, products.name, stock.location_type, stock.location_id,
           COALESCE(stores.name, warehouses.name) location_name
           FROM stock
           JOIN products ON products.id=stock.product_id
           LEFT JOIN stores ON stock.location_type='store' AND stores.id=stock.location_id
           LEFT JOIN warehouses ON stock.location_type='warehouse' AND warehouses.id=stock.location_id
           ORDER BY stock.location_type, location_name, products.name"""
    ).fetchall()


def get_stock_movements(limit=20):
    """Get recent stock movements"""
    return db().execute(
        """SELECT stock_movements.*, products.sku, products.name,
           COALESCE(stores.name, warehouses.name) location_name
           FROM stock_movements
           JOIN products ON products.id=stock_movements.product_id
           LEFT JOIN stores ON stock_movements.location_type='store' AND stores.id=stock_movements.location_id
           LEFT JOIN warehouses ON stock_movements.location_type='warehouse' AND warehouses.id=stock_movements.location_id
           ORDER BY stock_movements.id DESC LIMIT ?""",
        (limit,)
    ).fetchall()


# ============ SALES QUERIES ============
# Phase 2: sales no longer carry total/payment_method directly (see the
# schema note in app.py's SCHEMA); total is derived from sale_items and the
# tender breakdown comes from the payments table. All money here is integer
# cents — callers format for display with money.format_cents().

def get_recent_sales_with_totals(connection, limit=20):
    """Recent sales with store name, computed total_cents, and a comma-joined
    list of payment methods used (a sale can have split tender)."""
    return connection.execute(
        """SELECT sales.id, sales.created_at, sales.status, stores.name store_name,
               COALESCE((SELECT SUM(subtotal_cents) FROM sale_items WHERE sale_id = sales.id), 0) total_cents,
               (SELECT GROUP_CONCAT(method) FROM payments WHERE sale_id = sales.id) methods
           FROM sales
           JOIN stores ON stores.id = sales.store_id
           ORDER BY sales.id DESC LIMIT ?""",
        (limit,)
    ).fetchall()


def get_today_sales_total_cents(connection):
    """Total of completed sales' payments for the current calendar date.
    NOTE: this uses DATE('now') (UTC/server local) as a placeholder — Feature 3
    (Daily Closing) replaces this with a store-timezone-aware business-day
    boundary; this function only backs the dashboard summary tile."""
    result = connection.execute(
        """SELECT COALESCE(SUM(payments.amount_cents), 0) c
           FROM payments
           JOIN sales ON sales.id = payments.sale_id
           WHERE DATE(sales.created_at) = DATE('now') AND sales.status = 'COMPLETED'"""
    ).fetchone()
    return result["c"] if result else 0


def get_sales_by_method_for_date_and_store(connection, store_id, date_str):
    """{method: amount_cents} for a store on a given date, from completed sales'
    payments. Used by the (Phase 4-superseded) closures reconciliation check."""
    rows = connection.execute(
        """SELECT payments.method, SUM(payments.amount_cents) amount_cents
           FROM payments
           JOIN sales ON sales.id = payments.sale_id
           WHERE sales.store_id = ? AND DATE(sales.created_at) = ? AND sales.status = 'COMPLETED'
           GROUP BY payments.method""",
        (store_id, date_str)
    ).fetchall()
    return {row["method"]: row["amount_cents"] for row in rows}


# ============ CLOSURE QUERIES ============

def get_closures(limit=30):
    """Get recent closures with store information"""
    return db().execute(
        """SELECT cash_closures.*, stores.name store_name
           FROM cash_closures
           JOIN stores ON stores.id=cash_closures.store_id
           ORDER BY closure_date DESC LIMIT ?""",
        (limit,)
    ).fetchall()


def closure_exists(store_id, closure_date):
    """Check if a closure already exists for store on date"""
    result = db().execute(
        "SELECT 1 FROM cash_closures WHERE store_id=? AND closure_date=?",
        (store_id, closure_date)
    ).fetchone()
    return result is not None


# ============ USER QUERIES ============

def get_user_by_id(user_id):
    """Get user by ID"""
    return db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_username(username):
    """Get user by username"""
    return db().execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()


def get_user_assignments(user_id):
    """Get store and warehouse assignments for a user"""
    stores = db().execute("SELECT store_id FROM user_stores WHERE user_id=?", (user_id,)).fetchall()
    warehouses = db().execute("SELECT warehouse_id FROM user_warehouses WHERE user_id=?", (user_id,)).fetchall()
    return {
        "stores": [row["store_id"] for row in stores],
        "warehouses": [row["warehouse_id"] for row in warehouses]
    }


# ============ PRODUCT QUERIES ============

def get_product_by_id(product_id):
    """Get product by ID"""
    return db().execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()


def product_exists(product_id):
    """Check if product exists"""
    result = db().execute("SELECT 1 FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
    return result is not None


# ============ TRANSFER QUERIES ============

def get_recent_transfers(limit=20):
    """Get recent stock transfers"""
    return db().execute(
        """SELECT transfers.* FROM transfers
           ORDER BY transfers.id DESC LIMIT ?""",
        (limit,)
    ).fetchall()


# ============ AUDIT LOG ============
# audit_logs is append-only at the application layer: this module intentionally
# exposes no update/delete helper for it. The audit_logs_no_update/no_delete
# triggers in app.py's SCHEMA back this up at the DB layer too.

def write_audit_log(connection, *, user, action, entity_type, entity_id=None,
                     before=None, after=None, store_id=None, ip_address=None):
    """Insert one audit log row on the caller's connection, inside the
    caller's transaction, so the audit entry commits/rolls back atomically
    with the mutation it records. `user` is the g.user row (or None for
    system actions); before/after are plain dicts, JSON-serialized here."""
    connection.execute(
        "INSERT INTO audit_logs(user_id, role_at_time, action, entity_type, entity_id, "
        "before_json, after_json, store_id, ip_address) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            user["id"] if user else None,
            user["role"] if user else None,
            action,
            entity_type,
            entity_id,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
            store_id,
            ip_address,
        ),
    )


def _audit_filter_clause(filters):
    clauses = []
    params = []
    if filters.get("user_id"):
        clauses.append("audit_logs.user_id = ?")
        params.append(filters["user_id"])
    if filters.get("store_id"):
        clauses.append("audit_logs.store_id = ?")
        params.append(filters["store_id"])
    if filters.get("entity_type"):
        clauses.append("audit_logs.entity_type = ?")
        params.append(filters["entity_type"])
    if filters.get("action"):
        clauses.append("audit_logs.action = ?")
        params.append(filters["action"])
    if filters.get("date_from"):
        clauses.append("DATE(audit_logs.created_at) >= DATE(?)")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("DATE(audit_logs.created_at) <= DATE(?)")
        params.append(filters["date_to"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def get_audit_logs(connection, filters, page=1, page_size=50):
    """Paginated, filtered audit log listing, most recent first."""
    where, params = _audit_filter_clause(filters)
    offset = (page - 1) * page_size
    return connection.execute(
        f"""SELECT audit_logs.*, users.username
            FROM audit_logs
            LEFT JOIN users ON users.id = audit_logs.user_id
            {where}
            ORDER BY audit_logs.id DESC
            LIMIT ? OFFSET ?""",
        (*params, page_size, offset),
    ).fetchall()


def count_audit_logs(connection, filters):
    where, params = _audit_filter_clause(filters)
    result = connection.execute(
        f"SELECT COUNT(*) c FROM audit_logs {where}", params
    ).fetchone()
    return result["c"] if result else 0


# ============ BREAKAGES ============

def get_assigned_store_ids(connection, user):
    """Store ids a non-admin user can act on. Admin has no restriction (caller
    should skip filtering entirely for admin, not call this)."""
    rows = connection.execute("SELECT store_id FROM user_stores WHERE user_id=?", (user["id"],)).fetchall()
    return [row["store_id"] for row in rows]


def get_breakage_by_id(connection, breakage_id):
    return connection.execute("SELECT * FROM breakages WHERE id=?", (breakage_id,)).fetchone()


def get_breakages(connection, store_ids=None, limit=50):
    """Recent breakages with store/product names and reporter/approver
    usernames. store_ids=None means unrestricted (admin); an empty list means
    "no assigned stores" and correctly returns nothing."""
    where = ""
    params = []
    if store_ids is not None:
        if not store_ids:
            return []
        placeholders = ",".join("?" * len(store_ids))
        where = f"WHERE breakages.store_id IN ({placeholders})"
        params = list(store_ids)
    return connection.execute(
        f"""SELECT breakages.*, stores.name store_name, products.sku, products.name product_name,
               reporter.username reported_by_username, approver.username approved_by_username
           FROM breakages
           JOIN stores ON stores.id = breakages.store_id
           JOIN products ON products.id = breakages.product_id
           JOIN users reporter ON reporter.id = breakages.reported_by
           LEFT JOIN users approver ON approver.id = breakages.approved_by
           {where}
           ORDER BY breakages.id DESC LIMIT ?""",
        (*params, limit)
    ).fetchall()


# ============ CASH REGISTER SESSIONS (Feature 3) ============

def get_open_cash_session(connection, cashier_id, store_id):
    return connection.execute(
        "SELECT * FROM cash_sessions WHERE cashier_id=? AND store_id=? AND status='OPEN'",
        (cashier_id, store_id)
    ).fetchone()


def get_open_cash_session_for_cashier(connection, cashier_id):
    """A cashier should never have more than one OPEN session at a time,
    across any store — used to block opening a second one elsewhere."""
    return connection.execute(
        "SELECT * FROM cash_sessions WHERE cashier_id=? AND status='OPEN'", (cashier_id,)
    ).fetchone()


def get_open_sessions_for_store(connection, store_id):
    return connection.execute(
        "SELECT cash_sessions.*, users.username cashier_username FROM cash_sessions "
        "JOIN users ON users.id = cash_sessions.cashier_id "
        "WHERE cash_sessions.store_id=? AND cash_sessions.status='OPEN'",
        (store_id,)
    ).fetchall()


def get_cash_sessions_for_store(connection, store_id, limit=30):
    return connection.execute(
        "SELECT cash_sessions.*, users.username cashier_username FROM cash_sessions "
        "JOIN users ON users.id = cash_sessions.cashier_id "
        "WHERE cash_sessions.store_id=? ORDER BY cash_sessions.id DESC LIMIT ?",
        (store_id, limit)
    ).fetchall()


def get_cash_session_by_id(connection, session_id):
    return connection.execute("SELECT * FROM cash_sessions WHERE id=?", (session_id,)).fetchone()


# ============ CASH DENOMINATION COUNTS ============

def insert_denomination_counts(connection, counts, cash_session_id=None, daily_closing_id=None):
    """counts: list of (denomination_value_cents, quantity) with quantity > 0.
    Exactly one of cash_session_id/daily_closing_id must be given (enforced
    again by the table's CHECK constraint)."""
    for denomination_value_cents, quantity in counts:
        connection.execute(
            "INSERT INTO cash_denomination_counts(cash_session_id, daily_closing_id, denomination_value_cents, quantity) "
            "VALUES (?,?,?,?)",
            (cash_session_id, daily_closing_id, denomination_value_cents, quantity)
        )


def get_denomination_counts(connection, cash_session_id=None, daily_closing_id=None):
    if cash_session_id is not None:
        return connection.execute(
            "SELECT * FROM cash_denomination_counts WHERE cash_session_id=? ORDER BY denomination_value_cents DESC",
            (cash_session_id,)
        ).fetchall()
    return connection.execute(
        "SELECT * FROM cash_denomination_counts WHERE daily_closing_id=? ORDER BY denomination_value_cents DESC",
        (daily_closing_id,)
    ).fetchall()


# ============ DAILY CLOSING AGGREGATES (Feature 3) ============
# All window arguments are [start_utc, end_utc) strings from
# business_day.business_date_bounds_utc — timezone conversion already applied,
# these just filter naive-UTC created_at columns against that precomputed range.

def get_payments_total_cents(connection, store_id, start_utc, end_utc, methods):
    placeholders = ",".join("?" * len(methods))
    result = connection.execute(
        f"""SELECT COALESCE(SUM(payments.amount_cents), 0) c
            FROM payments JOIN sales ON sales.id = payments.sale_id
            WHERE sales.store_id = ? AND sales.status = 'COMPLETED'
              AND sales.created_at >= ? AND sales.created_at < ?
              AND payments.method IN ({placeholders})""",
        (store_id, start_utc, end_utc, *methods)
    ).fetchone()
    return result["c"] if result else 0


def get_sale_inventory_value_cents(connection, store_id, start_utc, end_utc):
    """SUM(quantity * unit_value_cents) over this store's SALE inventory
    movements in the window — the "inventory_withdrawn_value" sale component."""
    result = connection.execute(
        """SELECT COALESCE(SUM(quantity * unit_value_cents), 0) c
           FROM stock_movements
           WHERE location_type='store' AND location_id=? AND movement_type='SALE'
             AND created_at >= ? AND created_at < ?""",
        (store_id, start_utc, end_utc)
    ).fetchone()
    return result["c"] if result else 0


def get_breakage_total_cents(connection, store_id, start_utc, end_utc):
    """SUM(value_cents) of breakages APPROVED (decided_at) within the window —
    approval date, not report date, since that's when the loss is formally
    recognized and stock is actually deducted (flagged as an assumption in
    the Phase 4 summary)."""
    result = connection.execute(
        """SELECT COALESCE(SUM(value_cents), 0) c
           FROM breakages
           WHERE store_id=? AND status='APPROVED' AND decided_at >= ? AND decided_at < ?""",
        (store_id, start_utc, end_utc)
    ).fetchone()
    return result["c"] if result else 0


def get_previous_daily_closing(connection, store_id, business_date):
    """Most recent finalized closing for this store strictly before
    business_date — its cash_counted_total_cents becomes the new closing's
    opening_float_cents (Feature 3's automatic carry-forward)."""
    return connection.execute(
        """SELECT * FROM daily_closings WHERE store_id=? AND business_date < ?
           ORDER BY business_date DESC LIMIT 1""",
        (store_id, str(business_date))
    ).fetchone()


def get_daily_closing_for_date(connection, store_id, business_date):
    return connection.execute(
        "SELECT * FROM daily_closings WHERE store_id=? AND business_date=?",
        (store_id, str(business_date))
    ).fetchone()


def get_recent_daily_closings(connection, store_ids=None, limit=30):
    where = ""
    params = []
    if store_ids is not None:
        if not store_ids:
            return []
        placeholders = ",".join("?" * len(store_ids))
        where = f"WHERE daily_closings.store_id IN ({placeholders})"
        params = list(store_ids)
    return connection.execute(
        f"""SELECT daily_closings.*, stores.name store_name, users.username finalized_by_username
            FROM daily_closings
            JOIN stores ON stores.id = daily_closings.store_id
            JOIN users ON users.id = daily_closings.finalized_by
            {where}
            ORDER BY daily_closings.business_date DESC, daily_closings.id DESC LIMIT ?""",
        (*params, limit)
    ).fetchall()
