# diagnose_lending.py
# Run with: bench --site your-site.com execute diagnose_lending.diagnose
#
# This script does NOT create anything. It only inspects your Frappe setup
# and prints what it finds so you can fix the migration script accordingly.

import frappe
import openpyxl
import os

BASE_DIR = os.path.dirname(__file__)
CUSTOMER_FILE     = os.path.join(BASE_DIR, "Customer Data.xlsx")
DISBURSEMENT_FILE = os.path.join(BASE_DIR, "Disbursement Data.xlsx")
REPAYMENT_FILE    = os.path.join(BASE_DIR, "Repayment Data.xlsx")


def read_excel(filepath, max_rows=3):
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(rows[0])]
    data = []
    for row in rows[1:max_rows+1]:
        if all(v is None for v in row):
            continue
        data.append(dict(zip(headers, row)))
    return headers, data


def diagnose():
    print("\n" + "="*60)
    print("FRAPPE LENDING MIGRATION DIAGNOSTICS")
    print("="*60)

    # ── 1. Check Excel column headers ────────────────────────────────
    print("\n[1] EXCEL COLUMN HEADERS")
    print("-"*40)

    c_headers, c_rows = read_excel(CUSTOMER_FILE)
    print(f"\nCustomer Data.xlsx columns ({len(c_headers)}):")
    for h in c_headers:
        print(f"  · {repr(h)}")

    d_headers, d_rows = read_excel(DISBURSEMENT_FILE)
    print(f"\nDisbursement Data.xlsx columns ({len(d_headers)}):")
    for h in d_headers:
        print(f"  · {repr(h)}")

    r_headers, r_rows = read_excel(REPAYMENT_FILE)
    print(f"\nRepayment Data.xlsx columns ({len(r_headers)}):")
    for h in r_headers:
        print(f"  · {repr(h)}")

    # ── 2. Sample row values ──────────────────────────────────────────
    print("\n[2] SAMPLE ROW (Customer Data row 1)")
    print("-"*40)
    if c_rows:
        for k, v in c_rows[0].items():
            print(f"  {repr(k)}: {repr(v)}")

    print("\n[2b] SAMPLE ROW (Disbursement Data row 1)")
    print("-"*40)
    if d_rows:
        for k, v in d_rows[0].items():
            print(f"  {repr(k)}: {repr(v)}")

    # ── 3. Check Customer doctype fields ─────────────────────────────
    print("\n[3] CUSTOMER DOCTYPE — ALL FIELDS")
    print("-"*40)
    try:
        meta = frappe.get_meta("Customer")
        for f in meta.fields:
            print(f"  {f.fieldname:<40} ({f.fieldtype}) — {f.label}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # ── 4. Check Loan Application fields ─────────────────────────────
    print("\n[4] LOAN APPLICATION DOCTYPE — ALL FIELDS")
    print("-"*40)
    try:
        meta = frappe.get_meta("Loan Application")
        for f in meta.fields:
            print(f"  {f.fieldname:<40} ({f.fieldtype}) — {f.label}")
    except Exception as e:
        print(f"  ERROR getting Loan Application meta: {e}")
        # Try to list all loan-related doctypes
        print("\n  Searching for loan-related doctypes...")
        all_dt = frappe.db.sql(
            "SELECT name FROM `tabDocType` WHERE name LIKE '%Loan%' ORDER BY name",
            as_dict=True
        )
        for dt in all_dt:
            print(f"    · {dt.name}")

    # ── 5. Check Loan fields ──────────────────────────────────────────
    print("\n[5] LOAN DOCTYPE — ALL FIELDS")
    print("-"*40)
    try:
        meta = frappe.get_meta("Loan")
        for f in meta.fields:
            print(f"  {f.fieldname:<40} ({f.fieldtype}) — {f.label}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # ── 6. Check Loan Disbursement fields ────────────────────────────
    print("\n[6] LOAN DISBURSEMENT DOCTYPE — ALL FIELDS")
    print("-"*40)
    try:
        meta = frappe.get_meta("Loan Disbursement")
        for f in meta.fields:
            print(f"  {f.fieldname:<40} ({f.fieldtype}) — {f.label}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # ── 7. Check Loan Repayment fields ───────────────────────────────
    print("\n[7] LOAN REPAYMENT DOCTYPE — ALL FIELDS")
    print("-"*40)
    try:
        meta = frappe.get_meta("Loan Repayment")
        for f in meta.fields:
            print(f"  {f.fieldname:<40} ({f.fieldtype}) — {f.label}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # ── 8. Check existing Loan Types ─────────────────────────────────
    print("\n[8] EXISTING LOAN TYPES IN FRAPPE")
    print("-"*40)
    try:
        loan_types = frappe.db.sql(
            "SELECT name FROM `tabLoan Type` ORDER BY name", as_dict=True
        )
        if loan_types:
            for lt in loan_types:
                print(f"  · {lt.name}")
        else:
            print("  (none found)")
    except Exception as e:
        print(f"  ERROR: {e}")

    # ── 9. Loan types used in Excel ───────────────────────────────────
    print("\n[9] LOAN TYPES IN DISBURSEMENT EXCEL")
    print("-"*40)
    _, all_d_rows = read_excel(DISBURSEMENT_FILE, max_rows=9999)
    loan_products = set()
    for r in all_d_rows:
        val = r.get("Loan Product") or r.get("loan_product") or r.get("Product")
        if val:
            loan_products.add(str(val).strip())
    for lp in sorted(loan_products):
        print(f"  · {repr(lp)}")

    # ── 10. Check Customer Groups ─────────────────────────────────────
    print("\n[10] EXISTING CUSTOMER GROUPS")
    print("-"*40)
    try:
        groups = frappe.db.sql(
            "SELECT name FROM `tabCustomer Group` ORDER BY name", as_dict=True
        )
        for g in groups:
            print(f"  · {g.name}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # ── 11. Check existing customers count ───────────────────────────
    print("\n[11] EXISTING DATA COUNTS")
    print("-"*40)
    for dt in ["Customer", "Loan Application", "Loan", "Loan Disbursement", "Loan Repayment"]:
        try:
            count = frappe.db.count(dt)
            print(f"  {dt}: {count} records")
        except Exception as e:
            print(f"  {dt}: ERROR — {e}")

    print("\n" + "="*60)
    print("DIAGNOSTICS COMPLETE — share this output to fix the migration")
    print("="*60 + "\n")