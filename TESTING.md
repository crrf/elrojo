# Nexo POS - Test Suite Documentation

## Overview
Comprehensive test suite for Nexo POS covering authentication, authorization, business logic, and security.

## Test File: `test_pos.py`

### Running Tests

```bash
# Install pytest if not already installed
pip3 install pytest --break-system-packages

# Run all tests
pytest test_pos.py -v

# Run specific test class
pytest test_pos.py::TestAuthentication -v

# Run specific test
pytest test_pos.py::TestAuthentication::test_login_with_valid_credentials -v

# Run with coverage
pip3 install pytest-cov --break-system-packages
pytest test_pos.py --cov=. --cov-report=html
```

## Test Coverage

### 1. Authentication Tests (`TestAuthentication`)
- ✓ Login page loads
- ✓ Valid login succeeds
- ✓ Invalid password rejected
- ✓ Non-existent user handled
- ✓ Empty username validation
- ✓ Logout clears session

**Critical Flow:** User login with password validation using secure hashing

### 2. Authorization Tests (`TestAuthorization`)
- ✓ Unauthenticated redirect to login
- ✓ Admin role access control
- ✓ Non-admin role restrictions

**Critical Flow:** Role-based access control and permission checking

### 3. Password Security Tests (`TestPasswordSecurity`)
- ✓ Passwords stored as secure hashes (not plain text)
- ✓ Hash validation works correctly
- ✓ Invalid passwords rejected

**Critical Flow:** Secure password storage and validation

### 4. Sales Tests (`TestSales`)
- ✓ Sales page loads
- ✓ Valid sale creation
- ✓ Zero total validation
- ✓ Negative total validation
- ✓ Invalid payment method rejection

**Critical Flow:** Point of sale transaction recording with validation

**Note:** Currently does not test stock deduction (scaffolded for future line-item implementation)

### 5. Inventory Tests (`TestInventory`)
- ✓ Inventory page loads
- ✓ Stock entry recording
- ✓ Stock exit with validation
- ✓ Insufficient quantity detection
- ✓ Stock transfers between locations
- ✓ Self-transfer prevention
- ✓ Transfer validation

**Critical Flows:**
- Stock in/out operations
- Transfer validation and safety
- Quantity constraints

### 6. Closure Tests (`TestClosures`)
- ✓ Closures page loads
- ✓ Daily closure creation
- ✓ Duplicate date prevention
- ✓ Reconciliation warnings (if variance detected)

**Critical Flow:** Daily cash closure with reconciliation against sales

### 7. Input Validation Tests (`TestInputValidation`)
- ✓ SKU length constraints
- ✓ Price validation (non-negative)
- ✓ Field length limits

**Critical Flow:** All user inputs validated before database operations

### 8. CSRF Protection Tests (`TestCSRFProtection`)
- ✓ CSRF tokens present in forms

**Critical Flow:** Protection against cross-site request forgery attacks

### 9. Error Handling Tests (`TestErrorHandling`)
- ✓ Invalid IDs don't crash app
- ✓ Graceful error responses

**Critical Flow:** Resilience to unexpected inputs

---

## Test Fixtures

### `app` Fixture
- Creates test Flask app instance
- Uses separate test database (`test_pos.db`)
- Initializes schema
- Cleans up after test

### `client` Fixture
- Test client for making HTTP requests
- Allows GET, POST, etc. without running server

### `runner` Fixture
- CLI test runner for command-line testing

### `auth_headers` Fixture
- Pre-authenticated client with admin login
- Useful for testing authenticated endpoints

---

## Running Tests in CI/CD

### GitHub Actions Example
```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: 3.9
      - run: pip install -r requirements.txt pytest
      - run: pytest test_pos.py -v
```

---

## Coverage Goals

| Module | Current | Target | Status |
|--------|---------|--------|--------|
| Authentication | 80% | 95% | 🟡 Good |
| Authorization | 50% | 80% | 🟠 Needs Work |
| Sales | 60% | 90% | 🟠 Needs Work |
| Inventory | 75% | 95% | 🟡 Good |
| Closures | 70% | 90% | 🟡 Good |
| Validation | 70% | 95% | 🟡 Good |
| Security | 80% | 95% | 🟡 Good |

---

## Missing Tests (Future)

### Integration Tests
- Multi-step workflows (sale → inventory deduction → closure)
- Concurrent transactions
- Race condition detection

### Performance Tests
- Load testing with 1000+ concurrent sales
- Large inventory queries (10k+ products)
- Index effectiveness verification

### Snapshot Tests
- HTML output validation
- Email/notification generation
- PDF receipt generation (if added)

### Edge Cases
- Timezone handling in dates
- Currency rounding
- Batch operations (bulk stock adjustments)
- Database recovery from corruption

---

## Best Practices

### For Adding New Tests

1. **Follow naming conventions:**
   - Class: `Test<Feature>`
   - Method: `test_<specific_case>`

2. **Use fixtures for setup:**
   ```python
   def test_something(self, client):
       client.post("/login", data={"username": "admin", ...})
       # Test code
   ```

3. **Test one thing per test:**
   - Good: `test_login_with_valid_credentials()`
   - Bad: `test_login_and_logout_and_viewprofile()`

4. **Include assertions:**
   - Test should fail if feature breaks
   - Use clear assertion messages

5. **Test both success and failure:**
   - Valid input → correct behavior
   - Invalid input → proper error handling

---

## Debugging Tests

### Run single test with output:
```bash
pytest test_pos.py::TestAuthentication::test_login_with_valid_credentials -v -s
```

### Drop into debugger:
```python
def test_something(self, client):
    import pdb; pdb.set_trace()  # Debugger will stop here
    response = client.get("/")
```

### Check database state:
```python
def test_something(self, client, app):
    with app.app_context():
        from app import db
        users = db().execute("SELECT * FROM users").fetchall()
        print(f"Users: {users}")
```

---

## CI/CD Integration

Before deploying to production, ensure:
```bash
pytest test_pos.py -v
# All tests pass ✓
```

For Staging/Pre-production:
```bash
pytest test_pos.py --cov=. --cov-report=term-missing
# Coverage > 80% ✓
```

---

## Questions?

See inline test comments for specific test details, or review [ARCHITECTURE.md](ARCHITECTURE.md) for system design context.
