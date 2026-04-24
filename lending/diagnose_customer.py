# diagnose_customer.py
# Run with: bench execute lending.diagnose_customer.diagnose

import frappe
import traceback

def diagnose():
    """
    Tries to insert the first customer from Customer Data.xlsx
    and prints the FULL error so we can see exactly what field fails.
    """
    import openpyxl, os

    filepath = os.path.join(os.path.dirname(__file__), "Customer Data.xlsx")
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    # Skip title row, use row 2 as headers
    headers  = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(rows[1])]
    data_row = dict(zip(headers, rows[2]))  # first actual data row

    print("\nFirst data row:")
    for k, v in data_row.items():
        print(f"  {k!r}: {v!r}")

    print("\n── Customer doctype Select field options ──")
    meta = frappe.get_meta("Customer")
    for f in meta.fields:
        if f.fieldtype == "Select" and "custom" in f.fieldname:
            opts = [o for o in (f.options or "").split("\n") if o.strip()]
            print(f"  {f.fieldname} ({f.label}): {opts}")

    print("\n── Attempting test insert (will rollback) ──")
    try:
        frappe.db.begin()

        doc = frappe.new_doc("Customer")
        doc.customer_name  = str(data_row.get("Full Name") or "Test").strip()
        doc.customer_type  = "Individual"
        doc.customer_group = "Individual"
        doc.territory      = "All Territories"

        gender = str(data_row.get("Gender") or "").strip()
        if gender and not frappe.db.exists("Gender", gender):
            g = frappe.new_doc("Gender")
            g.gender = gender
            g.insert(ignore_permissions=True)
        doc.gender = gender or None

        doc.custom_cnic_no          = str(data_row.get("Borrower CNIC #") or "TEST123")
        doc.custom_date_of_birth    = None
        doc.custom_cnic_expiry_date = None
        doc.custom_marital_status   = str(data_row.get("Marital Status") or "").strip() or None
        doc.custom_religion         = str(data_row.get("Religion") or "").strip() or None
        doc.custom_spouse_name      = str(data_row.get("Parentage/Spouse Name") or "").strip() or None
        doc.custom_spouse_mobile    = str(data_row.get("Parentage/Spouse Mobile #") or "").strip() or None
        doc.custom_phone_no_1       = str(data_row.get("Mobile") or "").strip() or None

        print(f"\n  Inserting with:")
        print(f"    customer_name         = {doc.customer_name!r}")
        print(f"    custom_marital_status = {doc.custom_marital_status!r}")
        print(f"    custom_religion       = {doc.custom_religion!r}")
        print(f"    gender                = {doc.gender!r}")
        print(f"    custom_cnic_no        = {doc.custom_cnic_no!r}")

        doc.insert(ignore_permissions=True)
        print("\n  ✅ Insert succeeded!")

    except Exception:
        print("\n  ❌ Insert FAILED — full traceback:")
        print(traceback.format_exc())

    finally:
        frappe.db.rollback()
        print("\n  (rolled back — no data saved)")

    # Also check which custom fields actually exist
    print("\n── All custom fields on Customer ──")
    custom_fields = frappe.db.sql("""
        SELECT fieldname, fieldtype, label, options
        FROM `tabCustom Field`
        WHERE dt = 'Customer'
        ORDER BY fieldname
    """, as_dict=True)
    if custom_fields:
        for cf in custom_fields:
            opts = cf.options[:60] if cf.options else ""
            print(f"  {cf.fieldname} ({cf.fieldtype}) — {cf.label} | options: {opts!r}")
    else:
        print("  (no custom fields found — fields may be in doctype directly)")