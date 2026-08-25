# Nexo POS - Comprehensive Audit Report
**Date:** 2026-08-24  
**Project:** Flask-based Point of Sale (POS), Inventory Management, and Accounting System

---

## EXECUTIVE SUMMARY

This is a foundational POS system built with Flask and SQLite. While it demonstrates good separation of concerns and basic role-based access control, it has **critical security vulnerabilities**, **data integrity risks**, and lacks production-ready features. The codebase is relatively clean but needs modernization in password handling, input validation, CSRF protection, and transaction safety.

---

## SECTION 1: ARCHITECTURE & DATA FLOW

### Overall Structure
- **Tech Stack:** Flask 3.0+, SQLite3, Jinja2 templates, vanilla CSS
- **Architecture:** Monolithic Flask app with single database
- **Modules:**
  - **POS Module:** Sales recording with payment methods, cash closures
  - **Inventory Module:** Stock tracking per location (store/warehouse), stock movements, transfers
  - **Accounting Module:** Basic cash closure reconciliation (cash, card, other)
  - **Admin Module:** User management, role-based access, product/store/warehouse configuration

### Data Flow
```
User Login → Role Check → Location Assignment
                ↓
        POS Module           Inventory Module         Admin Module
            ↓                      ↓                        ↓
      Sales → Payment      Stock Movements         User/Location Config
          ↓                   Transfers
      Closures ←────────────  Stock
```

### Key Entities & Relationships
- **users** (id, username, password, role, active)
  - Roles: admin, admin_almacen (warehouse admin), vendedor (seller)
- **stores** & **warehouses** (id, name, address, active)
- **user_stores** & **user_warehouses** (many-to-many assignments)
- **products** (id, sku, name, price, active)
- **stock** (product_id, location_type, location_id, quantity) — tracks inventory
- **sales** (id, store_id, total, payment_method, user_id, created_at)
- **cash_closures** (id, store_id, closure_date, cash/card/other_total, notes, user_id)
- **stock_movements** (id, product_id, location_type, location_id, movement_type, quantity, reason, user_id, created_at)
- **transfers** (id, product_id, source_type, source_id, target_type, target_id, quantity, user_id, created_at)

---

## SECTION 2: CRITICAL BUGS & BROKEN FEATURES

### Bug #1: Sales Don't Deduct Stock
**Severity:** CRITICAL  
**Issue:** The `/sales` endpoint records a sale but **does not deduct stock** from inventory. This breaks the entire inventory system:
- Sales can be recorded indefinitely even if stock is 0
- Inventory counts become meaningless
- Accounting entries (closures) won't match actual stock

**Impact:** Impossible to reconcile inventory with sales history.

**Location:** `app.py`, `/sales` endpoint (lines 225–240)

---

### Bug #2: Missing Sales Line Items
**Severity:** HIGH  
**Issue:** The sales table only stores `total` and `payment_method`, but not *which products* were sold. You cannot:
- See which products were in a sale
- Calculate cost of goods sold (COGS)
- Analyze product performance
- Verify receipts match the sale record

**Expected:** A `sale_items` table should link products to sales (product_id, quantity, unit_price, line_total).

**Location:** Database schema (line 21), `/sales` endpoint

---

### Bug #3: No Sale-to-Closure Reconciliation
**Severity:** HIGH  
**Issue:** The closure endpoint records cash/card/other totals manually but:
- Does **not verify** they match total sales for that day
- Allows closures with arbitrary numbers unrelated to actual sales
- Provides no reconciliation mechanism

**Expected:** Closures should automatically calculate expected totals from sales, with discrepancies highlighted.

---

### Bug #4: Stock Movements Logic Flaw
**Severity:** MEDIUM  
**Issue:** In `/inventory`, when recording a stock movement:
```python
new_quantity = current_quantity + quantity if movement_type == "entry" else current_quantity - quantity
```
This **replaces** the quantity instead of updating it. If a product has no prior stock record, the logic should insert a new record, but the subsequent update logic is brittle.

**Better:** Use explicit INSERT/UPDATE with proper error handling.

---

### Bug #5: Transfer Between Same Location
**Severity:** MEDIUM  
**Issue:** The transfer endpoint doesn't prevent transferring stock from a location to itself, which is nonsensical:
```python
if source_type == target_type and source_id == target_id:
    # Should reject this
```

---

### Bug #6: No Handling of Negative Stock Edge Case
**Severity:** MEDIUM  
**Issue:** If multiple concurrent requests try to deduct stock, there's a race condition:
- User A checks stock (50 units)
- User B checks stock (50 units)
- Both deduct 40 units → final quantity is 10 instead of -30 (caught) or 10 (not caught)

SQLite doesn't have row-level locking by default.

---

## SECTION 3: SECURITY ISSUES (CRITICAL)

### Security Issue #1: Weak Password Hashing
**Severity:** CRITICAL  
**Issue:** Passwords are hashed with SHA256 directly:
```python
def hash_password(value):
    return sha256(value.encode("utf-8")).hexdigest()
```
Problems:
- SHA256 is not a password hash function (no salt, no work factor)
- Vulnerable to rainbow tables and fast GPU attacks
- No defense against brute force

**Fix:** Use `werkzeug.security.generate_password_hash()` or `argon2-cffi`.

---

### Security Issue #2: Hardcoded Default Secret Key
**Severity:** CRITICAL  
**Issue:** 
```python
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")
```
The fallback is a **public, known key** visible in source code. This breaks session security.

**Fix:** 
- Fail fast if `SECRET_KEY` is not set
- Never ship with a default

---

### Security Issue #3: No CSRF Protection
**Severity:** CRITICAL  
**Issue:** All forms are unprotected against CSRF attacks. An attacker can make a user's browser submit forms (create users, record fake sales, etc.) without user knowledge.

**Fix:** Use Flask-WTF with CSRF tokens on all POST forms.

---

### Security Issue #4: SQL Injection via Dynamic Table Names
**Severity:** HIGH  
**Issue:** In `/catalog`:
```python
table = "stores" if kind == "store" else "warehouses" if kind == "warehouse" else "products"
db().execute(f"INSERT INTO {table}(name,address) VALUES (?,?)", ...)
```
While the column values are parameterized, the table name is not. If `kind` is manipulated, arbitrary SQL can execute.

**Fix:** Use a whitelist dict instead of dynamic table names.

---

### Security Issue #5: Missing Input Validation
**Severity:** HIGH  
**Issues:**
- Product SKU can be duplicated if schema constraint fails silently
- No length limits on text fields
- No sanitization of user input before display (XSS risk)
- No validation of numeric ranges (price can be negative, quantity can be huge)
- No check for valid dates

**Fix:** Add Flask-WTF/Marshmallow for form validation.

---

### Security Issue #6: No Authentication on Initialization
**Severity:** MEDIUM  
**Issue:** `init_db()` runs every time the app starts and may create default admin user without logging. This is dangerous if accidentally called in production.

**Fix:** Only initialize once, with explicit migration system.

---

### Security Issue #7: Overly Permissive Role System
**Severity:** MEDIUM  
**Issue:**
- `admin_almacen` can create/edit products and locations, affecting all stores
- No audit trail of who made configuration changes
- Admins can modify other admins' records

**Fix:** Add audit logging; restrict configuration changes to superuser.

---

### Security Issue #8: No Rate Limiting or Account Lockout
**Severity:** MEDIUM  
**Issue:** Brute force attacks on login are not throttled.

**Fix:** Add rate limiting (Flask-Limiter) and account lockout after N failed attempts.

---

### Security Issue #9: Sensitive Data in URLs
**Severity:** LOW  
**Issue:** User/product IDs are exposed in URLs. While not directly exploitable (URLs are guessed anyway), consider UUIDs for sensitive resources.

---

## SECTION 4: DATA INTEGRITY ISSUES

### Integrity Issue #1: Inventory Doesn't Sync with Sales (CRITICAL)
As noted in Bug #1, sales don't deduct stock. This is a data integrity disaster.

---

### Integrity Issue #2: Missing Transaction Safety
**Severity:** HIGH  
**Issue:** Complex operations like transfers involve multiple SQL statements but don't use explicit transactions:
```python
connection.execute("INSERT INTO stock(...)")
connection.execute("INSERT INTO stock(...)")
connection.execute("INSERT INTO transfers(...)")
connection.commit()
```
If the app crashes between INSERT and COMMIT, data is left in an inconsistent state.

**Fix:** Use explicit `BEGIN TRANSACTION` or SQLite context managers.

---

### Integrity Issue #3: No Foreign Key Enforcement by Default
**Severity:** MEDIUM  
**Issue:** The schema enables `PRAGMA foreign_keys = ON` only at runtime:
```python
g.db.execute("PRAGMA foreign_keys = ON")
```
If this line is skipped, referential integrity is not enforced.

**Fix:** Enable it in SQLite connection string or schema-level constraints.

---

### Integrity Issue #4: Soft Deletes But No Archival
**Severity:** MEDIUM  
**Issue:** Records have `active` flags but are never actually deleted. Over time:
- Database grows indefinitely
- Old inactive records pollute queries
- Historical data is mixed with current data

**Fix:** Use proper archival tables or audit log schema.

---

### Integrity Issue #5: No Audit Trail
**Severity:** MEDIUM  
**Issue:** No record of:
- Who changed product prices
- Who created fake transfers
- When configurations changed
- What the state was before changes

**Fix:** Add audit logging for sensitive operations.

---

### Integrity Issue #6: Stock Query Performance
**Severity:** MEDIUM  
**Issue:** Stock queries use multiple LEFT JOINs on every request:
```sql
SELECT stock.quantity, products.sku, ... 
FROM stock 
JOIN products ... 
LEFT JOIN stores ... 
LEFT JOIN warehouses ...
```
With 10k products and 50 locations, this is slow. No indexes defined.

**Fix:** Add indexes on `stock.product_id`, `products.id`, `stores.id`, `warehouses.id`.

---

## SECTION 5: CODE SMELLS & QUALITY ISSUES

### Code Smell #1: Repeated Database Queries
**Issue:** Many endpoints query the same data multiple times:
```python
@app.route("/inventory", methods=["GET", "POST"])
def inventory():
    # Called for form dropdown
    products=connection.execute("SELECT * FROM products WHERE active=1").fetchall()
    # Called later for display
    product_rows=connection.execute("SELECT stock.*, products... FROM stock JOIN products...")
    # And stores/warehouses again
    stores=connection.execute("SELECT * FROM stores WHERE active=1").fetchall()
    warehouses=connection.execute("SELECT * FROM warehouses WHERE active=1").fetchall()
```
**Fix:** Fetch once, pass as context.

---

### Code Smell #2: No Helper Functions
**Issue:** SQL queries are repeated throughout:
```python
# Similar across 5+ endpoints:
db().execute("SELECT * FROM stores WHERE active=1").fetchall()
db().execute("SELECT * FROM products WHERE active=1").fetchall()
```
**Fix:** Create service functions:
```python
def get_active_stores():
    return db().execute("SELECT * FROM stores WHERE active=1").fetchall()
```

---

### Code Smell #3: Monolithic app.py
**Severity:** LOW  
**Issue:** All code (models, views, services) in one 400-line file. Hard to test, maintain, navigate.

**Fix:** Split into blueprints/modules (pos/, inventory/, admin/).

---

### Code Smell #4: No Error Logging
**Issue:** Exceptions are silently ignored or caught without logging:
```python
except sqlite3.IntegrityError:
    flash("El usuario ya existe...", "error")
    # No log.error() call
```
Hard to debug production issues.

**Fix:** Add logging module with file/stdout handlers.

---

### Code Smell #5: Magic Strings
**Severity:** LOW  
**Issue:** 
```python
if current_user.role in ['admin','admin_almacen']:
if movement_type == "entry":
table = "stores" if kind == "store" else "warehouses"
```
**Fix:** Use Enums or constants module.

---

### Code Smell #6: Inconsistent Naming
**Severity:** LOW  
**Issue:**
- `store_id` vs `location_id`
- `movement_type` vs `kind`
- Inconsistent abbreviations (x, sale, user)

**Fix:** Use consistent naming conventions.

---

### Code Smell #7: No Type Hints
**Issue:** Python code lacks type hints, making it hard to understand expected parameter/return types.

**Fix:** Add type hints (PEP 484).

---

## SECTION 6: UI/UX ISSUES

### UX Issue #1: No Confirmation Dialogs
**Issue:** Destructive operations (record sale, create user, transfer stock) happen immediately with no confirmation.

**Fix:** Add JavaScript confirmation or server-side verification step.

---

### UX Issue #2: No Loading States
**Issue:** Forms give no feedback after submission (no spinning loader or disabled button).

**Fix:** Add `button:disabled` CSS and JS to disable form during submission.

---

### UX Issue #3: Missing Pagination
**Issue:** Tables show only last 8–30 records. Users can't see historical data.

**Fix:** Add pagination with prev/next buttons.

---

### UX Issue #4: No Search/Filter
**Issue:** No way to search products by name/SKU or filter sales by date range.

**Fix:** Add search fields with GET parameter filtering.

---

### UX Issue #5: Inventory Not Linked to Sales
**Issue:** On the Sales page, you can't see if a product is in stock before recording a sale.

**Fix:** Add stock availability check to sales form.

---

### UX Issue #6: No Dark Mode or Accessibility
**Issue:** High contrast is OK, but no focus states on form inputs, no ARIA labels, no keyboard navigation hints.

**Fix:** Add focus styles, ARIA labels, ensure keyboard navigability.

---

### UX Issue #7: Timezone/Date Format Issues
**Issue:** Dates are stored in ISO format but no timezone handling. Closures can be recorded with wrong dates.

**Fix:** Standardize on UTC with client-side timezone display.

---

## SECTION 7: MISSING TESTS

**Severity:** HIGH  
No automated tests exist. Critical flows are untested:
- User login with invalid credentials
- Stock deduction on sale
- Transfer with insufficient stock
- Concurrent sales race conditions
- Cash closure reconciliation
- Permission checks

**Fix:** Add pytest fixtures and test suite for:
- Auth (login, logout, roles)
- Sales (create, deduct stock, generate line items)
- Inventory (transfers, movements, stock validation)
- Closures (reconciliation, date uniqueness)

---

## SECTION 8: DEPENDENCIES

### Current Dependencies
- **Flask >= 3.0, < 4.0** ✓ (Current, stable)
- **pytest >= 8.0, < 9.0** ✓ (Current, but not used)

### Missing Dependencies (Production)
- **werkzeug** (for password hashing) — needed for security
- **Flask-WTF** (for CSRF, form validation) — critical
- **Flask-Limiter** (for rate limiting) — recommended
- **python-dotenv** (for env variable management) — recommended

### Recommendations
- Update to latest Flask 3.1+ for bug fixes
- Add production WSGI server (Gunicorn, Waitress) — currently using Flask debug server
- Add database migration tool (Alembic, Flask-Migrate) — currently no migration system

---

## SECTION 9: MISSING FEATURES FOR PRODUCTION

1. **Sales Line Items** — Which products in each sale?
2. **Discount & Tax Calculation** — Currently just a flat total
3. **Return/Refund Handling** — No way to reverse a sale
4. **Low Stock Alerts** — No warnings when stock is low
5. **User Audit Log** — Who did what and when?
6. **Multi-Currency Support** — All prices hardcoded to single currency
7. **Expense Tracking** — Only sales, no expenses (COGS, utilities, etc.)
8. **Profit/Loss Reports** — No analytics
9. **Email Notifications** — No alerts for low stock, failed transfers
10. **Backup/Recovery** — No automated database backup

---

## SUMMARY OF FINDINGS

| Category | Count | Severity |
|----------|-------|----------|
| Critical Security Issues | 5 | 🔴 |
| High-Impact Data Bugs | 6 | 🔴 |
| Data Integrity Risks | 6 | 🟠 |
| Code Quality Issues | 7 | 🟡 |
| UI/UX Issues | 7 | 🟡 |
| Missing Tests | 1 (suite) | 🟠 |
| Missing Production Features | 10+ | 🟡 |

---

## RECOMMENDATIONS: PHASE 2 PRIORITIES

### Priority 1 (Do First — Security & Data Integrity)
1. ✅ Fix password hashing (Werkzeug or Argon2)
2. ✅ Add CSRF protection (Flask-WTF)
3. ✅ Implement sales stock deduction logic
4. ✅ Add sales line items table
5. ✅ Add input validation across all forms
6. ✅ Remove/fix hardcoded SECRET_KEY
7. ✅ Add transaction safety to complex operations

### Priority 2 (High Value)
8. ✅ Add sale-to-closure reconciliation logic
9. ✅ Add audit logging for sensitive operations
10. ✅ Implement database indexes for performance
11. ✅ Add test suite for critical flows

### Priority 3 (Good-to-Have)
12. ✅ Refactor into modular structure (blueprints)
13. ✅ Add rate limiting
14. ✅ Improve UI/UX (confirmations, pagination, search)
15. ✅ Add low-stock alerts

---

**Next Steps:** Await approval to proceed with Phase 2 implementation.
