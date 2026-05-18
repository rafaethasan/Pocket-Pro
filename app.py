from __future__ import annotations

import argparse
import calendar
import csv
import io
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (
    abort,
    Flask,
    flash,
    g,
    has_request_context,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.datastructures import FileStorage
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    redis = None  # type: ignore[assignment]

try:
    import psycopg2  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psycopg2 = None  # type: ignore[assignment]

BASE_DIR = Path(__file__).resolve().parent
_db_path_env = os.getenv("INVENTORY_DB_PATH", "").strip()
_backup_dir_env = os.getenv("INVENTORY_BACKUP_DIR", "").strip()
_admin_db_path_env = os.getenv("SOFTX_ADMIN_DB_PATH", "").strip()
_tenant_data_dir_env = os.getenv("SOFTX_TENANT_DATA_DIR", "").strip()
_offline_mode_env = os.getenv("SOFTX_OFFLINE_MODE", "").strip()
_receiver_upload_dir_env = os.getenv("SOFTX_RECEIVER_UPLOAD_DIR", "").strip()
_expense_receipt_dir_env = os.getenv("SOFTX_EXPENSE_RECEIPT_DIR", "").strip()
_profile_upload_dir_env = os.getenv("SOFTX_PROFILE_UPLOAD_DIR", "").strip()
_postgres_url_env = os.getenv("SOFTX_POSTGRES_URL", "").strip()
_postgres_schema_env = os.getenv("SOFTX_POSTGRES_SCHEMA", "softx").strip()
_redis_url_env = os.getenv("SOFTX_REDIS_URL", "redis://127.0.0.1:6379/0").strip()
_pocket_legacy_base_url_env = os.getenv("POCKET_LEGACY_BASE_URL", "https://app.corexbd.com").strip()


def _is_bad_host_path(raw_path: str) -> bool:
    normalized = (raw_path or "").strip().replace("\\", "/")
    lowered = normalized.lower()
    if not normalized:
        return False
    if lowered.startswith("/users/"):
        return True
    if re.match(r"^[a-z]:/", lowered):
        return True
    if "/documents/inventory" in lowered:
        return True
    return False


POCKET_LEGACY_BASE_URL = _pocket_legacy_base_url_env.rstrip("/")


def choose_default_runtime_data_dir() -> Path:
    if not Path("/opt/render").exists():
        return BASE_DIR
    render_data_dir = Path("/var/data")
    try:
        render_data_dir.mkdir(parents=True, exist_ok=True)
        probe_path = render_data_dir / ".pocketpro-write-check"
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink(missing_ok=True)
        return render_data_dir
    except OSError:
        return BASE_DIR


DEFAULT_RUNTIME_DATA_DIR = choose_default_runtime_data_dir()


DB_PATH = (
    DEFAULT_RUNTIME_DATA_DIR / "pocketpro.db"
    if _is_bad_host_path(_db_path_env)
    else (Path(_db_path_env) if _db_path_env else (DEFAULT_RUNTIME_DATA_DIR / "pocketpro.db"))
)
BACKUP_DIR = Path(_backup_dir_env) if _backup_dir_env else (DEFAULT_RUNTIME_DATA_DIR / "backups")
ADMIN_DB_PATH = (
    DEFAULT_RUNTIME_DATA_DIR / "pocketpro_admin.db"
    if _is_bad_host_path(_admin_db_path_env)
    else (Path(_admin_db_path_env) if _admin_db_path_env else (DEFAULT_RUNTIME_DATA_DIR / "pocketpro_admin.db"))
)
TENANT_DATA_DIR = (
    DEFAULT_RUNTIME_DATA_DIR / "pocketpro_tenants"
    if _is_bad_host_path(_tenant_data_dir_env)
    else (Path(_tenant_data_dir_env) if _tenant_data_dir_env else (DEFAULT_RUNTIME_DATA_DIR / "pocketpro_tenants"))
)
OFFLINE_MODE = _offline_mode_env == "1"
POSTGRES_URL = _postgres_url_env
POSTGRES_SCHEMA = re.sub(r"[^a-zA-Z0-9_]", "_", _postgres_schema_env).strip("_").lower() or "softx"

REDIS_URL = _redis_url_env
REDIS_CACHE_ENABLED = os.getenv("SOFTX_REDIS_CACHE_ENABLED", "0").strip() == "1"
REDIS_QUEUE_ENABLED = os.getenv("SOFTX_REDIS_QUEUE_ENABLED", "0").strip() == "1"
REDIS_QUEUE_NAME = os.getenv("SOFTX_REDIS_QUEUE_NAME", "softx_jobs").strip() or "softx_jobs"
CACHE_TTL_SECONDS = max(30, int(os.getenv("SOFTX_CACHE_TTL_SECONDS", "120") or "120"))
QUEUE_POLL_TIMEOUT_SECONDS = max(
    1,
    min(30, int(os.getenv("SOFTX_QUEUE_POLL_TIMEOUT_SECONDS", "5") or "5")),
)

SUPERADMIN_USER = os.getenv("SOFTX_SUPERADMIN_USER", "admin").strip() or "admin"
SUPERADMIN_PASS = os.getenv("SOFTX_SUPERADMIN_PASS", "921514@d2K").strip() or "921514@d2K"
SUPERADMIN_OTP = os.getenv("SOFTX_SUPERADMIN_OTP", "").strip()

BILLING_WEBHOOK_SECRET = os.getenv("SOFTX_BILLING_WEBHOOK_SECRET", "").strip()
BILLING_NOTIFY_WEBHOOK = os.getenv("SOFTX_BILLING_NOTIFY_WEBHOOK", "").strip()
NOTIFY_WEBHOOK_TIMEOUT_SECONDS = int(os.getenv("SOFTX_NOTIFY_TIMEOUT_SECONDS", "10") or "10")

MAX_LOGIN_ATTEMPTS = max(3, int(os.getenv("SOFTX_MAX_LOGIN_ATTEMPTS", "5") or "5"))
LOGIN_BLOCK_MINUTES = max(5, int(os.getenv("SOFTX_LOGIN_BLOCK_MINUTES", "15") or "15"))
LOGIN_RATE_LIMIT_ENABLED = os.getenv("SOFTX_LOGIN_RATE_LIMIT_ENABLED", "0").strip() == "1"

SALE_TYPES = {"WHOLESALE", "RETAIL"}
PAYMENT_STATUSES = {"PAID", "DUE"}
USER_ROLES = {"ADMIN", "USER"}
LOCAL_RETAIL_WHOLESALE_SHOP_NAME = "Local Retail Customer"
WARRANTY_TYPES = {"OFFICIAL", "UNOFFICIAL"}
BACKUP_SCHEDULE_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY"}
BACKUP_SCHEDULE_TYPES = {"PACKAGE", "DB_COPY"}
BACKUP_SCHEDULE_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
BACKUP_SCHEDULE_WEEKDAY_INDEX = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}

USER_ALLOWED_ENDPOINTS = {
    "dashboard",
    "products",
    "product_update",
    "product_delete",
    "model_catalog",
    "parse_product_text",
    "sales",
    "sale_collect_due",
    "sales_quick_return",
    "sale_return",
    "returns",
    "customers",
    "retail_customers",
    "customer_quick_sale",
    "customer_detail",
    "customer_day_invoice",
    "retail_sales",
    "retail_invoice",
    "retail_invoice_download",
    "expenses",
    "expense_approve",
    "expense_reject",
    "expense_delete",
    "expense_receipt",
    "money_center",
    "income_approve",
    "income_reject",
    "income_delete",
    "money_center_alias",
    "petty_cash_save",
    "reports",
    "reports_export_csv",
    "due_list",
    "daily_report",
    "daily_report_export_csv",
    "stock_report",
    "stock_report_export_csv",
    "backups",
    "download_backup",
    "imei_lookup",
    "imei_lookup_alias",
    "account_password",
    "account_password_alias",
    "dashboard_alias",
    "stock_visibility_api",
    "stock_reserve_api",
    "stock_release_api",
    "stock_transfer_api",
    "due_risk_api",
    "due_risk_followup_api",
    "tenant_limits_api",
    "health_api",
    "metrics_api",
    "automation_rules_api",
    "tenant_profile_image",
    "client_logout",
}

EXPENSE_CATEGORIES = [
    "housing_rent",
    "mortgage",
    "utilities",
    "internet_phone",
    "software_tools",
    "subscription_saas",
    "security_services",
    "rent",
    "salary",
    "employee_advance",
    "advance",
    "food_groceries",
    "snacks",
    "tea_snacks",
    "meal",
    "dining",
    "kitchen",
    "transport",
    "fuel",
    "vehicle_maintenance",
    "internet",
    "marketing",
    "commission",
    "sales_commission",
    "utility",
    "maintenance",
    "office_supplies",
    "packaging",
    "courier",
    "repair_service",
    "inventory_loss",
    "fraud_loss",
    "return_loss_manual",
    "medical",
    "insurance",
    "education_training",
    "childcare",
    "family_support",
    "pet_care",
    "charity",
    "travel",
    "entertainment",
    "tax_vat",
    "bank_charge",
    "loan_payment",
    "card_settlement",
    "emi_installment",
    "asset_purchase",
    "misc",
    "other",
]

INCOME_CATEGORIES = [
    "salary",
    "bonus",
    "business_sales",
    "service_income",
    "project_income",
    "freelance",
    "commission",
    "interest",
    "investment_dividend",
    "rental_income",
    "due_collection",
    "refund_cashback",
    "gift_received",
    "loan_received",
    "capital_injection",
    "asset_sale",
    "rebate_incentive",
    "partner_settlement",
    "wallet_transfer_in",
    "other_income",
]

EXPENSE_CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "housing_rent": {"en": "Housing Rent", "bn": "বাসা/শপ ভাড়া"},
    "mortgage": {"en": "Mortgage / Building", "bn": "বিল্ডিং/মর্টগেজ"},
    "utilities": {"en": "Utilities", "bn": "ইউটিলিটি"},
    "internet_phone": {"en": "Internet + Phone", "bn": "ইন্টারনেট + ফোন"},
    "software_tools": {"en": "Software Tools", "bn": "সফটওয়্যার টুলস"},
    "subscription_saas": {"en": "Subscription / SaaS", "bn": "সাবস্ক্রিপশন / SaaS"},
    "security_services": {"en": "Security Service", "bn": "সিকিউরিটি সার্ভিস"},
    "rent": {"en": "Rent", "bn": "ভাড়া"},
    "salary": {"en": "Salary", "bn": "বেতন"},
    "employee_advance": {"en": "Employee Advance", "bn": "কর্মচারী অ্যাডভান্স"},
    "advance": {"en": "Advance", "bn": "অ্যাডভান্স"},
    "food_groceries": {"en": "Food / Groceries", "bn": "খাবার / গ্রোসারি"},
    "snacks": {"en": "Snacks", "bn": "নাস্তা"},
    "tea_snacks": {"en": "Tea + Snacks", "bn": "চা + নাস্তা"},
    "meal": {"en": "Meal", "bn": "খাবার"},
    "dining": {"en": "Dining", "bn": "বাইরে খাওয়া"},
    "kitchen": {"en": "Kitchen", "bn": "রান্নাঘর"},
    "transport": {"en": "Transport", "bn": "পরিবহন"},
    "fuel": {"en": "Fuel", "bn": "জ্বালানি"},
    "vehicle_maintenance": {"en": "Vehicle Maintenance", "bn": "গাড়ি রক্ষণাবেক্ষণ"},
    "internet": {"en": "Internet", "bn": "ইন্টারনেট"},
    "marketing": {"en": "Marketing", "bn": "মার্কেটিং"},
    "commission": {"en": "Commission", "bn": "কমিশন"},
    "sales_commission": {"en": "Sales Commission", "bn": "সেলস কমিশন"},
    "utility": {"en": "Utility", "bn": "ইউটিলিটি"},
    "maintenance": {"en": "Maintenance", "bn": "রক্ষণাবেক্ষণ"},
    "office_supplies": {"en": "Office Supplies", "bn": "অফিস সামগ্রী"},
    "packaging": {"en": "Packaging", "bn": "প্যাকেজিং"},
    "courier": {"en": "Courier", "bn": "কুরিয়ার"},
    "repair_service": {"en": "Repair Service", "bn": "মেরামত সার্ভিস"},
    "inventory_loss": {"en": "Inventory Loss", "bn": "স্টক লস"},
    "fraud_loss": {"en": "Fraud / Theft Loss", "bn": "প্রতারণা/চুরি লস"},
    "return_loss_manual": {"en": "Return Loss (Manual)", "bn": "রিটার্ন লস (ম্যানুয়াল)"},
    "medical": {"en": "Medical", "bn": "চিকিৎসা"},
    "insurance": {"en": "Insurance", "bn": "ইনস্যুরেন্স"},
    "education_training": {"en": "Education / Training", "bn": "শিক্ষা / ট্রেনিং"},
    "childcare": {"en": "Childcare", "bn": "সন্তান ব্যয়"},
    "family_support": {"en": "Family Support", "bn": "পরিবার সহায়তা"},
    "pet_care": {"en": "Pet Care", "bn": "পোষা প্রাণী"},
    "charity": {"en": "Charity", "bn": "দান/সাহায্য"},
    "travel": {"en": "Travel", "bn": "ভ্রমণ"},
    "entertainment": {"en": "Entertainment", "bn": "বিনোদন"},
    "tax_vat": {"en": "Tax / VAT", "bn": "ট্যাক্স / ভ্যাট"},
    "bank_charge": {"en": "Bank Charge", "bn": "ব্যাংক চার্জ"},
    "loan_payment": {"en": "Loan Payment", "bn": "ঋণ পরিশোধ"},
    "card_settlement": {"en": "Card Settlement", "bn": "কার্ড সেটেলমেন্ট"},
    "emi_installment": {"en": "EMI Installment", "bn": "ইএমআই কিস্তি"},
    "asset_purchase": {"en": "Asset Purchase", "bn": "অ্যাসেট ক্রয়"},
    "misc": {"en": "Misc", "bn": "বিবিধ"},
    "other": {"en": "Other", "bn": "অন্যান্য"},
}

INCOME_CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "salary": {"en": "Salary", "bn": "বেতন"},
    "bonus": {"en": "Bonus", "bn": "বোনাস"},
    "business_sales": {"en": "Business Sales", "bn": "ব্যবসায়িক বিক্রি"},
    "service_income": {"en": "Service Income", "bn": "সার্ভিস আয়"},
    "project_income": {"en": "Project Income", "bn": "প্রজেক্ট আয়"},
    "freelance": {"en": "Freelance Income", "bn": "ফ্রিল্যান্স আয়"},
    "commission": {"en": "Commission", "bn": "কমিশন"},
    "interest": {"en": "Interest", "bn": "সুদ"},
    "investment_dividend": {"en": "Investment / Dividend", "bn": "বিনিয়োগ / ডিভিডেন্ড"},
    "rental_income": {"en": "Rental Income", "bn": "ভাড়া আয়"},
    "due_collection": {"en": "Due Collection", "bn": "বাকি আদায়"},
    "refund_cashback": {"en": "Refund / Cashback", "bn": "রিফান্ড / ক্যাশব্যাক"},
    "gift_received": {"en": "Gift Received", "bn": "উপহার প্রাপ্তি"},
    "loan_received": {"en": "Loan Received", "bn": "ঋণ গ্রহণ"},
    "capital_injection": {"en": "Capital Injection", "bn": "মূলধন যোগ"},
    "asset_sale": {"en": "Asset Sale", "bn": "অ্যাসেট বিক্রি"},
    "rebate_incentive": {"en": "Rebate / Incentive", "bn": "রিবেট / ইনসেনটিভ"},
    "partner_settlement": {"en": "Partner Settlement", "bn": "পার্টনার সেটেলমেন্ট"},
    "wallet_transfer_in": {"en": "Wallet Transfer In", "bn": "ওয়ালেট ট্রান্সফার ইন"},
    "other_income": {"en": "Other Income", "bn": "অন্যান্য আয়"},
}

EXPENSE_PAYMENT_METHODS = [
    "CASH",
    "BKASH",
    "NAGAD",
    "BANK",
    "CARD",
    "OTHER",
]

INCOME_PAYMENT_METHODS = [
    "CASH",
    "BKASH",
    "NAGAD",
    "BANK",
    "CARD",
    "MFS",
    "OTHER",
]

MODEL_CONDITION_STATES = {"NEW", "ACTIVE", "USED"}
STOCK_REPORT_STATUSES = {"ALL", "IN_STOCK", "SOLD"}
STOCK_REPORT_SORT_OPTIONS = {
    "received_desc": "p.received_date DESC, p.id DESC",
    "received_asc": "p.received_date ASC, p.id ASC",
    "brand_asc": "p.brand COLLATE NOCASE ASC, p.model COLLATE NOCASE ASC, p.id DESC",
    "model_asc": "p.model COLLATE NOCASE ASC, p.brand COLLATE NOCASE ASC, p.id DESC",
    "purchase_desc": "p.purchase_price DESC, p.id DESC",
    "wholesale_desc": "p.wholesale_price DESC, p.id DESC",
    "retail_desc": "p.retail_price DESC, p.id DESC",
    "due_desc": "COALESCE(sl.due_amount, 0) DESC, p.id DESC",
}

SUPPORTED_UI_LANGUAGES = {"bn", "en"}
DEFAULT_UI_LANGUAGE = "bn"

TRACKING_MODE_IMEI = "IMEI"
TRACKING_MODE_SERIAL = "SERIAL"
TRACKING_MODE_SKU = "SKU"

DEFAULT_PRIMARY_BUSINESS = "MOBILE_WHOLESALE"
PUBLIC_LAUNCH_ENABLED_MODULES = {DEFAULT_PRIMARY_BUSINESS}
BUSINESS_MODULE_ORDER = [
    "MOBILE_WHOLESALE",
    "ELECTRONICS",
    "GROCERY",
    "CLOTHING",
    "MEDICINE",
    "COSMETICS",
    "HARDWARE",
    "STATIONERY",
    "RESTAURANT",
    "POCKET_MONEY",
]
BUSINESS_MODULE_DEFS: dict[str, dict[str, str]] = {
    "MOBILE_WHOLESALE": {
        "label_en": "Mobile Wholesale",
        "label_bn": "মোবাইল হোলসেল",
        "tracking_mode": TRACKING_MODE_IMEI,
        "icon": "fa-solid fa-mobile-screen-button",
        "style_hint_en": "Fast IMEI workflow + wholesale focus",
        "style_hint_bn": "দ্রুত IMEI workflow + হোলসেল ফোকাস",
    },
    "ELECTRONICS": {
        "label_en": "Electronics",
        "label_bn": "ইলেকট্রনিক্স",
        "tracking_mode": TRACKING_MODE_SERIAL,
        "icon": "fa-solid fa-plug-circle-bolt",
        "style_hint_en": "Serial-driven product operations",
        "style_hint_bn": "Serial ভিত্তিক প্রোডাক্ট অপারেশন",
    },
    "GROCERY": {
        "label_en": "Grocery",
        "label_bn": "গ্রোসারি",
        "tracking_mode": TRACKING_MODE_SKU,
        "icon": "fa-solid fa-basket-shopping",
        "style_hint_en": "Daily fast billing + quantity flow",
        "style_hint_bn": "দৈনিক দ্রুত বিলিং + পরিমাণভিত্তিক flow",
    },
    "CLOTHING": {
        "label_en": "Clothing",
        "label_bn": "ক্লথিং",
        "tracking_mode": TRACKING_MODE_SKU,
        "icon": "fa-solid fa-shirt",
        "style_hint_en": "Size/variant-led retail and wholesale",
        "style_hint_bn": "Size/variant ভিত্তিক retail ও wholesale",
    },
    "MEDICINE": {
        "label_en": "Medicine",
        "label_bn": "মেডিসিন",
        "tracking_mode": TRACKING_MODE_SKU,
        "icon": "fa-solid fa-briefcase-medical",
        "style_hint_en": "Batch + expiry aware medicine sales",
        "style_hint_bn": "Batch + expiry সচেতন মেডিসিন সেল",
    },
    "COSMETICS": {
        "label_en": "Cosmetics",
        "label_bn": "কসমেটিকস",
        "tracking_mode": TRACKING_MODE_SKU,
        "icon": "fa-solid fa-wand-magic-sparkles",
        "style_hint_en": "Beauty catalog and quick POS",
        "style_hint_bn": "Beauty catalog ও দ্রুত POS",
    },
    "HARDWARE": {
        "label_en": "Hardware",
        "label_bn": "হার্ডওয়্যার",
        "tracking_mode": TRACKING_MODE_SKU,
        "icon": "fa-solid fa-screwdriver-wrench",
        "style_hint_en": "Tool parts stock and purchase control",
        "style_hint_bn": "Tool parts stock ও purchase control",
    },
    "STATIONERY": {
        "label_en": "Stationery",
        "label_bn": "স্টেশনারি",
        "tracking_mode": TRACKING_MODE_SKU,
        "icon": "fa-solid fa-pen-ruler",
        "style_hint_en": "SKU-based item and invoice simplicity",
        "style_hint_bn": "SKU ভিত্তিক item ও invoice simplicity",
    },
    "RESTAURANT": {
        "label_en": "Restaurant",
        "label_bn": "রেস্টুরেন্ট",
        "tracking_mode": TRACKING_MODE_SKU,
        "icon": "fa-solid fa-utensils",
        "style_hint_en": "Menu-led POS with quick counters",
        "style_hint_bn": "Menu ভিত্তিক POS ও quick counter",
    },
    "POCKET_MONEY": {
        "label_en": "Pocket Money",
        "label_bn": "পকেট মানি",
        "tracking_mode": TRACKING_MODE_SKU,
        "icon": "fa-solid fa-wallet",
        "style_hint_en": "Personal income/expense dashboard",
        "style_hint_bn": "পার্সোনাল আয়-খরচ ড্যাশবোর্ড",
    },
}
MODULE_PROFILE_BUSINESS = "BUSINESS"
MODULE_PROFILE_POCKET = "POCKET"
MODULE_PROFILE_ALL = "ALL"
VALID_MODULE_PROFILES = {MODULE_PROFILE_BUSINESS, MODULE_PROFILE_POCKET, MODULE_PROFILE_ALL}

PLAN_LIMIT_PRESETS: dict[str, dict[str, int]] = {
    "STARTER": {
        "max_branches": 1,
        "max_users": 5,
        "max_products": 5000,
        "max_monthly_orders": 6000,
    },
    "GROWTH": {
        "max_branches": 3,
        "max_users": 25,
        "max_products": 30000,
        "max_monthly_orders": 50000,
    },
    "PRO": {
        "max_branches": 15,
        "max_users": 200,
        "max_products": 250000,
        "max_monthly_orders": 300000,
    },
    "ENTERPRISE": {
        "max_branches": -1,
        "max_users": -1,
        "max_products": -1,
        "max_monthly_orders": -1,
    },
}

BRAND_KEYWORDS = {
    "APPLE": "Apple",
    "IPHONE": "Apple",
    "SAMSUNG": "Samsung",
    "XIAOMI": "Xiaomi",
    "REDMI": "Redmi",
    "POCO": "Poco",
    "REALME": "Realme",
    "OPPO": "Oppo",
    "VIVO": "Vivo",
    "ONEPLUS": "OnePlus",
    "NOKIA": "Nokia",
    "MOTOROLA": "Motorola",
    "INFINIX": "Infinix",
    "TECNO": "Tecno",
    "ITEL": "iTel",
    "GOOGLE": "Google",
    "HONOR": "Honor",
    "HUAWEI": "Huawei",
}

COLOR_KEYWORDS = [
    "BLACK",
    "WHITE",
    "BLUE",
    "GREEN",
    "RED",
    "GOLD",
    "SILVER",
    "GRAY",
    "GREY",
    "PURPLE",
    "PINK",
    "ORANGE",
    "YELLOW",
    "NAVY",
]

CATEGORY_KEYWORDS = {
    "Smartphone": ["PHONE", "SMARTPHONE", "MOBILE", "IPHONE", "ANDROID"],
    "Tablet": ["TABLET", "IPAD", "TAB"],
    "Feature Phone": ["FEATURE PHONE", "KEYPAD"],
    "Wearable": ["WATCH", "SMARTWATCH", "BAND"],
}
IPHONE_STORAGE_GB_VALUES = {32, 64, 128, 256, 512, 1024, 2048}

app = Flask(__name__)
app.config["SECRET_KEY"] = "inventory-secret-change-me"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SOFTX_SESSION_SECURE", "0").strip() == "1"
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


@app.context_processor
def inject_public_branding() -> dict[str, object]:
    pocket_host = is_pocket_public_host()
    return {
        "public_brand_name": "Pocket Pro" if pocket_host else "Soft X",
        "public_brand_is_pocket": pocket_host,
    }

PRIVATE_RECEIVER_UPLOAD_DIR = (
    Path(_receiver_upload_dir_env)
    if _receiver_upload_dir_env
    else (BASE_DIR / "private_uploads" / "receivers")
)
PRIVATE_EXPENSE_RECEIPT_DIR = (
    Path(_expense_receipt_dir_env)
    if _expense_receipt_dir_env
    else (BASE_DIR / "private_uploads" / "expense_receipts")
)
PRIVATE_PROFILE_UPLOAD_DIR = (
    Path(_profile_upload_dir_env)
    if _profile_upload_dir_env
    else (BASE_DIR / "private_uploads" / "tenant_profiles")
)
LEGACY_RECEIVER_UPLOAD_DIR = BASE_DIR / "static" / "uploads" / "receivers"
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
BACKUP_PACKAGE_FORMAT_VERSION = 1
BACKUP_PACKAGE_MANIFEST_NAME = "manifest.json"
BACKUP_PACKAGE_DB_ARCHIVE_PATH = "tenant-data/tenant.sqlite"
POCKET_RUNTIME_PACKAGE_TYPE = "POCKETPRO_RUNTIME_EXPORT"
POCKET_RUNTIME_PACKAGE_FORMAT_VERSION = 1
POCKET_RUNTIME_MANIFEST_NAME = "pocketpro-runtime-manifest.json"
POCKET_RUNTIME_MAIN_DB_ARCHIVE_PATH = "runtime-data/pocketpro.db"
POCKET_RUNTIME_ADMIN_DB_ARCHIVE_PATH = "runtime-data/pocketpro_admin.db"
POCKET_RUNTIME_TENANT_DIR_ARCHIVE_PATH = "runtime-data/pocketpro_tenants"


def slugify_text(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower())
    clean = clean.strip("-")
    return clean or "shop"


def normalize_username(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.@+-]", "", (value or "").strip().lower())
    return clean


def normalize_login_identifier(value: str) -> str:
    return (value or "").strip().lower()


def normalize_role(value: str, default: str = "USER") -> str:
    role = (value or "").strip().upper()
    if role in {"MANAGER", "CASHIER", "STAFF"}:
        role = "USER"
    if role not in USER_ROLES:
        return default
    return role


def normalize_language(value: str | None) -> str | None:
    language = (value or "").strip().lower()
    if language in SUPPORTED_UI_LANGUAGES:
        return language
    return None


def normalize_business_module(value: str | None, default: str = DEFAULT_PRIMARY_BUSINESS) -> str:
    module_key = (value or "").strip().upper()
    if module_key in BUSINESS_MODULE_DEFS:
        return module_key
    if default == "":
        return ""
    if default in BUSINESS_MODULE_DEFS:
        return default
    return DEFAULT_PRIMARY_BUSINESS


def normalize_module_profile(value: str | None, default: str = MODULE_PROFILE_BUSINESS) -> str:
    profile = (value or "").strip().upper()
    if profile in VALID_MODULE_PROFILES:
        return profile
    if default in VALID_MODULE_PROFILES:
        return default
    return MODULE_PROFILE_BUSINESS


def module_profile_from_primary_business(primary_business: str | None) -> str:
    normalized = normalize_business_module(primary_business, default=DEFAULT_PRIMARY_BUSINESS)
    if normalized == "POCKET_MONEY":
        return MODULE_PROFILE_POCKET
    return MODULE_PROFILE_BUSINESS


def parse_enabled_modules(
    values: list[str] | tuple[str, ...] | str | None,
    fallback_primary: str | None = None,
) -> list[str]:
    raw_items: list[str] = []
    if isinstance(values, (list, tuple)):
        for item in values:
            clean_item = str(item or "").strip()
            if clean_item:
                raw_items.append(clean_item)
    else:
        for item in re.split(r"[,;\s]+", str(values or "")):
            clean_item = item.strip()
            if clean_item:
                raw_items.append(clean_item)

    unique_modules: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        normalized = normalize_business_module(item, default="")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_modules.append(normalized)

    if not unique_modules:
        primary_default = normalize_business_module(fallback_primary or DEFAULT_PRIMARY_BUSINESS)
        unique_modules = [primary_default]
    return unique_modules


def get_business_module_options() -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for key in BUSINESS_MODULE_ORDER:
        data = BUSINESS_MODULE_DEFS[key]
        public_enabled = key in PUBLIC_LAUNCH_ENABLED_MODULES
        options.append(
            {
                "key": key,
                "label_en": data["label_en"],
                "label_bn": data["label_bn"],
                "tracking_mode": data["tracking_mode"],
                "icon": data.get("icon", "fa-solid fa-layer-group"),
                "style_hint_en": data.get("style_hint_en", data["label_en"]),
                "style_hint_bn": data.get("style_hint_bn", data["label_bn"]),
                "public_enabled": public_enabled,
                "coming_soon": not public_enabled,
            }
        )
    return options


def current_request_hostname() -> str:
    if not has_request_context():
        return ""
    forwarded_host = (
        request.headers.get("X-Forwarded-Host", "")
        or request.headers.get("X-Original-Host", "")
        or request.headers.get("Host", "")
        or request.host
        or ""
    )
    return forwarded_host.split(",", 1)[0].split(":", 1)[0].strip().lower()


def is_pocket_public_host() -> bool:
    host = current_request_hostname()
    return host == "pocketpro.corexbd.com" or host.startswith("pocketpro.")


def is_public_launch_module_enabled(module_key: str | None) -> bool:
    normalized = normalize_business_module(module_key, default="")
    return normalized in PUBLIC_LAUNCH_ENABLED_MODULES


def get_expense_category_options() -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in EXPENSE_CATEGORIES:
        if key in seen:
            continue
        seen.add(key)
        labels = EXPENSE_CATEGORY_LABELS.get(key, {})
        options.append(
            {
                "key": key,
                "label_en": str(labels.get("en") or key.replace("_", " ").title()),
                "label_bn": str(labels.get("bn") or key.replace("_", " ").title()),
            }
        )
    return options


def get_income_category_options() -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in INCOME_CATEGORIES:
        if key in seen:
            continue
        seen.add(key)
        labels = INCOME_CATEGORY_LABELS.get(key, {})
        options.append(
            {
                "key": key,
                "label_en": str(labels.get("en") or key.replace("_", " ").title()),
                "label_bn": str(labels.get("bn") or key.replace("_", " ").title()),
            }
        )
    return options


def category_label(kind: str, category_key: str, ui_language: str = DEFAULT_UI_LANGUAGE) -> str:
    normalized_kind = (kind or "").strip().lower()
    normalized_key = (category_key or "").strip().lower()
    if not normalized_key:
        return "-"
    label_map = EXPENSE_CATEGORY_LABELS if normalized_kind == "expense" else INCOME_CATEGORY_LABELS
    item = label_map.get(normalized_key, {})
    if (ui_language or DEFAULT_UI_LANGUAGE).lower() == "bn":
        return str(item.get("bn") or normalized_key.replace("_", " ").title())
    return str(item.get("en") or normalized_key.replace("_", " ").title())


def get_module_default_endpoint(module_key: str | None) -> str:
    clean_module = normalize_business_module(module_key, default=DEFAULT_PRIMARY_BUSINESS)
    if clean_module == "POCKET_MONEY":
        return "money_center"
    return "dashboard"


def get_tenant_default_endpoint(tenant_row: sqlite3.Row | dict[str, object] | None) -> str:
    profile = build_business_profile(tenant_row)
    return get_module_default_endpoint(str(profile.get("primary_module") or DEFAULT_PRIMARY_BUSINESS))


def tenant_has_module(tenant_row: sqlite3.Row | dict[str, object] | None, module_key: str) -> bool:
    normalized = normalize_business_module(module_key, default="")
    if not normalized:
        return False
    profile = build_business_profile(tenant_row)
    enabled = {
        normalize_business_module(str(item), default="")
        for item in list(profile.get("enabled_modules", []))
    }
    return normalized in enabled


_REDIS_CLIENT: object | None = None
_REDIS_CLIENT_FAILED = False


def row_as_dict(row: sqlite3.Row | dict[str, object] | None) -> dict[str, object]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def redis_available() -> bool:
    return redis is not None


def get_redis_client() -> object | None:
    global _REDIS_CLIENT, _REDIS_CLIENT_FAILED
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    if _REDIS_CLIENT_FAILED or not redis_available():
        return None
    try:
        client = redis.Redis.from_url(REDIS_URL, decode_responses=True)  # type: ignore[attr-defined]
        client.ping()  # type: ignore[union-attr]
        _REDIS_CLIENT = client
        return _REDIS_CLIENT
    except Exception:
        _REDIS_CLIENT_FAILED = True
        return None


def cache_get_json(key: str) -> dict[str, object] | None:
    if not REDIS_CACHE_ENABLED:
        return None
    client = get_redis_client()
    if client is None:
        return None
    try:
        raw_value = client.get(key)  # type: ignore[union-attr]
        if not raw_value:
            return None
        payload = json.loads(raw_value)
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


def cache_set_json(key: str, payload: dict[str, object], ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
    if not REDIS_CACHE_ENABLED:
        return
    client = get_redis_client()
    if client is None:
        return
    try:
        client.setex(key, ttl_seconds, json.dumps(payload, ensure_ascii=False))  # type: ignore[union-attr]
    except Exception:
        return


def queue_push_job(job_type: str, payload: dict[str, object] | None = None) -> bool:
    if not REDIS_QUEUE_ENABLED:
        return False
    client = get_redis_client()
    if client is None:
        return False
    safe_payload = payload or {}
    message = {
        "type": (job_type or "").strip(),
        "payload": safe_payload,
        "enqueued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        client.rpush(REDIS_QUEUE_NAME, json.dumps(message, ensure_ascii=False))  # type: ignore[union-attr]
        return True
    except Exception:
        return False


def queue_pop_job(timeout_seconds: int = QUEUE_POLL_TIMEOUT_SECONDS) -> dict[str, object] | None:
    if not REDIS_QUEUE_ENABLED:
        return None
    client = get_redis_client()
    if client is None:
        return None
    try:
        row = client.blpop(REDIS_QUEUE_NAME, timeout=max(1, timeout_seconds))  # type: ignore[union-attr]
    except Exception:
        return None
    if not row:
        return None
    _, raw_payload = row
    try:
        item = json.loads(raw_payload)
        if isinstance(item, dict):
            return item
    except Exception:
        return None
    return None


def sqlite_type_to_pg(sqlite_type: str) -> str:
    normalized = str(sqlite_type or "").strip().upper()
    if "INT" in normalized:
        return "BIGINT"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return "DOUBLE PRECISION"
    if "BLOB" in normalized:
        return "BYTEA"
    if any(token in normalized for token in ("DATE", "TIME")):
        return "TIMESTAMP"
    return "TEXT"


def get_postgres_connection():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed. Install dependencies first.")
    if not POSTGRES_URL:
        raise RuntimeError("SOFTX_POSTGRES_URL is not set.")
    return psycopg2.connect(POSTGRES_URL)  # type: ignore[operator]


def export_sqlite_to_postgres(
    sqlite_db_path: Path,
    schema_name: str,
    truncate_before_load: bool = True,
) -> dict[str, int]:
    if not sqlite_db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_db_path}")

    schema = re.sub(r"[^a-zA-Z0-9_]", "_", (schema_name or "").strip().lower()).strip("_") or "softx"
    table_count = 0
    row_count = 0

    with sqlite3.connect(sqlite_db_path) as sqlite_conn:
        sqlite_conn.row_factory = sqlite3.Row
        tables = sqlite_conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        with get_postgres_connection() as pg_conn:
            with pg_conn.cursor() as pg_cur:
                pg_cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

                for table_row in tables:
                    table_name = str(table_row["name"])
                    columns = sqlite_conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                    if not columns:
                        continue

                    column_defs: list[str] = []
                    pk_columns: list[str] = []
                    col_names: list[str] = []
                    for col in columns:
                        col_name = str(col["name"])
                        col_names.append(col_name)
                        col_type = sqlite_type_to_pg(str(col["type"] or "TEXT"))
                        nullable = "NOT NULL" if int(col["notnull"] or 0) == 1 else ""
                        column_defs.append(f'"{col_name}" {col_type} {nullable}'.strip())
                        if int(col["pk"] or 0) == 1:
                            pk_columns.append(col_name)

                    pk_sql = ""
                    if pk_columns:
                        quoted_pk = ", ".join([f'"{name}"' for name in pk_columns])
                        pk_sql = f", PRIMARY KEY ({quoted_pk})"

                    create_sql = (
                        f'CREATE TABLE IF NOT EXISTS "{schema}"."{table_name}" '
                        f"({', '.join(column_defs)}{pk_sql})"
                    )
                    pg_cur.execute(create_sql)

                    if truncate_before_load:
                        pg_cur.execute(f'TRUNCATE TABLE "{schema}"."{table_name}"')

                    sqlite_rows = sqlite_conn.execute(f"SELECT * FROM {table_name}").fetchall()
                    if sqlite_rows:
                        placeholders = ", ".join(["%s"] * len(col_names))
                        quoted_columns = ", ".join([f'"{name}"' for name in col_names])
                        insert_sql = (
                            f'INSERT INTO "{schema}"."{table_name}" '
                            f"({quoted_columns}) "
                            f"VALUES ({placeholders})"
                        )
                        for sqlite_row in sqlite_rows:
                            values = [sqlite_row[name] for name in col_names]
                            pg_cur.execute(insert_sql, values)
                        row_count += len(sqlite_rows)

                    table_count += 1
            pg_conn.commit()

    return {"tables": table_count, "rows": row_count}


def sync_all_tenants_to_postgres() -> dict[str, int]:
    success = 0
    failed = 0
    admin_db = sqlite3.connect(ADMIN_DB_PATH)
    admin_db.row_factory = sqlite3.Row
    try:
        tenants = admin_db.execute(
            """
            SELECT id, username, shop_name, db_path, is_active
            FROM tenant_accounts
            WHERE is_active = 1
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        admin_db.close()

    for tenant in tenants:
        db_path = Path(str(tenant["db_path"] or "").strip())
        if not db_path.exists():
            failed += 1
            continue
        tenant_schema = f"{POSTGRES_SCHEMA}_t{int(tenant['id'])}"
        try:
            export_sqlite_to_postgres(db_path, tenant_schema, truncate_before_load=True)
            success += 1
        except Exception:
            failed += 1
    return {"success": success, "failed": failed}


def harden_all_tenant_indexes() -> dict[str, int]:
    success = 0
    failed = 0
    admin_db = sqlite3.connect(ADMIN_DB_PATH)
    admin_db.row_factory = sqlite3.Row
    try:
        tenants = admin_db.execute(
            """
            SELECT id, db_path
            FROM tenant_accounts
            WHERE is_active = 1
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        admin_db.close()

    for tenant in tenants:
        db_path = Path(str(tenant["db_path"] or "").strip())
        if not db_path.exists():
            failed += 1
            continue
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                ensure_tenant_index_hardening(conn)
                conn.commit()
            success += 1
        except Exception:
            failed += 1
    return {"success": success, "failed": failed}


def ensure_tenant_index_hardening(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_products_brand_model_storage_status
            ON products(brand, model, storage, status);
        CREATE INDEX IF NOT EXISTS idx_products_received_status
            ON products(received_date, status);
        CREATE INDEX IF NOT EXISTS idx_products_model_color
            ON products(model, color);
        CREATE INDEX IF NOT EXISTS idx_products_branch_status
            ON products(branch_id, status);
        CREATE INDEX IF NOT EXISTS idx_sales_sale_type_sold_at
            ON sales(sale_type, sold_at);
        CREATE INDEX IF NOT EXISTS idx_sales_branch_sold_at
            ON sales(branch_id, sold_at);
        CREATE INDEX IF NOT EXISTS idx_sales_invoice_no
            ON sales(invoice_no);
        CREATE INDEX IF NOT EXISTS idx_sales_product_active
            ON sales(product_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_returns_sale_date
            ON sale_returns(sale_id, return_date);
        CREATE INDEX IF NOT EXISTS idx_due_collections_date
            ON due_collections(collected_at);
        CREATE INDEX IF NOT EXISTS idx_expenses_branch_status_date
            ON expenses(branch_id, approval_status, expense_date);
        CREATE INDEX IF NOT EXISTS idx_model_catalog_lookup
            ON model_catalog(brand, model_name, storage, region, color, condition_state, is_active);
        """
    )


def ensure_admin_index_hardening(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_tenant_accounts_username_active
            ON tenant_accounts(username, is_active);
        CREATE INDEX IF NOT EXISTS idx_tenant_accounts_shop_name
            ON tenant_accounts(shop_name);
        CREATE INDEX IF NOT EXISTS idx_tenant_accounts_paid_until
            ON tenant_accounts(paid_until, is_active);
        CREATE INDEX IF NOT EXISTS idx_tenant_accounts_plan
            ON tenant_accounts(plan_code, is_active);
        CREATE INDEX IF NOT EXISTS idx_billing_paid_on
            ON billing_transactions(paid_on, tenant_id);
        CREATE INDEX IF NOT EXISTS idx_audit_tenant_created
            ON audit_logs(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_security_events_tenant
            ON security_events(tenant_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_integration_configs_tenant
            ON integration_configs(tenant_id, provider, is_active);
        """
    )


def process_queue_job(job: dict[str, object]) -> tuple[bool, str]:
    job_type = str(job.get("type") or "").strip().lower()
    payload = job.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    if job_type == "backup_create":
        sync_google = bool(payload.get("sync_google", False))
        create_database_backup(trigger_type="QUEUE", sync_google=sync_google, db_path=DB_PATH)
        return True, "backup_create completed"

    if job_type == "postgres_sync_all_tenants":
        result = sync_all_tenants_to_postgres()
        return True, f"postgres_sync_all_tenants success={result['success']} failed={result['failed']}"

    if job_type == "postgres_sync_main":
        export_sqlite_to_postgres(DB_PATH, POSTGRES_SCHEMA, truncate_before_load=True)
        return True, "postgres_sync_main completed"

    if job_type == "tenant_reindex_all":
        result = harden_all_tenant_indexes()
        return True, f"tenant_reindex_all success={result['success']} failed={result['failed']}"

    if job_type == "tenant_reindex":
        db_path = Path(str(payload.get("db_path") or "").strip())
        if not db_path.exists():
            return False, f"tenant db not found: {db_path}"
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_tenant_index_hardening(conn)
            conn.commit()
        return True, "tenant_reindex completed"

    return False, f"unknown job type: {job_type}"


def run_queue_worker(run_once: bool = False) -> int:
    if not REDIS_QUEUE_ENABLED:
        print("Queue is disabled. Set SOFTX_REDIS_QUEUE_ENABLED=1")
        return 1
    if get_redis_client() is None:
        print("Redis connection failed.")
        return 1

    print(f"Soft X worker listening on queue: {REDIS_QUEUE_NAME}")
    processed = 0
    while True:
        job = queue_pop_job(timeout_seconds=QUEUE_POLL_TIMEOUT_SECONDS)
        if job is None:
            if run_once:
                break
            continue
        try:
            ok, message = process_queue_job(job)
            processed += 1
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {'OK' if ok else 'ERR'}: {message}")
        except Exception as exc:
            processed += 1
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERR: {exc}")
        if run_once:
            break
    return 0


def row_value(row: sqlite3.Row | dict[str, object] | None, key: str, default: str = "") -> str:
    if row is None:
        return default
    value: object | None = None
    if isinstance(row, dict):
        value = row.get(key, default)
    else:
        try:
            value = row[key]  # type: ignore[index]
        except Exception:
            value = default
    if value is None:
        return default
    return str(value)


def build_business_profile(tenant_row: sqlite3.Row | dict[str, object] | None) -> dict[str, object]:
    primary_module = normalize_business_module(
        row_value(tenant_row, "primary_business", DEFAULT_PRIMARY_BUSINESS),
        default=DEFAULT_PRIMARY_BUSINESS,
    )
    enabled_modules = parse_enabled_modules(
        row_value(tenant_row, "enabled_modules", ""),
        fallback_primary=primary_module,
    )
    if primary_module not in enabled_modules:
        enabled_modules.insert(0, primary_module)

    module_def = BUSINESS_MODULE_DEFS.get(primary_module, BUSINESS_MODULE_DEFS[DEFAULT_PRIMARY_BUSINESS])
    tracking_mode = module_def["tracking_mode"]

    if tracking_mode == TRACKING_MODE_IMEI:
        tracking_label_en = "IMEI"
        tracking_label_bn = "আইএমইআই"
        tracking_placeholder = "357XXXXXXXXXXXX"
        tracking_max_length = 15
        bulk_sample = "357123456789012"
    elif tracking_mode == TRACKING_MODE_SERIAL:
        tracking_label_en = "Serial Number"
        tracking_label_bn = "সিরিয়াল নাম্বার"
        tracking_placeholder = "SN-A1B2C3D4"
        tracking_max_length = 40
        bulk_sample = "SN-A1B2C3D4"
    else:
        tracking_label_en = "Item Code"
        tracking_label_bn = "আইটেম কোড"
        tracking_placeholder = "SKU-10001"
        tracking_max_length = 40
        bulk_sample = "SKU-10001"

    module_badges: list[dict[str, str]] = []
    for module_key in enabled_modules:
        data = BUSINESS_MODULE_DEFS[module_key]
        module_badges.append(
            {
                "key": module_key,
                "label_en": data["label_en"],
                "label_bn": data["label_bn"],
            }
        )

    return {
        "primary_module": primary_module,
        "primary_label_en": module_def["label_en"],
        "primary_label_bn": module_def["label_bn"],
        "enabled_modules": enabled_modules,
        "enabled_modules_csv": ",".join(enabled_modules),
        "enabled_labels_en": [BUSINESS_MODULE_DEFS[item]["label_en"] for item in enabled_modules],
        "enabled_labels_bn": [BUSINESS_MODULE_DEFS[item]["label_bn"] for item in enabled_modules],
        "module_badges": module_badges,
        "tracking_mode": tracking_mode,
        "tracking_label_en": tracking_label_en,
        "tracking_label_bn": tracking_label_bn,
        "tracking_placeholder": tracking_placeholder,
        "tracking_max_length": tracking_max_length,
        "bulk_sample": bulk_sample,
    }


def account_matches_module_profile(
    account_row: sqlite3.Row | dict[str, object] | None,
    module_profile: str,
) -> bool:
    profile_key = normalize_module_profile(module_profile, default=MODULE_PROFILE_ALL)
    if profile_key == MODULE_PROFILE_ALL:
        return True
    business_profile = build_business_profile(account_row)
    account_profile = module_profile_from_primary_business(str(business_profile.get("primary_module") or ""))
    return account_profile == profile_key


def parse_int_with_default(value: object, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def get_tenant_plan_limits(tenant_row: sqlite3.Row | dict[str, object] | None) -> dict[str, int | str]:
    raw_plan = row_value(tenant_row, "plan_code", "GROWTH").strip().upper() or "GROWTH"
    if raw_plan not in PLAN_LIMIT_PRESETS:
        raw_plan = "GROWTH"
    preset = PLAN_LIMIT_PRESETS[raw_plan]

    return {
        "plan_code": raw_plan,
        "max_branches": parse_int_with_default(row_value(tenant_row, "max_branches", ""), int(preset["max_branches"])),
        "max_users": parse_int_with_default(row_value(tenant_row, "max_users", ""), int(preset["max_users"])),
        "max_products": parse_int_with_default(row_value(tenant_row, "max_products", ""), int(preset["max_products"])),
        "max_monthly_orders": parse_int_with_default(
            row_value(tenant_row, "max_monthly_orders", ""),
            int(preset["max_monthly_orders"]),
        ),
    }


def get_tenant_usage_snapshot(db: sqlite3.Connection) -> dict[str, int]:
    month_prefix = date.today().isoformat()[:7]
    return {
        "branches": int(db.execute("SELECT COUNT(*) AS c FROM branches").fetchone()["c"] or 0),
        "users": int(db.execute("SELECT COUNT(*) AS c FROM users WHERE is_active = 1").fetchone()["c"] or 0),
        "products": int(db.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"] or 0),
        "monthly_orders": int(
            db.execute(
                """
                SELECT COUNT(*) AS c
                FROM sales
                WHERE is_active = 1
                  AND substr(sold_at, 1, 7) = ?
                """,
                (month_prefix,),
            ).fetchone()["c"]
            or 0
        ),
    }


def check_tenant_plan_limit(
    db: sqlite3.Connection,
    tenant_row: sqlite3.Row | dict[str, object] | None,
    limit_key: str,
    incoming_count: int = 1,
) -> tuple[bool, str, dict[str, int | str]]:
    limits = get_tenant_plan_limits(tenant_row)
    usage = get_tenant_usage_snapshot(db)

    mapping = {
        "max_branches": "branches",
        "max_users": "users",
        "max_products": "products",
        "max_monthly_orders": "monthly_orders",
    }
    usage_key = mapping.get(limit_key)
    if not usage_key:
        return False, "", limits

    limit_value = parse_int_with_default(limits.get(limit_key, -1), -1)
    if limit_value < 0:
        return False, "", limits

    current_usage = int(usage.get(usage_key, 0))
    projected_usage = current_usage + max(0, int(incoming_count))
    if projected_usage <= limit_value:
        return False, "", limits

    readable = {
        "max_branches": "branch",
        "max_users": "active user",
        "max_products": "product",
        "max_monthly_orders": "monthly order",
    }
    label = readable.get(limit_key, "resource")
    message = (
        f"Plan limit reached for {label}: "
        f"{current_usage}/{limit_value}. Upgrade plan or update tenant limits."
    )
    return True, message, limits


def resolve_ui_language(tenant_row: sqlite3.Row | dict[str, object] | None = None) -> str:
    session_lang = normalize_language(session.get("ui_lang")) if has_request_context() else None
    if session_lang:
        return session_lang

    if tenant_row is None and has_request_context():
        tenant_row = get_current_tenant()
    tenant_lang = normalize_language(row_value(tenant_row, "ui_language", ""))
    if tenant_lang:
        return tenant_lang
    return DEFAULT_UI_LANGUAGE


def pick_text(en_text: str, bn_text: str, language: str) -> str:
    return bn_text if language == "bn" else en_text


def get_current_tracking_mode() -> str:
    tenant = get_current_tenant() if has_request_context() else None
    profile = build_business_profile(tenant)
    return str(profile["tracking_mode"])


def get_tracking_label(language: str | None = None) -> str:
    tenant = get_current_tenant() if has_request_context() else None
    profile = build_business_profile(tenant)
    active_language = language or resolve_ui_language(tenant)
    return (
        str(profile["tracking_label_bn"])
        if active_language == "bn"
        else str(profile["tracking_label_en"])
    )


def tenant_db_path_for_username(username: str) -> Path:
    safe_name = slugify_text(username)
    if not safe_name:
        safe_name = "shop"

    candidate_dirs = [TENANT_DATA_DIR, BASE_DIR / "tenants"]
    for directory in candidate_dirs:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            return directory / f"{safe_name}.db"
        except OSError:
            continue

    return (BASE_DIR / f"{safe_name}.db").resolve()


def looks_like_invalid_tenant_db_path(raw_path: str) -> bool:
    text = (raw_path or "").strip()
    if not text:
        return True
    normalized = text.replace("\\", "/")
    lowered = normalized.lower()
    if lowered.startswith("/users/"):
        return True
    if re.match(r"^[a-z]:/", lowered):
        return True
    if "/documents/inventory" in lowered:
        return True
    if normalized.startswith("file://"):
        return True
    if not lowered.endswith(".db"):
        return True
    return False


def ensure_tenant_db_ready(
    admin_db: sqlite3.Connection,
    account_row: sqlite3.Row | dict[str, object],
) -> Path | None:
    primary_path = resolve_tenant_db_path_for_account(admin_db, account_row)
    fallback_username = normalize_username(row_value(account_row, "username", ""))
    if not fallback_username:
        fallback_username = slugify_text(row_value(account_row, "shop_name", "")) or "shop"
    fallback_path = tenant_db_path_for_username(fallback_username)

    candidate_paths: list[Path] = [primary_path]
    if str(primary_path) != str(fallback_path):
        candidate_paths.append(fallback_path)

    account_id = row_value(account_row, "id", 0)
    for candidate in candidate_paths:
        try:
            init_db_for_path(candidate)
            if account_id and str(candidate) != str(row_value(account_row, "db_path", "")).strip():
                try:
                    admin_db.execute(
                        "UPDATE tenant_accounts SET db_path = ? WHERE id = ?",
                        (str(candidate), int(account_id)),
                    )
                    admin_db.commit()
                except sqlite3.Error:
                    pass
            return candidate
        except (OSError, sqlite3.Error):
            continue

    return None


def resolve_tenant_db_path_for_account(
    admin_db: sqlite3.Connection,
    account_row: sqlite3.Row | dict[str, object],
) -> Path:
    username = normalize_username(row_value(account_row, "username", ""))
    fallback_username = username or slugify_text(row_value(account_row, "shop_name", "")) or "shop"
    expected_path = tenant_db_path_for_username(fallback_username)
    raw_db_path = row_value(account_row, "db_path", "")

    if looks_like_invalid_tenant_db_path(raw_db_path):
        target = expected_path
    else:
        target = Path(str(raw_db_path).strip())

    account_id = row_value(account_row, "id", 0)
    if str(target) != str(raw_db_path).strip() and account_id:
        try:
            admin_db.execute(
                "UPDATE tenant_accounts SET db_path = ? WHERE id = ?",
                (str(target), int(account_id)),
            )
            admin_db.commit()
        except sqlite3.Error:
            pass
    return target


def get_superadmin_credentials() -> tuple[str, str]:
    return SUPERADMIN_USER, SUPERADMIN_PASS


def make_password_hash(password: str) -> str:
    return generate_password_hash(password, method="pbkdf2:sha256")


def password_matches(stored_hash: str, plain_password: str) -> bool:
    clean_hash = str(stored_hash or "")
    if not clean_hash:
        return False
    try:
        return check_password_hash(clean_hash, plain_password)
    except (ValueError, TypeError):
        # Legacy compatibility: allow old plain-text stored passwords.
        return clean_hash == plain_password


def is_superadmin_logged_in() -> bool:
    return bool(session.get("is_superadmin"))


def safe_next_path(default_endpoint: str = "dashboard") -> str:
    next_url = request.values.get("next", "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for(default_endpoint)


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


def now_sqlite_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_request_ip() -> str:
    if not has_request_context():
        return ""
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "")


def get_request_user_agent() -> str:
    if not has_request_context():
        return ""
    return (request.headers.get("User-Agent", "") or "")[:320]


def add_months(source_date: date, months: int) -> date:
    month_index = source_date.month - 1 + months
    year = source_date.year + month_index // 12
    month = month_index % 12 + 1
    max_day = calendar.monthrange(year, month)[1]
    day = min(source_date.day, max_day)
    return date(year, month, day)


def calculate_coverage_end(start_date: date, months: int) -> date:
    next_anchor = add_months(start_date, months)
    return next_anchor - timedelta(days=1)


def is_tenant_subscription_active(tenant: sqlite3.Row | None) -> bool:
    if tenant is None:
        return False
    paid_until = parse_iso_date(tenant["paid_until"] if "paid_until" in tenant.keys() else None)
    if paid_until is None:
        return True
    return paid_until >= date.today()


def get_tenant_subscription_days_left(tenant: sqlite3.Row | None) -> int | None:
    if tenant is None:
        return None
    paid_until = parse_iso_date(tenant["paid_until"] if "paid_until" in tenant.keys() else None)
    if paid_until is None:
        return None
    return (paid_until - date.today()).days


def billing_status_for_paid_until(paid_until_raw: str | None) -> tuple[str, int | None]:
    paid_until = parse_iso_date(paid_until_raw)
    if paid_until is None:
        return "NO_LIMIT", None
    days_left = (paid_until - date.today()).days
    if days_left < 0:
        return "EXPIRED", days_left
    if days_left <= 5:
        return "DUE_SOON", days_left
    return "ACTIVE", days_left


def ensure_tenant_users_table(conn: sqlite3.Connection) -> None:
    users_sql_row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'users'
        """
    ).fetchone()
    users_sql = str(users_sql_row["sql"] or "").upper() if users_sql_row is not None else ""
    requires_role_migration = (
        users_sql_row is not None
        and ("'USER'" not in users_sql or "'MANAGER'" in users_sql or "'CASHIER'" in users_sql)
    )
    if requires_role_migration:
        conn.execute("ALTER TABLE users RENAME TO users_legacy_role")
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                full_name TEXT,
                role TEXT NOT NULL DEFAULT 'USER'
                    CHECK(role IN ('ADMIN', 'USER')),
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
            )
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                id, username, full_name, role, password_hash, is_active, created_at
            )
            SELECT
                id,
                username,
                full_name,
                CASE
                    WHEN UPPER(COALESCE(role, '')) = 'ADMIN' THEN 'ADMIN'
                    ELSE 'USER'
                END AS role,
                password_hash,
                CASE WHEN is_active = 1 THEN 1 ELSE 0 END AS is_active,
                COALESCE(created_at, DATETIME('now'))
            FROM users_legacy_role
            """
        )
        conn.execute("DROP TABLE users_legacy_role")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            full_name TEXT,
            role TEXT NOT NULL DEFAULT 'USER'
                CHECK(role IN ('ADMIN', 'USER')),
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role_active ON users(role, is_active)")


def create_or_update_tenant_user(
    db_path: Path,
    username: str,
    full_name: str,
    role: str,
    password: str,
    is_active: bool = True,
) -> int:
    uname = normalize_username(username)
    if not uname:
        raise ValueError("User username is required.")
    clean_role = normalize_role(role, default="USER")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        ensure_tenant_users_table(conn)
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (uname,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO users (username, full_name, role, password_hash, is_active)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uname,
                    full_name.strip() if full_name else "",
                    clean_role,
                    make_password_hash(password),
                    1 if is_active else 0,
                ),
            )
            user_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            user_id = int(existing["id"])
            conn.execute(
                """
                UPDATE users
                SET full_name = ?,
                    role = ?,
                    password_hash = ?,
                    is_active = ?
                WHERE id = ?
                """,
                (
                    full_name.strip() if full_name else "",
                    clean_role,
                    make_password_hash(password),
                    1 if is_active else 0,
                    user_id,
                ),
            )
        conn.commit()
        return user_id


def user_role_can_access(role: str, endpoint: str) -> bool:
    clean_role = normalize_role(role, default="USER")
    if clean_role == "ADMIN":
        return True
    return endpoint in USER_ALLOWED_ENDPOINTS


def build_role_access(role: str | None) -> dict[str, bool]:
    role_name = normalize_role(role or "", default="USER") if role else ""

    def can(endpoint_name: str) -> bool:
        if not role_name:
            return False
        return user_role_can_access(role_name, endpoint_name)

    return {
        "dashboard": can("dashboard"),
        "products": can("products"),
        "model_catalog": can("model_catalog"),
        "stock_report": can("stock_report"),
        "sales": can("sales"),
        "returns": can("returns"),
        "customers": can("customers"),
        "retail_customers": can("retail_customers"),
        "suppliers": can("suppliers"),
        "expenses": can("expenses"),
        "money_center": can("money_center"),
        "reports": can("reports"),
        "daily_report": can("daily_report"),
        "backups": can("backups"),
        "imei_lookup": can("imei_lookup"),
        "account_password": can("account_password"),
        "edit_tools": role_name == "ADMIN",
        "team_users": role_name == "ADMIN",
        "shop_settings": role_name == "ADMIN",
    }


def can_view_receiver_photo() -> bool:
    if has_request_context() and is_superadmin_logged_in():
        return True
    if not has_request_context():
        return False
    tenant_user = get_current_tenant_user()
    if tenant_user is None:
        return False
    return normalize_role(str(tenant_user["role"]), default="USER") == "ADMIN"


def is_tenant_admin(user_row: sqlite3.Row | None = None) -> bool:
    active_user = user_row or get_current_tenant_user()
    if active_user is None:
        return False
    return normalize_role(str(active_user["role"]), default="USER") == "ADMIN"


def init_admin_db() -> None:
    ADMIN_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ADMIN_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_name TEXT NOT NULL,
                owner_name TEXT,
                phone TEXT,
                email TEXT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                db_path TEXT NOT NULL UNIQUE,
                ui_language TEXT NOT NULL DEFAULT 'bn',
                primary_business TEXT NOT NULL DEFAULT 'MOBILE_WHOLESALE',
                enabled_modules TEXT NOT NULL DEFAULT 'MOBILE_WHOLESALE',
                billing_cycle TEXT NOT NULL DEFAULT 'MONTHLY',
                monthly_fee REAL NOT NULL DEFAULT 0,
                paid_until TEXT,
                billing_note TEXT,
                profile_image_path TEXT,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
            )
            """
        )
        existing_columns = get_table_columns(conn, "tenant_accounts")
        if "billing_cycle" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN billing_cycle TEXT NOT NULL DEFAULT 'MONTHLY'")
        if "email" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN email TEXT")
        if "monthly_fee" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN monthly_fee REAL NOT NULL DEFAULT 0")
        if "paid_until" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN paid_until TEXT")
        if "billing_note" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN billing_note TEXT")
        if "profile_image_path" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN profile_image_path TEXT")
        if "ui_language" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN ui_language TEXT NOT NULL DEFAULT 'bn'")
        if "primary_business" not in existing_columns:
            conn.execute(
                "ALTER TABLE tenant_accounts ADD COLUMN primary_business TEXT NOT NULL DEFAULT 'MOBILE_WHOLESALE'"
            )
        if "enabled_modules" not in existing_columns:
            conn.execute(
                "ALTER TABLE tenant_accounts ADD COLUMN enabled_modules TEXT NOT NULL DEFAULT 'MOBILE_WHOLESALE'"
            )
        if "plan_code" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN plan_code TEXT NOT NULL DEFAULT 'GROWTH'")
        if "max_branches" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN max_branches INTEGER NOT NULL DEFAULT -1")
        if "max_users" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN max_users INTEGER NOT NULL DEFAULT -1")
        if "max_products" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN max_products INTEGER NOT NULL DEFAULT -1")
        if "max_monthly_orders" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN max_monthly_orders INTEGER NOT NULL DEFAULT -1")
        if "security_mfa_required" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN security_mfa_required INTEGER NOT NULL DEFAULT 0")
        if "module_overrides_json" not in existing_columns:
            conn.execute("ALTER TABLE tenant_accounts ADD COLUMN module_overrides_json TEXT")

        conn.execute(
            """
            UPDATE tenant_accounts
            SET ui_language = 'bn'
            WHERE ui_language IS NULL OR TRIM(ui_language) = ''
            """
        )
        conn.execute(
            """
            UPDATE tenant_accounts
            SET primary_business = 'MOBILE_WHOLESALE'
            WHERE primary_business IS NULL OR TRIM(primary_business) = ''
            """
        )
        conn.execute(
            """
            UPDATE tenant_accounts
            SET enabled_modules = 'MOBILE_WHOLESALE'
            WHERE enabled_modules IS NULL OR TRIM(enabled_modules) = ''
            """
        )
        conn.execute(
            """
            UPDATE tenant_accounts
            SET plan_code = 'GROWTH'
            WHERE plan_code IS NULL OR TRIM(plan_code) = ''
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS billing_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                paid_on TEXT NOT NULL,
                period_months INTEGER NOT NULL,
                amount REAL NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                FOREIGN KEY(tenant_id) REFERENCES tenant_accounts(id) ON DELETE CASCADE
            )
            """
        )
        billing_columns = get_table_columns(conn, "billing_transactions")
        if "source" not in billing_columns:
            conn.execute("ALTER TABLE billing_transactions ADD COLUMN source TEXT NOT NULL DEFAULT 'MANUAL'")
        if "tx_ref" not in billing_columns:
            conn.execute("ALTER TABLE billing_transactions ADD COLUMN tx_ref TEXT")
        if "gateway" not in billing_columns:
            conn.execute("ALTER TABLE billing_transactions ADD COLUMN gateway TEXT")
        conn.execute(
            """
            UPDATE billing_transactions
            SET source = 'MANUAL'
            WHERE source IS NULL OR TRIM(source) = ''
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_scope TEXT NOT NULL,
                identifier TEXT NOT NULL,
                fail_count INTEGER NOT NULL DEFAULT 0,
                blocked_until TEXT,
                last_attempt_at TEXT,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                UNIQUE(account_scope, identifier)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_login_attempts_scope
            ON login_attempts(account_scope, identifier)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER,
                actor_type TEXT NOT NULL,
                actor_username TEXT,
                actor_role TEXT,
                action TEXT NOT NULL,
                endpoint TEXT,
                ip_address TEXT,
                user_agent TEXT,
                metadata TEXT,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                FOREIGN KEY(tenant_id) REFERENCES tenant_accounts(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_logs_recent
            ON audit_logs(id DESC, tenant_id)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS billing_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                reminder_date TEXT NOT NULL,
                reminder_type TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                sent_at TEXT,
                webhook_response TEXT,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                UNIQUE(tenant_id, reminder_date, reminder_type),
                FOREIGN KEY(tenant_id) REFERENCES tenant_accounts(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_billing_reminders_recent
            ON billing_reminders(id DESC, tenant_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_billing_tenant_id
            ON billing_transactions(tenant_id, id DESC)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER,
                severity TEXT NOT NULL DEFAULT 'LOW',
                event_type TEXT NOT NULL,
                event_source TEXT,
                actor_username TEXT,
                metadata TEXT,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                FOREIGN KEY(tenant_id) REFERENCES tenant_accounts(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_security_events_recent
            ON security_events(tenant_id, id DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS integration_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                config_json TEXT,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                UNIQUE(tenant_id, provider),
                FOREIGN KEY(tenant_id) REFERENCES tenant_accounts(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_plan_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                old_plan_code TEXT,
                new_plan_code TEXT NOT NULL,
                note TEXT,
                actor_username TEXT,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                FOREIGN KEY(tenant_id) REFERENCES tenant_accounts(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tenant_plan_events_recent
            ON tenant_plan_events(tenant_id, id DESC)
            """
        )
        ensure_admin_index_hardening(conn)
        conn.commit()


def get_admin_db() -> sqlite3.Connection:
    if "admin_db" not in g:
        g.admin_db = sqlite3.connect(ADMIN_DB_PATH)
        g.admin_db.row_factory = sqlite3.Row
    return g.admin_db


def write_audit_log(
    action: str,
    metadata: dict[str, object] | None = None,
    tenant_id: int | None = None,
    actor_type: str | None = None,
    actor_username: str | None = None,
    actor_role: str | None = None,
) -> None:
    if not action:
        return

    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    endpoint_name = request.endpoint if has_request_context() else ""
    ip_address = get_request_ip()
    user_agent = get_request_user_agent()

    if actor_type is None:
        if has_request_context() and is_superadmin_logged_in():
            actor_type = "SUPERADMIN"
            actor_username = actor_username or SUPERADMIN_USER
            actor_role = actor_role or "SUPERADMIN"
        elif has_request_context():
            tenant_user = get_current_tenant_user()
            tenant = get_current_tenant()
            if tenant_user is not None:
                actor_type = "TENANT_USER"
                actor_username = actor_username or str(tenant_user["username"])
                actor_role = actor_role or str(tenant_user["role"])
                if tenant_id is None and tenant is not None:
                    tenant_id = int(tenant["id"])
            else:
                actor_type = "SYSTEM"
        else:
            actor_type = "SYSTEM"

    conn: sqlite3.Connection | None = None
    should_close = False
    try:
        if has_request_context():
            conn = get_admin_db()
        else:
            conn = sqlite3.connect(ADMIN_DB_PATH)
            conn.row_factory = sqlite3.Row
            should_close = True

        conn.execute(
            """
            INSERT INTO audit_logs (
                tenant_id, actor_type, actor_username, actor_role,
                action, endpoint, ip_address, user_agent, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                actor_type or "SYSTEM",
                (actor_username or "")[:100],
                (actor_role or "")[:50],
                action[:120],
                (endpoint_name or "")[:120],
                (ip_address or "")[:80],
                (user_agent or "")[:320],
                meta_json,
            ),
        )
        conn.commit()
    except Exception:
        # Audit log should never break business flow.
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if should_close and conn is not None:
            conn.close()


def check_login_blocked(scope: str, identifier: str) -> tuple[bool, int]:
    if not LOGIN_RATE_LIMIT_ENABLED:
        return False, 0

    key = (identifier or "").strip().lower()
    if not key:
        return False, 0

    db = get_admin_db()
    row = db.execute(
        """
        SELECT fail_count, blocked_until
        FROM login_attempts
        WHERE account_scope = ? AND identifier = ?
        """,
        (scope, key),
    ).fetchone()
    if row is None:
        return False, 0

    blocked_until = parse_datetime(row["blocked_until"])
    if blocked_until is None:
        return False, 0

    now = datetime.now()
    if blocked_until <= now:
        db.execute(
            """
            UPDATE login_attempts
            SET fail_count = 0,
                blocked_until = NULL,
                updated_at = ?
            WHERE account_scope = ? AND identifier = ?
            """,
            (now_sqlite_text(), scope, key),
        )
        db.commit()
        return False, 0

    seconds_left = int((blocked_until - now).total_seconds())
    return True, max(1, seconds_left)


def register_login_failure(scope: str, identifier: str) -> tuple[bool, int]:
    if not LOGIN_RATE_LIMIT_ENABLED:
        return False, 0

    key = (identifier or "").strip().lower()
    if not key:
        return False, 0

    db = get_admin_db()
    now_text = now_sqlite_text()
    row = db.execute(
        """
        SELECT fail_count
        FROM login_attempts
        WHERE account_scope = ? AND identifier = ?
        """,
        (scope, key),
    ).fetchone()

    if row is None:
        fail_count = 1
        db.execute(
            """
            INSERT INTO login_attempts (
                account_scope, identifier, fail_count, last_attempt_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (scope, key, fail_count, now_text, now_text),
        )
    else:
        fail_count = int(row["fail_count"] or 0) + 1
        db.execute(
            """
            UPDATE login_attempts
            SET fail_count = ?,
                last_attempt_at = ?,
                updated_at = ?
            WHERE account_scope = ? AND identifier = ?
            """,
            (fail_count, now_text, now_text, scope, key),
        )

    blocked = fail_count >= MAX_LOGIN_ATTEMPTS
    seconds_left = 0
    if blocked:
        blocked_until = datetime.now() + timedelta(minutes=LOGIN_BLOCK_MINUTES)
        blocked_text = blocked_until.strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            """
            UPDATE login_attempts
            SET blocked_until = ?,
                updated_at = ?
            WHERE account_scope = ? AND identifier = ?
            """,
            (blocked_text, now_text, scope, key),
        )
        seconds_left = int((blocked_until - datetime.now()).total_seconds())

    db.commit()
    return blocked, max(0, seconds_left)


def clear_login_failures(scope: str, identifier: str) -> None:
    if not LOGIN_RATE_LIMIT_ENABLED:
        return

    key = (identifier or "").strip().lower()
    if not key:
        return
    db = get_admin_db()
    db.execute(
        """
        UPDATE login_attempts
        SET fail_count = 0,
            blocked_until = NULL,
            updated_at = ?
        WHERE account_scope = ? AND identifier = ?
        """,
        (now_sqlite_text(), scope, key),
    )
    db.commit()


def collect_subscription_payment(
    admin_db: sqlite3.Connection,
    account: sqlite3.Row,
    months: int,
    amount: float | None,
    note: str,
    source: str,
    tx_ref: str = "",
    gateway: str = "",
    paid_on: date | None = None,
) -> dict[str, object]:
    month_count = max(1, min(24, int(months)))
    payment_date = paid_on or date.today()
    paid_until_date = parse_iso_date(account["paid_until"])

    if paid_until_date is None or paid_until_date < payment_date:
        period_start = payment_date
    else:
        period_start = paid_until_date + timedelta(days=1)

    period_end = calculate_coverage_end(period_start, month_count)
    new_paid_until = period_end.isoformat()

    if amount is None:
        base_fee = float(account["monthly_fee"] or 0)
        total_amount = round(base_fee * month_count, 2)
    else:
        total_amount = round(float(amount), 2)

    admin_db.execute(
        """
        UPDATE tenant_accounts
        SET paid_until = ?,
            is_active = 1
        WHERE id = ?
        """,
        (new_paid_until, int(account["id"])),
    )
    admin_db.execute(
        """
        INSERT INTO billing_transactions (
            tenant_id, paid_on, period_months, amount, period_start, period_end, note, source, tx_ref, gateway
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(account["id"]),
            payment_date.isoformat(),
            month_count,
            total_amount,
            period_start.isoformat(),
            new_paid_until,
            note,
            source[:40],
            (tx_ref or "")[:140],
            (gateway or "")[:80],
        ),
    )
    return {
        "new_paid_until": new_paid_until,
        "period_start": period_start.isoformat(),
        "period_end": new_paid_until,
        "amount": total_amount,
        "months": month_count,
    }


def post_json_webhook(url: str, payload: dict[str, object]) -> tuple[bool, str]:
    if not url:
        return False, "Webhook URL is empty."
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=NOTIFY_WEBHOOK_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            return 200 <= int(response.status) < 300, raw[:400]
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        return False, f"HTTP {exc.code}: {raw[:300]}"
    except Exception as exc:
        return False, str(exc)


def _rewrite_proxy_header_value(raw_value: str) -> str:
    if not raw_value:
        return raw_value
    current_origin = request.host_url.rstrip("/")
    return (
        raw_value
        .replace("https://app.corexbd.com", current_origin)
        .replace("http://app.corexbd.com", current_origin)
        .replace("app.corexbd.com", request.host)
    )


def proxy_pocket_legacy_request(proxy_path: str):
    safe_path = proxy_path.lstrip("/")
    query_string = request.query_string.decode("utf-8", errors="ignore")
    upstream_url = f"{POCKET_LEGACY_BASE_URL}/{safe_path}"
    if query_string:
        upstream_url = f"{upstream_url}?{query_string}"

    body = request.get_data() if request.method not in {"GET", "HEAD"} else None
    forwarded_headers: dict[str, str] = {
        "User-Agent": request.headers.get("User-Agent", "PocketProProxy/1.0"),
        "Accept": request.headers.get("Accept", "*/*"),
    }
    for header_name in ("Content-Type", "Cookie", "Authorization"):
        header_value = request.headers.get(header_name, "").strip()
        if header_value:
            forwarded_headers[header_name] = header_value
    forwarded_headers["X-Forwarded-Host"] = request.host
    forwarded_headers["X-Forwarded-Proto"] = request.scheme

    upstream_request = urllib.request.Request(
        url=upstream_url,
        data=body,
        method=request.method,
        headers=forwarded_headers,
    )

    try:
        upstream_response = urllib.request.urlopen(upstream_request, timeout=30)
        response_body = upstream_response.read()
        status_code = int(upstream_response.status)
        upstream_headers = upstream_response.headers
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        status_code = int(exc.code)
        upstream_headers = exc.headers
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "message": f"Pocket legacy upstream unavailable: {exc}",
            }
        ), 502

    response = make_response(response_body, status_code)
    skipped_headers = {"content-length", "transfer-encoding", "content-encoding", "connection"}

    for header_name, header_value in upstream_headers.items():
        lowered_name = header_name.lower()
        if lowered_name in skipped_headers:
            continue
        if lowered_name == "set-cookie":
            continue
        response.headers[header_name] = _rewrite_proxy_header_value(header_value)

    for cookie_value in upstream_headers.get_all("Set-Cookie") or []:
        response.headers.add("Set-Cookie", _rewrite_proxy_header_value(cookie_value))

    return response


def run_subscription_automation(send_notifications: bool = True) -> dict[str, int]:
    admin_db = get_admin_db()
    today_iso = date.today().isoformat()

    accounts = admin_db.execute(
        """
        SELECT id, shop_name, username, owner_name, phone, paid_until, monthly_fee, is_active
        FROM tenant_accounts
        WHERE is_active = 1
        ORDER BY id DESC
        """
    ).fetchall()

    reminders_created = 0
    notifications_sent = 0
    notifications_failed = 0

    for account in accounts:
        status, days_left = billing_status_for_paid_until(account["paid_until"])
        reminder_type = ""
        if status == "DUE_SOON":
            reminder_type = "DUE_SOON"
            message = (
                f"Dear {account['shop_name']}, your subscription will expire in {days_left} day(s). "
                "Please pay monthly service bill to avoid interruption."
            )
        elif status == "EXPIRED":
            reminder_type = "EXPIRED"
            message = (
                f"Dear {account['shop_name']}, your subscription is expired. "
                "Please pay monthly service bill to reactivate service."
            )
        else:
            continue

        inserted = admin_db.execute(
            """
            INSERT OR IGNORE INTO billing_reminders (
                tenant_id, reminder_date, reminder_type, message, status
            )
            VALUES (?, ?, ?, ?, 'PENDING')
            """,
            (int(account["id"]), today_iso, reminder_type, message),
        ).rowcount
        if inserted:
            reminders_created += 1

        if send_notifications and BILLING_NOTIFY_WEBHOOK and inserted:
            payload = {
                "event": "SUBSCRIPTION_REMINDER",
                "reminder_type": reminder_type,
                "shop_name": account["shop_name"],
                "shop_username": account["username"],
                "phone": account["phone"],
                "owner_name": account["owner_name"],
                "paid_until": account["paid_until"],
                "days_left": days_left,
                "message": message,
            }
            ok, response_message = post_json_webhook(BILLING_NOTIFY_WEBHOOK, payload)
            admin_db.execute(
                """
                UPDATE billing_reminders
                SET status = ?,
                    sent_at = ?,
                    webhook_response = ?
                WHERE tenant_id = ? AND reminder_date = ? AND reminder_type = ?
                """,
                (
                    "SENT" if ok else "FAILED",
                    now_sqlite_text(),
                    response_message[:400],
                    int(account["id"]),
                    today_iso,
                    reminder_type,
                ),
            )
            if ok:
                notifications_sent += 1
            else:
                notifications_failed += 1

    admin_db.commit()
    return {
        "accounts_checked": len(accounts),
        "reminders_created": reminders_created,
        "notifications_sent": notifications_sent,
        "notifications_failed": notifications_failed,
    }


def get_current_tenant() -> sqlite3.Row | None:
    if "current_tenant" in g:
        return g.current_tenant

    tenant_id = session.get("tenant_id")
    if not tenant_id:
        g.current_tenant = None
        return None

    row = get_admin_db().execute(
        """
        SELECT
            id, shop_name, owner_name, phone, username, db_path,
            ui_language, primary_business, enabled_modules,
            billing_cycle, monthly_fee, paid_until, billing_note,
            plan_code, max_branches, max_users, max_products, max_monthly_orders,
            security_mfa_required, module_overrides_json, profile_image_path, is_active
        FROM tenant_accounts
        WHERE id = ?
        """,
        (tenant_id,),
    ).fetchone()

    if row is None or int(row["is_active"]) != 1:
        session.pop("tenant_id", None)
        g.current_tenant = None
        return None

    g.current_tenant = row
    return row


def get_current_tenant_user() -> sqlite3.Row | None:
    if "current_tenant_user" in g:
        return g.current_tenant_user

    tenant = get_current_tenant()
    if tenant is None:
        g.current_tenant_user = None
        return None

    user_id = session.get("tenant_user_id")
    if not user_id:
        g.current_tenant_user = None
        return None

    db = get_db()
    ensure_tenant_users_table(db)
    user = db.execute(
        """
        SELECT id, username, full_name, role, is_active
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()

    if user is None or int(user["is_active"]) != 1:
        session.pop("tenant_user_id", None)
        g.current_tenant_user = None
        return None

    g.current_tenant_user = user
    return user


def get_current_db_path() -> Path:
    if has_request_context():
        tenant = get_current_tenant()
        if tenant is not None:
            return resolve_tenant_db_path_for_account(get_admin_db(), tenant)
    return DB_PATH


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def get_or_create_supplier(conn: sqlite3.Connection, name: str) -> int:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Supplier name is required.")

    conn.execute("INSERT OR IGNORE INTO suppliers (name) VALUES (?)", (clean_name,))
    row = conn.execute("SELECT id FROM suppliers WHERE name = ?", (clean_name,)).fetchone()
    return int(row["id"])


def get_or_create_customer(conn: sqlite3.Connection, shop_name: str, phone: str = "") -> int:
    clean_name = shop_name.strip()
    if not clean_name:
        raise ValueError("Customer shop name is required.")

    conn.execute(
        "INSERT OR IGNORE INTO customers (shop_name, phone) VALUES (?, ?)",
        (clean_name, phone.strip()),
    )
    row = conn.execute("SELECT id FROM customers WHERE shop_name = ?", (clean_name,)).fetchone()
    return int(row["id"])


def get_or_create_retail_customer(
    conn: sqlite3.Connection,
    full_name: str,
    phone: str = "",
    address: str = "",
    region: str = "",
    note: str = "",
) -> int:
    clean_name = (full_name or "").strip()
    if not clean_name:
        raise ValueError("Retail customer name is required.")

    clean_phone = (phone or "").strip()
    clean_address = (address or "").strip()
    clean_region = (region or "").strip()
    clean_note = (note or "").strip()

    match_row: sqlite3.Row | None = None
    if clean_phone:
        match_row = conn.execute(
            """
            SELECT id, phone, address, region, note
            FROM retail_customers
            WHERE LOWER(TRIM(full_name)) = ? AND TRIM(phone) = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (clean_name.lower(), clean_phone),
        ).fetchone()

    if match_row is None:
        match_row = conn.execute(
            """
            SELECT id, phone, address, region, note
            FROM retail_customers
            WHERE LOWER(TRIM(full_name)) = ?
            ORDER BY
                CASE WHEN TRIM(COALESCE(phone, '')) = '' THEN 1 ELSE 0 END,
                id ASC
            LIMIT 1
            """,
            (clean_name.lower(),),
        ).fetchone()

    if match_row is None:
        conn.execute(
            """
            INSERT INTO retail_customers (full_name, phone, address, region, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean_name, clean_phone, clean_address, clean_region, clean_note),
        )
        row = conn.execute("SELECT last_insert_rowid()").fetchone()
        return int(row[0])

    retail_customer_id = int(match_row["id"])
    update_parts: list[str] = []
    update_values: list[str] = []

    # Keep existing customer record enriched with any newly supplied details.
    if clean_phone and not (str(match_row["phone"] or "").strip()):
        update_parts.append("phone = ?")
        update_values.append(clean_phone)
    if clean_address and not (str(match_row["address"] or "").strip()):
        update_parts.append("address = ?")
        update_values.append(clean_address)
    if clean_region and not (str(match_row["region"] or "").strip()):
        update_parts.append("region = ?")
        update_values.append(clean_region)
    if clean_note and not (str(match_row["note"] or "").strip()):
        update_parts.append("note = ?")
        update_values.append(clean_note)

    if update_parts:
        conn.execute(
            f"UPDATE retail_customers SET {', '.join(update_parts)} WHERE id = ?",
            (*update_values, retail_customer_id),
        )

    return retail_customer_id


def create_sales_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            retail_customer_id INTEGER,
            sale_type TEXT NOT NULL DEFAULT 'WHOLESALE'
                CHECK(sale_type IN ('WHOLESALE', 'RETAIL')),
            invoice_no TEXT,
            sold_price REAL NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'PAID'
                CHECK(payment_status IN ('PAID', 'DUE')),
            paid_amount REAL NOT NULL DEFAULT 0,
            due_amount REAL NOT NULL DEFAULT 0,
            sold_at TEXT NOT NULL,
            branch_id INTEGER NOT NULL DEFAULT 1,
            receiver_name TEXT,
            receiver_phone TEXT,
            receiver_photo_path TEXT,
            note TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            canceled_at TEXT,
            cancel_reason TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE RESTRICT,
            FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE RESTRICT,
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE RESTRICT
        )
        """
    )


def migrate_products_table(conn: sqlite3.Connection) -> None:
    columns = get_table_columns(conn, "products")

    if "category" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN category TEXT")

    if "retail_price" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN retail_price REAL NOT NULL DEFAULT 0")

    if "supplier_id" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL")

    if "note" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN note TEXT")
    if "branch_id" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")
    if "warranty_type" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN warranty_type TEXT NOT NULL DEFAULT ''")

    conn.execute(
        """
        UPDATE products
        SET retail_price = wholesale_price
        WHERE COALESCE(retail_price, 0) = 0
        """
    )
    conn.execute(
        """
        UPDATE products
        SET warranty_type = CASE
                WHEN UPPER(TRIM(COALESCE(warranty_type, ''))) = 'OFFICIAL' THEN 'OFFICIAL'
                WHEN UPPER(TRIM(COALESCE(warranty_type, ''))) = 'UNOFFICIAL' THEN 'UNOFFICIAL'
                ELSE ''
            END
        """
    )

    columns = get_table_columns(conn, "products")
    if "supplier" in columns and "supplier_id" in columns:
        legacy_rows = conn.execute(
            """
            SELECT id, TRIM(supplier) AS supplier_name
            FROM products
            WHERE supplier IS NOT NULL AND TRIM(supplier) <> ''
              AND supplier_id IS NULL
            """
        ).fetchall()
        for row in legacy_rows:
            supplier_id = get_or_create_supplier(conn, row["supplier_name"])
            conn.execute("UPDATE products SET supplier_id = ? WHERE id = ?", (supplier_id, row["id"]))


def migrate_sales_table(conn: sqlite3.Connection) -> None:
    columns = get_table_columns(conn, "sales")
    if not columns:
        create_sales_table(conn)
        return

    required_columns = {
        "product_id",
        "customer_id",
        "sale_type",
        "sold_price",
        "payment_status",
        "sold_at",
        "is_active",
    }
    is_legacy = "customer_name" in columns or "customer_phone" in columns or not required_columns.issubset(columns)
    if not is_legacy:
        if "retail_customer_id" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN retail_customer_id INTEGER")
        if "paid_amount" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN paid_amount REAL NOT NULL DEFAULT 0")
        if "due_amount" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN due_amount REAL NOT NULL DEFAULT 0")
        if "receiver_name" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN receiver_name TEXT")
        if "receiver_phone" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN receiver_phone TEXT")
        if "receiver_photo_path" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN receiver_photo_path TEXT")
        if "branch_id" not in columns:
            conn.execute("ALTER TABLE sales ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                """
                UPDATE sales
                SET branch_id = COALESCE(
                    (
                        SELECT p.branch_id
                        FROM products p
                        WHERE p.id = sales.product_id
                        LIMIT 1
                    ),
                    1
                )
                """
            )
        conn.execute(
            """
            UPDATE sales
            SET paid_amount = CASE
                    WHEN payment_status = 'PAID' THEN sold_price
                    ELSE COALESCE(paid_amount, 0)
                END,
                due_amount = CASE
                    WHEN payment_status = 'DUE' THEN sold_price - COALESCE(paid_amount, 0)
                    ELSE 0
                END
            WHERE COALESCE(paid_amount, 0) = 0 AND COALESCE(due_amount, 0) = 0
            """
        )
        conn.execute(
            """
            UPDATE sales
            SET due_amount = CASE
                    WHEN due_amount < 0 THEN 0
                    ELSE due_amount
                END,
                payment_status = CASE
                    WHEN COALESCE(due_amount, 0) <= 0 THEN 'PAID'
                    ELSE 'DUE'
                END
            """
        )
        return

    conn.execute("ALTER TABLE sales RENAME TO sales_legacy")
    create_sales_table(conn)

    legacy_rows = conn.execute("SELECT * FROM sales_legacy ORDER BY id").fetchall()
    for row in legacy_rows:
        customer_id = row["customer_id"] if "customer_id" in row.keys() else None

        if not customer_id:
            customer_name = row["customer_name"] if "customer_name" in row.keys() and row["customer_name"] else ""
            customer_phone = row["customer_phone"] if "customer_phone" in row.keys() and row["customer_phone"] else ""
            if customer_name:
                customer_id = get_or_create_customer(conn, customer_name, customer_phone)

        if not customer_id:
            customer_id = get_or_create_customer(conn, "Unknown Customer")

        sale_type = row["sale_type"] if "sale_type" in row.keys() else "WHOLESALE"
        if sale_type not in SALE_TYPES:
            sale_type = "WHOLESALE"

        payment_status = row["payment_status"] if "payment_status" in row.keys() else "PAID"
        if payment_status not in PAYMENT_STATUSES:
            payment_status = "PAID"

        sold_at = row["sold_at"] if "sold_at" in row.keys() and row["sold_at"] else date.today().isoformat()
        is_active = int(row["is_active"]) if "is_active" in row.keys() and row["is_active"] in (0, 1) else 1
        canceled_at = row["canceled_at"] if "canceled_at" in row.keys() else None
        cancel_reason = row["cancel_reason"] if "cancel_reason" in row.keys() else None
        note = row["note"] if "note" in row.keys() else ""

        conn.execute(
            """
            INSERT INTO sales (
                product_id, customer_id, retail_customer_id, sale_type, invoice_no,
                sold_price, payment_status, paid_amount, due_amount, sold_at, branch_id,
                receiver_name, receiver_phone, receiver_photo_path, note,
                is_active, canceled_at, cancel_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["product_id"],
                customer_id,
                None,
                sale_type,
                row["invoice_no"] if "invoice_no" in row.keys() else None,
                float(row["sold_price"]),
                payment_status,
                float(row["sold_price"]) if payment_status == "PAID" else 0.0,
                0.0 if payment_status == "PAID" else float(row["sold_price"]),
                sold_at,
                1,
                None,
                None,
                None,
                note,
                is_active,
                canceled_at,
                cancel_reason,
            ),
        )

    conn.execute("DROP TABLE sales_legacy")


def ensure_backup_schedule_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_schedule_settings (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            is_enabled INTEGER NOT NULL DEFAULT 0 CHECK(is_enabled IN (0, 1)),
            backup_type TEXT NOT NULL DEFAULT 'PACKAGE',
            frequency TEXT NOT NULL DEFAULT 'DAILY',
            run_hour INTEGER NOT NULL DEFAULT 3,
            run_minute INTEGER NOT NULL DEFAULT 0,
            weekly_day TEXT NOT NULL DEFAULT 'SUN',
            monthly_day INTEGER NOT NULL DEFAULT 1,
            sync_google INTEGER NOT NULL DEFAULT 0 CHECK(sync_google IN (0, 1)),
            last_run_at TEXT,
            next_run_at TEXT,
            last_status TEXT,
            last_filename TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        )
        """
    )

    columns = get_table_columns(conn, "backup_schedule_settings")
    if "is_enabled" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN is_enabled INTEGER NOT NULL DEFAULT 0")
    if "backup_type" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN backup_type TEXT NOT NULL DEFAULT 'PACKAGE'")
    if "frequency" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN frequency TEXT NOT NULL DEFAULT 'DAILY'")
    if "run_hour" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN run_hour INTEGER NOT NULL DEFAULT 3")
    if "run_minute" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN run_minute INTEGER NOT NULL DEFAULT 0")
    if "weekly_day" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN weekly_day TEXT NOT NULL DEFAULT 'SUN'")
    if "monthly_day" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN monthly_day INTEGER NOT NULL DEFAULT 1")
    if "sync_google" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN sync_google INTEGER NOT NULL DEFAULT 0")
    if "last_run_at" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN last_run_at TEXT")
    if "next_run_at" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN next_run_at TEXT")
    if "last_status" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN last_status TEXT")
    if "last_filename" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN last_filename TEXT")
    if "last_error" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN last_error TEXT")
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE backup_schedule_settings ADD COLUMN updated_at TEXT")

    conn.execute(
        """
        INSERT OR IGNORE INTO backup_schedule_settings (id, updated_at)
        VALUES (1, ?)
        """,
        (now_sqlite_text(),),
    )
    conn.execute(
        """
        UPDATE backup_schedule_settings
        SET is_enabled = CASE WHEN is_enabled = 1 THEN 1 ELSE 0 END,
            backup_type = CASE
                WHEN UPPER(TRIM(COALESCE(backup_type, ''))) = 'DB_COPY' THEN 'DB_COPY'
                ELSE 'PACKAGE'
            END,
            frequency = CASE
                WHEN UPPER(TRIM(COALESCE(frequency, ''))) = 'WEEKLY' THEN 'WEEKLY'
                WHEN UPPER(TRIM(COALESCE(frequency, ''))) = 'MONTHLY' THEN 'MONTHLY'
                ELSE 'DAILY'
            END,
            run_hour = CASE
                WHEN CAST(COALESCE(run_hour, 3) AS INTEGER) BETWEEN 0 AND 23
                    THEN CAST(run_hour AS INTEGER)
                ELSE 3
            END,
            run_minute = CASE
                WHEN CAST(COALESCE(run_minute, 0) AS INTEGER) BETWEEN 0 AND 59
                    THEN CAST(run_minute AS INTEGER)
                ELSE 0
            END,
            weekly_day = CASE
                WHEN UPPER(TRIM(COALESCE(weekly_day, ''))) IN ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')
                    THEN UPPER(TRIM(weekly_day))
                ELSE 'SUN'
            END,
            monthly_day = CASE
                WHEN CAST(COALESCE(monthly_day, 1) AS INTEGER) BETWEEN 1 AND 28
                    THEN CAST(monthly_day AS INTEGER)
                ELSE 1
            END,
            sync_google = CASE WHEN sync_google = 1 THEN 1 ELSE 0 END
        WHERE id = 1
        """
    )


def get_backup_schedule_settings(conn: sqlite3.Connection) -> sqlite3.Row:
    ensure_backup_schedule_table(conn)
    row = conn.execute(
        """
        SELECT *
        FROM backup_schedule_settings
        WHERE id = 1
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("Backup schedule settings row missing.")
    return row


def ensure_expense_finance_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_date TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'misc',
            sub_category TEXT,
            employee_name TEXT,
            amount REAL NOT NULL CHECK(amount >= 0),
            payment_method TEXT NOT NULL DEFAULT 'CASH',
            branch_id INTEGER NOT NULL DEFAULT 1,
            note TEXT,
            receipt_path TEXT,
            entered_by_user_id INTEGER,
            entered_by_username TEXT,
            approval_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(approval_status IN ('PENDING', 'APPROVED', 'REJECTED')),
            approved_by_user_id INTEGER,
            approved_at TEXT,
            rejected_note TEXT,
            is_recurring_source INTEGER NOT NULL DEFAULT 0 CHECK(is_recurring_source IN (0, 1)),
            recurring_template_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS expense_recurring_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'misc',
            sub_category TEXT,
            employee_name TEXT,
            amount REAL NOT NULL CHECK(amount >= 0),
            payment_method TEXT NOT NULL DEFAULT 'CASH',
            branch_id INTEGER NOT NULL DEFAULT 1,
            day_of_month INTEGER NOT NULL DEFAULT 1,
            note TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_by_user_id INTEGER,
            created_by_username TEXT,
            last_generated_month TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS petty_cash_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cash_date TEXT NOT NULL,
            branch_id INTEGER NOT NULL DEFAULT 1,
            opening_cash REAL NOT NULL DEFAULT 0,
            closing_cash REAL NOT NULL DEFAULT 0,
            note TEXT,
            created_by_user_id INTEGER,
            created_by_username TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            income_date TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other_income',
            sub_category TEXT,
            source_name TEXT,
            amount REAL NOT NULL CHECK(amount >= 0),
            payment_method TEXT NOT NULL DEFAULT 'CASH',
            branch_id INTEGER NOT NULL DEFAULT 1,
            note TEXT,
            receipt_path TEXT,
            entered_by_user_id INTEGER,
            entered_by_username TEXT,
            approval_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(approval_status IN ('PENDING', 'APPROVED', 'REJECTED')),
            approved_by_user_id INTEGER,
            approved_at TEXT,
            rejected_note TEXT,
            is_recurring_source INTEGER NOT NULL DEFAULT 0 CHECK(is_recurring_source IN (0, 1)),
            recurring_template_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS income_recurring_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other_income',
            sub_category TEXT,
            source_name TEXT,
            amount REAL NOT NULL CHECK(amount >= 0),
            payment_method TEXT NOT NULL DEFAULT 'CASH',
            branch_id INTEGER NOT NULL DEFAULT 1,
            day_of_month INTEGER NOT NULL DEFAULT 1,
            note TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_by_user_id INTEGER,
            created_by_username TEXT,
            last_generated_month TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE RESTRICT
        );
        """
    )

    expense_columns = get_table_columns(conn, "expenses")
    if "sub_category" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN sub_category TEXT")
    if "employee_name" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN employee_name TEXT")
    if "payment_method" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'CASH'")
    if "branch_id" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")
    if "receipt_path" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN receipt_path TEXT")
    if "entered_by_user_id" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN entered_by_user_id INTEGER")
    if "entered_by_username" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN entered_by_username TEXT")
    if "approval_status" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'PENDING'")
    if "approved_by_user_id" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN approved_by_user_id INTEGER")
    if "approved_at" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN approved_at TEXT")
    if "rejected_note" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN rejected_note TEXT")
    if "is_recurring_source" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN is_recurring_source INTEGER NOT NULL DEFAULT 0")
    if "recurring_template_id" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN recurring_template_id INTEGER")
    if "updated_at" not in expense_columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))")

    recurring_columns = get_table_columns(conn, "expense_recurring_templates")
    if "title" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN title TEXT NOT NULL DEFAULT 'Recurring Expense'")
    if "sub_category" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN sub_category TEXT")
    if "employee_name" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN employee_name TEXT")
    if "payment_method" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'CASH'")
    if "branch_id" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")
    if "note" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN note TEXT")
    if "is_active" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "created_by_user_id" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN created_by_user_id INTEGER")
    if "created_by_username" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN created_by_username TEXT")
    if "last_generated_month" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN last_generated_month TEXT")
    if "updated_at" not in recurring_columns:
        conn.execute("ALTER TABLE expense_recurring_templates ADD COLUMN updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))")

    petty_columns = get_table_columns(conn, "petty_cash_daily")
    if "branch_id" not in petty_columns:
        conn.execute("ALTER TABLE petty_cash_daily ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")
    if "opening_cash" not in petty_columns:
        conn.execute("ALTER TABLE petty_cash_daily ADD COLUMN opening_cash REAL NOT NULL DEFAULT 0")
    if "closing_cash" not in petty_columns:
        conn.execute("ALTER TABLE petty_cash_daily ADD COLUMN closing_cash REAL NOT NULL DEFAULT 0")
    if "note" not in petty_columns:
        conn.execute("ALTER TABLE petty_cash_daily ADD COLUMN note TEXT")
    if "created_by_user_id" not in petty_columns:
        conn.execute("ALTER TABLE petty_cash_daily ADD COLUMN created_by_user_id INTEGER")
    if "created_by_username" not in petty_columns:
        conn.execute("ALTER TABLE petty_cash_daily ADD COLUMN created_by_username TEXT")
    if "updated_at" not in petty_columns:
        conn.execute("ALTER TABLE petty_cash_daily ADD COLUMN updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))")

    income_columns = get_table_columns(conn, "incomes")
    if "sub_category" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN sub_category TEXT")
    if "source_name" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN source_name TEXT")
    if "payment_method" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'CASH'")
    if "branch_id" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")
    if "receipt_path" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN receipt_path TEXT")
    if "entered_by_user_id" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN entered_by_user_id INTEGER")
    if "entered_by_username" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN entered_by_username TEXT")
    if "approval_status" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'PENDING'")
    if "approved_by_user_id" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN approved_by_user_id INTEGER")
    if "approved_at" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN approved_at TEXT")
    if "rejected_note" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN rejected_note TEXT")
    if "is_recurring_source" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN is_recurring_source INTEGER NOT NULL DEFAULT 0")
    if "recurring_template_id" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN recurring_template_id INTEGER")
    if "updated_at" not in income_columns:
        conn.execute("ALTER TABLE incomes ADD COLUMN updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))")

    recurring_income_columns = get_table_columns(conn, "income_recurring_templates")
    if "title" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN title TEXT NOT NULL DEFAULT 'Recurring Income'")
    if "sub_category" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN sub_category TEXT")
    if "source_name" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN source_name TEXT")
    if "payment_method" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'CASH'")
    if "branch_id" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")
    if "note" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN note TEXT")
    if "is_active" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "created_by_user_id" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN created_by_user_id INTEGER")
    if "created_by_username" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN created_by_username TEXT")
    if "last_generated_month" not in recurring_income_columns:
        conn.execute("ALTER TABLE income_recurring_templates ADD COLUMN last_generated_month TEXT")
    if "updated_at" not in recurring_income_columns:
        conn.execute(
            "ALTER TABLE income_recurring_templates ADD COLUMN updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))"
        )

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_expenses_date_status
        ON expenses(expense_date, approval_status);
        CREATE INDEX IF NOT EXISTS idx_expenses_branch_date
        ON expenses(branch_id, expense_date);
        CREATE INDEX IF NOT EXISTS idx_expenses_category
        ON expenses(category, expense_date);
        CREATE INDEX IF NOT EXISTS idx_expenses_employee_date
        ON expenses(employee_name, expense_date);
        CREATE INDEX IF NOT EXISTS idx_incomes_date_status
        ON incomes(income_date, approval_status);
        CREATE INDEX IF NOT EXISTS idx_incomes_branch_date
        ON incomes(branch_id, income_date);
        CREATE INDEX IF NOT EXISTS idx_incomes_category
        ON incomes(category, income_date);
        CREATE INDEX IF NOT EXISTS idx_incomes_source_date
        ON incomes(source_name, income_date);
        CREATE INDEX IF NOT EXISTS idx_recurring_active
        ON expense_recurring_templates(is_active, day_of_month);
        CREATE INDEX IF NOT EXISTS idx_income_recurring_active
        ON income_recurring_templates(is_active, day_of_month);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_petty_cash_date_branch
        ON petty_cash_daily(cash_date, branch_id);
        """
    )

    conn.execute(
        """
        UPDATE expenses
        SET category = LOWER(TRIM(category))
        WHERE category IS NOT NULL AND TRIM(category) <> ''
        """
    )
    conn.execute(
        """
        UPDATE expenses
        SET category = 'misc'
        WHERE category IS NULL OR TRIM(category) = ''
        """
    )
    conn.execute(
        """
        UPDATE expenses
        SET employee_name = TRIM(employee_name)
        WHERE employee_name IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE incomes
        SET category = LOWER(TRIM(category))
        WHERE category IS NOT NULL AND TRIM(category) <> ''
        """
    )
    conn.execute(
        """
        UPDATE incomes
        SET category = 'other_income'
        WHERE category IS NULL OR TRIM(category) = ''
        """
    )
    conn.execute(
        """
        UPDATE incomes
        SET source_name = TRIM(source_name)
        WHERE source_name IS NOT NULL
        """
    )


def ensure_enterprise_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inventory_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_key TEXT NOT NULL UNIQUE,
            product_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL DEFAULT 1,
            reserved_for TEXT,
            reserved_by_user_id INTEGER,
            reserved_by_username TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK(status IN ('ACTIVE', 'RELEASED', 'CONSUMED', 'EXPIRED')),
            expires_at TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS branch_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_no TEXT NOT NULL UNIQUE,
            product_id INTEGER NOT NULL,
            from_branch_id INTEGER NOT NULL,
            to_branch_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'REQUESTED'
                CHECK(status IN ('REQUESTED', 'APPROVED', 'IN_TRANSIT', 'RECEIVED', 'CANCELED')),
            requested_by_user_id INTEGER,
            requested_by_username TEXT,
            approved_by_user_id INTEGER,
            approved_by_username TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY(from_branch_id) REFERENCES branches(id) ON DELETE RESTRICT,
            FOREIGN KEY(to_branch_id) REFERENCES branches(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS due_followups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            followup_date TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'CALL',
            note TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING', 'DONE', 'SKIPPED')),
            created_by_user_id INTEGER,
            created_by_username TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS fraud_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'LOW',
            sale_id INTEGER,
            product_id INTEGER,
            invoice_no TEXT,
            actor_username TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        );

        CREATE TABLE IF NOT EXISTS automation_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            conditions_json TEXT,
            actions_json TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_by_user_id INTEGER,
            created_by_username TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        );

        CREATE TABLE IF NOT EXISTS integration_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            target_provider TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING', 'SENT', 'FAILED')),
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        );

        CREATE TABLE IF NOT EXISTS supplier_returns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL,
            source_product_id INTEGER,
            imei TEXT NOT NULL,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            storage TEXT,
            color TEXT,
            category TEXT,
            warranty_type TEXT,
            purchase_price REAL NOT NULL DEFAULT 0,
            wholesale_price REAL NOT NULL DEFAULT 0,
            retail_price REAL NOT NULL DEFAULT 0,
            received_date TEXT,
            returned_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            note TEXT,
            created_by_user_id INTEGER,
            created_by_username TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE
        );
        """
    )

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_product_status
        ON inventory_reservations(product_id, status, expires_at);
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_branch_status
        ON inventory_reservations(branch_id, status);
        CREATE INDEX IF NOT EXISTS idx_branch_transfers_status
        ON branch_transfers(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_due_followups_customer_date
        ON due_followups(customer_id, followup_date, status);
        CREATE INDEX IF NOT EXISTS idx_fraud_events_recent
        ON fraud_events(id DESC, severity);
        CREATE INDEX IF NOT EXISTS idx_automation_rules_event
        ON automation_rules(event_type, is_active);
        CREATE INDEX IF NOT EXISTS idx_integration_outbox_status
        ON integration_outbox(status, id ASC);
        CREATE INDEX IF NOT EXISTS idx_supplier_returns_supplier_date
        ON supplier_returns(supplier_id, returned_at, id DESC);
        CREATE INDEX IF NOT EXISTS idx_supplier_returns_imei
        ON supplier_returns(imei, id DESC);
        """
    )

    reservation_columns = get_table_columns(conn, "inventory_reservations")
    if "branch_id" not in reservation_columns:
        conn.execute("ALTER TABLE inventory_reservations ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")

    products_columns = get_table_columns(conn, "products")
    if "branch_id" not in products_columns:
        conn.execute("ALTER TABLE products ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")

    sales_columns = get_table_columns(conn, "sales")
    if "branch_id" not in sales_columns:
        conn.execute("ALTER TABLE sales ADD COLUMN branch_id INTEGER NOT NULL DEFAULT 1")


def ensure_daily_report_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_report_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            report_no TEXT NOT NULL UNIQUE,
            snapshot_json TEXT NOT NULL,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'DRAFT'
                CHECK(status IN ('DRAFT', 'FINAL')),
            created_by_user_id INTEGER,
            created_by_username TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_daily_report_date
        ON daily_report_snapshots(report_date, id DESC);
        """
    )

    columns = get_table_columns(conn, "daily_report_snapshots")
    if "note" not in columns:
        conn.execute("ALTER TABLE daily_report_snapshots ADD COLUMN note TEXT")
    if "status" not in columns:
        conn.execute("ALTER TABLE daily_report_snapshots ADD COLUMN status TEXT NOT NULL DEFAULT 'DRAFT'")
    if "created_by_user_id" not in columns:
        conn.execute("ALTER TABLE daily_report_snapshots ADD COLUMN created_by_user_id INTEGER")
    if "created_by_username" not in columns:
        conn.execute("ALTER TABLE daily_report_snapshots ADD COLUMN created_by_username TEXT")


def safe_ensure_daily_report_tables(conn: sqlite3.Connection) -> None:
    try:
        ensure_daily_report_tables(conn)
    except sqlite3.Error as exc:
        # Shared hosting may block schema updates in some cases.
        # Keep core app running even if daily snapshot storage is unavailable.
        print(f"Daily report table init failed: {exc}")
    conn.execute(
        """
        UPDATE expenses
        SET payment_method = UPPER(TRIM(payment_method))
        WHERE payment_method IS NOT NULL AND TRIM(payment_method) <> ''
        """
    )
    conn.execute(
        """
        UPDATE expenses
        SET payment_method = 'CASH'
        WHERE payment_method IS NULL OR TRIM(payment_method) = ''
        """
    )
    conn.execute(
        """
        UPDATE expenses
        SET approval_status = CASE
            WHEN UPPER(COALESCE(approval_status, '')) IN ('APPROVED', 'PENDING', 'REJECTED')
                THEN UPPER(TRIM(approval_status))
            ELSE 'PENDING'
        END
        """
    )
    conn.execute(
        """
        UPDATE expense_recurring_templates
        SET category = LOWER(TRIM(category))
        WHERE category IS NOT NULL AND TRIM(category) <> ''
        """
    )
    conn.execute(
        """
        UPDATE expense_recurring_templates
        SET category = 'misc'
        WHERE category IS NULL OR TRIM(category) = ''
        """
    )
    conn.execute(
        """
        UPDATE expense_recurring_templates
        SET employee_name = TRIM(employee_name)
        WHERE employee_name IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE expense_recurring_templates
        SET payment_method = UPPER(TRIM(payment_method))
        WHERE payment_method IS NOT NULL AND TRIM(payment_method) <> ''
        """
    )
    conn.execute(
        """
        UPDATE expense_recurring_templates
        SET payment_method = 'CASH'
        WHERE payment_method IS NULL OR TRIM(payment_method) = ''
        """
    )
    conn.execute(
        """
        UPDATE expense_recurring_templates
        SET day_of_month = CASE
            WHEN day_of_month < 1 THEN 1
            WHEN day_of_month > 28 THEN 28
            ELSE day_of_month
        END
        """
    )


def ensure_model_catalog_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_number TEXT,
            storage TEXT,
            region TEXT,
            color TEXT,
            condition_state TEXT NOT NULL DEFAULT 'NEW'
                CHECK(condition_state IN ('NEW', 'ACTIVE', 'USED')),
            category TEXT,
            tac_prefix TEXT,
            keywords TEXT,
            extra_info TEXT,
            purchase_price REAL,
            wholesale_price REAL,
            retail_price REAL,
            supplier_id INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_by_user_id INTEGER,
            created_by_username TEXT,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_model_catalog_active_lookup
        ON model_catalog(is_active, brand, model_name);
        CREATE INDEX IF NOT EXISTS idx_model_catalog_tac
        ON model_catalog(tac_prefix);
        CREATE INDEX IF NOT EXISTS idx_model_catalog_supplier
        ON model_catalog(supplier_id);
        CREATE INDEX IF NOT EXISTS idx_model_catalog_profile_dedupe
        ON model_catalog(brand, model_name, storage, region, color, condition_state);
        """
    )

    columns = get_table_columns(conn, "model_catalog")
    if "model_number" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN model_number TEXT")
    if "region" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN region TEXT")
    if "condition_state" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN condition_state TEXT NOT NULL DEFAULT 'NEW'")
    if "category" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN category TEXT")
    if "tac_prefix" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN tac_prefix TEXT")
    if "keywords" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN keywords TEXT")
    if "extra_info" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN extra_info TEXT")
    if "purchase_price" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN purchase_price REAL")
    if "wholesale_price" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN wholesale_price REAL")
    if "retail_price" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN retail_price REAL")
    if "supplier_id" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN supplier_id INTEGER")
    if "is_active" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "created_by_user_id" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN created_by_user_id INTEGER")
    if "created_by_username" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN created_by_username TEXT")
    if "created_at" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN created_at TEXT")
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE model_catalog ADD COLUMN updated_at TEXT")

    conn.execute(
        """
        UPDATE model_catalog
        SET condition_state = CASE
            WHEN UPPER(TRIM(COALESCE(condition_state, ''))) IN ('NEW', 'ACTIVE', 'USED')
                THEN UPPER(TRIM(condition_state))
            WHEN UPPER(TRIM(COALESCE(condition_state, ''))) IN ('USE', 'SECONDHAND', 'SECOND_HAND', 'OLD')
                THEN 'USED'
            WHEN UPPER(TRIM(COALESCE(condition_state, ''))) IN ('ACTIVATED', 'OPENBOX', 'OPEN_BOX')
                THEN 'ACTIVE'
            ELSE 'NEW'
        END
        """
    )
    conn.execute(
        """
        UPDATE model_catalog
        SET tac_prefix = SUBSTR(REPLACE(REPLACE(REPLACE(COALESCE(tac_prefix, ''), ' ', ''), '-', ''), '.', ''), 1, 8)
        WHERE tac_prefix IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE model_catalog
        SET is_active = CASE WHEN COALESCE(is_active, 0) = 1 THEN 1 ELSE 0 END
        """
    )
    conn.execute(
        """
        UPDATE model_catalog
        SET category = COALESCE(NULLIF(TRIM(category), ''), 'Smartphone')
        """
    )
    conn.execute(
        """
        UPDATE model_catalog
        SET created_at = COALESCE(NULLIF(TRIM(created_at), ''), DATETIME('now')),
            updated_at = COALESCE(NULLIF(TRIM(updated_at), ''), DATETIME('now'))
        """
    )


def init_db_for_path(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        ensure_tenant_users_table(conn)

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                contact_person TEXT,
                phone TEXT,
                address TEXT,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_name TEXT NOT NULL UNIQUE,
                owner_name TEXT,
                phone TEXT,
                area TEXT,
                address TEXT,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS retail_customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                phone TEXT,
                address TEXT,
                region TEXT,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imei TEXT NOT NULL UNIQUE,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                category TEXT,
                color TEXT,
                storage TEXT,
                warranty_type TEXT NOT NULL DEFAULT '',
                purchase_price REAL NOT NULL,
                wholesale_price REAL NOT NULL,
                retail_price REAL NOT NULL DEFAULT 0,
                supplier_id INTEGER,
                branch_id INTEGER NOT NULL DEFAULT 1,
                received_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'IN_STOCK'
                    CHECK(status IN ('IN_STOCK', 'SOLD')),
                note TEXT,
                FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL,
                FOREIGN KEY(branch_id) REFERENCES branches(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                address TEXT,
                is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1))
            );
            """
        )

        create_sales_table(conn)
        migrate_products_table(conn)
        migrate_sales_table(conn)
        migrate_receiver_photo_storage(conn)

        conn.execute(
            """
            INSERT OR IGNORE INTO branches (id, name, is_default)
            VALUES (1, 'Main Branch', 1)
            """
        )
        ensure_expense_finance_tables(conn)
        ensure_model_catalog_table(conn)
        ensure_enterprise_tables(conn)
        safe_ensure_daily_report_tables(conn)
        ensure_backup_schedule_table(conn)

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sale_returns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL UNIQUE,
                return_date TEXT NOT NULL,
                reason TEXT,
                restock INTEGER NOT NULL DEFAULT 1 CHECK(restock IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (DATE('now')),
                FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS due_collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                collected_at TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT 'CASH',
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (DATE('now')),
                FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE,
                FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS retail_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT NOT NULL UNIQUE,
                sold_at TEXT NOT NULL,
                retail_customer_id INTEGER,
                customer_name TEXT NOT NULL,
                customer_phone TEXT,
                customer_address TEXT,
                customer_region TEXT,
                subtotal REAL NOT NULL DEFAULT 0,
                paid_amount REAL NOT NULL DEFAULT 0,
                due_amount REAL NOT NULL DEFAULT 0,
                payment_status TEXT NOT NULL DEFAULT 'PAID'
                    CHECK(payment_status IN ('PAID', 'DUE')),
                note TEXT,
                share_token TEXT NOT NULL UNIQUE,
                created_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
            );

            CREATE TABLE IF NOT EXISTS backup_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                local_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                google_status TEXT NOT NULL DEFAULT 'NOT_SENT',
                google_file_id TEXT
            );

            CREATE TABLE IF NOT EXISTS stock_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('FORCE_STOCK_IN')),
                event_date TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
            CREATE INDEX IF NOT EXISTS idx_products_brand_category ON products(brand, category);
            CREATE INDEX IF NOT EXISTS idx_products_supplier ON products(supplier_id);
            CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id);
            CREATE INDEX IF NOT EXISTS idx_sales_retail_customer ON sales(retail_customer_id);
            CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);
            CREATE INDEX IF NOT EXISTS idx_sales_active ON sales(is_active);
            CREATE INDEX IF NOT EXISTS idx_sales_due_active ON sales(customer_id, due_amount, is_active);
            CREATE INDEX IF NOT EXISTS idx_returns_date ON sale_returns(return_date);
            CREATE INDEX IF NOT EXISTS idx_due_collections_sale ON due_collections(sale_id);
            CREATE INDEX IF NOT EXISTS idx_due_collections_customer ON due_collections(customer_id, collected_at);
            CREATE INDEX IF NOT EXISTS idx_retail_customers_name_phone ON retail_customers(full_name, phone);
            CREATE INDEX IF NOT EXISTS idx_retail_invoices_date ON retail_invoices(sold_at, id DESC);
            CREATE INDEX IF NOT EXISTS idx_backup_created ON backup_logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_stock_adjustments_product ON stock_adjustments(product_id, event_date);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_active_product
                ON sales(product_id)
                WHERE is_active = 1;
            """
        )

        retail_columns = get_table_columns(conn, "retail_invoices")
        sales_columns = get_table_columns(conn, "sales")
        if "retail_customer_id" not in sales_columns:
            conn.execute("ALTER TABLE sales ADD COLUMN retail_customer_id INTEGER")
            sales_columns = get_table_columns(conn, "sales")
        if "retail_customer_id" not in retail_columns:
            conn.execute("ALTER TABLE retail_invoices ADD COLUMN retail_customer_id INTEGER")
            retail_columns = get_table_columns(conn, "retail_invoices")
        if "customer_region" not in retail_columns:
            conn.execute("ALTER TABLE retail_invoices ADD COLUMN customer_region TEXT")
        if "share_token" not in retail_columns:
            conn.execute("ALTER TABLE retail_invoices ADD COLUMN share_token TEXT")
            conn.execute(
                """
                UPDATE retail_invoices
                SET share_token = LOWER(HEX(RANDOMBLOB(16)))
                WHERE share_token IS NULL OR TRIM(share_token) = ''
                """
            )
        if "created_by_user_id" not in retail_columns:
            conn.execute("ALTER TABLE retail_invoices ADD COLUMN created_by_user_id INTEGER")

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_retail_invoices_customer
            ON retail_invoices(retail_customer_id, sold_at)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_retail_invoices_share_token
            ON retail_invoices(share_token)
            """
        )

        # Backfill retail customer master for older invoices and map sales rows.
        legacy_retail_rows = conn.execute(
            """
            SELECT id, customer_name, customer_phone, customer_address, customer_region
            FROM retail_invoices
            WHERE retail_customer_id IS NULL
            ORDER BY id ASC
            """
        ).fetchall()
        for row in legacy_retail_rows:
            retail_customer_id = get_or_create_retail_customer(
                conn,
                str(row["customer_name"] or "").strip() or "Walk-in Customer",
                str(row["customer_phone"] or "").strip(),
                str(row["customer_address"] or "").strip(),
                str(row["customer_region"] or "").strip(),
            )
            conn.execute(
                "UPDATE retail_invoices SET retail_customer_id = ? WHERE id = ?",
                (retail_customer_id, int(row["id"])),
            )

        if "retail_customer_id" in sales_columns:
            conn.execute(
                """
                UPDATE sales
                SET retail_customer_id = (
                    SELECT ri.retail_customer_id
                    FROM retail_invoices ri
                    WHERE ri.invoice_no = sales.invoice_no
                      AND ri.retail_customer_id IS NOT NULL
                    ORDER BY ri.id DESC
                    LIMIT 1
                )
                WHERE sale_type = 'RETAIL'
                  AND (retail_customer_id IS NULL OR retail_customer_id = 0)
                  AND invoice_no IS NOT NULL
                  AND TRIM(invoice_no) <> ''
                """
            )

        conn.execute(
            """
            UPDATE products
            SET status = 'SOLD'
            WHERE id IN (SELECT product_id FROM sales WHERE is_active = 1)
            """
        )
        conn.execute(
            """
            UPDATE products
            SET status = 'IN_STOCK'
            WHERE id NOT IN (SELECT product_id FROM sales WHERE is_active = 1)
            """
        )
        ensure_tenant_index_hardening(conn)
        conn.commit()


def init_db() -> None:
    init_db_for_path(DB_PATH)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = get_current_db_path()
        g.db = sqlite3.connect(db_path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON;")
        sales_columns = get_table_columns(g.db, "sales")
        retail_invoice_columns = get_table_columns(g.db, "retail_invoices")
        if (
            "due_amount" not in sales_columns
            or "retail_customer_id" not in sales_columns
            or not table_exists(g.db, "due_collections")
            or not table_exists(g.db, "retail_invoices")
            or not table_exists(g.db, "retail_customers")
            or not table_exists(g.db, "stock_adjustments")
            or "retail_customer_id" not in retail_invoice_columns
        ):
            g.db.close()
            init_db_for_path(db_path)
            g.db = sqlite3.connect(db_path)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON;")
        if migrate_receiver_photo_storage(g.db):
            g.db.commit()
        ensure_expense_finance_tables(g.db)
        migrate_products_table(g.db)
        ensure_model_catalog_table(g.db)
        ensure_enterprise_tables(g.db)
        safe_ensure_daily_report_tables(g.db)
        ensure_backup_schedule_table(g.db)
        ensure_tenant_index_hardening(g.db)
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(_error: object | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()
    admin_db = g.pop("admin_db", None)
    if admin_db is not None:
        admin_db.close()
    g.pop("current_tenant", None)
    g.pop("current_tenant_user", None)


def query_scalar(sql: str, params: tuple = ()) -> int | float:
    row = get_db().execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return 0
    return row[0]


def normalize_tracking_code(raw_value: str, mode: str) -> str:
    clean_mode = (mode or TRACKING_MODE_IMEI).upper()
    raw_text = (raw_value or "").strip()
    if not raw_text:
        return ""

    if clean_mode == TRACKING_MODE_IMEI:
        digits = re.sub(r"\D", "", raw_text)
        return digits[:15]

    normalized = re.sub(r"[^A-Za-z0-9._/-]", "", raw_text.upper())
    return normalized[:40]


def is_valid_tracking_code(code: str, mode: str) -> bool:
    clean_mode = (mode or TRACKING_MODE_IMEI).upper()
    value = (code or "").strip()
    if not value:
        return False

    if clean_mode == TRACKING_MODE_IMEI:
        return bool(re.fullmatch(r"\d{15}", value))

    if clean_mode == TRACKING_MODE_SERIAL:
        return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{4,39}", value.upper()))

    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{2,39}", value.upper()))


def normalize_tracking_text(raw_text: str, mode: str) -> list[str]:
    clean_mode = (mode or TRACKING_MODE_IMEI).upper()
    text = raw_text or ""

    if clean_mode == TRACKING_MODE_IMEI:
        all_digits = re.findall(r"\d{15}", text)
        if all_digits:
            unique_imeis: list[str] = []
            seen_imeis: set[str] = set()
            for item in all_digits:
                if item in seen_imeis:
                    continue
                seen_imeis.add(item)
                unique_imeis.append(item)
            return unique_imeis

        loose_items = re.split(r"[\n,\s]+", text)
        unique_loose: list[str] = []
        seen_loose: set[str] = set()
        for item in loose_items:
            normalized = normalize_tracking_code(item, clean_mode)
            if not normalized or normalized in seen_loose:
                continue
            seen_loose.add(normalized)
            unique_loose.append(normalized)
        return unique_loose

    tokens = re.split(r"[\n,;\t ]+", text)
    unique_codes: list[str] = []
    seen_codes: set[str] = set()
    for item in tokens:
        normalized = normalize_tracking_code(item, clean_mode)
        if not normalized or normalized in seen_codes:
            continue
        seen_codes.add(normalized)
        unique_codes.append(normalized)
    return unique_codes


def is_valid_imei(imei: str) -> bool:
    mode = get_current_tracking_mode()
    normalized = normalize_tracking_code(imei, mode)
    return is_valid_tracking_code(normalized, mode)


def parse_money(value: str, field_name: str) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid number.")
    if amount < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    return amount


def normalize_date(value: str) -> str:
    return value if value else date.today().isoformat()


def parse_optional_int(value: str) -> int | None:
    clean = value.strip()
    if not clean:
        return None
    if not clean.isdigit():
        return None
    return int(clean)


def parse_optional_money(value: str) -> float | None:
    clean = (value or "").strip()
    if not clean:
        return None
    try:
        amount = float(clean)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return amount


def normalize_model_condition(value: str) -> str:
    raw = (value or "").strip().upper()
    if raw in MODEL_CONDITION_STATES:
        return raw
    if raw in {"USE", "USED", "SECONDHAND", "SECOND_HAND", "OLD"}:
        return "USED"
    if raw in {"ACTIVE", "ACTIVATED", "OPENBOX", "OPEN_BOX"}:
        return "ACTIVE"
    return "NEW"


def normalize_warranty_type(value: str) -> str:
    raw = (value or "").strip().upper()
    if raw in WARRANTY_TYPES:
        return raw
    if raw in {"OFFICIAL WARRANTY", "AUTHORIZED", "AUTHORIZED WARRANTY"}:
        return "OFFICIAL"
    if raw in {"UNOFFICIAL WARRANTY", "WITHOUT WARRANTY", "NO WARRANTY", "NON OFFICIAL"}:
        return "UNOFFICIAL"
    return ""


def normalize_backup_schedule_frequency(value: str, default: str = "DAILY") -> str:
    raw = (value or "").strip().upper()
    if raw in BACKUP_SCHEDULE_FREQUENCIES:
        return raw
    return default


def normalize_backup_schedule_type(value: str, default: str = "PACKAGE") -> str:
    raw = (value or "").strip().upper()
    if raw in BACKUP_SCHEDULE_TYPES:
        return raw
    return default


def normalize_backup_schedule_weekday(value: str, default: str = "SUN") -> str:
    raw = (value or "").strip().upper()
    if raw in BACKUP_SCHEDULE_WEEKDAYS:
        return raw
    return default


def clamp_backup_schedule_hour(value: int | str | None, default: int = 3) -> int:
    try:
        hour = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(0, min(23, hour))


def clamp_backup_schedule_minute(value: int | str | None, default: int = 0) -> int:
    try:
        minute = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(0, min(59, minute))


def clamp_backup_schedule_month_day(value: int | str | None, default: int = 1) -> int:
    try:
        day_value = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(28, day_value))


def normalize_tac_prefix(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) >= 8:
        return digits[:8]
    return ""


def normalize_catalog_keywords(value: str) -> str:
    tokens = re.split(r"[,\n;|]+", value or "")
    cleaned: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        item = re.sub(r"\s+", " ", token.strip())
        if len(item) < 2:
            continue
        key = item.upper()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    return ", ".join(cleaned)


def normalize_text_field(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def canonical_text_key(value: str) -> str:
    return normalize_text_field(value).upper()


def find_duplicate_model_catalog_profile(
    conn: sqlite3.Connection,
    *,
    brand: str,
    model_name: str,
    storage: str,
    region: str,
    color: str,
    condition_state: str,
    exclude_id: int | None = None,
) -> sqlite3.Row | None:
    params: list[object] = [
        canonical_text_key(brand),
        canonical_text_key(model_name),
        canonical_text_key(storage),
        canonical_text_key(region),
        canonical_text_key(color),
        canonical_text_key(condition_state or "NEW"),
    ]
    sql = """
        SELECT id, brand, model_name, storage, region, color, condition_state
        FROM model_catalog
        WHERE UPPER(TRIM(COALESCE(brand, ''))) = ?
          AND UPPER(TRIM(COALESCE(model_name, ''))) = ?
          AND UPPER(TRIM(COALESCE(storage, ''))) = ?
          AND UPPER(TRIM(COALESCE(region, ''))) = ?
          AND UPPER(TRIM(COALESCE(color, ''))) = ?
          AND UPPER(TRIM(COALESCE(condition_state, 'NEW'))) = ?
    """
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(int(exclude_id))
    sql += " ORDER BY id DESC LIMIT 1"
    return conn.execute(sql, tuple(params)).fetchone()


def normalize_stock_report_status(value: str, default: str = "IN_STOCK") -> str:
    raw = (value or "").strip().upper()
    if raw in STOCK_REPORT_STATUSES:
        return raw
    return default


def normalize_stock_report_sort(value: str, default: str = "received_desc") -> str:
    raw = (value or "").strip().lower()
    if raw in STOCK_REPORT_SORT_OPTIONS:
        return raw
    return default


def fetch_stock_report_rows(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    status_filter: str = "IN_STOCK",
    brand_filter: str = "ALL",
    category_filter: str = "ALL",
    supplier_id: int | None = None,
    sort_key: str = "received_desc",
    limit: int = 2000,
) -> list[sqlite3.Row]:
    where_parts: list[str] = []
    params: list[object] = []

    clean_status = normalize_stock_report_status(status_filter)
    if clean_status != "ALL":
        where_parts.append("p.status = ?")
        params.append(clean_status)

    clean_brand = (brand_filter or "").strip()
    if clean_brand and clean_brand.upper() != "ALL":
        where_parts.append("UPPER(TRIM(p.brand)) = UPPER(?)")
        params.append(clean_brand)

    clean_category = (category_filter or "").strip()
    if clean_category and clean_category.upper() != "ALL":
        where_parts.append("UPPER(TRIM(COALESCE(p.category, ''))) = UPPER(?)")
        params.append(clean_category)

    if supplier_id is not None and supplier_id > 0:
        where_parts.append("p.supplier_id = ?")
        params.append(supplier_id)

    clean_q = (q or "").strip()
    if clean_q:
        like_q = f"%{clean_q}%"
        where_parts.append(
            """
            (
                UPPER(COALESCE(p.imei, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(p.brand, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(p.model, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(p.storage, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(p.color, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(p.category, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(s.name, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(sl.invoice_no, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(c.shop_name, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(rc.full_name, '')) LIKE UPPER(?)
            )
            """
        )
        params.extend([like_q] * 10)

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    order_sql = STOCK_REPORT_SORT_OPTIONS.get(
        normalize_stock_report_sort(sort_key),
        STOCK_REPORT_SORT_OPTIONS["received_desc"],
    )

    query = f"""
        SELECT
            p.id,
            p.imei,
            p.brand,
            p.model,
            COALESCE(p.storage, '') AS storage,
            COALESCE(p.color, '') AS color,
            COALESCE(p.category, '') AS category,
            COALESCE(p.warranty_type, '') AS warranty_type,
            p.purchase_price,
            p.wholesale_price,
            p.retail_price,
            p.received_date,
            p.status,
            COALESCE(p.note, '') AS note,
            p.supplier_id,
            s.name AS supplier_name,
            sl.id AS active_sale_id,
            sl.sold_at,
            sl.invoice_no,
            sl.sale_type,
            sl.sold_price,
            sl.paid_amount,
            sl.due_amount,
            sl.payment_status,
            CASE
                WHEN sl.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS holder_name
        FROM products p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        LEFT JOIN sales sl ON sl.product_id = p.id AND sl.is_active = 1
        LEFT JOIN customers c ON c.id = sl.customer_id
        LEFT JOIN retail_customers rc ON rc.id = sl.retail_customer_id
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ?
    """
    params.append(max(100, min(limit, 5000)))
    return conn.execute(query, tuple(params)).fetchall()


def build_stock_report_summary(rows: list[sqlite3.Row]) -> dict[str, float]:
    total_units = len(rows)
    in_stock_units = 0
    sold_units = 0
    in_stock_cost = 0.0
    in_stock_wholesale_value = 0.0
    in_stock_retail_value = 0.0
    due_outstanding = 0.0
    stock_age_days_total = 0
    stock_age_days_count = 0
    oldest_days = 0
    model_keys: set[str] = set()

    today_obj = date.today()
    for row in rows:
        status = str(row["status"] or "").upper()
        brand = str(row["brand"] or "").strip()
        model = str(row["model"] or "").strip()
        storage = str(row["storage"] or "").strip()
        color = str(row["color"] or "").strip()
        model_key = f"{brand}|{model}|{storage}|{color}"
        model_keys.add(model_key)

        if status == "IN_STOCK":
            in_stock_units += 1
            cost = float(row["purchase_price"] or 0)
            in_stock_cost += cost
            in_stock_wholesale_value += float(row["wholesale_price"] or 0)
            in_stock_retail_value += float(row["retail_price"] or 0)

            received_date = parse_iso_date(str(row["received_date"] or "").strip())
            if received_date is not None:
                age_days = max(0, (today_obj - received_date).days)
                stock_age_days_total += age_days
                stock_age_days_count += 1
                if age_days > oldest_days:
                    oldest_days = age_days
        elif status == "SOLD":
            sold_units += 1
            due_outstanding += float(row["due_amount"] or 0)

    potential_wholesale_profit = in_stock_wholesale_value - in_stock_cost
    potential_retail_profit = in_stock_retail_value - in_stock_cost
    avg_stock_age_days = (
        float(stock_age_days_total) / float(stock_age_days_count)
        if stock_age_days_count > 0
        else 0.0
    )
    stock_turnover_rate = (float(sold_units) / float(total_units) * 100.0) if total_units else 0.0

    return {
        "total_units": float(total_units),
        "in_stock_units": float(in_stock_units),
        "sold_units": float(sold_units),
        "in_stock_cost": in_stock_cost,
        "in_stock_wholesale_value": in_stock_wholesale_value,
        "in_stock_retail_value": in_stock_retail_value,
        "potential_wholesale_profit": potential_wholesale_profit,
        "potential_retail_profit": potential_retail_profit,
        "due_outstanding": due_outstanding,
        "avg_stock_age_days": avg_stock_age_days,
        "oldest_stock_days": float(oldest_days),
        "stock_turnover_rate": stock_turnover_rate,
        "model_count": float(len(model_keys)),
    }


def build_stock_model_summary(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    bucket: dict[str, dict[str, object]] = {}
    for row in rows:
        if str(row["status"] or "").upper() != "IN_STOCK":
            continue
        brand = str(row["brand"] or "").strip()
        model = str(row["model"] or "").strip()
        storage = str(row["storage"] or "").strip()
        color = str(row["color"] or "").strip()
        key = f"{brand}|{model}|{storage}|{color}"
        block = bucket.setdefault(
            key,
            {
                "brand": brand,
                "model": model,
                "storage": storage,
                "color": color,
                "units": 0,
                "cost": 0.0,
                "wholesale_value": 0.0,
                "retail_value": 0.0,
            },
        )
        block["units"] = int(block["units"]) + 1
        block["cost"] = float(block["cost"]) + float(row["purchase_price"] or 0)
        block["wholesale_value"] = float(block["wholesale_value"]) + float(row["wholesale_price"] or 0)
        block["retail_value"] = float(block["retail_value"]) + float(row["retail_price"] or 0)

    items = list(bucket.values())
    items.sort(
        key=lambda item: (
            -int(item["units"]),
            str(item["brand"]).upper(),
            str(item["model"]).upper(),
            str(item["storage"]).upper(),
        )
    )
    return items


def build_stock_due_risk_shops(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    bucket: dict[str, float] = {}
    for row in rows:
        if str(row["status"] or "").upper() != "SOLD":
            continue
        due_amount = float(row["due_amount"] or 0)
        if due_amount <= 0:
            continue
        holder = str(row["holder_name"] or "").strip() or "Unknown"
        bucket[holder] = bucket.get(holder, 0.0) + due_amount

    items = [
        {"holder_name": holder_name, "due_amount": amount}
        for holder_name, amount in bucket.items()
    ]
    items.sort(key=lambda item: float(item["due_amount"]), reverse=True)
    return items[:20]


def resolve_stock_report_filters(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    status: str = "IN_STOCK",
    brand: str = "ALL",
    category: str = "ALL",
    supplier_id_raw: str = "",
    sort: str = "received_desc",
    limit_raw: str = "",
) -> dict[str, object]:
    clean_q = (q or "").strip()
    status_filter = normalize_stock_report_status(status, default="IN_STOCK")

    brand_filter = (brand or "").strip() or "ALL"
    if brand_filter.upper() == "ALL":
        brand_filter = "ALL"

    category_filter = (category or "").strip() or "ALL"
    if category_filter.upper() == "ALL":
        category_filter = "ALL"

    supplier_id = parse_optional_int(supplier_id_raw)
    if supplier_id is not None:
        supplier_exists = conn.execute(
            "SELECT id FROM suppliers WHERE id = ?",
            (supplier_id,),
        ).fetchone()
        if supplier_exists is None:
            supplier_id = None

    sort_key = normalize_stock_report_sort(sort, default="received_desc")
    limit = parse_optional_int(limit_raw)
    if limit is None:
        limit = 2000
    limit = max(100, min(limit, 5000))

    return {
        "q": clean_q,
        "status_filter": status_filter,
        "brand_filter": brand_filter,
        "category_filter": category_filter,
        "supplier_id": supplier_id,
        "sort_key": sort_key,
        "limit": limit,
    }


def build_daily_report_data(conn: sqlite3.Connection, report_date: str) -> dict[str, object]:
    stock_in_rows_raw = conn.execute(
        """
        SELECT
            p.id,
            p.imei,
            p.brand,
            p.model,
            COALESCE(p.storage, '') AS storage,
            COALESCE(p.color, '') AS color,
            COALESCE(p.category, '') AS category,
            p.purchase_price,
            p.wholesale_price,
            p.retail_price,
            p.received_date,
            COALESCE(s.name, '') AS supplier_name
        FROM products p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        WHERE p.received_date = ?
        ORDER BY p.id DESC
        LIMIT 500
        """,
        (report_date,),
    ).fetchall()

    sales_rows_raw = conn.execute(
        """
        SELECT
            s.id,
            s.invoice_no,
            s.sold_at,
            s.sale_type,
            s.sold_price,
            s.paid_amount,
            s.due_amount,
            s.payment_status,
            p.imei,
            p.brand,
            p.model,
            COALESCE(p.storage, '') AS storage,
            p.purchase_price,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS party_name
        FROM sales s
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        WHERE s.is_active = 1
          AND s.sold_at = ?
        ORDER BY s.id DESC
        LIMIT 1200
        """,
        (report_date,),
    ).fetchall()

    return_rows_raw = conn.execute(
        """
        SELECT
            r.id,
            r.return_date,
            COALESCE(r.reason, '') AS reason,
            r.restock,
            s.id AS sale_id,
            s.invoice_no,
            s.sale_type,
            s.sold_price,
            s.paid_amount,
            s.due_amount,
            p.imei,
            p.brand,
            p.model,
            COALESCE(p.storage, '') AS storage,
            p.purchase_price,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS party_name
        FROM sale_returns r
        JOIN sales s ON s.id = r.sale_id
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        WHERE r.return_date = ?
        ORDER BY r.id DESC
        LIMIT 800
        """,
        (report_date,),
    ).fetchall()

    due_collection_rows_raw = conn.execute(
        """
        SELECT
            dc.id,
            dc.collected_at,
            dc.amount,
            dc.method,
            COALESCE(dc.note, '') AS note,
            s.invoice_no,
            p.imei,
            p.brand,
            p.model,
            COALESCE(c.shop_name, '') AS shop_name
        FROM due_collections dc
        JOIN sales s ON s.id = dc.sale_id
        LEFT JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = dc.customer_id
        WHERE dc.collected_at = ?
        ORDER BY dc.id DESC
        LIMIT 1000
        """,
        (report_date,),
    ).fetchall()

    expense_rows_raw = conn.execute(
        """
        SELECT
            e.id,
            e.expense_date,
            e.category,
            COALESCE(e.sub_category, '') AS sub_category,
            e.amount,
            e.payment_method,
            e.approval_status,
            COALESCE(e.note, '') AS note,
            COALESCE(b.name, 'Main Branch') AS branch_name
        FROM expenses e
        LEFT JOIN branches b ON b.id = e.branch_id
        WHERE e.expense_date = ?
        ORDER BY e.id DESC
        LIMIT 1200
        """,
        (report_date,),
    ).fetchall()

    income_rows_raw = conn.execute(
        """
        SELECT
            i.id,
            i.income_date,
            i.category,
            COALESCE(i.sub_category, '') AS sub_category,
            COALESCE(i.source_name, '') AS source_name,
            i.amount,
            i.payment_method,
            i.approval_status,
            COALESCE(i.note, '') AS note,
            COALESCE(b.name, 'Main Branch') AS branch_name
        FROM incomes i
        LEFT JOIN branches b ON b.id = i.branch_id
        WHERE i.income_date = ?
        ORDER BY i.id DESC
        LIMIT 1200
        """,
        (report_date,),
    ).fetchall()

    petty_rows = conn.execute(
        """
        SELECT cash_date, branch_id, opening_cash, closing_cash, COALESCE(note, '') AS note
        FROM petty_cash_daily
        WHERE cash_date = ?
        ORDER BY branch_id ASC
        LIMIT 50
        """,
        (report_date,),
    ).fetchall()

    sales_rows = [dict(row) for row in sales_rows_raw]
    stock_in_rows = [dict(row) for row in stock_in_rows_raw]
    return_rows = [dict(row) for row in return_rows_raw]
    due_collection_rows = [dict(row) for row in due_collection_rows_raw]
    expense_rows = [dict(row) for row in expense_rows_raw]
    income_rows = [dict(row) for row in income_rows_raw]
    petty_rows_dict = [dict(row) for row in petty_rows]

    wholesale_rows = [row for row in sales_rows if str(row.get("sale_type") or "").upper() == "WHOLESALE"]
    retail_rows = [row for row in sales_rows if str(row.get("sale_type") or "").upper() == "RETAIL"]

    sales_units = float(len(sales_rows))
    wholesale_units = float(len(wholesale_rows))
    retail_units = float(len(retail_rows))
    stock_in_units = float(len(stock_in_rows))
    return_units = float(len(return_rows))
    restock_units = float(sum(1 for row in return_rows if int(row.get("restock") or 0) == 1))

    sales_revenue = float(sum(float(row.get("sold_price") or 0) for row in sales_rows))
    wholesale_revenue = float(sum(float(row.get("sold_price") or 0) for row in wholesale_rows))
    retail_revenue = float(sum(float(row.get("sold_price") or 0) for row in retail_rows))
    paid_sales_total = float(sum(float(row.get("paid_amount") or 0) for row in sales_rows))
    due_generated = float(sum(float(row.get("due_amount") or 0) for row in sales_rows))
    sold_purchase_total = float(sum(float(row.get("purchase_price") or 0) for row in sales_rows))
    gross_profit = sales_revenue - sold_purchase_total

    return_loss = float(
        sum(
            max(0.0, float(row.get("sold_price") or 0) - float(row.get("purchase_price") or 0))
            for row in return_rows
        )
    )
    restock_purchase_total = float(
        sum(float(row.get("purchase_price") or 0) for row in return_rows if int(row.get("restock") or 0) == 1)
    )

    due_collected_total = float(sum(float(row.get("amount") or 0) for row in due_collection_rows))

    expense_approved = float(
        sum(float(row.get("amount") or 0) for row in expense_rows if str(row.get("approval_status")) == "APPROVED")
    )
    expense_pending = float(
        sum(float(row.get("amount") or 0) for row in expense_rows if str(row.get("approval_status")) == "PENDING")
    )
    expense_rejected = float(
        sum(float(row.get("amount") or 0) for row in expense_rows if str(row.get("approval_status")) == "REJECTED")
    )
    expense_cash_approved = float(
        sum(
            float(row.get("amount") or 0)
            for row in expense_rows
            if str(row.get("approval_status")) == "APPROVED"
            and str(row.get("payment_method") or "").upper() == "CASH"
        )
    )
    income_approved = float(
        sum(float(row.get("amount") or 0) for row in income_rows if str(row.get("approval_status")) == "APPROVED")
    )
    income_pending = float(
        sum(float(row.get("amount") or 0) for row in income_rows if str(row.get("approval_status")) == "PENDING")
    )
    income_rejected = float(
        sum(float(row.get("amount") or 0) for row in income_rows if str(row.get("approval_status")) == "REJECTED")
    )
    income_cash_approved = float(
        sum(
            float(row.get("amount") or 0)
            for row in income_rows
            if str(row.get("approval_status")) == "APPROVED"
            and str(row.get("payment_method") or "").upper() == "CASH"
        )
    )

    stock_in_cost = float(sum(float(row.get("purchase_price") or 0) for row in stock_in_rows))
    stock_in_wholesale_value = float(sum(float(row.get("wholesale_price") or 0) for row in stock_in_rows))

    closing_stock_units = float(query_scalar("SELECT COUNT(*) FROM products WHERE status = 'IN_STOCK'"))
    closing_stock_cost = float(
        query_scalar("SELECT COALESCE(SUM(purchase_price), 0) FROM products WHERE status = 'IN_STOCK'")
    )
    opening_stock_units = max(0.0, closing_stock_units + sales_units - stock_in_units - restock_units)
    opening_stock_cost = max(
        0.0,
        closing_stock_cost + sold_purchase_total - stock_in_cost - restock_purchase_total,
    )

    closing_due_outstanding = float(
        query_scalar("SELECT COALESCE(SUM(due_amount), 0) FROM sales WHERE is_active = 1 AND due_amount > 0")
    )

    opening_cash = float(sum(float(row.get("opening_cash") or 0) for row in petty_rows_dict))
    closing_cash = float(sum(float(row.get("closing_cash") or 0) for row in petty_rows_dict))
    cash_inflow = paid_sales_total + due_collected_total + income_cash_approved
    cash_outflow = expense_cash_approved
    expected_closing_cash = opening_cash + cash_inflow - cash_outflow
    shortage_overage = closing_cash - expected_closing_cash

    net_profit = gross_profit - expense_approved - return_loss
    net_profit_with_other_income = net_profit + income_approved
    net_cashflow = cash_inflow - cash_outflow

    top_due_shops = conn.execute(
        """
        SELECT
            c.shop_name,
            COALESCE(SUM(s.due_amount), 0) AS due_amount
        FROM sales s
        JOIN customers c ON c.id = s.customer_id
        WHERE s.is_active = 1
          AND s.sale_type = 'WHOLESALE'
          AND s.due_amount > 0
          AND c.shop_name <> ?
        GROUP BY c.id
        ORDER BY due_amount DESC, c.shop_name ASC
        LIMIT 5
        """,
        (LOCAL_RETAIL_WHOLESALE_SHOP_NAME,),
    ).fetchall()

    report_day_obj = parse_iso_date(report_date) or date.today()
    trend_start = (report_day_obj - timedelta(days=29)).isoformat()
    high_return_models = conn.execute(
        """
        SELECT
            p.brand,
            p.model,
            COALESCE(p.storage, '') AS storage,
            COUNT(*) AS return_count
        FROM sale_returns r
        JOIN sales s ON s.id = r.sale_id
        JOIN products p ON p.id = s.product_id
        WHERE r.return_date >= ? AND r.return_date <= ?
        GROUP BY UPPER(TRIM(p.brand)), UPPER(TRIM(p.model)), UPPER(TRIM(COALESCE(p.storage, '')))
        ORDER BY return_count DESC, p.brand ASC, p.model ASC
        LIMIT 5
        """,
        (trend_start, report_date),
    ).fetchall()

    return {
        "report_date": report_date,
        "kpis": {
            "opening_stock_units": opening_stock_units,
            "opening_stock_cost": opening_stock_cost,
            "stock_in_units": stock_in_units,
            "stock_in_cost": stock_in_cost,
            "stock_in_wholesale_value": stock_in_wholesale_value,
            "sales_units": sales_units,
            "wholesale_units": wholesale_units,
            "retail_units": retail_units,
            "sales_revenue": sales_revenue,
            "wholesale_revenue": wholesale_revenue,
            "retail_revenue": retail_revenue,
            "paid_sales_total": paid_sales_total,
            "due_generated": due_generated,
            "due_collected_total": due_collected_total,
            "return_units": return_units,
            "restock_units": restock_units,
            "return_loss": return_loss,
            "expense_approved": expense_approved,
            "expense_pending": expense_pending,
            "expense_rejected": expense_rejected,
            "expense_cash_approved": expense_cash_approved,
            "income_approved": income_approved,
            "income_pending": income_pending,
            "income_rejected": income_rejected,
            "income_cash_approved": income_cash_approved,
            "gross_profit": gross_profit,
            "net_profit": net_profit,
            "net_profit_with_other_income": net_profit_with_other_income,
            "cash_inflow": cash_inflow,
            "cash_outflow": cash_outflow,
            "net_cashflow": net_cashflow,
            "opening_cash": opening_cash,
            "closing_cash": closing_cash,
            "expected_closing_cash": expected_closing_cash,
            "shortage_overage": shortage_overage,
            "closing_due_outstanding": closing_due_outstanding,
            "closing_stock_units": closing_stock_units,
            "closing_stock_cost": closing_stock_cost,
        },
        "stock_in_rows": stock_in_rows,
        "sales_rows": sales_rows,
        "wholesale_rows": wholesale_rows,
        "retail_rows": retail_rows,
        "return_rows": return_rows,
        "due_collection_rows": due_collection_rows,
        "expense_rows": expense_rows,
        "income_rows": income_rows,
        "petty_rows": petty_rows_dict,
        "top_due_shops": [dict(row) for row in top_due_shops],
        "high_return_models": [dict(row) for row in high_return_models],
    }


def build_daily_health_signals(report_payload: dict[str, object]) -> dict[str, object]:
    kpis = report_payload.get("kpis", {})
    if not isinstance(kpis, dict):
        kpis = {}

    sales_units = float(kpis.get("sales_units", 0) or 0)
    net_profit = float(kpis.get("net_profit", 0) or 0)
    due_generated = float(kpis.get("due_generated", 0) or 0)
    due_collected = float(kpis.get("due_collected_total", 0) or 0)
    return_units = float(kpis.get("return_units", 0) or 0)
    pending_expense = float(kpis.get("expense_pending", 0) or 0)
    shortage_overage = float(kpis.get("shortage_overage", 0) or 0)
    opening_cash = float(kpis.get("opening_cash", 0) or 0)
    closing_stock_units = float(kpis.get("closing_stock_units", 0) or 0)
    cash_inflow = float(kpis.get("cash_inflow", 0) or 0)
    cash_outflow = float(kpis.get("cash_outflow", 0) or 0)

    score = 100
    signals: list[dict[str, str]] = []

    if sales_units <= 0:
        score -= 20
        signals.append(
            {
                "level": "warning",
                "title": "No sales logged today",
                "message": "No sale entry found for this date. Sales না থাকলে stock/cash reconciliation check করুন।",
            }
        )
    else:
        signals.append(
            {
                "level": "ok",
                "title": "Sales activity recorded",
                "message": f"Today total sales unit: {int(sales_units)}.",
            }
        )

    if net_profit < 0:
        score -= 28
        signals.append(
            {
                "level": "critical",
                "title": "Net profit is negative",
                "message": "Net profit নিচে নেমেছে। low-margin sale, return loss এবং expense re-check করুন।",
            }
        )
    elif net_profit < 1000:
        score -= 8
        signals.append(
            {
                "level": "warning",
                "title": "Net profit is low",
                "message": "Net profit কম। দামের strategy ও low margin model review করুন।",
            }
        )
    else:
        signals.append(
            {
                "level": "ok",
                "title": "Net profit is healthy",
                "message": "Today net profit positive and usable.",
            }
        )

    if due_generated > 0 and due_generated > (due_collected * 1.4):
        score -= 18
        signals.append(
            {
                "level": "warning",
                "title": "Due generation high vs collection",
                "message": "আজ নতুন due তুলনায় বেশি। শপভিত্তিক collection plan করুন।",
            }
        )
    elif due_generated > 0:
        signals.append(
            {
                "level": "ok",
                "title": "Due control acceptable",
                "message": "Due generate হয়েছে কিন্তু collection flow active আছে।",
            }
        )

    return_ratio = (return_units / sales_units * 100.0) if sales_units > 0 else 0.0
    if return_ratio >= 15.0:
        score -= 16
        signals.append(
            {
                "level": "warning",
                "title": "High return ratio",
                "message": f"Return ratio {return_ratio:.1f}%। model quality/verification pipeline tighten করুন।",
            }
        )

    if pending_expense > 0:
        score -= 8
        signals.append(
            {
                "level": "warning",
                "title": "Pending expense awaiting approval",
                "message": "Pending expense থাকলে net profit distorted হতে পারে। Admin approval করুন।",
            }
        )

    mismatch_ratio = (abs(shortage_overage) / opening_cash * 100.0) if opening_cash > 0 else 0.0
    if abs(shortage_overage) >= 1:
        if mismatch_ratio >= 5.0:
            score -= 18
            level = "critical"
            title = "Cash mismatch risk"
        else:
            score -= 8
            level = "warning"
            title = "Minor cash mismatch"
        signals.append(
            {
                "level": level,
                "title": title,
                "message": "Petty cash opening vs closing mismatch detect হয়েছে। cash out entries verify করুন।",
            }
        )
    else:
        signals.append(
            {
                "level": "ok",
                "title": "Cash matched",
                "message": "Opening/closing cash expected range-এর মধ্যে আছে।",
            }
        )

    if closing_stock_units <= 3:
        score -= 6
        signals.append(
            {
                "level": "warning",
                "title": "Low closing stock",
                "message": "Stock খুব কম। top selling models refill plan করুন।",
            }
        )

    if cash_outflow > cash_inflow and cash_outflow > 0:
        score -= 7
        signals.append(
            {
                "level": "warning",
                "title": "Cash outflow exceeded inflow",
                "message": "আজ cash-out বেশি হয়েছে। next-day working cash plan করুন।",
            }
        )

    if score < 0:
        score = 0
    if score > 100:
        score = 100

    if score >= 85:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 55:
        grade = "C"
    else:
        grade = "D"

    return {
        "score": int(round(score)),
        "grade": grade,
        "signals": signals[:10],
    }


def infer_business_module_from_product(category: str, brand: str = "", model: str = "") -> str:
    text = " ".join([category or "", brand or "", model or ""]).upper()
    if any(keyword in text for keyword in ["MEDICINE", "PHARMA", "CAPSULE", "SYRUP", "INJECTION"]):
        return "MEDICINE"
    if any(keyword in text for keyword in ["COSMETIC", "BEAUTY", "SKIN", "HAIR", "MAKEUP", "PERFUME"]):
        return "COSMETICS"
    if any(keyword in text for keyword in ["HARDWARE", "TOOL", "SCREW", "CABLE", "SWITCH", "BOLT"]):
        return "HARDWARE"
    if any(keyword in text for keyword in ["STATIONERY", "BOOK", "PEN", "PENCIL", "COPY", "NOTEBOOK"]):
        return "STATIONERY"
    if any(keyword in text for keyword in ["RESTAURANT", "FOOD", "MEAL", "KITCHEN", "DINING"]):
        return "RESTAURANT"
    if any(keyword in text for keyword in ["GROCERY", "RICE", "FOOD", "BEVERAGE", "SNACK", "DETERGENT"]):
        return "GROCERY"
    if any(keyword in text for keyword in ["CLOTH", "FASHION", "SHIRT", "PANT", "JEANS", "TSHIRT", "JACKET"]):
        return "CLOTHING"
    if any(keyword in text for keyword in ["LAPTOP", "TV", "FRIDGE", "AC", "ELECTRONIC", "HEADPHONE", "EARBUD"]):
        return "ELECTRONICS"
    return "MOBILE_WHOLESALE"


def resolve_expense_receipt_file(receipt_path: str) -> Path | None:
    raw = (receipt_path or "").strip()
    if not raw:
        return None
    filename = Path(raw.replace("\\", "/")).name
    if not filename:
        return None
    target = PRIVATE_EXPENSE_RECEIPT_DIR / filename
    if target.exists():
        return target
    return None


def save_expense_receipt(file_obj: FileStorage | None, prefix: str = "expense") -> str:
    if file_obj is None:
        return ""
    filename = secure_filename(file_obj.filename or "")
    if not filename:
        return ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Receipt photo must be JPG/PNG/WEBP.")

    PRIVATE_EXPENSE_RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    tenant_prefix = ""
    if has_request_context():
        tenant = get_current_tenant()
        if tenant is not None:
            tenant_prefix = f"t{int(tenant['id'])}-"

    generated_name = (
        f"{tenant_prefix}{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}.{ext}"
    )
    target = PRIVATE_EXPENSE_RECEIPT_DIR / generated_name
    file_obj.save(target)
    return generated_name


def resolve_profile_image_file(image_path: str) -> Path | None:
    raw = (image_path or "").strip()
    if not raw:
        return None
    filename = Path(raw.replace("\\", "/")).name
    if not filename:
        return None
    target = PRIVATE_PROFILE_UPLOAD_DIR / filename
    if target.exists():
        return target
    return None


def save_profile_image(file_obj: FileStorage | None, prefix: str = "profile") -> str:
    if file_obj is None:
        return ""
    filename = secure_filename(file_obj.filename or "")
    if not filename:
        return ""

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Profile image must be JPG/PNG/WEBP.")

    PRIVATE_PROFILE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tenant_prefix = ""
    if has_request_context():
        tenant = get_current_tenant()
        if tenant is not None:
            tenant_prefix = f"t{int(tenant['id'])}-"

    generated_name = (
        f"{tenant_prefix}{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}.{ext}"
    )
    target = PRIVATE_PROFILE_UPLOAD_DIR / generated_name
    file_obj.save(target)
    return generated_name


def delete_profile_image_file(image_path: str) -> None:
    target = resolve_profile_image_file(image_path)
    if target is None:
        return
    try:
        target.unlink()
    except OSError:
        return


def generate_monthly_recurring_expenses(db: sqlite3.Connection, run_for: date | None = None) -> int:
    ensure_expense_finance_tables(db)
    run_date = run_for or date.today()
    month_key = run_date.strftime("%Y-%m")
    templates = db.execute(
        """
        SELECT
            id, title, category, sub_category, employee_name, amount, payment_method, branch_id,
            day_of_month, note, is_active, last_generated_month
        FROM expense_recurring_templates
        WHERE is_active = 1
        ORDER BY id ASC
        """
    ).fetchall()

    created = 0
    for item in templates:
        last_generated = str(item["last_generated_month"] or "").strip()
        if last_generated == month_key:
            continue
        day_of_month = int(item["day_of_month"] or 1)
        day_of_month = max(1, min(day_of_month, 28))
        target_date = date(run_date.year, run_date.month, day_of_month)
        if target_date > run_date:
            continue

        db.execute(
            """
            INSERT INTO expenses (
                expense_date, category, sub_category, employee_name, amount, payment_method, branch_id,
                note, receipt_path, entered_by_user_id, entered_by_username,
                approval_status, approved_by_user_id, approved_at,
                rejected_note, is_recurring_source, recurring_template_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', NULL, 'system', 'APPROVED', NULL, ?, '', 1, ?, ?, ?)
            """,
            (
                target_date.isoformat(),
                str(item["category"] or "misc").strip().lower(),
                str(item["sub_category"] or "").strip(),
                str(item["employee_name"] or "").strip(),
                float(item["amount"] or 0),
                str(item["payment_method"] or "CASH").strip().upper(),
                int(item["branch_id"] or 1),
                str(item["note"] or item["title"] or "").strip(),
                now_sqlite_text(),
                int(item["id"]),
                now_sqlite_text(),
                now_sqlite_text(),
            ),
        )
        db.execute(
            """
            UPDATE expense_recurring_templates
            SET last_generated_month = ?, updated_at = ?
            WHERE id = ?
            """,
            (month_key, now_sqlite_text(), int(item["id"])),
        )
        created += 1

    if created:
        db.commit()
    return created


def generate_monthly_recurring_incomes(db: sqlite3.Connection, run_for: date | None = None) -> int:
    ensure_expense_finance_tables(db)
    run_date = run_for or date.today()
    month_key = run_date.strftime("%Y-%m")
    templates = db.execute(
        """
        SELECT
            id, title, category, sub_category, source_name, amount, payment_method, branch_id,
            day_of_month, note, is_active, last_generated_month
        FROM income_recurring_templates
        WHERE is_active = 1
        ORDER BY id ASC
        """
    ).fetchall()

    created = 0
    for item in templates:
        last_generated = str(item["last_generated_month"] or "").strip()
        if last_generated == month_key:
            continue
        day_of_month = int(item["day_of_month"] or 1)
        day_of_month = max(1, min(day_of_month, 28))
        target_date = date(run_date.year, run_date.month, day_of_month)
        if target_date > run_date:
            continue

        db.execute(
            """
            INSERT INTO incomes (
                income_date, category, sub_category, source_name, amount, payment_method, branch_id,
                note, receipt_path, entered_by_user_id, entered_by_username,
                approval_status, approved_by_user_id, approved_at,
                rejected_note, is_recurring_source, recurring_template_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', NULL, 'system', 'APPROVED', NULL, ?, '', 1, ?, ?, ?)
            """,
            (
                target_date.isoformat(),
                str(item["category"] or "other_income").strip().lower(),
                str(item["sub_category"] or "").strip(),
                str(item["source_name"] or "").strip(),
                float(item["amount"] or 0),
                str(item["payment_method"] or "CASH").strip().upper(),
                int(item["branch_id"] or 1),
                str(item["note"] or item["title"] or "").strip(),
                now_sqlite_text(),
                int(item["id"]),
                now_sqlite_text(),
                now_sqlite_text(),
            ),
        )
        db.execute(
            """
            UPDATE income_recurring_templates
            SET last_generated_month = ?, updated_at = ?
            WHERE id = ?
            """,
            (month_key, now_sqlite_text(), int(item["id"])),
        )
        created += 1

    if created:
        db.commit()
    return created


def normalize_imei_text(raw_text: str) -> list[str]:
    mode = get_current_tracking_mode()
    return normalize_tracking_text(raw_text, mode)


def get_or_create_local_retail_customer_id(conn: sqlite3.Connection) -> int:
    return get_or_create_customer(conn, LOCAL_RETAIL_WHOLESALE_SHOP_NAME)


def build_retail_invoice_no(conn: sqlite3.Connection, sold_date: str) -> str:
    date_part = sold_date.replace("-", "")
    prefix = f"RINV-{date_part}-"
    row = conn.execute(
        """
        SELECT invoice_no
        FROM retail_invoices
        WHERE invoice_no LIKE ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"{prefix}%",),
    ).fetchone()
    if row is None:
        return f"{prefix}001"
    last_invoice = str(row["invoice_no"] or "")
    match = re.search(r"(\d+)$", last_invoice)
    if not match:
        return f"{prefix}001"
    return f"{prefix}{(int(match.group(1)) + 1):03d}"


def build_sale_invoice_no(conn: sqlite3.Connection, sold_date: str, sale_type: str) -> str:
    clean_sale_type = "RETAIL" if str(sale_type or "").strip().upper() == "RETAIL" else "WHOLESALE"
    date_part = sold_date.replace("-", "")
    prefix = "RSL" if clean_sale_type == "RETAIL" else "WSL"
    invoice_prefix = f"{prefix}-{date_part}-"
    row = conn.execute(
        """
        SELECT invoice_no
        FROM sales
        WHERE sale_type = ?
          AND invoice_no LIKE ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (clean_sale_type, f"{invoice_prefix}%"),
    ).fetchone()
    if row is None:
        return f"{invoice_prefix}001"
    last_invoice = str(row["invoice_no"] or "")
    match = re.search(r"(\d+)$", last_invoice)
    if not match:
        return f"{invoice_prefix}001"
    return f"{invoice_prefix}{(int(match.group(1)) + 1):03d}"


def split_paid_amounts(total_paid: float, sold_prices: list[float]) -> list[float]:
    if not sold_prices:
        return []
    if total_paid <= 0:
        return [0.0 for _ in sold_prices]

    total_price = sum(sold_prices)
    if total_price <= 0:
        return [0.0 for _ in sold_prices]

    remaining_paid = round(total_paid, 2)
    allocations: list[float] = []
    for idx, price in enumerate(sold_prices):
        if idx == len(sold_prices) - 1:
            allocated = max(0.0, min(round(price, 2), round(remaining_paid, 2)))
        else:
            ratio = 0.0 if total_price <= 0 else (price / total_price)
            allocated = round(total_paid * ratio, 2)
            allocated = max(0.0, min(round(price, 2), allocated))
            if allocated > remaining_paid:
                allocated = round(remaining_paid, 2)
        allocations.append(allocated)
        remaining_paid = round(max(0.0, remaining_paid - allocated), 2)

    if remaining_paid > 0:
        for idx in range(len(allocations) - 1, -1, -1):
            room = round(max(0.0, sold_prices[idx] - allocations[idx]), 2)
            if room <= 0:
                continue
            add = round(min(room, remaining_paid), 2)
            allocations[idx] = round(allocations[idx] + add, 2)
            remaining_paid = round(max(0.0, remaining_paid - add), 2)
            if remaining_paid <= 0:
                break

    return allocations


def resolve_receiver_photo_file(photo_path: str) -> Path | None:
    raw = (photo_path or "").strip()
    if not raw:
        return None

    normalized = raw.replace("\\", "/").lstrip("/")
    if normalized.startswith("uploads/receivers/"):
        legacy_file = BASE_DIR / "static" / normalized
        if legacy_file.exists():
            return legacy_file
        migrated_file = PRIVATE_RECEIVER_UPLOAD_DIR / Path(normalized).name
        if migrated_file.exists():
            return migrated_file
        return None

    filename = Path(normalized).name
    if not filename:
        return None

    private_file = PRIVATE_RECEIVER_UPLOAD_DIR / filename
    if private_file.exists():
        return private_file

    legacy_file = LEGACY_RECEIVER_UPLOAD_DIR / filename
    if legacy_file.exists():
        return legacy_file
    return None


def migrate_receiver_photo_storage(conn: sqlite3.Connection) -> bool:
    if "receiver_photo_path" not in get_table_columns(conn, "sales"):
        return False

    rows = conn.execute(
        """
        SELECT id, receiver_photo_path
        FROM sales
        WHERE receiver_photo_path IS NOT NULL
          AND TRIM(receiver_photo_path) <> ''
        """
    ).fetchall()
    if not rows:
        return False

    PRIVATE_RECEIVER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    changed = False

    for row in rows:
        raw = str(row["receiver_photo_path"] or "").strip()
        if not raw:
            continue

        normalized = raw.replace("\\", "/").lstrip("/")
        filename = Path(normalized).name
        if not filename:
            continue

        private_target = PRIVATE_RECEIVER_UPLOAD_DIR / filename
        if normalized.startswith("uploads/receivers/"):
            legacy_source = BASE_DIR / "static" / normalized
            if legacy_source.exists():
                if not private_target.exists():
                    legacy_source.replace(private_target)
                else:
                    try:
                        legacy_source.unlink()
                    except OSError:
                        pass
            if private_target.exists() and raw != filename:
                conn.execute("UPDATE sales SET receiver_photo_path = ? WHERE id = ?", (filename, row["id"]))
                changed = True
            continue

        if raw != filename:
            if private_target.exists():
                conn.execute("UPDATE sales SET receiver_photo_path = ? WHERE id = ?", (filename, row["id"]))
                changed = True
            continue

        legacy_source = LEGACY_RECEIVER_UPLOAD_DIR / filename
        if legacy_source.exists() and not private_target.exists():
            legacy_source.replace(private_target)
            changed = True

    return changed


def save_receiver_photo(file_obj: FileStorage | None, prefix: str = "receiver") -> str:
    if file_obj is None:
        return ""
    filename = secure_filename(file_obj.filename or "")
    if not filename:
        return ""

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Receiver photo must be JPG/PNG/WEBP.")

    PRIVATE_RECEIVER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tenant_prefix = ""
    if has_request_context():
        tenant = get_current_tenant()
        if tenant is not None:
            tenant_prefix = f"t{int(tenant['id'])}-"

    generated_name = (
        f"{tenant_prefix}{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}.{ext}"
    )
    target = PRIVATE_RECEIVER_UPLOAD_DIR / generated_name
    file_obj.save(target)
    return generated_name


def normalize_memory_value_to_gb(raw_value: str, *, allow_tb: bool = True) -> str:
    token = re.sub(r"\s+", "", str(raw_value or "").upper())
    if not token:
        return ""
    token = token.replace("ＧＢ", "GB").replace("ＴＢ", "TB")

    if token.endswith("GB"):
        token = token[:-2]
    elif token.endswith("G"):
        token = token[:-1]
    elif token.endswith("TB"):
        if not allow_tb:
            return ""
        try:
            tb_value = float(token[:-2] or "0")
        except ValueError:
            return ""
        if tb_value <= 0:
            return ""
        gb_value = int(round(tb_value * 1024))
        if gb_value <= 0 or gb_value > 4096:
            return ""
        return str(gb_value)

    if not re.fullmatch(r"\d{1,4}", token):
        return ""

    gb_value = int(token)
    if gb_value <= 0 or gb_value > 4096:
        return ""
    return str(gb_value)


def infer_storage_variant(raw_text: str, *, iphone_only_storage: bool = False) -> str:
    text = str(raw_text or "").upper()
    if not text:
        return ""

    compact_text = re.sub(r"[|]", "/", text)
    compact_text = re.sub(r"\s+", " ", compact_text).strip()
    storage_candidates: list[int] = []

    def add_storage_candidate(raw_value: str) -> None:
        normalized = normalize_memory_value_to_gb(raw_value)
        if not normalized:
            return
        value = int(normalized)
        if value not in storage_candidates:
            storage_candidates.append(value)

    dual_patterns: list[tuple[str, int, int]] = [
        (
            r"\bRAM\s*[:=]?\s*(\d{1,2}(?:\.\d+)?\s*(?:GB|G)?)\b.{0,24}?\b(?:ROM|STORAGE|MEMORY)\s*[:=]?\s*(\d{1,4}(?:\.\d+)?\s*(?:GB|G|TB)?)\b",
            1,
            2,
        ),
        (
            r"\b(?:ROM|STORAGE|MEMORY)\s*[:=]?\s*(\d{1,4}(?:\.\d+)?\s*(?:GB|G|TB)?)\b.{0,24}?\bRAM\s*[:=]?\s*(\d{1,2}(?:\.\d+)?\s*(?:GB|G)?)\b",
            2,
            1,
        ),
        (
            r"\b(\d{1,2}(?:\.\d+)?\s*(?:GB|G)?)\s*[/+X]\s*(\d{1,4}(?:\.\d+)?\s*(?:GB|G|TB)?)\b",
            1,
            2,
        ),
    ]
    for pattern, ram_index, rom_index in dual_patterns:
        match = re.search(pattern, compact_text, flags=re.DOTALL)
        if not match:
            continue
        ram_value = normalize_memory_value_to_gb(match.group(ram_index), allow_tb=False)
        rom_value = normalize_memory_value_to_gb(match.group(rom_index))
        if not ram_value or not rom_value:
            continue
        ram_gb = int(ram_value)
        rom_gb = int(rom_value)
        if ram_gb > 64 or rom_gb < 16:
            continue
        if iphone_only_storage:
            return f"{rom_gb}GB"
        return f"{ram_gb}/{rom_gb}GB"

    ram_match = re.search(r"\bRAM\s*[:=]?\s*(\d{1,2}(?:\.\d+)?\s*(?:GB|G)?)\b", compact_text)
    rom_match = re.search(r"\b(?:ROM|STORAGE|MEMORY)\s*[:=]?\s*(\d{1,4}(?:\.\d+)?\s*(?:GB|G|TB)?)\b", compact_text)
    if ram_match:
        ram_value = normalize_memory_value_to_gb(ram_match.group(1), allow_tb=False)
    else:
        ram_value = ""
    if rom_match:
        rom_value = normalize_memory_value_to_gb(rom_match.group(1))
        if rom_value:
            add_storage_candidate(rom_value)
            if ram_value and not iphone_only_storage:
                ram_gb = int(ram_value)
                rom_gb = int(rom_value)
                if ram_gb <= 64 and rom_gb >= 16:
                    return f"{ram_gb}/{rom_gb}GB"

    for found in re.findall(r"\b(\d{1,4}(?:\.\d+)?\s*(?:GB|G|TB))\b", compact_text):
        add_storage_candidate(found)

    if iphone_only_storage:
        for found in re.findall(r"\b(32|64|128|256|512|1024|2048)\b", compact_text):
            add_storage_candidate(found)
        for candidate in storage_candidates:
            if candidate in IPHONE_STORAGE_GB_VALUES:
                return f"{candidate}GB"
        if storage_candidates:
            return f"{storage_candidates[0]}GB"
        return ""

    if ram_value and storage_candidates:
        ram_gb = int(ram_value)
        if ram_gb <= 64:
            for candidate in storage_candidates:
                if candidate >= max(16, ram_gb):
                    return f"{ram_gb}/{candidate}GB"

    if storage_candidates:
        return f"{storage_candidates[0]}GB"
    return ""


def is_plausible_model_name(raw_value: str) -> bool:
    value = re.sub(r"\s+", " ", str(raw_value or "").strip())
    if not value:
        return False
    if len(value) < 2 or len(value) > 48:
        return False
    if re.fullmatch(r"\d+", value):
        return False

    upper = value.upper()
    blocked_tokens = {
        "IMEI",
        "SERIAL",
        "SKU",
        "PURCHASE",
        "WHOLESALE",
        "RETAIL",
        "PRICE",
        "MRP",
        "BDT",
        "TK",
        "COST",
        "BUY",
        "RAM",
        "ROM",
        "STORAGE",
        "MODEL",
    }
    for token in blocked_tokens:
        if re.search(rf"\b{re.escape(token)}\b", upper):
            return False

    tokens = value.split()
    if len(tokens) > 5:
        return False
    if not re.search(r"[A-Za-z]", value):
        return False

    alnum = re.sub(r"[^A-Za-z0-9]", "", value)
    if alnum:
        digit_ratio = sum(ch.isdigit() for ch in alnum) / len(alnum)
        if digit_ratio > 0.75:
            return False

    return True


def infer_product_fields_from_text(raw_text: str) -> dict[str, str]:
    text = (raw_text or "").upper()
    iphone_context = bool(re.search(r"\bIPHONE\b", text))

    brand = ""
    model = ""
    storage = ""
    color = ""
    category = ""
    warranty_type = ""
    purchase_price = ""
    wholesale_price = ""
    retail_price = ""

    if re.search(r"\bOFFICIAL\b", text):
        warranty_type = "OFFICIAL"
    elif re.search(r"\bUNOFFICIAL\b|\bNO WARRANTY\b|\bWITHOUT WARRANTY\b", text):
        warranty_type = "UNOFFICIAL"

    for keyword, pretty_name in BRAND_KEYWORDS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            brand = pretty_name
            break
    if iphone_context and not brand:
        brand = "Apple"

    for keyword in COLOR_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            color = keyword.title()
            if color == "Grey":
                color = "Gray"
            break

    for category_name, keywords in CATEGORY_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in keywords):
            category = category_name
            break

    storage = infer_storage_variant(raw_text, iphone_only_storage=iphone_context)

    lines = [line.strip() for line in re.split(r"[\n\r]+", text) if line.strip()]
    if brand:
        for line in lines:
            if brand.upper() in line:
                cleaned = line.replace(brand.upper(), "").strip(" -_|:")
                cleaned = re.sub(
                    r"\b(IMEI|RAM|ROM|COLOR|COLOUR|MODEL|PURCHASE|WHOLESALE|RETAIL|PRICE|MRP|BUY|COST|BDT|TK)\b",
                    "",
                    cleaned,
                ).strip()
                if storage:
                    cleaned = cleaned.replace(storage.upper(), " ")
                if color:
                    cleaned = cleaned.replace(color.upper(), " ")
                cleaned = re.sub(r"\b\d{1,2}\s*/\s*\d{2,4}\s*(?:GB|G|TB)?\b", " ", cleaned)
                cleaned = re.sub(r"\b\d{2,4}\s*(?:GB|G|TB)\b", " ", cleaned)
                cleaned = re.sub(r"\b\d{1,2}\b$", " ", cleaned)
                cleaned = re.sub(r"\b\d{4,8}\b", " ", cleaned)
                cleaned = re.sub(r"\s+", " ", cleaned).strip()
                if cleaned:
                    model_tokens = cleaned.split()
                    model = " ".join(model_tokens[:4]).title()
                    break

    if model and not is_plausible_model_name(model):
        model = ""

    if not model:
        if brand:
            brand_pattern = re.escape(brand.upper())
            brand_match = re.search(
                rf"\b{brand_pattern}\b\s+([A-Z0-9][A-Z0-9+._-]*(?:\s+[A-Z0-9][A-Z0-9+._-]*){{0,4}})",
                text,
            )
            if brand_match:
                candidate_model = str(brand_match.group(1) or "").strip()
                candidate_model = re.split(
                    r"\b(IMEI|RAM|ROM|STORAGE|MEMORY|COLOR|COLOUR|PURCHASE|WHOLESALE|RETAIL|PRICE|MRP|BUY|COST|BDT|TK)\b",
                    candidate_model,
                    maxsplit=1,
                )[0].strip()
                if storage:
                    candidate_model = candidate_model.replace(storage.upper(), " ")
                if color:
                    candidate_model = candidate_model.replace(color.upper(), " ")
                candidate_model = re.sub(r"\b\d{1,2}\s*/\s*\d{2,4}\s*(?:GB|G|TB)?\b", " ", candidate_model)
                candidate_model = re.sub(r"\b\d{2,4}\s*(?:GB|G|TB)\b", " ", candidate_model)
                candidate_model = re.sub(r"\b\d{1,2}\b$", " ", candidate_model)
                candidate_model = re.sub(r"\s+", " ", candidate_model).strip()
                if candidate_model:
                    candidate_model = " ".join(candidate_model.split()[:4]).title()
                    if is_plausible_model_name(candidate_model):
                        model = candidate_model

    if not model:
        candidate = ""
        for line in lines:
            if "IMEI" in line:
                continue
            if re.search(r"[A-Z]", line) and re.search(r"\d", line):
                candidate = line
                break
        if candidate:
            candidate = re.sub(
                r"\b(RAM|ROM|COLOR|COLOUR|PURCHASE|WHOLESALE|RETAIL|PRICE|MRP|BUY|COST|BDT|TK)\b.*",
                "",
                candidate,
            ).strip()
            if storage:
                candidate = candidate.replace(storage.upper(), " ")
            if color:
                candidate = candidate.replace(color.upper(), " ")
            candidate = re.sub(r"\b\d{1,2}\s*/\s*\d{2,4}\s*(?:GB|G|TB)?\b", " ", candidate)
            candidate = re.sub(r"\b\d{2,4}\s*(?:GB|G|TB)\b", " ", candidate)
            candidate = re.sub(r"\b\d{1,2}\b$", " ", candidate)
            candidate = re.sub(r"\b\d{4,8}\b", " ", candidate)
            candidate = re.sub(r"\s+", " ", candidate).strip()
            model = " ".join(candidate.split()[:4]).title()

    def extract_labeled_amount(label_patterns: list[str]) -> str:
        for label in label_patterns:
            pattern = rf"{label}\s*[:=/-]?\s*([0-9][0-9,]{{2,8}}(?:\.[0-9]{{1,2}})?)"
            match = re.search(pattern, text)
            if not match:
                continue
            raw_amount = str(match.group(1) or "").replace(",", "")
            try:
                amount = float(raw_amount)
            except ValueError:
                continue
            if amount < 0:
                continue
            return f"{amount:.2f}"
        return ""

    purchase_price = extract_labeled_amount([r"PURCHASE", r"BUY", r"COST", r"\bPP\b"])
    wholesale_price = extract_labeled_amount([r"WHOLESALE", r"DEALER", r"\bW(?:HOLE)?\b"])
    retail_price = extract_labeled_amount([r"RETAIL", r"SELL", r"MRP", r"\bR(?:ETAIL)?\b"])

    if not (purchase_price and wholesale_price and retail_price):
        generic_numbers = []
        for found in re.findall(r"\b([1-9][0-9,]{2,8}(?:\.[0-9]{1,2})?)\b", text):
            value_text = str(found).replace(",", "")
            try:
                amount = float(value_text)
            except ValueError:
                continue
            if amount < 500 or amount > 500000:
                continue
            if len(str(int(amount))) >= 7:
                continue
            generic_numbers.append(f"{amount:.2f}")
        unique_generic = list(dict.fromkeys(generic_numbers))
        if len(unique_generic) >= 3:
            if not purchase_price:
                purchase_price = unique_generic[0]
            if not wholesale_price:
                wholesale_price = unique_generic[1]
            if not retail_price:
                retail_price = unique_generic[2]

    if model and not is_plausible_model_name(model):
        model = ""

    return {
        "brand": brand,
        "model": model,
        "storage": storage,
        "color": color,
        "category": category,
        "warranty_type": warranty_type,
        "purchase_price": purchase_price,
        "wholesale_price": wholesale_price,
        "retail_price": retail_price,
        "supplier_id": "",
    }


def parse_model_keyword_list(raw_keywords: str) -> list[str]:
    tokens = re.split(r"[,\n;|]+", raw_keywords or "")
    cleaned: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        normalized = re.sub(r"\s+", " ", token.strip().upper())
        if len(normalized) < 2:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    return cleaned


def build_model_catalog_note(
    model_number: str = "",
    region: str = "",
    condition_state: str = "",
    extra_info: str = "",
) -> str:
    parts: list[str] = []
    clean_model_number = (model_number or "").strip()
    clean_region = (region or "").strip()
    clean_condition = normalize_model_condition(condition_state or "")
    clean_extra = (extra_info or "").strip()

    if clean_model_number:
        parts.append(f"Model No: {clean_model_number}")
    if clean_region:
        parts.append(f"Region: {clean_region}")
    if clean_condition:
        parts.append(f"Condition: {clean_condition}")
    if clean_extra:
        parts.append(clean_extra)
    return " | ".join(parts)


def find_model_catalog_match(
    conn: sqlite3.Connection,
    raw_text: str,
    detected_codes: list[str],
) -> tuple[sqlite3.Row | None, int, bool]:
    ensure_model_catalog_table(conn)
    text_upper = str(raw_text or "").upper()
    if not text_upper and not detected_codes:
        return None, 0, False

    rows = conn.execute(
        """
        SELECT
            id, brand, model_name, model_number, storage, region, color,
            condition_state, category, tac_prefix, keywords, extra_info,
            purchase_price, wholesale_price, retail_price, supplier_id
        FROM model_catalog
        WHERE is_active = 1
        ORDER BY id DESC
        LIMIT 1200
        """
    ).fetchall()

    best_row: sqlite3.Row | None = None
    best_score = 0
    best_has_tac = False
    best_has_brand = False
    best_has_model = False
    best_has_model_number = False
    digit_codes = [re.sub(r"\D", "", str(item or "")) for item in (detected_codes or [])]

    for row in rows:
        score = 0
        has_tac_match = False

        tac_prefix = normalize_tac_prefix(str(row["tac_prefix"] or ""))
        if tac_prefix:
            for item in digit_codes:
                if item.startswith(tac_prefix):
                    has_tac_match = True
                    score += 260
                    break

        brand = str(row["brand"] or "").strip().upper()
        model_name = str(row["model_name"] or "").strip().upper()
        model_number = str(row["model_number"] or "").strip().upper()
        storage = str(row["storage"] or "").strip()
        color = str(row["color"] or "").strip().upper()
        region = str(row["region"] or "").strip().upper()

        has_brand = False
        has_model = False
        has_model_number = False

        if brand and re.search(rf"\b{re.escape(brand)}\b", text_upper):
            has_brand = True
            score += 46
        if model_name and model_name in text_upper:
            has_model = True
            score += 82
        if model_number and model_number in text_upper:
            has_model_number = True
            score += 58
        if color and re.search(rf"\b{re.escape(color)}\b", text_upper):
            score += 12
        if region and re.search(rf"\b{re.escape(region)}\b", text_upper):
            score += 10

        if storage:
            normalized_storage = infer_storage_variant(
                storage,
                iphone_only_storage=("APPLE" in brand or "IPHONE" in model_name),
            )
            if normalized_storage and normalized_storage.upper() in text_upper:
                score += 22
            elif storage.upper() in text_upper:
                score += 14

        for token in parse_model_keyword_list(str(row["keywords"] or ""))[:10]:
            if token and token in text_upper:
                score += 12

        if score > best_score:
            best_score = score
            best_row = row
            best_has_tac = has_tac_match
            best_has_brand = has_brand
            best_has_model = has_model
            best_has_model_number = has_model_number

    if best_row is None:
        return None, 0, False
    if best_has_tac:
        return best_row, best_score, True
    if best_score >= 120 and (best_has_model or best_has_model_number):
        return best_row, best_score, False
    if best_score >= 96 and best_has_brand and (best_has_model or best_has_model_number):
        return best_row, best_score, False
    return None, best_score, False


def infer_supplier_id_from_text(conn: sqlite3.Connection, raw_text: str) -> int | None:
    text = str(raw_text or "").upper()
    if not text:
        return None

    suppliers = conn.execute(
        """
        SELECT id, name
        FROM suppliers
        ORDER BY LENGTH(TRIM(name)) DESC, id DESC
        """
    ).fetchall()
    for row in suppliers:
        name = str(row["name"] or "").strip()
        if not name:
            continue
        if name.upper() in text:
            return int(row["id"])
    return None


def find_recent_product_profile(
    conn: sqlite3.Connection,
    *,
    brand: str = "",
    model: str = "",
    storage: str = "",
    color: str = "",
) -> sqlite3.Row | None:
    clean_brand = (brand or "").strip()
    clean_model = (model or "").strip()
    clean_storage = (storage or "").strip()
    clean_color = (color or "").strip()

    if not clean_brand and not clean_model:
        return None

    row = conn.execute(
        """
        SELECT
            p.*,
            s.name AS supplier_name,
            (
                CASE WHEN ? <> '' AND UPPER(p.brand) = UPPER(?) THEN 50 ELSE 0 END +
                CASE WHEN ? <> '' AND UPPER(p.model) LIKE '%' || UPPER(?) || '%' THEN 40 ELSE 0 END +
                CASE WHEN ? <> '' AND UPPER(COALESCE(p.storage, '')) = UPPER(?) THEN 6 ELSE 0 END +
                CASE WHEN ? <> '' AND UPPER(COALESCE(p.color, '')) = UPPER(?) THEN 4 ELSE 0 END
            ) AS score
        FROM products p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        WHERE
            (? = '' OR UPPER(p.brand) = UPPER(?))
            AND
            (? = '' OR UPPER(p.model) LIKE '%' || UPPER(?) || '%')
        ORDER BY score DESC, p.id DESC
        LIMIT 1
        """,
        (
            clean_brand,
            clean_brand,
            clean_model,
            clean_model,
            clean_storage,
            clean_storage,
            clean_color,
            clean_color,
            clean_brand,
            clean_brand,
            clean_model,
            clean_model,
        ),
    ).fetchone()
    return row


def backup_file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_safe_backup_archive_path(relative_path: str) -> bool:
    normalized = (relative_path or "").replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return False
    candidate = Path(normalized)
    if candidate.is_absolute():
        return False
    if any(part in {"..", ""} for part in candidate.parts):
        return False
    return True


def extract_backup_archive_member(archive: zipfile.ZipFile, member_name: str, target_root: Path) -> Path:
    normalized = (member_name or "").replace("\\", "/").strip().lstrip("/")
    if not is_safe_backup_archive_path(normalized):
        raise ValueError("Backup package contains unsafe file path.")
    target_path = target_root / normalized
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member_name, "r") as source, target_path.open("wb") as target:
        shutil.copyfileobj(source, target)
    return target_path


def log_backup_file(
    target_db: Path,
    backup_path: Path,
    trigger_type: str,
    google_status: str = "NOT_SENT",
    google_file_id: str | None = None,
) -> None:
    file_size = backup_path.stat().st_size if backup_path.exists() else 0
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(target_db) as conn:
        conn.execute(
            """
            INSERT INTO backup_logs (
                filename, local_path, created_at, trigger_type, file_size, google_status, google_file_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                backup_path.name,
                str(backup_path),
                created_at,
                trigger_type,
                file_size,
                google_status,
                google_file_id,
            ),
        )
        conn.commit()


def compute_next_backup_run(
    settings: sqlite3.Row | dict[str, object] | None,
    *,
    reference_dt: datetime | None = None,
) -> datetime | None:
    if row_value(settings, "is_enabled", "0") != "1":
        return None

    now_dt = reference_dt or datetime.now()
    frequency = normalize_backup_schedule_frequency(row_value(settings, "frequency", "DAILY"))
    hour = clamp_backup_schedule_hour(row_value(settings, "run_hour", "3"), default=3)
    minute = clamp_backup_schedule_minute(row_value(settings, "run_minute", "0"), default=0)
    weekly_day = normalize_backup_schedule_weekday(row_value(settings, "weekly_day", "SUN"))
    monthly_day = clamp_backup_schedule_month_day(row_value(settings, "monthly_day", "1"), default=1)

    if frequency == "DAILY":
        candidate = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_dt:
            candidate += timedelta(days=1)
        return candidate

    if frequency == "WEEKLY":
        target_weekday = BACKUP_SCHEDULE_WEEKDAY_INDEX.get(weekly_day, 6)
        candidate = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now_dt:
            candidate += timedelta(days=7)
        return candidate

    def build_month_candidate(year: int, month: int) -> datetime:
        last_day = calendar.monthrange(year, month)[1]
        return datetime(year, month, min(monthly_day, last_day), hour, minute)

    candidate = build_month_candidate(now_dt.year, now_dt.month)
    if candidate <= now_dt:
        next_month = now_dt.month + 1
        next_year = now_dt.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        candidate = build_month_candidate(next_year, next_month)
    return candidate


def save_backup_schedule_settings(
    conn: sqlite3.Connection,
    *,
    is_enabled: bool,
    backup_type: str,
    frequency: str,
    run_hour: int,
    run_minute: int,
    weekly_day: str,
    monthly_day: int,
    sync_google: bool,
    last_run_at: str | None = None,
    next_run_at: str | None = None,
    last_status: str | None = None,
    last_filename: str | None = None,
    last_error: str | None = None,
) -> None:
    ensure_backup_schedule_table(conn)
    conn.execute(
        """
        UPDATE backup_schedule_settings
        SET is_enabled = ?,
            backup_type = ?,
            frequency = ?,
            run_hour = ?,
            run_minute = ?,
            weekly_day = ?,
            monthly_day = ?,
            sync_google = ?,
            last_run_at = COALESCE(?, last_run_at),
            next_run_at = ?,
            last_status = COALESCE(?, last_status),
            last_filename = COALESCE(?, last_filename),
            last_error = ?,
            updated_at = ?
        WHERE id = 1
        """,
        (
            1 if is_enabled else 0,
            normalize_backup_schedule_type(backup_type),
            normalize_backup_schedule_frequency(frequency),
            clamp_backup_schedule_hour(run_hour),
            clamp_backup_schedule_minute(run_minute),
            normalize_backup_schedule_weekday(weekly_day),
            clamp_backup_schedule_month_day(monthly_day),
            1 if sync_google else 0,
            last_run_at,
            next_run_at,
            last_status,
            last_filename,
            last_error,
            now_sqlite_text(),
        ),
    )


def execute_scheduled_backup(
    settings: sqlite3.Row | dict[str, object] | None,
) -> tuple[Path, str, str]:
    backup_type = normalize_backup_schedule_type(row_value(settings, "backup_type", "PACKAGE"))
    sync_google = row_value(settings, "sync_google", "0") == "1"
    if backup_type == "DB_COPY":
        return create_database_backup(
            trigger_type="AUTO_SCHEDULED",
            sync_google=sync_google,
            db_path=get_current_db_path(),
        )
    return create_tenant_backup_package(
        trigger_type="AUTO_SCHEDULED_PACKAGE",
        sync_google=sync_google,
    )


def maybe_run_scheduled_tenant_backup(conn: sqlite3.Connection) -> None:
    if not has_request_context():
        return
    if request.method not in {"GET", "HEAD"}:
        return
    if get_current_tenant() is None:
        return

    settings = get_backup_schedule_settings(conn)
    if row_value(settings, "is_enabled", "0") != "1":
        return

    now_dt = datetime.now()
    next_run_raw = row_value(settings, "next_run_at", "")
    next_run_dt = parse_datetime(next_run_raw)

    if next_run_dt is None:
        next_run_dt = compute_next_backup_run(settings, reference_dt=now_dt)
        conn.execute(
            """
            UPDATE backup_schedule_settings
            SET next_run_at = ?, updated_at = ?
            WHERE id = 1
            """,
            (next_run_dt.strftime("%Y-%m-%d %H:%M:%S") if next_run_dt is not None else None, now_sqlite_text()),
        )
        conn.commit()

    if next_run_dt is None or next_run_dt > now_dt:
        return

    conn.commit()
    last_run_at = now_sqlite_text()
    next_after_run = compute_next_backup_run(settings, reference_dt=now_dt + timedelta(minutes=1))
    next_run_text = next_after_run.strftime("%Y-%m-%d %H:%M:%S") if next_after_run is not None else None

    try:
        backup_path, google_status, google_message = execute_scheduled_backup(settings)
        save_backup_schedule_settings(
            conn,
            is_enabled=True,
            backup_type=row_value(settings, "backup_type", "PACKAGE"),
            frequency=row_value(settings, "frequency", "DAILY"),
            run_hour=clamp_backup_schedule_hour(row_value(settings, "run_hour", "3")),
            run_minute=clamp_backup_schedule_minute(row_value(settings, "run_minute", "0")),
            weekly_day=row_value(settings, "weekly_day", "SUN"),
            monthly_day=clamp_backup_schedule_month_day(row_value(settings, "monthly_day", "1")),
            sync_google=row_value(settings, "sync_google", "0") == "1",
            last_run_at=last_run_at,
            next_run_at=next_run_text,
            last_status=f"SUCCESS ({google_status})",
            last_filename=backup_path.name,
            last_error="",
        )
        conn.commit()
        return
    except Exception as exc:
        save_backup_schedule_settings(
            conn,
            is_enabled=True,
            backup_type=row_value(settings, "backup_type", "PACKAGE"),
            frequency=row_value(settings, "frequency", "DAILY"),
            run_hour=clamp_backup_schedule_hour(row_value(settings, "run_hour", "3")),
            run_minute=clamp_backup_schedule_minute(row_value(settings, "run_minute", "0")),
            weekly_day=row_value(settings, "weekly_day", "SUN"),
            monthly_day=clamp_backup_schedule_month_day(row_value(settings, "monthly_day", "1")),
            sync_google=row_value(settings, "sync_google", "0") == "1",
            last_run_at=last_run_at,
            next_run_at=next_run_text,
            last_status="FAILED",
            last_filename="",
            last_error=str(exc)[:400],
        )
        conn.commit()


def upload_backup_to_google_drive(file_path: Path, mime_type: str | None = None) -> tuple[str, str | None, str]:
    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()

    if not service_account_file or not folder_id:
        return "NOT_CONFIGURED", None, "Google config not set."

    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError:
        return "LIB_MISSING", None, "Install google-api-python-client and google-auth."

    try:
        scopes = ["https://www.googleapis.com/auth/drive.file"]
        creds = service_account.Credentials.from_service_account_file(service_account_file, scopes=scopes)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        metadata = {"name": file_path.name, "parents": [folder_id]}
        final_mime_type = mime_type or ("application/zip" if file_path.suffix.lower() == ".zip" else "application/x-sqlite3")
        media = MediaFileUpload(str(file_path), mimetype=final_mime_type, resumable=False)
        created = service.files().create(body=metadata, media_body=media, fields="id").execute()
        file_id = created.get("id")
        if file_id:
            return "SYNCED", file_id, "Uploaded to Google Drive."
        return "FAILED", None, "Upload finished without file id."
    except Exception as exc:  # pragma: no cover
        return "FAILED", None, str(exc)


def collect_tenant_backup_assets(
    tenant: sqlite3.Row | dict[str, object] | None,
    tenant_db: sqlite3.Connection,
) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    def register_file(kind: str, source_path: Path | None, archive_dir: str) -> None:
        if source_path is None or not source_path.exists():
            return
        resolved_key = str(source_path.resolve())
        if resolved_key in seen_paths:
            return
        seen_paths.add(resolved_key)
        collected.append(
            {
                "kind": kind,
                "source_path": str(source_path),
                "archive_path": f"{archive_dir}/{source_path.name}",
                "filename": source_path.name,
            }
        )

    profile_path = resolve_profile_image_file(row_value(tenant, "profile_image_path", ""))
    register_file("profile_image", profile_path, "uploads/profile")

    if table_exists(tenant_db, "expenses") and "receipt_path" in get_table_columns(tenant_db, "expenses"):
        rows = tenant_db.execute(
            """
            SELECT DISTINCT receipt_path
            FROM expenses
            WHERE receipt_path IS NOT NULL AND TRIM(receipt_path) <> ''
            """
        ).fetchall()
        for row in rows:
            register_file(
                "expense_receipt",
                resolve_expense_receipt_file(str(row["receipt_path"] or "")),
                "uploads/expense_receipts",
            )

    if table_exists(tenant_db, "incomes") and "receipt_path" in get_table_columns(tenant_db, "incomes"):
        rows = tenant_db.execute(
            """
            SELECT DISTINCT receipt_path
            FROM incomes
            WHERE receipt_path IS NOT NULL AND TRIM(receipt_path) <> ''
            """
        ).fetchall()
        for row in rows:
            register_file(
                "income_receipt",
                resolve_expense_receipt_file(str(row["receipt_path"] or "")),
                "uploads/expense_receipts",
            )

    if table_exists(tenant_db, "sales") and "receiver_photo_path" in get_table_columns(tenant_db, "sales"):
        rows = tenant_db.execute(
            """
            SELECT DISTINCT receiver_photo_path
            FROM sales
            WHERE receiver_photo_path IS NOT NULL AND TRIM(receiver_photo_path) <> ''
            """
        ).fetchall()
        for row in rows:
            register_file(
                "receiver_photo",
                resolve_receiver_photo_file(str(row["receiver_photo_path"] or "")),
                "uploads/receivers",
            )

    return collected


def build_tenant_backup_manifest(
    tenant: sqlite3.Row | dict[str, object],
    tenant_db_path: Path,
    tenant_db_copy_path: Path,
    tenant_db: sqlite3.Connection,
    asset_entries: list[dict[str, str]],
) -> dict[str, object]:
    summary_tables = [
        "users",
        "branches",
        "products",
        "sales",
        "sale_returns",
        "customers",
        "retail_customers",
        "suppliers",
        "expenses",
        "incomes",
    ]
    summary_counts: dict[str, int] = {}
    for table_name in summary_tables:
        if not table_exists(tenant_db, table_name):
            continue
        try:
            row = tenant_db.execute(f"SELECT COUNT(*) AS total FROM {table_name}").fetchone()
            summary_counts[table_name] = int(row["total"] if row is not None else 0)
        except sqlite3.Error:
            summary_counts[table_name] = 0

    enabled_modules = parse_enabled_modules(row_value(tenant, "enabled_modules", ""), row_value(tenant, "primary_business", ""))
    profile_payload = {
        "tenant_id": int(row_value(tenant, "id", "0") or "0"),
        "username": row_value(tenant, "username", ""),
        "shop_name": row_value(tenant, "shop_name", ""),
        "owner_name": row_value(tenant, "owner_name", ""),
        "phone": row_value(tenant, "phone", ""),
        "ui_language": row_value(tenant, "ui_language", ""),
        "primary_business": row_value(tenant, "primary_business", ""),
        "enabled_modules": enabled_modules,
        "profile_image_path": row_value(tenant, "profile_image_path", ""),
        "db_file": tenant_db_path.name,
    }

    return {
        "package_type": "SOFTX_TENANT_BACKUP",
        "format_version": BACKUP_PACKAGE_FORMAT_VERSION,
        "software": "Soft X",
        "created_at": now_sqlite_text(),
        "tenant": profile_payload,
        "database": {
            "archive_path": BACKUP_PACKAGE_DB_ARCHIVE_PATH,
            "filename": tenant_db_copy_path.name,
            "sha256": backup_file_sha256(tenant_db_copy_path),
            "size_bytes": tenant_db_copy_path.stat().st_size,
        },
        "summary": summary_counts,
        "files": [
            {
                "kind": item["kind"],
                "archive_path": item["archive_path"],
                "filename": item["filename"],
            }
            for item in asset_entries
        ],
    }


def create_tenant_backup_package(
    trigger_type: str = "EXPORT_PACKAGE",
    sync_google: bool = False,
) -> tuple[Path, str, str]:
    tenant = get_current_tenant()
    if tenant is None:
        raise ValueError("Tenant session required for export package.")

    tenant_db_path = get_current_db_path()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    package_slug = slugify_text(row_value(tenant, "username", "") or row_value(tenant, "shop_name", "") or "softx")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    package_path = BACKUP_DIR / f"{package_slug}-backup-{timestamp}.softxbackup.zip"

    with tempfile.TemporaryDirectory(prefix="softx-export-", dir=str(BACKUP_DIR)) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        tenant_db_copy = temp_dir / "tenant.sqlite"
        with sqlite3.connect(tenant_db_path) as source, sqlite3.connect(tenant_db_copy) as target:
            source.backup(target)

        with sqlite3.connect(tenant_db_path) as tenant_conn:
            tenant_conn.row_factory = sqlite3.Row
            asset_entries = collect_tenant_backup_assets(tenant, tenant_conn)
            manifest_payload = build_tenant_backup_manifest(
                tenant=tenant,
                tenant_db_path=tenant_db_path,
                tenant_db_copy_path=tenant_db_copy,
                tenant_db=tenant_conn,
                asset_entries=asset_entries,
            )

        manifest_path = temp_dir / BACKUP_PACKAGE_MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(tenant_db_copy, BACKUP_PACKAGE_DB_ARCHIVE_PATH)
            archive.write(manifest_path, BACKUP_PACKAGE_MANIFEST_NAME)
            for item in asset_entries:
                source_path = Path(item["source_path"])
                if source_path.exists():
                    archive.write(source_path, item["archive_path"])

    google_status = "NOT_SENT"
    google_file_id = None
    google_message = "Full export package saved locally."
    if sync_google:
        google_status, google_file_id, google_message = upload_backup_to_google_drive(package_path, mime_type="application/zip")

    log_backup_file(
        target_db=tenant_db_path,
        backup_path=package_path,
        trigger_type=trigger_type,
        google_status=google_status,
        google_file_id=google_file_id,
    )
    return package_path, google_status, google_message


def restore_tenant_backup_package(uploaded_file: FileStorage) -> tuple[Path, dict[str, object]]:
    tenant = get_current_tenant()
    if tenant is None:
        raise ValueError("Tenant session required for import.")

    filename = secure_filename(uploaded_file.filename or "")
    if not filename:
        raise ValueError("Choose a backup package file first.")

    lowered_name = filename.lower()
    if not lowered_name.endswith(".zip"):
        raise ValueError("Backup import only accepts Soft X export package zip.")

    current_username = normalize_login_identifier(row_value(tenant, "username", ""))
    current_db_path = get_current_db_path()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="softx-import-", dir=str(BACKUP_DIR)) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        uploaded_archive_path = temp_dir / "incoming-backup.zip"
        uploaded_file.save(uploaded_archive_path)

        try:
            with zipfile.ZipFile(uploaded_archive_path, "r") as archive:
                archive_names = set(archive.namelist())
                if BACKUP_PACKAGE_MANIFEST_NAME not in archive_names:
                    raise ValueError("Backup package manifest missing.")
                if BACKUP_PACKAGE_DB_ARCHIVE_PATH not in archive_names:
                    raise ValueError("Backup package database file missing.")
                extract_backup_archive_member(archive, BACKUP_PACKAGE_MANIFEST_NAME, temp_dir)
                extract_backup_archive_member(archive, BACKUP_PACKAGE_DB_ARCHIVE_PATH, temp_dir)
                manifest_path = temp_dir / BACKUP_PACKAGE_MANIFEST_NAME
                try:
                    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Backup manifest invalid: {exc}") from exc

                if str(manifest_payload.get("package_type") or "") != "SOFTX_TENANT_BACKUP":
                    raise ValueError("This file is not a Soft X tenant backup package.")
                if int(manifest_payload.get("format_version") or 0) > BACKUP_PACKAGE_FORMAT_VERSION:
                    raise ValueError("Backup package is newer than this app version.")

                backup_tenant = manifest_payload.get("tenant") if isinstance(manifest_payload.get("tenant"), dict) else {}
                backup_username = normalize_login_identifier(str(backup_tenant.get("username") or ""))
                if backup_username and current_username and backup_username != current_username:
                    raise ValueError("This backup belongs to another account. Import blocked for safety.")

                db_payload = manifest_payload.get("database") if isinstance(manifest_payload.get("database"), dict) else {}
                expected_sha = str(db_payload.get("sha256") or "").strip().lower()
                extracted_db_path = temp_dir / BACKUP_PACKAGE_DB_ARCHIVE_PATH
                if not extracted_db_path.exists():
                    raise ValueError("Extracted database file missing.")
                if expected_sha:
                    actual_sha = backup_file_sha256(extracted_db_path)
                    if actual_sha.lower() != expected_sha:
                        raise ValueError("Backup package database hash mismatch.")

                for entry in manifest_payload.get("files", []) if isinstance(manifest_payload.get("files"), list) else []:
                    if not isinstance(entry, dict):
                        continue
                    archive_path = str(entry.get("archive_path") or "").strip()
                    if not archive_path:
                        continue
                    if not is_safe_backup_archive_path(archive_path):
                        raise ValueError("Backup package contains unsafe attachment path.")
                    if archive_path in archive_names:
                        extract_backup_archive_member(archive, archive_path, temp_dir)
        except zipfile.BadZipFile as exc:
            raise ValueError("Uploaded file is not a valid backup zip.") from exc

        safety_backup_path, _, _ = create_tenant_backup_package(trigger_type="IMPORT_SAFETY_AUTO", sync_google=False)

        active_db = g.pop("db", None)
        if active_db is not None:
            try:
                active_db.close()
            except Exception:
                pass

        extracted_db_path = temp_dir / BACKUP_PACKAGE_DB_ARCHIVE_PATH
        with sqlite3.connect(extracted_db_path) as source, sqlite3.connect(current_db_path) as target:
            source.backup(target)
        init_db_for_path(current_db_path)

        files_payload = manifest_payload.get("files", []) if isinstance(manifest_payload.get("files"), list) else []
        copied_files = 0
        for entry in files_payload:
            if not isinstance(entry, dict):
                continue
            archive_path = str(entry.get("archive_path") or "").strip()
            file_kind = str(entry.get("kind") or "").strip().lower()
            if not archive_path:
                continue
            extracted_file = temp_dir / archive_path
            if not extracted_file.exists():
                continue
            if file_kind == "profile_image":
                target_dir = PRIVATE_PROFILE_UPLOAD_DIR
            elif file_kind in {"expense_receipt", "income_receipt"}:
                target_dir = PRIVATE_EXPENSE_RECEIPT_DIR
            elif file_kind == "receiver_photo":
                target_dir = PRIVATE_RECEIVER_UPLOAD_DIR
            else:
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extracted_file, target_dir / extracted_file.name)
            copied_files += 1

        backup_tenant = manifest_payload.get("tenant") if isinstance(manifest_payload.get("tenant"), dict) else {}
        enabled_modules = parse_enabled_modules(
            backup_tenant.get("enabled_modules") if isinstance(backup_tenant.get("enabled_modules"), list) else str(backup_tenant.get("enabled_modules") or ""),
            str(backup_tenant.get("primary_business") or row_value(tenant, "primary_business", DEFAULT_PRIMARY_BUSINESS)),
        )
        restored_profile_image = str(backup_tenant.get("profile_image_path") or "").strip()
        if restored_profile_image:
            profile_target = PRIVATE_PROFILE_UPLOAD_DIR / Path(restored_profile_image).name
            if not profile_target.exists():
                restored_profile_image = ""

        get_admin_db().execute(
            """
            UPDATE tenant_accounts
            SET shop_name = ?, owner_name = ?, phone = ?, ui_language = ?,
                primary_business = ?, enabled_modules = ?, profile_image_path = ?
            WHERE id = ?
            """,
            (
                str(backup_tenant.get("shop_name") or row_value(tenant, "shop_name", "")),
                str(backup_tenant.get("owner_name") or row_value(tenant, "owner_name", "")),
                str(backup_tenant.get("phone") or row_value(tenant, "phone", "")),
                normalize_language(str(backup_tenant.get("ui_language") or "")) or row_value(tenant, "ui_language", DEFAULT_UI_LANGUAGE),
                normalize_business_module(
                    str(backup_tenant.get("primary_business") or row_value(tenant, "primary_business", DEFAULT_PRIMARY_BUSINESS)),
                    default=row_value(tenant, "primary_business", DEFAULT_PRIMARY_BUSINESS),
                ),
                ",".join(enabled_modules),
                restored_profile_image,
                int(row_value(tenant, "id", "0") or "0"),
            ),
        )
        get_admin_db().commit()
        g.pop("current_tenant", None)
        g.pop("current_tenant_user", None)

    manifest_result = dict(manifest_payload)
    manifest_result["copied_files_count"] = copied_files
    return safety_backup_path, manifest_result


def _runtime_file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return backup_file_sha256(path)


def _collect_runtime_tenant_entries(tenant_dir: Path) -> list[dict[str, object]]:
    if not tenant_dir.exists():
        return []
    entries: list[dict[str, object]] = []
    for path in sorted(tenant_dir.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(tenant_dir).as_posix()
        entries.append(
            {
                "relative_path": relative_path,
                "size_bytes": path.stat().st_size,
                "sha256": _runtime_file_sha256(path),
            }
        )
    return entries


def build_pocket_runtime_manifest(
    source_main_db: Path,
    source_admin_db: Path,
    source_tenant_dir: Path,
) -> dict[str, object]:
    tenant_files = _collect_runtime_tenant_entries(source_tenant_dir)
    return {
        "package_type": POCKET_RUNTIME_PACKAGE_TYPE,
        "format_version": POCKET_RUNTIME_PACKAGE_FORMAT_VERSION,
        "software": "Pocket Pro",
        "created_at": now_sqlite_text(),
        "runtime": {
            "main_db": {
                "archive_path": POCKET_RUNTIME_MAIN_DB_ARCHIVE_PATH,
                "filename": source_main_db.name,
                "sha256": _runtime_file_sha256(source_main_db),
                "size_bytes": source_main_db.stat().st_size if source_main_db.exists() else 0,
            },
            "admin_db": {
                "archive_path": POCKET_RUNTIME_ADMIN_DB_ARCHIVE_PATH,
                "filename": source_admin_db.name,
                "sha256": _runtime_file_sha256(source_admin_db),
                "size_bytes": source_admin_db.stat().st_size if source_admin_db.exists() else 0,
            },
            "tenant_dir": {
                "archive_root": POCKET_RUNTIME_TENANT_DIR_ARCHIVE_PATH,
                "directory_name": source_tenant_dir.name,
                "file_count": len(tenant_files),
            },
        },
        "tenant_files": tenant_files,
    }


def create_pocket_runtime_export_package(
    *,
    source_main_db: Path | None = None,
    source_admin_db: Path | None = None,
    source_tenant_dir: Path | None = None,
    output_dir: Path | None = None,
) -> Path:
    main_db = (source_main_db or DB_PATH).expanduser()
    admin_db = (source_admin_db or ADMIN_DB_PATH).expanduser()
    tenant_dir = (source_tenant_dir or TENANT_DATA_DIR).expanduser()

    if not main_db.exists():
        raise FileNotFoundError(f"Main Pocket Pro DB not found: {main_db}")
    if not admin_db.exists():
        raise FileNotFoundError(f"Admin Pocket Pro DB not found: {admin_db}")
    if not tenant_dir.exists():
        raise FileNotFoundError(f"Pocket Pro tenant directory not found: {tenant_dir}")

    export_dir = (output_dir or BACKUP_DIR).expanduser()
    export_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    package_path = export_dir / f"pocketpro-runtime-{timestamp}.zip"

    with tempfile.TemporaryDirectory(prefix="pocketpro-runtime-export-", dir=str(export_dir)) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        runtime_root = temp_dir / "runtime-data"
        runtime_root.mkdir(parents=True, exist_ok=True)

        main_copy = runtime_root / Path(POCKET_RUNTIME_MAIN_DB_ARCHIVE_PATH).name
        admin_copy = runtime_root / Path(POCKET_RUNTIME_ADMIN_DB_ARCHIVE_PATH).name
        with sqlite3.connect(main_db) as source, sqlite3.connect(main_copy) as target:
            source.backup(target)
        with sqlite3.connect(admin_db) as source, sqlite3.connect(admin_copy) as target:
            source.backup(target)

        tenant_copy_root = runtime_root / Path(POCKET_RUNTIME_TENANT_DIR_ARCHIVE_PATH).name
        shutil.copytree(tenant_dir, tenant_copy_root, dirs_exist_ok=True)

        manifest_payload = build_pocket_runtime_manifest(
            source_main_db=main_copy,
            source_admin_db=admin_copy,
            source_tenant_dir=tenant_copy_root,
        )
        manifest_path = temp_dir / POCKET_RUNTIME_MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(main_copy, POCKET_RUNTIME_MAIN_DB_ARCHIVE_PATH)
            archive.write(admin_copy, POCKET_RUNTIME_ADMIN_DB_ARCHIVE_PATH)
            archive.write(manifest_path, POCKET_RUNTIME_MANIFEST_NAME)
            for path in sorted(tenant_copy_root.rglob("*")):
                if not path.is_file():
                    continue
                relative_path = path.relative_to(tenant_copy_root).as_posix()
                archive.write(path, f"{POCKET_RUNTIME_TENANT_DIR_ARCHIVE_PATH}/{relative_path}")

    return package_path


def restore_pocket_runtime_export_package(
    package_path: Path,
    *,
    target_main_db: Path | None = None,
    target_admin_db: Path | None = None,
    target_tenant_dir: Path | None = None,
) -> tuple[Path | None, dict[str, object]]:
    source_package = package_path.expanduser()
    if not source_package.exists():
        raise FileNotFoundError(f"Pocket Pro runtime package not found: {source_package}")

    main_db = (target_main_db or DB_PATH).expanduser()
    admin_db = (target_admin_db or ADMIN_DB_PATH).expanduser()
    tenant_dir = (target_tenant_dir or TENANT_DATA_DIR).expanduser()
    main_db.parent.mkdir(parents=True, exist_ok=True)
    admin_db.parent.mkdir(parents=True, exist_ok=True)
    tenant_dir.mkdir(parents=True, exist_ok=True)

    safety_backup: Path | None = None
    if main_db.exists() and admin_db.exists():
        try:
            safety_backup = create_pocket_runtime_export_package(
                source_main_db=main_db,
                source_admin_db=admin_db,
                source_tenant_dir=tenant_dir,
                output_dir=BACKUP_DIR,
            )
        except Exception:
            safety_backup = None

    with tempfile.TemporaryDirectory(prefix="pocketpro-runtime-import-", dir=str(BACKUP_DIR)) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        with zipfile.ZipFile(source_package, "r") as archive:
            archive_names = set(archive.namelist())
            required_entries = {
                POCKET_RUNTIME_MANIFEST_NAME,
                POCKET_RUNTIME_MAIN_DB_ARCHIVE_PATH,
                POCKET_RUNTIME_ADMIN_DB_ARCHIVE_PATH,
            }
            missing = sorted(item for item in required_entries if item not in archive_names)
            if missing:
                raise ValueError(f"Runtime package missing required entries: {', '.join(missing)}")

            extract_backup_archive_member(archive, POCKET_RUNTIME_MANIFEST_NAME, temp_dir)
            extract_backup_archive_member(archive, POCKET_RUNTIME_MAIN_DB_ARCHIVE_PATH, temp_dir)
            extract_backup_archive_member(archive, POCKET_RUNTIME_ADMIN_DB_ARCHIVE_PATH, temp_dir)

            manifest_path = temp_dir / POCKET_RUNTIME_MANIFEST_NAME
            try:
                manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Runtime package manifest invalid: {exc}") from exc

            if str(manifest_payload.get("package_type") or "") != POCKET_RUNTIME_PACKAGE_TYPE:
                raise ValueError("This file is not a Pocket Pro runtime export package.")
            if int(manifest_payload.get("format_version") or 0) > POCKET_RUNTIME_PACKAGE_FORMAT_VERSION:
                raise ValueError("Runtime package is newer than this importer.")

            runtime_payload = manifest_payload.get("runtime") if isinstance(manifest_payload.get("runtime"), dict) else {}
            tenant_file_entries = (
                manifest_payload.get("tenant_files") if isinstance(manifest_payload.get("tenant_files"), list) else []
            )

            main_payload = runtime_payload.get("main_db") if isinstance(runtime_payload.get("main_db"), dict) else {}
            admin_payload = runtime_payload.get("admin_db") if isinstance(runtime_payload.get("admin_db"), dict) else {}

            extracted_main = temp_dir / POCKET_RUNTIME_MAIN_DB_ARCHIVE_PATH
            extracted_admin = temp_dir / POCKET_RUNTIME_ADMIN_DB_ARCHIVE_PATH
            if str(main_payload.get("sha256") or "").strip():
                actual_sha = backup_file_sha256(extracted_main)
                if actual_sha.lower() != str(main_payload.get("sha256") or "").strip().lower():
                    raise ValueError("Runtime package main DB hash mismatch.")
            if str(admin_payload.get("sha256") or "").strip():
                actual_sha = backup_file_sha256(extracted_admin)
                if actual_sha.lower() != str(admin_payload.get("sha256") or "").strip().lower():
                    raise ValueError("Runtime package admin DB hash mismatch.")

            for entry in tenant_file_entries:
                if not isinstance(entry, dict):
                    continue
                relative_path = str(entry.get("relative_path") or "").strip()
                if not relative_path:
                    continue
                archive_path = f"{POCKET_RUNTIME_TENANT_DIR_ARCHIVE_PATH}/{relative_path}"
                if archive_path not in archive_names:
                    raise ValueError(f"Runtime package missing tenant file: {relative_path}")
                extract_backup_archive_member(archive, archive_path, temp_dir)
                expected_sha = str(entry.get("sha256") or "").strip().lower()
                if expected_sha:
                    extracted_file = temp_dir / archive_path
                    actual_sha = backup_file_sha256(extracted_file)
                    if actual_sha.lower() != expected_sha:
                        raise ValueError(f"Runtime package tenant file hash mismatch: {relative_path}")

        active_db = g.pop("db", None) if has_request_context() else None
        if active_db is not None:
            try:
                active_db.close()
            except Exception:
                pass
        active_admin_db = g.pop("admin_db", None) if has_request_context() else None
        if active_admin_db is not None:
            try:
                active_admin_db.close()
            except Exception:
                pass

        with sqlite3.connect(extracted_main) as source, sqlite3.connect(main_db) as target:
            source.backup(target)
        with sqlite3.connect(extracted_admin) as source, sqlite3.connect(admin_db) as target:
            source.backup(target)

        if tenant_dir.exists():
            shutil.rmtree(tenant_dir)
        tenant_dir.mkdir(parents=True, exist_ok=True)

        extracted_tenant_root = temp_dir / POCKET_RUNTIME_TENANT_DIR_ARCHIVE_PATH
        copied_tenant_files = 0
        if extracted_tenant_root.exists():
            for path in sorted(extracted_tenant_root.rglob("*")):
                if not path.is_file():
                    continue
                relative_path = path.relative_to(extracted_tenant_root)
                destination = tenant_dir / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                copied_tenant_files += 1

    init_admin_db()
    init_db()
    return safety_backup, {
        "main_db": str(main_db),
        "admin_db": str(admin_db),
        "tenant_dir": str(tenant_dir),
        "tenant_file_count": copied_tenant_files,
        "safety_backup": str(safety_backup) if safety_backup is not None else "",
    }


def create_database_backup(
    trigger_type: str = "MANUAL",
    sync_google: bool = False,
    db_path: Path | None = None,
) -> tuple[Path, str, str]:
    target_db = db_path or get_current_db_path()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{target_db.stem}-inventory-{timestamp}.db"
    backup_path = BACKUP_DIR / filename

    with sqlite3.connect(target_db) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)

    google_status = "NOT_SENT"
    google_file_id = None
    google_message = "Local backup saved."

    if sync_google:
        google_status, google_file_id, google_message = upload_backup_to_google_drive(backup_path)

    log_backup_file(
        target_db=target_db,
        backup_path=backup_path,
        trigger_type=trigger_type,
        google_status=google_status,
        google_file_id=google_file_id,
    )

    return backup_path, google_status, google_message


def ensure_daily_backup() -> None:
    if not DB_PATH.exists():
        return

    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT created_at
            FROM backup_logs
            WHERE trigger_type = 'AUTO_DAILY'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if row and str(row["created_at"]).startswith(today):
        return

    sync_google = os.getenv("AUTO_GOOGLE_BACKUP", "0").strip() == "1"
    if REDIS_QUEUE_ENABLED and queue_push_job("backup_create", {"sync_google": sync_google}):
        return
    create_database_backup(trigger_type="AUTO_DAILY", sync_google=sync_google, db_path=DB_PATH)


def get_profit_summary(db: sqlite3.Connection, condition_sql: str, params: tuple = ()) -> sqlite3.Row:
    return db.execute(
        f"""
        SELECT
            COUNT(*) AS units,
            COALESCE(SUM(s.sold_price), 0) AS revenue,
            COALESCE(SUM(s.sold_price - p.purchase_price), 0) AS profit
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.is_active = 1 AND ({condition_sql})
        """,
        params,
    ).fetchone()


def process_sale_return(
    db: sqlite3.Connection,
    sale_id: int,
    return_date: str,
    reason: str,
    restock: bool = True,
) -> sqlite3.Row:
    sale = db.execute(
        """
        SELECT
            s.id, s.product_id, s.customer_id, s.invoice_no, s.sold_at, s.sold_price, s.is_active,
            p.imei, p.purchase_price
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.id = ?
        """,
        (sale_id,),
    ).fetchone()

    if sale is None:
        raise ValueError("Sale not found.")
    if int(sale["is_active"]) != 1:
        raise ValueError("This sale is already returned/canceled.")

    already_returned = db.execute("SELECT 1 FROM sale_returns WHERE sale_id = ?", (sale_id,)).fetchone()
    if already_returned:
        raise ValueError("This sale already has a return record.")

    db.execute(
        """
        INSERT INTO sale_returns (sale_id, return_date, reason, restock)
        VALUES (?, ?, ?, ?)
        """,
        (sale_id, return_date, reason, 1 if restock else 0),
    )
    db.execute(
        """
        UPDATE sales
        SET is_active = 0,
            canceled_at = ?,
            cancel_reason = 'RETURNED',
            due_amount = 0,
            payment_status = 'PAID'
        WHERE id = ?
        """,
        (return_date, sale_id),
    )

    if restock:
        db.execute("UPDATE products SET status = 'IN_STOCK' WHERE id = ?", (sale["product_id"],))

    sale_date = parse_iso_date(str(sale["sold_at"] or ""))
    returned_date = parse_iso_date(return_date)
    if sale_date is not None and returned_date is not None:
        delta_days = (returned_date - sale_date).days
        if delta_days <= 1:
            add_fraud_event(
                db,
                event_type="QUICK_RETURN",
                severity="HIGH",
                sale_id=int(sale["id"]),
                product_id=int(sale["product_id"]),
                invoice_no=str(sale["invoice_no"] or ""),
                metadata={"delta_days": delta_days, "restock": 1 if restock else 0, "reason": reason},
            )

    enqueue_integration_outbox(
        db,
        event_type="SALE_RETURNED",
        payload={
            "sale_id": int(sale["id"]),
            "product_id": int(sale["product_id"]),
            "customer_id": int(sale["customer_id"] or 0),
            "invoice_no": str(sale["invoice_no"] or ""),
            "imei": str(sale["imei"] or ""),
            "return_date": return_date,
            "restock": 1 if restock else 0,
            "reason": reason,
        },
    )

    db.commit()
    return sale


def enqueue_integration_outbox(
    db: sqlite3.Connection,
    event_type: str,
    payload: dict[str, object],
    target_provider: str = "WEBHOOK",
) -> None:
    try:
        db.execute(
            """
            INSERT INTO integration_outbox (
                event_type, target_provider, payload_json, status, retry_count, created_at, updated_at
            )
            VALUES (?, ?, ?, 'PENDING', 0, ?, ?)
            """,
            (
                event_type[:80],
                target_provider[:80],
                json.dumps(payload, ensure_ascii=False),
                now_sqlite_text(),
                now_sqlite_text(),
            ),
        )
    except Exception:
        return


def add_fraud_event(
    db: sqlite3.Connection,
    event_type: str,
    severity: str,
    sale_id: int | None,
    product_id: int | None,
    invoice_no: str,
    metadata: dict[str, object],
) -> None:
    actor = get_current_tenant_user() if has_request_context() else None
    actor_username = str(actor["username"]) if actor is not None else "system"
    try:
        db.execute(
            """
            INSERT INTO fraud_events (
                event_type, severity, sale_id, product_id, invoice_no, actor_username, metadata, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type[:80],
                (severity or "LOW").upper()[:20],
                sale_id,
                product_id,
                (invoice_no or "")[:80],
                actor_username[:120],
                json.dumps(metadata, ensure_ascii=False),
                now_sqlite_text(),
            ),
        )
    except Exception:
        return


def maybe_mark_low_margin_fraud(
    db: sqlite3.Connection,
    *,
    sale_id: int | None,
    product_id: int | None,
    invoice_no: str,
    sold_price: float,
    purchase_price: float,
) -> None:
    if purchase_price <= 0:
        return
    margin = sold_price - purchase_price
    margin_pct = (margin / purchase_price) * 100.0
    if sold_price < purchase_price:
        add_fraud_event(
            db,
            event_type="UNDER_COST_SALE",
            severity="HIGH",
            sale_id=sale_id,
            product_id=product_id,
            invoice_no=invoice_no,
            metadata={
                "sold_price": sold_price,
                "purchase_price": purchase_price,
                "margin": margin,
                "margin_pct": margin_pct,
            },
        )
    elif margin_pct < 3:
        add_fraud_event(
            db,
            event_type="LOW_MARGIN_ALERT",
            severity="MEDIUM",
            sale_id=sale_id,
            product_id=product_id,
            invoice_no=invoice_no,
            metadata={
                "sold_price": sold_price,
                "purchase_price": purchase_price,
                "margin": margin,
                "margin_pct": margin_pct,
            },
        )


def expire_active_reservations(db: sqlite3.Connection) -> int:
    now_text = now_sqlite_text()
    updated = db.execute(
        """
        UPDATE inventory_reservations
        SET status = 'EXPIRED',
            updated_at = ?
        WHERE status = 'ACTIVE'
          AND expires_at IS NOT NULL
          AND TRIM(expires_at) <> ''
          AND expires_at <= ?
        """,
        (now_text, now_text),
    ).rowcount
    return int(updated or 0)


@app.template_filter("money")
def money_filter(value: float | int | None) -> str:
    if value is None:
        return "0.00"
    return f"{float(value):,.2f}"


@app.context_processor
def inject_user_context() -> dict[str, object]:
    tenant = get_current_tenant() if has_request_context() else None
    tenant_user = get_current_tenant_user() if has_request_context() else None
    is_admin = is_superadmin_logged_in() if has_request_context() else False
    show_receiver_photo = can_view_receiver_photo() if has_request_context() else False
    subscription_days_left = get_tenant_subscription_days_left(tenant) if tenant is not None else None
    ui_lang = resolve_ui_language(tenant)
    business_profile = build_business_profile(tenant)
    tenant_plan = get_tenant_plan_limits(tenant)
    tenant_usage: dict[str, int] = {}
    if tenant is not None and has_request_context():
        try:
            tenant_usage = get_tenant_usage_snapshot(get_db())
        except Exception:
            tenant_usage = {}
    role_access = (
        build_role_access(str(tenant_user["role"]) if tenant_user is not None else None)
        if has_request_context()
        else {}
    )

    def txt(en_text: str, bn_text: str) -> str:
        return pick_text(en_text, bn_text, ui_lang)

    tracking_label = txt(
        str(business_profile["tracking_label_en"]),
        str(business_profile["tracking_label_bn"]),
    )
    tracking_lookup_label = f"{tracking_label} {txt('Lookup', 'লুকআপ')}"
    is_pocket_module = bool(tenant is not None and get_tenant_default_endpoint(tenant) == "money_center")

    return {
        "current_tenant": tenant,
        "current_tenant_user": tenant_user,
        "is_superadmin": is_admin,
        "is_pocket_module": is_pocket_module,
        "subscription_days_left": subscription_days_left,
        "role_access": role_access,
        "ui_lang": ui_lang,
        "txt": txt,
        "business_profile": business_profile,
        "tenant_plan": tenant_plan,
        "tenant_usage": tenant_usage,
        "tracking_code_label": tracking_label,
        "tracking_lookup_label": tracking_lookup_label,
        "tracking_code_mode": str(business_profile["tracking_mode"]).lower(),
        "tracking_code_placeholder": str(business_profile["tracking_placeholder"]),
        "tracking_code_max_length": int(business_profile["tracking_max_length"]),
        "tracking_code_sample": str(business_profile["bulk_sample"]),
        "business_module_options": get_business_module_options(),
        "expense_category_options": get_expense_category_options(),
        "income_category_options": get_income_category_options(),
        "category_label": lambda kind, key: category_label(kind, key, ui_lang),
        "offline_mode": OFFLINE_MODE,
        "can_view_receiver_photo": show_receiver_photo,
    }


@app.before_request
def enforce_authentication() -> object | None:
    endpoint = request.endpoint or ""
    if not endpoint:
        return None

    if endpoint.startswith("static"):
        return None

    if endpoint in {
        "login_selector",
        "client_login",
        "client_login_user_entry",
        "client_login_admin_entry",
        "client_register",
        "client_register_alias",
        "forgot_password",
        "admin_login",
        "superadmin_login",
        "client_logout",
        "admin_logout",
        "set_language",
        "pocket_native_compat_proxy",
        "pocket_native_auth_register",
        "pocket_native_auth_login",
        "pocket_native_auth_guest",
        "pocket_native_auth_me",
        "pocket_native_auth_logout",
        "pocket_native_dashboard",
        "pocket_native_records",
        "pocket_native_accounts",
        "pocket_native_recurring",
        "pocket_native_budget",
        "pocket_native_budget_save",
        "pocket_native_goals",
        "pocket_native_goals_save",
        "pocket_native_goal_saved",
        "pocket_native_categories",
        "pocket_native_category_save",
        "pocket_native_category_delete",
        "pocket_native_category_move",
        "pocket_native_transaction_form",
        "pocket_native_transaction_save",
        "pocket_native_record_delete",
        "pocket_page_compat_proxy",
        "pocket_web_compat_proxy",
        "pocket_terms_of_use",
        "pocket_privacy_policy",
        "pocket_account_deletion",
        "pocket_pro_open",
        "billing_webhook_collect",
        "owner_login_alias",
        "shop_direct_login",
        "retail_invoice_public",
        "manifest_webmanifest",
        "service_worker_js",
    }:
        return None

    if endpoint.startswith("admin_"):
        if not is_superadmin_logged_in():
            return redirect(url_for("superadmin_login", next=request.path))
        return None

    tenant = get_current_tenant()
    if tenant is not None and not is_tenant_subscription_active(tenant):
        session.pop("tenant_id", None)
        session.pop("tenant_user_id", None)
        g.pop("current_tenant", None)
        g.pop("current_tenant_user", None)
        flash("Subscription expired. মাসিক বিল পরিশোধ করে আবার login করুন।", "error")
        return redirect(url_for("login_selector"))

    if tenant is None:
        if is_superadmin_logged_in():
            return redirect(url_for("admin_accounts"))
        return redirect(url_for("login_selector", next=request.path))

    tenant_user = get_current_tenant_user()
    if tenant_user is None:
        return redirect(url_for("login_selector", next=request.path))

    user_role = str(tenant_user["role"])
    if not user_role_can_access(user_role, endpoint):
        flash("You do not have permission for this section.", "error")
        return redirect(url_for("dashboard"))

    try:
        maybe_run_scheduled_tenant_backup(get_db())
    except Exception as exc:
        print(f"[Soft X] scheduled backup skipped: {exc}")

    return None


@app.get("/set-language/<lang>")
def set_language(lang: str):
    selected_lang = normalize_language(lang) or DEFAULT_UI_LANGUAGE
    session["ui_lang"] = selected_lang
    tenant = get_current_tenant()
    if tenant is not None:
        write_audit_log(
            action="TENANT_LANGUAGE_SWITCH",
            tenant_id=int(tenant["id"]),
            actor_type="TENANT_USER",
            metadata={"ui_lang": selected_lang},
        )
    elif is_superadmin_logged_in():
        write_audit_log(
            action="SUPERADMIN_LANGUAGE_SWITCH",
            actor_type="SUPERADMIN",
            actor_username=SUPERADMIN_USER,
            actor_role="SUPERADMIN",
            metadata={"ui_lang": selected_lang},
        )

    next_path = request.args.get("next", "").strip()
    if next_path.startswith("/") and not next_path.startswith("//"):
        return redirect(next_path)

    if is_superadmin_logged_in():
        return redirect(url_for("admin_accounts"))
    if get_current_tenant() is not None:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_selector"))


def pocket_native_category_items(kind: str) -> list[dict[str, object]]:
    normalized = (kind or "").strip().lower()
    if normalized == "income":
        raw_items = [
            ("salary", "Salary", "#58A6FF", "wallet"),
            ("bonus", "Bonus", "#8B5CF6", "gift"),
            ("business_sales", "Business Sales", "#22c55e", "briefcase"),
            ("service_income", "Service Income", "#06b6d4", "badge"),
            ("project_income", "Project Income", "#0EA5E9", "project"),
            ("freelance", "Freelance", "#14B8A6", "laptop"),
            ("commission", "Commission", "#F59E0B", "percent"),
            ("rental_income", "Rental Income", "#10B981", "home"),
            ("due_collection", "Due Collection", "#00D5FF", "collect"),
            ("refund_cashback", "Refund / Cashback", "#34D399", "cashback"),
            ("gift_received", "Gift Received", "#EC4899", "gift"),
            ("loan_received", "Loan Received", "#6366F1", "loan"),
            ("capital_injection", "Capital", "#0F766E", "bank"),
            ("asset_sale", "Asset Sale", "#F97316", "asset"),
            ("other_income", "Other Income", "#8b5cf6", "sparkles"),
        ]
    else:
        raw_items = [
            ("food_groceries", "Food / Groceries", "#ff8a4c", "utensils"),
            ("housing_rent", "Housing Rent", "#8B5CF6", "home"),
            ("utilities", "Utilities", "#38bdf8", "bolt"),
            ("internet_phone", "Internet + Phone", "#0EA5E9", "wifi"),
            ("transport", "Transport", "#22C55E", "car"),
            ("fuel", "Fuel", "#F97316", "fuel"),
            ("shopping", "Shopping", "#f472b6", "shopping_bag"),
            ("bills", "Bills", "#f59e0b", "receipt"),
            ("education_training", "Education / Training", "#6366F1", "school"),
            ("medical", "Medical", "#EF4444", "medical"),
            ("entertainment", "Entertainment", "#EC4899", "movie"),
            ("software_tools", "Software Tools", "#00D5FF", "code"),
            ("subscription_saas", "Subscriptions", "#A855F7", "repeat"),
            ("office_supplies", "Office Supplies", "#64748B", "office"),
            ("marketing", "Marketing", "#F43F5E", "megaphone"),
            ("loan_payment", "Loan Payment", "#FB7185", "loan"),
            ("emi_installment", "EMI / Installment", "#F59E0B", "calendar"),
            ("other", "Other", "#94A3B8", "more"),
        ]
    return [
        {
            "key": key,
            "label": label,
            "colorHex": color,
            "iconClass": "",
            "iconKey": icon_key,
        }
        for key, label, color, icon_key in raw_items
    ]


def ensure_pocket_native_app_tables(conn: sqlite3.Connection) -> None:
    ensure_expense_finance_tables(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pocket_native_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category_key TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            period_type TEXT NOT NULL DEFAULT 'MONTHLY',
            note TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        );
        CREATE TABLE IF NOT EXISTS pocket_native_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            target_amount REAL NOT NULL DEFAULT 0,
            saved_amount REAL NOT NULL DEFAULT 0,
            target_date TEXT,
            note TEXT,
            plan_frequency TEXT NOT NULL DEFAULT 'MONTHLY',
            plan_amount REAL NOT NULL DEFAULT 0,
            auto_reminder_on INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        );
        CREATE TABLE IF NOT EXISTS pocket_native_goal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            saved_at TEXT NOT NULL,
            note TEXT,
            kind TEXT NOT NULL DEFAULT 'save',
            created_at TEXT NOT NULL DEFAULT (DATETIME('now'))
        );
        CREATE TABLE IF NOT EXISTS pocket_native_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            category_key TEXT NOT NULL,
            label TEXT NOT NULL,
            icon_key TEXT NOT NULL DEFAULT '',
            color_hex TEXT NOT NULL DEFAULT '#58A6FF',
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            updated_at TEXT NOT NULL DEFAULT (DATETIME('now')),
            UNIQUE(kind, category_key)
        );
        """
    )


def pocket_native_slug(value: str, fallback: str = "custom") -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return clean or fallback


def pocket_native_currency_payload() -> dict[str, str]:
    return {"currencyCode": "BDT", "currencySymbol": "৳"}


def pocket_native_category_lookup(db: sqlite3.Connection, kind: str) -> dict[str, dict[str, object]]:
    return {str(item.get("key") or ""): item for item in pocket_native_all_categories(db, kind)}


def pocket_native_category_label(db: sqlite3.Connection, kind: str, key: str) -> str:
    clean_key = str(key or "").strip()
    lookup = pocket_native_category_lookup(db, kind)
    if clean_key in lookup:
        return str(lookup[clean_key].get("label") or clean_key.replace("_", " ").title())
    label_map = INCOME_CATEGORY_LABELS if kind == "income" else EXPENSE_CATEGORY_LABELS
    return str(label_map.get(clean_key, {}).get("en") or clean_key.replace("_", " ").title() or "Other")


def pocket_native_category_color(db: sqlite3.Connection, kind: str, key: str, fallback: str) -> str:
    lookup = pocket_native_category_lookup(db, kind)
    item = lookup.get(str(key or "").strip())
    return str(item.get("colorHex") or fallback) if item else fallback


def pocket_native_db() -> sqlite3.Connection:
    db = get_db()
    ensure_pocket_native_app_tables(db)
    return db


def pocket_native_all_categories(db: sqlite3.Connection, kind: str) -> list[dict[str, object]]:
    current_kind = "income" if (kind or "").strip().lower() == "income" else "expense"
    defaults = pocket_native_category_items(current_kind)
    try:
        rows = db.execute(
            """
            SELECT category_key, label, icon_key, color_hex
            FROM pocket_native_categories
            WHERE kind = ? AND is_active = 1
            ORDER BY sort_order ASC, id ASC
            """,
            (current_kind,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    custom_items = [
        {
            "key": str(row["category_key"] or ""),
            "label": str(row["label"] or ""),
            "colorHex": str(row["color_hex"] or "#58A6FF"),
            "iconClass": "",
            "iconKey": str(row["icon_key"] or ""),
        }
        for row in rows
    ]
    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in [*custom_items, *defaults]:
        key = str(item.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def pocket_native_month_bounds(month_value: str) -> tuple[str, str, str]:
    clean = (month_value or "").strip()
    try:
        first_day = datetime.strptime(clean, "%Y-%m").date().replace(day=1)
    except ValueError:
        today = date.today()
        first_day = today.replace(day=1)
    last_day = first_day.replace(day=calendar.monthrange(first_day.year, first_day.month)[1])
    return first_day.isoformat(), last_day.isoformat(), first_day.strftime("%B %Y")


def pocket_native_user_payload(
    account: sqlite3.Row | dict[str, object] | None = None,
    tenant_user: sqlite3.Row | dict[str, object] | None = None,
) -> dict[str, object]:
    username = row_value(tenant_user, "username", "") if tenant_user is not None else ""
    if not username:
        username = row_value(account, "username", "") if account is not None else "pocket"
    full_name = row_value(tenant_user, "full_name", "") if tenant_user is not None else ""
    if not full_name:
        full_name = row_value(account, "owner_name", "") or row_value(account, "shop_name", "") or "Pocket Pro User"
    initials = "".join(part[:1].upper() for part in str(full_name).split()[:2]).strip() or "PP"
    return {
        "tenantId": int(row_value(account, "id", 0) or 0) if account is not None else 0,
        "userId": int(row_value(tenant_user, "id", 0) or 0) if tenant_user is not None else 0,
        "username": str(username or ""),
        "fullName": str(full_name or ""),
        "email": str(row_value(account, "email", "") or (username if "@" in str(username or "") else "")),
        "bio": "",
        "role": str(row_value(tenant_user, "role", "ADMIN") if tenant_user is not None else "ADMIN"),
        "avatarUrl": "",
        "avatarInitials": initials[:2],
        "customAvatarKey": "",
        "customAvatarUri": "",
        "planCode": "FREE",
        "planLabel": "Free",
        "demoAccessEnabled": False,
    }


def pocket_native_current_auth() -> tuple[sqlite3.Row | None, dict[str, object] | None]:
    tenant = get_current_tenant()
    tenant_user = get_current_tenant_user()
    if tenant is None or tenant_user is None:
        return None, None
    return tenant, pocket_native_user_payload(tenant, tenant_user)


def pocket_native_account_matches_login(
    account: sqlite3.Row | dict[str, object],
    login_identifier: str,
) -> bool:
    clean_login = normalize_login_identifier(login_identifier)
    if not clean_login:
        return False
    candidates = {
        normalize_login_identifier(row_value(account, "username", "")),
        normalize_login_identifier(row_value(account, "email", "")),
        normalize_login_identifier(row_value(account, "phone", "")),
    }
    if "@" in clean_login:
        candidates.add(normalize_login_identifier(clean_login.split("@", 1)[0]))
    return clean_login in {item for item in candidates if item}


def pocket_native_authenticate_existing_account(
    admin_db: sqlite3.Connection,
    login_identifier: str,
    password: str,
) -> tuple[sqlite3.Row | dict[str, object] | None, dict[str, object] | None]:
    clean_login = normalize_login_identifier(login_identifier)
    if not clean_login or not password:
        return None, None

    active_accounts = admin_db.execute(
        "SELECT * FROM tenant_accounts WHERE is_active = 1 ORDER BY id DESC"
    ).fetchall()
    prioritized_accounts = [
        account for account in active_accounts if pocket_native_account_matches_login(account, clean_login)
    ]
    if prioritized_accounts:
        prioritized_ids = {int(row_value(account, "id", 0) or 0) for account in prioritized_accounts}
        active_accounts = prioritized_accounts + [
            account for account in active_accounts if int(row_value(account, "id", 0) or 0) not in prioritized_ids
        ]

    for account in active_accounts:
        tenant_db_path = ensure_tenant_db_ready(admin_db, account)
        if tenant_db_path is None:
            continue

        owner_login_match = pocket_native_account_matches_login(account, clean_login)
        try:
            with sqlite3.connect(tenant_db_path) as tenant_conn:
                tenant_conn.row_factory = sqlite3.Row
                tenant_conn.execute("PRAGMA foreign_keys = ON;")
                ensure_tenant_users_table(tenant_conn)

                if owner_login_match:
                    tenant_admin_user = tenant_conn.execute(
                        """
                        SELECT id, username, full_name, role, password_hash, is_active
                        FROM users
                        WHERE role = 'ADMIN' AND is_active = 1
                        ORDER BY CASE
                            WHEN LOWER(username) = ? THEN 0
                            WHEN LOWER(username) = 'admin' THEN 1
                            ELSE 2
                        END, id ASC
                        LIMIT 1
                        """,
                        (clean_login,),
                    ).fetchone()
                    if tenant_admin_user is not None and password_matches(
                        str(tenant_admin_user["password_hash"]), password
                    ):
                        return account, {
                            "id": int(tenant_admin_user["id"]),
                            "username": str(tenant_admin_user["username"]),
                            "full_name": str(tenant_admin_user["full_name"] or ""),
                            "role": "ADMIN",
                        }

                user_columns = get_table_columns(tenant_conn, "users")
                if "oauth_email" in user_columns:
                    tenant_user = tenant_conn.execute(
                        """
                        SELECT id, username, full_name, role, password_hash, is_active, oauth_email
                        FROM users
                        WHERE (LOWER(username) = ? OR LOWER(COALESCE(oauth_email, '')) = ?)
                          AND is_active = 1
                        ORDER BY CASE WHEN role = 'ADMIN' THEN 0 ELSE 1 END, id ASC
                        LIMIT 1
                        """,
                        (clean_login, clean_login),
                    ).fetchone()
                else:
                    tenant_user = tenant_conn.execute(
                        """
                        SELECT id, username, full_name, role, password_hash, is_active
                        FROM users
                        WHERE LOWER(username) = ? AND is_active = 1
                        ORDER BY CASE WHEN role = 'ADMIN' THEN 0 ELSE 1 END, id ASC
                        LIMIT 1
                        """,
                        (clean_login,),
                    ).fetchone()
                if tenant_user is not None and password_matches(str(tenant_user["password_hash"]), password):
                    return account, {
                        "id": int(tenant_user["id"]),
                        "username": str(tenant_user["username"]),
                        "full_name": str(tenant_user["full_name"] or ""),
                        "role": normalize_role(str(tenant_user["role"]), default="USER"),
                    }
        except sqlite3.Error:
            continue

        if owner_login_match and password_matches(str(row_value(account, "password_hash", "")), password):
            full_name = row_value(account, "owner_name", "") or f"{row_value(account, 'shop_name', 'Pocket Pro')} Admin"
            admin_user_id = create_or_update_tenant_user(
                db_path=tenant_db_path,
                username="admin",
                full_name=full_name,
                role="ADMIN",
                password=password,
                is_active=True,
            )
            return account, {
                "id": int(admin_user_id),
                "username": "admin",
                "full_name": full_name,
                "role": "ADMIN",
            }

    return None, None


def pocket_native_find_account(login_identifier: str) -> sqlite3.Row | None:
    clean_login = normalize_login_identifier(login_identifier)
    if not clean_login:
        return None
    admin_db = get_admin_db()
    fallback_username = clean_login.split("@", 1)[0] if "@" in clean_login else clean_login
    try:
        return admin_db.execute(
            """
            SELECT
                id, shop_name, owner_name, phone, email, username, password_hash, is_active,
                paid_until, billing_cycle, monthly_fee, db_path,
                ui_language, primary_business, enabled_modules
            FROM tenant_accounts
            WHERE is_active = 1
              AND (
                LOWER(username) = ?
                OR LOWER(COALESCE(email, '')) = ?
                OR LOWER(username) = ?
              )
            ORDER BY id DESC
            LIMIT 1
            """,
            (clean_login, clean_login, fallback_username),
        ).fetchone()
    except sqlite3.Error:
        init_admin_db()
        return admin_db.execute(
            """
            SELECT
                id, shop_name, owner_name, phone, username, password_hash, is_active,
                paid_until, billing_cycle, monthly_fee, db_path,
                ui_language, primary_business, enabled_modules
            FROM tenant_accounts
            WHERE LOWER(username) IN (?, ?) AND is_active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (clean_login, fallback_username),
        ).fetchone()


@app.post("/api/pocket/native/auth/register")
def pocket_native_auth_register():
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email") or "").strip().lower()
    username = normalize_username(str(payload.get("username") or email or ""))
    password = str(payload.get("password") or "")
    confirm_password = str(payload.get("confirm_password") or "")
    ui_language = normalize_language(str(payload.get("ui_language") or "")) or DEFAULT_UI_LANGUAGE

    if len(username) < 4:
        return jsonify(ok=False, message="Username must be at least 4 characters."), 400
    if len(password) < 6:
        return jsonify(ok=False, message="Password must be at least 6 characters."), 400
    if password != confirm_password:
        return jsonify(ok=False, message="Password and confirm password do not match."), 400

    admin_db = get_admin_db()
    db_path = tenant_db_path_for_username(username)
    existing = admin_db.execute(
        """
        SELECT id
        FROM tenant_accounts
        WHERE LOWER(username) = LOWER(?)
           OR (TRIM(COALESCE(email, '')) <> '' AND LOWER(email) = LOWER(?))
        LIMIT 1
        """,
        (username, email),
    ).fetchone()
    if existing is not None:
        return jsonify(ok=False, message="This account already exists. Please login."), 409
    if db_path.exists():
        return jsonify(ok=False, message="Account data already exists. Please login."), 409

    try:
        init_db_for_path(db_path)
        full_name = email.split("@")[0].replace(".", " ").replace("_", " ").title() if email else username
        create_or_update_tenant_user(
            db_path=db_path,
            username="admin",
            full_name=full_name,
            role="ADMIN",
            password=password,
            is_active=True,
        )
        if username != "admin":
            create_or_update_tenant_user(
                db_path=db_path,
                username=username,
                full_name=full_name,
                role="ADMIN",
                password=password,
                is_active=True,
            )
        admin_db.execute(
            """
            INSERT INTO tenant_accounts (
                shop_name, owner_name, phone, email, username, password_hash, db_path,
                ui_language, primary_business, enabled_modules,
                billing_cycle, monthly_fee, paid_until, billing_note, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                username,
                full_name,
                "",
                email,
                username,
                make_password_hash(password),
                str(db_path),
                ui_language,
                "POCKET_MONEY",
                "POCKET_MONEY",
                "MONTHLY",
                0.0,
                None,
                "Pocket Pro native signup",
            ),
        )
        tenant_id = int(admin_db.execute("SELECT last_insert_rowid()").fetchone()[0])
        admin_db.commit()
        account = pocket_native_find_account(username)
        session.clear()
        session["tenant_id"] = tenant_id
        session["tenant_user_id"] = 1
        session["ui_lang"] = ui_language
        return jsonify(
            ok=True,
            message="Pocket Pro account created.",
            user=pocket_native_user_payload(account, {"id": 1, "username": username, "full_name": full_name, "role": "ADMIN"}),
        )
    except Exception as exc:
        admin_db.rollback()
        return jsonify(ok=False, message=f"Registration failed: {exc}"), 500


@app.post("/api/pocket/native/auth/login")
def pocket_native_auth_login():
    payload = request.get_json(silent=True) or {}
    login_identifier = normalize_login_identifier(str(payload.get("login_identifier") or ""))
    password = str(payload.get("password") or "")
    admin_db = get_admin_db()
    account, tenant_user_payload = pocket_native_authenticate_existing_account(
        admin_db,
        login_identifier,
        password,
    )
    if account is None or tenant_user_payload is None:
        return jsonify(ok=False, message="Login failed. Username/Email/Password check করুন।"), 401

    session.clear()
    session["tenant_id"] = int(row_value(account, "id", 0) or 0)
    session["tenant_user_id"] = int(tenant_user_payload["id"])
    session["ui_lang"] = normalize_language(str(row_value(account, "ui_language", ""))) or DEFAULT_UI_LANGUAGE
    return jsonify(ok=True, message="Login successful.", user=pocket_native_user_payload(account, tenant_user_payload))


@app.post("/api/pocket/native/auth/guest")
def pocket_native_auth_guest():
    return jsonify(
        ok=True,
        message="Guest mode ready.",
        user={
            **pocket_native_user_payload(None, {"id": 0, "username": "guest", "full_name": "Pocket Guest", "role": "USER"}),
            "demoAccessEnabled": True,
        },
    )


@app.get("/api/pocket/native/auth/me")
def pocket_native_auth_me():
    _, user = pocket_native_current_auth()
    if user is None:
        return jsonify(ok=False, message="Not signed in."), 401
    return jsonify(ok=True, message="", user=user)


@app.post("/api/pocket/native/auth/logout")
def pocket_native_auth_logout():
    session.clear()
    return jsonify(ok=True, message="Logged out.", user=None)


@app.get("/api/pocket/native/dashboard")
def pocket_native_dashboard():
    _, user = pocket_native_current_auth()
    db = pocket_native_db()
    today_iso = date.today().isoformat()
    month_start, month_end, _ = pocket_native_month_bounds(today_iso[:7])

    income_total = float(db.execute("SELECT COALESCE(SUM(amount), 0) FROM incomes").fetchone()[0] or 0)
    expense_total = float(db.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses").fetchone()[0] or 0)
    month_income = float(
        db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM incomes WHERE income_date BETWEEN ? AND ?",
            (month_start, month_end),
        ).fetchone()[0]
        or 0
    )
    month_expense = float(
        db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE expense_date BETWEEN ? AND ?",
            (month_start, month_end),
        ).fetchone()[0]
        or 0
    )
    today_income = float(
        db.execute("SELECT COALESCE(SUM(amount), 0) FROM incomes WHERE income_date = ?", (today_iso,)).fetchone()[0]
        or 0
    )
    today_expense = float(
        db.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE expense_date = ?", (today_iso,)).fetchone()[0]
        or 0
    )
    balance = income_total - expense_total

    budget_rows = db.execute(
        """
        SELECT category_key, amount
        FROM pocket_native_budgets
        WHERE is_active = 1
        """
    ).fetchall()
    budget_amount = sum(float(row["amount"] or 0) for row in budget_rows)
    budget_categories = [str(row["category_key"] or "") for row in budget_rows]
    budget_spent = 0.0
    if budget_categories:
        placeholders = ",".join("?" for _ in budget_categories)
        budget_spent = float(
            db.execute(
                f"""
                SELECT COALESCE(SUM(amount), 0)
                FROM expenses
                WHERE category IN ({placeholders})
                  AND expense_date BETWEEN ? AND ?
                """,
                (*budget_categories, month_start, month_end),
            ).fetchone()[0]
            or 0
        )
    budget_remaining = max(0.0, budget_amount - budget_spent)
    budget_used_percent = int(round((budget_spent / budget_amount) * 100)) if budget_amount > 0 else 0

    goal_row = db.execute(
        """
        SELECT COALESCE(SUM(saved_amount), 0) AS saved,
               COALESCE(SUM(target_amount), 0) AS target
        FROM pocket_native_goals
        WHERE status = 'ACTIVE'
        """
    ).fetchone()
    goal_saved = float(goal_row["saved"] or 0) if goal_row is not None else 0.0
    goal_target = float(goal_row["target"] or 0) if goal_row is not None else 0.0
    goal_progress = int(round((goal_saved / goal_target) * 100)) if goal_target > 0 else 0

    category_rows = db.execute(
        """
        SELECT category, COALESCE(SUM(amount), 0) AS amount
        FROM expenses
        WHERE expense_date BETWEEN ? AND ?
        GROUP BY category
        ORDER BY amount DESC
        LIMIT 8
        """,
        (month_start, month_end),
    ).fetchall()
    analytics_categories = [
        {
            "key": str(row["category"] or "other"),
            "label": pocket_native_category_label(db, "expense", str(row["category"] or "other")),
            "amount": float(row["amount"] or 0),
            "colorHex": pocket_native_category_color(db, "expense", str(row["category"] or "other"), "#58A6FF"),
        }
        for row in category_rows
    ]

    weekly_rows = db.execute(
        """
        SELECT STRFTIME('%W', expense_date) AS week_no,
               MIN(expense_date) AS start_date,
               COALESCE(SUM(amount), 0) AS amount
        FROM expenses
        WHERE expense_date BETWEEN ? AND ?
        GROUP BY week_no
        ORDER BY start_date ASC
        """,
        (month_start, month_end),
    ).fetchall()
    weekly = [
        {
            "label": f"Week {index + 1}",
            "date": str(row["start_date"] or ""),
            "amount": float(row["amount"] or 0),
        }
        for index, row in enumerate(weekly_rows)
    ]

    recent_income = db.execute(
        """
        SELECT id, income_date AS entry_date, category, source_name AS party_name, amount, note, 'income' AS kind
        FROM incomes
        ORDER BY income_date DESC, id DESC
        LIMIT 10
        """
    ).fetchall()
    recent_expense = db.execute(
        """
        SELECT id, expense_date AS entry_date, category, employee_name AS party_name, amount, note, 'expense' AS kind
        FROM expenses
        ORDER BY expense_date DESC, id DESC
        LIMIT 10
        """
    ).fetchall()
    recent_activity = []
    for row in sorted([*recent_income, *recent_expense], key=lambda item: (str(item["entry_date"] or ""), int(item["id"] or 0)), reverse=True)[:12]:
        kind = str(row["kind"] or "")
        category = str(row["category"] or "other")
        amount = float(row["amount"] or 0)
        recent_activity.append(
            {
                "id": int(row["id"] or 0),
                "kind": kind,
                "mode": kind,
                "label": str(row["party_name"] or pocket_native_category_label(db, kind, category)),
                "categoryLabel": pocket_native_category_label(db, kind, category),
                "note": str(row["note"] or ""),
                "date": str(row["entry_date"] or ""),
                "amount": amount,
                "signedAmount": amount if kind == "income" else -amount,
                "colorHex": "#22c55e" if kind == "income" else pocket_native_category_color(db, "expense", category, "#ff4da6"),
                "iconClass": "",
                "canDelete": True,
                "detailUrl": "",
                "editUrl": "",
                "deleteUrl": "",
            }
        )

    return jsonify(
        ok=True,
        message="Pocket Pro dashboard ready.",
        user=user,
        dashboard={
            **pocket_native_currency_payload(),
            "summary": {
                "totalBalance": balance,
                "monthIncome": month_income,
                "monthExpense": month_expense,
                "todayIncome": today_income,
                "todayExpense": today_expense,
                "budgetRemaining": budget_remaining,
                "budgetAmount": budget_amount,
                "budgetSpent": budget_spent,
                "budgetUsedPercent": budget_used_percent,
                "goalSaved": goal_saved,
                "goalTarget": goal_target,
                "goalProgressPercent": goal_progress,
                "accounts": 1,
                "openingBalance": 0.0,
            },
            "accounts": [
                {"id": 1, "name": "My Wallet", "type": "CASH", "openingBalance": 0.0, "currentBalance": balance, "isDefault": True}
            ],
            "analytics": {"categories": analytics_categories, "weekly": weekly},
            "recentActivity": recent_activity,
            "reportsPdfUrl": "",
        },
    )


@app.get("/api/pocket/native/records")
def pocket_native_records():
    _, user = pocket_native_current_auth()
    db = pocket_native_db()
    start_date, end_date, month_label = pocket_native_month_bounds(str(request.args.get("month") or ""))
    income_rows = db.execute(
        """
        SELECT id, income_date AS entry_date, category, source_name AS party_name, amount, note
        FROM incomes
        WHERE income_date BETWEEN ? AND ?
        ORDER BY income_date DESC, id DESC
        LIMIT 300
        """,
        (start_date, end_date),
    ).fetchall()
    expense_rows = db.execute(
        """
        SELECT id, expense_date AS entry_date, category, employee_name AS party_name, amount, note
        FROM expenses
        WHERE expense_date BETWEEN ? AND ?
        ORDER BY expense_date DESC, id DESC
        LIMIT 300
        """,
        (start_date, end_date),
    ).fetchall()
    grouped: dict[str, list[dict[str, object]]] = {}
    income_total = 0.0
    expense_total = 0.0
    for row in income_rows:
        amount = float(row["amount"] or 0)
        income_total += amount
        entry_date = str(row["entry_date"] or start_date)
        grouped.setdefault(entry_date, []).append(
            {
                "id": int(row["id"] or 0),
                "kind": "income",
                "mode": "income",
                "label": str(row["party_name"] or row["category"] or "Income"),
                "note": str(row["note"] or ""),
                "date": entry_date,
                "amount": amount,
                "signedAmount": amount,
                "colorHex": "#22c55e",
                "iconClass": "",
                "canDelete": True,
                "detailUrl": "",
                "editUrl": "",
                "deleteUrl": "",
            }
        )
    for row in expense_rows:
        amount = float(row["amount"] or 0)
        expense_total += amount
        entry_date = str(row["entry_date"] or start_date)
        grouped.setdefault(entry_date, []).append(
            {
                "id": int(row["id"] or 0),
                "kind": "expense",
                "mode": "expense",
                "label": str(row["party_name"] or row["category"] or "Expense"),
                "note": str(row["note"] or ""),
                "date": entry_date,
                "amount": amount,
                "signedAmount": -amount,
                "colorHex": "#ff4da6",
                "iconClass": "",
                "canDelete": True,
                "detailUrl": "",
                "editUrl": "",
                "deleteUrl": "",
            }
        )
    groups = []
    for entry_date in sorted(grouped.keys(), reverse=True):
        rows = grouped[entry_date]
        groups.append(
            {
                "dateIso": entry_date,
                "dateLabel": entry_date,
                "weekdayLabel": "",
                "incomeTotal": sum(float(item["amount"]) for item in rows if item["kind"] == "income"),
                "expenseTotal": sum(float(item["amount"]) for item in rows if item["kind"] == "expense"),
                "rows": rows,
            }
        )
    return jsonify(
        ok=True,
        message="Pocket Pro records ready.",
        user=user,
        records={
            "monthValue": start_date[:7],
            "monthLabel": month_label,
            "yearLabel": start_date[:4],
            **pocket_native_currency_payload(),
            "summary": {
                "income": income_total,
                "expense": expense_total,
                "lent": 0.0,
                "balance": income_total - expense_total,
                "records": len(income_rows) + len(expense_rows),
            },
            "groups": groups,
        },
    )


@app.get("/api/pocket/native/accounts")
def pocket_native_accounts():
    _, user = pocket_native_current_auth()
    db = pocket_native_db()
    income_total = float(db.execute("SELECT COALESCE(SUM(amount), 0) FROM incomes").fetchone()[0] or 0)
    expense_total = float(db.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses").fetchone()[0] or 0)
    balance = income_total - expense_total
    return jsonify(
        ok=True,
        message="Pocket Pro accounts ready.",
        user=user,
        accounts={
            **pocket_native_currency_payload(),
            "activeFilter": "ALL",
            "filters": [
                {"key": "ALL", "label": "All", "iconToken": "all"},
                {"key": "CASH", "label": "Cash", "iconToken": "cash"},
            ],
            "summary": {
                "totalBalance": balance,
                "totalIncome": income_total,
                "totalExpense": expense_total,
                "accounts": 1,
                "openingBalance": 0.0,
                "netChange": balance,
            },
            "accounts": [
                {
                    "id": 1,
                    "name": "My Wallet",
                    "type": "CASH",
                    "typeLabel": "Cash",
                    "openingBalance": 0.0,
                    "currentBalance": balance,
                    "incomeTotal": income_total,
                    "expenseTotal": expense_total,
                    "netChange": balance,
                    "isDefault": True,
                    "note": "All accounts combined",
                    "accentHex": "#58A6FF",
                    "iconToken": "cash",
                    "statementUrl": "",
                    "statementPdfUrl": "",
                }
            ],
            "recentActivity": [],
            "statementPdfBaseUrl": "",
        },
    )


@app.get("/api/pocket/native/recurring")
def pocket_native_recurring():
    _, user = pocket_native_current_auth()
    return jsonify(
        ok=True,
        message="Pocket Pro reminders ready.",
        user=user,
        payload={
            **pocket_native_currency_payload(),
            "summary": {
                "totalTemplates": 0,
                "activeTemplates": 0,
                "incomeTemplates": 0,
                "expenseTemplates": 0,
                "nextDueTitle": "",
                "nextDueDate": "",
            },
            "templates": [],
        },
    )


@app.get("/api/pocket/native/budget")
def pocket_native_budget():
    _, user = pocket_native_current_auth()
    db = pocket_native_db()
    budget_rows = db.execute(
        """
        SELECT id, name, category_key, amount, period_type, note, is_active
        FROM pocket_native_budgets
        WHERE is_active = 1
        ORDER BY id DESC
        """,
    ).fetchall()
    expense_by_category = {
        str(row["category"] or ""): float(row["spent"] or 0)
        for row in db.execute(
            """
            SELECT category, COALESCE(SUM(amount), 0) AS spent
            FROM expenses
            GROUP BY category
            """
        ).fetchall()
    }
    budgets = []
    total_budget = 0.0
    total_spent = 0.0
    for row in budget_rows:
        amount = float(row["amount"] or 0)
        spent = float(expense_by_category.get(str(row["category_key"] or ""), 0))
        remaining = max(0.0, amount - spent)
        progress = int(round((spent / amount) * 100)) if amount > 0 else 0
        total_budget += amount
        total_spent += spent
        budgets.append(
            {
                "id": int(row["id"] or 0),
                "name": str(row["name"] or "Budget"),
                "categoryKey": str(row["category_key"] or "all"),
                "categoryLabel": str(row["category_key"] or "All").replace("_", " ").title(),
                "amount": amount,
                "spent": spent,
                "remaining": remaining,
                "progress": progress,
                "periodType": str(row["period_type"] or "MONTHLY"),
                "note": str(row["note"] or ""),
                "accentHex": "#58A6FF",
                "isActive": bool(row["is_active"]),
                "dueDayOfMonth": 1,
            }
        )
    used_percent = int(round((total_spent / total_budget) * 100)) if total_budget > 0 else 0
    return jsonify(
        ok=True,
        message="Pocket Pro budget ready.",
        user=user,
        budget={
            **pocket_native_currency_payload(),
            "summary": {
                "totalBudget": total_budget,
                "spent": total_spent,
                "remaining": max(0.0, total_budget - total_spent),
                "usedPercent": used_percent,
                "activeBudgets": len(budgets),
            },
            "budgets": budgets,
            "categoryOptions": pocket_native_all_categories(db, "expense"),
            "tipTitle": "Budget stays synced",
            "tipBody": "Expenses in the same category automatically reduce remaining budget.",
        },
    )


@app.post("/api/pocket/native/budget/save")
def pocket_native_budget_save():
    _, user = pocket_native_current_auth()
    db = pocket_native_db()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("budgetName") or "Monthly Budget").strip()
    category_key = pocket_native_slug(str(payload.get("categoryKey") or "all"), "all")
    amount = max(0.0, float(payload.get("amount") or 0))
    period_type = str(payload.get("periodType") or "MONTHLY").strip().upper() or "MONTHLY"
    note = str(payload.get("note") or "").strip()
    if amount <= 0:
        return jsonify(ok=False, message="Budget amount must be greater than 0."), 400
    existing = db.execute(
        "SELECT id FROM pocket_native_budgets WHERE LOWER(category_key) = LOWER(?) AND is_active = 1 LIMIT 1",
        (category_key,),
    ).fetchone()
    if existing is not None:
        db.execute(
            """
            UPDATE pocket_native_budgets
            SET name = ?, amount = ?, period_type = ?, note = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, amount, period_type, note, now_sqlite_text(), int(existing["id"])),
        )
    else:
        db.execute(
            """
            INSERT INTO pocket_native_budgets (name, category_key, amount, period_type, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, category_key, amount, period_type, note),
        )
    db.commit()
    response = pocket_native_budget()
    response.json["message"] = "Budget saved."
    response.json["user"] = user
    return response


@app.get("/api/pocket/native/goals")
def pocket_native_goals():
    _, user = pocket_native_current_auth()
    db = pocket_native_db()
    rows = db.execute(
        """
        SELECT id, name, target_amount, saved_amount, target_date, note,
               plan_frequency, plan_amount, auto_reminder_on, status
        FROM pocket_native_goals
        WHERE status = 'ACTIVE'
        ORDER BY id DESC
        """,
    ).fetchall()
    goals = []
    target_total = 0.0
    saved_total = 0.0
    for row in rows:
        target = float(row["target_amount"] or 0)
        saved = float(row["saved_amount"] or 0)
        remaining = max(0.0, target - saved)
        progress = int(round((saved / target) * 100)) if target > 0 else 0
        history_rows = db.execute(
            """
            SELECT id, amount, saved_at, note, kind
            FROM pocket_native_goal_history
            WHERE goal_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (int(row["id"]),),
        ).fetchall()
        goals.append(
            {
                "id": int(row["id"] or 0),
                "name": str(row["name"] or "Goal"),
                "targetAmount": target,
                "savedAmount": saved,
                "remainingAmount": remaining,
                "progress": progress,
                "targetDate": str(row["target_date"] or ""),
                "status": str(row["status"] or "ACTIVE"),
                "deadlineState": "OPEN",
                "deadlineText": str(row["target_date"] or ""),
                "note": str(row["note"] or ""),
                "planFrequency": str(row["plan_frequency"] or "MONTHLY"),
                "planAmount": float(row["plan_amount"] or 0),
                "autoReminderOn": bool(row["auto_reminder_on"]),
                "history": [
                    {
                        "id": str(item["id"] or ""),
                        "amount": float(item["amount"] or 0),
                        "date": str(item["saved_at"] or ""),
                        "note": str(item["note"] or ""),
                        "kind": str(item["kind"] or "save"),
                    }
                    for item in history_rows
                ],
                "accentStartHex": "#58A6FF",
                "accentEndHex": "#00D5FF",
            }
        )
        target_total += target
        saved_total += saved
    total_progress = int(round((saved_total / target_total) * 100)) if target_total > 0 else 0
    return jsonify(
        ok=True,
        message="Pocket Pro goals ready.",
        user=user,
        goals={
            **pocket_native_currency_payload(),
            "summary": {
                "totalProgressPercent": total_progress,
                "savedAmount": saved_total,
                "targetAmount": target_total,
                "remainingAmount": max(0.0, target_total - saved_total),
                "goalsCount": len(goals),
            },
            "goals": goals,
            "quickAddOptions": [100.0, 500.0, 1000.0, 5000.0],
            "motivationTitle": "Keep saving",
            "motivationBody": "Small saves build the goal.",
        },
    )


@app.post("/api/pocket/native/goals/save")
def pocket_native_goals_save():
    db = pocket_native_db()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("goalName") or "New Goal").strip()
    target = max(0.0, float(payload.get("targetAmount") or 0))
    saved = max(0.0, float(payload.get("savedAmount") or 0))
    if target <= 0:
        return jsonify(ok=False, message="Goal target amount must be greater than 0."), 400
    db.execute(
        """
        INSERT INTO pocket_native_goals (
            name, target_amount, saved_amount, target_date, note,
            plan_frequency, plan_amount, auto_reminder_on
        )
        VALUES (?, ?, ?, ?, ?, 'MONTHLY', 0, 1)
        """,
        (
            name,
            target,
            min(saved, target),
            str(payload.get("targetDate") or ""),
            str(payload.get("note") or ""),
        ),
    )
    goal_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    if saved > 0:
        db.execute(
            """
            INSERT INTO pocket_native_goal_history (goal_id, amount, saved_at, note, kind)
            VALUES (?, ?, ?, ?, 'save')
            """,
            (goal_id, min(saved, target), date.today().isoformat(), "Initial save"),
        )
    db.commit()
    response = pocket_native_goals()
    response.json["message"] = "Goal saved."
    return response


@app.post("/api/pocket/native/goals/<int:goal_id>/saved")
def pocket_native_goal_saved(goal_id: int):
    db = pocket_native_db()
    payload = request.get_json(silent=True) or {}
    amount = max(0.0, float(payload.get("amount") or 0))
    if amount <= 0:
        return jsonify(ok=False, message="Save amount must be greater than 0."), 400
    row = db.execute("SELECT id, target_amount, saved_amount FROM pocket_native_goals WHERE id = ?", (goal_id,)).fetchone()
    if row is None:
        return jsonify(ok=False, message="Goal not found."), 404
    new_saved = min(float(row["target_amount"] or 0), float(row["saved_amount"] or 0) + amount)
    db.execute(
        "UPDATE pocket_native_goals SET saved_amount = ?, updated_at = ? WHERE id = ?",
        (new_saved, now_sqlite_text(), goal_id),
    )
    db.execute(
        """
        INSERT INTO pocket_native_goal_history (goal_id, amount, saved_at, note, kind)
        VALUES (?, ?, ?, '', 'save')
        """,
        (goal_id, amount, date.today().isoformat()),
    )
    db.commit()
    response = pocket_native_goals()
    response.json["message"] = "Goal money saved."
    return response


@app.get("/api/pocket/native/categories")
def pocket_native_categories():
    kind = "income" if str(request.args.get("kind") or "").strip().lower() == "income" else "expense"
    db = pocket_native_db()
    return jsonify(
        ok=True,
        message="Pocket Pro categories ready.",
        categories={
            "currentKind": kind,
            "categories": pocket_native_all_categories(db, kind),
            "iconOptions": [
                {"key": "wallet", "label": "Wallet", "iconClass": ""},
                {"key": "receipt", "label": "Receipt", "iconClass": ""},
                {"key": "sparkles", "label": "Smart", "iconClass": ""},
            ],
            "colorSwatches": ["#58A6FF", "#22c55e", "#06b6d4", "#f59e0b", "#f472b6", "#ff6a59", "#8b5cf6"],
        },
    )


@app.post("/api/pocket/native/categories/save")
def pocket_native_category_save():
    db = pocket_native_db()
    payload = request.get_json(silent=True) or {}
    kind = "income" if str(payload.get("kind") or "").strip().lower() == "income" else "expense"
    label = str(payload.get("label") or "").strip()
    if not label:
        return jsonify(ok=False, message="Category name is required."), 400
    category_key = pocket_native_slug(str(payload.get("originalKey") or "") or label)
    duplicate = db.execute(
        """
        SELECT id FROM pocket_native_categories
        WHERE kind = ? AND LOWER(label) = LOWER(?) AND category_key <> ?
        LIMIT 1
        """,
        (kind, label, category_key),
    ).fetchone()
    if duplicate is not None:
        return jsonify(ok=False, message="This category already exists."), 409
    db.execute(
        """
        INSERT INTO pocket_native_categories (kind, category_key, label, icon_key, color_hex, sort_order)
        VALUES (?, ?, ?, ?, ?, COALESCE((SELECT MAX(sort_order) + 1 FROM pocket_native_categories WHERE kind = ?), 1))
        ON CONFLICT(kind, category_key) DO UPDATE SET
            label = excluded.label,
            icon_key = excluded.icon_key,
            color_hex = excluded.color_hex,
            is_active = 1,
            updated_at = DATETIME('now')
        """,
        (
            kind,
            category_key,
            label,
            str(payload.get("iconKey") or ""),
            str(payload.get("colorHex") or "#58A6FF"),
            kind,
        ),
    )
    db.commit()
    return pocket_native_categories()


@app.post("/api/pocket/native/categories/delete")
def pocket_native_category_delete():
    db = pocket_native_db()
    payload = request.get_json(silent=True) or {}
    kind = "income" if str(payload.get("kind") or "").strip().lower() == "income" else "expense"
    category_key = pocket_native_slug(str(payload.get("categoryKey") or ""))
    db.execute(
        "UPDATE pocket_native_categories SET is_active = 0, updated_at = ? WHERE kind = ? AND category_key = ?",
        (now_sqlite_text(), kind, category_key),
    )
    db.commit()
    return pocket_native_categories()


@app.post("/api/pocket/native/categories/move")
def pocket_native_category_move():
    return pocket_native_categories()


@app.get("/api/pocket/native/transaction/form")
def pocket_native_transaction_form():
    _, user = pocket_native_current_auth()
    db = pocket_native_db()
    mode = str(request.args.get("mode") or "expense").strip().lower()
    if mode not in {"income", "expense", "lent", "borrow", "repay", "receive"}:
        mode = "expense"
    category_kind = "income" if mode in {"income", "borrow", "receive"} else "expense"
    titles = {
        "income": ("Income", "Save income"),
        "expense": ("Expense", "Save expense"),
        "lent": ("Give Loan", "Save loan"),
        "borrow": ("Take Loan", "Save loan"),
        "repay": ("Pay Back", "Save payment"),
        "receive": ("Get Back", "Save collection"),
    }
    title, submit_label = titles.get(mode, titles["expense"])
    return jsonify(
        ok=True,
        message="Pocket Pro form ready.",
        user=user,
        form={
            "mode": mode,
            **pocket_native_currency_payload(),
            "defaultEntryDate": date.today().isoformat(),
            "defaultAccountId": 1,
            "defaultCategoryKey": pocket_native_all_categories(db, category_kind)[0]["key"],
            "defaultPaymentMethod": "CASH",
            "title": title,
            "subtitle": "Fast entry",
            "partyLabel": "Name",
            "submitLabel": submit_label,
            "categorySettingsUrl": "",
            "categoryCreateUrl": "",
            "categoryEditBaseUrl": "",
            "accounts": [
                {
                    "id": 1,
                    "name": "My Wallet",
                    "type": "CASH",
                    "typeLabel": "Cash",
                    "currentBalance": 0.0,
                    "isDefault": True,
                    "accentHex": "#58A6FF",
                }
            ],
            "categories": pocket_native_all_categories(db, category_kind),
            "paymentMethods": [
                {"key": "CASH", "label": "Cash"},
                {"key": "BANK", "label": "Bank"},
                {"key": "MOBILE_BANKING", "label": "Mobile banking"},
            ],
            "isEditMode": False,
            "editEntryKind": "",
            "editEntryId": 0,
            "initialAmount": 0.0,
            "initialPartyName": "",
            "initialNote": "",
            "deleteUrl": "",
        },
    )


@app.post("/api/pocket/native/transaction")
def pocket_native_transaction_save():
    tenant = get_current_tenant()
    current_user = get_current_tenant_user()
    db = pocket_native_db()
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "expense").strip().lower()
    amount = max(0.0, float(payload.get("amount") or 0))
    if amount <= 0:
        return jsonify(ok=False, message="Amount must be greater than 0."), 400
    entry_date = normalize_date(str(payload.get("entryDate") or payload.get("entry_date") or payload.get("date") or ""))
    category_key = pocket_native_slug(str(payload.get("categoryKey") or payload.get("category_key") or ""), "misc")
    payment_method = str(payload.get("paymentMethod") or payload.get("payment_method") or "CASH").strip().upper() or "CASH"
    party_name = str(payload.get("partyName") or payload.get("party_name") or "").strip()
    note = str(payload.get("note") or "").strip()
    user_id = int(row_value(current_user, "id", 0) or 0) if current_user is not None else None
    username = str(row_value(current_user, "username", "") or "") if current_user is not None else ""
    if mode in {"income", "borrow", "receive"}:
        db.execute(
            """
            INSERT INTO incomes (
                income_date, category, source_name, amount, payment_method, branch_id, note,
                entered_by_user_id, entered_by_username, approval_status, approved_by_user_id, approved_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 'APPROVED', ?, ?, ?, ?)
            """,
            (entry_date, category_key, party_name, amount, payment_method, note, user_id, username, user_id, now_sqlite_text(), now_sqlite_text(), now_sqlite_text()),
        )
        message = "Income saved."
    else:
        db.execute(
            """
            INSERT INTO expenses (
                expense_date, category, employee_name, amount, payment_method, branch_id, note,
                entered_by_user_id, entered_by_username, approval_status, approved_by_user_id, approved_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 'APPROVED', ?, ?, ?, ?)
            """,
            (entry_date, category_key, party_name, amount, payment_method, note, user_id, username, user_id, now_sqlite_text(), now_sqlite_text(), now_sqlite_text()),
        )
        message = "Expense saved."
    db.commit()
    return jsonify(ok=True, message=message)


@app.post("/api/pocket/native/records/delete")
def pocket_native_record_delete():
    db = pocket_native_db()
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind") or "").strip().lower()
    record_id = int(payload.get("id") or 0)
    if kind == "income":
        db.execute("DELETE FROM incomes WHERE id = ?", (record_id,))
    elif kind == "expense":
        db.execute("DELETE FROM expenses WHERE id = ?", (record_id,))
    else:
        return jsonify(ok=False, message="Invalid record."), 400
    db.commit()
    return jsonify(ok=True, message="Record deleted.")


@app.route("/api/pocket/native/<path:proxy_path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def pocket_native_compat_proxy(proxy_path: str):
    return proxy_pocket_legacy_request(f"api/pocket/native/{proxy_path}")


@app.route("/pocket/<path:proxy_path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def pocket_page_compat_proxy(proxy_path: str):
    return proxy_pocket_legacy_request(f"pocket/{proxy_path}")


@app.route("/pocket-pro/<path:proxy_path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def pocket_web_compat_proxy(proxy_path: str):
    return proxy_pocket_legacy_request(f"pocket-pro/{proxy_path}")


@app.get("/terms")
@app.get("/terms-and-conditions")
@app.get("/terms-of-use")
@app.get("/pocket/terms-of-use")
def pocket_terms_of_use():
    if not is_pocket_public_host():
        return proxy_pocket_legacy_request("pocket/terms-of-use")
    return make_response(
        render_template(
            "pocket_static_page.html",
            page_title="Terms of Use",
            page_heading="Pocket Pro Terms of Use",
            intro_text="Pocket Pro is a personal money manager for budgets, reminders, goals, loans, and installment tracking.",
            sections=[
                (
                    "Account use",
                    [
                        "Use one account per person and keep your password private.",
                        "You are responsible for the entries, reminders, and records saved in your account.",
                    ],
                ),
                (
                    "Data and backup",
                    [
                        "Pocket Pro stores your finance information to help you review balances, budgets, reminders, and goals.",
                        "Keep your own backup/export habits active for extra safety.",
                    ],
                ),
                (
                    "Fair use",
                    [
                        "Do not misuse Pocket Pro for fraud, spam, or illegal activity.",
                        "We may change or improve features over time to keep the app stable.",
                    ],
                ),
            ],
        )
    )


@app.get("/privacy")
@app.get("/privacy-policy")
@app.get("/pocket/privacy-policy")
def pocket_privacy_policy():
    if not is_pocket_public_host():
        return proxy_pocket_legacy_request("pocket/privacy-policy")
    return make_response(
        render_template(
            "pocket_static_page.html",
            page_title="Privacy Policy",
            page_heading="Pocket Pro Privacy Policy",
            intro_text="Pocket Pro only uses the information needed to support your money records, reminders, goals, and account access.",
            sections=[
                (
                    "What we store",
                    [
                        "Basic account details such as email, username, and profile information.",
                        "Your finance records, budgets, goals, reminders, and related app settings.",
                    ],
                ),
                (
                    "Why we store it",
                    [
                        "To show your account data, sync your app experience, and keep your records available when you sign in again.",
                        "To improve stability, support, and product quality.",
                    ],
                ),
                (
                    "Your control",
                    [
                        "You can update your profile information inside the app.",
                        "You can request removal or support help through the Pocket Pro support channel.",
                    ],
                ),
            ],
        )
    )


@app.get("/account-deletion")
@app.get("/pocket/account-deletion")
def pocket_account_deletion():
    if not is_pocket_public_host():
        return proxy_pocket_legacy_request("pocket/account-deletion")
    return make_response(
        render_template(
            "pocket_static_page.html",
            page_title="Account Deletion",
            page_heading="Pocket Pro Account Deletion",
            intro_text="Pocket Pro users can request account deletion and removal of stored account data from this page.",
            sections=[
                (
                    "How to request deletion",
                    [
                        "Send an account deletion request from the email address used for your Pocket Pro account.",
                        "Include your Pocket Pro username and write that you want your account and related app data deleted.",
                    ],
                ),
                (
                    "What will be deleted",
                    [
                        "Your account profile, finance records, budgets, goals, reminders, and app data connected to that Pocket Pro account.",
                        "Some limited records may be retained only when required for legal, security, or abuse-prevention reasons.",
                    ],
                ),
                (
                    "Support contact",
                    [
                        "Email your deletion request to support@corexbd.com.",
                        "Deletion requests are reviewed and processed as soon as reasonably possible.",
                    ],
                ),
            ],
        )
    )


@app.get("/pocket-pro/open")
def pocket_pro_open():
    if is_pocket_public_host():
        return redirect(url_for("login_selector"))
    return proxy_pocket_legacy_request("pocket-pro/open")


@app.get("/login")
def login_selector():
    tenant = get_current_tenant()
    if tenant is not None and get_current_tenant_user() is not None:
        return redirect(url_for(get_tenant_default_endpoint(tenant)))
    if is_superadmin_logged_in():
        return redirect(url_for("admin_accounts"))
    view_mode = (request.args.get("view", "login") or "login").strip().lower()
    if view_mode not in {"login", "register"}:
        view_mode = "login"
    mode = (request.args.get("mode", "user") or "user").strip().lower()
    if mode not in {"admin", "user"}:
        mode = "user"
    login_prefill = normalize_login_identifier(request.args.get("login", ""))
    next_path = request.args.get("next", "").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

    if is_pocket_public_host():
        selected_module = "POCKET_MONEY"
        module_profile = MODULE_PROFILE_POCKET
        mode = "admin"
        business_modules = [item for item in get_business_module_options() if str(item["key"]) == "POCKET_MONEY"]
    else:
        module_from_query = normalize_business_module(request.args.get("module", ""), default="")
        module_profile_arg = normalize_module_profile(request.args.get("profile", MODULE_PROFILE_ALL), default=MODULE_PROFILE_ALL)

        if module_from_query:
            selected_module = module_from_query
        elif module_profile_arg == MODULE_PROFILE_POCKET:
            selected_module = "POCKET_MONEY"
        else:
            selected_module = DEFAULT_PRIMARY_BUSINESS

        if not is_public_launch_module_enabled(selected_module):
            selected_module = DEFAULT_PRIMARY_BUSINESS

        if selected_module == "POCKET_MONEY":
            module_profile = MODULE_PROFILE_POCKET
            mode = "admin"
        else:
            module_profile = MODULE_PROFILE_BUSINESS

        business_modules = get_business_module_options()
    selected_module_info = next(
        (item for item in business_modules if str(item["key"]) == selected_module),
        next((item for item in business_modules if str(item["key"]) == DEFAULT_PRIMARY_BUSINESS), business_modules[0]),
    )

    return render_template(
        "login_selector.html",
        module_profile=module_profile,
        business_modules=business_modules,
        selected_module=selected_module,
        selected_module_info=selected_module_info,
        selected_is_pocket=(selected_module == "POCKET_MONEY"),
        view_mode=view_mode,
        mode=mode,
        next_path=next_path,
        login_prefill=login_prefill,
    )


@app.get("/login/user")
def client_login_user_entry():
    login_hint = normalize_login_identifier(
        request.args.get("login", "") or request.args.get("user", "")
    )
    next_path = request.args.get("next", "").strip()
    module_profile = normalize_module_profile(request.args.get("profile", MODULE_PROFILE_BUSINESS))
    if is_pocket_public_host():
        module_profile = MODULE_PROFILE_POCKET
    redirect_kwargs: dict[str, str] = {
        "mode": "admin" if module_profile == MODULE_PROFILE_POCKET else "user",
        "profile": module_profile.lower(),
    }
    if login_hint:
        redirect_kwargs["login"] = login_hint
    if next_path.startswith("/") and not next_path.startswith("//"):
        redirect_kwargs["next"] = next_path
    return redirect(url_for("client_login", **redirect_kwargs))


@app.get("/login/admin")
def client_login_admin_entry():
    login_hint = normalize_login_identifier(
        request.args.get("login", "") or request.args.get("shop", "")
    )
    next_path = request.args.get("next", "").strip()
    module_profile = normalize_module_profile(request.args.get("profile", MODULE_PROFILE_BUSINESS))
    if is_pocket_public_host():
        module_profile = MODULE_PROFILE_POCKET
    redirect_kwargs: dict[str, str] = {"mode": "admin", "profile": module_profile.lower()}
    if login_hint:
        redirect_kwargs["login"] = login_hint
    if next_path.startswith("/") and not next_path.startswith("//"):
        redirect_kwargs["next"] = next_path
    return redirect(url_for("client_login", **redirect_kwargs))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    tenant = get_current_tenant()
    if tenant is not None and get_current_tenant_user() is not None:
        return redirect(url_for(get_tenant_default_endpoint(tenant)))
    if is_superadmin_logged_in():
        return redirect(url_for("admin_accounts"))

    mode = (request.values.get("mode", "user") or "user").strip().lower()
    if mode not in {"admin", "user"}:
        mode = "user"
    if is_pocket_public_host():
        selected_module = "POCKET_MONEY"
        module_profile = MODULE_PROFILE_POCKET
        mode = "admin"
    else:
        module_from_query = normalize_business_module(request.values.get("module", ""), default="")
        module_profile = normalize_module_profile(
            request.values.get("profile", MODULE_PROFILE_ALL),
            default=MODULE_PROFILE_ALL,
        )

        if module_from_query:
            selected_module = module_from_query
        elif module_profile == MODULE_PROFILE_POCKET:
            selected_module = "POCKET_MONEY"
        else:
            selected_module = DEFAULT_PRIMARY_BUSINESS

        if selected_module == "POCKET_MONEY":
            module_profile = MODULE_PROFILE_POCKET
            mode = "admin"
        else:
            module_profile = MODULE_PROFILE_BUSINESS
    next_path = request.values.get("next", "").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

    if request.method == "POST":
        login_identifier = normalize_login_identifier(request.form.get("login_identifier", ""))
        if not login_identifier:
            flash("Enter your Login ID or Email.", "error")
            return redirect(
                url_for(
                    "forgot_password",
                    profile=module_profile.lower(),
                    module=selected_module.lower(),
                    mode=mode,
                    next=next_path,
                )
            )

        flash(
            "Password reset is admin-assisted now. Contact your shop admin or Soft X support with your Login ID.",
            "success",
        )
        login_kwargs: dict[str, str] = {
            "view": "login",
            "profile": module_profile.lower(),
            "module": selected_module.lower(),
            "mode": mode,
            "login": login_identifier,
        }
        if next_path:
            login_kwargs["next"] = next_path
        return redirect(url_for("login_selector", **login_kwargs))

    return render_template(
        "forgot_password.html",
        module_profile=module_profile,
        selected_module=selected_module,
        selected_is_pocket=(selected_module == "POCKET_MONEY"),
        mode=mode,
        next_path=next_path,
    )


@app.route("/shop-login", methods=["GET", "POST"])
def client_login():
    try:
        existing_tenant = get_current_tenant()
        if existing_tenant is not None and get_current_tenant_user() is not None:
            return redirect(url_for(get_tenant_default_endpoint(existing_tenant)))
    except Exception:
        session.pop("tenant_id", None)
        session.pop("tenant_user_id", None)
        g.pop("current_tenant", None)
        g.pop("current_tenant_user", None)

    mode = (request.values.get("mode", "user") or "user").strip().lower()
    if mode not in {"user", "admin"}:
        mode = "user"
    forced_admin_login = mode == "admin"
    module_profile = normalize_module_profile(
        request.values.get("profile", request.values.get("module_profile", MODULE_PROFILE_BUSINESS)),
        default=MODULE_PROFILE_BUSINESS,
    )
    selected_module_key = normalize_business_module(request.values.get("module_key", ""), default="")
    if is_pocket_public_host():
        module_profile = MODULE_PROFILE_POCKET
        selected_module_key = "POCKET_MONEY"
        mode = "admin"
        forced_admin_login = True
    elif module_profile == MODULE_PROFILE_POCKET:
        selected_module_key = "POCKET_MONEY"
        mode = "admin"
        forced_admin_login = True
    elif selected_module_key == "POCKET_MONEY":
        selected_module_key = ""
    gateway_mode = request.values.get("gateway", "").strip() == "1"

    prefill_identifier = normalize_login_identifier(request.args.get("login", ""))
    if not prefill_identifier and forced_admin_login:
        prefill_identifier = normalize_login_identifier(request.args.get("shop", ""))
    if not prefill_identifier and (not forced_admin_login):
        prefill_identifier = normalize_login_identifier(request.args.get("user", ""))
    next_path = request.values.get("next", "").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

    if request.method == "POST":
        try:
            login_identifier = normalize_login_identifier(request.form.get("login_identifier", ""))
            if not login_identifier and prefill_identifier:
                login_identifier = prefill_identifier
            password = request.form.get("password", "")
            attempt_key = f"{mode}:{login_identifier}"
            if gateway_mode:
                login_redirect_kwargs: dict[str, str] = {
                    "view": "login",
                    "mode": mode,
                    "profile": module_profile.lower(),
                }
                if selected_module_key:
                    login_redirect_kwargs["module"] = selected_module_key.lower()
                if login_identifier:
                    login_redirect_kwargs["login"] = login_identifier
                if next_path:
                    login_redirect_kwargs["next"] = next_path
                login_redirect = url_for("login_selector", **login_redirect_kwargs)
            else:
                login_redirect_kwargs = {"mode": mode, "profile": module_profile.lower()}
                if selected_module_key:
                    login_redirect_kwargs["module_key"] = selected_module_key.lower()
                if login_identifier:
                    login_redirect_kwargs["login"] = login_identifier
                if next_path:
                    login_redirect_kwargs["next"] = next_path
                login_redirect = url_for("client_login", **login_redirect_kwargs)

            if not login_identifier or not password:
                flash("Username/Email and Password are required.", "error")
                return redirect(login_redirect)

            blocked, seconds_left = check_login_blocked("TENANT", attempt_key)
            if blocked:
                minutes_left = max(1, (seconds_left + 59) // 60)
                flash(f"Too many failed attempts. Try again after {minutes_left} minute(s).", "error")
                write_audit_log(
                    action="TENANT_LOGIN_BLOCKED",
                    actor_type="TENANT_LOGIN",
                    actor_username=attempt_key,
                    actor_role="LOGIN",
                    metadata={"mode": mode, "login_identifier": login_identifier},
                )
                return redirect(login_redirect)

            admin_db = get_admin_db()
            try:
                active_accounts = admin_db.execute(
                    """
                    SELECT
                        id, shop_name, username, password_hash, is_active,
                        paid_until, billing_cycle, monthly_fee, db_path,
                        ui_language, primary_business, enabled_modules
                    FROM tenant_accounts
                    WHERE is_active = 1
                    ORDER BY id DESC
                    """
                ).fetchall()
            except sqlite3.Error:
                init_admin_db()
                admin_db = get_admin_db()
                active_accounts = admin_db.execute(
                    """
                    SELECT
                        id, shop_name, username, password_hash, is_active,
                        paid_until, billing_cycle, monthly_fee, db_path,
                        ui_language, primary_business, enabled_modules
                    FROM tenant_accounts
                    WHERE is_active = 1
                    ORDER BY id DESC
                    """
                ).fetchall()

            if module_profile != MODULE_PROFILE_ALL:
                active_accounts = [
                    row
                    for row in active_accounts
                    if account_matches_module_profile(row, module_profile)
                ]

            if selected_module_key:
                filtered_accounts: list[sqlite3.Row] = []
                for row in active_accounts:
                    primary_module = normalize_business_module(
                        str(row["primary_business"] or ""),
                        default=DEFAULT_PRIMARY_BUSINESS,
                    )
                    enabled_modules = parse_enabled_modules(
                        str(row["enabled_modules"] or ""),
                        fallback_primary=primary_module,
                    )
                    if selected_module_key == primary_module or selected_module_key in enabled_modules:
                        filtered_accounts.append(row)
                active_accounts = filtered_accounts

            if not active_accounts:
                flash(
                    "No active account found for selected module. Try another module or contact support.",
                    "error",
                )
                return redirect(login_redirect)

            selected_account: sqlite3.Row | None = None
            tenant_user_payload: dict[str, object] | None = None
            expired_subscription_until: str | None = None

            if forced_admin_login:
                matching_accounts = [
                    row
                    for row in active_accounts
                    if normalize_login_identifier(str(row["username"])) == login_identifier
                ]
                tenant_setup_error = False
                for account in matching_accounts:
                    if not is_tenant_subscription_active(account):
                        if expired_subscription_until is None:
                            expired_subscription_until = str(account["paid_until"] or "N/A")
                        continue

                    tenant_db_path = ensure_tenant_db_ready(admin_db, account)
                    if tenant_db_path is None:
                        tenant_setup_error = True
                        continue

                    tenant_admin_user: sqlite3.Row | None = None
                    try:
                        with sqlite3.connect(tenant_db_path) as tenant_conn:
                            tenant_conn.row_factory = sqlite3.Row
                            tenant_conn.execute("PRAGMA foreign_keys = ON;")
                            ensure_tenant_users_table(tenant_conn)
                            tenant_admin_user = tenant_conn.execute(
                                """
                                SELECT id, username, full_name, role, password_hash, is_active
                                FROM users
                                WHERE role = 'ADMIN' AND is_active = 1
                                ORDER BY CASE WHEN LOWER(username) = 'admin' THEN 0 ELSE 1 END, id ASC
                                LIMIT 1
                                """
                            ).fetchone()
                    except sqlite3.Error:
                        tenant_setup_error = True
                        continue

                    if (
                        tenant_admin_user is not None
                        and password_matches(str(tenant_admin_user["password_hash"]), password)
                    ):
                        selected_account = account
                        tenant_user_payload = {
                            "id": int(tenant_admin_user["id"]),
                            "username": str(tenant_admin_user["username"]),
                            "full_name": str(tenant_admin_user["full_name"] or ""),
                            "role": "ADMIN",
                        }
                        break

                    if password_matches(str(account["password_hash"]), password):
                        # Backward compatible bootstrap for old account-level password login.
                        try:
                            admin_user_id = create_or_update_tenant_user(
                                db_path=tenant_db_path,
                                username="admin",
                                full_name=f"{account['shop_name']} Admin",
                                role="ADMIN",
                                password=password,
                                is_active=True,
                            )
                        except sqlite3.Error:
                            tenant_setup_error = True
                            continue
                        selected_account = account
                        tenant_user_payload = {
                            "id": admin_user_id,
                            "username": "admin",
                            "full_name": f"{account['shop_name']} Admin",
                            "role": "ADMIN",
                        }
                        break
                if selected_account is None and tenant_setup_error:
                    flash("Account setup error. Please contact support/admin.", "error")
                    return redirect(login_redirect)
            else:
                user_matches: list[tuple[sqlite3.Row, dict[str, object]]] = []
                tenant_setup_error = False
                for account in active_accounts:
                    tenant_db_path = ensure_tenant_db_ready(admin_db, account)
                    if tenant_db_path is None:
                        tenant_setup_error = True
                        continue

                    tenant_user: sqlite3.Row | None = None
                    try:
                        with sqlite3.connect(tenant_db_path) as tenant_conn:
                            tenant_conn.row_factory = sqlite3.Row
                            tenant_conn.execute("PRAGMA foreign_keys = ON;")
                            ensure_tenant_users_table(tenant_conn)
                            tenant_user = tenant_conn.execute(
                                """
                                SELECT id, username, full_name, role, password_hash, is_active
                                FROM users
                                WHERE LOWER(username) = ?
                                LIMIT 1
                                """,
                                (login_identifier,),
                            ).fetchone()
                    except sqlite3.Error:
                        tenant_setup_error = True
                        continue

                    if tenant_user is None or int(tenant_user["is_active"]) != 1:
                        continue

                    if not password_matches(str(tenant_user["password_hash"]), password):
                        continue

                    if not is_tenant_subscription_active(account):
                        if expired_subscription_until is None:
                            expired_subscription_until = str(account["paid_until"] or "N/A")
                        continue

                    payload = {
                        "id": int(tenant_user["id"]),
                        "username": str(tenant_user["username"]),
                        "full_name": str(tenant_user["full_name"] or ""),
                        "role": normalize_role(str(tenant_user["role"]), default="USER"),
                    }
                    user_matches.append((account, payload))

                if len(user_matches) == 1:
                    selected_account, tenant_user_payload = user_matches[0]
                elif len(user_matches) > 1:
                    now_blocked, now_seconds = register_login_failure("TENANT", attempt_key)
                    write_audit_log(
                        action="TENANT_LOGIN_FAILED",
                        actor_type="TENANT_LOGIN",
                        actor_username=attempt_key,
                        actor_role="LOGIN",
                        metadata={"mode": mode, "reason": "ambiguous_username"},
                    )
                    if now_blocked:
                        minutes_left = max(1, (now_seconds + 59) // 60)
                        flash(f"Login blocked for {minutes_left} minute(s).", "error")
                    else:
                        flash("Same username found in multiple shops. Use a unique username/email.", "error")
                    return redirect(login_redirect)
                elif tenant_setup_error and not user_matches:
                    flash("Some shop accounts are not configured correctly. Contact admin.", "error")
                    return redirect(login_redirect)

            if selected_account is None or tenant_user_payload is None:
                now_blocked, now_seconds = register_login_failure("TENANT", attempt_key)
                write_audit_log(
                    action="TENANT_LOGIN_FAILED",
                    actor_type="TENANT_LOGIN",
                    actor_username=attempt_key,
                    actor_role="LOGIN",
                    metadata={"mode": mode, "reason": "invalid_credentials"},
                )
                if now_blocked:
                    minutes_left = max(1, (now_seconds + 59) // 60)
                    flash(f"Login blocked for {minutes_left} minute(s).", "error")
                    return redirect(login_redirect)
                if expired_subscription_until is not None:
                    flash(
                        f"Subscription expired ({expired_subscription_until}). মাসিক বিল পরিশোধ করে login করুন।",
                        "error",
                    )
                else:
                    flash("Login failed. Username/Email/Password check করুন।", "error")
                return redirect(login_redirect)

            clear_login_failures("TENANT", attempt_key)
            write_audit_log(
                action="TENANT_LOGIN_SUCCESS",
                tenant_id=int(selected_account["id"]),
                actor_type="TENANT_USER",
                actor_username=str(tenant_user_payload["username"]),
                actor_role=str(tenant_user_payload["role"]),
                metadata={"mode": mode, "login_identifier": login_identifier},
            )
            session.clear()
            session["tenant_id"] = int(selected_account["id"])
            session["tenant_user_id"] = int(tenant_user_payload["id"])
            session["ui_lang"] = normalize_language(str(selected_account["ui_language"])) or DEFAULT_UI_LANGUAGE
            flash(
                f"Welcome {tenant_user_payload['role']} | {tenant_user_payload['username']} ({selected_account['shop_name']}).",
                "success",
            )
            default_endpoint = get_module_default_endpoint(str(selected_account["primary_business"] or ""))
            target = safe_next_path(default_endpoint=default_endpoint)
            return redirect(target)
        except Exception as exc:
            print("SOFTX LOGIN ERROR:", repr(exc))
            print(traceback.format_exc())
            session.pop("tenant_id", None)
            session.pop("tenant_user_id", None)
            g.pop("current_tenant", None)
            g.pop("current_tenant_user", None)
            flash("Login system error. Please retry. যদি আবার হয়, support-এ screenshot দিন।", "error")
            if gateway_mode:
                error_redirect_kwargs: dict[str, str] = {
                    "view": "login",
                    "mode": mode,
                    "profile": module_profile.lower(),
                }
                if selected_module_key:
                    error_redirect_kwargs["module"] = selected_module_key.lower()
                if next_path:
                    error_redirect_kwargs["next"] = next_path
                return redirect(url_for("login_selector", **error_redirect_kwargs))
            return redirect(
                url_for(
                    "client_login",
                    mode=mode,
                    profile=module_profile.lower(),
                    module_key=selected_module_key.lower() if selected_module_key else "",
                )
            )

    return render_template(
        "login.html",
        mode=mode,
        module_profile=module_profile,
        next_path=next_path,
        prefill_login_identifier=prefill_identifier,
        lock_login_identifier=bool(prefill_identifier),
        selected_module_key=selected_module_key,
        gateway_mode=gateway_mode,
    )


@app.route("/register", methods=["GET", "POST"])
def client_register():
    tenant = get_current_tenant()
    if tenant is not None and get_current_tenant_user() is not None:
        return redirect(url_for(get_tenant_default_endpoint(tenant)))
    if is_superadmin_logged_in():
        return redirect(url_for("admin_accounts"))

    force_pocket_host = is_pocket_public_host()
    module_profile = normalize_module_profile(
        request.values.get(
            "profile",
            request.values.get(
                "module_profile",
                MODULE_PROFILE_POCKET if force_pocket_host else MODULE_PROFILE_BUSINESS,
            ),
        ),
        default=MODULE_PROFILE_POCKET if force_pocket_host else MODULE_PROFILE_BUSINESS,
    )
    if not force_pocket_host and module_profile == MODULE_PROFILE_POCKET:
        module_profile = MODULE_PROFILE_BUSINESS
    selected_module_key = normalize_business_module(request.values.get("module_key", ""), default="")
    if force_pocket_host:
        selected_module_key = "POCKET_MONEY"
    elif selected_module_key == "POCKET_MONEY":
        selected_module_key = ""
    if (not force_pocket_host) and selected_module_key and not is_public_launch_module_enabled(selected_module_key):
        selected_module_key = DEFAULT_PRIMARY_BUSINESS
    gateway_mode = request.values.get("gateway", "").strip() == "1"
    is_pocket_profile = module_profile == MODULE_PROFILE_POCKET

    if force_pocket_host:
        business_modules = [item for item in get_business_module_options() if item["key"] == "POCKET_MONEY"]
    else:
        business_modules = [item for item in get_business_module_options() if item["key"] != "POCKET_MONEY"]
    if not business_modules:
        business_modules = get_business_module_options()

    default_primary_business = DEFAULT_PRIMARY_BUSINESS
    if selected_module_key and selected_module_key != "POCKET_MONEY":
        default_primary_business = selected_module_key
    next_path = request.values.get("next", "").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = ""

    def build_register_redirect(profile_value: str, module_value: str = "") -> str:
        if gateway_mode:
            redirect_kwargs: dict[str, str] = {
                "view": "register",
                "mode": "admin",
                "profile": profile_value.lower(),
            }
            if module_value:
                redirect_kwargs["module"] = module_value.lower()
            if next_path:
                redirect_kwargs["next"] = next_path
            return url_for("login_selector", **redirect_kwargs)
        redirect_kwargs = {"profile": profile_value.lower()}
        if module_value:
            redirect_kwargs["module_key"] = module_value.lower()
        if next_path:
            redirect_kwargs["next"] = next_path
        return url_for("client_register", **redirect_kwargs)

    if request.method == "POST":
        module_profile = normalize_module_profile(
            request.form.get("module_profile", module_profile),
            default=MODULE_PROFILE_POCKET if force_pocket_host else MODULE_PROFILE_BUSINESS,
        )
        if not force_pocket_host and module_profile == MODULE_PROFILE_POCKET:
            module_profile = MODULE_PROFILE_BUSINESS
        selected_module_key = normalize_business_module(
            request.form.get("module_key", selected_module_key),
            default="",
        )
        if force_pocket_host:
            selected_module_key = "POCKET_MONEY"
        elif selected_module_key == "POCKET_MONEY":
            selected_module_key = ""
        if (not force_pocket_host) and selected_module_key and not is_public_launch_module_enabled(selected_module_key):
            selected_module_key = DEFAULT_PRIMARY_BUSINESS
        is_pocket_profile = module_profile == MODULE_PROFILE_POCKET

        shop_name = request.form.get("shop_name", "").strip()
        owner_name = request.form.get("owner_name", "").strip()
        phone = request.form.get("phone", "").strip()
        username = normalize_username(request.form.get("username", ""))
        pocket_full_name = request.form.get("full_name", "").strip()
        pocket_user_name = request.form.get("user_name", "").strip()
        pocket_login_id = normalize_username(request.form.get("login_id", ""))
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        ui_language = normalize_language(request.form.get("ui_language", "")) or DEFAULT_UI_LANGUAGE

        if is_pocket_profile:
            owner_name = pocket_full_name
            shop_name = pocket_user_name
            username = pocket_login_id or normalize_username(pocket_user_name) or username
            selected_primary_business = "POCKET_MONEY"
            selected_modules = ["POCKET_MONEY"]
            monthly_fee = 99.0
            billing_note = "Self signup: pocket money profile"
        else:
            selected_primary_business = DEFAULT_PRIMARY_BUSINESS
            selected_modules = [DEFAULT_PRIMARY_BUSINESS]
            monthly_fee = 0.0
            billing_note = "Self signup: business profile"

        enabled_modules = ",".join(selected_modules)

        if is_pocket_profile and not owner_name:
            flash("Full Name is required.", "error")
            return redirect(build_register_redirect(module_profile, selected_module_key))
        if is_pocket_profile and not shop_name:
            flash("User Name is required.", "error")
            return redirect(build_register_redirect(module_profile, selected_module_key))
        if not shop_name:
            flash("Shop/Profile name is required.", "error")
            return redirect(build_register_redirect(module_profile, selected_module_key))
        if len(username) < 4:
            flash("Login ID must be at least 4 characters.", "error")
            return redirect(build_register_redirect(module_profile, selected_module_key))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(build_register_redirect(module_profile, selected_module_key))
        if password != confirm_password:
            flash("Password and confirm password do not match.", "error")
            return redirect(build_register_redirect(module_profile, selected_module_key))

        admin_db = get_admin_db()
        db_path = tenant_db_path_for_username(username)

        existing = admin_db.execute(
            "SELECT id FROM tenant_accounts WHERE username = ?",
            (username,),
        ).fetchone()
        if existing is not None:
            flash("This login ID already exists. Use another username/email.", "error")
            return redirect(build_register_redirect(module_profile, selected_module_key))

        if db_path.exists():
            flash("Tenant data path already exists. Use another login ID.", "error")
            return redirect(build_register_redirect(module_profile, selected_module_key))

        try:
            init_db_for_path(db_path)
            admin_full_name = owner_name or f"{shop_name} Admin"
            create_or_update_tenant_user(
                db_path=db_path,
                username="admin",
                full_name=admin_full_name,
                role="ADMIN",
                password=password,
                is_active=True,
            )
            if username != "admin":
                create_or_update_tenant_user(
                    db_path=db_path,
                    username=username,
                    full_name=admin_full_name,
                    role="ADMIN",
                    password=password,
                    is_active=True,
                )

            admin_db.execute(
                """
                INSERT INTO tenant_accounts (
                    shop_name, owner_name, phone, username, password_hash, db_path,
                    ui_language, primary_business, enabled_modules,
                    billing_cycle, monthly_fee, paid_until, billing_note, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    shop_name,
                    owner_name,
                    phone,
                    username,
                    make_password_hash(password),
                    str(db_path),
                    ui_language,
                    selected_primary_business,
                    enabled_modules,
                    "MONTHLY",
                    monthly_fee,
                    None,
                    billing_note,
                ),
            )
            tenant_id = int(admin_db.execute("SELECT last_insert_rowid()").fetchone()[0])
            admin_db.commit()
            write_audit_log(
                action="TENANT_SELF_REGISTERED",
                tenant_id=tenant_id,
                actor_type="PUBLIC_SIGNUP",
                actor_username=username,
                actor_role="PUBLIC",
                metadata={
                    "shop_name": shop_name,
                    "owner_name": owner_name,
                    "primary_business": selected_primary_business,
                    "enabled_modules": enabled_modules,
                },
            )
            flash(
                f"Registration successful for {shop_name}. Now login with ID: {username}",
                "success",
            )
            if gateway_mode:
                login_kwargs: dict[str, str] = {
                    "view": "login",
                    "mode": "admin",
                    "profile": module_profile.lower(),
                    "login": username,
                }
                if selected_primary_business:
                    login_kwargs["module"] = selected_primary_business.lower()
                if next_path:
                    login_kwargs["next"] = next_path
                return redirect(url_for("login_selector", **login_kwargs))
            login_kwargs = {
                "mode": "admin",
                "profile": module_profile.lower(),
                "login": username,
            }
            if selected_module_key:
                login_kwargs["module_key"] = selected_module_key.lower()
            if next_path:
                login_kwargs["next"] = next_path
            return redirect(url_for("client_login", **login_kwargs))
        except sqlite3.IntegrityError:
            admin_db.rollback()
            flash("Registration failed due to duplicate entry. Try another login ID.", "error")
        except Exception as exc:
            admin_db.rollback()
            flash(f"Registration failed: {exc}", "error")
        return redirect(build_register_redirect(module_profile, selected_module_key))

    return render_template(
        "register.html",
        module_profile=module_profile,
        business_modules=business_modules,
        default_primary_business=default_primary_business,
        next_path=next_path,
        selected_module_key=selected_module_key,
        gateway_mode=gateway_mode,
    )


@app.get("/signup")
def client_register_alias():
    query_profile = request.args.get("profile", MODULE_PROFILE_BUSINESS)
    return redirect(url_for("client_register", profile=query_profile))


@app.get("/owner-login")
def owner_login_alias():
    return redirect(url_for("superadmin_login"))


@app.get("/s/<shop_username>")
def shop_direct_login(shop_username: str):
    clean_shop_username = normalize_username(shop_username)
    if not clean_shop_username:
        return redirect(url_for("client_login", mode="user"))
    return redirect(url_for("client_login", mode="admin", login=clean_shop_username))


@app.get("/logout")
def client_logout():
    tenant = get_current_tenant()
    tenant_user = get_current_tenant_user()
    if tenant is not None and tenant_user is not None:
        write_audit_log(
            action="TENANT_LOGOUT",
            tenant_id=int(tenant["id"]),
            actor_type="TENANT_USER",
            actor_username=str(tenant_user["username"]),
            actor_role=str(tenant_user["role"]),
            metadata={"shop_name": tenant["shop_name"]},
        )
    session.pop("tenant_id", None)
    session.pop("tenant_user_id", None)
    g.pop("current_tenant", None)
    g.pop("current_tenant_user", None)
    flash("Logged out successfully.", "success")
    return redirect(url_for("login_selector"))


@app.route("/superadmin", methods=["GET", "POST"])
def superadmin_login():
    return admin_login()


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if is_superadmin_logged_in():
        return redirect(url_for("admin_accounts"))

    login_endpoint = "superadmin_login" if request.endpoint == "superadmin_login" else "admin_login"

    if request.method == "POST":
        username = normalize_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        otp_input = request.form.get("otp", "").strip()
        configured_user, configured_pass = get_superadmin_credentials()
        attempt_key = normalize_username(configured_user) or "superadmin"

        blocked, seconds_left = check_login_blocked("SUPERADMIN", attempt_key)
        if blocked:
            minutes_left = max(1, (seconds_left + 59) // 60)
            flash(f"Too many failed attempts. Try again after {minutes_left} minute(s).", "error")
            write_audit_log(
                action="SUPERADMIN_LOGIN_BLOCKED",
                actor_type="SUPERADMIN_LOGIN",
                actor_username=attempt_key,
                actor_role="LOGIN",
            )
            return redirect(url_for(login_endpoint))

        otp_ok = True if not SUPERADMIN_OTP else (otp_input == SUPERADMIN_OTP)
        if username == normalize_username(configured_user) and password == configured_pass and otp_ok:
            clear_login_failures("SUPERADMIN", attempt_key)
            session.clear()
            session["is_superadmin"] = True
            write_audit_log(
                action="SUPERADMIN_LOGIN_SUCCESS",
                actor_type="SUPERADMIN",
                actor_username=configured_user,
                actor_role="SUPERADMIN",
            )
            flash("Super Admin login successful.", "success")
            target = request.args.get("next", "").strip()
            if target.startswith("/") and not target.startswith("//"):
                return redirect(target)
            return redirect(url_for("admin_accounts"))

        now_blocked, now_seconds = register_login_failure("SUPERADMIN", attempt_key)
        write_audit_log(
            action="SUPERADMIN_LOGIN_FAILED",
            actor_type="SUPERADMIN_LOGIN",
            actor_username=attempt_key,
            actor_role="LOGIN",
            metadata={"otp_required": bool(SUPERADMIN_OTP)},
        )
        if now_blocked:
            minutes_left = max(1, (now_seconds + 59) // 60)
            flash(f"Login blocked for {minutes_left} minute(s).", "error")
            return redirect(url_for(login_endpoint))
        if SUPERADMIN_OTP and not otp_ok:
            flash("Super Admin OTP ভুল.", "error")
            return redirect(url_for(login_endpoint))
        flash("Super Admin credential ভুল.", "error")
        return redirect(url_for(login_endpoint))

    return render_template(
        "admin_login.html",
        superadmin_user=SUPERADMIN_USER,
        otp_required=bool(SUPERADMIN_OTP),
    )


@app.get("/admin/logout")
def admin_logout():
    write_audit_log(
        action="SUPERADMIN_LOGOUT",
        actor_type="SUPERADMIN",
        actor_username=SUPERADMIN_USER,
        actor_role="SUPERADMIN",
    )
    session.pop("is_superadmin", None)
    flash("Super Admin logged out.", "success")
    return redirect(url_for("superadmin_login"))


@app.get("/admin/pocket/runtime/status")
def admin_pocket_runtime_status():
    admin_db = get_admin_db()
    tenant_count = 0
    try:
        tenant_count = int(admin_db.execute("SELECT COUNT(*) FROM tenant_accounts").fetchone()[0] or 0)
    except sqlite3.Error:
        tenant_count = 0

    tenant_files = [path for path in TENANT_DATA_DIR.glob("*.db") if path.is_file()] if TENANT_DATA_DIR.exists() else []
    return jsonify(
        ok=True,
        mainDb=str(DB_PATH),
        adminDb=str(ADMIN_DB_PATH),
        tenantDir=str(TENANT_DATA_DIR),
        tenantAccounts=tenant_count,
        tenantFiles=len(tenant_files),
    )


@app.post("/admin/pocket/runtime/import")
def admin_pocket_runtime_import():
    upload = request.files.get("runtime_package")
    if upload is None or not upload.filename:
        return jsonify(ok=False, message="runtime_package zip file is required."), 400

    filename = secure_filename(upload.filename)
    if not filename.lower().endswith(".zip"):
        return jsonify(ok=False, message="Only Pocket Pro runtime .zip packages are allowed."), 400

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    upload_path = BACKUP_DIR / f"uploaded-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{filename}"
    upload.save(upload_path)

    try:
        safety_backup, result = restore_pocket_runtime_export_package(upload_path)
    except Exception as exc:
        return jsonify(ok=False, message=f"Pocket Pro runtime import failed: {exc}"), 400

    write_audit_log(
        action="POCKET_RUNTIME_IMPORT",
        actor_type="SUPERADMIN",
        actor_username=SUPERADMIN_USER,
        actor_role="SUPERADMIN",
        metadata={
            "upload": str(upload_path),
            "safety_backup": str(safety_backup) if safety_backup is not None else "",
            "tenant_file_count": result.get("tenant_file_count", 0),
        },
    )
    return jsonify(ok=True, message="Pocket Pro runtime import completed.", result=result)


@app.route("/admin/accounts", methods=["GET", "POST"])
def admin_accounts():
    admin_db = get_admin_db()

    if request.method == "POST":
        shop_name = request.form.get("shop_name", "").strip()
        owner_name = request.form.get("owner_name", "").strip()
        phone = request.form.get("phone", "").strip()
        username = normalize_username(request.form.get("username", ""))
        password = request.form.get("password", "").strip()
        ui_language = normalize_language(request.form.get("ui_language", "")) or DEFAULT_UI_LANGUAGE
        selected_primary_business = normalize_business_module(
            request.form.get("primary_business", DEFAULT_PRIMARY_BUSINESS),
            default=DEFAULT_PRIMARY_BUSINESS,
        )
        selected_modules = parse_enabled_modules(
            request.form.getlist("enabled_modules"),
            fallback_primary=selected_primary_business,
        )
        if selected_primary_business not in selected_modules:
            selected_modules.insert(0, selected_primary_business)
        enabled_modules = ",".join(selected_modules)
        seed_from_main = request.form.get("seed_from_main", "0") == "1"
        billing_cycle = "MONTHLY"
        billing_note = request.form.get("billing_note", "").strip()
        monthly_fee_raw = request.form.get("monthly_fee", "").strip()
        if selected_primary_business == "POCKET_MONEY" and not monthly_fee_raw:
            monthly_fee_raw = "99"
        initial_months_raw = request.form.get("initial_months", "1").strip()

        if not shop_name:
            flash("Shop name is required.", "error")
            return redirect(url_for("admin_accounts"))
        if len(username) < 4:
            flash("Username must be at least 4 characters.", "error")
            return redirect(url_for("admin_accounts"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("admin_accounts"))

        try:
            monthly_fee = parse_money(monthly_fee_raw or "0", "Monthly Fee")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_accounts"))

        try:
            initial_months = int(initial_months_raw or "1")
        except ValueError:
            initial_months = 1
        if initial_months < 1:
            initial_months = 1
        if initial_months > 24:
            initial_months = 24

        billing_start = date.today()
        paid_until_date = calculate_coverage_end(billing_start, initial_months)
        paid_until = paid_until_date.isoformat()
        initial_amount = round(monthly_fee * initial_months, 2)

        db_path = tenant_db_path_for_username(username)

        existing = admin_db.execute(
            "SELECT id FROM tenant_accounts WHERE username = ?",
            (username,),
        ).fetchone()
        if existing is not None:
            flash("This username already exists.", "error")
            return redirect(url_for("admin_accounts"))

        if db_path.exists():
            flash("Tenant DB already exists for this username. Use another username.", "error")
            return redirect(url_for("admin_accounts"))

        try:
            if seed_from_main and DB_PATH.exists():
                with sqlite3.connect(DB_PATH) as source, sqlite3.connect(db_path) as target:
                    source.backup(target)
            else:
                init_db_for_path(db_path)

            # Every tenant gets a default panel admin user for role-based login.
            create_or_update_tenant_user(
                db_path=db_path,
                username="admin",
                full_name=f"{shop_name} Admin",
                role="ADMIN",
                password=password,
                is_active=True,
            )

            admin_db.execute(
                """
                INSERT INTO tenant_accounts (
                    shop_name, owner_name, phone, username, password_hash, db_path,
                    ui_language, primary_business, enabled_modules,
                    billing_cycle, monthly_fee, paid_until, billing_note, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    shop_name,
                    owner_name,
                    phone,
                    username,
                    make_password_hash(password),
                    str(db_path),
                    ui_language,
                    selected_primary_business,
                    enabled_modules,
                    billing_cycle,
                    monthly_fee,
                    paid_until,
                    billing_note,
                ),
            )
            tenant_id = int(admin_db.execute("SELECT last_insert_rowid()").fetchone()[0])
            admin_db.execute(
                """
                INSERT INTO billing_transactions (
                    tenant_id, paid_on, period_months, amount, period_start, period_end, note, source, tx_ref, gateway
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    billing_start.isoformat(),
                    initial_months,
                    initial_amount,
                    billing_start.isoformat(),
                    paid_until,
                    billing_note or "Initial subscription",
                    "INITIAL",
                    "",
                    "",
                ),
            )
            admin_db.commit()
            write_audit_log(
                action="TENANT_ACCOUNT_CREATED",
                tenant_id=tenant_id,
                actor_type="SUPERADMIN",
                actor_username=SUPERADMIN_USER,
                actor_role="SUPERADMIN",
                metadata={
                    "shop_name": shop_name,
                    "username": username,
                    "monthly_fee": monthly_fee,
                    "initial_months": initial_months,
                },
            )
            flash(
                f"Account created for {shop_name}. Billing active until {paid_until}.",
                "success",
            )
        except sqlite3.IntegrityError:
            admin_db.rollback()
            flash("Account create failed due to duplicate entry.", "error")
        except Exception as exc:
            admin_db.rollback()
            flash(f"Account create failed: {exc}", "error")

        return redirect(url_for("admin_accounts"))

    today = date.today()
    today_iso = today.isoformat()
    month_prefix = today_iso[:7]
    year_prefix = today_iso[:4]
    soon_date = (today + timedelta(days=5)).isoformat()
    automation_summary = run_subscription_automation(send_notifications=True)

    def admin_scalar(sql: str, params: tuple = ()) -> float:
        row = admin_db.execute(sql, params).fetchone()
        if row is None or row[0] is None:
            return 0
        return float(row[0])

    metrics = {
        "total_accounts": int(admin_scalar("SELECT COUNT(*) FROM tenant_accounts")),
        "active_accounts": int(admin_scalar("SELECT COUNT(*) FROM tenant_accounts WHERE is_active = 1")),
        "inactive_accounts": int(admin_scalar("SELECT COUNT(*) FROM tenant_accounts WHERE is_active = 0")),
        "monthly_target": float(
            admin_scalar("SELECT COALESCE(SUM(monthly_fee), 0) FROM tenant_accounts WHERE is_active = 1")
        ),
        "monthly_collection": float(
            admin_scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM billing_transactions WHERE substr(paid_on, 1, 7) = ?",
                (month_prefix,),
            )
        ),
        "yearly_collection": float(
            admin_scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM billing_transactions WHERE substr(paid_on, 1, 4) = ?",
                (year_prefix,),
            )
        ),
        "lifetime_collection": float(admin_scalar("SELECT COALESCE(SUM(amount), 0) FROM billing_transactions")),
        "monthly_payers": int(
            admin_scalar(
                "SELECT COUNT(DISTINCT tenant_id) FROM billing_transactions WHERE substr(paid_on, 1, 7) = ?",
                (month_prefix,),
            )
        ),
        "yearly_payers": int(
            admin_scalar(
                "SELECT COUNT(DISTINCT tenant_id) FROM billing_transactions WHERE substr(paid_on, 1, 4) = ?",
                (year_prefix,),
            )
        ),
        "monthly_transactions": int(
            admin_scalar(
                "SELECT COUNT(*) FROM billing_transactions WHERE substr(paid_on, 1, 7) = ?",
                (month_prefix,),
            )
        ),
        "yearly_transactions": int(
            admin_scalar(
                "SELECT COUNT(*) FROM billing_transactions WHERE substr(paid_on, 1, 4) = ?",
                (year_prefix,),
            )
        ),
        "overdue_accounts": int(
            admin_scalar(
                """
                SELECT COUNT(*)
                FROM tenant_accounts
                WHERE is_active = 1
                  AND paid_until IS NOT NULL
                  AND TRIM(paid_until) <> ''
                  AND paid_until < ?
                """,
                (today_iso,),
            )
        ),
        "due_soon_accounts": int(
            admin_scalar(
                """
                SELECT COUNT(*)
                FROM tenant_accounts
                WHERE is_active = 1
                  AND paid_until IS NOT NULL
                  AND TRIM(paid_until) <> ''
                  AND paid_until >= ?
                  AND paid_until <= ?
                """,
                (today_iso, soon_date),
            )
        ),
        "overdue_estimated": float(
            admin_scalar(
                """
                SELECT COALESCE(SUM(monthly_fee), 0)
                FROM tenant_accounts
                WHERE is_active = 1
                  AND paid_until IS NOT NULL
                  AND TRIM(paid_until) <> ''
                  AND paid_until < ?
                """,
                (today_iso,),
            )
        ),
    }
    metrics["monthly_gap"] = max(0.0, float(metrics["monthly_target"]) - float(metrics["monthly_collection"]))

    period = request.args.get("period", "MONTH").strip().upper()
    if period == "YEAR":
        period_sql = "substr(bt.paid_on, 1, 4) = ?"
        period_params: tuple[str, ...] = (year_prefix,)
        period_label = "This Year"
    elif period == "LIFETIME":
        period = "LIFETIME"
        period_sql = "1 = 1"
        period_params = ()
        period_label = "Lifetime"
    else:
        period = "MONTH"
        period_sql = "substr(bt.paid_on, 1, 7) = ?"
        period_params = (month_prefix,)
        period_label = "This Month"

    recent_collections = admin_db.execute(
        """
        SELECT
            bt.id, bt.tenant_id, bt.paid_on, bt.period_months, bt.amount,
            bt.period_start, bt.period_end, bt.note, bt.source, bt.tx_ref, bt.gateway,
            a.shop_name, a.username
        FROM billing_transactions bt
        JOIN tenant_accounts a ON a.id = bt.tenant_id
        ORDER BY bt.id DESC
        LIMIT 40
        """
    ).fetchall()

    payers = admin_db.execute(
        f"""
        SELECT
            a.id AS tenant_id,
            a.shop_name,
            a.username,
            COUNT(*) AS payment_count,
            COALESCE(SUM(bt.amount), 0) AS total_amount,
            MAX(bt.paid_on) AS last_paid_on
        FROM billing_transactions bt
        JOIN tenant_accounts a ON a.id = bt.tenant_id
        WHERE {period_sql}
        GROUP BY a.id
        ORDER BY total_amount DESC, payment_count DESC, a.shop_name
        LIMIT 25
        """,
        period_params,
    ).fetchall()

    accounts_raw = admin_db.execute(
        """
        SELECT
            a.*,
            (
                SELECT bt.paid_on
                FROM billing_transactions bt
                WHERE bt.tenant_id = a.id
                ORDER BY bt.id DESC
                LIMIT 1
            ) AS last_paid_on,
            (
                SELECT bt.amount
                FROM billing_transactions bt
                WHERE bt.tenant_id = a.id
                ORDER BY bt.id DESC
                LIMIT 1
            ) AS last_paid_amount,
            (
                SELECT COUNT(*)
                FROM billing_transactions bt
                WHERE bt.tenant_id = a.id
            ) AS payment_count,
            (
                SELECT COALESCE(SUM(bt.amount), 0)
                FROM billing_transactions bt
                WHERE bt.tenant_id = a.id
            ) AS total_paid
        FROM tenant_accounts a
        ORDER BY a.id DESC
        """
    ).fetchall()

    accounts: list[dict[str, object]] = []
    for row in accounts_raw:
        entry = dict(row)
        status, days_left = billing_status_for_paid_until(entry.get("paid_until"))
        entry["billing_status"] = status
        entry["days_left"] = days_left
        profile = build_business_profile(entry)
        entry["business_profile"] = profile
        entry["primary_business_label_en"] = profile["primary_label_en"]
        entry["primary_business_label_bn"] = profile["primary_label_bn"]
        entry["enabled_modules_label_en"] = ", ".join(profile["enabled_labels_en"])
        entry["enabled_modules_label_bn"] = ", ".join(profile["enabled_labels_bn"])
        accounts.append(entry)

    due_accounts = [
        item for item in accounts if int(item.get("is_active", 0)) == 1 and item["billing_status"] in {"EXPIRED", "DUE_SOON"}
    ][:20]

    reminders = admin_db.execute(
        """
        SELECT
            r.id, r.tenant_id, r.reminder_date, r.reminder_type,
            r.message, r.status, r.sent_at, r.webhook_response,
            a.shop_name, a.username
        FROM billing_reminders r
        JOIN tenant_accounts a ON a.id = r.tenant_id
        ORDER BY r.id DESC
        LIMIT 40
        """
    ).fetchall()

    recent_audit = admin_db.execute(
        """
        SELECT
            l.id, l.action, l.actor_type, l.actor_username,
            l.endpoint, l.ip_address, l.created_at,
            a.shop_name
        FROM audit_logs l
        LEFT JOIN tenant_accounts a ON a.id = l.tenant_id
        ORDER BY l.id DESC
        LIMIT 40
        """
    ).fetchall()

    return render_template(
        "admin_accounts.html",
        accounts=accounts,
        due_accounts=due_accounts,
        recent_collections=recent_collections,
        payers=payers,
        metrics=metrics,
        period=period,
        period_label=period_label,
        superadmin_user=SUPERADMIN_USER,
        business_modules=get_business_module_options(),
        plan_presets=PLAN_LIMIT_PRESETS,
        reminders=reminders,
        recent_audit=recent_audit,
        automation_summary=automation_summary,
        billing_webhook_secret_set=bool(BILLING_WEBHOOK_SECRET),
        billing_webhook_url=url_for("billing_webhook_collect", _external=True),
        notify_webhook_url=BILLING_NOTIFY_WEBHOOK,
    )


@app.post("/admin/accounts/<int:account_id>/plan")
def admin_update_tenant_plan(account_id: int):
    admin_db = get_admin_db()
    account = admin_db.execute(
        """
        SELECT id, username, shop_name, plan_code, max_branches, max_users, max_products, max_monthly_orders
        FROM tenant_accounts
        WHERE id = ?
        """,
        (account_id,),
    ).fetchone()
    if account is None:
        if request.is_json:
            return jsonify({"ok": False, "message": "Tenant not found."}), 404
        flash("Tenant account not found.", "error")
        return redirect(url_for("admin_accounts"))

    source = request.get_json(silent=True) if request.is_json else request.form
    requested_plan = str(source.get("plan_code", "")).strip().upper()
    if requested_plan not in PLAN_LIMIT_PRESETS:
        requested_plan = "GROWTH"
    preset = PLAN_LIMIT_PRESETS[requested_plan]

    max_branches = parse_int_with_default(source.get("max_branches", preset["max_branches"]), preset["max_branches"])
    max_users = parse_int_with_default(source.get("max_users", preset["max_users"]), preset["max_users"])
    max_products = parse_int_with_default(source.get("max_products", preset["max_products"]), preset["max_products"])
    max_monthly_orders = parse_int_with_default(
        source.get("max_monthly_orders", preset["max_monthly_orders"]),
        preset["max_monthly_orders"],
    )
    note = str(source.get("note", "")).strip()

    admin_db.execute(
        """
        UPDATE tenant_accounts
        SET plan_code = ?,
            max_branches = ?,
            max_users = ?,
            max_products = ?,
            max_monthly_orders = ?
        WHERE id = ?
        """,
        (requested_plan, max_branches, max_users, max_products, max_monthly_orders, account_id),
    )
    admin_db.execute(
        """
        INSERT INTO tenant_plan_events (
            tenant_id, old_plan_code, new_plan_code, note, actor_username, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            str(account["plan_code"] or ""),
            requested_plan,
            note or f"Plan updated to {requested_plan}",
            SUPERADMIN_USER,
            now_sqlite_text(),
        ),
    )
    admin_db.execute(
        """
        INSERT INTO security_events (
            tenant_id, severity, event_type, event_source, actor_username, metadata, created_at
        )
        VALUES (?, 'LOW', 'TENANT_PLAN_UPDATED', 'SUPERADMIN_PANEL', ?, ?, ?)
        """,
        (
            account_id,
            SUPERADMIN_USER,
            json.dumps(
                {
                    "old_plan": str(account["plan_code"] or ""),
                    "new_plan": requested_plan,
                    "max_branches": max_branches,
                    "max_users": max_users,
                    "max_products": max_products,
                    "max_monthly_orders": max_monthly_orders,
                },
                ensure_ascii=False,
            ),
            now_sqlite_text(),
        ),
    )
    admin_db.commit()

    write_audit_log(
        action="TENANT_PLAN_UPDATED",
        tenant_id=account_id,
        actor_type="SUPERADMIN",
        actor_username=SUPERADMIN_USER,
        actor_role="SUPERADMIN",
        metadata={
            "plan_code": requested_plan,
            "max_branches": max_branches,
            "max_users": max_users,
            "max_products": max_products,
            "max_monthly_orders": max_monthly_orders,
        },
    )

    result_payload = {
        "ok": True,
        "tenant_id": account_id,
        "shop_name": str(account["shop_name"] or ""),
        "username": str(account["username"] or ""),
        "plan_code": requested_plan,
        "max_branches": max_branches,
        "max_users": max_users,
        "max_products": max_products,
        "max_monthly_orders": max_monthly_orders,
    }
    if request.is_json:
        return jsonify(result_payload)

    flash(
        f"Plan updated for {account['shop_name']} ({account['username']}): {requested_plan}",
        "success",
    )
    return redirect(url_for("admin_accounts"))


@app.post("/admin/accounts/<int:account_id>/modules")
def admin_update_tenant_modules(account_id: int):
    admin_db = get_admin_db()
    account = admin_db.execute(
        """
        SELECT id, shop_name, username, primary_business, enabled_modules, module_overrides_json
        FROM tenant_accounts
        WHERE id = ?
        """,
        (account_id,),
    ).fetchone()
    if account is None:
        if request.is_json:
            return jsonify({"ok": False, "message": "Tenant not found."}), 404
        flash("Tenant account not found.", "error")
        return redirect(url_for("admin_accounts"))

    source = request.get_json(silent=True) if request.is_json else request.form
    primary_business = normalize_business_module(
        str(source.get("primary_business", account["primary_business"] or DEFAULT_PRIMARY_BUSINESS)),
        default=DEFAULT_PRIMARY_BUSINESS,
    )
    enabled_raw: object
    if hasattr(source, "getlist"):
        enabled_list = source.getlist("enabled_modules")
        if len(enabled_list) == 1 and re.search(r"[,;\s]", str(enabled_list[0] or "")):
            enabled_raw = str(enabled_list[0] or "")
        else:
            enabled_raw = enabled_list
    else:
        enabled_raw = source.get("enabled_modules", "")
    enabled_modules = parse_enabled_modules(
        enabled_raw,
        fallback_primary=primary_business,
    )
    if primary_business not in enabled_modules:
        enabled_modules.insert(0, primary_business)
    enabled_modules_csv = ",".join(enabled_modules)

    raw_overrides = str(source.get("module_overrides_json", account["module_overrides_json"] or "")).strip()
    clean_overrides = ""
    if raw_overrides:
        try:
            parsed = json.loads(raw_overrides)
            if not isinstance(parsed, dict):
                raise ValueError("module_overrides_json must be a JSON object.")
            clean_overrides = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            message = "module_overrides_json must be valid JSON object."
            if request.is_json:
                return jsonify({"ok": False, "message": message}), 400
            flash(message, "error")
            return redirect(url_for("admin_accounts"))

    admin_db.execute(
        """
        UPDATE tenant_accounts
        SET primary_business = ?,
            enabled_modules = ?,
            module_overrides_json = ?
        WHERE id = ?
        """,
        (primary_business, enabled_modules_csv, clean_overrides, account_id),
    )
    admin_db.commit()
    write_audit_log(
        action="TENANT_MODULES_UPDATED",
        tenant_id=account_id,
        actor_type="SUPERADMIN",
        actor_username=SUPERADMIN_USER,
        actor_role="SUPERADMIN",
        metadata={
            "primary_business": primary_business,
            "enabled_modules": enabled_modules_csv,
        },
    )

    payload = {
        "ok": True,
        "tenant_id": account_id,
        "primary_business": primary_business,
        "enabled_modules": enabled_modules,
    }
    if request.is_json:
        return jsonify(payload)
    flash(
        f"Modules updated for {account['shop_name']} ({account['username']}).",
        "success",
    )
    return redirect(url_for("admin_accounts"))


@app.post("/admin/automation/run")
def admin_run_automation():
    summary = run_subscription_automation(send_notifications=True)
    write_audit_log(
        action="ADMIN_AUTOMATION_RUN",
        actor_type="SUPERADMIN",
        actor_username=SUPERADMIN_USER,
        actor_role="SUPERADMIN",
        metadata=summary,
    )
    flash(
        (
            f"Automation completed. Checked {summary['accounts_checked']} account(s), "
            f"created {summary['reminders_created']} reminder(s), "
            f"sent {summary['notifications_sent']}, failed {summary['notifications_failed']}."
        ),
        "success",
    )
    return redirect(url_for("admin_accounts"))


@app.post("/api/billing/webhook")
def billing_webhook_collect():
    payload = request.get_json(silent=True) or {}
    secret = str(payload.get("secret", "")).strip()
    if not BILLING_WEBHOOK_SECRET:
        return jsonify({"ok": False, "message": "Webhook secret is not configured."}), 503
    if secret != BILLING_WEBHOOK_SECRET:
        write_audit_log(
            action="BILLING_WEBHOOK_DENIED",
            actor_type="SYSTEM_WEBHOOK",
            actor_username="unknown",
            actor_role="WEBHOOK",
            metadata={"reason": "invalid_secret"},
        )
        return jsonify({"ok": False, "message": "Invalid secret."}), 403

    shop_username = normalize_username(str(payload.get("shop_username", "")))
    if not shop_username:
        return jsonify({"ok": False, "message": "shop_username is required."}), 400

    try:
        months = int(str(payload.get("months", "1")))
    except ValueError:
        months = 1
    months = max(1, min(24, months))

    amount_value = payload.get("amount", None)
    amount: float | None = None
    if amount_value not in (None, ""):
        try:
            amount = parse_money(str(amount_value), "amount")
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    note = str(payload.get("note", "")).strip() or "Gateway collection webhook"
    tx_ref = str(payload.get("tx_ref", "")).strip()
    gateway = str(payload.get("gateway", "CUSTOM")).strip().upper() or "CUSTOM"

    admin_db = get_admin_db()
    account = admin_db.execute(
        """
        SELECT id, shop_name, username, monthly_fee, paid_until, is_active
        FROM tenant_accounts
        WHERE username = ?
        """,
        (shop_username,),
    ).fetchone()
    if account is None:
        return jsonify({"ok": False, "message": "Shop account not found."}), 404
    if int(account["is_active"]) != 1:
        return jsonify({"ok": False, "message": "Shop account is inactive."}), 403

    result = collect_subscription_payment(
        admin_db=admin_db,
        account=account,
        months=months,
        amount=amount,
        note=note,
        source="WEBHOOK",
        tx_ref=tx_ref,
        gateway=gateway,
        paid_on=date.today(),
    )
    admin_db.commit()

    write_audit_log(
        action="BILLING_WEBHOOK_COLLECT",
        tenant_id=int(account["id"]),
        actor_type="SYSTEM_WEBHOOK",
        actor_username=gateway,
        actor_role="WEBHOOK",
        metadata={
            "shop_username": shop_username,
            "months": result["months"],
            "amount": result["amount"],
            "tx_ref": tx_ref,
        },
    )

    return jsonify(
        {
            "ok": True,
            "shop_username": shop_username,
            "shop_name": account["shop_name"],
            "new_paid_until": result["new_paid_until"],
            "period_start": result["period_start"],
            "period_end": result["period_end"],
            "amount": result["amount"],
            "months": result["months"],
        }
    )


@app.get("/admin/audit")
def admin_audit():
    admin_db = get_admin_db()
    action_filter = request.args.get("action", "").strip().upper()
    tenant_filter = normalize_username(request.args.get("shop", ""))

    where_parts: list[str] = []
    params: list[str] = []
    if action_filter:
        where_parts.append("l.action = ?")
        params.append(action_filter)
    if tenant_filter:
        where_parts.append("a.username = ?")
        params.append(tenant_filter)

    sql = """
        SELECT
            l.id, l.tenant_id, l.actor_type, l.actor_username, l.actor_role,
            l.action, l.endpoint, l.ip_address, l.user_agent, l.metadata, l.created_at,
            a.shop_name, a.username AS shop_username
        FROM audit_logs l
        LEFT JOIN tenant_accounts a ON a.id = l.tenant_id
    """
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    sql += " ORDER BY l.id DESC LIMIT 400"

    logs = admin_db.execute(sql, tuple(params)).fetchall()
    return render_template("admin_audit.html", logs=logs, action_filter=action_filter, tenant_filter=tenant_filter)


@app.post("/admin/accounts/<int:account_id>/toggle")
def admin_toggle_account(account_id: int):
    action = request.form.get("action", "deactivate").strip().lower()
    new_status = 1 if action == "activate" else 0
    admin_db = get_admin_db()

    account = admin_db.execute(
        "SELECT id, shop_name FROM tenant_accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if account is None:
        flash("Account not found.", "error")
        return redirect(url_for("admin_accounts"))

    admin_db.execute(
        "UPDATE tenant_accounts SET is_active = ? WHERE id = ?",
        (new_status, account_id),
    )
    admin_db.commit()
    write_audit_log(
        action="TENANT_ACCOUNT_STATUS_CHANGED",
        tenant_id=int(account["id"]),
        actor_type="SUPERADMIN",
        actor_username=SUPERADMIN_USER,
        actor_role="SUPERADMIN",
        metadata={"new_status": new_status},
    )
    flash(
        f"Account {'activated' if new_status == 1 else 'deactivated'}: {account['shop_name']}",
        "success",
    )
    return redirect(url_for("admin_accounts"))


@app.post("/admin/accounts/<int:account_id>/password")
def admin_reset_account_password(account_id: int):
    new_password = request.form.get("new_password", "").strip()
    if len(new_password) < 6:
        flash("New password must be at least 6 characters.", "error")
        return redirect(url_for("admin_accounts"))

    admin_db = get_admin_db()
    account = admin_db.execute(
        "SELECT id, shop_name FROM tenant_accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if account is None:
        flash("Account not found.", "error")
        return redirect(url_for("admin_accounts"))

    admin_db.execute(
        "UPDATE tenant_accounts SET password_hash = ? WHERE id = ?",
        (make_password_hash(new_password), account_id),
    )
    admin_db.commit()
    write_audit_log(
        action="TENANT_ACCOUNT_PASSWORD_RESET",
        tenant_id=int(account["id"]),
        actor_type="SUPERADMIN",
        actor_username=SUPERADMIN_USER,
        actor_role="SUPERADMIN",
    )
    flash(f"Password reset complete for {account['shop_name']}.", "success")
    return redirect(url_for("admin_accounts"))


@app.post("/admin/accounts/<int:account_id>/collect")
def admin_collect_bill(account_id: int):
    admin_db = get_admin_db()
    account = admin_db.execute(
        """
        SELECT id, shop_name, monthly_fee, paid_until, is_active
        FROM tenant_accounts
        WHERE id = ?
        """,
        (account_id,),
    ).fetchone()
    if account is None:
        flash("Account not found.", "error")
        return redirect(url_for("admin_accounts"))

    months_raw = request.form.get("months", "1").strip()
    amount_raw = request.form.get("amount", "").strip()
    note = request.form.get("note", "").strip()

    try:
        months = int(months_raw or "1")
    except ValueError:
        months = 1
    if months < 1:
        months = 1
    if months > 24:
        months = 24

    default_fee = float(account["monthly_fee"] or 0)
    if amount_raw:
        try:
            amount = parse_money(amount_raw, "Bill Amount")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_accounts"))
    else:
        amount = round(default_fee * months, 2)

    result = collect_subscription_payment(
        admin_db=admin_db,
        account=account,
        months=months,
        amount=amount,
        note=note or "Monthly service bill collection",
        source="MANUAL",
        tx_ref="",
        gateway="",
        paid_on=date.today(),
    )
    admin_db.commit()
    write_audit_log(
        action="TENANT_BILL_COLLECTED",
        tenant_id=int(account["id"]),
        actor_type="SUPERADMIN",
        actor_username=SUPERADMIN_USER,
        actor_role="SUPERADMIN",
        metadata={"months": result["months"], "amount": result["amount"]},
    )
    flash(
        f"Bill collected for {account['shop_name']}. Service active until {result['new_paid_until']}.",
        "success",
    )
    return redirect(url_for("admin_accounts"))


@app.route("/shop-settings", methods=["GET", "POST"])
def shop_settings():
    tenant = get_current_tenant()
    current_user = get_current_tenant_user()
    if tenant is None or current_user is None:
        return redirect(url_for("client_login"))

    if normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only tenant admin can update shop settings.", "error")
        return redirect(url_for("dashboard"))

    admin_db = get_admin_db()
    db = get_db()
    account = admin_db.execute(
        """
        SELECT
            id, shop_name, owner_name, phone, username, db_path,
            ui_language, primary_business, enabled_modules,
            billing_cycle, monthly_fee, paid_until, billing_note, profile_image_path, is_active, created_at
        FROM tenant_accounts
        WHERE id = ?
        """,
        (int(tenant["id"]),),
    ).fetchone()
    if account is None:
        flash("Shop account not found.", "error")
        return redirect(url_for("dashboard"))

    branches = db.execute(
        """
        SELECT id, name
        FROM branches
        ORDER BY is_default DESC, id ASC
        """
    ).fetchall()
    default_cash_branch_id = int(branches[0]["id"]) if branches else 1
    default_cash_clear_date = date.today().isoformat()

    if request.method == "POST":
        action = (request.form.get("action", "save_settings") or "save_settings").strip().lower()
        if action == "clear_cash_snapshot":
            cash_clear_date = normalize_date(request.form.get("cash_clear_date", "").strip() or default_cash_clear_date)
            cash_clear_branch_id = parse_optional_int(request.form.get("cash_clear_branch_id", "")) or default_cash_branch_id
            cash_clear_note = normalize_text_field(request.form.get("cash_clear_note", "")) or "Cash cleared from settings."
            branch_row = db.execute("SELECT id, name FROM branches WHERE id = ?", (cash_clear_branch_id,)).fetchone()
            if branch_row is None:
                flash("Invalid branch selected for cash clear.", "error")
                return redirect(url_for("shop_settings"))

            existing_cash = db.execute(
                """
                SELECT id
                FROM petty_cash_daily
                WHERE cash_date = ? AND branch_id = ?
                LIMIT 1
                """,
                (cash_clear_date, cash_clear_branch_id),
            ).fetchone()
            if existing_cash is None:
                db.execute(
                    """
                    INSERT INTO petty_cash_daily (
                        cash_date, branch_id, opening_cash, closing_cash, note,
                        created_by_user_id, created_by_username, created_at, updated_at
                    )
                    VALUES (?, ?, 0, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        cash_clear_date,
                        cash_clear_branch_id,
                        cash_clear_note,
                        int(current_user["id"]),
                        str(current_user["username"]),
                        now_sqlite_text(),
                        now_sqlite_text(),
                    ),
                )
            else:
                db.execute(
                    """
                    UPDATE petty_cash_daily
                    SET opening_cash = 0,
                        closing_cash = 0,
                        note = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        cash_clear_note,
                        now_sqlite_text(),
                        int(existing_cash["id"]),
                    ),
                )
            db.commit()
            write_audit_log(
                action="TENANT_PETTY_CASH_CLEARED",
                tenant_id=int(account["id"]),
                actor_type="TENANT_USER",
                actor_username=str(current_user["username"]),
                actor_role=str(current_user["role"]),
                metadata={
                    "cash_date": cash_clear_date,
                    "branch_id": cash_clear_branch_id,
                    "note": cash_clear_note,
                },
            )
            flash("Cash snapshot cleared safely.", "success")
            return redirect(url_for("shop_settings"))

        shop_name = request.form.get("shop_name", "").strip()
        owner_name = request.form.get("owner_name", "").strip()
        phone = request.form.get("phone", "").strip()
        remove_profile_image = request.form.get("remove_profile_image", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        profile_image_file = request.files.get("profile_image")
        ui_language = normalize_language(request.form.get("ui_language", "")) or DEFAULT_UI_LANGUAGE
        selected_primary_business = normalize_business_module(
            request.form.get("primary_business", DEFAULT_PRIMARY_BUSINESS),
            default=DEFAULT_PRIMARY_BUSINESS,
        )
        selected_modules = parse_enabled_modules(
            request.form.getlist("enabled_modules"),
            fallback_primary=selected_primary_business,
        )
        if selected_primary_business not in selected_modules:
            selected_modules.insert(0, selected_primary_business)
        enabled_modules = ",".join(selected_modules)

        if not shop_name:
            flash("Shop name is required.", "error")
            return redirect(url_for("shop_settings"))

        existing_profile_image = str(account["profile_image_path"] or "").strip()
        profile_image_path = existing_profile_image
        if remove_profile_image and existing_profile_image:
            delete_profile_image_file(existing_profile_image)
            profile_image_path = ""

        if profile_image_file is not None and (profile_image_file.filename or "").strip():
            try:
                saved_image = save_profile_image(profile_image_file, prefix="tenant-profile")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("shop_settings"))
            if saved_image:
                profile_image_path = saved_image
                if existing_profile_image and existing_profile_image != saved_image:
                    delete_profile_image_file(existing_profile_image)

        admin_db.execute(
            """
            UPDATE tenant_accounts
            SET shop_name = ?,
                owner_name = ?,
                phone = ?,
                ui_language = ?,
                primary_business = ?,
                enabled_modules = ?,
                profile_image_path = ?
            WHERE id = ?
            """,
            (
                shop_name,
                owner_name,
                phone,
                ui_language,
                selected_primary_business,
                enabled_modules,
                profile_image_path,
                int(account["id"]),
            ),
        )
        admin_db.commit()
        session["ui_lang"] = ui_language
        g.pop("current_tenant", None)

        write_audit_log(
            action="TENANT_SHOP_SETTINGS_UPDATED",
            tenant_id=int(account["id"]),
            actor_type="TENANT_USER",
            actor_username=str(current_user["username"]),
            actor_role=str(current_user["role"]),
            metadata={
                "ui_language": ui_language,
                "primary_business": selected_primary_business,
                "enabled_modules": enabled_modules,
                "profile_image_updated": int(profile_image_path != existing_profile_image),
            },
        )
        flash("Shop settings updated successfully.", "success")
        return redirect(url_for("shop_settings"))

    business_profile = build_business_profile(account)
    return render_template(
        "shop_settings.html",
        account=account,
        business_profile=business_profile,
        business_modules=get_business_module_options(),
        branches=branches,
        default_cash_branch_id=default_cash_branch_id,
        default_cash_clear_date=default_cash_clear_date,
    )


@app.route("/account-password", methods=["GET", "POST"])
def account_password():
    tenant = get_current_tenant()
    current_user = get_current_tenant_user()
    if tenant is None or current_user is None:
        return redirect(url_for("client_login"))

    db = get_db()
    ensure_tenant_users_table(db)
    user_row = db.execute(
        """
        SELECT id, username, role, password_hash
        FROM users
        WHERE id = ?
        """,
        (int(current_user["id"]),),
    ).fetchone()
    if user_row is None:
        session.pop("tenant_user_id", None)
        g.pop("current_tenant_user", None)
        flash("User session expired. Please login again.", "error")
        return redirect(url_for("login_selector"))

    if request.method == "POST":
        action = (request.form.get("action", "change_password") or "change_password").strip().lower()
        if action == "update_profile_image":
            if normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
                flash("Only admin can update profile image.", "error")
                return redirect(url_for("account_password"))

            profile_image_file = request.files.get("profile_image")
            remove_profile_image = request.form.get("remove_profile_image", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            admin_db = get_admin_db()
            account_row = admin_db.execute(
                "SELECT id, profile_image_path FROM tenant_accounts WHERE id = ?",
                (int(tenant["id"]),),
            ).fetchone()
            if account_row is None:
                flash("Account not found.", "error")
                return redirect(url_for("account_password"))

            existing_profile_image = str(account_row["profile_image_path"] or "").strip()
            profile_image_path = existing_profile_image

            if remove_profile_image and existing_profile_image:
                delete_profile_image_file(existing_profile_image)
                profile_image_path = ""

            if profile_image_file is not None and (profile_image_file.filename or "").strip():
                try:
                    saved_image = save_profile_image(profile_image_file, prefix="tenant-profile")
                except ValueError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("account_password"))
                if saved_image:
                    profile_image_path = saved_image
                    if existing_profile_image and existing_profile_image != saved_image:
                        delete_profile_image_file(existing_profile_image)

            if profile_image_path == existing_profile_image:
                flash("No profile image change detected.", "error")
                return redirect(url_for("account_password"))

            admin_db.execute(
                "UPDATE tenant_accounts SET profile_image_path = ? WHERE id = ?",
                (profile_image_path, int(account_row["id"])),
            )
            admin_db.commit()
            g.pop("current_tenant", None)
            write_audit_log(
                action="TENANT_PROFILE_IMAGE_UPDATED",
                tenant_id=int(tenant["id"]),
                actor_type="TENANT_USER",
                actor_username=str(current_user["username"]),
                actor_role=str(current_user["role"]),
                metadata={"updated": 1},
            )
            flash("Profile image updated.", "success")
            return redirect(url_for("account_password"))

        current_password = request.form.get("current_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not current_password or not new_password or not confirm_password:
            flash("All password fields are required.", "error")
            return redirect(url_for("account_password"))
        if len(new_password) < 6:
            flash("New password must be at least 6 characters.", "error")
            return redirect(url_for("account_password"))
        if new_password != confirm_password:
            flash("New password and confirm password do not match.", "error")
            return redirect(url_for("account_password"))
        if not password_matches(str(user_row["password_hash"]), current_password):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("account_password"))
        if password_matches(str(user_row["password_hash"]), new_password):
            flash("New password must be different from current password.", "error")
            return redirect(url_for("account_password"))

        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (make_password_hash(new_password), int(user_row["id"])),
        )
        db.commit()
        write_audit_log(
            action="TENANT_USER_PASSWORD_CHANGED",
            tenant_id=int(tenant["id"]),
            actor_type="TENANT_USER",
            actor_username=str(current_user["username"]),
            actor_role=str(current_user["role"]),
            metadata={"username": str(user_row["username"])},
        )
        flash("Password changed successfully.", "success")
        return redirect(url_for("account_password"))

    return render_template("account_password.html")


@app.get("/account/password")
def account_password_alias():
    return redirect(url_for("account_password"))


@app.route("/team-users", methods=["GET", "POST"])
def team_users():
    current_user = get_current_tenant_user()
    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only tenant admin can manage team users.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    ensure_tenant_users_table(db)

    if request.method == "POST":
        username = normalize_username(request.form.get("username", ""))
        full_name = request.form.get("full_name", "").strip()
        role = normalize_role(request.form.get("role", "USER"), default="USER")
        password = request.form.get("password", "").strip()
        tenant = get_current_tenant()

        blocked_by_limit, limit_message, _limit_info = check_tenant_plan_limit(
            db,
            tenant,
            "max_users",
            incoming_count=1,
        )
        if blocked_by_limit:
            flash(limit_message, "error")
            return redirect(url_for("team_users"))

        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return redirect(url_for("team_users"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("team_users"))

        existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing is not None:
            flash("This user id already exists.", "error")
            return redirect(url_for("team_users"))

        db.execute(
            """
            INSERT INTO users (username, full_name, role, password_hash, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (username, full_name, role, make_password_hash(password)),
        )
        db.commit()
        if tenant is not None:
            write_audit_log(
                action="TENANT_TEAM_USER_CREATED",
                tenant_id=int(tenant["id"]),
                actor_type="TENANT_USER",
                actor_username=str(current_user["username"]),
                actor_role=str(current_user["role"]),
                metadata={"username": username, "role": role},
            )
        flash(f"User created: {username} ({role})", "success")
        return redirect(url_for("team_users"))

    users = db.execute(
        """
        SELECT id, username, full_name, role, is_active, created_at
        FROM users
        ORDER BY
            CASE role
                WHEN 'ADMIN' THEN 1
                WHEN 'USER' THEN 2
                ELSE 3
            END,
            id DESC
        """
    ).fetchall()
    return render_template("team_users.html", users=users)


@app.post("/team-users/<int:user_id>/toggle")
def team_toggle_user(user_id: int):
    current_user = get_current_tenant_user()
    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only tenant admin can update users.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    ensure_tenant_users_table(db)
    target = db.execute("SELECT id, username, role, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        flash("User not found.", "error")
        return redirect(url_for("team_users"))

    action = request.form.get("action", "deactivate").strip().lower()
    new_status = 1 if action == "activate" else 0
    if int(target["id"]) == int(current_user["id"]) and new_status == 0:
        flash("নিজের admin account deactivate করা যাবে না।", "error")
        return redirect(url_for("team_users"))

    db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, user_id))
    db.commit()
    tenant = get_current_tenant()
    if tenant is not None:
        write_audit_log(
            action="TENANT_TEAM_USER_STATUS_CHANGED",
            tenant_id=int(tenant["id"]),
            actor_type="TENANT_USER",
            actor_username=str(current_user["username"]),
            actor_role=str(current_user["role"]),
            metadata={"username": str(target["username"]), "is_active": new_status},
        )
    flash(
        f"User {'activated' if new_status == 1 else 'deactivated'}: {target['username']}",
        "success",
    )
    return redirect(url_for("team_users"))


@app.post("/team-users/<int:user_id>/password")
def team_reset_password(user_id: int):
    current_user = get_current_tenant_user()
    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only tenant admin can reset password.", "error")
        return redirect(url_for("dashboard"))

    new_password = request.form.get("new_password", "").strip()
    if len(new_password) < 6:
        flash("New password must be at least 6 characters.", "error")
        return redirect(url_for("team_users"))

    db = get_db()
    ensure_tenant_users_table(db)
    target = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        flash("User not found.", "error")
        return redirect(url_for("team_users"))

    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (make_password_hash(new_password), user_id),
    )
    db.commit()
    tenant = get_current_tenant()
    if tenant is not None:
        write_audit_log(
            action="TENANT_TEAM_USER_PASSWORD_RESET",
            tenant_id=int(tenant["id"]),
            actor_type="TENANT_USER",
            actor_username=str(current_user["username"]),
            actor_role=str(current_user["role"]),
            metadata={"username": str(target["username"])},
        )
    flash(f"Password reset done for {target['username']}.", "success")
    return redirect(url_for("team_users"))


@app.post("/team-users/<int:user_id>/role")
def team_change_role(user_id: int):
    current_user = get_current_tenant_user()
    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only tenant admin can change role.", "error")
        return redirect(url_for("dashboard"))

    new_role = normalize_role(request.form.get("role", "USER"), default="USER")

    db = get_db()
    ensure_tenant_users_table(db)
    target = db.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        flash("User not found.", "error")
        return redirect(url_for("team_users"))

    if int(target["id"]) == int(current_user["id"]) and new_role != "ADMIN":
        flash("নিজের role ADMIN থেকে কমানো যাবে না।", "error")
        return redirect(url_for("team_users"))

    db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    db.commit()
    tenant = get_current_tenant()
    if tenant is not None:
        write_audit_log(
            action="TENANT_TEAM_USER_ROLE_CHANGED",
            tenant_id=int(tenant["id"]),
            actor_type="TENANT_USER",
            actor_username=str(current_user["username"]),
            actor_role=str(current_user["role"]),
            metadata={"username": str(target["username"]), "new_role": new_role},
        )
    flash(f"Role updated: {target['username']} -> {new_role}", "success")
    return redirect(url_for("team_users"))


@app.route("/")
def dashboard():
    db = get_db()
    today = date.today().isoformat()
    month_prefix = today[:7]
    year_prefix = today[:4]
    tenant = get_current_tenant()
    if tenant is not None and get_tenant_default_endpoint(tenant) == "money_center":
        return redirect(url_for("money_center"))
    tenant_id = int(tenant["id"]) if tenant is not None else 0

    state_row = db.execute(
        """
        SELECT
            (SELECT COALESCE(MAX(id), 0) FROM products) AS products_max_id,
            (SELECT COALESCE(MAX(id), 0) FROM sales) AS sales_max_id,
            (SELECT COALESCE(MAX(id), 0) FROM sale_returns) AS returns_max_id,
            (SELECT COALESCE(MAX(id), 0) FROM expenses) AS expenses_max_id,
            (SELECT COALESCE(MAX(id), 0) FROM due_collections) AS due_collections_max_id,
            (SELECT COALESCE(MAX(id), 0) FROM backup_logs) AS backup_max_id
        """
    ).fetchone()
    state_signature = (
        f"{int(state_row['products_max_id'] or 0)}-"
        f"{int(state_row['sales_max_id'] or 0)}-"
        f"{int(state_row['returns_max_id'] or 0)}-"
        f"{int(state_row['expenses_max_id'] or 0)}-"
        f"{int(state_row['due_collections_max_id'] or 0)}-"
        f"{int(state_row['backup_max_id'] or 0)}"
    )
    key_source = f"softx:dashboard:v3:t{tenant_id}:{today}:{state_signature}"
    cache_key = f"softx:dashboard:{hashlib.sha1(key_source.encode('utf-8')).hexdigest()}"
    cached_payload = cache_get_json(cache_key)
    if cached_payload:
        return render_template(
            "dashboard.html",
            metrics=dict(cached_payload.get("metrics", {})),
            ops_stats=dict(cached_payload.get("ops_stats", {})),
            backup_info=dict(cached_payload.get("backup_info", {})),
            profit_cards=dict(cached_payload.get("profit_cards", {})),
            finance_summary=dict(cached_payload.get("finance_summary", {})),
            home_cards=dict(cached_payload.get("home_cards", {})),
            brand_profit=list(cached_payload.get("brand_profit", [])),
            category_profit=list(cached_payload.get("category_profit", [])),
            recent_sales=list(cached_payload.get("recent_sales", [])),
            recent_returns=list(cached_payload.get("recent_returns", [])),
            due_customer_focus=list(cached_payload.get("due_customer_focus", [])),
            low_stock_models=list(cached_payload.get("low_stock_models", [])),
            today=today,
        )

    profit_today = get_profit_summary(db, "s.sold_at = ?", (today,))
    profit_month = get_profit_summary(db, "substr(s.sold_at, 1, 7) = ?", (month_prefix,))
    profit_year = get_profit_summary(db, "substr(s.sold_at, 1, 4) = ?", (year_prefix,))
    profit_lifetime = get_profit_summary(db, "1 = 1")

    def get_return_summary(condition_sql: str, params: tuple = ()) -> sqlite3.Row:
        return db.execute(
            f"""
            SELECT
                COUNT(*) AS units,
                COALESCE(SUM(s.sold_price), 0) AS return_value,
                COALESCE(SUM(s.sold_price - p.purchase_price), 0) AS return_profit_impact
            FROM sale_returns r
            JOIN sales s ON s.id = r.sale_id
            JOIN products p ON p.id = s.product_id
            WHERE ({condition_sql})
            """,
            params,
        ).fetchone()

    return_today = get_return_summary("r.return_date = ?", (today,))
    return_month = get_return_summary("substr(r.return_date, 1, 7) = ?", (month_prefix,))
    return_year = get_return_summary("substr(r.return_date, 1, 4) = ?", (year_prefix,))
    return_lifetime = get_return_summary("1 = 1")

    expense_today = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE approval_status = 'APPROVED' AND expense_date = ?
            """,
            (today,),
        )
    )
    expense_month = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE approval_status = 'APPROVED' AND substr(expense_date, 1, 7) = ?
            """,
            (month_prefix,),
        )
    )
    expense_year = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE approval_status = 'APPROVED' AND substr(expense_date, 1, 4) = ?
            """,
            (year_prefix,),
        )
    )
    expense_lifetime = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE approval_status = 'APPROVED'
            """
        )
    )
    expense_today_live = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE expense_date = ?
              AND COALESCE(approval_status, 'PENDING') <> 'REJECTED'
            """,
            (today,),
        )
    )
    expense_today_live_count = int(
        query_scalar(
            """
            SELECT COUNT(*)
            FROM expenses
            WHERE expense_date = ?
              AND COALESCE(approval_status, 'PENDING') <> 'REJECTED'
            """,
            (today,),
        )
    )
    today_purchase_total = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(purchase_price), 0)
            FROM products
            WHERE received_date = ?
            """,
            (today,),
        )
    )
    today_purchase_units = int(
        query_scalar(
            """
            SELECT COUNT(*)
            FROM products
            WHERE received_date = ?
            """,
            (today,),
        )
    )
    today_due_total = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(due_amount), 0)
            FROM sales
            WHERE is_active = 1
              AND sold_at = ?
            """,
            (today,),
        )
    )
    today_due_count = int(
        query_scalar(
            """
            SELECT COUNT(*)
            FROM sales
            WHERE is_active = 1
              AND sold_at = ?
              AND COALESCE(due_amount, 0) > 0
            """,
            (today,),
        )
    )
    today_return_profit_pressure = float(return_today["return_profit_impact"] or 0)
    today_net_profit_live = float(profit_today["profit"] or 0) - expense_today_live - today_return_profit_pressure

    finance_summary = {
        "expense_today": expense_today,
        "expense_month": expense_month,
        "expense_year": expense_year,
        "expense_lifetime": expense_lifetime,
        "expense_today_live": expense_today_live,
        "expense_today_live_count": expense_today_live_count,
        "net_profit_today": float(profit_today["profit"] or 0) - expense_today,
        "net_profit_today_live": today_net_profit_live,
        "net_profit_month": float(profit_month["profit"] or 0) - expense_month,
        "net_profit_year": float(profit_year["profit"] or 0) - expense_year,
        "net_profit_lifetime": float(profit_lifetime["profit"] or 0) - expense_lifetime,
        "return_value_today": float(return_today["return_value"] or 0),
        "return_value_month": float(return_month["return_value"] or 0),
        "return_value_year": float(return_year["return_value"] or 0),
        "return_value_lifetime": float(return_lifetime["return_value"] or 0),
        "return_profit_pressure_today": today_return_profit_pressure,
        "return_units_today": int(return_today["units"] or 0),
        "return_units_month": int(return_month["units"] or 0),
        "return_units_year": int(return_year["units"] or 0),
        "return_units_lifetime": int(return_lifetime["units"] or 0),
    }

    metrics = {
        "total_products": int(query_scalar("SELECT COUNT(*) FROM products")),
        "in_stock": int(query_scalar("SELECT COUNT(*) FROM products WHERE status = 'IN_STOCK'")),
        "sold_count": int(query_scalar("SELECT COUNT(*) FROM products WHERE status = 'SOLD'")),
        "total_wholesale_shops": int(
            query_scalar(
                "SELECT COUNT(*) FROM customers WHERE shop_name <> ?",
                (LOCAL_RETAIL_WHOLESALE_SHOP_NAME,),
            )
        ),
        "total_retail_customers": int(query_scalar("SELECT COUNT(*) FROM retail_customers")),
        "total_suppliers": int(query_scalar("SELECT COUNT(*) FROM suppliers")),
        "due_amount": float(
            query_scalar("SELECT COALESCE(SUM(due_amount), 0) FROM sales WHERE is_active = 1")
        ),
    }

    ops_stats = {
        "today_sales_count": int(
            query_scalar(
                "SELECT COUNT(*) FROM sales WHERE sold_at = ? AND is_active = 1",
                (today,),
            )
        ),
        "today_returns_count": int(
            query_scalar("SELECT COUNT(*) FROM sale_returns WHERE return_date = ?", (today,))
        ),
        "due_sales_count": int(
            query_scalar("SELECT COUNT(*) FROM sales WHERE is_active = 1 AND due_amount > 0")
        ),
        "due_customers_count": int(
            query_scalar(
                """
                SELECT COUNT(DISTINCT customer_id)
                FROM sales
                WHERE is_active = 1
                  AND due_amount > 0
                  AND sale_type = 'WHOLESALE'
                """
            )
        ),
        "today_collected_due": float(
            query_scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM due_collections WHERE collected_at = ?",
                (today,),
            )
        ),
        "today_retail_invoice_count": int(
            query_scalar("SELECT COUNT(*) FROM retail_invoices WHERE sold_at = ?", (today,))
        ),
        "today_purchase_units": today_purchase_units,
        "today_due_count": today_due_count,
        "today_expense_live_count": expense_today_live_count,
    }

    backup_info = db.execute(
        """
        SELECT filename, created_at, google_status
        FROM backup_logs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    profit_cards = {
        "today": profit_today,
        "month": profit_month,
        "year": profit_year,
        "lifetime": profit_lifetime,
    }

    home_cards = {
        "today_sales_total": float(profit_today["revenue"] or 0),
        "today_sales_units": int(profit_today["units"] or 0),
        "today_purchase_total": today_purchase_total,
        "today_purchase_units": today_purchase_units,
        "today_due_total": today_due_total,
        "today_due_count": today_due_count,
        "today_net_profit": today_net_profit_live,
        "today_expense_total": expense_today_live,
        "today_expense_count": expense_today_live_count,
        "today_return_pressure": today_return_profit_pressure,
    }

    brand_profit = db.execute(
        """
        SELECT
            p.brand AS label,
            COUNT(*) AS units,
            COALESCE(SUM(s.sold_price), 0) AS revenue,
            COALESCE(SUM(s.sold_price - p.purchase_price), 0) AS profit
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.is_active = 1
        GROUP BY p.brand
        ORDER BY profit DESC
        LIMIT 10
        """
    ).fetchall()

    category_profit = db.execute(
        """
        SELECT
            COALESCE(NULLIF(TRIM(p.category), ''), 'Uncategorized') AS label,
            COUNT(*) AS units,
            COALESCE(SUM(s.sold_price), 0) AS revenue,
            COALESCE(SUM(s.sold_price - p.purchase_price), 0) AS profit
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.is_active = 1
        GROUP BY COALESCE(NULLIF(TRIM(p.category), ''), 'Uncategorized')
        ORDER BY profit DESC
        LIMIT 10
        """
    ).fetchall()

    recent_sales = db.execute(
        """
        SELECT
            s.id, s.sold_at, s.sale_type, s.sold_price, s.payment_status, s.is_active,
            s.paid_amount, s.due_amount,
            p.imei, p.brand, p.model, p.purchase_price,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE COALESCE(c.shop_name, 'Wholesale Shop')
            END AS shop_name,
            (s.sold_price - p.purchase_price) AS profit
        FROM sales s
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        ORDER BY s.id DESC
        LIMIT 10
        """
    ).fetchall()

    recent_returns = db.execute(
        """
        SELECT
            r.return_date,
            r.reason,
            p.imei,
            p.brand,
            p.model,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS shop_name
        FROM sale_returns r
        JOIN sales s ON s.id = r.sale_id
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        ORDER BY r.id DESC
        LIMIT 10
        """
    ).fetchall()

    due_customer_focus = db.execute(
        """
        SELECT
            c.id AS customer_id,
            c.shop_name,
            COUNT(*) AS due_items,
            COALESCE(SUM(s.due_amount), 0) AS due_amount
        FROM sales s
        JOIN customers c ON c.id = s.customer_id
        WHERE s.is_active = 1
          AND s.due_amount > 0
          AND s.sale_type = 'WHOLESALE'
          AND c.shop_name <> ?
        GROUP BY c.id, c.shop_name
        ORDER BY due_amount DESC
        LIMIT 8
        """,
        (LOCAL_RETAIL_WHOLESALE_SHOP_NAME,),
    ).fetchall()

    low_stock_models = db.execute(
        """
        SELECT
            p.brand,
            p.model,
            COALESCE(NULLIF(TRIM(p.storage), ''), '-') AS storage,
            COUNT(*) AS units
        FROM products p
        WHERE p.status = 'IN_STOCK'
        GROUP BY p.brand, p.model, COALESCE(NULLIF(TRIM(p.storage), ''), '-')
        HAVING COUNT(*) <= 3
        ORDER BY units ASC, p.brand ASC, p.model ASC
        LIMIT 8
        """
    ).fetchall()

    metrics_dict = dict(metrics)
    ops_stats_dict = dict(ops_stats)
    backup_info_dict = row_as_dict(backup_info)
    profit_cards_dict = {
        "today": row_as_dict(profit_today),
        "month": row_as_dict(profit_month),
        "year": row_as_dict(profit_year),
        "lifetime": row_as_dict(profit_lifetime),
    }
    finance_summary_dict = dict(finance_summary)
    home_cards_dict = dict(home_cards)
    brand_profit_list = [row_as_dict(row) for row in brand_profit]
    category_profit_list = [row_as_dict(row) for row in category_profit]
    recent_sales_list = [row_as_dict(row) for row in recent_sales]
    recent_returns_list = [row_as_dict(row) for row in recent_returns]
    due_customer_focus_list = [row_as_dict(row) for row in due_customer_focus]
    low_stock_models_list = [row_as_dict(row) for row in low_stock_models]

    cache_set_json(
        cache_key,
        {
            "metrics": metrics_dict,
            "ops_stats": ops_stats_dict,
            "backup_info": backup_info_dict,
            "profit_cards": profit_cards_dict,
            "finance_summary": finance_summary_dict,
            "home_cards": home_cards_dict,
            "brand_profit": brand_profit_list,
            "category_profit": category_profit_list,
            "recent_sales": recent_sales_list,
            "recent_returns": recent_returns_list,
            "due_customer_focus": due_customer_focus_list,
            "low_stock_models": low_stock_models_list,
        },
    )

    return render_template(
        "dashboard.html",
        metrics=metrics_dict,
        ops_stats=ops_stats_dict,
        backup_info=backup_info_dict,
        profit_cards=profit_cards_dict,
        finance_summary=finance_summary_dict,
        home_cards=home_cards_dict,
        brand_profit=brand_profit_list,
        category_profit=category_profit_list,
        recent_sales=recent_sales_list,
        recent_returns=recent_returns_list,
        due_customer_focus=due_customer_focus_list,
        low_stock_models=low_stock_models_list,
        today=today,
    )


@app.get("/dashboard")
def dashboard_alias():
    return redirect(url_for("dashboard"))


@app.get("/profile")
def profile_center():
    tenant = get_current_tenant()
    current_user = get_current_tenant_user()
    if tenant is None or current_user is None:
        return redirect(url_for("client_login"))

    admin_db = get_admin_db()
    account = admin_db.execute(
        """
        SELECT
            id, shop_name, owner_name, phone, username, db_path,
            ui_language, primary_business, enabled_modules,
            billing_cycle, monthly_fee, paid_until, billing_note,
            profile_image_path, is_active, created_at
        FROM tenant_accounts
        WHERE id = ?
        """,
        (int(tenant["id"]),),
    ).fetchone()
    if account is None:
        flash("Shop account not found.", "error")
        return redirect(url_for("dashboard"))

    business_profile = build_business_profile(account)
    return render_template(
        "profile_center.html",
        account=account,
        current_user=current_user,
        business_profile=business_profile,
    )


@app.route("/products", methods=["GET", "POST"])
def products():
    db = get_db()
    ensure_model_catalog_table(db)

    def redirect_products_view(**extra: object):
        params: dict[str, object] = {}
        current_pview = normalize_text_field(request.values.get("pview", "")).lower()
        if current_pview in {"single", "bulk", "fast", "inventory"}:
            params["pview"] = current_pview

        current_q = normalize_text_field(request.values.get("q", ""))
        if current_q:
            params["q"] = current_q

        current_status = normalize_text_field(request.values.get("status", "")).upper()
        if current_status in {"ALL", "IN_STOCK", "SOLD"}:
            params["status"] = current_status

        for key, value in extra.items():
            if value is not None:
                params[key] = value
        return redirect(url_for("products", **params))

    if request.method == "POST":
        entry_mode = request.form.get("entry_mode", "single").strip().lower()
        tracking_mode = get_current_tracking_mode()
        received_date = normalize_date(request.form.get("received_date", "").strip())
        branch_id = parse_optional_int(request.form.get("branch_id", "1")) or 1
        imeis: list[str] = []

        brand = ""
        model = ""
        category = ""
        color = ""
        storage = ""
        note = ""
        warranty_type = ""
        supplier_id: int | None = None
        purchase_price = 0.0
        wholesale_price = 0.0
        retail_price = 0.0
        catalog_profile_row: sqlite3.Row | None = None

        if entry_mode == "clone_existing":
            source_product_id = parse_optional_int(request.form.get("source_product_id", ""))
            note = normalize_text_field(request.form.get("note", ""))
            if source_product_id is None:
                flash("Please select an existing product profile.", "error")
                return redirect_products_view()

            source_product = db.execute(
                """
                SELECT
                    id, brand, model, category, color, storage,
                    COALESCE(warranty_type, '') AS warranty_type,
                    purchase_price, wholesale_price, retail_price,
                    supplier_id, note
                FROM products
                WHERE id = ?
                """,
                (source_product_id,),
            ).fetchone()
            if source_product is None:
                flash("Selected product profile not found.", "error")
                return redirect_products_view()

            brand = normalize_text_field(str(source_product["brand"] or ""))
            model = normalize_text_field(str(source_product["model"] or ""))
            category = normalize_text_field(str(source_product["category"] or ""))
            color = normalize_text_field(str(source_product["color"] or ""))
            storage = normalize_text_field(str(source_product["storage"] or ""))
            warranty_type = normalize_warranty_type(str(source_product["warranty_type"] or ""))
            purchase_price = float(source_product["purchase_price"] or 0)
            wholesale_price = float(source_product["wholesale_price"] or 0)
            retail_price = float(source_product["retail_price"] or 0)
            supplier_id = (
                int(source_product["supplier_id"])
                if source_product["supplier_id"] is not None
                else None
            )
            if not note:
                note = normalize_text_field(str(source_product["note"] or ""))

            for raw in request.form.getlist("imei_rows_clone[]"):
                normalized_code = normalize_tracking_code(raw or "", tracking_mode)
                if normalized_code:
                    imeis.append(normalized_code)
            imeis.extend(normalize_imei_text(request.form.get("bulk_imeis_clone", "")))
        else:
            brand = normalize_text_field(request.form.get("brand", ""))
            model = normalize_text_field(request.form.get("model", ""))
            category = normalize_text_field(request.form.get("category", ""))
            color = normalize_text_field(request.form.get("color", ""))
            storage = normalize_text_field(request.form.get("storage", ""))
            warranty_type = normalize_warranty_type(request.form.get("warranty_type", ""))
            note = normalize_text_field(request.form.get("note", ""))
            supplier_id = parse_optional_int(request.form.get("supplier_id", ""))
            catalog_profile_id = parse_optional_int(request.form.get("catalog_profile_id", ""))

            if catalog_profile_id is not None:
                catalog_profile_row = db.execute(
                    """
                    SELECT
                        id, brand, model_name, model_number, storage, region, color,
                        condition_state, category, purchase_price, wholesale_price, retail_price,
                        supplier_id, extra_info
                    FROM model_catalog
                    WHERE id = ? AND is_active = 1
                    """,
                    (catalog_profile_id,),
                ).fetchone()
                if catalog_profile_row is None:
                    flash("Selected model profile is not available.", "error")
                    return redirect_products_view()

                if not brand:
                    brand = normalize_text_field(str(catalog_profile_row["brand"] or ""))
                if not model:
                    model = normalize_text_field(str(catalog_profile_row["model_name"] or ""))
                if not category:
                    category = normalize_text_field(str(catalog_profile_row["category"] or ""))
                if not color:
                    color = normalize_text_field(str(catalog_profile_row["color"] or ""))
                if not storage:
                    storage = normalize_text_field(str(catalog_profile_row["storage"] or ""))
                if supplier_id is None and catalog_profile_row["supplier_id"] is not None:
                    supplier_id = int(catalog_profile_row["supplier_id"])

            if not brand or not model:
                flash("Brand and Model are required.", "error")
                return redirect_products_view()

            if supplier_id is not None:
                supplier = db.execute("SELECT id FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
                if supplier is None:
                    flash("Selected supplier not found.", "error")
                    return redirect_products_view()

            def parse_price_with_profile(
                raw_value: str,
                label: str,
                catalog_default: object | None,
            ) -> float:
                cleaned = (raw_value or "").strip()
                if cleaned:
                    return parse_money(cleaned, label)
                if catalog_default is not None:
                    candidate = float(catalog_default or 0)
                    if candidate > 0:
                        return candidate
                raise ValueError(f"{label} is required.")

            try:
                purchase_price = parse_price_with_profile(
                    request.form.get("purchase_price", ""),
                    "Purchase Price",
                    catalog_profile_row["purchase_price"] if catalog_profile_row is not None else None,
                )
                wholesale_price = parse_price_with_profile(
                    request.form.get("wholesale_price", ""),
                    "Wholesale Price",
                    catalog_profile_row["wholesale_price"] if catalog_profile_row is not None else None,
                )
                retail_price = parse_price_with_profile(
                    request.form.get("retail_price", ""),
                    "Retail Price",
                    catalog_profile_row["retail_price"] if catalog_profile_row is not None else None,
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect_products_view()

            if not note and catalog_profile_row is not None:
                note = build_model_catalog_note(
                    model_number=str(catalog_profile_row["model_number"] or "").strip(),
                    region=str(catalog_profile_row["region"] or "").strip(),
                    condition_state=normalize_model_condition(
                        str(catalog_profile_row["condition_state"] or "NEW")
                    ),
                    extra_info=str(catalog_profile_row["extra_info"] or "").strip(),
                )

            if entry_mode == "bulk":
                for raw in request.form.getlist("imei_rows[]"):
                    normalized_code = normalize_tracking_code(raw or "", tracking_mode)
                    if normalized_code:
                        imeis.append(normalized_code)
                imeis.extend(normalize_imei_text(request.form.get("bulk_imeis", "")))
            else:
                imei = normalize_tracking_code(request.form.get("imei", "").strip(), tracking_mode)
                if imei:
                    imeis.append(imei)

        iphone_storage = ("APPLE" in brand.upper()) or ("IPHONE" in model.upper())
        normalized_storage = infer_storage_variant(storage, iphone_only_storage=iphone_storage)
        if normalized_storage:
            storage = normalized_storage

        if not imeis:
            flash(f"At least one {get_tracking_label()} is required.", "error")
            return redirect_products_view()

        branch = db.execute("SELECT id FROM branches WHERE id = ?", (branch_id,)).fetchone()
        if branch is None:
            flash("Invalid branch selected.", "error")
            return redirect_products_view()

        unique_imeis: list[str] = []
        seen: set[str] = set()
        for item in imeis:
            if item not in seen:
                seen.add(item)
                unique_imeis.append(item)

        invalid_imeis = [item for item in unique_imeis if not is_valid_imei(item)]
        valid_imeis = [item for item in unique_imeis if is_valid_imei(item)]

        tenant = get_current_tenant()
        blocked_by_limit, limit_message, _limit_info = check_tenant_plan_limit(
            db,
            tenant,
            "max_products",
            incoming_count=len(valid_imeis),
        )
        if blocked_by_limit:
            flash(limit_message, "error")
            return redirect_products_view()

        inserted = 0
        duplicate_count = 0
        for imei in valid_imeis:
            try:
                db.execute(
                    """
                    INSERT INTO products (
                        imei, brand, model, category, color, storage,
                        warranty_type,
                        purchase_price, wholesale_price, retail_price,
                        supplier_id, branch_id, received_date, status, note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'IN_STOCK', ?)
                    """,
                    (
                        imei,
                        brand,
                        model,
                        category,
                        color,
                        storage,
                        warranty_type,
                        purchase_price,
                        wholesale_price,
                        retail_price,
                        supplier_id,
                        branch_id,
                        received_date,
                        note,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                duplicate_count += 1

        db.commit()

        messages: list[str] = []
        if inserted:
            messages.append(f"Inserted {inserted} item(s).")
        if duplicate_count:
            messages.append(f"Skipped {duplicate_count} duplicate {get_tracking_label()}.")
        if invalid_imeis:
            messages.append(f"Invalid {get_tracking_label()}: {len(invalid_imeis)}")

        if inserted:
            flash(" ".join(messages), "success")
        else:
            flash("No item inserted. " + " ".join(messages), "error")

        return redirect_products_view()

    q = request.args.get("q", "").strip()
    status = request.args.get("status", "ALL").strip().upper()

    where_parts: list[str] = []
    params: list[str] = []

    if q:
        wildcard = f"%{q}%"
        where_parts.append(
            """
            (
                p.imei LIKE ?
                OR p.brand LIKE ?
                OR p.model LIKE ?
                OR COALESCE(p.category, '') LIKE ?
                OR COALESCE(c.shop_name, '') LIKE ?
                OR COALESCE(rc.full_name, '') LIKE ?
            )
            """
        )
        params.extend([wildcard, wildcard, wildcard, wildcard, wildcard, wildcard])

    if status in {"IN_STOCK", "SOLD"}:
        where_parts.append("p.status = ?")
        params.append(status)
    else:
        status = "ALL"

    sql = """
        SELECT
            p.*,
            sup.name AS supplier_name,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS customer_shop,
            s.sold_at,
            s.sold_price,
            (s.sold_price - p.purchase_price) AS profit_per_phone
        FROM products p
        LEFT JOIN suppliers sup ON sup.id = p.supplier_id
        LEFT JOIN sales s ON s.product_id = p.id AND s.is_active = 1
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
    """
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    sql += " ORDER BY p.id DESC"

    products_list = db.execute(sql, tuple(params)).fetchall()
    suppliers = db.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()
    product_profiles = db.execute(
        """
        SELECT
            p.id,
            p.brand,
            p.model,
            COALESCE(p.storage, '') AS storage,
            COALESCE(p.color, '') AS color,
            COALESCE(p.category, '') AS category,
            p.purchase_price,
            p.wholesale_price,
            p.retail_price,
            COALESCE(p.warranty_type, '') AS warranty_type,
            p.supplier_id,
            s.name AS supplier_name
        FROM products p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        INNER JOIN (
            SELECT MAX(id) AS id
            FROM products
            GROUP BY
                UPPER(TRIM(brand)),
                UPPER(TRIM(model)),
                UPPER(TRIM(COALESCE(storage, ''))),
                UPPER(TRIM(COALESCE(color, ''))),
                UPPER(TRIM(COALESCE(warranty_type, '')))
        ) latest ON latest.id = p.id
        ORDER BY p.id DESC
        LIMIT 500
        """
    ).fetchall()
    model_catalog_profiles = db.execute(
        """
        SELECT
            mc.id,
            mc.brand,
            mc.model_name,
            COALESCE(mc.model_number, '') AS model_number,
            COALESCE(mc.storage, '') AS storage,
            COALESCE(mc.category, '') AS category,
            COALESCE(mc.color, '') AS color,
            COALESCE(mc.region, '') AS region,
            COALESCE(mc.condition_state, 'NEW') AS condition_state,
            mc.purchase_price,
            mc.wholesale_price,
            mc.retail_price,
            mc.supplier_id,
            COALESCE(mc.extra_info, '') AS extra_info
        FROM model_catalog mc
        WHERE mc.is_active = 1
        ORDER BY
            UPPER(TRIM(mc.brand)) ASC,
            UPPER(TRIM(mc.model_name)) ASC,
            UPPER(TRIM(COALESCE(mc.storage, ''))) ASC,
            mc.id DESC
        LIMIT 2000
        """
    ).fetchall()

    edit_product: sqlite3.Row | None = None
    edit_product_has_sales = False
    edit_product_can_delete = False
    edit_product_id = parse_optional_int(request.args.get("edit_id", ""))
    if edit_product_id is not None:
        edit_product = db.execute(
            """
            SELECT
                p.*,
                sup.name AS supplier_name,
                EXISTS(
                    SELECT 1
                    FROM sales s
                    WHERE s.product_id = p.id
                    LIMIT 1
                ) AS has_sale_history,
                EXISTS(
                    SELECT 1
                    FROM stock_adjustments sa
                    WHERE sa.product_id = p.id
                    LIMIT 1
                ) AS has_stock_adjustment
            FROM products p
            LEFT JOIN suppliers sup ON sup.id = p.supplier_id
            WHERE p.id = ?
            """,
            (edit_product_id,),
        ).fetchone()
        if edit_product is None:
            flash("Selected product not found.", "error")
        else:
            edit_product_has_sales = int(edit_product["has_sale_history"] or 0) == 1
            edit_product_can_delete = (
                str(edit_product["status"] or "").upper() != "SOLD"
                and not edit_product_has_sales
                and int(edit_product["has_stock_adjustment"] or 0) == 0
            )

    return render_template(
        "products.html",
        products=products_list,
        suppliers=suppliers,
        product_profiles=product_profiles,
        model_catalog_profiles=model_catalog_profiles,
        edit_product=edit_product,
        edit_product_has_sales=edit_product_has_sales,
        edit_product_can_delete=edit_product_can_delete,
        q=q,
        status=status,
        today=date.today().isoformat(),
    )


@app.post("/products/<int:product_id>/update")
def product_update(product_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))

    db = get_db()
    tracking_mode = get_current_tracking_mode()
    product = db.execute(
        """
        SELECT id, imei, status
        FROM products
        WHERE id = ?
        """,
        (product_id,),
    ).fetchone()
    if product is None:
        flash("Product not found.", "error")
        return redirect(safe_next_path("products"))

    imei = normalize_tracking_code(request.form.get("imei", "").strip(), tracking_mode)
    brand = normalize_text_field(request.form.get("brand", ""))
    model = normalize_text_field(request.form.get("model", ""))
    category = normalize_text_field(request.form.get("category", ""))
    color = normalize_text_field(request.form.get("color", ""))
    storage_input = request.form.get("storage", "").strip()
    warranty_type = normalize_warranty_type(request.form.get("warranty_type", ""))
    note = normalize_text_field(request.form.get("note", ""))
    supplier_id = parse_optional_int(request.form.get("supplier_id", ""))
    received_date = normalize_date(request.form.get("received_date", "").strip())

    if not imei or not is_valid_imei(imei):
        flash(f"Valid {get_tracking_label()} is required.", "error")
        return redirect(safe_next_path("products"))
    if not brand or not model:
        flash("Brand and model are required.", "error")
        return redirect(safe_next_path("products"))

    iphone_storage = ("APPLE" in brand.upper()) or ("IPHONE" in model.upper())
    storage = infer_storage_variant(storage_input, iphone_only_storage=iphone_storage) or normalize_text_field(storage_input)

    if supplier_id is not None:
        supplier_row = db.execute("SELECT id FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        if supplier_row is None:
            flash("Selected supplier not found.", "error")
            return redirect(safe_next_path("products"))

    duplicate_code = db.execute(
        "SELECT id FROM products WHERE imei = ? AND id <> ?",
        (imei, product_id),
    ).fetchone()
    if duplicate_code is not None:
        flash(f"This {get_tracking_label()} already exists in another product.", "error")
        return redirect(safe_next_path("products"))

    try:
        purchase_price = parse_money(request.form.get("purchase_price", ""), "Purchase Price")
        wholesale_price = parse_money(request.form.get("wholesale_price", ""), "Wholesale Price")
        retail_price = parse_money(request.form.get("retail_price", ""), "Retail Price")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(safe_next_path("products"))

    db.execute(
        """
        UPDATE products
        SET imei = ?,
            brand = ?,
            model = ?,
            category = ?,
            color = ?,
            storage = ?,
            warranty_type = ?,
            purchase_price = ?,
            wholesale_price = ?,
            retail_price = ?,
            supplier_id = ?,
            received_date = ?,
            note = ?
        WHERE id = ?
        """,
        (
            imei,
            brand,
            model,
            category,
            color,
            storage,
            warranty_type,
            purchase_price,
            wholesale_price,
            retail_price,
            supplier_id,
            received_date,
            note,
            product_id,
        ),
    )
    db.commit()
    write_audit_log(
        action="PRODUCT_UPDATED",
        metadata={
            "product_id": product_id,
            "status": str(product["status"] or ""),
            "tracking_code": imei,
            "brand": brand,
            "model": model,
            "warranty_type": warranty_type,
        },
    )
    flash("Product information updated.", "success")
    return redirect(safe_next_path("products"))


@app.route("/products/<int:product_id>/delete", methods=["GET", "POST"])
def product_delete(product_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can delete product records.", "error")
        return redirect(safe_next_path("dashboard"))

    db = get_db()
    next_path = safe_next_path("products")
    product = db.execute(
        """
        SELECT id, imei, brand, model, status
        FROM products
        WHERE id = ?
        """,
        (product_id,),
    ).fetchone()
    if product is None:
        flash("Product not found.", "error")
        return redirect(next_path)

    has_sales = bool(query_scalar("SELECT COUNT(*) FROM sales WHERE product_id = ?", (product_id,)))
    has_adjustments = bool(
        query_scalar("SELECT COUNT(*) FROM stock_adjustments WHERE product_id = ?", (product_id,))
    )
    if str(product["status"] or "").upper() == "SOLD" or has_sales or has_adjustments:
        flash(
            "This product cannot be deleted because it already has sale/return history. Edit it instead.",
            "error",
        )
        return redirect(next_path)

    if request.method == "GET":
        return render_template(
            "product_delete_confirm.html",
            product=product,
            next_path=next_path,
            confirm_tracking_code="",
            confirm_delete_phrase="",
            confirm_ack=False,
        )

    confirm_tracking_code = normalize_tracking_code(
        request.form.get("confirm_tracking_code", "").strip(),
        get_current_tracking_mode(),
    )
    confirm_delete_phrase = request.form.get("confirm_delete_phrase", "").strip().upper()
    confirm_ack = request.form.get("confirm_ack") == "1"

    if confirm_tracking_code != str(product["imei"] or "") or confirm_delete_phrase != "DELETE" or not confirm_ack:
        flash(
            "Delete confirmation failed. Type the exact product IMEI and DELETE, then tick the confirmation box.",
            "error",
        )
        return render_template(
            "product_delete_confirm.html",
            product=product,
            next_path=next_path,
            confirm_tracking_code=confirm_tracking_code,
            confirm_delete_phrase=request.form.get("confirm_delete_phrase", "").strip(),
            confirm_ack=confirm_ack,
        )

    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()
    write_audit_log(
        action="PRODUCT_DELETED",
        metadata={
            "product_id": product_id,
            "tracking_code": str(product["imei"] or ""),
            "brand": str(product["brand"] or ""),
            "model": str(product["model"] or ""),
        },
    )
    flash("Product deleted safely.", "success")
    return redirect(next_path)


@app.route("/model-catalog", methods=["GET", "POST"])
def model_catalog():
    db = get_db()
    ensure_model_catalog_table(db)
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("login_selector"))

    if request.method == "POST":
        action = request.form.get("action", "save").strip().lower()
        catalog_id = parse_optional_int(request.form.get("catalog_id", ""))

        if action == "toggle":
            if not catalog_id:
                flash("Invalid model profile.", "error")
                return redirect(url_for("model_catalog"))
            row = db.execute(
                "SELECT id, is_active FROM model_catalog WHERE id = ?",
                (catalog_id,),
            ).fetchone()
            if row is None:
                flash("Model profile not found.", "error")
                return redirect(url_for("model_catalog"))
            next_state = 0 if int(row["is_active"]) == 1 else 1
            db.execute(
                "UPDATE model_catalog SET is_active = ?, updated_at = ? WHERE id = ?",
                (next_state, now_sqlite_text(), catalog_id),
            )
            db.commit()
            flash("Model profile status updated.", "success")
            return redirect(url_for("model_catalog"))

        if action == "delete":
            if not catalog_id:
                flash("Invalid model profile.", "error")
                return redirect(url_for("model_catalog"))
            db.execute("DELETE FROM model_catalog WHERE id = ?", (catalog_id,))
            db.commit()
            flash("Model profile deleted.", "success")
            return redirect(url_for("model_catalog"))

        brand = normalize_text_field(request.form.get("brand", ""))
        model_name = normalize_text_field(request.form.get("model_name", ""))
        model_number = normalize_text_field(request.form.get("model_number", ""))
        storage_input = request.form.get("storage", "").strip()
        region = normalize_text_field(request.form.get("region", ""))
        color = normalize_text_field(request.form.get("color", ""))
        condition_state = normalize_model_condition(request.form.get("condition_state", "NEW"))
        category = normalize_text_field(request.form.get("category", "")) or "Smartphone"
        tac_prefix = normalize_tac_prefix(request.form.get("tac_prefix", ""))
        keywords = normalize_catalog_keywords(request.form.get("keywords", ""))
        extra_info = normalize_text_field(request.form.get("extra_info", ""))
        supplier_id = parse_optional_int(request.form.get("supplier_id", ""))
        purchase_price = parse_optional_money(request.form.get("purchase_price", ""))
        wholesale_price = parse_optional_money(request.form.get("wholesale_price", ""))
        retail_price = parse_optional_money(request.form.get("retail_price", ""))
        is_active = 1 if request.form.get("is_active", "1") in {"1", "on", "true", "yes"} else 0

        if not brand or not model_name:
            flash("Brand and model name are required.", "error")
            return redirect(url_for("model_catalog"))

        if supplier_id is not None:
            supplier_row = db.execute("SELECT id FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
            if supplier_row is None:
                supplier_id = None

        iphone_storage = ("APPLE" in brand.upper()) or ("IPHONE" in model_name.upper())
        storage = infer_storage_variant(storage_input, iphone_only_storage=iphone_storage)
        if not storage:
            storage = normalize_text_field(storage_input)

        duplicate_row = find_duplicate_model_catalog_profile(
            db,
            brand=brand,
            model_name=model_name,
            storage=storage,
            region=region,
            color=color,
            condition_state=condition_state,
            exclude_id=None,
        )
        if duplicate_row is not None:
            flash(
                "Duplicate model blocked: same Brand + Model + Storage + Region + Color + Condition already exists.",
                "error",
            )
            return redirect(url_for("model_catalog"))

        db.execute(
            """
            INSERT INTO model_catalog (
                brand, model_name, model_number, storage, region, color,
                condition_state, category, tac_prefix, keywords, extra_info,
                purchase_price, wholesale_price, retail_price, supplier_id,
                is_active, created_by_user_id, created_by_username, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                brand,
                model_name,
                model_number or None,
                storage or None,
                region or None,
                color or None,
                condition_state,
                category,
                tac_prefix or None,
                keywords or None,
                extra_info or None,
                purchase_price,
                wholesale_price,
                retail_price,
                supplier_id,
                is_active,
                int(current_user["id"]),
                str(current_user["username"]),
                now_sqlite_text(),
                now_sqlite_text(),
            ),
        )
        db.commit()
        flash("Model profile saved. Photo/OCR auto fill will use it.", "success")
        return redirect(url_for("model_catalog"))

    q = request.args.get("q", "").strip()
    where_parts: list[str] = []
    params: list[str] = []
    if q:
        where_parts.append(
            """
            (
                UPPER(COALESCE(mc.brand, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(mc.model_name, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(mc.model_number, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(mc.storage, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(mc.region, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(mc.color, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(mc.tac_prefix, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(mc.keywords, '')) LIKE UPPER(?)
                OR UPPER(COALESCE(mc.extra_info, '')) LIKE UPPER(?)
            )
            """
        )
        like_q = f"%{q}%"
        params.extend([like_q] * 9)
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    rows = db.execute(
        f"""
        SELECT
            mc.*,
            s.name AS supplier_name
        FROM model_catalog mc
        LEFT JOIN suppliers s ON s.id = mc.supplier_id
        {where_sql}
        ORDER BY mc.is_active DESC, mc.id DESC
        LIMIT 800
        """,
        tuple(params),
    ).fetchall()

    stats = db.execute(
        """
        SELECT
            COUNT(*) AS total_profiles,
            COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_profiles
        FROM model_catalog
        """
    ).fetchone()
    suppliers = db.execute("SELECT id, name FROM suppliers ORDER BY name ASC").fetchall()

    return render_template(
        "model_catalog.html",
        rows=rows,
        suppliers=suppliers,
        q=q,
        stats=stats,
        today=date.today().isoformat(),
        condition_states=["NEW", "ACTIVE", "USED"],
    )


@app.post("/api/parse-product-text")
def parse_product_text():
    payload = request.get_json(silent=True) or {}
    raw_text = str(payload.get("text", ""))
    db = get_db()
    detected_codes = normalize_tracking_text(raw_text, get_current_tracking_mode())
    fields = infer_product_fields_from_text(raw_text)
    catalog_profile, catalog_score, catalog_tac_match = find_model_catalog_match(db, raw_text, detected_codes)

    if catalog_profile is not None:
        catalog_brand = str(catalog_profile["brand"] or "").strip()
        catalog_model = str(catalog_profile["model_name"] or "").strip()
        catalog_model_no = str(catalog_profile["model_number"] or "").strip()
        catalog_region = str(catalog_profile["region"] or "").strip()
        catalog_color = str(catalog_profile["color"] or "").strip()
        catalog_condition = normalize_model_condition(str(catalog_profile["condition_state"] or "NEW"))
        catalog_category = str(catalog_profile["category"] or "").strip()
        catalog_storage_raw = str(catalog_profile["storage"] or "").strip()
        iphone_storage = ("APPLE" in catalog_brand.upper()) or ("IPHONE" in catalog_model.upper())
        catalog_storage = infer_storage_variant(catalog_storage_raw, iphone_only_storage=iphone_storage) or catalog_storage_raw

        if catalog_brand:
            fields["brand"] = catalog_brand
        if catalog_model:
            fields["model"] = catalog_model
        if catalog_storage and (
            not fields.get("storage")
            or str(fields.get("storage") or "").strip().upper() != catalog_storage.strip().upper()
        ):
            fields["storage"] = catalog_storage
        if catalog_color and not fields.get("color"):
            fields["color"] = catalog_color
        if not fields.get("category") and catalog_category:
            fields["category"] = catalog_category

        if not fields.get("purchase_price") and catalog_profile["purchase_price"] is not None:
            amount = float(catalog_profile["purchase_price"] or 0)
            if amount > 0:
                fields["purchase_price"] = f"{amount:.2f}"
        if not fields.get("wholesale_price") and catalog_profile["wholesale_price"] is not None:
            amount = float(catalog_profile["wholesale_price"] or 0)
            if amount > 0:
                fields["wholesale_price"] = f"{amount:.2f}"
        if not fields.get("retail_price") and catalog_profile["retail_price"] is not None:
            amount = float(catalog_profile["retail_price"] or 0)
            if amount > 0:
                fields["retail_price"] = f"{amount:.2f}"
        if not fields.get("supplier_id") and catalog_profile["supplier_id"] is not None:
            fields["supplier_id"] = str(catalog_profile["supplier_id"])

        fields["region"] = catalog_region
        fields["condition_state"] = catalog_condition
        fields["model_number"] = catalog_model_no

        catalog_note = build_model_catalog_note(
            model_number=catalog_model_no,
            region=catalog_region,
            condition_state=catalog_condition,
            extra_info=str(catalog_profile["extra_info"] or "").strip(),
        )
        if catalog_note:
            fields["note"] = catalog_note

    supplier_id = infer_supplier_id_from_text(db, raw_text)
    if supplier_id is not None and not fields.get("supplier_id"):
        fields["supplier_id"] = str(supplier_id)

    inferred_model = str(fields.get("model", "") or "").strip()
    if inferred_model and not is_plausible_model_name(inferred_model):
        fields["model"] = ""
        inferred_model = ""

    should_try_recent_profile = (
        catalog_profile is None
        and bool(str(fields.get("brand", "") or "").strip())
        and bool(inferred_model)
    )

    profile = None
    if should_try_recent_profile:
        profile = find_recent_product_profile(
            db,
            brand=fields.get("brand", ""),
            model=fields.get("model", ""),
            storage=fields.get("storage", ""),
            color=fields.get("color", ""),
        )

    if profile is not None:
        if not fields.get("brand"):
            fields["brand"] = str(profile["brand"] or "")
        if not fields.get("model"):
            fields["model"] = str(profile["model"] or "")
        if not fields.get("category"):
            fields["category"] = str(profile["category"] or "")
        if not fields.get("storage"):
            fields["storage"] = str(profile["storage"] or "")
        if not fields.get("color"):
            fields["color"] = str(profile["color"] or "")
        if not fields.get("warranty_type"):
            fields["warranty_type"] = normalize_warranty_type(str(profile["warranty_type"] or ""))
        if not fields.get("purchase_price"):
            fields["purchase_price"] = f"{float(profile['purchase_price'] or 0):.2f}"
        if not fields.get("wholesale_price"):
            fields["wholesale_price"] = f"{float(profile['wholesale_price'] or 0):.2f}"
        if not fields.get("retail_price"):
            fields["retail_price"] = f"{float(profile['retail_price'] or 0):.2f}"
        if not fields.get("supplier_id") and profile["supplier_id"] is not None:
            fields["supplier_id"] = str(profile["supplier_id"])

    safe_auto_fill = False
    if catalog_profile is not None:
        safe_auto_fill = True
    else:
        safe_auto_fill = bool(
            str(fields.get("brand", "") or "").strip()
            and str(fields.get("model", "") or "").strip()
            and is_plausible_model_name(str(fields.get("model", "") or "").strip())
            and str(fields.get("storage", "") or "").strip()
        )

    if not safe_auto_fill and catalog_profile is None:
        fields["brand"] = ""
        fields["model"] = ""
        fields["storage"] = ""
        fields["color"] = ""
        fields["warranty_type"] = ""
        fields["purchase_price"] = ""
        fields["wholesale_price"] = ""
        fields["retail_price"] = ""
        fields["supplier_id"] = ""

    return jsonify(
        {
            "ok": True,
            "fields": fields,
            "codes": detected_codes[:200],
            "catalog_match": catalog_profile is not None,
            "catalog_score": catalog_score,
            "catalog_tac_match": catalog_tac_match,
            "catalog_id": int(catalog_profile["id"]) if catalog_profile is not None else None,
            "safe_auto_fill": safe_auto_fill,
        }
    )


@app.get("/api/stock/visibility")
def stock_visibility_api():
    db = get_db()
    ensure_enterprise_tables(db)
    expire_active_reservations(db)
    db.commit()

    q = request.args.get("q", "").strip()
    branch_id = parse_optional_int(request.args.get("branch_id", "").strip())
    limit = parse_optional_int(request.args.get("limit", "150").strip()) or 150
    limit = max(10, min(limit, 500))

    where_parts = ["p.status = 'IN_STOCK'"]
    params: list[object] = []
    if branch_id is not None:
        where_parts.append("p.branch_id = ?")
        params.append(branch_id)
    if q:
        wildcard = f"%{q}%"
        where_parts.append(
            """
            (
                p.imei LIKE ?
                OR p.brand LIKE ?
                OR p.model LIKE ?
                OR COALESCE(p.storage, '') LIKE ?
                OR COALESCE(p.color, '') LIKE ?
            )
            """
        )
        params.extend([wildcard, wildcard, wildcard, wildcard, wildcard])

    rows = db.execute(
        f"""
        SELECT
            p.id, p.imei, p.brand, p.model, COALESCE(p.storage, '') AS storage,
            COALESCE(p.color, '') AS color, p.branch_id, COALESCE(b.name, 'Main Branch') AS branch_name
        FROM products p
        LEFT JOIN branches b ON b.id = p.branch_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY p.id DESC
        LIMIT {limit}
        """,
        tuple(params),
    ).fetchall()

    active_reservations = db.execute(
        """
        SELECT product_id, reservation_key, reserved_for, reserved_by_username, expires_at
        FROM inventory_reservations
        WHERE status = 'ACTIVE'
        """
    ).fetchall()
    reservation_by_product = {int(item["product_id"]): dict(item) for item in active_reservations}

    items: list[dict[str, object]] = []
    reserved_count = 0
    for row in rows:
        product_id = int(row["id"])
        reservation = reservation_by_product.get(product_id)
        is_reserved = reservation is not None
        if is_reserved:
            reserved_count += 1
        items.append(
            {
                "product_id": product_id,
                "imei": str(row["imei"]),
                "brand": str(row["brand"] or ""),
                "model": str(row["model"] or ""),
                "storage": str(row["storage"] or ""),
                "color": str(row["color"] or ""),
                "branch_id": int(row["branch_id"] or 1),
                "branch_name": str(row["branch_name"] or ""),
                "reserved": is_reserved,
                "reservation_key": str(reservation["reservation_key"]) if reservation else "",
                "reserved_for": str(reservation["reserved_for"]) if reservation else "",
                "reserved_by": str(reservation["reserved_by_username"]) if reservation else "",
                "reservation_expires_at": str(reservation["expires_at"]) if reservation else "",
            }
        )

    return jsonify(
        {
            "ok": True,
            "summary": {
                "visible_items": len(items),
                "reserved_items": reserved_count,
                "free_items": max(0, len(items) - reserved_count),
            },
            "items": items,
        }
    )


@app.post("/api/stock/reserve")
def stock_reserve_api():
    db = get_db()
    ensure_enterprise_tables(db)
    expire_active_reservations(db)

    payload = request.get_json(silent=True) or {}
    imei = normalize_tracking_code(str(payload.get("imei", "")).strip(), get_current_tracking_mode())
    product_id_raw = str(payload.get("product_id", "")).strip()
    product_id = parse_optional_int(product_id_raw) if product_id_raw else None
    reserve_minutes = parse_int_with_default(payload.get("reserve_minutes", 60), 60)
    reserve_minutes = max(5, min(reserve_minutes, 24 * 60))
    reserved_for = normalize_text_field(str(payload.get("reserved_for", "")))
    note = normalize_text_field(str(payload.get("note", "")))

    if product_id is None and not imei:
        return jsonify({"ok": False, "message": "Provide product_id or IMEI."}), 400

    if product_id is not None:
        product = db.execute(
            """
            SELECT id, imei, status, branch_id
            FROM products
            WHERE id = ?
            """,
            (product_id,),
        ).fetchone()
    else:
        product = db.execute(
            """
            SELECT id, imei, status, branch_id
            FROM products
            WHERE imei = ?
            """,
            (imei,),
        ).fetchone()

    if product is None:
        return jsonify({"ok": False, "message": "Product not found."}), 404
    if str(product["status"]) != "IN_STOCK":
        return jsonify({"ok": False, "message": "Product is not in stock."}), 409

    existing = db.execute(
        """
        SELECT reservation_key, expires_at, reserved_by_username, reserved_for
        FROM inventory_reservations
        WHERE product_id = ? AND status = 'ACTIVE'
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(product["id"]),),
    ).fetchone()
    if existing is not None:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "Product already reserved.",
                    "reservation_key": str(existing["reservation_key"] or ""),
                    "expires_at": str(existing["expires_at"] or ""),
                    "reserved_by": str(existing["reserved_by_username"] or ""),
                    "reserved_for": str(existing["reserved_for"] or ""),
                }
            ),
            409,
        )

    tenant_user = get_current_tenant_user()
    reservation_key = uuid.uuid4().hex[:24].upper()
    expires_at = (datetime.now() + timedelta(minutes=reserve_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        """
        INSERT INTO inventory_reservations (
            reservation_key, product_id, branch_id, reserved_for,
            reserved_by_user_id, reserved_by_username, status, expires_at, note, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?)
        """,
        (
            reservation_key,
            int(product["id"]),
            int(product["branch_id"] or 1),
            reserved_for,
            int(tenant_user["id"]) if tenant_user is not None else None,
            str(tenant_user["username"]) if tenant_user is not None else "",
            expires_at,
            note,
            now_sqlite_text(),
            now_sqlite_text(),
        ),
    )
    enqueue_integration_outbox(
        db,
        event_type="STOCK_RESERVED",
        payload={
            "reservation_key": reservation_key,
            "product_id": int(product["id"]),
            "imei": str(product["imei"] or ""),
            "reserved_for": reserved_for,
            "expires_at": expires_at,
        },
    )
    db.commit()
    return jsonify(
        {
            "ok": True,
            "reservation_key": reservation_key,
            "product_id": int(product["id"]),
            "imei": str(product["imei"] or ""),
            "expires_at": expires_at,
        }
    )


@app.post("/api/stock/release")
def stock_release_api():
    db = get_db()
    ensure_enterprise_tables(db)
    expire_active_reservations(db)

    payload = request.get_json(silent=True) or {}
    reservation_key = str(payload.get("reservation_key", "")).strip().upper()
    imei = normalize_tracking_code(str(payload.get("imei", "")).strip(), get_current_tracking_mode())
    product_id_raw = str(payload.get("product_id", "")).strip()
    product_id = parse_optional_int(product_id_raw) if product_id_raw else None
    note = normalize_text_field(str(payload.get("note", "")))

    row: sqlite3.Row | None = None
    if reservation_key:
        row = db.execute(
            """
            SELECT id, reservation_key, product_id
            FROM inventory_reservations
            WHERE reservation_key = ? AND status = 'ACTIVE'
            ORDER BY id DESC
            LIMIT 1
            """,
            (reservation_key,),
        ).fetchone()
    elif product_id is not None:
        row = db.execute(
            """
            SELECT id, reservation_key, product_id
            FROM inventory_reservations
            WHERE product_id = ? AND status = 'ACTIVE'
            ORDER BY id DESC
            LIMIT 1
            """,
            (product_id,),
        ).fetchone()
    elif imei:
        row = db.execute(
            """
            SELECT r.id, r.reservation_key, r.product_id
            FROM inventory_reservations r
            JOIN products p ON p.id = r.product_id
            WHERE p.imei = ? AND r.status = 'ACTIVE'
            ORDER BY r.id DESC
            LIMIT 1
            """,
            (imei,),
        ).fetchone()

    if row is None:
        return jsonify({"ok": False, "message": "Active reservation not found."}), 404

    updated_note = note
    if updated_note:
        updated_note = f"RELEASE_NOTE: {updated_note}"
    db.execute(
        """
        UPDATE inventory_reservations
        SET status = 'RELEASED',
            note = CASE
                WHEN ? <> '' THEN ?
                ELSE note
            END,
            updated_at = ?
        WHERE id = ?
        """,
        (updated_note, updated_note, now_sqlite_text(), int(row["id"])),
    )
    enqueue_integration_outbox(
        db,
        event_type="STOCK_RESERVATION_RELEASED",
        payload={
            "reservation_key": str(row["reservation_key"] or ""),
            "product_id": int(row["product_id"] or 0),
        },
    )
    db.commit()
    return jsonify(
        {
            "ok": True,
            "reservation_key": str(row["reservation_key"] or ""),
            "product_id": int(row["product_id"] or 0),
        }
    )


@app.post("/api/stock/transfer")
def stock_transfer_api():
    tenant_user = get_current_tenant_user()
    if tenant_user is None or normalize_role(str(tenant_user["role"]), default="USER") != "ADMIN":
        return jsonify({"ok": False, "message": "Only tenant admin can transfer stock."}), 403

    db = get_db()
    ensure_enterprise_tables(db)

    payload = request.get_json(silent=True) or {}
    from_branch_id = parse_int_with_default(payload.get("from_branch_id", 1), 1)
    to_branch_id = parse_int_with_default(payload.get("to_branch_id", 1), 1)
    if from_branch_id <= 0 or to_branch_id <= 0 or from_branch_id == to_branch_id:
        return jsonify({"ok": False, "message": "Invalid from/to branch."}), 400

    from_branch = db.execute("SELECT id, name FROM branches WHERE id = ?", (from_branch_id,)).fetchone()
    to_branch = db.execute("SELECT id, name FROM branches WHERE id = ?", (to_branch_id,)).fetchone()
    if from_branch is None or to_branch is None:
        return jsonify({"ok": False, "message": "Branch not found."}), 404

    raw_product_ids = payload.get("product_ids", [])
    raw_imeis = payload.get("imeis", [])
    product_ids: list[int] = []
    imeis: list[str] = []
    if isinstance(raw_product_ids, list):
        for item in raw_product_ids:
            pid = parse_int_with_default(item, 0)
            if pid > 0:
                product_ids.append(pid)
    if isinstance(raw_imeis, str):
        raw_imeis = re.split(r"[,\n; ]+", raw_imeis.strip())
    if isinstance(raw_imeis, list):
        for item in raw_imeis:
            code = normalize_tracking_code(str(item or "").strip(), get_current_tracking_mode())
            if code:
                imeis.append(code)

    candidates: list[sqlite3.Row] = []
    if product_ids:
        placeholders = ", ".join(["?"] * len(product_ids))
        candidates.extend(
            db.execute(
                f"""
                SELECT id, imei, branch_id, status
                FROM products
                WHERE id IN ({placeholders})
                """,
                tuple(product_ids),
            ).fetchall()
        )
    if imeis:
        placeholders = ", ".join(["?"] * len(imeis))
        candidates.extend(
            db.execute(
                f"""
                SELECT id, imei, branch_id, status
                FROM products
                WHERE imei IN ({placeholders})
                """,
                tuple(imeis),
            ).fetchall()
        )

    unique_by_id: dict[int, sqlite3.Row] = {}
    for row in candidates:
        unique_by_id[int(row["id"])] = row
    candidate_rows = list(unique_by_id.values())
    if not candidate_rows:
        return jsonify({"ok": False, "message": "No products found for transfer."}), 404

    transferred = 0
    skipped = 0
    base_no = datetime.now().strftime("TRF-%Y%m%d%H%M%S")
    for idx, row in enumerate(candidate_rows, start=1):
        if str(row["status"] or "") != "IN_STOCK":
            skipped += 1
            continue
        if int(row["branch_id"] or 1) != from_branch_id:
            skipped += 1
            continue
        transfer_no = f"{base_no}-{idx:03d}"
        db.execute(
            """
            INSERT INTO branch_transfers (
                transfer_no, product_id, from_branch_id, to_branch_id, status,
                requested_by_user_id, requested_by_username, approved_by_user_id, approved_by_username,
                note, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'RECEIVED', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transfer_no,
                int(row["id"]),
                from_branch_id,
                to_branch_id,
                int(tenant_user["id"]),
                str(tenant_user["username"]),
                int(tenant_user["id"]),
                str(tenant_user["username"]),
                f"Auto transfer from {from_branch['name']} to {to_branch['name']}",
                now_sqlite_text(),
                now_sqlite_text(),
            ),
        )
        db.execute("UPDATE products SET branch_id = ? WHERE id = ?", (to_branch_id, int(row["id"])))
        enqueue_integration_outbox(
            db,
            event_type="STOCK_TRANSFERRED",
            payload={
                "transfer_no": transfer_no,
                "product_id": int(row["id"]),
                "imei": str(row["imei"] or ""),
                "from_branch_id": from_branch_id,
                "to_branch_id": to_branch_id,
            },
        )
        transferred += 1

    db.commit()
    return jsonify(
        {
            "ok": True,
            "from_branch_id": from_branch_id,
            "to_branch_id": to_branch_id,
            "transferred": transferred,
            "skipped": skipped,
        }
    )


@app.get("/api/due-risk")
def due_risk_api():
    db = get_db()
    ensure_enterprise_tables(db)
    limit = parse_optional_int(request.args.get("limit", "50").strip()) or 50
    limit = max(10, min(limit, 200))

    rows = db.execute(
        f"""
        SELECT
            c.id AS customer_id,
            c.shop_name,
            COUNT(*) AS open_items,
            COALESCE(SUM(s.due_amount), 0) AS total_due,
            COALESCE(SUM(CASE WHEN (julianday('now') - julianday(s.sold_at)) <= 7 THEN s.due_amount ELSE 0 END), 0) AS bucket_0_7,
            COALESCE(SUM(CASE WHEN (julianday('now') - julianday(s.sold_at)) > 7 AND (julianday('now') - julianday(s.sold_at)) <= 30 THEN s.due_amount ELSE 0 END), 0) AS bucket_8_30,
            COALESCE(SUM(CASE WHEN (julianday('now') - julianday(s.sold_at)) > 30 AND (julianday('now') - julianday(s.sold_at)) <= 60 THEN s.due_amount ELSE 0 END), 0) AS bucket_31_60,
            COALESCE(SUM(CASE WHEN (julianday('now') - julianday(s.sold_at)) > 60 THEN s.due_amount ELSE 0 END), 0) AS bucket_61_plus,
            MAX(s.sold_at) AS last_sale_date
        FROM sales s
        JOIN customers c ON c.id = s.customer_id
        WHERE s.is_active = 1
          AND s.sale_type = 'WHOLESALE'
          AND s.due_amount > 0
          AND c.shop_name <> ?
        GROUP BY c.id, c.shop_name
        ORDER BY total_due DESC
        LIMIT {limit}
        """,
        (LOCAL_RETAIL_WHOLESALE_SHOP_NAME,),
    ).fetchall()

    pending_followups = db.execute(
        """
        SELECT customer_id, COUNT(*) AS pending_count
        FROM due_followups
        WHERE status = 'PENDING'
        GROUP BY customer_id
        """
    ).fetchall()
    followup_map = {int(item["customer_id"]): int(item["pending_count"] or 0) for item in pending_followups}

    items: list[dict[str, object]] = []
    total_due = 0.0
    for row in rows:
        customer_id = int(row["customer_id"])
        item_due = float(row["total_due"] or 0)
        total_due += item_due
        high_risk_due = float(row["bucket_31_60"] or 0) + float(row["bucket_61_plus"] or 0)
        risk_level = "LOW"
        if item_due >= 500000 or high_risk_due >= 250000:
            risk_level = "HIGH"
        elif item_due >= 100000 or high_risk_due >= 50000:
            risk_level = "MEDIUM"
        items.append(
            {
                "customer_id": customer_id,
                "shop_name": str(row["shop_name"] or ""),
                "open_items": int(row["open_items"] or 0),
                "total_due": item_due,
                "bucket_0_7": float(row["bucket_0_7"] or 0),
                "bucket_8_30": float(row["bucket_8_30"] or 0),
                "bucket_31_60": float(row["bucket_31_60"] or 0),
                "bucket_61_plus": float(row["bucket_61_plus"] or 0),
                "last_sale_date": str(row["last_sale_date"] or ""),
                "pending_followups": int(followup_map.get(customer_id, 0)),
                "risk_level": risk_level,
            }
        )

    return jsonify({"ok": True, "summary": {"shops": len(items), "total_due": total_due}, "items": items})


@app.post("/api/due-risk/followup")
def due_risk_followup_api():
    db = get_db()
    ensure_enterprise_tables(db)
    payload = request.get_json(silent=True) or {}
    customer_id = parse_int_with_default(payload.get("customer_id", 0), 0)
    if customer_id <= 0:
        return jsonify({"ok": False, "message": "customer_id is required."}), 400

    customer = db.execute("SELECT id, shop_name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if customer is None:
        return jsonify({"ok": False, "message": "Customer not found."}), 404

    followup_date = normalize_date(str(payload.get("followup_date", "")).strip() or date.today().isoformat())
    channel = normalize_text_field(str(payload.get("channel", "CALL"))).upper() or "CALL"
    if channel not in {"CALL", "SMS", "VISIT", "WHATSAPP", "OTHER"}:
        channel = "OTHER"
    note = normalize_text_field(str(payload.get("note", "")))
    status = normalize_text_field(str(payload.get("status", "PENDING"))).upper() or "PENDING"
    if status not in {"PENDING", "DONE", "SKIPPED"}:
        status = "PENDING"
    actor = get_current_tenant_user()

    db.execute(
        """
        INSERT INTO due_followups (
            customer_id, followup_date, channel, note, status,
            created_by_user_id, created_by_username, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            customer_id,
            followup_date,
            channel,
            note,
            status,
            int(actor["id"]) if actor is not None else None,
            str(actor["username"]) if actor is not None else "",
            now_sqlite_text(),
        ),
    )
    followup_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    db.commit()

    return jsonify(
        {
            "ok": True,
            "followup_id": followup_id,
            "customer_id": customer_id,
            "shop_name": str(customer["shop_name"] or ""),
            "followup_date": followup_date,
            "channel": channel,
            "status": status,
        }
    )


@app.get("/api/tenant/limits")
def tenant_limits_api():
    tenant = get_current_tenant()
    if tenant is None:
        return jsonify({"ok": False, "message": "Tenant not found in session."}), 401
    db = get_db()
    limits = get_tenant_plan_limits(tenant)
    usage = get_tenant_usage_snapshot(db)
    return jsonify({"ok": True, "plan": limits, "usage": usage})


@app.get("/api/health")
def health_api():
    db = get_db()
    db.execute("SELECT 1").fetchone()
    tenant = get_current_tenant()
    limits = get_tenant_plan_limits(tenant)
    usage = get_tenant_usage_snapshot(db)

    redis_ok = get_redis_client() is not None
    queue_size = 0
    client = get_redis_client()
    if client is not None:
        try:
            queue_size = int(client.llen(REDIS_QUEUE_NAME))  # type: ignore[union-attr]
        except Exception:
            queue_size = 0

    return jsonify(
        {
            "ok": True,
            "status": "healthy",
            "service": "Soft X",
            "now": now_sqlite_text(),
            "tenant_id": int(tenant["id"]) if tenant is not None else 0,
            "plan": limits,
            "usage": usage,
            "redis_ok": redis_ok,
            "queue_size": queue_size,
        }
    )


@app.get("/api/metrics")
def metrics_api():
    db = get_db()
    ensure_enterprise_tables(db)
    expire_active_reservations(db)
    today = date.today().isoformat()
    month_prefix = today[:7]
    tenant = get_current_tenant()
    plan = get_tenant_plan_limits(tenant)
    usage = get_tenant_usage_snapshot(db)

    metrics = {
        "in_stock": int(query_scalar("SELECT COUNT(*) FROM products WHERE status = 'IN_STOCK'")),
        "sold_active": int(query_scalar("SELECT COUNT(*) FROM sales WHERE is_active = 1")),
        "today_sales": int(query_scalar("SELECT COUNT(*) FROM sales WHERE sold_at = ? AND is_active = 1", (today,))),
        "month_sales": int(
            query_scalar("SELECT COUNT(*) FROM sales WHERE is_active = 1 AND substr(sold_at, 1, 7) = ?", (month_prefix,))
        ),
        "due_outstanding": float(query_scalar("SELECT COALESCE(SUM(due_amount), 0) FROM sales WHERE is_active = 1")),
        "active_reservations": int(
            query_scalar("SELECT COUNT(*) FROM inventory_reservations WHERE status = 'ACTIVE'")
        ),
        "pending_followups": int(query_scalar("SELECT COUNT(*) FROM due_followups WHERE status = 'PENDING'")),
        "fraud_events_7d": int(
            query_scalar(
                """
                SELECT COUNT(*)
                FROM fraud_events
                WHERE created_at >= DATETIME('now', '-7 day')
                """
            )
        ),
    }
    db.commit()
    return jsonify({"ok": True, "metrics": metrics, "plan": plan, "usage": usage})


@app.route("/api/automation/rules", methods=["GET", "POST"])
def automation_rules_api():
    db = get_db()
    ensure_enterprise_tables(db)
    current_user = get_current_tenant_user()

    if request.method == "GET":
        rows = db.execute(
            """
            SELECT id, rule_name, event_type, conditions_json, actions_json, is_active, created_at, updated_at
            FROM automation_rules
            ORDER BY is_active DESC, id DESC
            LIMIT 300
            """
        ).fetchall()
        return jsonify({"ok": True, "items": [dict(row) for row in rows]})

    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        return jsonify({"ok": False, "message": "Only tenant admin can manage automation rules."}), 403

    payload = request.get_json(silent=True) or {}
    rule_id = parse_optional_int(str(payload.get("rule_id", "")).strip())
    rule_name = normalize_text_field(str(payload.get("rule_name", "")))
    event_type = normalize_text_field(str(payload.get("event_type", ""))).upper()
    is_active = 1 if str(payload.get("is_active", "1")).strip().lower() in {"1", "true", "yes", "on"} else 0
    conditions = payload.get("conditions", {})
    actions = payload.get("actions", {})

    if not rule_name or not event_type:
        return jsonify({"ok": False, "message": "rule_name and event_type are required."}), 400

    conditions_json = json.dumps(conditions if isinstance(conditions, dict) else {}, ensure_ascii=False)
    actions_json = json.dumps(actions if isinstance(actions, dict) else {}, ensure_ascii=False)

    if rule_id is None:
        db.execute(
            """
            INSERT INTO automation_rules (
                rule_name, event_type, conditions_json, actions_json, is_active,
                created_by_user_id, created_by_username, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_name,
                event_type,
                conditions_json,
                actions_json,
                is_active,
                int(current_user["id"]),
                str(current_user["username"]),
                now_sqlite_text(),
                now_sqlite_text(),
            ),
        )
        db.commit()
        created_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        return jsonify({"ok": True, "rule_id": created_id, "message": "Rule created."})

    existing = db.execute("SELECT id FROM automation_rules WHERE id = ?", (rule_id,)).fetchone()
    if existing is None:
        return jsonify({"ok": False, "message": "Rule not found."}), 404

    db.execute(
        """
        UPDATE automation_rules
        SET rule_name = ?, event_type = ?, conditions_json = ?, actions_json = ?,
            is_active = ?, updated_at = ?
        WHERE id = ?
        """,
        (rule_name, event_type, conditions_json, actions_json, is_active, now_sqlite_text(), rule_id),
    )
    db.commit()
    return jsonify({"ok": True, "rule_id": int(rule_id), "message": "Rule updated."})


@app.route("/sales", methods=["GET", "POST"])
def sales():
    db = get_db()

    def redirect_sales_view(**extra: object):
        params: dict[str, object] = {}
        current_sview = normalize_text_field(request.values.get("sview", "")).lower()
        if current_sview in {"entry", "return", "due", "history"}:
            params["sview"] = current_sview
        for key, value in extra.items():
            if value is not None:
                params[key] = value
        return redirect(url_for("sales", **params))

    if request.method == "POST":
        product_id = parse_optional_int(request.form.get("product_id", ""))
        customer_id = parse_optional_int(request.form.get("customer_id", ""))
        retail_customer_id = parse_optional_int(request.form.get("retail_customer_id", ""))
        sale_type = request.form.get("sale_type", "WHOLESALE").strip().upper()
        invoice_no = request.form.get("invoice_no", "").strip().upper()
        payment_status = request.form.get("payment_status", "PAID").strip().upper()
        sold_date = normalize_date(request.form.get("sold_at", "").strip())
        note = request.form.get("note", "").strip()
        sold_price_raw = request.form.get("sold_price", "").strip()
        paid_amount_raw = request.form.get("paid_amount", "").strip()
        receiver_name = request.form.get("receiver_name", "").strip()
        receiver_phone = request.form.get("receiver_phone", "").strip()
        retail_customer_name = request.form.get("retail_customer_name", "").strip()
        retail_customer_phone = request.form.get("retail_customer_phone", "").strip()
        retail_customer_address = request.form.get("retail_customer_address", "").strip()
        receiver_photo = request.files.get("receiver_photo")
        tracking_label = get_tracking_label()

        if product_id is None:
            flash(f"Please select a valid {tracking_label}.", "error")
            return redirect_sales_view()

        if sale_type not in SALE_TYPES:
            sale_type = "WHOLESALE"

        if payment_status not in PAYMENT_STATUSES:
            payment_status = "PAID"

        customer = None
        if customer_id is not None:
            customer = db.execute("SELECT id, shop_name FROM customers WHERE id = ?", (customer_id,)).fetchone()

        selected_retail_customer = None
        if retail_customer_id is not None:
            selected_retail_customer = db.execute(
                """
                SELECT id, full_name, phone, address
                FROM retail_customers
                WHERE id = ?
                """,
                (retail_customer_id,),
            ).fetchone()

        if sale_type == "WHOLESALE":
            if customer_id is None:
                flash("Please select a wholesale shop for wholesale sale.", "error")
                return redirect_sales_view()
            if customer is None:
                flash("Wholesale shop not found.", "error")
                return redirect_sales_view()
            if str(customer["shop_name"] or "") == LOCAL_RETAIL_WHOLESALE_SHOP_NAME:
                flash("Please select a valid wholesale shop.", "error")
                return redirect_sales_view()
            retail_customer_id = None
        else:
            if retail_customer_id is not None and selected_retail_customer is None:
                flash("Selected retail customer not found.", "error")
                return redirect_sales_view()

            if selected_retail_customer is not None:
                if not retail_customer_name:
                    retail_customer_name = str(selected_retail_customer["full_name"] or "").strip()
                if not retail_customer_phone:
                    retail_customer_phone = str(selected_retail_customer["phone"] or "").strip()
                if not retail_customer_address:
                    retail_customer_address = str(selected_retail_customer["address"] or "").strip()
            if not retail_customer_name:
                flash("Retail customer name is required for retail sale.", "error")
                return redirect_sales_view()

            try:
                retail_customer_id = get_or_create_retail_customer(
                    db,
                    retail_customer_name,
                    retail_customer_phone,
                    retail_customer_address,
                    "",
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect_sales_view()

            try:
                customer_id = get_or_create_local_retail_customer_id(db)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect_sales_view()

            retail_note_parts: list[str] = []
            if retail_customer_name:
                retail_note_parts.append(f"Retail Customer: {retail_customer_name}")
            if retail_customer_phone:
                retail_note_parts.append(f"Retail Phone: {retail_customer_phone}")
            if retail_customer_address:
                retail_note_parts.append(f"Retail Address: {retail_customer_address}")
            if note:
                retail_note_parts.append(f"Note: {note}")
            if retail_note_parts:
                note = " | ".join(retail_note_parts)

        if not invoice_no:
            invoice_no = build_sale_invoice_no(db, sold_date, sale_type)

        product = db.execute(
            """
            SELECT id, imei, status, wholesale_price, retail_price, purchase_price, branch_id
            FROM products
            WHERE id = ?
            """,
            (product_id,),
        ).fetchone()

        if product is None:
            flash("Selected product not found.", "error")
            return redirect_sales_view()

        if product["status"] != "IN_STOCK":
            flash(f"This {tracking_label} is not available in stock.", "error")
            return redirect_sales_view()
        branch_id = parse_optional_int(request.form.get("branch_id", "")) or int(product["branch_id"] or 1)
        branch = db.execute("SELECT id FROM branches WHERE id = ?", (branch_id,)).fetchone()
        if branch is None:
            flash("Invalid branch selected.", "error")
            return redirect_sales_view()

        if sold_price_raw:
            try:
                sold_price = parse_money(sold_price_raw, "Sold Price")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect_sales_view()
        else:
            sold_price = (
                float(product["wholesale_price"])
                if sale_type == "WHOLESALE"
                else float(product["retail_price"])
            )

        if paid_amount_raw:
            try:
                paid_amount = parse_money(paid_amount_raw, "Paid Amount")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect_sales_view()
        else:
            paid_amount = sold_price if payment_status == "PAID" else 0.0

        if paid_amount > sold_price:
            flash("Paid Amount cannot be greater than Sold Price.", "error")
            return redirect_sales_view()

        due_amount = max(0.0, sold_price - paid_amount)
        payment_status = "PAID" if due_amount <= 0.00001 else "DUE"

        tenant = get_current_tenant()
        blocked_by_limit, limit_message, _limit_info = check_tenant_plan_limit(
            db,
            tenant,
            "max_monthly_orders",
            incoming_count=1,
        )
        if blocked_by_limit:
            flash(limit_message, "error")
            return redirect_sales_view()

        receiver_photo_path = ""
        if receiver_photo is not None and (receiver_photo.filename or "").strip():
            try:
                receiver_photo_path = save_receiver_photo(receiver_photo, prefix="sale")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect_sales_view()

        try:
            db.execute(
                """
                INSERT INTO sales (
                    product_id, customer_id, retail_customer_id, sale_type, invoice_no,
                    sold_price, payment_status, paid_amount, due_amount, sold_at, branch_id,
                    receiver_name, receiver_phone, receiver_photo_path, note, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    product_id,
                    customer_id,
                    retail_customer_id,
                    sale_type,
                    invoice_no,
                    sold_price,
                    payment_status,
                    paid_amount,
                    due_amount,
                    sold_date,
                    branch_id,
                    receiver_name,
                    receiver_phone,
                    receiver_photo_path,
                    note,
                ),
            )
            db.execute("UPDATE products SET status = 'SOLD' WHERE id = ?", (product_id,))
            db.execute(
                """
                UPDATE inventory_reservations
                SET status = 'CONSUMED',
                    updated_at = ?
                WHERE product_id = ?
                  AND status = 'ACTIVE'
                """,
                (now_sqlite_text(), int(product_id)),
            )
            sale_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            maybe_mark_low_margin_fraud(
                db,
                sale_id=sale_id,
                product_id=int(product_id),
                invoice_no=invoice_no,
                sold_price=float(sold_price),
                purchase_price=float(product["purchase_price"] or 0),
            )
            enqueue_integration_outbox(
                db,
                event_type="SALE_CREATED",
                payload={
                    "sale_id": sale_id,
                    "product_id": int(product_id),
                    "customer_id": int(customer_id or 0),
                    "retail_customer_id": int(retail_customer_id or 0),
                    "sale_type": sale_type,
                    "invoice_no": invoice_no,
                    "sold_price": float(sold_price),
                    "paid_amount": float(paid_amount),
                    "due_amount": float(due_amount),
                    "sold_at": sold_date,
                    "branch_id": int(branch_id),
                },
            )
            db.commit()
            flash(f"{tracking_label} {product['imei']} sold successfully.", "success")
        except sqlite3.IntegrityError:
            db.rollback()
            flash(f"Sale failed. This {tracking_label} may already be sold.", "error")

        return redirect_sales_view()

    in_stock_products = db.execute(
        """
        SELECT id, imei, brand, model, storage, color, category, wholesale_price, retail_price
        FROM products
        WHERE status = 'IN_STOCK'
        ORDER BY id DESC
        """
    ).fetchall()

    customers = db.execute(
        """
        SELECT id, shop_name, phone
        FROM customers
        WHERE shop_name <> ?
        ORDER BY shop_name
        """,
        (LOCAL_RETAIL_WHOLESALE_SHOP_NAME,),
    ).fetchall()

    retail_customers = db.execute(
        """
        SELECT id, full_name, phone, address, region
        FROM retail_customers
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()

    sales_list = db.execute(
        """
        SELECT
            s.*,
            p.imei, p.brand, p.model, p.purchase_price,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS shop_name,
            (s.sold_price - p.purchase_price) AS profit,
            r.return_date,
            r.reason AS return_reason,
            (
                SELECT COALESCE(SUM(dc.amount), 0)
                FROM due_collections dc
                WHERE dc.sale_id = s.id
            ) AS collected_due
        FROM sales s
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        LEFT JOIN sale_returns r ON r.sale_id = s.id
        ORDER BY s.id DESC
        LIMIT 400
        """
    ).fetchall()

    due_sales = db.execute(
        """
        SELECT
            s.id, s.sold_at, s.invoice_no, s.sale_type, s.sold_price,
            s.paid_amount, s.due_amount, p.imei, p.brand, p.model,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS shop_name
        FROM sales s
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        WHERE s.is_active = 1 AND s.due_amount > 0
        ORDER BY s.sold_at ASC, s.id ASC
        LIMIT 300
        """
    ).fetchall()

    return render_template(
        "sales.html",
        in_stock_products=in_stock_products,
        customers=customers,
        retail_customers=retail_customers,
        sales=sales_list,
        due_sales=due_sales,
        today=date.today().isoformat(),
    )


@app.get("/sales/<int:sale_id>/receiver-photo")
def sale_receiver_photo(sale_id: int):
    if not can_view_receiver_photo():
        flash("Receiver photo is visible only to shop admin.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    sale = db.execute(
        """
        SELECT id, receiver_photo_path
        FROM sales
        WHERE id = ?
        """,
        (sale_id,),
    ).fetchone()
    if sale is None:
        flash("Sale not found.", "error")
        return redirect(url_for("sales"))

    raw_path = str(sale["receiver_photo_path"] or "").strip()
    if not raw_path:
        flash("No receiver photo found for this sale.", "error")
        return redirect(request.referrer or url_for("sales"))

    photo_file = resolve_receiver_photo_file(raw_path)
    if photo_file is None or not photo_file.exists():
        flash("Receiver photo file is missing.", "error")
        return redirect(request.referrer or url_for("sales"))

    return send_from_directory(str(photo_file.parent), photo_file.name, as_attachment=False, max_age=0)


@app.get("/tenant/profile-image/<path:filename>")
def tenant_profile_image(filename: str):
    tenant = get_current_tenant()
    if tenant is None:
        abort(403)

    safe_name = Path(filename).name
    if not safe_name:
        abort(404)

    match = re.match(r"^t(\d+)-", safe_name)
    if match and int(match.group(1)) != int(tenant["id"]):
        abort(403)

    target = resolve_profile_image_file(safe_name)
    if target is None:
        abort(404)
    return send_from_directory(str(target.parent), target.name, as_attachment=False, max_age=0)


@app.post("/sales/<int:sale_id>/return")
def sale_return(sale_id: int):
    db = get_db()
    return_date = normalize_date(request.form.get("return_date", "").strip())
    reason = request.form.get("reason", "").strip() or "Customer return"
    restock = request.form.get("restock", "1") == "1"
    tracking_label = get_tracking_label()
    sales_view = normalize_text_field(request.form.get("sview", "")).lower()

    try:
        sale = process_sale_return(db, sale_id=sale_id, return_date=return_date, reason=reason, restock=restock)
        flash(f"Return complete for {tracking_label} {sale['imei']} and stock updated.", "success")
    except ValueError as exc:
        flash(str(exc), "error")

    if sales_view in {"entry", "return", "due", "history"}:
        return redirect(url_for("sales", sview=sales_view))
    return redirect(url_for("sales"))


@app.route("/retail-sales", methods=["GET", "POST"])
def retail_sales():
    db = get_db()
    tracking_label = get_tracking_label()
    today = date.today().isoformat()

    if request.method == "POST":
        retail_customer_id = parse_optional_int(request.form.get("retail_customer_id", ""))
        customer_name = request.form.get("customer_name", "").strip()
        customer_phone = request.form.get("customer_phone", "").strip()
        customer_address = request.form.get("customer_address", "").strip()
        sold_date = normalize_date(request.form.get("sold_at", "").strip())
        note = request.form.get("note", "").strip()
        payment_status = request.form.get("payment_status", "PAID").strip().upper()
        imei_bulk = request.form.get("imei_bulk", "")
        paid_total_raw = request.form.get("paid_total", "").strip()
        one_price_raw = request.form.get("one_price", "").strip()
        manual_invoice_no = request.form.get("invoice_no", "").strip().upper()
        sale_branch_id = parse_optional_int(request.form.get("branch_id", ""))

        selected_retail_customer = None
        if retail_customer_id is not None:
            selected_retail_customer = db.execute(
                """
                SELECT id, full_name, phone, address
                FROM retail_customers
                WHERE id = ?
                """,
                (retail_customer_id,),
            ).fetchone()
            if selected_retail_customer is None:
                flash("Selected retail customer not found.", "error")
                return redirect(url_for("retail_sales"))

        if selected_retail_customer is not None:
            if not customer_name:
                customer_name = str(selected_retail_customer["full_name"] or "").strip()
            if not customer_phone:
                customer_phone = str(selected_retail_customer["phone"] or "").strip()
            if not customer_address:
                customer_address = str(selected_retail_customer["address"] or "").strip()

        if not customer_name:
            flash("Customer name is required for retail invoice.", "error")
            return redirect(url_for("retail_sales"))

        if payment_status not in PAYMENT_STATUSES:
            payment_status = "PAID"

        fixed_price: float | None = None
        if one_price_raw:
            try:
                fixed_price = parse_money(one_price_raw, "One Price")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("retail_sales"))

        imeis = normalize_imei_text(imei_bulk)
        if not imeis:
            flash(f"At least one valid {tracking_label} is required.", "error")
            return redirect(url_for("retail_sales"))

        available_products: list[sqlite3.Row] = []
        not_found = 0
        not_in_stock = 0
        for imei in imeis:
            product = db.execute(
                """
                SELECT id, imei, brand, model, storage, color, status, retail_price, purchase_price, branch_id
                FROM products
                WHERE imei = ?
                """,
                (imei,),
            ).fetchone()
            if product is None:
                not_found += 1
                continue
            if product["status"] != "IN_STOCK":
                not_in_stock += 1
                continue
            available_products.append(product)

        if not available_products:
            flash(
                "No valid in-stock products found from your list. Please check IMEI/stock status.",
                "error",
            )
            return redirect(url_for("retail_sales"))

        tenant = get_current_tenant()
        blocked_by_limit, limit_message, _limit_info = check_tenant_plan_limit(
            db,
            tenant,
            "max_monthly_orders",
            incoming_count=len(available_products),
        )
        if blocked_by_limit:
            flash(limit_message, "error")
            return redirect(url_for("retail_sales"))
        if sale_branch_id is not None:
            branch = db.execute("SELECT id FROM branches WHERE id = ?", (sale_branch_id,)).fetchone()
            if branch is None:
                flash("Invalid branch selected.", "error")
                return redirect(url_for("retail_sales"))

        sold_prices = [
            float(fixed_price) if fixed_price is not None else float(item["retail_price"])
            for item in available_products
        ]
        subtotal = round(sum(sold_prices), 2)

        if paid_total_raw:
            try:
                paid_total = parse_money(paid_total_raw, "Paid Total")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("retail_sales"))
        else:
            paid_total = subtotal if payment_status == "PAID" else 0.0

        if paid_total > subtotal:
            flash("Paid total cannot be greater than subtotal.", "error")
            return redirect(url_for("retail_sales"))

        due_total = round(max(0.0, subtotal - paid_total), 2)
        final_status = "PAID" if due_total <= 0.00001 else "DUE"
        paid_allocations = split_paid_amounts(paid_total, sold_prices)

        if manual_invoice_no:
            duplicate = db.execute(
                "SELECT 1 FROM retail_invoices WHERE invoice_no = ?",
                (manual_invoice_no,),
            ).fetchone()
            if duplicate is not None:
                flash("This invoice number already exists. Use another invoice number.", "error")
                return redirect(url_for("retail_sales"))
            invoice_no = manual_invoice_no
        else:
            invoice_no = build_retail_invoice_no(db, sold_date)

        try:
            retail_customer_id = get_or_create_retail_customer(
                db,
                customer_name,
                customer_phone,
                customer_address,
                "",
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("retail_sales"))

        local_customer_id = get_or_create_local_retail_customer_id(db)
        created_by_user_id = parse_optional_int(str(session.get("tenant_user_id", "")))
        share_token = uuid.uuid4().hex

        try:
            for idx, product in enumerate(available_products):
                sold_price = round(sold_prices[idx], 2)
                paid_amount = round(paid_allocations[idx], 2)
                due_amount = round(max(0.0, sold_price - paid_amount), 2)
                item_payment_status = "PAID" if due_amount <= 0.00001 else "DUE"
                retail_note_parts = [
                    f"Retail Customer: {customer_name}",
                ]
                if customer_address:
                    retail_note_parts.append(f"Address: {customer_address}")
                if note:
                    retail_note_parts.append(f"Note: {note}")
                retail_note = " | ".join(retail_note_parts)

                db.execute(
                    """
                    INSERT INTO sales (
                        product_id, customer_id, retail_customer_id, sale_type, invoice_no,
                        sold_price, payment_status, paid_amount, due_amount, sold_at, branch_id,
                        receiver_name, receiver_phone, receiver_photo_path, note, is_active
                    )
                    VALUES (?, ?, ?, 'RETAIL', ?, ?, ?, ?, ?, ?, ?, ?, '', ?, 1)
                    """,
                    (
                        int(product["id"]),
                        local_customer_id,
                        retail_customer_id,
                        invoice_no,
                        sold_price,
                        item_payment_status,
                        paid_amount,
                        due_amount,
                        sold_date,
                        sale_branch_id or int(product["branch_id"] or 1),
                        customer_name,
                        customer_phone,
                        retail_note,
                    ),
                )
                sale_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                maybe_mark_low_margin_fraud(
                    db,
                    sale_id=sale_id,
                    product_id=int(product["id"]),
                    invoice_no=invoice_no,
                    sold_price=float(sold_price),
                    purchase_price=float(product["purchase_price"] or 0),
                )
                enqueue_integration_outbox(
                    db,
                    event_type="SALE_CREATED",
                    payload={
                        "sale_id": sale_id,
                        "product_id": int(product["id"]),
                        "customer_id": int(local_customer_id),
                        "retail_customer_id": int(retail_customer_id or 0),
                        "sale_type": "RETAIL",
                        "invoice_no": invoice_no,
                        "sold_price": float(sold_price),
                        "paid_amount": float(paid_amount),
                        "due_amount": float(due_amount),
                        "sold_at": sold_date,
                        "branch_id": int(sale_branch_id or int(product["branch_id"] or 1)),
                    },
                )
                db.execute("UPDATE products SET status = 'SOLD' WHERE id = ?", (int(product["id"]),))
                db.execute(
                    """
                    UPDATE inventory_reservations
                    SET status = 'CONSUMED',
                        updated_at = ?
                    WHERE product_id = ?
                      AND status = 'ACTIVE'
                    """,
                    (now_sqlite_text(), int(product["id"])),
                )

            db.execute(
                """
                INSERT INTO retail_invoices (
                    invoice_no, sold_at, retail_customer_id, customer_name, customer_phone, customer_address, customer_region,
                    subtotal, paid_amount, due_amount, payment_status, note, share_token, created_by_user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice_no,
                    sold_date,
                    retail_customer_id,
                    customer_name,
                    customer_phone,
                    customer_address,
                    "",
                    subtotal,
                    round(paid_total, 2),
                    due_total,
                    final_status,
                    note,
                    share_token,
                    created_by_user_id,
                ),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Retail invoice create failed. Please check stock/invoice number and try again.", "error")
            return redirect(url_for("retail_sales"))

        extra = []
        if not_found:
            extra.append(f"not found: {not_found}")
        if not_in_stock:
            extra.append(f"out of stock: {not_in_stock}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        flash(
            f"Retail invoice {invoice_no} created with {len(available_products)} item(s){suffix}.",
            "success",
        )
        return redirect(url_for("retail_invoice", invoice_no=invoice_no))

    preview_invoice_no = build_retail_invoice_no(db, today)
    in_stock_products = db.execute(
        """
        SELECT id, imei, brand, model, storage, color, retail_price
        FROM products
        WHERE status = 'IN_STOCK'
        ORDER BY id DESC
        LIMIT 800
        """
    ).fetchall()
    retail_customers = db.execute(
        """
        SELECT id, full_name, phone, address
        FROM retail_customers
        ORDER BY id DESC
        LIMIT 400
        """
    ).fetchall()
    recent_invoices = db.execute(
        """
        SELECT
            ri.invoice_no,
            ri.sold_at,
            ri.customer_name,
            ri.subtotal,
            ri.paid_amount,
            ri.due_amount,
            ri.payment_status
        FROM retail_invoices ri
        ORDER BY ri.id DESC
        LIMIT 30
        """
    ).fetchall()
    return render_template(
        "retail_sales.html",
        today=today,
        preview_invoice_no=preview_invoice_no,
        in_stock_products=in_stock_products,
        retail_customers=retail_customers,
        recent_invoices=recent_invoices,
    )


def load_retail_invoice_rows(conn: sqlite3.Connection, invoice_no: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    invoice = conn.execute(
        """
        SELECT *
        FROM retail_invoices
        WHERE invoice_no = ?
        """,
        (invoice_no,),
    ).fetchone()
    if invoice is None:
        return None, []

    items = conn.execute(
        """
        SELECT
            s.id, s.sold_at, s.sold_price, s.paid_amount, s.due_amount, s.payment_status,
            p.imei, p.brand, p.model, p.storage, p.color
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.invoice_no = ? AND s.sale_type = 'RETAIL'
        ORDER BY s.id ASC
        """,
        (invoice_no,),
    ).fetchall()
    return invoice, items


@app.get("/retail-invoice/<invoice_no>")
def retail_invoice(invoice_no: str):
    clean_invoice_no = (invoice_no or "").strip().upper()
    if not clean_invoice_no:
        flash("Retail invoice not found.", "error")
        return redirect(url_for("retail_sales"))

    db = get_db()
    invoice, items = load_retail_invoice_rows(db, clean_invoice_no)
    if invoice is None:
        flash("Retail invoice not found.", "error")
        return redirect(url_for("retail_sales"))

    tenant = get_current_tenant()
    shop_username = normalize_username(str(tenant["username"])) if tenant is not None else "shop"
    share_url = url_for(
        "retail_invoice_public",
        shop_username=shop_username,
        token=str(invoice["share_token"]),
        _external=True,
    )
    qr_url = "https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=" + urllib.parse.quote(
        share_url,
        safe="",
    )
    return render_template(
        "retail_invoice.html",
        invoice=invoice,
        items=items,
        share_url=share_url,
        qr_url=qr_url,
    )


@app.get("/retail-invoice/<invoice_no>/download")
def retail_invoice_download(invoice_no: str):
    clean_invoice_no = (invoice_no or "").strip().upper()
    if not clean_invoice_no:
        return redirect(url_for("retail_sales"))

    db = get_db()
    invoice, items = load_retail_invoice_rows(db, clean_invoice_no)
    if invoice is None:
        flash("Retail invoice not found.", "error")
        return redirect(url_for("retail_sales"))

    tenant = get_current_tenant()
    shop_username = normalize_username(str(tenant["username"])) if tenant is not None else "shop"
    share_url = url_for(
        "retail_invoice_public",
        shop_username=shop_username,
        token=str(invoice["share_token"]),
        _external=True,
    )
    html = render_template(
        "retail_invoice_public.html",
        invoice=invoice,
        items=items,
        share_url=share_url,
        download_mode=True,
    )
    response = make_response(html)
    safe_invoice_no = re.sub(r"[^A-Za-z0-9_-]", "-", clean_invoice_no)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{safe_invoice_no}.html"'
    return response


@app.get("/invoice/<shop_username>/<token>")
def retail_invoice_public(shop_username: str, token: str):
    shop_id = normalize_username(shop_username)
    token_value = re.sub(r"[^a-fA-F0-9]", "", (token or "").strip()).lower()
    if not shop_id or not token_value:
        return "Invoice link invalid.", 404

    admin_db = get_admin_db()
    account = admin_db.execute(
        """
        SELECT id, db_path, shop_name, username, is_active
        FROM tenant_accounts
        WHERE username = ?
        """,
        (shop_id,),
    ).fetchone()
    if account is None or int(account["is_active"]) != 1:
        return "Shop not found.", 404

    tenant_db_path = resolve_tenant_db_path_for_account(admin_db, account)
    if not tenant_db_path.exists():
        return "Shop database not found.", 404

    with sqlite3.connect(tenant_db_path) as conn:
        conn.row_factory = sqlite3.Row
        invoice = conn.execute(
            """
            SELECT *
            FROM retail_invoices
            WHERE share_token = ?
            LIMIT 1
            """,
            (token_value,),
        ).fetchone()
        if invoice is None:
            return "Invoice not found.", 404
        items = conn.execute(
            """
            SELECT
                s.id, s.sold_at, s.sold_price, s.paid_amount, s.due_amount, s.payment_status,
                p.imei, p.brand, p.model, p.storage, p.color
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE s.invoice_no = ? AND s.sale_type = 'RETAIL'
            ORDER BY s.id ASC
            """,
            (invoice["invoice_no"],),
        ).fetchall()

    share_url = url_for(
        "retail_invoice_public",
        shop_username=shop_id,
        token=token_value,
        _external=True,
    )
    html = render_template(
        "retail_invoice_public.html",
        invoice=invoice,
        items=items,
        share_url=share_url,
        public_shop_name=str(account["shop_name"] or ""),
        download_mode=request.args.get("download", "0") == "1",
    )
    if request.args.get("download", "0") == "1":
        response = make_response(html)
        safe_invoice_no = re.sub(r"[^A-Za-z0-9_-]", "-", str(invoice["invoice_no"]))
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="{safe_invoice_no}.html"'
        return response
    return html


@app.post("/sales/<int:sale_id>/collect")
def sale_collect_due(sale_id: int):
    db = get_db()
    amount_raw = request.form.get("amount", "").strip()
    collected_at = normalize_date(request.form.get("collected_at", "").strip())
    method = request.form.get("method", "CASH").strip().upper() or "CASH"
    note = request.form.get("note", "").strip()
    redirect_to = request.form.get("redirect_to", "sales").strip().lower()
    sales_view = normalize_text_field(request.form.get("sview", "")).lower()

    def redirect_sales_after_collect():
        if redirect_to == "customer_detail":
            return redirect(request.referrer or url_for("sales"))
        if sales_view in {"entry", "return", "due", "history"}:
            return redirect(url_for("sales", sview=sales_view))
        return redirect(url_for("sales"))

    try:
        amount = parse_money(amount_raw, "Collection Amount")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect_sales_after_collect()

    sale = db.execute(
        """
        SELECT id, customer_id, sold_price, paid_amount, due_amount, is_active
        FROM sales
        WHERE id = ?
        """,
        (sale_id,),
    ).fetchone()
    if sale is None:
        flash("Sale not found.", "error")
        return redirect_sales_after_collect()

    if int(sale["is_active"]) != 1:
        flash("Cannot collect from returned/canceled sale.", "error")
        return redirect_sales_after_collect()

    current_due = float(sale["due_amount"] or 0)
    if current_due <= 0:
        flash("This sale already has no due.", "success")
        return redirect_sales_after_collect()

    if amount > current_due:
        flash(f"Collection amount বেশি। সর্বোচ্চ due: {current_due:,.2f}", "error")
        return redirect_sales_after_collect()

    new_paid = float(sale["paid_amount"] or 0) + amount
    new_due = max(0.0, float(sale["sold_price"]) - new_paid)
    new_status = "PAID" if new_due <= 0.00001 else "DUE"

    db.execute(
        """
        UPDATE sales
        SET paid_amount = ?, due_amount = ?, payment_status = ?
        WHERE id = ?
        """,
        (new_paid, new_due, new_status, sale_id),
    )
    db.execute(
        """
        INSERT INTO due_collections (sale_id, customer_id, amount, collected_at, method, note)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (sale_id, int(sale["customer_id"]), amount, collected_at, method, note),
    )
    db.commit()

    flash(f"Due collection saved. Remaining due: {new_due:,.2f}", "success")
    if redirect_to == "customer_detail":
        customer_id = int(sale["customer_id"])
        return redirect(url_for("customer_detail", customer_id=customer_id))
    if sales_view in {"entry", "return", "due", "history"}:
        return redirect(url_for("sales", sview=sales_view))
    return redirect(url_for("sales"))


@app.post("/sales/quick-return")
def sales_quick_return():
    db = get_db()
    tracking_mode = get_current_tracking_mode()
    tracking_label = get_tracking_label()
    imei = normalize_tracking_code(request.form.get("imei", "").strip(), tracking_mode)
    reason = request.form.get("reason", "").strip() or "Customer return"
    return_date = normalize_date(request.form.get("return_date", "").strip())
    restock = request.form.get("restock", "1") == "1"
    sales_view = normalize_text_field(request.form.get("sview", "")).lower()

    if not is_valid_imei(imei):
        flash(f"Invalid {tracking_label}.", "error")
        if sales_view in {"entry", "return", "due", "history"}:
            return redirect(url_for("sales", sview=sales_view))
        return redirect(url_for("sales"))

    sale_row = db.execute(
        """
        SELECT s.id
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE p.imei = ? AND s.is_active = 1
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (imei,),
    ).fetchone()

    if sale_row is None:
        flash(
            f"No active sale found for {tracking_label} {imei}. Use Returns page with Force Stock-In if item is physically returned.",
            "error",
        )
        if sales_view in {"entry", "return", "due", "history"}:
            return redirect(url_for("sales", sview=sales_view))
        return redirect(url_for("sales"))

    try:
        process_sale_return(
            db,
            sale_id=int(sale_row["id"]),
            return_date=return_date,
            reason=reason,
            restock=restock,
        )
        flash(f"{tracking_label} {imei} returned and stock updated.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    if sales_view in {"entry", "return", "due", "history"}:
        return redirect(url_for("sales", sview=sales_view))
    return redirect(url_for("sales"))


@app.route("/returns", methods=["GET", "POST"])
def returns():
    db = get_db()

    if request.method == "POST":
        tracking_mode = get_current_tracking_mode()
        tracking_label = get_tracking_label()
        imei = normalize_tracking_code(request.form.get("imei", "").strip(), tracking_mode)
        reason = request.form.get("reason", "").strip() or "Customer return"
        return_date = normalize_date(request.form.get("return_date", "").strip())
        restock = request.form.get("restock", "1") == "1"
        force_stock_in = request.form.get("force_stock_in", "0") == "1"

        if not is_valid_imei(imei):
            if tracking_mode == TRACKING_MODE_IMEI:
                flash(f"{tracking_label} must be exactly 15 digits.", "error")
            else:
                flash(f"{tracking_label} format is invalid.", "error")
            return redirect(url_for("returns"))

        sale_row = db.execute(
            """
            SELECT s.id
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE p.imei = ? AND s.is_active = 1
            ORDER BY s.id DESC
            LIMIT 1
            """,
            (imei,),
        ).fetchone()

        if sale_row is None:
            product_row = db.execute(
                """
                SELECT id, status
                FROM products
                WHERE imei = ?
                """,
                (imei,),
            ).fetchone()
            if product_row is None:
                flash(f"No active sale found and {tracking_label} is not in inventory.", "error")
                return redirect(url_for("returns"))

            if force_stock_in and restock:
                if str(product_row["status"] or "").upper() == "IN_STOCK":
                    flash(f"{tracking_label} is already in stock.", "success")
                    return redirect(url_for("returns"))

                db.execute(
                    "UPDATE products SET status = 'IN_STOCK' WHERE id = ?",
                    (int(product_row["id"]),),
                )
                db.execute(
                    """
                    INSERT INTO stock_adjustments (product_id, action, event_date, reason)
                    VALUES (?, 'FORCE_STOCK_IN', ?, ?)
                    """,
                    (int(product_row["id"]), return_date, reason),
                )
                db.commit()
                flash(
                    f"{tracking_label} {imei} stock-in completed (without active sale).",
                    "success",
                )
                return redirect(url_for("returns"))

            flash(
                f"No active sale found for this {tracking_label}. If this item is physically returned, enable Force Stock-In.",
                "error",
            )
            return redirect(url_for("returns"))

        try:
            process_sale_return(
                db,
                sale_id=int(sale_row["id"]),
                return_date=return_date,
                reason=reason,
                restock=restock,
            )
            flash(f"{tracking_label} {imei} returned and wholesale canceled.", "success")
        except ValueError as exc:
            flash(str(exc), "error")

        return redirect(url_for("returns"))

    return_list = db.execute(
        """
        SELECT
            r.*,
            p.imei, p.brand, p.model,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS shop_name,
            s.sold_at
        FROM sale_returns r
        JOIN sales s ON s.id = r.sale_id
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        ORDER BY r.id DESC
        LIMIT 300
        """
    ).fetchall()

    return render_template("returns.html", returns=return_list, today=date.today().isoformat())


@app.route("/customers", methods=["GET", "POST"])
def customers():
    db = get_db()

    if request.method == "POST":
        shop_name = request.form.get("shop_name", "").strip()
        owner_name = request.form.get("owner_name", "").strip()
        phone = request.form.get("phone", "").strip()
        area = request.form.get("area", "").strip()
        address = request.form.get("address", "").strip()
        note = request.form.get("note", "").strip()

        if not shop_name:
            flash("Shop name is required.", "error")
            return redirect(url_for("customers"))
        if shop_name == LOCAL_RETAIL_WHOLESALE_SHOP_NAME:
            flash("This shop name is reserved for internal retail ledger.", "error")
            return redirect(url_for("customers"))

        try:
            db.execute(
                """
                INSERT INTO customers (shop_name, owner_name, phone, area, address, note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (shop_name, owner_name, phone, area, address, note),
            )
            db.commit()
            flash("Wholesale shop added successfully.", "success")
        except sqlite3.IntegrityError:
            db.rollback()
            flash("This shop name already exists.", "error")

        return redirect(url_for("customers"))

    customer_list = db.execute(
        """
        SELECT
            c.*,
            COALESCE(SUM(CASE WHEN s.is_active = 1 AND s.sale_type = 'WHOLESALE' THEN 1 ELSE 0 END), 0) AS sold_units,
            COALESCE(SUM(CASE WHEN s.is_active = 1 AND s.sale_type = 'WHOLESALE' THEN s.sold_price ELSE 0 END), 0) AS sales_amount,
            COALESCE(SUM(CASE WHEN s.is_active = 1 AND s.sale_type = 'WHOLESALE' THEN s.sold_price - p.purchase_price ELSE 0 END), 0) AS profit_amount,
            COALESCE(SUM(CASE WHEN s.is_active = 1 AND s.sale_type = 'WHOLESALE' THEN s.due_amount ELSE 0 END), 0) AS due_amount
        FROM customers c
        LEFT JOIN sales s ON s.customer_id = c.id
        LEFT JOIN products p ON p.id = s.product_id
        WHERE c.shop_name <> ?
        GROUP BY c.id
        ORDER BY c.id DESC
        """,
        (LOCAL_RETAIL_WHOLESALE_SHOP_NAME,),
    ).fetchall()

    return render_template("customers.html", customers=customer_list, today=date.today().isoformat())


@app.route("/retail-customers", methods=["GET", "POST"])
def retail_customers():
    db = get_db()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        note = request.form.get("note", "").strip()

        if not full_name:
            flash("Retail customer name is required.", "error")
            return redirect(url_for("retail_customers"))

        try:
            retail_customer_id = get_or_create_retail_customer(
                db,
                full_name=full_name,
                phone=phone,
                address=address,
                region="",
                note=note,
            )
            db.commit()
            flash(f"Retail customer saved (ID: {retail_customer_id}).", "success")
        except ValueError as exc:
            db.rollback()
            flash(str(exc), "error")

        return redirect(url_for("retail_customers"))

    retail_customer_list = db.execute(
        """
        SELECT
            rc.*,
            COALESCE(COUNT(ri.id), 0) AS invoice_count,
            COALESCE(SUM(ri.subtotal), 0) AS sales_amount,
            COALESCE(SUM(ri.paid_amount), 0) AS paid_amount,
            COALESCE(SUM(ri.due_amount), 0) AS due_amount,
            MAX(ri.sold_at) AS last_sale_date
        FROM retail_customers rc
        LEFT JOIN retail_invoices ri ON ri.retail_customer_id = rc.id
        GROUP BY rc.id
        ORDER BY rc.id DESC
        """
    ).fetchall()

    return render_template(
        "retail_customers.html",
        retail_customers=retail_customer_list,
        today=date.today().isoformat(),
    )


@app.post("/customers/<int:customer_id>/quick-sale")
def customer_quick_sale(customer_id: int):
    db = get_db()

    customer = db.execute("SELECT id, shop_name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if customer is None:
        flash("Wholesale shop not found.", "error")
        return redirect(url_for("customers"))
    if str(customer["shop_name"] or "") == LOCAL_RETAIL_WHOLESALE_SHOP_NAME:
        flash("Internal retail ledger cannot be used here.", "error")
        return redirect(url_for("customers"))

    sale_type = request.form.get("sale_type", "WHOLESALE").strip().upper()
    payment_status = request.form.get("payment_status", "PAID").strip().upper()
    sold_date = normalize_date(request.form.get("sold_at", "").strip())
    note = request.form.get("note", "").strip()
    invoice_no = request.form.get("invoice_no", "").strip().upper()
    one_price_raw = request.form.get("sold_price", "").strip()
    paid_amount_raw = request.form.get("paid_amount", "").strip()
    receiver_name = request.form.get("receiver_name", "").strip()
    receiver_phone = request.form.get("receiver_phone", "").strip()
    receiver_photo = request.files.get("receiver_photo")
    imei_bulk = request.form.get("imei_bulk", "")

    if sale_type not in SALE_TYPES:
        sale_type = "WHOLESALE"
    if sale_type != "WHOLESALE":
        sale_type = "WHOLESALE"
    if payment_status not in PAYMENT_STATUSES:
        payment_status = "PAID"
    if not invoice_no:
        invoice_no = build_sale_invoice_no(db, sold_date, sale_type)

    fixed_price: float | None = None
    if one_price_raw:
        try:
            fixed_price = parse_money(one_price_raw, "Sold Price")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("customer_detail", customer_id=customer_id))

    fixed_paid: float | None = None
    if paid_amount_raw:
        try:
            fixed_paid = parse_money(paid_amount_raw, "Paid Amount")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("customer_detail", customer_id=customer_id))

    receiver_photo_path = ""
    if receiver_photo is not None and (receiver_photo.filename or "").strip():
        try:
            receiver_photo_path = save_receiver_photo(receiver_photo, prefix="quick-sale")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("customer_detail", customer_id=customer_id))

    imeis = normalize_imei_text(imei_bulk)
    if not imeis:
        flash(f"At least one {get_tracking_label()} is required.", "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    tenant = get_current_tenant()
    blocked_by_limit, limit_message, _limit_info = check_tenant_plan_limit(
        db,
        tenant,
        "max_monthly_orders",
        incoming_count=len(imeis),
    )
    if blocked_by_limit:
        flash(limit_message, "error")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    inserted = 0
    not_found = 0
    out_of_stock = 0
    duplicate = 0

    for imei in imeis:
        if not is_valid_imei(imei):
            continue

        product = db.execute(
            """
            SELECT id, imei, status, wholesale_price, retail_price, purchase_price, branch_id
            FROM products
            WHERE imei = ?
            """,
            (imei,),
        ).fetchone()
        if product is None:
            not_found += 1
            continue
        if product["status"] != "IN_STOCK":
            out_of_stock += 1
            continue

        sold_price = (
            fixed_price
            if fixed_price is not None
            else (float(product["wholesale_price"]) if sale_type == "WHOLESALE" else float(product["retail_price"]))
        )
        paid_amount = (
            min(fixed_paid, sold_price)
            if fixed_paid is not None
            else (sold_price if payment_status == "PAID" else 0.0)
        )
        due_amount = max(0.0, sold_price - paid_amount)
        final_status = "PAID" if due_amount <= 0.00001 else "DUE"

        try:
            db.execute(
                """
                INSERT INTO sales (
                    product_id, customer_id, retail_customer_id, sale_type, invoice_no,
                    sold_price, payment_status, paid_amount, due_amount, sold_at, branch_id,
                    receiver_name, receiver_phone, receiver_photo_path, note, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    product["id"],
                    customer_id,
                    None,
                    sale_type,
                    invoice_no,
                    sold_price,
                    final_status,
                    paid_amount,
                    due_amount,
                    sold_date,
                    int(product["branch_id"] or 1),
                    receiver_name,
                    receiver_phone,
                    receiver_photo_path,
                    note,
                ),
            )
            db.execute("UPDATE products SET status = 'SOLD' WHERE id = ?", (product["id"],))
            db.execute(
                """
                UPDATE inventory_reservations
                SET status = 'CONSUMED',
                    updated_at = ?
                WHERE product_id = ?
                  AND status = 'ACTIVE'
                """,
                (now_sqlite_text(), int(product["id"])),
            )
            sale_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            maybe_mark_low_margin_fraud(
                db,
                sale_id=sale_id,
                product_id=int(product["id"]),
                invoice_no=invoice_no,
                sold_price=float(sold_price),
                purchase_price=float(product["purchase_price"] or 0),
            )
            enqueue_integration_outbox(
                db,
                event_type="SALE_CREATED",
                payload={
                    "sale_id": sale_id,
                    "product_id": int(product["id"]),
                    "customer_id": int(customer_id),
                    "retail_customer_id": 0,
                    "sale_type": sale_type,
                    "invoice_no": invoice_no,
                    "sold_price": float(sold_price),
                    "paid_amount": float(paid_amount),
                    "due_amount": float(due_amount),
                    "sold_at": sold_date,
                    "branch_id": int(product["branch_id"] or 1),
                },
            )
            inserted += 1
        except sqlite3.IntegrityError:
            duplicate += 1

    db.commit()
    flash(
        f"Quick sale done: sold {inserted}, not found {not_found}, out of stock {out_of_stock}, duplicate {duplicate}.",
        "success" if inserted else "error",
    )
    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.route("/customers/<int:customer_id>")
def customer_detail(customer_id: int):
    db = get_db()
    customer = db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if customer is None:
        flash("Wholesale shop not found.", "error")
        return redirect(url_for("customers"))
    if str(customer["shop_name"] or "") == LOCAL_RETAIL_WHOLESALE_SHOP_NAME:
        flash("This ledger is internal and not shown here.", "error")
        return redirect(url_for("customers"))

    today = date.today().isoformat()
    month_prefix = today[:7]
    year_prefix = today[:4]

    def customer_profit(condition_sql: str, params: tuple = ()) -> sqlite3.Row:
        return db.execute(
            f"""
            SELECT
                COUNT(*) AS units,
                COALESCE(SUM(s.sold_price), 0) AS revenue,
                COALESCE(SUM(s.sold_price - p.purchase_price), 0) AS profit,
                COALESCE(SUM(s.due_amount), 0) AS due
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE s.customer_id = ? AND s.sale_type = 'WHOLESALE' AND s.is_active = 1 AND ({condition_sql})
            """,
            (customer_id, *params),
        ).fetchone()

    period_stats = {
        "today": customer_profit("s.sold_at = ?", (today,)),
        "month": customer_profit("substr(s.sold_at, 1, 7) = ?", (month_prefix,)),
        "year": customer_profit("substr(s.sold_at, 1, 4) = ?", (year_prefix,)),
        "lifetime": customer_profit("1 = 1"),
    }

    sale_history = db.execute(
        """
        SELECT
            s.*,
            p.imei, p.brand, p.model, p.purchase_price,
            (s.sold_price - p.purchase_price) AS profit,
            r.return_date,
            r.reason AS return_reason
        FROM sales s
        JOIN products p ON p.id = s.product_id
        LEFT JOIN sale_returns r ON r.sale_id = s.id
        WHERE s.customer_id = ? AND s.sale_type = 'WHOLESALE'
        ORDER BY s.id DESC
        LIMIT 500
        """,
        (customer_id,),
    ).fetchall()

    due_sales = db.execute(
        """
        SELECT
            s.id, s.sold_at, s.invoice_no, s.sale_type,
            s.sold_price, s.paid_amount, s.due_amount, s.payment_status,
            p.imei, p.brand, p.model
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.customer_id = ? AND s.sale_type = 'WHOLESALE' AND s.is_active = 1 AND s.due_amount > 0
        ORDER BY s.sold_at ASC, s.id ASC
        """,
        (customer_id,),
    ).fetchall()

    collection_history = db.execute(
        """
        SELECT
            dc.*,
            p.imei
        FROM due_collections dc
        JOIN sales s ON s.id = dc.sale_id
        LEFT JOIN products p ON p.id = s.product_id
        WHERE dc.customer_id = ?
          AND s.sale_type = 'WHOLESALE'
        ORDER BY dc.id DESC
        LIMIT 200
        """,
        (customer_id,),
    ).fetchall()

    outstanding_due = db.execute(
        """
        SELECT COALESCE(SUM(s.due_amount), 0) AS amount
        FROM sales s
        WHERE s.customer_id = ? AND s.sale_type = 'WHOLESALE' AND s.is_active = 1
        """,
        (customer_id,),
    ).fetchone()

    return render_template(
        "customer_detail.html",
        customer=customer,
        period_stats=period_stats,
        sales=sale_history,
        due_sales=due_sales,
        collection_history=collection_history,
        outstanding_due=float(outstanding_due["amount"] if outstanding_due is not None else 0),
        today=today,
    )


@app.get("/due-list")
def due_list():
    db = get_db()
    due_view = normalize_text_field(request.args.get("dview", "")).lower()
    if due_view not in {"due", "collected"}:
        due_view = "due"
    search_query = normalize_text_field(request.args.get("q", ""))
    search_key = search_query.casefold()

    total_due = float(
        db.execute(
            """
            SELECT COALESCE(SUM(due_amount), 0) AS amount
            FROM sales
            WHERE is_active = 1
              AND COALESCE(due_amount, 0) > 0
            """
        ).fetchone()["amount"]
        or 0
    )
    total_collected = float(
        db.execute("SELECT COALESCE(SUM(amount), 0) AS amount FROM due_collections").fetchone()["amount"] or 0
    )

    due_rows_raw = db.execute(
        """
        SELECT
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE COALESCE(NULLIF(TRIM(c.shop_name), ''), 'Wholesale Shop')
            END AS party_name,
            CASE
                WHEN s.sale_type = 'RETAIL' THEN 'RETAIL'
                ELSE 'WHOLESALE'
            END AS party_type,
            CASE
                WHEN s.sale_type = 'RETAIL' THEN COALESCE(rc.id, 0)
                ELSE COALESCE(c.id, 0)
            END AS party_id,
            CASE
                WHEN s.sale_type = 'RETAIL' THEN COALESCE(NULLIF(TRIM(rc.phone), ''), '')
                ELSE COALESCE(NULLIF(TRIM(c.phone), ''), '')
            END AS phone,
            COUNT(*) AS due_invoice_count,
            COALESCE(SUM(s.due_amount), 0) AS due_amount,
            MAX(s.sold_at) AS last_due_date
        FROM sales s
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        WHERE s.is_active = 1
          AND COALESCE(s.due_amount, 0) > 0
        GROUP BY party_name, party_type, party_id, phone
        ORDER BY due_amount DESC, party_name ASC
        """
    ).fetchall()

    collected_rows_raw = db.execute(
        """
        SELECT
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE COALESCE(NULLIF(TRIM(c.shop_name), ''), 'Wholesale Shop')
            END AS party_name,
            CASE
                WHEN s.sale_type = 'RETAIL' THEN 'RETAIL'
                ELSE 'WHOLESALE'
            END AS party_type,
            CASE
                WHEN s.sale_type = 'RETAIL' THEN COALESCE(rc.id, 0)
                ELSE COALESCE(c.id, 0)
            END AS party_id,
            CASE
                WHEN s.sale_type = 'RETAIL' THEN COALESCE(NULLIF(TRIM(rc.phone), ''), '')
                ELSE COALESCE(NULLIF(TRIM(c.phone), ''), '')
            END AS phone,
            COUNT(dc.id) AS collection_count,
            COALESCE(SUM(dc.amount), 0) AS collected_amount,
            MAX(dc.collected_at) AS last_collected_at
        FROM due_collections dc
        JOIN sales s ON s.id = dc.sale_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        GROUP BY party_name, party_type, party_id, phone
        ORDER BY collected_amount DESC, party_name ASC
        """
    ).fetchall()

    def build_initials(name: str) -> str:
        parts = [item for item in re.split(r"[\s\-_./]+", (name or "").strip()) if item]
        if not parts:
            return "SX"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return f"{parts[0][:1]}{parts[1][:1]}".upper()

    def build_party_meta(row: sqlite3.Row) -> dict[str, object]:
        party_name = str(row["party_name"] or "").strip() or "Unknown Party"
        party_type = str(row["party_type"] or "WHOLESALE").upper()
        party_id = int(row["party_id"] or 0)
        phone = str(row["phone"] or "").strip()
        if party_type == "RETAIL":
            label_en = "Retail Customer"
            label_bn = "রিটেইল কাস্টমার"
            detail_url = url_for("sales", sview="due")
            collect_url = url_for("sales", sview="due")
            tone = "retail"
        else:
            label_en = "Wholesale Shop"
            label_bn = "হোলসেল শপ"
            detail_url = url_for("customer_detail", customer_id=party_id) if party_id > 0 else url_for("customers")
            collect_url = (
                f"{url_for('customer_detail', customer_id=party_id)}#dueCollectionCard"
                if party_id > 0
                else url_for("customers")
            )
            tone = "wholesale"
        return {
            "party_name": party_name,
            "party_type": party_type,
            "party_id": party_id,
            "phone": phone,
            "initials": build_initials(party_name),
            "party_label_en": label_en,
            "party_label_bn": label_bn,
            "detail_url": detail_url,
            "collect_url": collect_url,
            "tone": tone,
        }

    due_rows: list[dict[str, object]] = []
    for row in due_rows_raw:
        item = build_party_meta(row)
        item.update(
            {
                "due_invoice_count": int(row["due_invoice_count"] or 0),
                "due_amount": float(row["due_amount"] or 0),
                "last_due_date": str(row["last_due_date"] or ""),
            }
        )
        if search_key and search_key not in str(item["party_name"]).casefold() and search_key not in str(
            item["phone"] or ""
        ).casefold():
            continue
        due_rows.append(item)

    collected_rows: list[dict[str, object]] = []
    for row in collected_rows_raw:
        item = build_party_meta(row)
        item.update(
            {
                "collection_count": int(row["collection_count"] or 0),
                "collected_amount": float(row["collected_amount"] or 0),
                "last_collected_at": str(row["last_collected_at"] or ""),
            }
        )
        if search_key and search_key not in str(item["party_name"]).casefold() and search_key not in str(
            item["phone"] or ""
        ).casefold():
            continue
        collected_rows.append(item)

    return render_template(
        "due_list.html",
        due_view=due_view,
        search_query=search_query,
        total_due=total_due,
        total_collected=total_collected,
        due_rows=due_rows,
        collected_rows=collected_rows,
        due_party_count=len(due_rows),
        collected_party_count=len(collected_rows),
    )


@app.get("/customers/<int:customer_id>/day-invoice")
def customer_day_invoice(customer_id: int):
    db = get_db()
    sale_date = normalize_date(request.args.get("sale_date", "").strip())
    customer = db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if customer is None:
        flash("Wholesale shop not found.", "error")
        return redirect(url_for("customers"))

    sales_rows = db.execute(
        """
        SELECT
            s.id, s.invoice_no, s.sold_at, s.sale_type, s.sold_price,
            s.paid_amount, s.due_amount, s.payment_status,
            s.receiver_name, s.receiver_phone, s.receiver_photo_path,
            p.imei, p.brand, p.model
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.customer_id = ? AND s.sale_type = 'WHOLESALE' AND s.sold_at = ? AND s.is_active = 1
        ORDER BY s.id ASC
        """,
        (customer_id, sale_date),
    ).fetchall()

    returned_today = db.execute(
        """
        SELECT
            p.imei, p.brand, p.model, r.return_date, r.reason
        FROM sale_returns r
        JOIN sales s ON s.id = r.sale_id
        JOIN products p ON p.id = s.product_id
        WHERE s.customer_id = ? AND s.sale_type = 'WHOLESALE' AND r.return_date = ?
        ORDER BY r.id ASC
        """,
        (customer_id, sale_date),
    ).fetchall()

    collected_today = db.execute(
        """
        SELECT
            dc.collected_at, dc.amount, dc.method, dc.note, p.imei
        FROM due_collections dc
        JOIN sales s ON s.id = dc.sale_id
        LEFT JOIN products p ON p.id = s.product_id
        WHERE dc.customer_id = ? AND dc.collected_at = ?
          AND s.sale_type = 'WHOLESALE'
        ORDER BY dc.id ASC
        """,
        (customer_id, sale_date),
    ).fetchall()

    totals = {
        "units": len(sales_rows),
        "sold_amount": sum(float(row["sold_price"] or 0) for row in sales_rows),
        "paid_amount": sum(float(row["paid_amount"] or 0) for row in sales_rows),
        "due_amount": sum(float(row["due_amount"] or 0) for row in sales_rows),
        "return_units": len(returned_today),
        "collected_due": sum(float(row["amount"] or 0) for row in collected_today),
    }

    return render_template(
        "customer_day_invoice.html",
        customer=customer,
        sale_date=sale_date,
        sales_rows=sales_rows,
        returned_today=returned_today,
        collected_today=collected_today,
        totals=totals,
    )


@app.post("/suppliers/return-stock")
def supplier_return_stock():
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can return stock back to supplier.", "error")
        return redirect(url_for("suppliers"))

    db = get_db()
    ensure_enterprise_tables(db)

    supplier_id = parse_optional_int(request.form.get("supplier_id", ""))
    tracking_mode = get_current_tracking_mode()
    tracking_label = get_tracking_label()
    tracking_code = normalize_tracking_code(request.form.get("imei", "").strip(), tracking_mode)
    return_date = normalize_date(request.form.get("return_date", "").strip())
    reason = normalize_text_field(request.form.get("reason", "")) or "Supplier return"
    note = normalize_text_field(request.form.get("note", ""))
    confirm_ack = request.form.get("confirm_ack") == "1"

    redirect_url = url_for(
        "suppliers",
        supplier_id=(supplier_id or ""),
        imei=tracking_code,
    )

    if supplier_id is None:
        flash("Supplier নির্বাচন করুন।", "error")
        return redirect(redirect_url)
    if not tracking_code or not is_valid_imei(tracking_code):
        flash(f"Valid {tracking_label} is required for supplier return.", "error")
        return redirect(redirect_url)
    if not confirm_ack:
        flash("Confirm the physical handover before returning stock to supplier.", "error")
        return redirect(redirect_url)

    supplier = db.execute(
        "SELECT id, name FROM suppliers WHERE id = ?",
        (supplier_id,),
    ).fetchone()
    if supplier is None:
        flash("Selected supplier not found.", "error")
        return redirect(url_for("suppliers"))

    product = db.execute(
        """
        SELECT
            id, imei, brand, model,
            COALESCE(storage, '') AS storage,
            COALESCE(color, '') AS color,
            COALESCE(category, '') AS category,
            COALESCE(warranty_type, '') AS warranty_type,
            purchase_price, wholesale_price, retail_price,
            COALESCE(received_date, '') AS received_date,
            supplier_id,
            status
        FROM products
        WHERE imei = ?
        """,
        (tracking_code,),
    ).fetchone()
    if product is None:
        flash(f"No product found for {tracking_label} {tracking_code}.", "error")
        return redirect(redirect_url)
    if int(product["supplier_id"] or 0) != int(supplier_id):
        flash("This item is not linked to the selected supplier.", "error")
        return redirect(redirect_url)
    if str(product["status"] or "").upper() != "IN_STOCK":
        flash("Only in-stock products can be returned to supplier.", "error")
        return redirect(redirect_url)

    sale_history = int(query_scalar("SELECT COUNT(*) FROM sales WHERE product_id = ?", (int(product["id"]),)))
    stock_adjustments = int(
        query_scalar("SELECT COUNT(*) FROM stock_adjustments WHERE product_id = ?", (int(product["id"]),))
    )
    active_reservations = int(
        query_scalar(
            """
            SELECT COUNT(*)
            FROM inventory_reservations
            WHERE product_id = ?
              AND status = 'ACTIVE'
            """,
            (int(product["id"]),),
        )
    )

    if sale_history > 0:
        flash(
            "This item already has sale/return history. Keep it in inventory and use edit/history tools instead.",
            "error",
        )
        return redirect(redirect_url)
    if stock_adjustments > 0 or active_reservations > 0:
        flash(
            "This item has stock adjustment or active reservation. Clear that first, then return to supplier.",
            "error",
        )
        return redirect(redirect_url)

    db.execute(
        """
        INSERT INTO supplier_returns (
            supplier_id, source_product_id, imei, brand, model, storage, color, category,
            warranty_type, purchase_price, wholesale_price, retail_price, received_date,
            returned_at, reason, note, created_by_user_id, created_by_username
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(supplier_id),
            int(product["id"]),
            str(product["imei"] or ""),
            str(product["brand"] or ""),
            str(product["model"] or ""),
            str(product["storage"] or ""),
            str(product["color"] or ""),
            str(product["category"] or ""),
            str(product["warranty_type"] or ""),
            float(product["purchase_price"] or 0),
            float(product["wholesale_price"] or 0),
            float(product["retail_price"] or 0),
            str(product["received_date"] or ""),
            return_date,
            reason,
            note,
            parse_optional_int(str(current_user["id"])),
            str(current_user["username"] or ""),
        ),
    )
    db.execute("DELETE FROM products WHERE id = ?", (int(product["id"]),))
    db.commit()
    write_audit_log(
        action="SUPPLIER_RETURN_CREATED",
        metadata={
            "supplier_id": int(supplier_id),
            "supplier_name": str(supplier["name"] or ""),
            "product_id": int(product["id"]),
            "tracking_code": str(product["imei"] or ""),
            "brand": str(product["brand"] or ""),
            "model": str(product["model"] or ""),
            "returned_at": return_date,
            "reason": reason,
        },
    )
    flash(
        f"{tracking_label} {tracking_code} returned to supplier {supplier['name']} and removed from stock.",
        "success",
    )
    return redirect(url_for("suppliers", supplier_id=supplier_id))


@app.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    db = get_db()
    ensure_enterprise_tables(db)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        contact_person = request.form.get("contact_person", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        note = request.form.get("note", "").strip()

        if not name:
            flash("Supplier name is required.", "error")
            return redirect(url_for("suppliers"))

        try:
            db.execute(
                """
                INSERT INTO suppliers (name, contact_person, phone, address, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, contact_person, phone, address, note),
            )
            db.commit()
            flash("Supplier added successfully.", "success")
        except sqlite3.IntegrityError:
            db.rollback()
            flash("This supplier already exists.", "error")

        return redirect(url_for("suppliers"))

    supplier_prefill_id = parse_optional_int(request.args.get("supplier_id", ""))
    imei_prefill = normalize_tracking_code(
        request.args.get("imei", "").strip(),
        get_current_tracking_mode(),
    )

    supplier_list = db.execute(
        """
        SELECT
            s.*,
            COALESCE(COUNT(p.id), 0) AS bought_units,
            COALESCE(SUM(p.purchase_price), 0) AS purchase_total,
            COALESCE(SUM(CASE WHEN p.status = 'IN_STOCK' THEN 1 ELSE 0 END), 0) AS in_stock_units,
            COALESCE(sr.returned_units, 0) AS returned_units,
            COALESCE(sr.returned_cost, 0) AS returned_cost,
            sr.last_returned_at
        FROM suppliers s
        LEFT JOIN products p ON p.supplier_id = s.id
        LEFT JOIN (
            SELECT
                supplier_id,
                COUNT(*) AS returned_units,
                COALESCE(SUM(purchase_price), 0) AS returned_cost,
                MAX(returned_at) AS last_returned_at
            FROM supplier_returns
            GROUP BY supplier_id
        ) sr ON sr.supplier_id = s.id
        GROUP BY s.id
        ORDER BY s.id DESC
        """
    ).fetchall()

    recent_supplier_returns = db.execute(
        """
        SELECT
            sr.*,
            s.name AS supplier_name
        FROM supplier_returns sr
        JOIN suppliers s ON s.id = sr.supplier_id
        ORDER BY sr.id DESC
        LIMIT 120
        """
    ).fetchall()

    return render_template(
        "suppliers.html",
        suppliers=supplier_list,
        recent_supplier_returns=recent_supplier_returns,
        supplier_prefill_id=supplier_prefill_id,
        imei_prefill=imei_prefill,
        today=date.today().isoformat(),
    )


@app.get("/edit-tools")
def edit_tools():
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can open Edit Tools.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    tool = (request.args.get("tool", "products") or "products").strip().lower()
    allowed_tools = {"products", "customers", "retail_customers", "suppliers"}
    if tool not in allowed_tools:
        tool = "products"
    q = request.args.get("q", "").strip()
    edit_id = parse_optional_int(request.args.get("edit_id", ""))

    summary_counts = {
        "products": int(query_scalar("SELECT COUNT(*) FROM products")),
        "customers": int(
            query_scalar(
                "SELECT COUNT(*) FROM customers WHERE shop_name <> ?",
                (LOCAL_RETAIL_WHOLESALE_SHOP_NAME,),
            )
        ),
        "retail_customers": int(query_scalar("SELECT COUNT(*) FROM retail_customers")),
        "suppliers": int(query_scalar("SELECT COUNT(*) FROM suppliers")),
    }

    search_text = q
    wildcard = f"%{search_text}%"
    results: list[sqlite3.Row] = []

    selected_product: sqlite3.Row | None = None
    selected_customer: sqlite3.Row | None = None
    selected_retail_customer: sqlite3.Row | None = None
    selected_supplier: sqlite3.Row | None = None

    selected_product_has_sales = False
    selected_product_can_delete = False
    selected_customer_can_delete = False
    selected_retail_customer_can_delete = False
    selected_supplier_can_delete = False
    selected_product_metrics = {
        "sales_count": 0,
        "return_count": 0,
        "adjustment_count": 0,
        "lifetime_profit": 0.0,
    }
    selected_product_sales_preview: list[sqlite3.Row] = []
    selected_product_adjustment_preview: list[sqlite3.Row] = []

    if tool == "products":
        sql = """
            SELECT
                p.id,
                p.imei,
                p.brand,
                p.model,
                COALESCE(p.storage, '') AS storage,
                COALESCE(p.color, '') AS color,
                COALESCE(p.category, '') AS category,
                COALESCE(p.warranty_type, '') AS warranty_type,
                COALESCE(sup.name, '') AS supplier_name,
                p.status,
                p.purchase_price,
                p.wholesale_price,
                p.retail_price,
                p.received_date
            FROM products p
            LEFT JOIN suppliers sup ON sup.id = p.supplier_id
        """
        params: list[object] = []
        if search_text:
            sql += """
                WHERE (
                    p.imei LIKE ?
                    OR p.brand LIKE ?
                    OR p.model LIKE ?
                    OR COALESCE(p.storage, '') LIKE ?
                    OR COALESCE(p.color, '') LIKE ?
                    OR COALESCE(sup.name, '') LIKE ?
                )
            """
            params.extend([wildcard, wildcard, wildcard, wildcard, wildcard, wildcard])
        sql += " ORDER BY p.id DESC LIMIT 80"
        results = db.execute(sql, tuple(params)).fetchall()

        if edit_id is not None:
            selected_product = db.execute(
                """
                SELECT
                    p.*,
                    sup.name AS supplier_name,
                    EXISTS(
                        SELECT 1
                        FROM sales s
                        WHERE s.product_id = p.id
                        LIMIT 1
                    ) AS has_sale_history,
                    EXISTS(
                        SELECT 1
                        FROM stock_adjustments sa
                        WHERE sa.product_id = p.id
                        LIMIT 1
                    ) AS has_stock_adjustment
                FROM products p
                LEFT JOIN suppliers sup ON sup.id = p.supplier_id
                WHERE p.id = ?
                """,
                (edit_id,),
            ).fetchone()
            if selected_product is None:
                flash("Selected product not found.", "error")
            else:
                selected_product_has_sales = int(selected_product["has_sale_history"] or 0) == 1
                selected_product_can_delete = (
                    str(selected_product["status"] or "").upper() != "SOLD"
                    and not selected_product_has_sales
                    and int(selected_product["has_stock_adjustment"] or 0) == 0
                )
                selected_product_metrics = {
                    "sales_count": int(
                        query_scalar(
                            "SELECT COUNT(*) FROM sales WHERE product_id = ?",
                            (edit_id,),
                        )
                    ),
                    "return_count": int(
                        query_scalar(
                            """
                            SELECT COUNT(*)
                            FROM sale_returns sr
                            JOIN sales sl ON sl.id = sr.sale_id
                            WHERE sl.product_id = ?
                            """,
                            (edit_id,),
                        )
                    ),
                    "adjustment_count": int(
                        query_scalar(
                            "SELECT COUNT(*) FROM stock_adjustments WHERE product_id = ?",
                            (edit_id,),
                        )
                    ),
                    "lifetime_profit": float(
                        query_scalar(
                            """
                            SELECT COALESCE(SUM(CASE WHEN sl.is_active = 1 THEN sl.sold_price - p.purchase_price ELSE 0 END), 0)
                            FROM sales sl
                            JOIN products p ON p.id = sl.product_id
                            WHERE sl.product_id = ?
                            """,
                            (edit_id,),
                        )
                        or 0
                    ),
                }
                selected_product_sales_preview = db.execute(
                    """
                    SELECT
                        sl.sold_at,
                        sl.sale_type,
                        sl.invoice_no,
                        sl.sold_price,
                        sl.due_amount,
                        sl.is_active,
                        CASE
                            WHEN sl.sale_type = 'RETAIL'
                                THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                            ELSE c.shop_name
                        END AS holder_name,
                        sr.return_date,
                        sr.reason AS return_reason
                    FROM sales sl
                    LEFT JOIN customers c ON c.id = sl.customer_id
                    LEFT JOIN retail_customers rc ON rc.id = sl.retail_customer_id
                    LEFT JOIN sale_returns sr ON sr.sale_id = sl.id
                    WHERE sl.product_id = ?
                    ORDER BY sl.id DESC
                    LIMIT 6
                    """,
                    (edit_id,),
                ).fetchall()
                selected_product_adjustment_preview = db.execute(
                    """
                    SELECT action, event_date, reason, created_at
                    FROM stock_adjustments
                    WHERE product_id = ?
                    ORDER BY id DESC
                    LIMIT 6
                    """,
                    (edit_id,),
                ).fetchall()

    elif tool == "customers":
        sql = """
            SELECT
                c.*,
                COALESCE(SUM(CASE WHEN s.is_active = 1 AND s.sale_type = 'WHOLESALE' THEN 1 ELSE 0 END), 0) AS sold_units,
                COALESCE(SUM(CASE WHEN s.is_active = 1 AND s.sale_type = 'WHOLESALE' THEN s.due_amount ELSE 0 END), 0) AS due_amount,
                COALESCE(SUM(CASE WHEN s.is_active = 1 AND s.sale_type = 'WHOLESALE' THEN s.sold_price - p.purchase_price ELSE 0 END), 0) AS profit_amount
            FROM customers c
            LEFT JOIN sales s ON s.customer_id = c.id
            LEFT JOIN products p ON p.id = s.product_id
            WHERE c.shop_name <> ?
        """
        params = [LOCAL_RETAIL_WHOLESALE_SHOP_NAME]
        if search_text:
            sql += """
                AND (
                    c.shop_name LIKE ?
                    OR COALESCE(c.owner_name, '') LIKE ?
                    OR COALESCE(c.phone, '') LIKE ?
                    OR COALESCE(c.area, '') LIKE ?
                    OR COALESCE(c.address, '') LIKE ?
                )
            """
            params.extend([wildcard, wildcard, wildcard, wildcard, wildcard])
        sql += " GROUP BY c.id ORDER BY c.id DESC LIMIT 80"
        results = db.execute(sql, tuple(params)).fetchall()

        if edit_id is not None:
            selected_customer = db.execute(
                """
                SELECT
                    c.*,
                    EXISTS(
                        SELECT 1
                        FROM sales s
                        WHERE s.customer_id = c.id
                          AND s.sale_type = 'WHOLESALE'
                        LIMIT 1
                    ) AS has_sales_history,
                    COALESCE((
                        SELECT SUM(s.due_amount)
                        FROM sales s
                        WHERE s.customer_id = c.id
                          AND s.sale_type = 'WHOLESALE'
                          AND s.is_active = 1
                    ), 0) AS due_amount_total
                FROM customers c
                WHERE c.id = ? AND c.shop_name <> ?
                """,
                (edit_id, LOCAL_RETAIL_WHOLESALE_SHOP_NAME),
            ).fetchone()
            if selected_customer is None:
                flash("Selected wholesale shop not found.", "error")
            else:
                selected_customer_can_delete = (
                    int(selected_customer["has_sales_history"] or 0) == 0
                    and float(selected_customer["due_amount_total"] or 0) <= 0.00001
                )

    elif tool == "retail_customers":
        sql = """
            SELECT
                rc.*,
                COALESCE(COUNT(ri.id), 0) AS invoice_count,
                COALESCE(SUM(ri.due_amount), 0) AS due_amount,
                MAX(ri.sold_at) AS last_sale_date
            FROM retail_customers rc
            LEFT JOIN retail_invoices ri ON ri.retail_customer_id = rc.id
            WHERE 1 = 1
        """
        params = []
        if search_text:
            sql += """
                AND (
                    rc.full_name LIKE ?
                    OR COALESCE(rc.phone, '') LIKE ?
                    OR COALESCE(rc.address, '') LIKE ?
                    OR COALESCE(rc.note, '') LIKE ?
                )
            """
            params.extend([wildcard, wildcard, wildcard, wildcard])
        sql += " GROUP BY rc.id ORDER BY rc.id DESC LIMIT 80"
        results = db.execute(sql, tuple(params)).fetchall()

        if edit_id is not None:
            selected_retail_customer = db.execute(
                """
                SELECT
                    rc.*,
                    COALESCE((
                        SELECT COUNT(*)
                        FROM retail_invoices ri
                        WHERE ri.retail_customer_id = rc.id
                    ), 0) AS invoice_count,
                    COALESCE((
                        SELECT SUM(ri.due_amount)
                        FROM retail_invoices ri
                        WHERE ri.retail_customer_id = rc.id
                    ), 0) AS due_amount_total
                FROM retail_customers rc
                WHERE rc.id = ?
                """,
                (edit_id,),
            ).fetchone()
            if selected_retail_customer is None:
                flash("Selected retail customer not found.", "error")
            else:
                selected_retail_customer_can_delete = (
                    int(selected_retail_customer["invoice_count"] or 0) == 0
                    and float(selected_retail_customer["due_amount_total"] or 0) <= 0.00001
                )

    elif tool == "suppliers":
        sql = """
            SELECT
                s.*,
                COALESCE(COUNT(p.id), 0) AS bought_units,
                COALESCE(SUM(CASE WHEN p.status = 'IN_STOCK' THEN 1 ELSE 0 END), 0) AS in_stock_units
            FROM suppliers s
            LEFT JOIN products p ON p.supplier_id = s.id
            WHERE 1 = 1
        """
        params = []
        if search_text:
            sql += """
                AND (
                    s.name LIKE ?
                    OR COALESCE(s.contact_person, '') LIKE ?
                    OR COALESCE(s.phone, '') LIKE ?
                    OR COALESCE(s.address, '') LIKE ?
                )
            """
            params.extend([wildcard, wildcard, wildcard, wildcard])
        sql += " GROUP BY s.id ORDER BY s.id DESC LIMIT 80"
        results = db.execute(sql, tuple(params)).fetchall()

        if edit_id is not None:
            selected_supplier = db.execute(
                """
                SELECT
                    s.*,
                    COALESCE((
                        SELECT COUNT(*)
                        FROM products p
                        WHERE p.supplier_id = s.id
                    ), 0) AS product_count,
                    COALESCE((
                        SELECT COUNT(*)
                        FROM model_catalog mc
                        WHERE mc.supplier_id = s.id
                    ), 0) AS catalog_count
                FROM suppliers s
                WHERE s.id = ?
                """,
                (edit_id,),
            ).fetchone()
            if selected_supplier is None:
                flash("Selected supplier not found.", "error")
            else:
                selected_supplier_can_delete = (
                    int(selected_supplier["product_count"] or 0) == 0
                    and int(selected_supplier["catalog_count"] or 0) == 0
                )

    suppliers_for_product = db.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()

    return render_template(
        "edit_tools.html",
        tool=tool,
        q=q,
        summary_counts=summary_counts,
        results=results,
        suppliers=suppliers_for_product,
        selected_product=selected_product,
        selected_product_has_sales=selected_product_has_sales,
        selected_product_can_delete=selected_product_can_delete,
        selected_customer=selected_customer,
        selected_customer_can_delete=selected_customer_can_delete,
        selected_retail_customer=selected_retail_customer,
        selected_retail_customer_can_delete=selected_retail_customer_can_delete,
        selected_supplier=selected_supplier,
        selected_supplier_can_delete=selected_supplier_can_delete,
        selected_product_metrics=selected_product_metrics,
        selected_product_sales_preview=selected_product_sales_preview,
        selected_product_adjustment_preview=selected_product_adjustment_preview,
        today=date.today().isoformat(),
    )


@app.post("/customers/<int:customer_id>/update")
def customer_update(customer_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can update wholesale shop info.", "error")
        return redirect(safe_next_path("dashboard"))

    db = get_db()
    row = db.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        flash("Wholesale shop not found.", "error")
        return redirect(safe_next_path("edit_tools"))

    shop_name = normalize_text_field(request.form.get("shop_name", ""))
    owner_name = normalize_text_field(request.form.get("owner_name", ""))
    phone = normalize_text_field(request.form.get("phone", ""))
    area = normalize_text_field(request.form.get("area", ""))
    address = normalize_text_field(request.form.get("address", ""))
    note = normalize_text_field(request.form.get("note", ""))

    if not shop_name:
        flash("Shop name is required.", "error")
        return redirect(safe_next_path("edit_tools"))
    if shop_name == LOCAL_RETAIL_WHOLESALE_SHOP_NAME:
        flash("This shop name is reserved.", "error")
        return redirect(safe_next_path("edit_tools"))

    duplicate = db.execute(
        """
        SELECT id
        FROM customers
        WHERE LOWER(TRIM(shop_name)) = LOWER(TRIM(?))
          AND id <> ?
        """,
        (shop_name, customer_id),
    ).fetchone()
    if duplicate is not None:
        flash("Another wholesale shop already uses this name.", "error")
        return redirect(safe_next_path("edit_tools"))

    db.execute(
        """
        UPDATE customers
        SET shop_name = ?, owner_name = ?, phone = ?, area = ?, address = ?, note = ?
        WHERE id = ?
        """,
        (shop_name, owner_name, phone, area, address, note, customer_id),
    )
    db.commit()
    write_audit_log(
        action="CUSTOMER_UPDATED",
        metadata={"customer_id": customer_id, "shop_name": shop_name, "phone": phone},
    )
    flash("Wholesale shop information updated.", "success")
    return redirect(safe_next_path("edit_tools"))


@app.post("/customers/<int:customer_id>/delete")
def customer_delete(customer_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can delete wholesale shop info.", "error")
        return redirect(safe_next_path("dashboard"))

    db = get_db()
    customer = db.execute(
        "SELECT id, shop_name FROM customers WHERE id = ?",
        (customer_id,),
    ).fetchone()
    if customer is None:
        flash("Wholesale shop not found.", "error")
        return redirect(safe_next_path("edit_tools"))

    sales_count = int(
        query_scalar(
            "SELECT COUNT(*) FROM sales WHERE customer_id = ? AND sale_type = 'WHOLESALE'",
            (customer_id,),
        )
    )
    if sales_count > 0:
        flash("This wholesale shop already has sale history. Delete is blocked.", "error")
        return redirect(safe_next_path("edit_tools"))

    db.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    db.commit()
    write_audit_log(
        action="CUSTOMER_DELETED",
        metadata={"customer_id": customer_id, "shop_name": str(customer["shop_name"] or "")},
    )
    flash("Wholesale shop deleted safely.", "success")
    return redirect(safe_next_path("edit_tools"))


@app.post("/retail-customers/<int:customer_id>/update")
def retail_customer_update(customer_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can update retail customer info.", "error")
        return redirect(safe_next_path("dashboard"))

    db = get_db()
    row = db.execute("SELECT id FROM retail_customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        flash("Retail customer not found.", "error")
        return redirect(safe_next_path("edit_tools"))

    full_name = normalize_text_field(request.form.get("full_name", ""))
    phone = normalize_text_field(request.form.get("phone", ""))
    address = normalize_text_field(request.form.get("address", ""))
    note = normalize_text_field(request.form.get("note", ""))

    if not full_name:
        flash("Retail customer name is required.", "error")
        return redirect(safe_next_path("edit_tools"))

    duplicate = db.execute(
        """
        SELECT id
        FROM retail_customers
        WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(?))
          AND TRIM(COALESCE(phone, '')) = TRIM(COALESCE(?, ''))
          AND id <> ?
        """,
        (full_name, phone, customer_id),
    ).fetchone()
    if duplicate is not None:
        flash("Another retail customer already uses this name and phone.", "error")
        return redirect(safe_next_path("edit_tools"))

    db.execute(
        """
        UPDATE retail_customers
        SET full_name = ?, phone = ?, address = ?, note = ?
        WHERE id = ?
        """,
        (full_name, phone, address, note, customer_id),
    )
    db.commit()
    write_audit_log(
        action="RETAIL_CUSTOMER_UPDATED",
        metadata={"retail_customer_id": customer_id, "full_name": full_name, "phone": phone},
    )
    flash("Retail customer information updated.", "success")
    return redirect(safe_next_path("edit_tools"))


@app.post("/retail-customers/<int:customer_id>/delete")
def retail_customer_delete(customer_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can delete retail customer info.", "error")
        return redirect(safe_next_path("dashboard"))

    db = get_db()
    row = db.execute(
        "SELECT id, full_name FROM retail_customers WHERE id = ?",
        (customer_id,),
    ).fetchone()
    if row is None:
        flash("Retail customer not found.", "error")
        return redirect(safe_next_path("edit_tools"))

    invoice_count = int(
        query_scalar("SELECT COUNT(*) FROM retail_invoices WHERE retail_customer_id = ?", (customer_id,))
    )
    if invoice_count > 0:
        flash("This retail customer already has invoice history. Delete is blocked.", "error")
        return redirect(safe_next_path("edit_tools"))

    db.execute("DELETE FROM retail_customers WHERE id = ?", (customer_id,))
    db.commit()
    write_audit_log(
        action="RETAIL_CUSTOMER_DELETED",
        metadata={"retail_customer_id": customer_id, "full_name": str(row["full_name"] or "")},
    )
    flash("Retail customer deleted safely.", "success")
    return redirect(safe_next_path("edit_tools"))


@app.post("/suppliers/<int:supplier_id>/update")
def supplier_update(supplier_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can update supplier info.", "error")
        return redirect(safe_next_path("dashboard"))

    db = get_db()
    row = db.execute("SELECT id FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if row is None:
        flash("Supplier not found.", "error")
        return redirect(safe_next_path("edit_tools"))

    name = normalize_text_field(request.form.get("name", ""))
    contact_person = normalize_text_field(request.form.get("contact_person", ""))
    phone = normalize_text_field(request.form.get("phone", ""))
    address = normalize_text_field(request.form.get("address", ""))
    note = normalize_text_field(request.form.get("note", ""))

    if not name:
        flash("Supplier name is required.", "error")
        return redirect(safe_next_path("edit_tools"))

    duplicate = db.execute(
        """
        SELECT id
        FROM suppliers
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
          AND id <> ?
        """,
        (name, supplier_id),
    ).fetchone()
    if duplicate is not None:
        flash("Another supplier already uses this name.", "error")
        return redirect(safe_next_path("edit_tools"))

    db.execute(
        """
        UPDATE suppliers
        SET name = ?, contact_person = ?, phone = ?, address = ?, note = ?
        WHERE id = ?
        """,
        (name, contact_person, phone, address, note, supplier_id),
    )
    db.commit()
    write_audit_log(
        action="SUPPLIER_UPDATED",
        metadata={"supplier_id": supplier_id, "name": name, "phone": phone},
    )
    flash("Supplier information updated.", "success")
    return redirect(safe_next_path("edit_tools"))


@app.post("/suppliers/<int:supplier_id>/delete")
def supplier_delete(supplier_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    if not is_tenant_admin(current_user):
        flash("Only admin can delete supplier info.", "error")
        return redirect(safe_next_path("dashboard"))

    db = get_db()
    supplier = db.execute("SELECT id, name FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if supplier is None:
        flash("Supplier not found.", "error")
        return redirect(safe_next_path("edit_tools"))

    product_count = int(query_scalar("SELECT COUNT(*) FROM products WHERE supplier_id = ?", (supplier_id,)))
    catalog_count = int(query_scalar("SELECT COUNT(*) FROM model_catalog WHERE supplier_id = ?", (supplier_id,)))
    if product_count > 0 or catalog_count > 0:
        flash("This supplier is linked with product/model data. Delete is blocked.", "error")
        return redirect(safe_next_path("edit_tools"))

    db.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))
    db.commit()
    write_audit_log(
        action="SUPPLIER_DELETED",
        metadata={"supplier_id": supplier_id, "name": str(supplier["name"] or "")},
    )
    flash("Supplier deleted safely.", "success")
    return redirect(safe_next_path("edit_tools"))


@app.route("/money-center", methods=["GET", "POST"])
def money_center():
    db = get_db()
    tenant = get_current_tenant()
    current_user = get_current_tenant_user()
    if tenant is None or current_user is None:
        return redirect(url_for("client_login"))

    if not tenant_has_module(tenant, "POCKET_MONEY"):
        flash("Pocket Money module is not enabled for this shop.", "error")
        return redirect(url_for("dashboard"))

    is_admin = normalize_role(str(current_user["role"]), default="USER") == "ADMIN"
    ensure_expense_finance_tables(db)
    generated_exp = generate_monthly_recurring_expenses(db)
    generated_inc = generate_monthly_recurring_incomes(db)
    if generated_exp:
        flash(f"Auto recurring expense posted: {generated_exp}", "success")
    if generated_inc:
        flash(f"Auto recurring income posted: {generated_inc}", "success")

    if request.method == "POST":
        action = request.form.get("action", "add_income").strip().lower()
        if action == "toggle_income_recurring":
            if not is_admin:
                flash("Only admin can update recurring income templates.", "error")
                return redirect(url_for("money_center"))
            template_id = parse_optional_int(request.form.get("template_id", ""))
            if not template_id:
                flash("Invalid recurring template.", "error")
                return redirect(url_for("money_center"))
            row = db.execute(
                "SELECT id, is_active FROM income_recurring_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                flash("Recurring income template not found.", "error")
                return redirect(url_for("money_center"))
            next_status = 0 if int(row["is_active"]) == 1 else 1
            db.execute(
                """
                UPDATE income_recurring_templates
                SET is_active = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_status, now_sqlite_text(), template_id),
            )
            db.commit()
            flash("Recurring income template updated.", "success")
            return redirect(url_for("money_center"))

        income_date = normalize_date(request.form.get("income_date", "").strip())
        category = request.form.get("category", "other_income").strip().lower()
        if category not in INCOME_CATEGORIES:
            category = "other_income"
        sub_category = normalize_text_field(request.form.get("sub_category", ""))
        source_name = normalize_text_field(request.form.get("source_name", ""))
        amount = parse_money(request.form.get("amount", "0"), "Income amount")
        payment_method = request.form.get("payment_method", "CASH").strip().upper()
        if payment_method not in INCOME_PAYMENT_METHODS:
            payment_method = "OTHER"
        branch_id = parse_optional_int(request.form.get("branch_id", "1")) or 1
        note = normalize_text_field(request.form.get("note", ""))
        auto_approve = request.form.get("auto_approve", "0") == "1"
        approval_status = "APPROVED" if (is_admin and auto_approve) else "PENDING"
        approved_by_user_id = int(current_user["id"]) if approval_status == "APPROVED" else None
        approved_at = now_sqlite_text() if approval_status == "APPROVED" else None

        branch = db.execute("SELECT id FROM branches WHERE id = ?", (branch_id,)).fetchone()
        if branch is None:
            flash("Invalid branch selected.", "error")
            return redirect(url_for("money_center"))
        if amount <= 0:
            flash("Income amount must be greater than 0.", "error")
            return redirect(url_for("money_center"))

        receipt_file = request.files.get("receipt_photo")
        receipt_path = ""
        if receipt_file is not None and (receipt_file.filename or "").strip():
            try:
                receipt_path = save_expense_receipt(receipt_file, prefix="income")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("money_center"))

        db.execute(
            """
            INSERT INTO incomes (
                income_date, category, sub_category, source_name, amount, payment_method, branch_id,
                note, receipt_path, entered_by_user_id, entered_by_username, approval_status,
                approved_by_user_id, approved_at, rejected_note, is_recurring_source,
                recurring_template_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, NULL, ?, ?)
            """,
            (
                income_date,
                category,
                sub_category,
                source_name,
                amount,
                payment_method,
                branch_id,
                note,
                receipt_path,
                int(current_user["id"]),
                str(current_user["username"]),
                approval_status,
                approved_by_user_id,
                approved_at,
                now_sqlite_text(),
                now_sqlite_text(),
            ),
        )

        if is_admin and request.form.get("is_recurring", "0") == "1":
            day_of_month = parse_optional_int(request.form.get("recurring_day_of_month", "1")) or 1
            day_of_month = max(1, min(day_of_month, 28))
            template_title = request.form.get("recurring_title", "").strip() or f"{category.title()} recurring"
            db.execute(
                """
                INSERT INTO income_recurring_templates (
                    title, category, sub_category, source_name, amount, payment_method, branch_id,
                    day_of_month, note, is_active, created_by_user_id,
                    created_by_username, last_generated_month, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?)
                """,
                (
                    template_title,
                    category,
                    sub_category,
                    source_name,
                    amount,
                    payment_method,
                    branch_id,
                    day_of_month,
                    note,
                    int(current_user["id"]),
                    str(current_user["username"]),
                    now_sqlite_text(),
                    now_sqlite_text(),
                ),
            )

        db.commit()
        flash(
            "Income saved." if approval_status == "APPROVED" else "Income saved as pending approval.",
            "success",
        )
        return redirect(url_for("money_center", date_from=income_date, date_to=income_date, branch_id=branch_id))

    date_from = normalize_date(request.args.get("date_from", "").strip())
    date_to = normalize_date(request.args.get("date_to", "").strip())
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    status_filter = request.args.get("status", "ALL").strip().upper()
    if status_filter not in {"ALL", "PENDING", "APPROVED", "REJECTED"}:
        status_filter = "ALL"
    branch_filter = request.args.get("branch_id", "ALL").strip().upper()

    income_where_parts = ["i.income_date BETWEEN ? AND ?"]
    income_params: list[object] = [date_from, date_to]
    if status_filter != "ALL":
        income_where_parts.append("i.approval_status = ?")
        income_params.append(status_filter)
    if branch_filter != "ALL":
        branch_id = parse_optional_int(branch_filter) or 1
        income_where_parts.append("i.branch_id = ?")
        income_params.append(branch_id)

    incomes_rows = db.execute(
        f"""
        SELECT
            i.*,
            b.name AS branch_name,
            au.username AS approved_by_username
        FROM incomes i
        LEFT JOIN branches b ON b.id = i.branch_id
        LEFT JOIN users au ON au.id = i.approved_by_user_id
        WHERE {' AND '.join(income_where_parts)}
        ORDER BY i.income_date DESC, i.id DESC
        LIMIT 600
        """,
        tuple(income_params),
    ).fetchall()

    branches = db.execute(
        """
        SELECT id, name, is_default
        FROM branches
        ORDER BY is_default DESC, name ASC
        """
    ).fetchall()

    recurring_templates = db.execute(
        """
        SELECT id, title, category, sub_category, source_name, amount, payment_method, branch_id,
               day_of_month, note, is_active, last_generated_month, created_at
        FROM income_recurring_templates
        ORDER BY is_active DESC, id DESC
        LIMIT 200
        """
    ).fetchall()

    totals_where_parts = ["income_date BETWEEN ? AND ?"]
    totals_params: list[object] = [date_from, date_to]
    if branch_filter != "ALL":
        totals_branch_id = parse_optional_int(branch_filter) or 1
        totals_where_parts.append("branch_id = ?")
        totals_params.append(totals_branch_id)

    income_totals = db.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN approval_status = 'APPROVED' THEN amount ELSE 0 END), 0) AS approved_total,
            COALESCE(SUM(CASE WHEN approval_status = 'PENDING' THEN amount ELSE 0 END), 0) AS pending_total,
            COALESCE(SUM(CASE WHEN approval_status = 'REJECTED' THEN amount ELSE 0 END), 0) AS rejected_total
        FROM incomes
        WHERE {' AND '.join(totals_where_parts)}
        """,
        tuple(totals_params),
    ).fetchone()

    expense_totals = db.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN approval_status = 'APPROVED' THEN amount ELSE 0 END), 0) AS approved_total,
            COALESCE(SUM(CASE WHEN approval_status = 'PENDING' THEN amount ELSE 0 END), 0) AS pending_total,
            COALESCE(SUM(CASE WHEN approval_status = 'REJECTED' THEN amount ELSE 0 END), 0) AS rejected_total
        FROM expenses
        WHERE {' AND '.join(totals_where_parts).replace('income_date', 'expense_date')}
        """,
        tuple(totals_params),
    ).fetchone()

    expense_rows = db.execute(
        """
        SELECT expense_date AS ledger_date, category, sub_category, employee_name AS source_name, amount, approval_status
        FROM expenses
        WHERE expense_date BETWEEN ? AND ?
        ORDER BY expense_date DESC, id DESC
        LIMIT 200
        """,
        (date_from, date_to),
    ).fetchall()

    income_category_summary = db.execute(
        """
        SELECT category, COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS total_entry
        FROM incomes
        WHERE income_date BETWEEN ? AND ?
          AND approval_status = 'APPROVED'
        GROUP BY category
        ORDER BY total_amount DESC, category ASC
        LIMIT 12
        """,
        (date_from, date_to),
    ).fetchall()
    expense_category_summary = db.execute(
        """
        SELECT category, COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS total_entry
        FROM expenses
        WHERE expense_date BETWEEN ? AND ?
          AND approval_status = 'APPROVED'
        GROUP BY category
        ORDER BY total_amount DESC, category ASC
        LIMIT 12
        """,
        (date_from, date_to),
    ).fetchall()

    approved_income = float(income_totals["approved_total"] if income_totals is not None else 0)
    approved_expense = float(expense_totals["approved_total"] if expense_totals is not None else 0)
    net_balance = approved_income - approved_expense

    return render_template(
        "money_center.html",
        date_from=date_from,
        date_to=date_to,
        status_filter=status_filter,
        branch_filter=branch_filter,
        branches=branches,
        incomes_rows=incomes_rows,
        recurring_templates=recurring_templates,
        income_totals=income_totals,
        expense_totals=expense_totals,
        approved_income=approved_income,
        approved_expense=approved_expense,
        net_balance=net_balance,
        expense_rows=expense_rows,
        income_category_summary=income_category_summary,
        expense_category_summary=expense_category_summary,
        income_categories=get_income_category_options(),
        income_methods=INCOME_PAYMENT_METHODS,
        is_admin_user=is_admin,
    )


@app.get("/money")
def money_center_alias():
    return redirect(url_for("money_center"))


@app.get("/pocket-more")
def pocket_more():
    tenant = get_current_tenant()
    current_user = get_current_tenant_user()
    if tenant is None or current_user is None:
        return redirect(url_for("client_login"))

    if not tenant_has_module(tenant, "POCKET_MONEY"):
        flash("Pocket Money module is not enabled for this shop.", "error")
        return redirect(url_for("dashboard"))

    return render_template("pocket_more.html")


@app.post("/incomes/<int:income_id>/approve")
def income_approve(income_id: int):
    current_user = get_current_tenant_user()
    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only admin can approve income.", "error")
        return redirect(url_for("money_center"))
    db = get_db()
    row = db.execute("SELECT id FROM incomes WHERE id = ?", (income_id,)).fetchone()
    if row is None:
        flash("Income entry not found.", "error")
        return redirect(url_for("money_center"))
    db.execute(
        """
        UPDATE incomes
        SET approval_status = 'APPROVED',
            approved_by_user_id = ?,
            approved_at = ?,
            rejected_note = '',
            updated_at = ?
        WHERE id = ?
        """,
        (int(current_user["id"]), now_sqlite_text(), now_sqlite_text(), income_id),
    )
    db.commit()
    flash("Income approved.", "success")
    return redirect(url_for("money_center"))


@app.post("/incomes/<int:income_id>/reject")
def income_reject(income_id: int):
    current_user = get_current_tenant_user()
    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only admin can reject income.", "error")
        return redirect(url_for("money_center"))
    db = get_db()
    row = db.execute("SELECT id FROM incomes WHERE id = ?", (income_id,)).fetchone()
    if row is None:
        flash("Income entry not found.", "error")
        return redirect(url_for("money_center"))
    rejected_note = request.form.get("rejected_note", "").strip()
    db.execute(
        """
        UPDATE incomes
        SET approval_status = 'REJECTED',
            approved_by_user_id = ?,
            approved_at = ?,
            rejected_note = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (int(current_user["id"]), now_sqlite_text(), rejected_note, now_sqlite_text(), income_id),
    )
    db.commit()
    flash("Income rejected.", "success")
    return redirect(url_for("money_center"))


@app.post("/incomes/<int:income_id>/delete")
def income_delete(income_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    redirect_target = safe_next_path("money_center")

    is_admin = normalize_role(str(current_user["role"]), default="USER") == "ADMIN"
    db = get_db()
    row = db.execute(
        """
        SELECT id, approval_status, entered_by_user_id, receipt_path
        FROM incomes
        WHERE id = ?
        """,
        (income_id,),
    ).fetchone()
    if row is None:
        flash("Income entry not found.", "error")
        return redirect(redirect_target)

    is_owner = int(row["entered_by_user_id"] or 0) == int(current_user["id"])
    if not is_admin and not (is_owner and str(row["approval_status"]) == "PENDING"):
        flash("You do not have permission to delete this income.", "error")
        return redirect(redirect_target)

    receipt_file = resolve_expense_receipt_file(str(row["receipt_path"] or ""))
    db.execute("DELETE FROM incomes WHERE id = ?", (income_id,))
    db.commit()
    if receipt_file is not None:
        try:
            receipt_file.unlink()
        except OSError:
            pass
    flash("Income deleted.", "success")
    return redirect(redirect_target)


@app.route("/expenses", methods=["GET", "POST"])
def expenses():
    db = get_db()
    tenant = get_current_tenant()
    is_pocket_module = bool(tenant is not None and get_tenant_default_endpoint(tenant) == "money_center")
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    is_admin = normalize_role(str(current_user["role"]), default="USER") == "ADMIN"
    ensure_expense_finance_tables(db)
    generated = generate_monthly_recurring_expenses(db)
    if generated:
        flash(f"Auto recurring expense posted: {generated}", "success")

    if request.method == "POST":
        action = request.form.get("action", "add_expense").strip().lower()
        if is_pocket_module and action in {"save_petty", "toggle_recurring"}:
            flash("This section is hidden in Pocket Money mode.", "error")
            return redirect(url_for("expenses"))
        if action == "save_petty":
            if not is_admin:
                flash("Only admin can update petty cash.", "error")
                return redirect(url_for("expenses"))
            petty_date = normalize_date(request.form.get("petty_date", "").strip())
            branch_id = parse_optional_int(request.form.get("petty_branch_id", "1")) or 1
            opening_cash = parse_money(request.form.get("opening_cash", "0"), "Opening cash")
            closing_cash = parse_money(request.form.get("closing_cash", "0"), "Closing cash")
            note = request.form.get("petty_note", "").strip()
            branch = db.execute("SELECT id FROM branches WHERE id = ?", (branch_id,)).fetchone()
            if branch is None:
                flash("Invalid branch selected.", "error")
                return redirect(url_for("expenses"))
            existing = db.execute(
                "SELECT id FROM petty_cash_daily WHERE cash_date = ? AND branch_id = ?",
                (petty_date, branch_id),
            ).fetchone()
            if existing is None:
                db.execute(
                    """
                    INSERT INTO petty_cash_daily (
                        cash_date, branch_id, opening_cash, closing_cash, note,
                        created_by_user_id, created_by_username, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        petty_date,
                        branch_id,
                        opening_cash,
                        closing_cash,
                        note,
                        int(current_user["id"]),
                        str(current_user["username"]),
                        now_sqlite_text(),
                        now_sqlite_text(),
                    ),
                )
            else:
                db.execute(
                    """
                    UPDATE petty_cash_daily
                    SET opening_cash = ?, closing_cash = ?, note = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (opening_cash, closing_cash, note, now_sqlite_text(), int(existing["id"])),
                )
            db.commit()
            flash("Petty cash updated.", "success")
            return redirect(url_for("expenses", date_from=petty_date, date_to=petty_date, branch_id=branch_id))

        if action == "toggle_recurring":
            if not is_admin:
                flash("Only admin can update recurring templates.", "error")
                return redirect(url_for("expenses"))
            template_id = parse_optional_int(request.form.get("template_id", ""))
            if not template_id:
                flash("Invalid recurring template.", "error")
                return redirect(url_for("expenses"))
            row = db.execute(
                "SELECT id, is_active FROM expense_recurring_templates WHERE id = ?",
                (template_id,),
            ).fetchone()
            if row is None:
                flash("Recurring template not found.", "error")
                return redirect(url_for("expenses"))
            next_status = 0 if int(row["is_active"]) == 1 else 1
            db.execute(
                """
                UPDATE expense_recurring_templates
                SET is_active = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_status, now_sqlite_text(), template_id),
            )
            db.commit()
            flash("Recurring template updated.", "success")
            return redirect(url_for("expenses"))

        expense_date = normalize_date(request.form.get("expense_date", "").strip())
        category = request.form.get("category", "misc").strip().lower()
        if category not in EXPENSE_CATEGORIES:
            category = "misc"
        sub_category = normalize_text_field(request.form.get("sub_category", ""))
        employee_name = normalize_text_field(request.form.get("employee_name", ""))
        amount = parse_money(request.form.get("amount", "0"), "Expense amount")
        payment_method = request.form.get("payment_method", "CASH").strip().upper()
        if payment_method not in EXPENSE_PAYMENT_METHODS:
            payment_method = "OTHER"
        branch_id = parse_optional_int(request.form.get("branch_id", "1")) or 1
        note = normalize_text_field(request.form.get("note", ""))
        auto_approve = request.form.get("auto_approve", "0") == "1"
        approval_status = "APPROVED" if (is_admin and auto_approve) else "PENDING"
        approved_by_user_id = int(current_user["id"]) if approval_status == "APPROVED" else None
        approved_at = now_sqlite_text() if approval_status == "APPROVED" else None

        branch = db.execute("SELECT id FROM branches WHERE id = ?", (branch_id,)).fetchone()
        if branch is None:
            flash("Invalid branch selected.", "error")
            return redirect(url_for("expenses"))
        if amount <= 0:
            flash("Expense amount must be greater than 0.", "error")
            return redirect(url_for("expenses"))

        receipt_file = request.files.get("receipt_photo")
        receipt_path = ""
        if receipt_file is not None and (receipt_file.filename or "").strip():
            try:
                receipt_path = save_expense_receipt(receipt_file, prefix="expense")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("expenses"))

        db.execute(
            """
            INSERT INTO expenses (
                expense_date, category, sub_category, employee_name, amount, payment_method, branch_id,
                note, receipt_path, entered_by_user_id, entered_by_username, approval_status,
                approved_by_user_id, approved_at, rejected_note, is_recurring_source,
                recurring_template_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, NULL, ?, ?)
            """,
            (
                expense_date,
                category,
                sub_category,
                employee_name,
                amount,
                payment_method,
                branch_id,
                note,
                receipt_path,
                int(current_user["id"]),
                str(current_user["username"]),
                approval_status,
                approved_by_user_id,
                approved_at,
                now_sqlite_text(),
                now_sqlite_text(),
            ),
        )

        if is_admin and request.form.get("is_recurring", "0") == "1":
            day_of_month = parse_optional_int(request.form.get("recurring_day_of_month", "1")) or 1
            day_of_month = max(1, min(day_of_month, 28))
            template_title = request.form.get("recurring_title", "").strip() or f"{category.title()} recurring"
            db.execute(
                """
                INSERT INTO expense_recurring_templates (
                    title, category, sub_category, employee_name, amount, payment_method, branch_id,
                    day_of_month, note, is_active, created_by_user_id,
                    created_by_username, last_generated_month, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?)
                """,
                (
                    template_title,
                    category,
                    sub_category,
                    employee_name,
                    amount,
                    payment_method,
                    branch_id,
                    day_of_month,
                    note,
                    int(current_user["id"]),
                    str(current_user["username"]),
                    now_sqlite_text(),
                    now_sqlite_text(),
                ),
            )

        db.commit()
        flash(
            "Expense saved." if approval_status == "APPROVED" else "Expense saved as pending approval.",
            "success",
        )
        return redirect(url_for("expenses", date_from=expense_date, date_to=expense_date, branch_id=branch_id))

    date_from = normalize_date(request.args.get("date_from", "").strip())
    date_to = normalize_date(request.args.get("date_to", "").strip())
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    status_filter = request.args.get("status", "ALL").strip().upper()
    if status_filter not in {"ALL", "PENDING", "APPROVED", "REJECTED"}:
        status_filter = "ALL"
    branch_filter = request.args.get("branch_id", "ALL").strip().upper()

    where_parts = ["e.expense_date BETWEEN ? AND ?"]
    params: list[object] = [date_from, date_to]
    if status_filter != "ALL":
        where_parts.append("e.approval_status = ?")
        params.append(status_filter)
    if branch_filter != "ALL":
        branch_id = parse_optional_int(branch_filter) or 1
        where_parts.append("e.branch_id = ?")
        params.append(branch_id)

    expenses_rows = db.execute(
        f"""
        SELECT
            e.*,
            b.name AS branch_name,
            au.username AS approved_by_username
        FROM expenses e
        LEFT JOIN branches b ON b.id = e.branch_id
        LEFT JOIN users au ON au.id = e.approved_by_user_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY e.expense_date DESC, e.id DESC
        LIMIT 600
        """,
        tuple(params),
    ).fetchall()

    branches = db.execute(
        """
        SELECT id, name, is_default
        FROM branches
        ORDER BY is_default DESC, name ASC
        """
    ).fetchall()

    recurring_templates = db.execute(
        """
        SELECT id, title, category, sub_category, amount, payment_method, branch_id,
               employee_name, day_of_month, note, is_active, last_generated_month, created_at
        FROM expense_recurring_templates
        ORDER BY is_active DESC, id DESC
        LIMIT 200
        """
    ).fetchall()

    totals_where_parts = ["expense_date BETWEEN ? AND ?"]
    totals_params: list[object] = [date_from, date_to]
    if branch_filter != "ALL":
        totals_branch_id = parse_optional_int(branch_filter) or 1
        totals_where_parts.append("branch_id = ?")
        totals_params.append(totals_branch_id)

    expense_totals = db.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN approval_status = 'APPROVED' THEN amount ELSE 0 END), 0) AS approved_total,
            COALESCE(SUM(CASE WHEN approval_status = 'PENDING' THEN amount ELSE 0 END), 0) AS pending_total,
            COALESCE(SUM(CASE WHEN approval_status = 'REJECTED' THEN amount ELSE 0 END), 0) AS rejected_total
        FROM expenses
        WHERE {' AND '.join(totals_where_parts)}
        """,
        tuple(totals_params),
    ).fetchone()

    advance_where_parts = ["expense_date BETWEEN ? AND ?"]
    advance_params: list[object] = [date_from, date_to]
    if branch_filter != "ALL":
        advance_branch_id = parse_optional_int(branch_filter) or 1
        advance_where_parts.append("branch_id = ?")
        advance_params.append(advance_branch_id)

    employee_advances: list[sqlite3.Row] = []
    if not is_pocket_module:
        employee_advances = db.execute(
            f"""
            SELECT
                COALESCE(NULLIF(TRIM(employee_name), ''), entered_by_username, 'Unknown') AS employee_name,
                COUNT(*) AS entry_count,
                COALESCE(SUM(amount), 0) AS total_amount,
                COALESCE(SUM(CASE WHEN approval_status = 'APPROVED' THEN amount ELSE 0 END), 0) AS approved_amount,
                COALESCE(SUM(CASE WHEN approval_status = 'PENDING' THEN amount ELSE 0 END), 0) AS pending_amount
            FROM expenses
            WHERE {' AND '.join(advance_where_parts)}
              AND LOWER(COALESCE(category, '')) IN ('employee_advance', 'advance')
            GROUP BY COALESCE(NULLIF(TRIM(employee_name), ''), entered_by_username, 'Unknown')
            ORDER BY total_amount DESC, employee_name ASC
            LIMIT 60
            """,
            tuple(advance_params),
        ).fetchall()

    petty_date = normalize_date(request.args.get("petty_date", "").strip() or date_to)
    petty_branch_id = parse_optional_int(request.args.get("petty_branch_id", "")) or (
        int(branches[0]["id"]) if branches else 1
    )
    petty_row = db.execute(
        """
        SELECT *
        FROM petty_cash_daily
        WHERE cash_date = ? AND branch_id = ?
        LIMIT 1
        """,
        (petty_date, petty_branch_id),
    ).fetchone()

    cash_sale = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(s.paid_amount), 0)
            FROM sales s
            WHERE s.is_active = 1 AND s.sold_at = ?
            """,
            (petty_date,),
        )
    )
    due_collection = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM due_collections
            WHERE collected_at = ?
            """,
            (petty_date,),
        )
    )
    cash_expense = float(
        query_scalar(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM expenses
            WHERE approval_status = 'APPROVED'
              AND expense_date = ?
              AND payment_method = 'CASH'
              AND branch_id = ?
            """,
            (petty_date, petty_branch_id),
        )
    )
    opening_cash = float(petty_row["opening_cash"] if petty_row is not None else 0)
    closing_cash = float(petty_row["closing_cash"] if petty_row is not None else 0)
    expected_closing = opening_cash + cash_sale + due_collection - cash_expense
    shortage_overage = closing_cash - expected_closing

    return render_template(
        "expenses.html",
        expenses_rows=expenses_rows,
        branches=branches,
        recurring_templates=recurring_templates,
        expense_totals=expense_totals,
        date_from=date_from,
        date_to=date_to,
        status_filter=status_filter,
        branch_filter=branch_filter,
        petty_date=petty_date,
        petty_branch_id=petty_branch_id,
        petty_row=petty_row,
        cash_sale=cash_sale,
        due_collection=due_collection,
        cash_expense=cash_expense,
        expected_closing=expected_closing,
        shortage_overage=shortage_overage,
        is_admin_user=is_admin,
        expense_categories=EXPENSE_CATEGORIES,
        expense_category_options=get_expense_category_options(),
        expense_methods=EXPENSE_PAYMENT_METHODS,
        employee_advances=employee_advances,
        is_pocket_module=is_pocket_module,
    )


@app.post("/expenses/<int:expense_id>/approve")
def expense_approve(expense_id: int):
    current_user = get_current_tenant_user()
    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only admin can approve expense.", "error")
        return redirect(url_for("expenses"))
    db = get_db()
    row = db.execute("SELECT id, approval_status FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if row is None:
        flash("Expense not found.", "error")
        return redirect(url_for("expenses"))
    db.execute(
        """
        UPDATE expenses
        SET approval_status = 'APPROVED',
            approved_by_user_id = ?,
            approved_at = ?,
            rejected_note = '',
            updated_at = ?
        WHERE id = ?
        """,
        (int(current_user["id"]), now_sqlite_text(), now_sqlite_text(), expense_id),
    )
    db.commit()
    flash("Expense approved.", "success")
    return redirect(url_for("expenses"))


@app.post("/expenses/<int:expense_id>/reject")
def expense_reject(expense_id: int):
    current_user = get_current_tenant_user()
    if current_user is None or normalize_role(str(current_user["role"]), default="USER") != "ADMIN":
        flash("Only admin can reject expense.", "error")
        return redirect(url_for("expenses"))
    db = get_db()
    row = db.execute("SELECT id FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if row is None:
        flash("Expense not found.", "error")
        return redirect(url_for("expenses"))
    rejected_note = request.form.get("rejected_note", "").strip()
    db.execute(
        """
        UPDATE expenses
        SET approval_status = 'REJECTED',
            approved_by_user_id = ?,
            approved_at = ?,
            rejected_note = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (int(current_user["id"]), now_sqlite_text(), rejected_note, now_sqlite_text(), expense_id),
    )
    db.commit()
    flash("Expense rejected.", "success")
    return redirect(url_for("expenses"))


@app.post("/expenses/<int:expense_id>/delete")
def expense_delete(expense_id: int):
    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    redirect_target = safe_next_path("expenses")
    is_admin = normalize_role(str(current_user["role"]), default="USER") == "ADMIN"
    db = get_db()
    row = db.execute(
        """
        SELECT id, approval_status, entered_by_user_id, receipt_path
        FROM expenses
        WHERE id = ?
        """,
        (expense_id,),
    ).fetchone()
    if row is None:
        flash("Expense not found.", "error")
        return redirect(redirect_target)
    is_owner = int(row["entered_by_user_id"] or 0) == int(current_user["id"])
    if not is_admin and not (is_owner and str(row["approval_status"]) == "PENDING"):
        flash("You do not have permission to delete this expense.", "error")
        return redirect(redirect_target)

    receipt_file = resolve_expense_receipt_file(str(row["receipt_path"] or ""))
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    db.commit()
    if receipt_file is not None:
        try:
            receipt_file.unlink()
        except OSError:
            pass
    flash("Expense deleted.", "success")
    return redirect(redirect_target)


@app.get("/expenses/receipt/<path:filename>")
def expense_receipt(filename: str):
    safe_name = Path(filename).name
    target = resolve_expense_receipt_file(safe_name)
    if target is None:
        flash("Receipt not found.", "error")
        return redirect(request.referrer or url_for("expenses"))
    return send_from_directory(target.parent, target.name, as_attachment=False)


@app.post("/petty-cash/save")
def petty_cash_save():
    return redirect(url_for("expenses"))


@app.route("/daily-report", methods=["GET", "POST"])
def daily_report():
    db = get_db()
    ensure_expense_finance_tables(db)
    safe_ensure_daily_report_tables(db)
    has_snapshot_table = table_exists(db, "daily_report_snapshots")

    current_user = get_current_tenant_user()
    if current_user is None:
        return redirect(url_for("client_login"))
    is_admin = normalize_role(str(current_user["role"]), default="USER") == "ADMIN"

    selected_date = normalize_date(
        (request.form.get("report_date", "") if request.method == "POST" else request.args.get("report_date", "")).strip()
    )

    if request.method == "POST":
        action = request.form.get("action", "save_snapshot").strip().lower()
        if action == "save_snapshot":
            if not has_snapshot_table:
                flash(
                    "Daily snapshot storage is not available on this hosting. Live report is still active.",
                    "error",
                )
                return redirect(url_for("daily_report", report_date=selected_date))
            note = request.form.get("note", "").strip()
            requested_status = request.form.get("status", "DRAFT").strip().upper()
            snapshot_status = "FINAL" if requested_status == "FINAL" else "DRAFT"
            if snapshot_status == "FINAL" and not is_admin:
                flash("Only admin can mark daily closing as FINAL.", "error")
                return redirect(url_for("daily_report", report_date=selected_date))

            live_data = build_daily_report_data(db, selected_date)
            today_count = db.execute(
                """
                SELECT COALESCE(MAX(id), 0) AS max_id
                FROM daily_report_snapshots
                WHERE report_date = ?
                """,
                (selected_date,),
            ).fetchone()
            serial = int(today_count["max_id"] if today_count is not None else 0) + 1
            report_no = f"DR-{selected_date.replace('-', '')}-{serial:04d}"

            compact_snapshot = {
                "report_date": selected_date,
                "kpis": live_data.get("kpis", {}),
                "top_due_shops": list(live_data.get("top_due_shops", []))[:20],
                "high_return_models": list(live_data.get("high_return_models", []))[:20],
                "stock_in_rows": list(live_data.get("stock_in_rows", []))[:250],
                "wholesale_rows": list(live_data.get("wholesale_rows", []))[:350],
                "retail_rows": list(live_data.get("retail_rows", []))[:350],
                "return_rows": list(live_data.get("return_rows", []))[:300],
                "due_collection_rows": list(live_data.get("due_collection_rows", []))[:350],
                "expense_rows": list(live_data.get("expense_rows", []))[:350],
                "petty_rows": list(live_data.get("petty_rows", []))[:50],
                "saved_at": now_sqlite_text(),
            }

            db.execute(
                """
                INSERT INTO daily_report_snapshots (
                    report_date,
                    report_no,
                    snapshot_json,
                    note,
                    status,
                    created_by_user_id,
                    created_by_username,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    selected_date,
                    report_no,
                    json.dumps(compact_snapshot, ensure_ascii=False),
                    note,
                    snapshot_status,
                    int(current_user["id"]),
                    str(current_user["username"]),
                    now_sqlite_text(),
                ),
            )
            db.commit()

            snapshot_row_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            flash(
                f"Daily report snapshot saved ({report_no}, {snapshot_status}).",
                "success",
            )
            return redirect(
                url_for(
                    "daily_report",
                    report_date=selected_date,
                    snapshot_id=snapshot_row_id,
                )
            )

    live_data = build_daily_report_data(db, selected_date)
    snapshot_id = parse_optional_int(request.args.get("snapshot_id", "").strip())
    active_snapshot = None
    report_data = live_data

    if snapshot_id is not None and has_snapshot_table:
        active_snapshot = db.execute(
            """
            SELECT id, report_date, report_no, status, note, created_by_username, created_at, snapshot_json
            FROM daily_report_snapshots
            WHERE id = ?
            LIMIT 1
            """,
            (snapshot_id,),
        ).fetchone()
        if active_snapshot is not None:
            try:
                loaded_payload = json.loads(str(active_snapshot["snapshot_json"] or "{}"))
                if isinstance(loaded_payload, dict):
                    report_data = loaded_payload
                    if str(active_snapshot["report_date"] or "").strip():
                        selected_date = str(active_snapshot["report_date"])
            except json.JSONDecodeError:
                flash("Saved snapshot payload damaged. Showing live data.", "error")

    if has_snapshot_table:
        snapshots_today = db.execute(
            """
            SELECT id, report_no, report_date, status, note, created_by_username, created_at
            FROM daily_report_snapshots
            WHERE report_date = ?
            ORDER BY id DESC
            LIMIT 30
            """,
            (selected_date,),
        ).fetchall()
        snapshots_recent = db.execute(
            """
            SELECT id, report_no, report_date, status, created_by_username, created_at
            FROM daily_report_snapshots
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()
    else:
        snapshots_today = []
        snapshots_recent = []
    health_data = build_daily_health_signals(report_data if isinstance(report_data, dict) else {})

    return render_template(
        "daily_report.html",
        report_date=selected_date,
        report_data=report_data,
        live_data=live_data,
        active_snapshot=active_snapshot,
        snapshots_today=snapshots_today,
        snapshots_recent=snapshots_recent,
        health_data=health_data,
        is_admin_user=is_admin,
    )


@app.get("/daily-report/export.csv")
def daily_report_export_csv():
    db = get_db()
    safe_ensure_daily_report_tables(db)
    has_snapshot_table = table_exists(db, "daily_report_snapshots")

    selected_date = normalize_date(request.args.get("report_date", "").strip())
    snapshot_id = parse_optional_int(request.args.get("snapshot_id", "").strip())

    payload: dict[str, object] | None = None
    snapshot_no = ""
    snapshot_status = "LIVE"
    if snapshot_id is not None and has_snapshot_table:
        snap = db.execute(
            """
            SELECT id, report_no, status, snapshot_json
            FROM daily_report_snapshots
            WHERE id = ?
            LIMIT 1
            """,
            (snapshot_id,),
        ).fetchone()
        if snap is not None:
            try:
                parsed = json.loads(str(snap["snapshot_json"] or "{}"))
                if isinstance(parsed, dict):
                    payload = parsed
                    snapshot_no = str(snap["report_no"] or "")
                    snapshot_status = str(snap["status"] or "DRAFT")
                    if str(parsed.get("report_date") or "").strip():
                        selected_date = str(parsed.get("report_date"))
            except json.JSONDecodeError:
                payload = None

    if payload is None:
        payload = build_daily_report_data(db, selected_date)

    kpis = payload.get("kpis", {})
    if not isinstance(kpis, dict):
        kpis = {}

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["section", "field", "value"])
    writer.writerow(["meta", "report_date", selected_date])
    writer.writerow(["meta", "snapshot_no", snapshot_no or "LIVE"])
    writer.writerow(["meta", "snapshot_status", snapshot_status])
    for key, value in kpis.items():
        writer.writerow(["summary", key, value])

    writer.writerow([])
    writer.writerow(["stock_in", "imei", "brand", "model", "storage", "purchase_price", "supplier"])
    for row in payload.get("stock_in_rows", []) or []:
        if not isinstance(row, dict):
            continue
        writer.writerow(
            [
                "stock_in",
                row.get("imei", ""),
                row.get("brand", ""),
                row.get("model", ""),
                row.get("storage", ""),
                row.get("purchase_price", 0),
                row.get("supplier_name", ""),
            ]
        )

    writer.writerow([])
    writer.writerow(
        [
            "sales",
            "sale_type",
            "invoice_no",
            "imei",
            "party",
            "sold_price",
            "paid_amount",
            "due_amount",
            "purchase_price",
        ]
    )
    for row in payload.get("sales_rows", []) or []:
        if not isinstance(row, dict):
            continue
        writer.writerow(
            [
                "sales",
                row.get("sale_type", ""),
                row.get("invoice_no", ""),
                row.get("imei", ""),
                row.get("party_name", ""),
                row.get("sold_price", 0),
                row.get("paid_amount", 0),
                row.get("due_amount", 0),
                row.get("purchase_price", 0),
            ]
        )

    writer.writerow([])
    writer.writerow(["returns", "invoice_no", "imei", "party", "reason", "restock"])
    for row in payload.get("return_rows", []) or []:
        if not isinstance(row, dict):
            continue
        writer.writerow(
            [
                "returns",
                row.get("invoice_no", ""),
                row.get("imei", ""),
                row.get("party_name", ""),
                row.get("reason", ""),
                row.get("restock", 0),
            ]
        )

    writer.writerow([])
    writer.writerow(["due_collection", "shop", "invoice_no", "imei", "method", "amount", "note"])
    for row in payload.get("due_collection_rows", []) or []:
        if not isinstance(row, dict):
            continue
        writer.writerow(
            [
                "due_collection",
                row.get("shop_name", ""),
                row.get("invoice_no", ""),
                row.get("imei", ""),
                row.get("method", ""),
                row.get("amount", 0),
                row.get("note", ""),
            ]
        )

    writer.writerow([])
    writer.writerow(
        [
            "expenses",
            "category",
            "sub_category",
            "employee_name",
            "payment_method",
            "status",
            "amount",
            "note",
        ]
    )
    for row in payload.get("expense_rows", []) or []:
        if not isinstance(row, dict):
            continue
        writer.writerow(
            [
                "expenses",
                row.get("category", ""),
                row.get("sub_category", ""),
                row.get("employee_name", ""),
                row.get("payment_method", ""),
                row.get("approval_status", ""),
                row.get("amount", 0),
                row.get("note", ""),
            ]
        )

    output = make_response(buffer.getvalue())
    output.headers["Content-Type"] = "text/csv; charset=utf-8"
    output.headers["Content-Disposition"] = (
        f'attachment; filename="daily-report-{selected_date}.csv"'
    )
    return output


@app.route("/reports")
def reports():
    db = get_db()
    ensure_expense_finance_tables(db)
    generate_monthly_recurring_expenses(db)
    current_user = get_current_tenant_user()

    today_obj = date.today()
    today_iso = today_obj.isoformat()

    period = request.args.get("period", "").strip().upper()
    preset = request.args.get("preset", "").strip().lower()
    if not preset:
        preset = {
            "TODAY": "today",
            "MONTH": "month",
            "YEAR": "year",
            "LIFETIME": "lifetime",
        }.get(period, "month")
    if preset not in {"today", "week", "month", "year", "custom", "lifetime"}:
        preset = "month"

    tenant = get_current_tenant()
    is_pocket_module = bool(tenant is not None and get_tenant_default_endpoint(tenant) == "money_center")
    if is_pocket_module:
        current_user_id = int(current_user["id"]) if current_user is not None else 0
        current_user_is_admin = bool(
            current_user is not None
            and normalize_role(str(current_user["role"]), default="USER") == "ADMIN"
        )
        current_report_path = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
        generate_monthly_recurring_incomes(db)

        earliest_income = db.execute("SELECT MIN(income_date) AS d FROM incomes").fetchone()
        earliest_expense = db.execute("SELECT MIN(expense_date) AS d FROM expenses").fetchone()
        earliest_dates = [
            str(item["d"])
            for item in (earliest_income, earliest_expense)
            if item is not None and item["d"]
        ]
        lifetime_start_obj = min((date.fromisoformat(item) for item in earliest_dates), default=today_obj)

        req_start = request.args.get("date_from", "").strip()
        req_end = request.args.get("date_to", "").strip()
        req_start_obj = parse_iso_date(req_start)
        req_end_obj = parse_iso_date(req_end)

        if preset == "today":
            start_obj = today_obj
            end_obj = today_obj
        elif preset == "week":
            start_obj = today_obj - timedelta(days=today_obj.weekday())
            end_obj = today_obj
        elif preset == "month":
            start_obj = today_obj.replace(day=1)
            end_obj = today_obj
        elif preset == "year":
            start_obj = today_obj.replace(month=1, day=1)
            end_obj = today_obj
        elif preset == "lifetime":
            start_obj = lifetime_start_obj
            end_obj = today_obj
        else:
            start_obj = req_start_obj or today_obj.replace(day=1)
            end_obj = req_end_obj or today_obj

        if end_obj < start_obj:
            start_obj, end_obj = end_obj, start_obj

        start_date = start_obj.isoformat()
        end_date = end_obj.isoformat()

        branches = db.execute(
            """
            SELECT id, name, is_default
            FROM branches
            ORDER BY is_default DESC, name ASC
            """
        ).fetchall()

        branch_filter = request.args.get("branch_id", "ALL").strip().upper()
        valid_branch_values = {"ALL"} | {str(int(item["id"])) for item in branches}
        if branch_filter not in valid_branch_values:
            branch_filter = "ALL"

        status_filter = request.args.get("status", "ALL").strip().upper()
        if status_filter not in {"ALL", "PENDING", "APPROVED", "REJECTED"}:
            status_filter = "ALL"

        branch_sql = ""
        branch_params: list[object] = []
        if branch_filter != "ALL":
            branch_sql = " AND branch_id = ?"
            branch_params.append(int(branch_filter))

        status_sql = ""
        status_params: list[object] = []
        if status_filter != "ALL":
            status_sql = " AND approval_status = ?"
            status_params.append(status_filter)

        def compute_pocket_metrics(from_date: str, to_date: str) -> dict[str, float]:
            scoped_branch_sql = ""
            scoped_branch_params: list[object] = []
            if branch_filter != "ALL":
                scoped_branch_sql = " AND branch_id = ?"
                scoped_branch_params.append(int(branch_filter))

            income_params = [from_date, to_date, *scoped_branch_params]
            expense_params = [from_date, to_date, *scoped_branch_params]

            approved_income = float(
                query_scalar(
                    f"""
                    SELECT COALESCE(SUM(amount), 0)
                    FROM incomes
                    WHERE income_date BETWEEN ? AND ?
                      {scoped_branch_sql}
                      AND approval_status = 'APPROVED'
                    """,
                    tuple(income_params),
                )
            )
            approved_expense = float(
                query_scalar(
                    f"""
                    SELECT COALESCE(SUM(amount), 0)
                    FROM expenses
                    WHERE expense_date BETWEEN ? AND ?
                      {scoped_branch_sql}
                      AND approval_status = 'APPROVED'
                    """,
                    tuple(expense_params),
                )
            )
            pending_income = float(
                query_scalar(
                    f"""
                    SELECT COALESCE(SUM(amount), 0)
                    FROM incomes
                    WHERE income_date BETWEEN ? AND ?
                      {scoped_branch_sql}
                      AND approval_status = 'PENDING'
                    """,
                    tuple(income_params),
                )
            )
            pending_expense = float(
                query_scalar(
                    f"""
                    SELECT COALESCE(SUM(amount), 0)
                    FROM expenses
                    WHERE expense_date BETWEEN ? AND ?
                      {scoped_branch_sql}
                      AND approval_status = 'PENDING'
                    """,
                    tuple(expense_params),
                )
            )
            approved_income_count = int(
                query_scalar(
                    f"""
                    SELECT COUNT(*)
                    FROM incomes
                    WHERE income_date BETWEEN ? AND ?
                      {scoped_branch_sql}
                      AND approval_status = 'APPROVED'
                    """,
                    tuple(income_params),
                )
            )
            approved_expense_count = int(
                query_scalar(
                    f"""
                    SELECT COUNT(*)
                    FROM expenses
                    WHERE expense_date BETWEEN ? AND ?
                      {scoped_branch_sql}
                      AND approval_status = 'APPROVED'
                    """,
                    tuple(expense_params),
                )
            )
            return {
                "income": approved_income,
                "expense": approved_expense,
                "net_balance": approved_income - approved_expense,
                "pending_income": pending_income,
                "pending_expense": pending_expense,
                "income_count": float(approved_income_count),
                "expense_count": float(approved_expense_count),
            }

        today_range = (today_iso, today_iso)
        mtd_range = (today_obj.replace(day=1).isoformat(), today_iso)
        ytd_range = (today_obj.replace(month=1, day=1).isoformat(), today_iso)
        life_range = (lifetime_start_obj.isoformat(), today_iso)
        executive_cards = {
            "today": compute_pocket_metrics(*today_range),
            "mtd": compute_pocket_metrics(*mtd_range),
            "ytd": compute_pocket_metrics(*ytd_range),
            "lifetime": compute_pocket_metrics(*life_range),
        }
        selected_metrics = compute_pocket_metrics(start_date, end_date)

        income_category_rows = [
            dict(row)
            for row in db.execute(
                f"""
                SELECT category, COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS total_entry
                FROM incomes
                WHERE income_date BETWEEN ? AND ?
                  {branch_sql}
                  AND approval_status = 'APPROVED'
                GROUP BY category
                ORDER BY total_amount DESC, category ASC
                LIMIT 12
                """,
                tuple([start_date, end_date, *branch_params]),
            ).fetchall()
        ]
        expense_category_rows = [
            dict(row)
            for row in db.execute(
                f"""
                SELECT category, COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS total_entry
                FROM expenses
                WHERE expense_date BETWEEN ? AND ?
                  {branch_sql}
                  AND approval_status = 'APPROVED'
                GROUP BY category
                ORDER BY total_amount DESC, category ASC
                LIMIT 12
                """,
                tuple([start_date, end_date, *branch_params]),
            ).fetchall()
        ]

        income_ledger_rows = db.execute(
            f"""
            SELECT
                id,
                income_date AS entry_date,
                category,
                sub_category,
                source_name,
                amount,
                payment_method,
                approval_status,
                note,
                entered_by_user_id
            FROM incomes
            WHERE income_date BETWEEN ? AND ?
              {branch_sql}
              {status_sql}
            ORDER BY income_date DESC, id DESC
            LIMIT 600
            """,
            tuple([start_date, end_date, *branch_params, *status_params]),
        ).fetchall()
        expense_ledger_rows = db.execute(
            f"""
            SELECT
                id,
                expense_date AS entry_date,
                category,
                sub_category,
                employee_name AS source_name,
                amount,
                payment_method,
                approval_status,
                note,
                entered_by_user_id
            FROM expenses
            WHERE expense_date BETWEEN ? AND ?
              {branch_sql}
              {status_sql}
            ORDER BY expense_date DESC, id DESC
            LIMIT 600
            """,
            tuple([start_date, end_date, *branch_params, *status_params]),
        ).fetchall()

        transaction_rows: list[dict[str, object]] = []
        for row in income_ledger_rows:
            approval_status = str(row["approval_status"] or "")
            entered_by_user_id = int(row["entered_by_user_id"] or 0)
            transaction_rows.append(
                {
                    "row_id": int(row["id"]),
                    "date": str(row["entry_date"]),
                    "entry_type": "INCOME",
                    "category_kind": "income",
                    "category": str(row["category"] or ""),
                    "sub_category": str(row["sub_category"] or ""),
                    "source_name": str(row["source_name"] or ""),
                    "payment_method": str(row["payment_method"] or ""),
                    "approval_status": approval_status,
                    "amount": float(row["amount"] or 0),
                    "signed_amount": float(row["amount"] or 0),
                    "note": str(row["note"] or ""),
                    "can_delete": current_user_is_admin
                    or (entered_by_user_id == current_user_id and approval_status == "PENDING"),
                    "delete_url": url_for("income_delete", income_id=int(row["id"])),
                }
            )
        for row in expense_ledger_rows:
            amount = float(row["amount"] or 0)
            approval_status = str(row["approval_status"] or "")
            entered_by_user_id = int(row["entered_by_user_id"] or 0)
            transaction_rows.append(
                {
                    "row_id": int(row["id"]),
                    "date": str(row["entry_date"]),
                    "entry_type": "EXPENSE",
                    "category_kind": "expense",
                    "category": str(row["category"] or ""),
                    "sub_category": str(row["sub_category"] or ""),
                    "source_name": str(row["source_name"] or ""),
                    "payment_method": str(row["payment_method"] or ""),
                    "approval_status": approval_status,
                    "amount": amount,
                    "signed_amount": -amount,
                    "note": str(row["note"] or ""),
                    "can_delete": current_user_is_admin
                    or (entered_by_user_id == current_user_id and approval_status == "PENDING"),
                    "delete_url": url_for("expense_delete", expense_id=int(row["id"])),
                }
            )
        transaction_rows.sort(
            key=lambda item: (str(item.get("date") or ""), int(item.get("row_id") or 0)),
            reverse=True,
        )

        drill_kind = request.args.get("drill_kind", "").strip().lower()
        drill_key = request.args.get("drill_key", "").strip()
        drill_title = ""
        if drill_kind == "income_category" and drill_key:
            drill_title = f"Income Category: {drill_key}"
            transaction_rows = [
                item
                for item in transaction_rows
                if str(item.get("entry_type") or "") == "INCOME"
                and str(item.get("category") or "") == drill_key
            ]
        elif drill_kind == "expense_category" and drill_key:
            drill_title = f"Expense Category: {drill_key}"
            transaction_rows = [
                item
                for item in transaction_rows
                if str(item.get("entry_type") or "") == "EXPENSE"
                and str(item.get("category") or "") == drill_key
            ]
        transaction_rows = transaction_rows[:500]

        total_days = (end_obj - start_obj).days + 1
        trend_start_obj = start_obj
        if total_days > 120:
            trend_start_obj = end_obj - timedelta(days=119)
        trend_start = trend_start_obj.isoformat()

        trend_income_rows = db.execute(
            f"""
            SELECT income_date AS d, COALESCE(SUM(amount), 0) AS total
            FROM incomes
            WHERE income_date BETWEEN ? AND ?
              {branch_sql}
              AND approval_status = 'APPROVED'
            GROUP BY income_date
            ORDER BY income_date ASC
            """,
            tuple([trend_start, end_date, *branch_params]),
        ).fetchall()
        trend_expense_rows = db.execute(
            f"""
            SELECT expense_date AS d, COALESCE(SUM(amount), 0) AS total
            FROM expenses
            WHERE expense_date BETWEEN ? AND ?
              {branch_sql}
              AND approval_status = 'APPROVED'
            GROUP BY expense_date
            ORDER BY expense_date ASC
            """,
            tuple([trend_start, end_date, *branch_params]),
        ).fetchall()
        income_by_day = {str(row["d"]): float(row["total"] or 0) for row in trend_income_rows}
        expense_by_day = {str(row["d"]): float(row["total"] or 0) for row in trend_expense_rows}

        trend_labels: list[str] = []
        trend_income: list[float] = []
        trend_expense: list[float] = []
        trend_net: list[float] = []
        cursor = trend_start_obj
        while cursor <= end_obj:
            key = cursor.isoformat()
            income_value = income_by_day.get(key, 0.0)
            expense_value = expense_by_day.get(key, 0.0)
            trend_labels.append(key)
            trend_income.append(round(income_value, 2))
            trend_expense.append(round(expense_value, 2))
            trend_net.append(round(income_value - expense_value, 2))
            cursor += timedelta(days=1)

        period_label = {
            "today": "Today",
            "week": "This Week",
            "month": "This Month",
            "year": "This Year",
            "custom": "Custom Range",
            "lifetime": "Lifetime",
        }.get(preset, "This Month")
        quick_range_links = [
            {"preset": "today", "label": "Today"},
            {"preset": "week", "label": "This Week"},
            {"preset": "month", "label": "This Month"},
            {"preset": "year", "label": "This Year"},
        ]

        return render_template(
            "reports_pocket.html",
            period=period or preset.upper(),
            period_label=period_label,
            preset=preset,
            date_from=start_date,
            date_to=end_date,
            branch_filter=branch_filter,
            status_filter=status_filter,
            branches=branches,
            quick_range_links=quick_range_links,
            executive_cards=executive_cards,
            selected_metrics=selected_metrics,
            income_category_rows=income_category_rows,
            expense_category_rows=expense_category_rows,
            top_income_categories=income_category_rows[:5],
            top_expense_categories=expense_category_rows[:5],
            trend_labels=trend_labels,
            trend_income=trend_income,
            trend_expense=trend_expense,
            trend_net=trend_net,
            transaction_rows=transaction_rows,
            drill_kind=drill_kind,
            drill_key=drill_key,
            drill_title=drill_title,
            trend_start=trend_start,
            current_report_path=current_report_path,
        )

    earliest_sales = db.execute("SELECT MIN(sold_at) AS d FROM sales").fetchone()
    earliest_returns = db.execute("SELECT MIN(return_date) AS d FROM sale_returns").fetchone()
    earliest_expenses = db.execute("SELECT MIN(expense_date) AS d FROM expenses").fetchone()
    earliest_collect = db.execute("SELECT MIN(collected_at) AS d FROM due_collections").fetchone()
    earliest_dates = [
        str(item["d"])
        for item in (earliest_sales, earliest_returns, earliest_expenses, earliest_collect)
        if item is not None and item["d"]
    ]
    lifetime_start_obj = min((date.fromisoformat(item) for item in earliest_dates), default=today_obj)

    req_start = request.args.get("date_from", "").strip()
    req_end = request.args.get("date_to", "").strip()
    req_start_obj = parse_iso_date(req_start)
    req_end_obj = parse_iso_date(req_end)

    if preset == "today":
        start_obj = today_obj
        end_obj = today_obj
    elif preset == "week":
        start_obj = today_obj - timedelta(days=today_obj.weekday())
        end_obj = today_obj
    elif preset == "month":
        start_obj = today_obj.replace(day=1)
        end_obj = today_obj
    elif preset == "year":
        start_obj = today_obj.replace(month=1, day=1)
        end_obj = today_obj
    elif preset == "lifetime":
        start_obj = lifetime_start_obj
        end_obj = today_obj
    else:
        start_obj = req_start_obj or today_obj.replace(day=1)
        end_obj = req_end_obj or today_obj

    if end_obj < start_obj:
        start_obj, end_obj = end_obj, start_obj

    start_date = start_obj.isoformat()
    end_date = end_obj.isoformat()

    branch_filter = request.args.get("branch_id", "ALL").strip().upper()
    sale_type_filter = request.args.get("sale_type", "ALL").strip().upper()
    if sale_type_filter not in {"ALL", "WHOLESALE", "RETAIL"}:
        sale_type_filter = "ALL"
    module_filter = request.args.get("module", "ALL").strip().upper()
    module_options = [{"key": "ALL", "label_en": "All Modules", "label_bn": "সব মডিউল"}]
    module_options.extend(get_business_module_options())
    valid_modules = {item["key"] for item in module_options}
    if module_filter not in valid_modules:
        module_filter = "ALL"

    branches = db.execute(
        """
        SELECT id, name, is_default
        FROM branches
        ORDER BY is_default DESC, name ASC
        """
    ).fetchall()
    valid_branch_values = {"ALL"} | {str(int(item["id"])) for item in branches}
    if branch_filter not in valid_branch_values:
        branch_filter = "ALL"

    sales_raw = db.execute(
        """
        SELECT
            s.id,
            s.product_id,
            s.customer_id,
            s.retail_customer_id,
            s.sale_type,
            s.invoice_no,
            s.sold_price,
            s.paid_amount,
            s.due_amount,
            s.sold_at,
            s.payment_status,
            p.imei,
            p.brand,
            p.model,
            p.category,
            p.purchase_price,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS party_name
        FROM sales s
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        WHERE s.is_active = 1
          AND s.sold_at >= ?
          AND s.sold_at <= ?
        ORDER BY s.sold_at ASC, s.id ASC
        """,
        (lifetime_start_obj.isoformat(), end_date),
    ).fetchall()

    returns_raw = db.execute(
        """
        SELECT
            r.id,
            r.sale_id,
            r.return_date,
            r.reason,
            s.sale_type,
            s.sold_price,
            p.purchase_price,
            p.imei,
            p.brand,
            p.model,
            p.category,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS party_name
        FROM sale_returns r
        JOIN sales s ON s.id = r.sale_id
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        WHERE r.return_date >= ?
          AND r.return_date <= ?
        ORDER BY r.return_date ASC, r.id ASC
        """,
        (lifetime_start_obj.isoformat(), end_date),
    ).fetchall()

    collections_raw = db.execute(
        """
        SELECT
            dc.id,
            dc.sale_id,
            dc.customer_id,
            dc.amount,
            dc.collected_at,
            dc.method,
            dc.note,
            s.sale_type,
            p.imei,
            p.brand,
            p.model,
            p.category,
            c.shop_name AS party_name
        FROM due_collections dc
        JOIN sales s ON s.id = dc.sale_id
        LEFT JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = dc.customer_id
        WHERE dc.collected_at >= ?
          AND dc.collected_at <= ?
        ORDER BY dc.collected_at ASC, dc.id ASC
        """,
        (lifetime_start_obj.isoformat(), end_date),
    ).fetchall()

    expenses_raw = db.execute(
        """
        SELECT
            e.*,
            b.name AS branch_name,
            au.username AS approved_by_username
        FROM expenses e
        LEFT JOIN branches b ON b.id = e.branch_id
        LEFT JOIN users au ON au.id = e.approved_by_user_id
        WHERE e.expense_date >= ?
          AND e.expense_date <= ?
        ORDER BY e.expense_date ASC, e.id ASC
        """,
        (lifetime_start_obj.isoformat(), end_date),
    ).fetchall()

    sales_rows: list[dict[str, object]] = []
    for row in sales_raw:
        item = dict(row)
        item["business_module"] = infer_business_module_from_product(
            str(item.get("category") or ""),
            str(item.get("brand") or ""),
            str(item.get("model") or ""),
        )
        item["profit"] = float(item.get("sold_price") or 0) - float(item.get("purchase_price") or 0)
        sales_rows.append(item)

    return_rows: list[dict[str, object]] = []
    for row in returns_raw:
        item = dict(row)
        item["business_module"] = infer_business_module_from_product(
            str(item.get("category") or ""),
            str(item.get("brand") or ""),
            str(item.get("model") or ""),
        )
        item["return_loss"] = max(
            0.0,
            float(item.get("sold_price") or 0) - float(item.get("purchase_price") or 0),
        )
        return_rows.append(item)

    collection_rows: list[dict[str, object]] = []
    for row in collections_raw:
        item = dict(row)
        item["business_module"] = infer_business_module_from_product(
            str(item.get("category") or ""),
            str(item.get("brand") or ""),
            str(item.get("model") or ""),
        )
        collection_rows.append(item)

    expense_rows: list[dict[str, object]] = [dict(row) for row in expenses_raw]

    def pass_sales_filters(item: dict[str, object]) -> bool:
        if sale_type_filter != "ALL" and str(item.get("sale_type") or "").upper() != sale_type_filter:
            return False
        if module_filter != "ALL" and str(item.get("business_module") or "") != module_filter:
            return False
        return True

    def pass_expense_branch_filter(item: dict[str, object]) -> bool:
        if branch_filter == "ALL":
            return True
        return str(int(item.get("branch_id") or 1)) == branch_filter

    def in_range(value: str, from_date: str, to_date: str) -> bool:
        clean = (value or "").strip()
        return bool(clean) and from_date <= clean <= to_date

    def compute_metrics(from_date: str, to_date: str) -> dict[str, float]:
        scoped_sales = [
            item for item in sales_rows if in_range(str(item.get("sold_at") or ""), from_date, to_date) and pass_sales_filters(item)
        ]
        scoped_returns = [
            item for item in return_rows if in_range(str(item.get("return_date") or ""), from_date, to_date) and pass_sales_filters(item)
        ]
        scoped_collections = [
            item
            for item in collection_rows
            if in_range(str(item.get("collected_at") or ""), from_date, to_date) and pass_sales_filters(item)
        ]
        scoped_expenses = [
            item
            for item in expense_rows
            if in_range(str(item.get("expense_date") or ""), from_date, to_date)
            and pass_expense_branch_filter(item)
            and str(item.get("approval_status") or "").upper() == "APPROVED"
        ]
        gross_profit = sum(float(item.get("profit") or 0) for item in scoped_sales)
        revenue = sum(float(item.get("sold_price") or 0) for item in scoped_sales)
        return_loss = sum(float(item.get("return_loss") or 0) for item in scoped_returns)
        approved_expense = sum(float(item.get("amount") or 0) for item in scoped_expenses)
        due_collection = sum(float(item.get("amount") or 0) for item in scoped_collections)
        cash_sale = sum(float(item.get("paid_amount") or 0) for item in scoped_sales)
        cash_expense = sum(
            float(item.get("amount") or 0)
            for item in scoped_expenses
            if str(item.get("payment_method") or "").upper() == "CASH"
        )
        due_outstanding = sum(
            float(item.get("due_amount") or 0)
            for item in sales_rows
            if item.get("due_amount")
            and float(item.get("due_amount") or 0) > 0
            and str(item.get("sold_at") or "") <= to_date
            and pass_sales_filters(item)
        )
        net_profit = gross_profit - approved_expense - return_loss
        cashflow = cash_sale + due_collection - cash_expense
        return {
            "units": float(len(scoped_sales)),
            "revenue": revenue,
            "gross_profit": gross_profit,
            "expense": approved_expense,
            "return_loss": return_loss,
            "net_profit": net_profit,
            "due_collection": due_collection,
            "due_outstanding": due_outstanding,
            "cash_sale": cash_sale,
            "cash_expense": cash_expense,
            "cashflow": cashflow,
        }

    today_range = (today_iso, today_iso)
    mtd_range = (today_obj.replace(day=1).isoformat(), today_iso)
    ytd_range = (today_obj.replace(month=1, day=1).isoformat(), today_iso)
    life_range = (lifetime_start_obj.isoformat(), today_iso)

    executive_cards = {
        "today": compute_metrics(*today_range),
        "mtd": compute_metrics(*mtd_range),
        "ytd": compute_metrics(*ytd_range),
        "lifetime": compute_metrics(*life_range),
    }
    selected_metrics = compute_metrics(start_date, end_date)

    selected_sales = [
        item for item in sales_rows if in_range(str(item.get("sold_at") or ""), start_date, end_date) and pass_sales_filters(item)
    ]
    selected_returns = [
        item
        for item in return_rows
        if in_range(str(item.get("return_date") or ""), start_date, end_date) and pass_sales_filters(item)
    ]
    selected_collections = [
        item
        for item in collection_rows
        if in_range(str(item.get("collected_at") or ""), start_date, end_date) and pass_sales_filters(item)
    ]
    selected_expenses = [
        item
        for item in expense_rows
        if in_range(str(item.get("expense_date") or ""), start_date, end_date)
        and pass_expense_branch_filter(item)
        and str(item.get("approval_status") or "").upper() == "APPROVED"
    ]

    wholesale_profit = sum(float(item.get("profit") or 0) for item in selected_sales if item.get("sale_type") == "WHOLESALE")
    wholesale_revenue = sum(
        float(item.get("sold_price") or 0) for item in selected_sales if item.get("sale_type") == "WHOLESALE"
    )
    retail_profit = sum(float(item.get("profit") or 0) for item in selected_sales if item.get("sale_type") == "RETAIL")
    retail_revenue = sum(float(item.get("sold_price") or 0) for item in selected_sales if item.get("sale_type") == "RETAIL")
    due_collection_total = sum(float(item.get("amount") or 0) for item in selected_collections)
    return_loss_total = sum(float(item.get("return_loss") or 0) for item in selected_returns)

    section_metrics = [
        {
            "key": "WHOLESALE",
            "label": "Wholesale",
            "revenue": wholesale_revenue,
            "profit": wholesale_profit,
            "net_impact": wholesale_profit,
            "drill_kind": "section",
            "drill_key": "WHOLESALE",
        },
        {
            "key": "RETAIL",
            "label": "Retail POS",
            "revenue": retail_revenue,
            "profit": retail_profit,
            "net_impact": retail_profit,
            "drill_kind": "section",
            "drill_key": "RETAIL",
        },
        {
            "key": "RETURNS",
            "label": "Returns",
            "revenue": 0.0,
            "profit": -return_loss_total,
            "net_impact": -return_loss_total,
            "drill_kind": "section",
            "drill_key": "RETURN",
        },
        {
            "key": "DUE_COLLECTION",
            "label": "Due Collection",
            "revenue": due_collection_total,
            "profit": due_collection_total,
            "net_impact": due_collection_total,
            "drill_kind": "section",
            "drill_key": "DUE_COLLECTION",
        },
        {
            "key": "EXPENSE",
            "label": "Expense",
            "revenue": 0.0,
            "profit": -sum(float(item.get("amount") or 0) for item in selected_expenses),
            "net_impact": -sum(float(item.get("amount") or 0) for item in selected_expenses),
            "drill_kind": "section",
            "drill_key": "EXPENSE",
        },
    ]

    brand_bucket: dict[str, dict[str, float]] = {}
    category_bucket: dict[str, dict[str, float]] = {}
    for item in selected_sales:
        brand_name = str(item.get("brand") or "Unknown").strip() or "Unknown"
        category_name = str(item.get("category") or "").strip() or "Uncategorized"
        brand_bucket.setdefault(brand_name, {"units": 0.0, "revenue": 0.0, "profit": 0.0})
        category_bucket.setdefault(category_name, {"units": 0.0, "revenue": 0.0, "profit": 0.0})
        brand_bucket[brand_name]["units"] += 1
        brand_bucket[brand_name]["revenue"] += float(item.get("sold_price") or 0)
        brand_bucket[brand_name]["profit"] += float(item.get("profit") or 0)
        category_bucket[category_name]["units"] += 1
        category_bucket[category_name]["revenue"] += float(item.get("sold_price") or 0)
        category_bucket[category_name]["profit"] += float(item.get("profit") or 0)

    brand_profit = [
        {
            "label": label,
            "units": int(values["units"]),
            "revenue": values["revenue"],
            "profit": values["profit"],
            "profit_ratio": (values["profit"] / values["revenue"] * 100) if values["revenue"] else 0,
        }
        for label, values in brand_bucket.items()
    ]
    brand_profit.sort(key=lambda item: item["profit"], reverse=True)

    category_profit = [
        {
            "label": label,
            "units": int(values["units"]),
            "revenue": values["revenue"],
            "profit": values["profit"],
            "profit_ratio": (values["profit"] / values["revenue"] * 100) if values["revenue"] else 0,
        }
        for label, values in category_bucket.items()
    ]
    category_profit.sort(key=lambda item: item["profit"], reverse=True)

    partner_map: dict[str, dict[str, float]] = {}
    for item in selected_sales:
        if str(item.get("sale_type") or "") != "WHOLESALE":
            continue
        party = str(item.get("party_name") or "Unknown Shop").strip() or "Unknown Shop"
        block = partner_map.setdefault(
            party,
            {"units": 0.0, "revenue": 0.0, "profit": 0.0, "due_amount": 0.0, "returns": 0.0},
        )
        block["units"] += 1
        block["revenue"] += float(item.get("sold_price") or 0)
        block["profit"] += float(item.get("profit") or 0)
        block["due_amount"] += float(item.get("due_amount") or 0)
    for item in selected_returns:
        if str(item.get("sale_type") or "") != "WHOLESALE":
            continue
        party = str(item.get("party_name") or "Unknown Shop").strip() or "Unknown Shop"
        block = partner_map.setdefault(
            party,
            {"units": 0.0, "revenue": 0.0, "profit": 0.0, "due_amount": 0.0, "returns": 0.0},
        )
        block["returns"] += 1

    partner_profit = [
        {
            "label": label,
            "units": int(values["units"]),
            "revenue": values["revenue"],
            "profit": values["profit"],
            "due_amount": values["due_amount"],
            "returns": int(values["returns"]),
        }
        for label, values in partner_map.items()
    ]
    partner_profit.sort(key=lambda item: item["profit"], reverse=True)

    model_sales_map: dict[str, float] = {}
    model_profit_map: dict[str, float] = {}
    model_return_map: dict[str, float] = {}
    model_due_map: dict[str, float] = {}
    for item in selected_sales:
        key = f"{str(item.get('brand') or '').strip()} {str(item.get('model') or '').strip()}".strip() or "Unknown Model"
        model_sales_map[key] = model_sales_map.get(key, 0.0) + 1
        model_profit_map[key] = model_profit_map.get(key, 0.0) + float(item.get("profit") or 0)
        model_due_map[key] = model_due_map.get(key, 0.0) + float(item.get("due_amount") or 0)
    for item in selected_returns:
        key = f"{str(item.get('brand') or '').strip()} {str(item.get('model') or '').strip()}".strip() or "Unknown Model"
        model_return_map[key] = model_return_map.get(key, 0.0) + 1

    high_return_rate_models = []
    for key, sold_units in model_sales_map.items():
        if sold_units <= 0:
            continue
        returns_count = model_return_map.get(key, 0.0)
        rate = (returns_count / sold_units) * 100
        if returns_count > 0:
            high_return_rate_models.append(
                {
                    "model": key,
                    "sold_units": int(sold_units),
                    "returns_count": int(returns_count),
                    "return_rate": rate,
                }
            )
    high_return_rate_models.sort(key=lambda item: item["return_rate"], reverse=True)
    high_return_rate_models = high_return_rate_models[:5]

    low_margin_alerts = []
    for key, sold_units in model_sales_map.items():
        revenue = sum(
            float(item.get("sold_price") or 0)
            for item in selected_sales
            if f"{str(item.get('brand') or '').strip()} {str(item.get('model') or '').strip()}".strip() == key
        )
        if sold_units < 1 or revenue <= 0:
            continue
        profit = model_profit_map.get(key, 0.0)
        margin = (profit / revenue) * 100
        if margin < 3:
            low_margin_alerts.append(
                {
                    "model": key,
                    "units": int(sold_units),
                    "revenue": revenue,
                    "profit": profit,
                    "margin": margin,
                }
            )
    low_margin_alerts.sort(key=lambda item: item["margin"])
    low_margin_alerts = low_margin_alerts[:5]

    due_risk_map: dict[str, float] = {}
    for item in sales_rows:
        if str(item.get("sale_type") or "") != "WHOLESALE":
            continue
        if not pass_sales_filters(item):
            continue
        due_amount = float(item.get("due_amount") or 0)
        if due_amount <= 0:
            continue
        if str(item.get("sold_at") or "") > end_date:
            continue
        party = str(item.get("party_name") or "Unknown Shop").strip() or "Unknown Shop"
        due_risk_map[party] = due_risk_map.get(party, 0.0) + due_amount
    high_due_risk_shops = [
        {"shop_name": key, "due_amount": value}
        for key, value in sorted(due_risk_map.items(), key=lambda pair: pair[1], reverse=True)[:5]
    ]

    top_profitable_sections = sorted(section_metrics, key=lambda item: item["net_impact"], reverse=True)[:5]
    top_loss_sections = sorted(section_metrics, key=lambda item: item["net_impact"])[:5]

    total_days = (end_obj - start_obj).days + 1
    trend_start_obj = start_obj
    if total_days > 120:
        trend_start_obj = end_obj - timedelta(days=119)
    trend_start = trend_start_obj.isoformat()

    trend_sales = [item for item in selected_sales if str(item.get("sold_at") or "") >= trend_start]
    trend_returns = [item for item in selected_returns if str(item.get("return_date") or "") >= trend_start]
    trend_expenses = [item for item in selected_expenses if str(item.get("expense_date") or "") >= trend_start]
    trend_collections = [item for item in selected_collections if str(item.get("collected_at") or "") >= trend_start]

    revenue_by_day: dict[str, float] = {}
    profit_by_day: dict[str, float] = {}
    cash_sale_by_day: dict[str, float] = {}
    expense_by_day: dict[str, float] = {}
    due_collection_by_day: dict[str, float] = {}
    return_loss_by_day: dict[str, float] = {}
    cash_expense_by_day: dict[str, float] = {}

    for item in trend_sales:
        key = str(item.get("sold_at") or "")
        revenue_by_day[key] = revenue_by_day.get(key, 0.0) + float(item.get("sold_price") or 0)
        profit_by_day[key] = profit_by_day.get(key, 0.0) + float(item.get("profit") or 0)
        cash_sale_by_day[key] = cash_sale_by_day.get(key, 0.0) + float(item.get("paid_amount") or 0)
    for item in trend_expenses:
        key = str(item.get("expense_date") or "")
        amount = float(item.get("amount") or 0)
        expense_by_day[key] = expense_by_day.get(key, 0.0) + amount
        if str(item.get("payment_method") or "").upper() == "CASH":
            cash_expense_by_day[key] = cash_expense_by_day.get(key, 0.0) + amount
    for item in trend_collections:
        key = str(item.get("collected_at") or "")
        due_collection_by_day[key] = due_collection_by_day.get(key, 0.0) + float(item.get("amount") or 0)
    for item in trend_returns:
        key = str(item.get("return_date") or "")
        return_loss_by_day[key] = return_loss_by_day.get(key, 0.0) + float(item.get("return_loss") or 0)

    trend_labels: list[str] = []
    trend_revenue: list[float] = []
    trend_gross_profit: list[float] = []
    trend_expense: list[float] = []
    trend_net_profit: list[float] = []
    trend_cashflow: list[float] = []
    weekday_sum = [0.0 for _ in range(7)]
    weekday_count = [0 for _ in range(7)]

    cursor = trend_start_obj
    while cursor <= end_obj:
        key = cursor.isoformat()
        rev = revenue_by_day.get(key, 0.0)
        gp = profit_by_day.get(key, 0.0)
        exp = expense_by_day.get(key, 0.0)
        ret_loss = return_loss_by_day.get(key, 0.0)
        due_col = due_collection_by_day.get(key, 0.0)
        cash_exp = cash_expense_by_day.get(key, 0.0)
        net = gp - exp - ret_loss
        cashflow = cash_sale_by_day.get(key, 0.0) + due_col - cash_exp

        trend_labels.append(key)
        trend_revenue.append(round(rev, 2))
        trend_gross_profit.append(round(gp, 2))
        trend_expense.append(round(exp, 2))
        trend_net_profit.append(round(net, 2))
        trend_cashflow.append(round(cashflow, 2))

        weekday_index = cursor.weekday()
        weekday_sum[weekday_index] += cashflow
        weekday_count[weekday_index] += 1
        cursor += timedelta(days=1)

    weekday_heatmap = []
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for idx, name in enumerate(weekday_names):
        avg = (weekday_sum[idx] / weekday_count[idx]) if weekday_count[idx] else 0.0
        weekday_heatmap.append({"weekday": name, "avg_cashflow": round(avg, 2), "days": weekday_count[idx]})

    drill_kind = request.args.get("drill_kind", "").strip().lower()
    drill_key = request.args.get("drill_key", "").strip()
    drill_title = ""
    drill_rows: list[dict[str, object]] = []

    if drill_kind == "section":
        key = drill_key.upper()
        if key in {"WHOLESALE", "RETAIL"}:
            drill_title = f"{key.title()} Transactions"
            for item in selected_sales:
                if str(item.get("sale_type") or "").upper() != key:
                    continue
                drill_rows.append(
                    {
                        "date": item.get("sold_at"),
                        "ref": item.get("invoice_no") or item.get("imei"),
                        "type": item.get("sale_type"),
                        "party": item.get("party_name"),
                        "revenue": float(item.get("sold_price") or 0),
                        "profit": float(item.get("profit") or 0),
                        "note": "",
                    }
                )
        elif key == "RETURN":
            drill_title = "Return Transactions"
            for item in selected_returns:
                drill_rows.append(
                    {
                        "date": item.get("return_date"),
                        "ref": item.get("imei"),
                        "type": "RETURN",
                        "party": item.get("party_name"),
                        "revenue": -float(item.get("sold_price") or 0),
                        "profit": -float(item.get("return_loss") or 0),
                        "note": item.get("reason") or "",
                    }
                )
        elif key == "DUE_COLLECTION":
            drill_title = "Due Collection Transactions"
            for item in selected_collections:
                drill_rows.append(
                    {
                        "date": item.get("collected_at"),
                        "ref": item.get("imei"),
                        "type": "DUE_COLLECTION",
                        "party": item.get("party_name"),
                        "revenue": float(item.get("amount") or 0),
                        "profit": float(item.get("amount") or 0),
                        "note": item.get("method") or "",
                    }
                )
        elif key == "EXPENSE":
            drill_title = "Approved Expense Transactions"
            for item in selected_expenses:
                drill_rows.append(
                    {
                        "date": item.get("expense_date"),
                        "ref": f"EXP-{item.get('id')}",
                        "type": "EXPENSE",
                        "party": item.get("branch_name") or "Main Branch",
                        "revenue": -float(item.get("amount") or 0),
                        "profit": -float(item.get("amount") or 0),
                        "note": item.get("category") or "",
                    }
                )
    elif drill_kind == "brand" and drill_key:
        drill_title = f"Brand Transactions: {drill_key}"
        for item in selected_sales:
            if str(item.get("brand") or "").strip() != drill_key:
                continue
            drill_rows.append(
                {
                    "date": item.get("sold_at"),
                    "ref": item.get("invoice_no") or item.get("imei"),
                    "type": item.get("sale_type"),
                    "party": item.get("party_name"),
                    "revenue": float(item.get("sold_price") or 0),
                    "profit": float(item.get("profit") or 0),
                    "note": f"{item.get('brand')} {item.get('model')}",
                }
            )
    elif drill_kind == "category" and drill_key:
        drill_title = f"Category Transactions: {drill_key}"
        for item in selected_sales:
            category_name = str(item.get("category") or "").strip() or "Uncategorized"
            if category_name != drill_key:
                continue
            drill_rows.append(
                {
                    "date": item.get("sold_at"),
                    "ref": item.get("invoice_no") or item.get("imei"),
                    "type": item.get("sale_type"),
                    "party": item.get("party_name"),
                    "revenue": float(item.get("sold_price") or 0),
                    "profit": float(item.get("profit") or 0),
                    "note": f"{item.get('brand')} {item.get('model')}",
                }
            )
    elif drill_kind == "partner" and drill_key:
        drill_title = f"Partner Transactions: {drill_key}"
        for item in selected_sales:
            if str(item.get("sale_type") or "").upper() != "WHOLESALE":
                continue
            if str(item.get("party_name") or "").strip() != drill_key:
                continue
            drill_rows.append(
                {
                    "date": item.get("sold_at"),
                    "ref": item.get("invoice_no") or item.get("imei"),
                    "type": "WHOLESALE",
                    "party": item.get("party_name"),
                    "revenue": float(item.get("sold_price") or 0),
                    "profit": float(item.get("profit") or 0),
                    "note": f"{item.get('brand')} {item.get('model')}",
                }
            )

    drill_rows = sorted(drill_rows, key=lambda item: str(item.get("date") or ""), reverse=True)[:400]

    period_label = {
        "today": "Today",
        "week": "This Week",
        "month": "This Month",
        "year": "This Year",
        "custom": "Custom Range",
        "lifetime": "Lifetime",
    }.get(preset, "This Month")

    quick_range_links = [
        {"preset": "today", "label": "Today"},
        {"preset": "week", "label": "This Week"},
        {"preset": "month", "label": "This Month"},
        {"preset": "year", "label": "This Year"},
    ]

    return render_template(
        "reports.html",
        period=period or preset.upper(),
        period_label=period_label,
        preset=preset,
        date_from=start_date,
        date_to=end_date,
        branch_filter=branch_filter,
        sale_type_filter=sale_type_filter,
        module_filter=module_filter,
        branches=branches,
        module_options=module_options,
        quick_range_links=quick_range_links,
        summary={
            "units": int(selected_metrics["units"]),
            "revenue": selected_metrics["revenue"],
            "profit": selected_metrics["gross_profit"],
        },
        executive_cards=executive_cards,
        selected_metrics=selected_metrics,
        section_metrics=section_metrics,
        brand_profit=brand_profit,
        category_profit=category_profit,
        customer_profit=partner_profit,
        partner_profit=partner_profit,
        top_profitable_sections=top_profitable_sections,
        top_loss_sections=top_loss_sections,
        high_due_risk_shops=high_due_risk_shops,
        high_return_rate_models=high_return_rate_models,
        low_margin_alerts=low_margin_alerts,
        trend_labels=trend_labels,
        trend_revenue=trend_revenue,
        trend_gross_profit=trend_gross_profit,
        trend_expense=trend_expense,
        trend_net_profit=trend_net_profit,
        trend_cashflow=trend_cashflow,
        weekday_heatmap=weekday_heatmap,
        drill_kind=drill_kind,
        drill_key=drill_key,
        drill_title=drill_title,
        drill_rows=drill_rows,
        trend_start=trend_start,
    )


@app.get("/reports/export.csv")
def reports_export_csv():
    db = get_db()
    ensure_expense_finance_tables(db)
    today_obj = date.today()
    preset = request.args.get("preset", "").strip().lower()
    period = request.args.get("period", "").strip().upper()
    if not preset:
        preset = {
            "TODAY": "today",
            "MONTH": "month",
            "YEAR": "year",
            "LIFETIME": "lifetime",
    }.get(period, "month")
    if preset not in {"today", "week", "month", "year", "custom", "lifetime"}:
        preset = "month"

    tenant = get_current_tenant()
    is_pocket_module = bool(tenant is not None and get_tenant_default_endpoint(tenant) == "money_center")

    if is_pocket_module:
        earliest_income = db.execute("SELECT MIN(income_date) AS d FROM incomes").fetchone()
        earliest_expense = db.execute("SELECT MIN(expense_date) AS d FROM expenses").fetchone()
        earliest_dates = [
            str(item["d"])
            for item in (earliest_income, earliest_expense)
            if item is not None and item["d"]
        ]
        lifetime_start_obj = min((date.fromisoformat(item) for item in earliest_dates), default=today_obj)
    else:
        earliest_sales = db.execute("SELECT MIN(sold_at) AS d FROM sales").fetchone()
        earliest_date = (
            str(earliest_sales["d"]) if earliest_sales is not None and earliest_sales["d"] else today_obj.isoformat()
        )
        lifetime_start_obj = parse_iso_date(earliest_date) or today_obj

    req_start_obj = parse_iso_date(request.args.get("date_from", "").strip())
    req_end_obj = parse_iso_date(request.args.get("date_to", "").strip())

    if preset == "today":
        start_obj = today_obj
        end_obj = today_obj
    elif preset == "week":
        start_obj = today_obj - timedelta(days=today_obj.weekday())
        end_obj = today_obj
    elif preset == "month":
        start_obj = today_obj.replace(day=1)
        end_obj = today_obj
    elif preset == "year":
        start_obj = today_obj.replace(month=1, day=1)
        end_obj = today_obj
    elif preset == "lifetime":
        start_obj = lifetime_start_obj
        end_obj = today_obj
    else:
        start_obj = req_start_obj or today_obj.replace(day=1)
        end_obj = req_end_obj or today_obj

    if end_obj < start_obj:
        start_obj, end_obj = end_obj, start_obj

    start_date = start_obj.isoformat()
    end_date = end_obj.isoformat()

    if is_pocket_module:
        branches = db.execute(
            """
            SELECT id
            FROM branches
            """
        ).fetchall()
        branch_filter = request.args.get("branch_id", "ALL").strip().upper()
        valid_branch_values = {"ALL"} | {str(int(item["id"])) for item in branches}
        if branch_filter not in valid_branch_values:
            branch_filter = "ALL"

        status_filter = request.args.get("status", "ALL").strip().upper()
        if status_filter not in {"ALL", "PENDING", "APPROVED", "REJECTED"}:
            status_filter = "ALL"

        branch_sql = ""
        branch_params: list[object] = []
        if branch_filter != "ALL":
            branch_sql = " AND branch_id = ?"
            branch_params.append(int(branch_filter))

        status_sql = ""
        status_params: list[object] = []
        if status_filter != "ALL":
            status_sql = " AND approval_status = ?"
            status_params.append(status_filter)

        income_rows = db.execute(
            f"""
            SELECT
                income_date AS entry_date,
                category,
                sub_category,
                source_name,
                amount,
                payment_method,
                approval_status,
                note
            FROM incomes
            WHERE income_date BETWEEN ? AND ?
              {branch_sql}
              {status_sql}
            ORDER BY income_date DESC, id DESC
            """,
            tuple([start_date, end_date, *branch_params, *status_params]),
        ).fetchall()
        expense_rows = db.execute(
            f"""
            SELECT
                expense_date AS entry_date,
                category,
                sub_category,
                employee_name AS source_name,
                amount,
                payment_method,
                approval_status,
                note
            FROM expenses
            WHERE expense_date BETWEEN ? AND ?
              {branch_sql}
              {status_sql}
            ORDER BY expense_date DESC, id DESC
            """,
            tuple([start_date, end_date, *branch_params, *status_params]),
        ).fetchall()

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "entry_type",
                "date",
                "category",
                "sub_category",
                "source",
                "payment_method",
                "status",
                "amount",
                "note",
            ]
        )
        for row in income_rows:
            writer.writerow(
                [
                    "income",
                    row["entry_date"],
                    row["category"] or "",
                    row["sub_category"] or "",
                    row["source_name"] or "",
                    row["payment_method"] or "",
                    row["approval_status"] or "",
                    row["amount"] or 0,
                    row["note"] or "",
                ]
            )
        for row in expense_rows:
            writer.writerow(
                [
                    "expense",
                    row["entry_date"],
                    row["category"] or "",
                    row["sub_category"] or "",
                    row["source_name"] or "",
                    row["payment_method"] or "",
                    row["approval_status"] or "",
                    row["amount"] or 0,
                    row["note"] or "",
                ]
            )

        output = make_response(buffer.getvalue())
        output.headers["Content-Type"] = "text/csv; charset=utf-8"
        output.headers["Content-Disposition"] = f'attachment; filename="money-report-{preset}.csv"'
        return output

    sale_type_filter = request.args.get("sale_type", "ALL").strip().upper()
    if sale_type_filter not in {"ALL", "WHOLESALE", "RETAIL"}:
        sale_type_filter = "ALL"

    module_filter = request.args.get("module", "ALL").strip().upper()
    module_options = [{"key": "ALL", "label_en": "All Modules", "label_bn": "সব মডিউল"}]
    module_options.extend(get_business_module_options())
    valid_modules = {item["key"] for item in module_options}
    if module_filter not in valid_modules:
        module_filter = "ALL"

    rows_raw = db.execute(
        """
        SELECT
            s.sold_at,
            s.invoice_no,
            p.imei,
            p.brand,
            p.model,
            p.category,
            CASE
                WHEN s.sale_type = 'RETAIL'
                    THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                ELSE c.shop_name
            END AS shop_name,
            s.sale_type,
            p.purchase_price,
            s.sold_price,
            s.paid_amount,
            s.due_amount,
            (s.sold_price - p.purchase_price) AS profit,
            s.payment_status
        FROM sales s
        JOIN products p ON p.id = s.product_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN retail_customers rc ON rc.id = s.retail_customer_id
        WHERE s.is_active = 1
          AND s.sold_at >= ?
          AND s.sold_at <= ?
        ORDER BY s.sold_at DESC, s.id DESC
        """,
        (start_date, end_date),
    ).fetchall()

    rows = []
    for row in rows_raw:
        item = dict(row)
        item["business_module"] = infer_business_module_from_product(
            str(item.get("category") or ""),
            str(item.get("brand") or ""),
            str(item.get("model") or ""),
        )
        if sale_type_filter != "ALL" and str(item.get("sale_type") or "").upper() != sale_type_filter:
            continue
        if module_filter != "ALL" and str(item.get("business_module") or "") != module_filter:
            continue
        rows.append(item)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "date",
            "invoice_no",
            "imei",
            "brand",
            "model",
            "category",
            "business_module",
            "customer",
            "sale_type",
            "purchase_price",
            "sold_price",
            "paid_amount",
            "due_amount",
            "profit",
            "payment_status",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["sold_at"],
                row["invoice_no"] or "",
                row["imei"],
                row["brand"],
                row["model"],
                row["category"] or "",
                row["business_module"],
                row["shop_name"] or "",
                row["sale_type"],
                row["purchase_price"],
                row["sold_price"],
                row["paid_amount"],
                row["due_amount"],
                row["profit"],
                row["payment_status"],
            ]
        )

    output = make_response(buffer.getvalue())
    output.headers["Content-Type"] = "text/csv; charset=utf-8"
    output.headers["Content-Disposition"] = f'attachment; filename="profit-report-{preset}.csv"'
    return output


@app.get("/stock-report")
def stock_report():
    db = get_db()
    filters = resolve_stock_report_filters(
        db,
        q=request.args.get("q", ""),
        status=request.args.get("status", "IN_STOCK"),
        brand=request.args.get("brand", "ALL"),
        category=request.args.get("category", "ALL"),
        supplier_id_raw=request.args.get("supplier_id", ""),
        sort=request.args.get("sort", "received_desc"),
        limit_raw=request.args.get("limit", ""),
    )

    rows = fetch_stock_report_rows(
        db,
        q=str(filters["q"]),
        status_filter=str(filters["status_filter"]),
        brand_filter=str(filters["brand_filter"]),
        category_filter=str(filters["category_filter"]),
        supplier_id=int(filters["supplier_id"]) if filters["supplier_id"] is not None else None,
        sort_key=str(filters["sort_key"]),
        limit=int(filters["limit"]),
    )
    summary = build_stock_report_summary(rows)
    model_summary = build_stock_model_summary(rows)
    due_risk_shops = build_stock_due_risk_shops(rows)

    today_obj = date.today()
    detailed_rows: list[dict[str, object]] = []
    stale_in_stock_rows: list[dict[str, object]] = []
    low_margin_sales: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        received_obj = parse_iso_date(str(item.get("received_date") or "").strip())
        age_days = max(0, (today_obj - received_obj).days) if received_obj is not None else None
        purchase_price = float(item.get("purchase_price") or 0)
        sold_price = float(item.get("sold_price") or 0)
        realized_profit = sold_price - purchase_price if sold_price > 0 else None
        realized_margin = (
            (realized_profit / sold_price * 100.0)
            if realized_profit is not None and sold_price > 0
            else None
        )
        item["age_days"] = age_days
        item["realized_profit"] = realized_profit
        item["realized_margin"] = realized_margin
        item["potential_wholesale_profit"] = float(item.get("wholesale_price") or 0) - purchase_price
        item["potential_retail_profit"] = float(item.get("retail_price") or 0) - purchase_price
        detailed_rows.append(item)

        if str(item.get("status") or "").upper() == "IN_STOCK" and age_days is not None:
            stale_in_stock_rows.append(item)
        if (
            str(item.get("status") or "").upper() == "SOLD"
            and realized_margin is not None
            and realized_margin < 3.0
        ):
            low_margin_sales.append(item)

    stale_in_stock_rows.sort(key=lambda entry: int(entry.get("age_days") or 0), reverse=True)
    low_margin_sales.sort(key=lambda entry: float(entry.get("realized_margin") or 0.0))

    brand_options = [
        str(row["brand"]).strip()
        for row in db.execute(
            """
            SELECT DISTINCT TRIM(brand) AS brand
            FROM products
            WHERE TRIM(COALESCE(brand, '')) <> ''
            ORDER BY UPPER(TRIM(brand)) ASC
            LIMIT 500
            """
        ).fetchall()
    ]
    category_options = [
        str(row["category"]).strip()
        for row in db.execute(
            """
            SELECT DISTINCT TRIM(COALESCE(category, '')) AS category
            FROM products
            WHERE TRIM(COALESCE(category, '')) <> ''
            ORDER BY UPPER(TRIM(COALESCE(category, ''))) ASC
            LIMIT 500
            """
        ).fetchall()
    ]
    suppliers = db.execute(
        """
        SELECT id, name
        FROM suppliers
        ORDER BY UPPER(name) ASC
        """
    ).fetchall()

    sort_options = [
        {"key": "received_desc", "label_en": "Newest Received", "label_bn": "সর্বশেষ ইন আগে"},
        {"key": "received_asc", "label_en": "Oldest Received", "label_bn": "পুরোনো ইন আগে"},
        {"key": "brand_asc", "label_en": "Brand A-Z", "label_bn": "ব্র্যান্ড A-Z"},
        {"key": "model_asc", "label_en": "Model A-Z", "label_bn": "মডেল A-Z"},
        {"key": "purchase_desc", "label_en": "Highest Cost", "label_bn": "সর্বোচ্চ ক্রয় দাম"},
        {"key": "wholesale_desc", "label_en": "Highest Wholesale", "label_bn": "সর্বোচ্চ হোলসেল দাম"},
        {"key": "retail_desc", "label_en": "Highest Retail", "label_bn": "সর্বোচ্চ রিটেইল দাম"},
        {"key": "due_desc", "label_en": "Highest Due", "label_bn": "সর্বোচ্চ বাকি"},
    ]
    limit_options = [300, 600, 1000, 2000, 5000]

    active_filter_count = 0
    if str(filters["q"]):
        active_filter_count += 1
    if str(filters["status_filter"]) != "IN_STOCK":
        active_filter_count += 1
    if str(filters["brand_filter"]).upper() != "ALL":
        active_filter_count += 1
    if str(filters["category_filter"]).upper() != "ALL":
        active_filter_count += 1
    if filters["supplier_id"] is not None:
        active_filter_count += 1
    if str(filters["sort_key"]) != "received_desc":
        active_filter_count += 1
    if int(filters["limit"]) != 2000:
        active_filter_count += 1

    return render_template(
        "stock_report.html",
        rows=detailed_rows,
        summary=summary,
        model_summary=model_summary,
        due_risk_shops=due_risk_shops,
        stale_in_stock_rows=stale_in_stock_rows[:20],
        low_margin_sales=low_margin_sales[:20],
        brand_options=brand_options,
        category_options=category_options,
        suppliers=suppliers,
        sort_options=sort_options,
        limit_options=limit_options,
        q=str(filters["q"]),
        status_filter=str(filters["status_filter"]),
        brand_filter=str(filters["brand_filter"]),
        category_filter=str(filters["category_filter"]),
        supplier_filter=int(filters["supplier_id"]) if filters["supplier_id"] is not None else "",
        sort_key=str(filters["sort_key"]),
        limit_value=int(filters["limit"]),
        active_filter_count=active_filter_count,
    )


@app.get("/stock-report/export.csv")
def stock_report_export_csv():
    db = get_db()
    filters = resolve_stock_report_filters(
        db,
        q=request.args.get("q", ""),
        status=request.args.get("status", "IN_STOCK"),
        brand=request.args.get("brand", "ALL"),
        category=request.args.get("category", "ALL"),
        supplier_id_raw=request.args.get("supplier_id", ""),
        sort=request.args.get("sort", "received_desc"),
        limit_raw=request.args.get("limit", "5000"),
    )

    rows = fetch_stock_report_rows(
        db,
        q=str(filters["q"]),
        status_filter=str(filters["status_filter"]),
        brand_filter=str(filters["brand_filter"]),
        category_filter=str(filters["category_filter"]),
        supplier_id=int(filters["supplier_id"]) if filters["supplier_id"] is not None else None,
        sort_key=str(filters["sort_key"]),
        limit=int(filters["limit"]),
    )

    today_obj = date.today()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "imei",
            "brand",
            "model",
            "storage",
            "color",
            "category",
            "warranty_type",
            "status",
            "supplier",
            "received_date",
            "age_days",
            "purchase_price",
            "wholesale_price",
            "retail_price",
            "active_invoice",
            "sold_to",
            "sale_type",
            "sold_date",
            "sold_price",
            "paid_amount",
            "due_amount",
            "payment_status",
            "realized_profit",
            "realized_margin_percent",
            "note",
        ]
    )
    for row in rows:
        received_obj = parse_iso_date(str(row["received_date"] or "").strip())
        age_days = max(0, (today_obj - received_obj).days) if received_obj is not None else ""
        purchase_price = float(row["purchase_price"] or 0)
        sold_price = float(row["sold_price"] or 0)
        realized_profit = sold_price - purchase_price if sold_price > 0 else ""
        realized_margin = ((sold_price - purchase_price) / sold_price * 100.0) if sold_price > 0 else ""
        writer.writerow(
            [
                row["imei"],
                row["brand"],
                row["model"],
                row["storage"] or "",
                row["color"] or "",
                row["category"] or "",
                row["warranty_type"] or "",
                row["status"],
                row["supplier_name"] or "",
                row["received_date"],
                age_days,
                row["purchase_price"],
                row["wholesale_price"],
                row["retail_price"],
                row["invoice_no"] or "",
                row["holder_name"] or "",
                row["sale_type"] or "",
                row["sold_at"] or "",
                row["sold_price"] or "",
                row["paid_amount"] or "",
                row["due_amount"] or "",
                row["payment_status"] or "",
                realized_profit,
                realized_margin,
                row["note"] or "",
            ]
        )

    output = make_response(buffer.getvalue())
    output.headers["Content-Type"] = "text/csv; charset=utf-8"
    output.headers["Content-Disposition"] = (
        f'attachment; filename="stock-report-{date.today().isoformat()}.csv"'
    )
    return output


@app.route("/backups", methods=["GET", "POST"])
def backups():
    db = get_db()
    tenant = get_current_tenant()
    current_user = get_current_tenant_user()
    is_admin_user = bool(current_user is not None and normalize_role(str(current_user["role"]), default="USER") == "ADMIN")
    backup_schedule = get_backup_schedule_settings(db)

    if request.method == "POST":
        action = request.form.get("action", "create_db_backup").strip().lower()
        sync_google = request.form.get("sync_google", "0") == "1"

        if not is_admin_user:
            flash("Only admin can manage backup export/import tools.", "error")
            return redirect(url_for("backups"))

        if action == "save_schedule":
            schedule_enabled = request.form.get("schedule_enabled", "0") in {"1", "on", "true", "yes"}
            schedule_backup_type = normalize_backup_schedule_type(request.form.get("schedule_backup_type", "PACKAGE"))
            schedule_frequency = normalize_backup_schedule_frequency(request.form.get("schedule_frequency", "DAILY"))
            run_time_raw = (request.form.get("schedule_run_time", "03:00") or "03:00").strip()
            if ":" in run_time_raw:
                raw_hour, raw_minute = run_time_raw.split(":", 1)
            else:
                raw_hour, raw_minute = "3", "0"
            run_hour = clamp_backup_schedule_hour(raw_hour, default=3)
            run_minute = clamp_backup_schedule_minute(raw_minute, default=0)
            weekly_day = normalize_backup_schedule_weekday(request.form.get("schedule_weekly_day", "SUN"))
            monthly_day = clamp_backup_schedule_month_day(request.form.get("schedule_monthly_day", "1"), default=1)
            schedule_sync_google = request.form.get("schedule_sync_google", "0") in {"1", "on", "true", "yes"}

            next_run_dt = compute_next_backup_run(
                {
                    "is_enabled": "1" if schedule_enabled else "0",
                    "backup_type": schedule_backup_type,
                    "frequency": schedule_frequency,
                    "run_hour": str(run_hour),
                    "run_minute": str(run_minute),
                    "weekly_day": weekly_day,
                    "monthly_day": str(monthly_day),
                    "sync_google": "1" if schedule_sync_google else "0",
                },
                reference_dt=datetime.now(),
            )
            save_backup_schedule_settings(
                db,
                is_enabled=schedule_enabled,
                backup_type=schedule_backup_type,
                frequency=schedule_frequency,
                run_hour=run_hour,
                run_minute=run_minute,
                weekly_day=weekly_day,
                monthly_day=monthly_day,
                sync_google=schedule_sync_google,
                next_run_at=next_run_dt.strftime("%Y-%m-%d %H:%M:%S") if next_run_dt is not None else None,
                last_error=row_value(backup_schedule, "last_error", ""),
            )
            db.commit()
            flash(
                (
                    "Scheduled auto backup updated. "
                    + (
                        f"Next run: {next_run_dt.strftime('%Y-%m-%d %H:%M:%S')}"
                        if next_run_dt is not None
                        else "Auto backup is paused."
                    )
                ),
                "success",
            )
            return redirect(url_for("backups"))

        if action == "run_scheduled_now":
            try:
                backup_path, google_status, google_message = execute_scheduled_backup(backup_schedule)
                next_run_dt = compute_next_backup_run(backup_schedule, reference_dt=datetime.now() + timedelta(minutes=1))
                save_backup_schedule_settings(
                    db,
                    is_enabled=row_value(backup_schedule, "is_enabled", "0") == "1",
                    backup_type=row_value(backup_schedule, "backup_type", "PACKAGE"),
                    frequency=row_value(backup_schedule, "frequency", "DAILY"),
                    run_hour=clamp_backup_schedule_hour(row_value(backup_schedule, "run_hour", "3")),
                    run_minute=clamp_backup_schedule_minute(row_value(backup_schedule, "run_minute", "0")),
                    weekly_day=row_value(backup_schedule, "weekly_day", "SUN"),
                    monthly_day=clamp_backup_schedule_month_day(row_value(backup_schedule, "monthly_day", "1")),
                    sync_google=row_value(backup_schedule, "sync_google", "0") == "1",
                    last_run_at=now_sqlite_text(),
                    next_run_at=next_run_dt.strftime("%Y-%m-%d %H:%M:%S") if next_run_dt is not None else None,
                    last_status=f"SUCCESS ({google_status})",
                    last_filename=backup_path.name,
                    last_error="",
                )
                db.commit()
                flash(f"Scheduled backup completed: {backup_path.name}. {google_message}", "success")
            except Exception as exc:
                next_run_dt = compute_next_backup_run(backup_schedule, reference_dt=datetime.now() + timedelta(minutes=1))
                save_backup_schedule_settings(
                    db,
                    is_enabled=row_value(backup_schedule, "is_enabled", "0") == "1",
                    backup_type=row_value(backup_schedule, "backup_type", "PACKAGE"),
                    frequency=row_value(backup_schedule, "frequency", "DAILY"),
                    run_hour=clamp_backup_schedule_hour(row_value(backup_schedule, "run_hour", "3")),
                    run_minute=clamp_backup_schedule_minute(row_value(backup_schedule, "run_minute", "0")),
                    weekly_day=row_value(backup_schedule, "weekly_day", "SUN"),
                    monthly_day=clamp_backup_schedule_month_day(row_value(backup_schedule, "monthly_day", "1")),
                    sync_google=row_value(backup_schedule, "sync_google", "0") == "1",
                    last_run_at=now_sqlite_text(),
                    next_run_at=next_run_dt.strftime("%Y-%m-%d %H:%M:%S") if next_run_dt is not None else None,
                    last_status="FAILED",
                    last_filename="",
                    last_error=str(exc)[:400],
                )
                db.commit()
                flash(f"Scheduled backup failed: {exc}", "error")
            return redirect(url_for("backups"))

        if action == "create_package":
            try:
                backup_path, google_status, google_message = create_tenant_backup_package(
                    trigger_type="EXPORT_PACKAGE",
                    sync_google=sync_google,
                )
                write_audit_log(
                    action="TENANT_BACKUP_PACKAGE_CREATED",
                    metadata={
                        "filename": backup_path.name,
                        "google_status": google_status,
                        "tenant_id": int(row_value(tenant, "id", "0") or "0") if tenant is not None else None,
                    },
                )
                if google_status in {"SYNCED", "NOT_SENT", "NOT_CONFIGURED"}:
                    flash(f"Full export package ready: {backup_path.name}. {google_message}", "success")
                else:
                    flash(f"Export package created locally: {backup_path.name}. Google sync: {google_message}", "error")
            except Exception as exc:
                flash(f"Export package failed: {exc}", "error")
            return redirect(url_for("backups"))

        if action == "import_package":
            upload_file = request.files.get("backup_file")
            if upload_file is None or not (upload_file.filename or "").strip():
                flash("Choose a Soft X backup package zip file first.", "error")
                return redirect(url_for("backups"))
            try:
                safety_backup_path, manifest_payload = restore_tenant_backup_package(upload_file)
                write_audit_log(
                    action="TENANT_BACKUP_PACKAGE_IMPORTED",
                    metadata={
                        "backup_username": (
                            str((manifest_payload.get("tenant") or {}).get("username") or "")
                            if isinstance(manifest_payload.get("tenant"), dict)
                            else ""
                        ),
                        "copied_files_count": int(manifest_payload.get("copied_files_count") or 0),
                        "safety_backup": safety_backup_path.name,
                    },
                )
                flash(
                    "Backup imported successfully. "
                    f"Automatic safety backup saved as {safety_backup_path.name}.",
                    "success",
                )
            except Exception as exc:
                flash(f"Backup import blocked: {exc}", "error")
            return redirect(url_for("backups"))

        backup_path, google_status, google_message = create_database_backup(
            trigger_type="MANUAL",
            sync_google=sync_google,
        )
        write_audit_log(
            action="TENANT_DB_BACKUP_CREATED",
            metadata={
                "filename": backup_path.name,
                "google_status": google_status,
            },
        )
        if google_status in {"SYNCED", "NOT_SENT", "NOT_CONFIGURED"}:
            flash(f"Database copy created: {backup_path.name}. {google_message}", "success")
        else:
            flash(f"Database copy created locally: {backup_path.name}. Google sync: {google_message}", "error")
        return redirect(url_for("backups"))

    logs = db.execute(
        """
        SELECT *
        FROM backup_logs
        ORDER BY id DESC
        LIMIT 200
        """
    ).fetchall()
    backup_schedule = get_backup_schedule_settings(db)
    backup_schedule_time_value = (
        f"{clamp_backup_schedule_hour(row_value(backup_schedule, 'run_hour', '3')):02d}:"
        f"{clamp_backup_schedule_minute(row_value(backup_schedule, 'run_minute', '0')):02d}"
    )
    backup_schedule_weekdays = [
        {"value": "MON", "label_en": "Monday", "label_bn": "সোমবার"},
        {"value": "TUE", "label_en": "Tuesday", "label_bn": "মঙ্গলবার"},
        {"value": "WED", "label_en": "Wednesday", "label_bn": "বুধবার"},
        {"value": "THU", "label_en": "Thursday", "label_bn": "বৃহস্পতিবার"},
        {"value": "FRI", "label_en": "Friday", "label_bn": "শুক্রবার"},
        {"value": "SAT", "label_en": "Saturday", "label_bn": "শনিবার"},
        {"value": "SUN", "label_en": "Sunday", "label_bn": "রবিবার"},
    ]

    return render_template(
        "backups.html",
        logs=logs,
        is_admin_user=is_admin_user,
        current_db_name=get_current_db_path().name,
        current_tenant_label=(row_value(tenant, "shop_name", "") or row_value(tenant, "owner_name", "") or "Soft X"),
        backup_schedule=backup_schedule,
        backup_schedule_time_value=backup_schedule_time_value,
        backup_schedule_weekdays=backup_schedule_weekdays,
        google_drive_ready=bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() and os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()),
    )


@app.get("/backups/download/<path:filename>")
def download_backup(filename: str):
    safe_name = Path(filename).name
    target = BACKUP_DIR / safe_name
    if not target.exists():
        flash("Backup file not found.", "error")
        return redirect(url_for("backups"))
    return send_from_directory(BACKUP_DIR, safe_name, as_attachment=True)


@app.get("/manifest.webmanifest")
def manifest_webmanifest():
    response = send_from_directory(str(BASE_DIR / "static"), "manifest.webmanifest")
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/service-worker.js")
def service_worker_js():
    response = send_from_directory(str(BASE_DIR / "static"), "service-worker.js")
    response.headers["Content-Type"] = "application/javascript; charset=utf-8"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/imei-lookup")
def imei_lookup_alias():
    return redirect(url_for("imei_lookup"))


@app.route("/imei")
def imei_lookup():
    db = get_db()
    tracking_mode = get_current_tracking_mode()
    imei = normalize_tracking_code(request.args.get("imei", "").strip(), tracking_mode)
    record = None
    sale_history: list[sqlite3.Row] = []
    stock_adjustments: list[sqlite3.Row] = []
    lookup_metrics = {
        "sales_count": 0,
        "return_count": 0,
        "adjustment_count": 0,
        "lifetime_profit": 0.0,
    }

    if imei:
        record = db.execute(
            """
            SELECT p.*, s.name AS supplier_name
            FROM products p
            LEFT JOIN suppliers s ON s.id = p.supplier_id
            WHERE p.imei = ?
            """,
            (imei,),
        ).fetchone()

        if record is not None:
            sale_history = db.execute(
                """
                SELECT
                    sl.*,
                    CASE
                        WHEN sl.sale_type = 'RETAIL'
                            THEN COALESCE(NULLIF(TRIM(rc.full_name), ''), c.shop_name, 'Retail Customer')
                        ELSE c.shop_name
                    END AS shop_name,
                    (sl.sold_price - p.purchase_price) AS profit,
                    r.return_date,
                    r.reason AS return_reason
                FROM sales sl
                JOIN products p ON p.id = sl.product_id
                LEFT JOIN customers c ON c.id = sl.customer_id
                LEFT JOIN retail_customers rc ON rc.id = sl.retail_customer_id
                LEFT JOIN sale_returns r ON r.sale_id = sl.id
                WHERE sl.product_id = ?
                ORDER BY sl.id DESC
                """,
                (record["id"],),
            ).fetchall()

            stock_adjustments = db.execute(
                """
                SELECT id, action, event_date, reason, created_at
                FROM stock_adjustments
                WHERE product_id = ?
                ORDER BY id DESC
                LIMIT 200
                """,
                (record["id"],),
            ).fetchall()
            lookup_metrics = {
                "sales_count": len(sale_history),
                "return_count": sum(1 for row in sale_history if str(row["return_date"] or "").strip()),
                "adjustment_count": len(stock_adjustments),
                "lifetime_profit": sum(
                    float(row["profit"] or 0)
                    for row in sale_history
                    if int(row["is_active"] or 0) == 1
                ),
            }

    return render_template(
        "imei_lookup.html",
        imei=imei,
        record=record,
        sale_history=sale_history,
        stock_adjustments=stock_adjustments,
        lookup_metrics=lookup_metrics,
    )


init_admin_db()
init_db()
ensure_daily_backup()
with app.app_context():
    run_subscription_automation(send_notifications=False)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wholesale Mobile Inventory")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "5000")))
    parser.add_argument("--backup", action="store_true", help="Create backup and exit")
    parser.add_argument("--sync-google", action="store_true", help="Sync backup to Google Drive")
    parser.add_argument("--worker", action="store_true", help="Run Redis queue worker")
    parser.add_argument("--worker-once", action="store_true", help="Run one queue job and exit")
    parser.add_argument(
        "--queue-job",
        choices=[
            "backup_create",
            "postgres_sync_main",
            "postgres_sync_all_tenants",
            "tenant_reindex_all",
        ],
        help="Enqueue a background job",
    )
    parser.add_argument("--migrate-postgres", action="store_true", help="Migrate SQLite DB to PostgreSQL")
    parser.add_argument("--sync-tenants-postgres", action="store_true", help="Sync all tenant DBs to PostgreSQL")
    parser.add_argument("--harden-tenant-indexes", action="store_true", help="Apply index hardening to all tenants")
    parser.add_argument(
        "--export-pocket-runtime",
        action="store_true",
        help="Export Pocket Pro main/admin/tenant runtime into one migration zip",
    )
    parser.add_argument(
        "--import-pocket-runtime",
        action="store_true",
        help="Import Pocket Pro main/admin/tenant runtime from one migration zip",
    )
    parser.add_argument(
        "--runtime-package",
        default="",
        help="Pocket Pro runtime zip path for --import-pocket-runtime",
    )
    parser.add_argument(
        "--sqlite-db",
        default="",
        help="SQLite DB path for migration (default: main INVENTORY_DB_PATH)",
    )
    parser.add_argument(
        "--postgres-schema",
        default=POSTGRES_SCHEMA,
        help="PostgreSQL schema name for migration",
    )
    parser.add_argument("--no-debug", action="store_true", help="Run without debug mode")
    args = parser.parse_args()

    if args.queue_job:
        if queue_push_job(args.queue_job):
            print(f"Queued job: {args.queue_job}")
        else:
            print("Failed to queue job. Check Redis config and SOFTX_REDIS_QUEUE_ENABLED=1")
    elif args.worker or args.worker_once:
        raise SystemExit(run_queue_worker(run_once=bool(args.worker_once)))
    elif args.harden_tenant_indexes:
        summary = harden_all_tenant_indexes()
        print(f"Tenant index hardening done. Success={summary['success']} Failed={summary['failed']}")
    elif args.sync_tenants_postgres:
        try:
            result = sync_all_tenants_to_postgres()
            print(f"Tenant PostgreSQL sync done. Success={result['success']} Failed={result['failed']}")
        except Exception as exc:
            print(f"Tenant PostgreSQL sync failed: {exc}")
            raise SystemExit(1)
    elif args.migrate_postgres:
        try:
            source_path = Path(args.sqlite_db).expanduser() if args.sqlite_db else DB_PATH
            result = export_sqlite_to_postgres(source_path, args.postgres_schema, truncate_before_load=True)
            print(
                "Main PostgreSQL migration done. "
                f"Schema={args.postgres_schema} Tables={result['tables']} Rows={result['rows']}"
            )
        except Exception as exc:
            print(f"Main PostgreSQL migration failed: {exc}")
            raise SystemExit(1)
    elif args.export_pocket_runtime:
        try:
            package_path = create_pocket_runtime_export_package()
            print(f"Pocket Pro runtime export ready: {package_path}")
            print("This zip contains main DB, admin DB, and all tenant DB files.")
        except Exception as exc:
            print(f"Pocket Pro runtime export failed: {exc}")
            raise SystemExit(1)
    elif args.import_pocket_runtime:
        if not args.runtime_package:
            print("--runtime-package is required with --import-pocket-runtime")
            raise SystemExit(1)
        try:
            safety_backup, result = restore_pocket_runtime_export_package(Path(args.runtime_package))
            print("Pocket Pro runtime import completed.")
            print(f"Main DB: {result['main_db']}")
            print(f"Admin DB: {result['admin_db']}")
            print(f"Tenant Dir: {result['tenant_dir']}")
            print(f"Tenant files copied: {result['tenant_file_count']}")
            if safety_backup is not None:
                print(f"Automatic safety backup: {safety_backup}")
        except Exception as exc:
            print(f"Pocket Pro runtime import failed: {exc}")
            raise SystemExit(1)
    elif args.backup:
        path, status, message = create_database_backup("CLI", sync_google=args.sync_google)
        print(f"Backup: {path}")
        print(f"Google status: {status}")
        print(message)
    else:
        app.run(debug=not args.no_debug, host=args.host, port=args.port)
