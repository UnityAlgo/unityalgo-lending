# migrate_lending_data.py
# Run with: bench --site your-site.com execute lending.migrate_lending_data.migrate
# Validate:  bench --site your-site.com execute lending.migrate_lending_data.validate

import frappe
from frappe.utils import nowdate
import openpyxl
import os
import traceback
import re
from collections import defaultdict

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

BRANCH = "Head Office - HO"
SALES_PERSON = "Shakeel"

DISBURSEMENT_ACCOUNT = "1320 - Loans Disbursed (Principal) - CFS"
LOAN_ACCOUNT = "1320 - Loans Disbursed (Principal) - CFS"
INTEREST_INCOME_ACCOUNT = "4130 - Interest Income - CFS"
INTEREST_RECEIVABLE_ACCOUNT = "1330 - Interest Receivable - CFS"
PENALTY_INCOME_ACCOUNT = "4140 - Penalty & Late Fee Income - CFS"
PENALTY_RECEIVABLE_ACCOUNT = "1340 - Penalty Receivable - CFS"
PAYMENT_ACCOUNT = "1100 - Cash In Hand - CFS"
MODE_OF_PAYMENT = "Cash"

REPAYMENT_METHOD = "Repay Over Number of Periods"
MIN_DAYS_FIRST_REPAYMENT = 30
MIN_AUTO_CLOSURE_TOLERANCE = 0
MAX_AUTO_CLOSURE_TOLERANCE = 100
PENALTY_INTEREST_METHOD = "Rate"
PENALTY_INTEREST_RATE = 0.0
PENALTY_STOP_THRESHOLD_DAYS = 90

# Default loan product for historical loans (not in disbursement file)
# These loans are closed — product is used for account linkage only
HISTORICAL_LOAN_PRODUCT = ""  # leave blank to auto-pick first available product

BASE_DIR = os.path.dirname(__file__)
CUSTOMER_FILE = os.path.join(BASE_DIR, "Customer Data.xlsx")
DISBURSEMENT_FILE = os.path.join(BASE_DIR, "Disbursement Data.xlsx")
REPAYMENT_FILE = os.path.join(BASE_DIR, "Repayment Data.xlsx")

COMPANY = ""

# ── Helpers ───────────────────────────────────────────────────────────────────


def read_excel(filepath):
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    first = str(rows[0][0] or "").strip()
    if len(first) > 15 or any(
        w in first for w in ["Ltd", "Services", "Admin", "Pvt", "Care"]
    ):
        header_row, data_rows = rows[1], rows[2:]
    else:
        header_row, data_rows = rows[0], rows[1:]
    headers = [
        str(h).strip() if h is not None else f"col_{i}"
        for i, h in enumerate(header_row)
    ]
    result = []
    for row in data_rows:
        if all(v is None for v in row):
            continue
        result.append(dict(zip(headers, row)))
    return result


def safe_date(val):
    if not val:
        return None
    if hasattr(val, "date"):
        return str(val.date())
    try:
        from frappe.utils import getdate

        return str(getdate(str(val)))
    except Exception:
        return None


def safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "").strip() or default)
    except Exception:
        return default


def cnic_str(raw):
    if isinstance(raw, (int, float)):
        return str(int(raw)).strip()
    return str(raw or "").strip()


def strip_installment(full_ref):
    """'007-10455-00035' -> '007-10455'"""
    parts = str(full_ref).rsplit("-", 1)
    return parts[0] if len(parts) == 2 else str(full_ref)


def full_log(title, tb):
    print(f"\n  ERROR: {title}")
    print(tb)
    frappe.log_error(f"{title}\n{tb}", "Lending Migration")


def guess_repayment_periods(product_name):
    m = re.search(r"(\d+)\s*[Mm]", product_name)
    if m:
        return int(m.group(1))
    m = re.search(r"-0*(\d+)$", product_name)
    if m:
        return int(m.group(1))
    return 6


def build_loan_map_from_db():
    """Load all submitted loans that have LoanRef tag."""
    loan_map = {}
    loans = frappe.db.sql(
        "SELECT name, description FROM `tabLoan` WHERE description LIKE %s AND docstatus = 1",
        ("%LoanRef:%",),
        as_dict=True,
    )
    for loan in loans:
        m = re.search(r"LoanRef:([^\s,]+)", loan.description or "")
        if m:
            loan_map[m.group(1).strip()] = loan.name
    return loan_map


def get_default_loan_product():
    """Pick the first available loan product for historical loans."""
    if HISTORICAL_LOAN_PRODUCT and frappe.db.exists(
        "Loan Product", HISTORICAL_LOAN_PRODUCT
    ):
        return HISTORICAL_LOAN_PRODUCT
    result = frappe.db.get_value("Loan Product", {"disabled": 0}, "name")
    return result or ""


# ── Select field maps ─────────────────────────────────────────────────────────

MARITAL_STATUS_MAP = {
    "married": "Married",
    "single": "Single",
    "unmarried": "Single",
    "un married": "Single",
    "divorced": "Divorced",
    "divorce": "Divorced",
    "widowed": "Widow",
    "widow": "Widow",
    "separated": "Divorced",
    "other": None,
}

RELIGION_MAP = {
    "islam": "Islam",
    "muslim": "Islam",
    "christianity": "Christianity",
    "christian": "Christian",
    "hinduism": "Hindu",
    "hindu": "Hindu",
    "sikhism": "Other",
    "sikh": "Other",
    "other": "Other",
}


def get_allowed_options(fieldname):
    try:
        meta = frappe.get_meta("Customer")
        for f in meta.fields:
            if f.fieldname == fieldname and f.options:
                return set(o.strip() for o in f.options.split("\n") if o.strip())
    except Exception:
        pass
    return set()


def safe_select(raw, mapping, allowed_set):
    if not raw:
        return None
    raw_str = str(raw).strip()
    for opt in allowed_set:
        if opt.lower() == raw_str.lower():
            return opt
    mapped = mapping.get(raw_str.lower())
    if mapped and (not allowed_set or mapped in allowed_set):
        return mapped
    return None


# ── Build historical loan index from repayment file ───────────────────────────


def build_historical_loan_index(
    repayment_rows, disbursement_loan_nos, existing_loan_map
):
    """
    Find all unique loan#s in the repayment file that are NOT in the
    disbursement file or DB. Group their rows to derive loan details.

    Returns: dict of loan_no -> {cnic, name, total_amount, earliest_date, latest_date}
    """
    # All loan#s we already have
    known = set(disbursement_loan_nos) | set(existing_loan_map.keys())

    grouped = defaultdict(
        lambda: {
            "cnic": None,
            "borrower_name": None,
            "total_amount": 0.0,
            "total_principal": 0.0,
            "earliest_date": None,
            "latest_date": None,
            "rows": 0,
        }
    )

    for r in repayment_rows:
        full_ref = str(r.get("Loan#") or "").strip()
        loan_no = strip_installment(full_ref)
        if loan_no in known:
            continue  # handled by disbursement file

        cnic = cnic_str(r.get("Borrower CNIC #"))
        date = safe_date(r.get("Collection Date"))
        amt = safe_float(r.get("Amount"))
        prin = safe_float(r.get("Principal Paid"))

        entry = grouped[loan_no]
        if not entry["cnic"] and cnic:
            entry["cnic"] = cnic
        if not entry["borrower_name"]:
            entry["borrower_name"] = str(r.get("Name") or "").strip()
        entry["total_amount"] += amt
        entry["total_principal"] += prin
        entry["rows"] += 1
        if date:
            if not entry["earliest_date"] or date < entry["earliest_date"]:
                entry["earliest_date"] = date
            if not entry["latest_date"] or date > entry["latest_date"]:
                entry["latest_date"] = date

    return dict(grouped)


# ── PRE-VALIDATION ────────────────────────────────────────────────────────────


def validate():
    global COMPANY
    COMPANY = frappe.db.get_single_value("Global Defaults", "default_company") or ""

    print("\n" + "=" * 60)
    print("PRE-MIGRATION VALIDATION REPORT")
    print("=" * 60)

    print("\n[1] Reading Excel files...")
    customer_rows = read_excel(CUSTOMER_FILE)
    disbursement_rows = read_excel(DISBURSEMENT_FILE)
    repayment_rows = read_excel(REPAYMENT_FILE)
    print(f"  Customers:     {len(customer_rows):,} rows")
    print(f"  Disbursements: {len(disbursement_rows):,} rows")
    print(f"  Repayments:    {len(repayment_rows):,} rows")

    print("\n[2] Config checks...")
    all_ok = True
    for label, dt, val in [
        ("Company", "Company", COMPANY),
        ("Branch", "Branch", BRANCH),
        ("Sales Person", "Sales Person", SALES_PERSON),
        ("Disbursement Account", "Account", DISBURSEMENT_ACCOUNT),
        ("Payment Account", "Account", PAYMENT_ACCOUNT),
        ("Loan Account", "Account", LOAN_ACCOUNT),
        ("Interest Income Account", "Account", INTEREST_INCOME_ACCOUNT),
        ("Interest Receivable", "Account", INTEREST_RECEIVABLE_ACCOUNT),
        ("Penalty Income Account", "Account", PENALTY_INCOME_ACCOUNT),
        ("Penalty Receivable", "Account", PENALTY_RECEIVABLE_ACCOUNT),
    ]:
        if not val:
            print(f"  ❌ EMPTY   : {label}")
            all_ok = False
        elif not frappe.db.exists(dt, val):
            print(f"  ❌ MISSING : {label} = '{val}'")
            all_ok = False
        else:
            print(f"  ✅ OK      : {label}")
    if not all_ok:
        print("\n  ❌ Fix config errors before running migrate()")
        return

    print("\n[3] Customer validation...")
    allowed_marital = get_allowed_options("custom_marital_status")
    allowed_religion = get_allowed_options("custom_religion")
    cust_new = cust_existing = cust_skip = 0
    marital_unmapped = {}
    religion_unmapped = {}
    for r in customer_rows:
        cnic = cnic_str(r.get("Borrower CNIC #"))
        name = str(r.get("Full Name") or "").strip()
        if not name or not cnic or cnic == "0":
            cust_skip += 1
            continue
        if frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name"):
            cust_existing += 1
        else:
            cust_new += 1
        mar_raw = str(r.get("Marital Status") or "").strip()
        rel_raw = str(r.get("Religion") or "").strip()
        if (
            mar_raw
            and safe_select(mar_raw, MARITAL_STATUS_MAP, allowed_marital) is None
        ):
            marital_unmapped[mar_raw] = marital_unmapped.get(mar_raw, 0) + 1
        if rel_raw and safe_select(rel_raw, RELIGION_MAP, allowed_religion) is None:
            religion_unmapped[rel_raw] = religion_unmapped.get(rel_raw, 0) + 1
    print(f"  Will create  : {cust_new:,} new customers")
    print(f"  Already exist: {cust_existing:,} (will skip)")
    print(f"  Invalid rows : {cust_skip:,}")
    if marital_unmapped:
        print(f"  ⚠️  Unmapped marital status (left blank): {marital_unmapped}")
    else:
        print(f"  ✅ All marital status values map correctly")
    if religion_unmapped:
        print(f"  ⚠️  Unmapped religion (left blank): {religion_unmapped}")
    else:
        print(f"  ✅ All religion values map correctly")

    print("\n[4] Loan Product validation...")
    products_in_excel = set()
    for r in disbursement_rows:
        p = str(r.get("Loan Product") or "").strip()
        if p:
            products_in_excel.add(p)
    prod_existing = {
        p for p in products_in_excel if frappe.db.exists("Loan Product", p)
    }
    prod_new = products_in_excel - prod_existing
    prod_no_acct = {
        p
        for p in prod_existing
        if not frappe.db.get_value("Loan Product", p, "disbursement_account")
    }
    print(f"  Products in Excel      : {len(products_in_excel)}")
    print(f"  Already in Frappe      : {len(prod_existing)}")
    print(f"  Need accounts patch    : {len(prod_no_acct)} {sorted(prod_no_acct)}")
    print(f"  Will create new        : {len(prod_new)} {sorted(prod_new)}")
    default_product = get_default_loan_product()
    print(
        f"  Historical loan product: '{default_product}' (used for closed historical loans)"
    )

    print("\n[5] Loan validation (from disbursement file)...")
    existing_loan_map = build_loan_map_from_db()
    disbursement_loan_nos = set()
    for r in disbursement_rows:
        ln = str(r.get("Loan#") or "").strip()
        if ln:
            disbursement_loan_nos.add(ln)

    # Build CNIC set
    all_cnics = set(cnic_str(r.get("Borrower CNIC #")) for r in customer_rows)
    all_cnics |= set(
        str(row[0])
        for row in frappe.db.sql(
            "SELECT custom_cnic_no FROM `tabCustomer` WHERE custom_cnic_no IS NOT NULL AND custom_cnic_no != ''"
        )
    )
    loan_new = loan_existing = loan_no_customer = 0
    for r in disbursement_rows:
        loan_no = str(r.get("Loan#") or "").strip()
        cnic = cnic_str(r.get("Borrower CNIC #"))
        if not loan_no:
            continue
        if loan_no in existing_loan_map:
            loan_existing += 1
            continue
        if cnic not in all_cnics:
            loan_no_customer += 1
        loan_new += 1
    print(f"  Will create  : {loan_new:,} new loans (active/current)")
    print(f"  Already exist: {loan_existing:,} (will skip)")
    if loan_no_customer:
        print(f"  ⚠️  No customer found: {loan_no_customer:,} rows")
    else:
        print(f"  ✅ All loans have matching customers")

    print("\n[6] Repayment validation...")
    hist_index = build_historical_loan_index(
        repayment_rows, disbursement_loan_nos, existing_loan_map
    )
    all_loan_nos = disbursement_loan_nos | set(existing_loan_map.keys())

    rep_matched_active = 0  # repayments for loans in disbursement file
    rep_matched_hist = 0  # repayments for historical loans we'll create
    rep_no_customer = 0  # historical loans with no CNIC match
    rep_unmatched = 0  # truly unresolvable

    for r in repayment_rows:
        full_ref = str(r.get("Loan#") or "").strip()
        loan_no = strip_installment(full_ref)
        if loan_no in all_loan_nos:
            rep_matched_active += 1
        elif loan_no in hist_index:
            cnic = hist_index[loan_no]["cnic"]
            if cnic and cnic in all_cnics:
                rep_matched_hist += 1
            else:
                rep_no_customer += 1
        else:
            rep_unmatched += 1

    print(f"  Total repayment rows              : {len(repayment_rows):,}")
    print(f"  Matched to active loans           : {rep_matched_active:,}")
    print(
        f"  Matched via historical loans      : {rep_matched_hist:,} "
        f"({len(hist_index):,} new closed loans will be created)"
    )
    if rep_no_customer:
        print(
            f"  ⚠️  Historical loans with no CNIC  : {rep_no_customer:,} (will be skipped)"
        )
    if rep_unmatched:
        print(f"  ⚠️  Truly unresolvable             : {rep_unmatched:,}")
    total_importable = rep_matched_active + rep_matched_hist
    print(f"  ✅ Total repayments to import      : {total_importable:,} (as DRAFT)")
    print(f"  ℹ️  Repayments saved as DRAFT — submit individually if GL posting needed")

    # Sample historical loans
    if hist_index:
        print(f"\n  Sample historical loans to be created:")
        for loan_no, info in list(hist_index.items())[:5]:
            print(
                f"    · {loan_no}  CNIC={info['cnic']}  "
                f"payments={info['rows']}  total_paid={info['total_amount']:,.0f}"
            )

    print("\n[7] Current data in Frappe...")
    for dt in [
        "Customer",
        "Loan Product",
        "Loan",
        "Loan Disbursement",
        "Loan Repayment",
    ]:
        try:
            print(f"  {dt}: {frappe.db.count(dt):,}")
        except Exception as e:
            print(f"  {dt}: ERROR — {e}")

    print("\n" + "=" * 60)
    print("VALIDATION COMPLETE")
    print(f"  Expected after migration:")
    print(f"    Customers   : ~{cust_new + cust_existing:,}")
    print(
        f"    Loans       : ~{loan_new + loan_existing + len(hist_index):,} "
        f"({loan_new:,} active + {len(hist_index):,} historical closed)"
    )
    print(f"    Repayments  : ~{total_importable:,} (draft)")
    print(
        "\nRun: bench --site your-site.com execute lending.migrate_lending_data.migrate"
    )
    print("=" * 60 + "\n")


# ── Step 1: Customers ─────────────────────────────────────────────────────────


def migrate_customers(rows):
    cnic_to_customer = {}
    created = skipped = errors = 0
    allowed_marital = get_allowed_options("custom_marital_status")
    allowed_religion = get_allowed_options("custom_religion")

    for r in rows:
        full_name = str(r.get("Full Name") or "").strip()
        cnic = cnic_str(r.get("Borrower CNIC #"))
        if not full_name or not cnic or cnic == "0":
            skipped += 1
            continue

        existing = frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name")
        if existing:
            cnic_to_customer[cnic] = existing
            skipped += 1
            continue

        marital = safe_select(
            r.get("Marital Status"), MARITAL_STATUS_MAP, allowed_marital
        )
        religion = safe_select(r.get("Religion"), RELIGION_MAP, allowed_religion)

        try:
            gender_val = str(r.get("Gender") or "").strip()
            if gender_val and not frappe.db.exists("Gender", gender_val):
                g = frappe.new_doc("Gender")
                g.gender = gender_val
                g.insert(ignore_permissions=True)

            doc = frappe.new_doc("Customer")
            doc.customer_name = full_name
            doc.customer_type = "Individual"
            doc.customer_group = "Individual"
            doc.territory = "All Territories"
            doc.gender = gender_val or None
            doc.custom_cnic_no = cnic
            doc.custom_date_of_birth = safe_date(r.get("DOB"))
            doc.custom_cnic_expiry_date = safe_date(r.get("CNIC Expiry Date"))
            doc.custom_marital_status = marital
            doc.custom_religion = religion
            doc.custom_spouse_name = (
                str(r.get("Parentage/Spouse Name") or "").strip() or None
            )
            doc.custom_spouse_mobile = (
                str(r.get("Parentage/Spouse Mobile #") or "").strip() or None
            )
            doc.custom_phone_no_1 = str(r.get("Mobile") or "").strip() or None
            doc.insert(ignore_permissions=True)
            frappe.db.commit()

            cnic_to_customer[cnic] = doc.name
            created += 1
            if created % 100 == 0:
                print(f"  ...{created} customers created")

        except Exception:
            full_log(f"Customer [{full_name}/{cnic}]", traceback.format_exc())
            errors += 1

    print(f"Customers — created: {created}, skipped: {skipped}, errors: {errors}")
    return cnic_to_customer


# ── Step 2: Loan Products ─────────────────────────────────────────────────────


def ensure_loan_products(rows):
    products = {}
    for r in rows:
        name = str(r.get("Loan Product") or "").strip()
        rate = safe_float(r.get("Interest Rate"))
        if name:
            products.setdefault(name, [])
            if rate:
                products[name].append(rate)

    for product_name, rates in sorted(products.items()):
        avg_rate = sum(rates) / len(rates) if rates else 0.0
        periods = guess_repayment_periods(product_name)

        if frappe.db.exists("Loan Product", product_name):
            has_acct = frappe.db.get_value(
                "Loan Product", product_name, "disbursement_account"
            )
            if not has_acct:
                frappe.db.set_value(
                    "Loan Product",
                    product_name,
                    {
                        "disbursement_account": DISBURSEMENT_ACCOUNT,
                        "payment_account": PAYMENT_ACCOUNT,
                        "loan_account": LOAN_ACCOUNT,
                        "interest_income_account": INTEREST_INCOME_ACCOUNT,
                        "interest_receivable_account": INTEREST_RECEIVABLE_ACCOUNT,
                        "penalty_income_account": PENALTY_INCOME_ACCOUNT,
                        "penalty_receivable_account": PENALTY_RECEIVABLE_ACCOUNT,
                        "mode_of_payment": MODE_OF_PAYMENT,
                    },
                    update_modified=False,
                )
                frappe.db.commit()
                print(f"  Patched: {product_name}")
            else:
                print(f"  Complete: {product_name}")
            continue

        try:
            doc = frappe.new_doc("Loan Product")
            doc.product_code = product_name
            doc.product_name = product_name
            doc.company = COMPANY
            doc.is_term_loan = 1
            doc.rate_of_interest = avg_rate
            doc.repayment_method = REPAYMENT_METHOD
            doc.repayment_periods = periods
            doc.repayment_schedule_type = "Monthly as per repayment start date"
            doc.mode_of_payment = MODE_OF_PAYMENT
            doc.min_days_bw_disbursement_first_repayment = MIN_DAYS_FIRST_REPAYMENT
            doc.min_auto_closure_tolerance_amount = MIN_AUTO_CLOSURE_TOLERANCE
            doc.max_auto_closure_tolerance_amount = MAX_AUTO_CLOSURE_TOLERANCE
            doc.penalty_interest_method = PENALTY_INTEREST_METHOD
            doc.penalty_interest_rate = PENALTY_INTEREST_RATE
            doc.penalty_stop_threshold_days = PENALTY_STOP_THRESHOLD_DAYS
            doc.disbursement_account = DISBURSEMENT_ACCOUNT
            doc.payment_account = PAYMENT_ACCOUNT
            doc.loan_account = LOAN_ACCOUNT
            doc.interest_income_account = INTEREST_INCOME_ACCOUNT
            doc.interest_receivable_account = INTEREST_RECEIVABLE_ACCOUNT
            doc.penalty_income_account = PENALTY_INCOME_ACCOUNT
            doc.penalty_receivable_account = PENALTY_RECEIVABLE_ACCOUNT
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
            print(
                f"  Created: {product_name} (rate={avg_rate:.1f}% periods={periods}m)"
            )
        except Exception:
            full_log(f"Loan Product [{product_name}]", traceback.format_exc())

    print(f"Loan Products processed: {len(products)}")


# ── Step 3: Active Loans (from disbursement file) ─────────────────────────────


def migrate_loans(rows, cnic_to_customer):
    loan_to_loan = {}
    created = skipped = errors = 0
    existing_map = build_loan_map_from_db()

    for r in rows:
        loan_no = str(r.get("Loan#") or "").strip()
        cnic = cnic_str(r.get("Borrower CNIC #"))
        customer = cnic_to_customer.get(cnic)
        if not loan_no or not customer:
            skipped += 1
            continue

        if loan_no in existing_map:
            loan_to_loan[loan_no] = existing_map[loan_no]
            skipped += 1
            continue

        product = str(r.get("Loan Product") or "").strip() or None
        disb_date = safe_date(r.get("Disbursed Date")) or nowdate()

        prod_data = {}
        if product and frappe.db.exists("Loan Product", product):
            prod_data = (
                frappe.db.get_value(
                    "Loan Product",
                    product,
                    [
                        "disbursement_account",
                        "payment_account",
                        "loan_account",
                        "interest_income_account",
                        "penalty_income_account",
                        "mode_of_payment",
                        "rate_of_interest",
                        "penalty_stop_threshold_days",
                        "repayment_method",
                        "repayment_periods",
                        "is_term_loan",
                    ],
                    as_dict=True,
                )
                or {}
            )

        try:
            doc = frappe.new_doc("Loan")
            doc.applicant_type = "Customer"
            doc.applicant = customer
            doc.company = COMPANY
            doc.branch = BRANCH
            doc.loan_product = product
            doc.loan_amount = safe_float(r.get("Disbursed"))
            doc.disbursed_amount = safe_float(r.get("Disbursed"))
            doc.posting_date = disb_date
            doc.disbursement_date = disb_date
            doc.repayment_start_date = disb_date
            doc.sales_person = SALES_PERSON
            doc.status = "Disbursed"
            doc.description = f"Migrated. LoanRef:{loan_no}"
            doc.rate_of_interest = prod_data.get("rate_of_interest") or safe_float(
                r.get("Interest Rate")
            )
            doc.penalty_stop_threshold_days = (
                prod_data.get("penalty_stop_threshold_days")
                or PENALTY_STOP_THRESHOLD_DAYS
            )
            doc.is_term_loan = prod_data.get("is_term_loan", 1)
            doc.repayment_method = prod_data.get("repayment_method") or REPAYMENT_METHOD
            doc.repayment_periods = prod_data.get(
                "repayment_periods"
            ) or guess_repayment_periods(product or "")
            doc.mode_of_payment = prod_data.get("mode_of_payment") or MODE_OF_PAYMENT
            doc.disbursement_account = (
                prod_data.get("disbursement_account") or DISBURSEMENT_ACCOUNT
            )
            doc.payment_account = prod_data.get("payment_account") or PAYMENT_ACCOUNT
            doc.loan_account = prod_data.get("loan_account") or LOAN_ACCOUNT
            doc.interest_income_account = (
                prod_data.get("interest_income_account") or INTEREST_INCOME_ACCOUNT
            )
            doc.penalty_income_account = (
                prod_data.get("penalty_income_account") or PENALTY_INCOME_ACCOUNT
            )
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()

            loan_to_loan[loan_no] = doc.name
            created += 1
            if created % 100 == 0:
                print(f"  ...{created} loans created")

        except Exception:
            full_log(f"Loan [{loan_no}]", traceback.format_exc())
            errors += 1

    print(f"Active Loans — created: {created}, skipped: {skipped}, errors: {errors}")
    return loan_to_loan


# ── Step 4: Historical Loans (from repayment file, closed) ───────────────────


def migrate_historical_loans(hist_index, cnic_to_customer):
    """
    Create minimal closed loan records for loans that appear in the
    repayment file but not in the disbursement file. These are
    fully paid historical loans.

    loan_amount = total principal paid (best estimate of original loan)
    status = Closed
    """
    loan_to_loan = {}
    created = skipped = errors = 0
    existing_map = build_loan_map_from_db()
    default_product = get_default_loan_product()

    prod_data = {}
    if default_product:
        prod_data = (
            frappe.db.get_value(
                "Loan Product",
                default_product,
                [
                    "disbursement_account",
                    "payment_account",
                    "loan_account",
                    "interest_income_account",
                    "penalty_income_account",
                    "mode_of_payment",
                    "rate_of_interest",
                    "penalty_stop_threshold_days",
                    "repayment_method",
                    "repayment_periods",
                    "is_term_loan",
                ],
                as_dict=True,
            )
            or {}
        )

    for loan_no, info in hist_index.items():
        if loan_no in existing_map:
            loan_to_loan[loan_no] = existing_map[loan_no]
            skipped += 1
            continue

        cnic = info.get("cnic", "")
        customer = cnic_to_customer.get(cnic)
        if not customer:
            # Try DB lookup
            customer = frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name")
        if not customer:
            skipped += 1
            continue

        # Use total principal as loan amount estimate; fall back to total amount
        loan_amount = info["total_principal"] or info["total_amount"] or 1.0
        close_date = info["latest_date"] or nowdate()
        disb_date = info["earliest_date"] or close_date

        try:
            doc = frappe.new_doc("Loan")
            doc.applicant_type = "Customer"
            doc.applicant = customer
            doc.company = COMPANY
            doc.branch = BRANCH
            doc.loan_product = default_product or None
            doc.loan_amount = loan_amount
            doc.disbursed_amount = loan_amount
            doc.posting_date = disb_date
            doc.disbursement_date = disb_date
            doc.repayment_start_date = disb_date
            doc.sales_person = SALES_PERSON
            doc.status = "Closed"
            doc.description = f"Migrated historical. LoanRef:{loan_no}"
            doc.rate_of_interest = prod_data.get("rate_of_interest", 0)
            doc.penalty_stop_threshold_days = (
                prod_data.get("penalty_stop_threshold_days")
                or PENALTY_STOP_THRESHOLD_DAYS
            )
            doc.is_term_loan = prod_data.get("is_term_loan", 1)
            doc.repayment_method = prod_data.get("repayment_method") or REPAYMENT_METHOD
            doc.repayment_periods = prod_data.get("repayment_periods") or 6
            doc.mode_of_payment = prod_data.get("mode_of_payment") or MODE_OF_PAYMENT
            doc.disbursement_account = (
                prod_data.get("disbursement_account") or DISBURSEMENT_ACCOUNT
            )
            doc.payment_account = prod_data.get("payment_account") or PAYMENT_ACCOUNT
            doc.loan_account = prod_data.get("loan_account") or LOAN_ACCOUNT
            doc.interest_income_account = (
                prod_data.get("interest_income_account") or INTEREST_INCOME_ACCOUNT
            )
            doc.penalty_income_account = (
                prod_data.get("penalty_income_account") or PENALTY_INCOME_ACCOUNT
            )
            doc.insert(ignore_permissions=True)
            doc.submit()
            frappe.db.commit()

            loan_to_loan[loan_no] = doc.name
            created += 1
            if created % 100 == 0:
                print(f"  ...{created} historical loans created")

        except Exception:
            full_log(f"Historical Loan [{loan_no}]", traceback.format_exc())
            errors += 1

    print(
        f"Historical Loans — created: {created}, skipped: {skipped}, errors: {errors}"
    )
    return loan_to_loan


# ── Step 5: Loan Disbursements ────────────────────────────────────────────────


def migrate_disbursements(rows, loan_to_loan):
    created = skipped = errors = 0
    for r in rows:
        loan_no = str(r.get("Loan#") or "").strip()
        loan_doc = loan_to_loan.get(loan_no)
        if not loan_doc:
            skipped += 1
            continue
        if frappe.db.get_value("Loan Disbursement", {"against_loan": loan_doc}, "name"):
            skipped += 1
            continue
        try:
            loan_rec = (
                frappe.db.get_value(
                    "Loan",
                    loan_doc,
                    [
                        "applicant_type",
                        "applicant",
                        "company",
                        "branch",
                        "loan_product",
                    ],
                    as_dict=True,
                )
                or {}
            )
            disb_date = safe_date(r.get("Disbursed Date")) or nowdate()
            doc = frappe.new_doc("Loan Disbursement")
            doc.against_loan = loan_doc
            doc.disbursement_date = disb_date
            doc.disbursed_amount = safe_float(r.get("Disbursed"))
            doc.applicant_type = loan_rec.get("applicant_type", "Customer")
            doc.applicant = loan_rec.get("applicant")
            doc.company = loan_rec.get("company", COMPANY)
            doc.branch = loan_rec.get("branch", BRANCH)
            doc.loan_product = loan_rec.get("loan_product")
            doc.insert(ignore_permissions=True)
            # NOT submitted — Loan already has disbursed_amount set.
            frappe.db.commit()
            created += 1
        except Exception:
            full_log(f"Disbursement [{loan_no}]", traceback.format_exc())
            errors += 1
    print(
        f"Disbursements — created: {created} (draft), skipped: {skipped}, errors: {errors}"
    )


# ── Step 6: Loan Repayments ───────────────────────────────────────────────────


def migrate_repayments(rows, active_loan_map, historical_loan_map):
    created = skipped = errors = 0
    unmatched = set()

    # Merge all loan maps: DB + current active + current historical
    full_map = build_loan_map_from_db()
    full_map.update(active_loan_map)
    full_map.update(historical_loan_map)
    print(f"  Loans available for matching: {len(full_map):,}")

    # Idempotency: track existing repayments
    existing_repayments = set(
        f"{r[0]}_{r[1]}_{r[2]}"
        for r in frappe.db.sql(
            "SELECT against_loan, DATE(posting_date), amount_paid FROM `tabLoan Repayment` WHERE docstatus < 2"
        )
    )

    for r in rows:
        full_ref = str(r.get("Loan#") or "").strip()
        loan_no = strip_installment(full_ref)
        loan_doc = full_map.get(loan_no)

        if not loan_doc:
            unmatched.add(loan_no)
            skipped += 1
            continue

        col_date = r.get("Collection Date")
        date_str = (
            col_date.strftime("%Y-%m-%d")
            if hasattr(col_date, "strftime")
            else (safe_date(col_date) or nowdate())
        )
        amount = safe_float(r.get("Amount"))
        dupe_key = f"{loan_doc}_{date_str}_{amount}"
        if dupe_key in existing_repayments:
            skipped += 1
            continue

        try:
            loan_rec = (
                frappe.db.get_value(
                    "Loan",
                    loan_doc,
                    [
                        "applicant_type",
                        "applicant",
                        "company",
                        "branch",
                        "loan_product",
                        "is_term_loan",
                    ],
                    as_dict=True,
                )
                or {}
            )

            doc = frappe.new_doc("Loan Repayment")
            doc.against_loan = loan_doc
            doc.posting_date = date_str + " 00:00:00"
            doc.amount_paid = amount
            doc.repayment_type = "Normal Repayment"
            doc.applicant_type = loan_rec.get("applicant_type", "Customer")
            doc.applicant = loan_rec.get("applicant")
            doc.company = loan_rec.get("company", COMPANY)
            doc.branch = loan_rec.get("branch", BRANCH)
            doc.loan_product = loan_rec.get("loan_product")
            doc.is_term_loan = loan_rec.get("is_term_loan", 1)
            doc.principal_amount_paid = safe_float(r.get("Principal Paid"))
            doc.total_interest_paid = safe_float(r.get("Interest Paid"))
            doc.total_penalty_paid = safe_float(r.get("Penalty Paid"))
            # DRAFT — submit triggers GL entries requiring repayment schedule
            doc.insert(ignore_permissions=True)
            frappe.db.commit()

            existing_repayments.add(dupe_key)
            created += 1
            if created % 500 == 0:
                print(f"  ...{created} repayments created")

        except Exception:
            full_log(
                f"Repayment [{loan_no}/{date_str}/{amount}]", traceback.format_exc()
            )
            errors += 1

    if unmatched:
        print(f"\n  Still unmatched ({len(unmatched):,}): {sorted(unmatched)[:8]} ...")
    print(
        f"Repayments — created: {created} (draft), skipped: {skipped}, errors: {errors}"
    )


# ── Entry Point ───────────────────────────────────────────────────────────────


def migrate():
    global COMPANY
    COMPANY = (
        frappe.db.get_single_value("Global Defaults", "default_company") or COMPANY
    )
    if not COMPANY:
        print("ERROR: Company not found.")
        return
    if not BRANCH or not SALES_PERSON:
        print("ERROR: BRANCH and SALES_PERSON must be set.")
        return

    print(f"\nCompany     : {COMPANY}")
    print(f"Branch      : {BRANCH}")
    print(f"Sales Person: {SALES_PERSON}")
    print("=" * 60)
    print("FRAPPE LENDING MIGRATION")
    print("=" * 60)

    print("\n[1] Reading Excel files...")
    customer_rows = read_excel(CUSTOMER_FILE)
    disbursement_rows = read_excel(DISBURSEMENT_FILE)
    repayment_rows = read_excel(REPAYMENT_FILE)
    print(f"  Customers:     {len(customer_rows):,}")
    print(f"  Disbursements: {len(disbursement_rows):,}")
    print(f"  Repayments:    {len(repayment_rows):,}")

    # Build historical loan index before migration starts
    disbursement_loan_nos = {
        str(r.get("Loan#") or "").strip() for r in disbursement_rows
    }
    existing_map = build_loan_map_from_db()
    hist_index = build_historical_loan_index(
        repayment_rows, disbursement_loan_nos, existing_map
    )
    print(f"  Historical closed loans to create: {len(hist_index):,}")

    print("\n[2] Migrating Customers...")
    cnic_to_customer = migrate_customers(customer_rows)

    print("\n[3] Ensuring Loan Products...")
    ensure_loan_products(disbursement_rows)

    print("\n[4] Migrating Active Loans (from disbursement file)...")
    active_loan_map = migrate_loans(disbursement_rows, cnic_to_customer)

    print("\n[5] Migrating Historical Loans (from repayment file)...")
    historical_loan_map = migrate_historical_loans(hist_index, cnic_to_customer)

    print("\n[6] Migrating Loan Disbursements (draft)...")
    migrate_disbursements(disbursement_rows, active_loan_map)

    print("\n[7] Migrating Loan Repayments (draft)...")
    migrate_repayments(repayment_rows, active_loan_map, historical_loan_map)

    print("\n" + "=" * 60)
    print("MIGRATION COMPLETE")
    print(f"  Customers mapped         : {len(cnic_to_customer):,}")
    print(f"  Active loans created     : {len(active_loan_map):,}")
    print(f"  Historical loans created : {len(historical_loan_map):,}")
    total_loans = len(active_loan_map) + len(historical_loan_map)
    print(f"  Total loans              : {total_loans:,}")
    print("\nRepayments saved as DRAFT.")
    print("Check Frappe > Tools > Error Log for row-level errors.")
    print("=" * 60)
