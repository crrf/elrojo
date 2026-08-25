"""
Nexo POS - Application Constants and Configuration
Centralized definitions to reduce magic strings and improve maintainability
"""

# User Roles
# NOTE: Phase 1 audit expanded the original 3-role model (admin, admin_almacen,
# vendedor) to 5 roles to match the target RBAC model. ROLE_MIGRATION_MAP below
# is applied once by migrate_db() to remap any existing rows/sessions.
ROLE_ADMIN = "admin"
ROLE_STORE_MANAGER = "store_manager"
ROLE_CASHIER = "cashier"
ROLE_WAREHOUSE_OPERATOR = "warehouse_operator"
ROLE_ACCOUNTANT = "accountant"

VALID_ROLES = [ROLE_ADMIN, ROLE_STORE_MANAGER, ROLE_CASHIER, ROLE_WAREHOUSE_OPERATOR, ROLE_ACCOUNTANT]
ROLE_DISPLAY_NAMES = {
    ROLE_ADMIN: "Administrador",
    ROLE_STORE_MANAGER: "Gerente de tienda",
    ROLE_CASHIER: "Cajero",
    ROLE_WAREHOUSE_OPERATOR: "Operario de almacén",
    ROLE_ACCOUNTANT: "Contador",
}

# Legacy role names -> new role names, applied once during migration.
ROLE_MIGRATION_MAP = {
    "admin_almacen": ROLE_WAREHOUSE_OPERATOR,
    "vendedor": ROLE_CASHIER,
}

# ============ PERMISSIONS ============
# Central map of permission -> roles allowed to exercise it. Endpoints enforce
# this server-side via permission_required() in app.py; never rely on the
# frontend/templates hiding a link as the sole guard.
PERM_CATALOG_MANAGE = "catalog.manage"
PERM_USERS_MANAGE = "users.manage"
PERM_INVENTORY_MANAGE = "inventory.manage"
PERM_TRANSFER_MANAGE = "transfer.manage"
PERM_SALES_CREATE = "sales.create"
PERM_CLOSURES_MANAGE = "closures.manage"
PERM_BREAKAGE_REPORT = "breakage.report"
PERM_BREAKAGE_APPROVE = "breakage.approve"
PERM_AUDIT_VIEW = "audit.view"

ROLE_PERMISSIONS = {
    ROLE_ADMIN: {
        PERM_CATALOG_MANAGE, PERM_USERS_MANAGE, PERM_INVENTORY_MANAGE, PERM_TRANSFER_MANAGE,
        PERM_SALES_CREATE, PERM_CLOSURES_MANAGE, PERM_BREAKAGE_REPORT, PERM_BREAKAGE_APPROVE,
        PERM_AUDIT_VIEW,
    },
    ROLE_STORE_MANAGER: {
        PERM_CATALOG_MANAGE, PERM_INVENTORY_MANAGE, PERM_TRANSFER_MANAGE,
        PERM_CLOSURES_MANAGE, PERM_BREAKAGE_REPORT, PERM_BREAKAGE_APPROVE,
    },
    ROLE_CASHIER: {
        PERM_SALES_CREATE, PERM_BREAKAGE_REPORT,
    },
    ROLE_WAREHOUSE_OPERATOR: {
        PERM_INVENTORY_MANAGE, PERM_TRANSFER_MANAGE, PERM_BREAKAGE_REPORT,
    },
    ROLE_ACCOUNTANT: {
        PERM_CLOSURES_MANAGE,
    },
}


def has_permission(role, permission):
    return permission in ROLE_PERMISSIONS.get(role, set())

# Location Types
LOCATION_STORE = "store"
LOCATION_WAREHOUSE = "warehouse"

VALID_LOCATION_TYPES = [LOCATION_STORE, LOCATION_WAREHOUSE]
LOCATION_DISPLAY_NAMES = {
    LOCATION_STORE: "Tienda",
    LOCATION_WAREHOUSE: "Almacén"
}


# Payment Methods
# Phase 2: aligned to the target model's canonical enum (CASH/TRANSFER/CARD)
# instead of the old free-form Spanish strings (Efectivo/Tarjeta/Otro), since
# Feature 3's reconciliation formulas key off exactly these three methods
# (cash_sales_total = SUM(CASH), transfer_sales_total = SUM(TRANSFER + CARD)).
# Display labels stay in Spanish via PAYMENT_DISPLAY_NAMES.
PAYMENT_CASH = "CASH"
PAYMENT_TRANSFER = "TRANSFER"
PAYMENT_CARD = "CARD"

VALID_PAYMENT_METHODS = [PAYMENT_CASH, PAYMENT_TRANSFER, PAYMENT_CARD]
PAYMENT_DISPLAY_NAMES = {
    PAYMENT_CASH: "Efectivo",
    PAYMENT_TRANSFER: "Transferencia",
    PAYMENT_CARD: "Tarjeta",
}

# Inventory movement types (Phase 2 expanded the original entry/exit pair to
# cover sales, transfers, and — Phase 3 — breakages).
MOVEMENT_TYPE_ENTRY = "ENTRY"
MOVEMENT_TYPE_EXIT = "EXIT"
MOVEMENT_TYPE_SALE = "SALE"
MOVEMENT_TYPE_TRANSFER_IN = "TRANSFER_IN"
MOVEMENT_TYPE_TRANSFER_OUT = "TRANSFER_OUT"
MOVEMENT_TYPE_BREAKAGE = "BREAKAGE"

# Only ENTRY/EXIT are chosen manually via the /inventory form; the other
# types are only ever written by the sale/transfer/breakage flows themselves.
VALID_MANUAL_MOVEMENT_TYPES = [MOVEMENT_TYPE_ENTRY, MOVEMENT_TYPE_EXIT]
MOVEMENT_DISPLAY_NAMES = {
    MOVEMENT_TYPE_ENTRY: "Entrada",
    MOVEMENT_TYPE_EXIT: "Salida",
    MOVEMENT_TYPE_SALE: "Venta",
    MOVEMENT_TYPE_TRANSFER_IN: "Traspaso (entrada)",
    MOVEMENT_TYPE_TRANSFER_OUT: "Traspaso (salida)",
    MOVEMENT_TYPE_BREAKAGE: "Rotura/Pérdida",
}

# Flash message categories
FLASH_SUCCESS = "success"
FLASH_ERROR = "error"
FLASH_WARNING = "warning"
FLASH_INFO = "info"

# Validation Constraints
MIN_USERNAME_LENGTH = 1
MAX_USERNAME_LENGTH = 100
MIN_PASSWORD_LENGTH = 6
MIN_SKU_LENGTH = 1
MAX_SKU_LENGTH = 50
MIN_NAME_LENGTH = 1
MAX_NAME_LENGTH = 255
MAX_ADDRESS_LENGTH = 500
MAX_REASON_LENGTH = 255
MAX_NOTES_LENGTH = 500

MIN_PRICE = 0.0
MAX_PRICE = 1_000_000.0
MIN_QUANTITY = 1
MAX_QUANTITY = 1_000_000

# Database Limits
MAX_RECENT_RECORDS = 20
MAX_CLOSURE_HISTORY = 30
MAX_MOVEMENT_HISTORY = 20

# POS sale form: fixed number of static rows (no JS in this app — see Phase 2
# summary). A cart with more distinct products/tenders than this needs to be
# split into more than one sale.
MAX_SALE_ITEM_ROWS = 8
MAX_SALE_PAYMENT_ROWS = 3

# ============ BREAKAGE / ROTURA ============
BREAKAGE_REASON_DAMAGED = "damaged"
BREAKAGE_REASON_EXPIRED = "expired"
BREAKAGE_REASON_LOST = "lost"
BREAKAGE_REASON_OTHER = "other"
VALID_BREAKAGE_REASONS = [BREAKAGE_REASON_DAMAGED, BREAKAGE_REASON_EXPIRED, BREAKAGE_REASON_LOST, BREAKAGE_REASON_OTHER]
BREAKAGE_REASON_DISPLAY_NAMES = {
    BREAKAGE_REASON_DAMAGED: "Dañado",
    BREAKAGE_REASON_EXPIRED: "Vencido",
    BREAKAGE_REASON_LOST: "Perdido",
    BREAKAGE_REASON_OTHER: "Otro",
}

BREAKAGE_STATUS_PENDING = "PENDING"
BREAKAGE_STATUS_APPROVED = "APPROVED"
BREAKAGE_STATUS_REJECTED = "REJECTED"

# Reconciliation Settings
# Business decision confirmed with product owner 2026-08-24: variance tolerance
# is exactly 0 — any non-zero cash_variance/reconciliation_difference requires
# a mandatory justification_note and flips the closing to FINALIZED_WITH_VARIANCE.
RECONCILIATION_VARIANCE_THRESHOLD_CENTS = 0

# Business decision confirmed with product owner 2026-08-24: breakages are
# valued at sale_price (treated as a lost sale / opportunity cost), not cost.
BREAKAGE_VALUATION_METHOD = "sale_price"  # "cost" | "sale_price"

# ============ AUDIT LOG ============
AUDIT_ACTION_CREATE = "CREATE"
AUDIT_ACTION_UPDATE = "UPDATE"
AUDIT_ACTION_DELETE = "DELETE"
AUDIT_ACTION_APPROVE = "APPROVE"
AUDIT_ACTION_REJECT = "REJECT"
AUDIT_ACTION_FINALIZE = "FINALIZE"

AUDIT_PAGE_SIZE = 50

# ============ CASH REGISTER SESSIONS ============
CASH_SESSION_STATUS_OPEN = "OPEN"
CASH_SESSION_STATUS_CLOSED = "CLOSED"

# ============ DAILY CLOSING (Feature 3) ============
DAILY_CLOSING_STATUS_FINALIZED = "FINALIZED"
DAILY_CLOSING_STATUS_FINALIZED_WITH_VARIANCE = "FINALIZED_WITH_VARIANCE"
VALID_DAILY_CLOSING_STATUSES = [DAILY_CLOSING_STATUS_FINALIZED, DAILY_CLOSING_STATUS_FINALIZED_WITH_VARIANCE]

# Default store timezone when a store is created without specifying one.
# Business-day boundaries for that store's daily closing are computed in this
# zone (see business_day.py), never in UTC.
DEFAULT_STORE_TIMEZONE = "UTC"

# Cash denomination set (value in cents, descending) used for the physical
# cash count on both cash-session close and daily-closing finalize. This is a
# generic USD-like bill/coin set — CONFIRM THE ACTUAL CURRENCY/DENOMINATIONS
# WITH THE PRODUCT OWNER before relying on this for a real deployment; it was
# not specified in the brief and is called out again in the Phase 4 summary.
CASH_DENOMINATIONS_CENTS = [10000, 5000, 2000, 1000, 500, 200, 100, 50, 25, 10, 5, 1]
