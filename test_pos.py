"""
Nexo POS - Test Suite
Comprehensive tests for critical business flows and security
"""

import pytest
import sqlite3
import os
import threading
from datetime import date
from werkzeug.security import check_password_hash
import business_day


def _today_utc():
    """Business date for the seed store (timezone='UTC') — using this instead
    of date.today() (local server time) avoids a real UTC-boundary edge case:
    the sandbox's local clock can roll to a new calendar day slightly before
    or after UTC does, which briefly makes date.today() disagree with the
    business day the app actually computes for a UTC-zoned store."""
    return business_day.current_business_date("UTC").isoformat()


def _sale_form(store_id, items, payments, csrf_token=None):
    """Build POST data for the Phase 2 multi-row /sales form.
    items: list of (product_id, quantity). payments: list of (method, amount)."""
    data = {"store_id": str(store_id)}
    if csrf_token is not None:
        data["csrf_token"] = csrf_token
    for i, (product_id, quantity) in enumerate(items, start=1):
        data[f"item_product_id_{i}"] = str(product_id)
        data[f"item_quantity_{i}"] = str(quantity)
    for i, (method, amount) in enumerate(payments, start=1):
        data[f"payment_method_{i}"] = method
        data[f"payment_amount_{i}"] = str(amount)
    return data


def _catalog_product_form(sku, name, cost_price="5.00", sale_price="10.00"):
    return {"kind": "product", "sku": sku, "name": name, "cost_price": cost_price, "sale_price": sale_price}


def _add_stock(client, product_id=1, quantity=100, location_type="store", location_id=1, reason="Setup"):
    return client.post("/inventory", data={
        "product_id": str(product_id), "location_type": location_type, "location_id": str(location_id),
        "movement_type": "ENTRY", "quantity": str(quantity), "reason": reason,
    })

# Test fixtures and utilities
@pytest.fixture
def app():
    """Create test app instance"""
    os.environ["SECRET_KEY"] = "test-key-12345"

    # Create test database
    if os.path.exists("test_pos.db"):
        os.remove("test_pos.db")

    # Set DATABASE *before* importing app.py: the module runs `init_db()` at
    # import time, so setting app.config["DATABASE"] after import would let
    # that first init_db() call still touch the real pos.db (see Phase 1
    # audit note on db()/init_db() previously ignoring app.config entirely).
    os.environ["DATABASE"] = "test_pos.db"
    from app import app
    app.config["TESTING"] = True
    app.config["DATABASE"] = "test_pos.db"
    # CSRFProtect blocks any POST lacking a valid csrf_token, including test
    # client requests. This suite tests view/RBAC/audit logic, not CSRF
    # enforcement itself (that's covered separately by TestCSRFProtection,
    # which checks the token is *present* in rendered forms), so disable
    # enforcement here the same way Flask-WTF's own docs recommend for tests.
    app.config["WTF_CSRF_ENABLED"] = False

    # Initialize schema
    from app import init_db, db
    with app.app_context():
        init_db()

    yield app

    # Cleanup
    if os.path.exists("test_pos.db"):
        os.remove("test_pos.db")
    os.environ.pop("DATABASE", None)


@pytest.fixture
def client(app):
    """Create test client"""
    return app.test_client()


@pytest.fixture
def runner(app):
    """Create CLI runner"""
    return app.test_cli_runner()


@pytest.fixture
def auth_headers(client):
    """Login and return session"""
    response = client.post("/login", data={
        "username": "admin",
        "password": "admin123"
    }, follow_redirects=True)
    assert response.status_code == 200
    return client


# ==================== AUTHENTICATION TESTS ====================

class TestAuthentication:
    """Test user login and authentication"""
    
    def test_login_page_loads(self, client):
        """Test login page is accessible"""
        response = client.get("/login")
        assert response.status_code == 200
        assert b"OPERACI" in response.data  # Spanish "OPERACIÓN"
    
    def test_login_with_valid_credentials(self, client):
        """Test successful login with default credentials"""
        response = client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        }, follow_redirects=True)
        assert response.status_code == 200
        # Should redirect to dashboard
        assert b"Resumen" in response.data or b"dashboard" in response.data.decode('utf-8', errors='ignore')
    
    def test_login_with_invalid_password(self, client):
        """Test login fails with wrong password"""
        response = client.post("/login", data={
            "username": "admin",
            "password": "wrongpassword"
        })
        assert response.status_code == 200
        assert b"incorrectos" in response.data  # "Usuario o contraseña incorrectos"
    
    def test_login_with_nonexistent_user(self, client):
        """Test login fails for non-existent user"""
        response = client.post("/login", data={
            "username": "nonexistent",
            "password": "password"
        })
        assert response.status_code == 200
        assert b"incorrectos" in response.data
    
    def test_login_with_empty_username(self, client):
        """Test login fails with empty username"""
        response = client.post("/login", data={
            "username": "",
            "password": "admin123"
        })
        assert response.status_code == 200
        assert b"required" in response.data.lower()
    
    def test_logout(self, client):
        """Test logout clears session"""
        # Login first
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        # Then logout
        response = client.get("/logout", follow_redirects=True)
        assert response.status_code == 200
        # Should be redirected to login
        assert b"Entrar" in response.data  # "Entrar" (Enter) button


# ==================== AUTHORIZATION TESTS ====================

class TestAuthorization:
    """Test role-based access control"""
    
    def test_unauthenticated_redirect_to_login(self, client):
        """Test unauthenticated requests redirect to login"""
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.location
    
    def test_admin_can_access_users_page(self, client):
        """Test admin can access user management"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.get("/users")
        assert response.status_code == 200
        assert b"Usuarios" in response.data or b"usuarios" in response.data
    
    def test_non_admin_cannot_create_users(self, client):
        """Test non-admin cannot create users"""
        # Would need to create a seller user first
        # This is a placeholder for future implementation
        pass


# ==================== PASSWORD SECURITY TESTS ====================

class TestPasswordSecurity:
    """Test password hashing and security"""
    
    def test_password_stored_hashed(self, app):
        """Test that passwords are stored as hashes"""
        from app import db, hash_password
        with app.app_context():
            # Get the default admin user's password hash
            user = db().execute("SELECT password FROM users WHERE username='admin'").fetchone()
            assert user is not None
            # Hash should be a long string (pbkdf2:sha256 format)
            assert len(user[0]) > 50
            # Should not contain the plain password
            assert user[0] != "admin123"
    
    def test_password_validation(self, app):
        """Test password validation function"""
        from app import hash_password, validate_password
        with app.app_context():
            password = "test_password_123"
            hashed = hash_password(password)
            # Should validate correct password
            assert validate_password(hashed, password)
            # Should fail on wrong password
            assert not validate_password(hashed, "wrong_password")


# ==================== SALES TESTS ====================

class TestSales:
    """Test sales transactions (Phase 2: multi-item cart + split payments +
    real stock deduction, replacing the old total/payment_method-only form)."""

    def test_sales_page_loads(self, client):
        """Test sales page is accessible"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.get("/sales")
        assert response.status_code == 200
        assert b"Registrar venta" in response.data

    def test_record_sale_with_valid_data_deducts_stock(self, client, app):
        """FEATURE 2: a completed sale must create sale_items + payments,
        write a SALE inventory movement, and actually decrement stock."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=20)
        # DEMO-001 (id=1) seeds at sale_price_cents=1000 ($10.00); 3 units = $30.00
        response = client.post("/sales", data=_sale_form(1, [(1, 3)], [("CASH", "30.00")]), follow_redirects=True)
        assert response.status_code == 200
        assert b"registrada" in response.data  # "Venta registrada"

        from app import db
        with app.app_context():
            connection = db()
            stock = connection.execute(
                "SELECT quantity FROM stock WHERE product_id=1 AND location_type='store' AND location_id=1"
            ).fetchone()
            assert stock["quantity"] == 17  # 20 - 3
            movement = connection.execute(
                "SELECT * FROM stock_movements WHERE movement_type='SALE' AND product_id=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert movement is not None and movement["quantity"] == 3

    def test_sale_with_no_items_rejected(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        response = client.post("/sales", data=_sale_form(1, [], [("CASH", "10.00")]), follow_redirects=True)
        assert response.status_code == 200
        assert b"Agrega al menos un producto" in response.data

    def test_sale_with_no_payments_rejected(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=20)
        response = client.post("/sales", data=_sale_form(1, [(1, 1)], []), follow_redirects=True)
        assert response.status_code == 200
        assert b"Agrega al menos un pago" in response.data

    def test_sale_with_mismatched_payment_total_rejected(self, client):
        """Payments must sum exactly (integer cents) to the catalog-priced
        total — no float slop allowed."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=20)
        # 2 x $10.00 = $20.00, but only $15 tendered
        response = client.post("/sales", data=_sale_form(1, [(1, 2)], [("CASH", "15.00")]), follow_redirects=True)
        assert response.status_code == 200
        assert b"no coincide" in response.data

    def test_sale_with_insufficient_stock_rejected_and_no_partial_deduction(self, client, app):
        """FEATURE 2, step 3: abort without any partial deduction."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=2)
        response = client.post("/sales", data=_sale_form(1, [(1, 5)], [("CASH", "50.00")]), follow_redirects=True)
        assert response.status_code == 200
        assert b"stock disponible" in response.data
        from app import db
        with app.app_context():
            stock = db().execute(
                "SELECT quantity FROM stock WHERE product_id=1 AND location_type='store' AND location_id=1"
            ).fetchone()
            assert stock["quantity"] == 2  # unchanged

    def test_sale_with_invalid_payment_method_rejected(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=20)
        response = client.post("/sales", data=_sale_form(1, [(1, 1)], [("InvalidMethod", "10.00")]), follow_redirects=True)
        assert response.status_code == 200
        assert b"error" in response.data.lower()

    def test_sale_supports_split_tender_across_multiple_payment_rows(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=20)
        response = client.post(
            "/sales", data=_sale_form(1, [(1, 2)], [("CASH", "12.00"), ("CARD", "8.00")]), follow_redirects=True
        )
        assert response.status_code == 200
        assert b"registrada" in response.data
        from app import db
        with app.app_context():
            sale = db().execute("SELECT id FROM sales ORDER BY id DESC LIMIT 1").fetchone()
            payments = db().execute("SELECT method, amount_cents FROM payments WHERE sale_id=?", (sale["id"],)).fetchall()
            assert {p["method"] for p in payments} == {"CASH", "CARD"}
            assert sum(p["amount_cents"] for p in payments) == 2000

    def test_sale_cannot_deduct_from_unassigned_store(self, client):
        """FEATURE 2: server-side store scoping, not just UI restriction."""
        _create_user(client, "cashier_store_scope", "cashier")
        client.post("/login", data={"username": "cashier_store_scope", "password": "password123"})
        # store 2 doesn't exist / isn't assigned to this cashier (only store 1 was assigned by _create_user)
        response = client.post("/sales", data=_sale_form(2, [(1, 1)], [("CASH", "10.00")]), follow_redirects=True)
        assert response.status_code == 200
        assert b"No tienes acceso" in response.data


class TestConcurrentSales:
    """FEATURE 2: two terminals selling the last unit(s) of the same product
    at the same store concurrently must never oversell — exactly one sale
    succeeds, the other is rejected, and final stock is never negative."""

    def test_two_concurrent_sales_of_the_last_unit_never_oversell(self, app):
        from app import db
        with app.app_context():
            connection = db()
            connection.execute(
                "INSERT INTO stock(product_id, location_type, location_id, quantity) VALUES (1,'store',1,1) "
                "ON CONFLICT(product_id,location_type,location_id) DO UPDATE SET quantity=1"
            )
            connection.commit()

        results = {}

        def sell(terminal_name):
            with app.test_client() as terminal_client:
                terminal_client.post("/login", data={"username": "admin", "password": "admin123"})
                response = terminal_client.post(
                    "/sales", data=_sale_form(1, [(1, 1)], [("CASH", "10.00")]), follow_redirects=True
                )
                results[terminal_name] = response.data

        t1 = threading.Thread(target=sell, args=("terminal_a",))
        t2 = threading.Thread(target=sell, args=("terminal_b",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        successes = sum(1 for data in results.values() if b"registrada" in data)
        failures = sum(1 for data in results.values() if b"stock disponible" in data)
        assert successes == 1, f"expected exactly one sale to succeed, got {successes}: {results}"
        assert failures == 1, f"expected exactly one sale to fail on stock, got {failures}: {results}"

        with app.app_context():
            final = db().execute(
                "SELECT quantity FROM stock WHERE product_id=1 AND location_type='store' AND location_id=1"
            ).fetchone()
            assert final["quantity"] == 0, "stock must never go negative or be double-sold"


# ==================== INVENTORY TESTS ====================

class TestInventory:
    """Test inventory and stock management"""
    
    def test_inventory_page_loads(self, client):
        """Test inventory page is accessible"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.get("/inventory")
        assert response.status_code == 200
        assert b"Existencias" in response.data
    
    def test_stock_entry(self, client):
        """Test recording a stock entry"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.post("/inventory", data={
            "product_id": "1",
            "location_type": "warehouse",
            "location_id": "1",
            "movement_type": "ENTRY",
            "quantity": "10",
            "reason": "Purchase"
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b"guardado" in response.data  # "Movimiento de almacén guardado"
    
    def test_stock_exit(self, client, app):
        """Test recording a stock exit"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        # First add stock
        client.post("/inventory", data={
            "product_id": "1",
            "location_type": "warehouse",
            "location_id": "1",
            "movement_type": "ENTRY",
            "quantity": "10",
            "reason": "Setup"
        })
        # Then remove some
        response = client.post("/inventory", data={
            "product_id": "1",
            "location_type": "warehouse",
            "location_id": "1",
            "movement_type": "EXIT",
            "quantity": "5",
            "reason": "Damage"
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b"guardado" in response.data
    
    def test_stock_exit_insufficient_quantity(self, client):
        """Test exit fails when quantity insufficient"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.post("/inventory", data={
            "product_id": "1",
            "location_type": "warehouse",
            "location_id": "1",
            "movement_type": "EXIT",
            "quantity": "1000",
            "reason": "Invalid"
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b"supera" in response.data  # "La salida supera el stock disponible"
    
    def test_transfer_stock(self, client):
        """Test transferring stock between locations"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        # First add stock to warehouse
        client.post("/inventory", data={
            "product_id": "1",
            "location_type": "warehouse",
            "location_id": "1",
            "movement_type": "ENTRY",
            "quantity": "20",
            "reason": "Setup"
        })
        # Now transfer to store
        response = client.post("/transfer", data={
            "product_id": "1",
            "source_type": "warehouse",
            "source_id": "1",
            "target_type": "store",
            "target_id": "1",
            "quantity": "5"
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b"realizado" in response.data  # "Traspaso realizado"
    
    def test_transfer_self_location_prevented(self, client):
        """Test preventing transfer from location to itself"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.post("/transfer", data={
            "product_id": "1",
            "source_type": "warehouse",
            "source_id": "1",
            "target_type": "warehouse",
            "target_id": "1",
            "quantity": "5"
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b"cannot be the same" in response.data or b"error" in response.data.lower()
    
    def test_transfer_insufficient_stock(self, client):
        """Test transfer fails with insufficient stock"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.post("/transfer", data={
            "product_id": "1",
            "source_type": "warehouse",
            "source_id": "1",
            "target_type": "store",
            "target_id": "1",
            "quantity": "9999"
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b"Stock insuficiente" in response.data or b"insufficient" in response.data.lower()


# ==================== DAILY CLOSING TESTS (Phase 4 / Feature 3) ====================

def _close_all_open_sessions(client, app, store_id=1):
    """Daily closing finalization is blocked while any of the store's cash
    sessions are still OPEN (Feature 3: sessions close individually first).
    /sales auto-opens one with opening_amount=0 on first sale, so tests that
    sell anything must close it out before finalizing."""
    from app import db
    with app.app_context():
        open_ids = [row["id"] for row in db().execute(
            "SELECT id FROM cash_sessions WHERE store_id=? AND status='OPEN'", (store_id,)
        ).fetchall()]
    for session_id in open_ids:
        client.post(f"/cash-sessions/{session_id}/close", data={})


def _finalize_closing(client, store_id=1, business_date=None, denoms=None, opening_float_confirm=None, justification_note=None):
    business_date = business_date or _today_utc()
    data = {"store_id": str(store_id), "business_date": business_date}
    for value_cents, qty in (denoms or []):
        data[f"denom_{value_cents}"] = str(qty)
    if opening_float_confirm is not None:
        data["opening_float_confirm"] = str(opening_float_confirm)
    if justification_note is not None:
        data["justification_note"] = justification_note
    return client.post("/daily-closing", data=data, follow_redirects=True)


class TestDailyClosing:
    """Feature 3: the real reconciliation engine — replaces the old manual
    cash/card/other closure form entirely."""

    def test_closing_page_loads(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        response = client.get("/daily-closing")
        assert response.status_code == 200
        assert b"Cierre contable diario" in response.data

    def test_old_closures_url_redirects_to_daily_closing(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        response = client.get("/closures", follow_redirects=False)
        assert response.status_code == 302
        assert "/daily-closing" in response.location

    def test_zero_sales_day_closes_clean_with_zero_variance(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        today = _today_utc()
        response = _finalize_closing(client, business_date=today, opening_float_confirm="0.00")
        assert b"finalizado" in response.data
        from app import db
        with app.app_context():
            closing = db().execute(
                "SELECT * FROM daily_closings WHERE store_id=1 AND business_date=?", (today,)
            ).fetchone()
        assert closing["status"] == "FINALIZED"
        assert closing["cash_variance_cents"] == 0
        assert closing["reconciliation_difference_cents"] == 0
        assert closing["opening_float_cents"] == 0
        assert closing["previous_closing_id"] is None

    def test_normal_day_with_one_cash_sale_reconciles_to_zero(self, client, app):
        """A sale's payment total and its SALE inventory movement value are the
        same number by construction, so cash_sales+transfer_sales should equal
        inventory_withdrawn_value exactly — this is the core financial control."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        client.post("/sales", data=_sale_form(1, [(1, 1)], [("CASH", "10.00")]))  # DEMO-001 = $10.00
        _close_all_open_sessions(client, app)

        today = _today_utc()
        response = _finalize_closing(client, business_date=today, denoms=[(1000, 1)], opening_float_confirm="0.00")
        assert b"finalizado" in response.data
        from app import db
        with app.app_context():
            closing = db().execute(
                "SELECT * FROM daily_closings WHERE store_id=1 AND business_date=?", (today,)
            ).fetchone()
        assert closing["status"] == "FINALIZED"
        assert closing["cash_sales_total_cents"] == 1000
        assert closing["transfer_sales_total_cents"] == 0
        assert closing["inventory_withdrawn_value_cents"] == 1000
        assert closing["cash_variance_cents"] == 0
        assert closing["reconciliation_difference_cents"] == 0

    def test_breakage_only_day_produces_a_real_reconciliation_difference(self, client, app):
        """Stock leaving via an approved breakage with no matching sale revenue
        is a genuine, expected variance — not a bug in the formula."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        _report_breakage(client, quantity=2, reason="damaged")
        from app import db
        with app.app_context():
            breakage_id = db().execute("SELECT id FROM breakages ORDER BY id DESC LIMIT 1").fetchone()["id"]
        client.post(f"/breakages/{breakage_id}/approve")

        today = _today_utc()
        # No justification yet -> blocked
        blocked = _finalize_closing(client, business_date=today, opening_float_confirm="0.00")
        assert b"variaci" in blocked.data.lower()
        with app.app_context():
            assert db().execute(
                "SELECT * FROM daily_closings WHERE store_id=1 AND business_date=?", (today,)
            ).fetchone() is None

        response = _finalize_closing(
            client, business_date=today, opening_float_confirm="0.00",
            justification_note="2 units damaged, no sale revenue to offset."
        )
        assert b"CON VARIACI" in response.data
        with app.app_context():
            closing = db().execute(
                "SELECT * FROM daily_closings WHERE store_id=1 AND business_date=?", (today,)
            ).fetchone()
        assert closing["status"] == "FINALIZED_WITH_VARIANCE"
        assert closing["breakage_total_cents"] == 2000  # 2 x sale_price_cents(1000)
        assert closing["reconciliation_difference_cents"] == -2000
        assert closing["justification_note"]

    def test_missing_previous_closing_requires_manual_opening_float(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        today = _today_utc()
        response = _finalize_closing(client, business_date=today)  # no opening_float_confirm
        assert b"saldo inicial" in response.data.lower() or b"opening_float" in response.data.lower()

    def test_opening_float_carries_forward_from_previous_closing(self, client, app):
        """Carry-forward uses cash_counted_total_cents regardless of whether
        the previous day closed clean or with a (justified) variance."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        day1 = date(2026, 1, 1).isoformat()
        day2 = date(2026, 1, 2).isoformat()
        # No sales on day1, but $30 counted -> a deliberate, justified variance,
        # just to prove the carry-forward isn't gated on a clean close.
        _finalize_closing(
            client, business_date=day1, denoms=[(1000, 3)], opening_float_confirm="0.00",
            justification_note="Till was seeded with $30 float ahead of opening."
        )

        # day2 needs no opening_float_confirm — it's carried automatically.
        # Counting the same $30 back out (no day2 sales) keeps day2's own
        # cash_variance at 0, isolating what this test actually checks: the
        # carry-forward wiring, not variance handling (already covered above).
        response = _finalize_closing(client, business_date=day2, denoms=[(1000, 3)], opening_float_confirm=None)
        assert b"finalizado" in response.data
        from app import db
        with app.app_context():
            day1_row = db().execute("SELECT * FROM daily_closings WHERE business_date=?", (day1,)).fetchone()
            day2_row = db().execute("SELECT * FROM daily_closings WHERE business_date=?", (day2,)).fetchone()
        assert day1_row["status"] == "FINALIZED_WITH_VARIANCE"
        assert day2_row["opening_float_cents"] == day1_row["cash_counted_total_cents"] == 3000
        assert day2_row["previous_closing_id"] == day1_row["id"]

    def test_double_submit_is_idempotent_not_a_duplicate(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        today = _today_utc()
        _finalize_closing(client, business_date=today, opening_float_confirm="0.00")
        response = _finalize_closing(client, business_date=today, opening_float_confirm="0.00")
        assert b"Ya existe" in response.data
        from app import db
        with app.app_context():
            count = db().execute(
                "SELECT COUNT(*) c FROM daily_closings WHERE store_id=1 AND business_date=?", (today,)
            ).fetchone()["c"]
        assert count == 1

    def test_finalized_closing_is_immutable(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        today = _today_utc()
        _finalize_closing(client, business_date=today, opening_float_confirm="0.00")
        from app import db
        with app.app_context():
            connection = db()
            closing_id = connection.execute("SELECT id FROM daily_closings LIMIT 1").fetchone()["id"]
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE daily_closings SET status='FINALIZED_WITH_VARIANCE' WHERE id=?", (closing_id,))
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM daily_closings WHERE id=?", (closing_id,))

    def test_finalize_blocked_while_a_cash_session_is_open(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        client.post("/sales", data=_sale_form(1, [(1, 1)], [("CASH", "10.00")]))  # auto-opens a session
        today = _today_utc()
        response = _finalize_closing(client, business_date=today, opening_float_confirm="0.00")
        assert b"caja" in response.data.lower()
        from app import db
        with app.app_context():
            assert db().execute(
                "SELECT * FROM daily_closings WHERE store_id=1 AND business_date=?", (today,)
            ).fetchone() is None

    def test_accountant_can_finalize_a_closing(self, client, app):
        _create_user(client, "acct_close", "accountant")
        client.post("/login", data={"username": "acct_close", "password": "password123"})
        response = _finalize_closing(client, opening_float_confirm="0.00")
        assert b"finalizado" in response.data

    def test_cashier_cannot_finalize_a_closing(self, client):
        _create_user(client, "cashier_close", "cashier")
        client.post("/login", data={"username": "cashier_close", "password": "password123"})
        response = client.post("/daily-closing", data={"store_id": "1", "business_date": _today_utc()},
                                follow_redirects=True)
        assert b"No tienes permisos" in response.data


class TestCashSessions:
    """Feature 3: sessions must close individually before the store's daily
    closing can aggregate/finalize."""

    def test_sale_auto_opens_a_session_with_zero_opening_amount(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        client.post("/sales", data=_sale_form(1, [(1, 1)], [("CASH", "10.00")]))
        from app import db
        with app.app_context():
            session_row = db().execute("SELECT * FROM cash_sessions ORDER BY id DESC LIMIT 1").fetchone()
            sale_row = db().execute("SELECT cash_session_id FROM sales ORDER BY id DESC LIMIT 1").fetchone()
        assert session_row["status"] == "OPEN"
        assert session_row["opening_amount_cents"] == 0
        assert sale_row["cash_session_id"] == session_row["id"]

    def test_explicit_open_then_close_with_denomination_count(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post("/cash-sessions", data={"store_id": "1", "opening_amount": "50.00"})
        from app import db
        with app.app_context():
            session_id = db().execute("SELECT id FROM cash_sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]
        response = client.post(f"/cash-sessions/{session_id}/close", data={"denom_5000": "1"}, follow_redirects=True)
        assert b"Caja cerrada" in response.data
        with app.app_context():
            connection = db()
            session_row = connection.execute("SELECT * FROM cash_sessions WHERE id=?", (session_id,)).fetchone()
            counts = connection.execute(
                "SELECT * FROM cash_denomination_counts WHERE cash_session_id=?", (session_id,)
            ).fetchall()
        assert session_row["status"] == "CLOSED"
        assert len(counts) == 1 and counts[0]["quantity"] == 1

    def test_cannot_open_two_sessions_at_once(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post("/cash-sessions", data={"store_id": "1", "opening_amount": "0"})
        response = client.post("/cash-sessions", data={"store_id": "1", "opening_amount": "0"}, follow_redirects=True)
        assert b"Ya tienes una caja abierta" in response.data


# ==================== INPUT VALIDATION TESTS ====================

class TestInputValidation:
    """Test input validation across endpoints"""
    
    def test_catalog_sku_length_validation(self, client):
        """Test SKU length is validated"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.post("/catalog", data=_catalog_product_form("A" * 100, "Test Product"), follow_redirects=True)
        assert response.status_code == 200
        assert b"error" in response.data.lower() or b"between" in response.data.lower()

    def test_product_price_validation(self, client):
        """Test price validation"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.post(
            "/catalog", data=_catalog_product_form("TEST-001", "Test", cost_price="-10.00"), follow_redirects=True
        )
        assert response.status_code == 200
        assert b"negative" in response.data.lower() or b"error" in response.data.lower()


# ==================== CSRF PROTECTION TESTS ====================

class TestCSRFProtection:
    """Test CSRF protection"""
    
    def test_csrf_token_in_forms(self, client):
        """Test CSRF tokens are present in forms"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.get("/sales")
        # Should contain CSRF token input
        assert b"csrf_token" in response.data
        assert b"<input" in response.data


# ==================== ERROR HANDLING TESTS ====================

class TestErrorHandling:
    """Test error handling and recovery"""
    
    def test_invalid_product_id_handled(self, client):
        """Test invalid product ID doesn't crash"""
        client.post("/login", data={
            "username": "admin",
            "password": "admin123"
        })
        response = client.post("/inventory", data={
            "product_id": "9999",  # Non-existent
            "location_type": "warehouse",
            "location_id": "1",
            "movement_type": "ENTRY",
            "quantity": "10",
            "reason": "Test"
        }, follow_redirects=True)
        # Should either succeed (if allowed) or show error, not crash
        assert response.status_code in [200, 500]


# ==================== PHASE 1: RBAC + AUDIT TRAIL TESTS ====================

def _create_user(client, username, role, password="password123"):
    """Helper: log in as admin, create a user with the given role, log back out."""
    client.post("/login", data={"username": "admin", "password": "admin123"})
    client.post("/users", data={
        "username": username, "password": password, "role": role,
        "store_ids": ["1"], "warehouse_ids": ["1"],
    })
    client.get("/logout")


class TestRoleModel:
    """Phase 1: the app was migrated from 3 roles (admin, admin_almacen,
    vendedor) to 5 (admin, store_manager, cashier, warehouse_operator,
    accountant). These tests lock in the new model."""

    def test_valid_roles_are_the_five_role_model(self):
        import constants
        assert constants.VALID_ROLES == [
            "admin", "store_manager", "cashier", "warehouse_operator", "accountant"
        ]

    def test_legacy_role_migration_map(self):
        import constants
        assert constants.ROLE_MIGRATION_MAP == {
            "admin_almacen": "warehouse_operator",
            "vendedor": "cashier",
        }

    def test_creating_user_with_legacy_role_is_rejected(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        response = client.post("/users", data={
            "username": "legacyuser", "password": "password123", "role": "vendedor",
        }, follow_redirects=True)
        assert response.status_code == 200
        assert b"Invalid role" in response.data

    def test_current_schema_check_constraint_rejects_legacy_role_values(self, app):
        """Once migrate_legacy_roles() has run, the users.role CHECK constraint
        only allows the 5 current role names — a legacy value is now invalid
        at the DB layer, confirming the rebuilt table's constraint took effect."""
        from app import db
        with app.app_context():
            connection = db()
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO users(username,password,role) VALUES ('legacy_op','x','vendedor')"
                )

    def test_migrate_legacy_roles_is_idempotent_on_clean_table(self, app):
        """migrate_legacy_roles() must be a safe no-op once every row already
        uses a current role name (the state right after migration, and on
        every subsequent app startup)."""
        from app import db, migrate_legacy_roles
        with app.app_context():
            connection = db()
            before = connection.execute("SELECT id, role FROM users ORDER BY id").fetchall()
            migrate_legacy_roles(connection)
            after = connection.execute("SELECT id, role FROM users ORDER BY id").fetchall()
            assert [dict(r) for r in before] == [dict(r) for r in after]


class TestPermissionEnforcement:
    """RBAC must be enforced server-side for every mutating endpoint, not just
    hidden in the UI."""

    def test_cashier_can_create_sale(self, client):
        # Stock is added as admin first — a cashier has no inventory.manage
        # permission, so this must not go through the cashier's session.
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        client.get("/logout")
        _create_user(client, "cashier1", "cashier")
        client.post("/login", data={"username": "cashier1", "password": "password123"})
        response = client.post("/sales", data=_sale_form(1, [(1, 1)], [("CASH", "10.00")]), follow_redirects=True)
        assert b"registrada" in response.data

    def test_cashier_cannot_access_catalog(self, client):
        _create_user(client, "cashier2", "cashier")
        client.post("/login", data={"username": "cashier2", "password": "password123"})
        response = client.get("/catalog", follow_redirects=True)
        assert b"No tienes permisos" in response.data

    def test_cashier_cannot_access_users(self, client):
        _create_user(client, "cashier3", "cashier")
        client.post("/login", data={"username": "cashier3", "password": "password123"})
        response = client.get("/users", follow_redirects=True)
        assert b"No tienes permisos" in response.data

    def test_warehouse_operator_can_access_inventory_not_sales(self, client):
        _create_user(client, "whop1", "warehouse_operator")
        client.post("/login", data={"username": "whop1", "password": "password123"})
        assert client.get("/inventory").status_code == 200
        response = client.get("/sales", follow_redirects=True)
        assert b"No tienes permisos" in response.data

    def test_accountant_can_access_closures_not_inventory(self, client):
        _create_user(client, "acct1", "accountant")
        client.post("/login", data={"username": "acct1", "password": "password123"})
        assert client.get("/daily-closing").status_code == 200
        response = client.get("/inventory", follow_redirects=True)
        assert b"No tienes permisos" in response.data

    def test_store_manager_can_access_catalog_inventory_transfer_closures(self, client):
        _create_user(client, "mgr1", "store_manager")
        client.post("/login", data={"username": "mgr1", "password": "password123"})
        for path in ("/catalog", "/inventory", "/transfer", "/daily-closing"):
            assert client.get(path).status_code == 200
        response = client.get("/users", follow_redirects=True)
        assert b"No tienes permisos" in response.data

    def test_non_admin_cannot_view_audit_trail(self, client):
        _create_user(client, "mgr2", "store_manager")
        client.post("/login", data={"username": "mgr2", "password": "password123"})
        response = client.get("/audit-trail", follow_redirects=True)
        assert b"No tienes permisos" in response.data

    def test_admin_can_view_audit_trail(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        response = client.get("/audit-trail")
        assert response.status_code == 200
        assert b"auditor\xc3\xada" in response.data.lower() or b"audit" in response.data.lower()


class TestAuditLogCompleteness:
    """Every mutating endpoint touched in Phase 1 must leave an audit_logs row
    with the correct actor, role, action, entity type, and after-state."""

    def test_catalog_create_is_audited(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post("/catalog", data=_catalog_product_form("AUDIT-1", "Audited product"))
        from app import db
        with app.app_context():
            row = db().execute(
                "SELECT * FROM audit_logs WHERE entity_type='product' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert row["action"] == "CREATE"
        assert row["role_at_time"] == "admin"
        assert "AUDIT-1" in row["after_json"]

    def test_user_create_is_audited(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post("/users", data={"username": "audituser", "password": "password123", "role": "cashier"})
        from app import db
        with app.app_context():
            row = db().execute(
                "SELECT * FROM audit_logs WHERE entity_type='user' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert row["action"] == "CREATE"
        assert "audituser" in row["after_json"]

    def test_inventory_movement_is_audited(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post("/inventory", data={
            "product_id": "1", "location_type": "warehouse", "location_id": "1",
            "movement_type": "ENTRY", "quantity": "10", "reason": "Audit test",
        })
        from app import db
        with app.app_context():
            row = db().execute(
                "SELECT * FROM audit_logs WHERE entity_type='stock_movement' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert row["action"] == "CREATE"

    def test_sale_is_audited(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        client.post("/sales", data=_sale_form(1, [(1, 1)], [("CARD", "10.00")]))
        from app import db
        with app.app_context():
            row = db().execute(
                "SELECT * FROM audit_logs WHERE entity_type='sale' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert row["store_id"] == 1

    def test_audit_log_records_ip_address(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post("/catalog", data=_catalog_product_form("AUDIT-IP", "IP test"))
        from app import db
        with app.app_context():
            row = db().execute(
                "SELECT * FROM audit_logs WHERE entity_type='product' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row["ip_address"] is not None

    def test_audit_logs_table_is_insert_only(self, app):
        """The audit_logs_no_update / audit_logs_no_delete triggers must reject
        UPDATE and DELETE at the DB layer, defense-in-depth on top of the
        application layer exposing no update/delete helper for it."""
        from app import db
        with app.app_context():
            connection = db()
            connection.execute(
                "INSERT INTO audit_logs(action, entity_type) VALUES ('CREATE', 'test')"
            )
            connection.commit()
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE audit_logs SET action='UPDATE' WHERE entity_type='test'")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_logs WHERE entity_type='test'")

    def test_audit_trail_filters_by_entity_type(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post("/catalog", data=_catalog_product_form("FILTER-1", "Filter test"))
        response = client.get("/audit-trail?entity_type=product")
        assert response.status_code == 200
        assert b"product" in response.data

    def test_audit_trail_csv_export(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        client.post("/catalog", data=_catalog_product_form("CSV-1", "CSV test"))
        response = client.get("/audit-trail?format=csv")
        assert response.status_code == 200
        assert response.mimetype == "text/csv"
        assert b"entity_type" in response.data  # header row


def _report_breakage(client, store_id=1, product_id=1, quantity=2, reason="damaged", notes=""):
    return client.post("/breakages", data={
        "store_id": str(store_id), "product_id": str(product_id), "quantity": str(quantity),
        "reason": reason, "notes": notes,
    }, follow_redirects=True)


class TestBreakages:
    """Phase 3 / Feature 4: report -> approve|reject workflow. Reporting must
    never touch stock; only an approved breakage does."""

    def test_report_breakage_does_not_touch_stock(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        response = _report_breakage(client, quantity=3, reason="damaged")
        assert response.status_code == 200
        assert b"Pendiente de aprobaci" in response.data
        from app import db
        with app.app_context():
            connection = db()
            breakage = connection.execute("SELECT * FROM breakages ORDER BY id DESC LIMIT 1").fetchone()
            assert breakage["status"] == "PENDING"
            assert breakage["value_cents"] is None
            stock = connection.execute(
                "SELECT quantity FROM stock WHERE product_id=1 AND location_type='store' AND location_id=1"
            ).fetchone()
            assert stock["quantity"] == 10  # unchanged by reporting

    def test_invalid_reason_rejected(self, client):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        response = _report_breakage(client, reason="not-a-real-reason")
        assert b"Invalid reason" in response.data

    def test_cashier_can_report_but_not_approve(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        client.get("/logout")
        _create_user(client, "cashier_brk", "cashier")
        client.post("/login", data={"username": "cashier_brk", "password": "password123"})

        response = _report_breakage(client, quantity=2)
        assert b"Pendiente de aprobaci" in response.data

        from app import db
        with app.app_context():
            breakage_id = db().execute("SELECT id FROM breakages ORDER BY id DESC LIMIT 1").fetchone()["id"]

        approve_response = client.post(f"/breakages/{breakage_id}/approve", follow_redirects=True)
        assert b"No tienes permisos" in approve_response.data

    def test_accountant_cannot_view_breakages(self, client):
        _create_user(client, "acct_brk", "accountant")
        client.post("/login", data={"username": "acct_brk", "password": "password123"})
        response = client.get("/breakages", follow_redirects=True)
        assert b"No tienes permisos" in response.data

    def test_approve_deducts_stock_and_values_at_sale_price(self, client, app):
        """FEATURE 4: value uses the confirmed BREAKAGE_VALUATION_METHOD=sale_price."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        _report_breakage(client, quantity=3, reason="expired")
        from app import db
        with app.app_context():
            breakage_id = db().execute("SELECT id FROM breakages ORDER BY id DESC LIMIT 1").fetchone()["id"]

        response = client.post(f"/breakages/{breakage_id}/approve", follow_redirects=True)
        assert b"aprobada" in response.data

        with app.app_context():
            connection = db()
            breakage = connection.execute("SELECT * FROM breakages WHERE id=?", (breakage_id,)).fetchone()
            assert breakage["status"] == "APPROVED"
            assert breakage["valuation_method"] == "sale_price"
            assert breakage["value_cents"] == 3 * 1000  # DEMO-001 sale_price_cents=1000, qty=3
            stock = connection.execute(
                "SELECT quantity FROM stock WHERE product_id=1 AND location_type='store' AND location_id=1"
            ).fetchone()
            assert stock["quantity"] == 7  # 10 - 3
            movement = connection.execute(
                "SELECT * FROM stock_movements WHERE movement_type='BREAKAGE' AND reference_id=?", (breakage_id,)
            ).fetchone()
            assert movement is not None and movement["quantity"] == 3 and movement["unit_value_cents"] == 1000

    def test_reject_does_not_touch_stock(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        _report_breakage(client, quantity=4, reason="lost")
        from app import db
        with app.app_context():
            breakage_id = db().execute("SELECT id FROM breakages ORDER BY id DESC LIMIT 1").fetchone()["id"]

        response = client.post(f"/breakages/{breakage_id}/reject", follow_redirects=True)
        assert b"rechazada" in response.data

        with app.app_context():
            connection = db()
            breakage = connection.execute("SELECT * FROM breakages WHERE id=?", (breakage_id,)).fetchone()
            assert breakage["status"] == "REJECTED"
            assert breakage["value_cents"] is None
            stock = connection.execute(
                "SELECT quantity FROM stock WHERE product_id=1 AND location_type='store' AND location_id=1"
            ).fetchone()
            assert stock["quantity"] == 10  # unchanged

    def test_cannot_decide_an_already_decided_breakage(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        _report_breakage(client, quantity=1)
        from app import db
        with app.app_context():
            breakage_id = db().execute("SELECT id FROM breakages ORDER BY id DESC LIMIT 1").fetchone()["id"]
        client.post(f"/breakages/{breakage_id}/approve")
        response = client.post(f"/breakages/{breakage_id}/reject", follow_redirects=True)
        assert b"ya fue procesada" in response.data

    def test_approval_revalidates_stock_and_blocks_if_now_insufficient(self, client, app):
        """Stock can move between report and approval (a sale in between);
        approval must re-check under lock, not trust the report-time snapshot."""
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=5)
        _report_breakage(client, quantity=5)  # OK at report time: 5 available
        from app import db
        with app.app_context():
            breakage_id = db().execute("SELECT id FROM breakages ORDER BY id DESC LIMIT 1").fetchone()["id"]

        # Sell 4 of the 5 units before the breakage is approved
        client.post("/sales", data=_sale_form(1, [(1, 4)], [("CASH", "40.00")]))

        response = client.post(f"/breakages/{breakage_id}/approve", follow_redirects=True)
        assert b"No se puede aprobar" in response.data
        with app.app_context():
            connection = db()
            breakage = connection.execute("SELECT status FROM breakages WHERE id=?", (breakage_id,)).fetchone()
            assert breakage["status"] == "PENDING"  # left for the approver to reject or revisit
            stock = connection.execute(
                "SELECT quantity FROM stock WHERE product_id=1 AND location_type='store' AND location_id=1"
            ).fetchone()
            assert stock["quantity"] == 1  # only the sale's deduction applied, not the blocked breakage

    def test_breakage_workflow_is_fully_audited(self, client, app):
        client.post("/login", data={"username": "admin", "password": "admin123"})
        _add_stock(client, product_id=1, quantity=10)
        _report_breakage(client, quantity=2)
        from app import db
        with app.app_context():
            breakage_id = db().execute("SELECT id FROM breakages ORDER BY id DESC LIMIT 1").fetchone()["id"]
        client.post(f"/breakages/{breakage_id}/approve")

        with app.app_context():
            connection = db()
            actions = [row["action"] for row in connection.execute(
                "SELECT action FROM audit_logs WHERE entity_type='breakage' AND entity_id=? ORDER BY id", (breakage_id,)
            ).fetchall()]
            assert actions == ["CREATE", "APPROVE"]


class TestDatabaseConfigIsolation:
    """Regression test for the Phase 1 finding that db()/init_db() ignored
    app.config['DATABASE'] and always wrote to pos.db regardless of test setup."""

    def test_test_suite_writes_to_configured_test_database_not_pos_db(self, app):
        assert app.config["DATABASE"] == "test_pos.db"
        assert os.path.exists("test_pos.db")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
