# bench --site your-site.com execute lending.delete_import_data.main

import frappe


def main():
    # ── Loan Repayments ───────────────────────────────────────────
    print("\nDeleting Loan Repayments...")
    repayments = frappe.get_all("Loan Repayment", fields=["name", "docstatus"])
    deleted = errors = 0
    for i in repayments:
        print(f"deleting {i}")
        try:
            doc = frappe.get_doc("Loan Repayment", i.name)
            if doc.docstatus == 1:
                doc.cancel()
            doc.delete()
            deleted += 1
            print(f"{i} deleted")
        except Exception as e:
            print(f"  ERROR {i.name}: {e}")
            errors += 1
    frappe.db.commit()
    print(f"  Repayments deleted: {deleted}, errors: {errors}")

    # ── Loan Disbursements ────────────────────────────────────────
    print("\nDeleting Loan Disbursements...")
    disbursements = frappe.get_all("Loan Disbursement", fields=["name", "docstatus"])
    deleted = errors = 0
    for i in disbursements:
        try:
            doc = frappe.get_doc("Loan Disbursement", i.name)
            if doc.docstatus == 1:
                doc.cancel()
            doc.delete()
            deleted += 1
        except Exception as e:
            print(f"  ERROR {i.name}: {e}")
            errors += 1
    frappe.db.commit()
    print(f"  Disbursements deleted: {deleted}, errors: {errors}")

    # ── Loan Repayment Schedules ──────────────────────────────────
    print("\nDeleting Loan Repayment Schedules...")
    schedules = frappe.get_all("Loan Repayment Schedule", fields=["name", "docstatus"])
    deleted = errors = 0
    for i in schedules:
        try:
            doc = frappe.get_doc("Loan Repayment Schedule", i.name)
            if doc.docstatus == 1:
                doc.cancel()
            doc.delete()
            deleted += 1
        except Exception as e:
            print(f"  ERROR {i.name}: {e}")
            errors += 1
    frappe.db.commit()
    print(f"  Schedules deleted: {deleted}, errors: {errors}")

    # ── Loans ─────────────────────────────────────────────────────
    print("\nDeleting Loan interest accrual...")
    loans = frappe.get_all("Loan Interest Accrual", fields=["name", "docstatus"])
    deleted = errors = 0
    for i in loans:
        try:
            doc = frappe.get_doc("Loan Interest Accrual", i.name)
            if doc.docstatus == 1:
                doc.cancel()
            doc.delete()
            print(f"{i} deleted")
            deleted += 1
        except Exception as e:
            print(f"  ERROR {i.name}: {e}")
            print(frappe.get_traceback())
            errors += 1
    frappe.db.commit()
    print(f"  Loan Interest Accrual deleted: {deleted}, errors: {errors}")

    # ── Loans ─────────────────────────────────────────────────────
    print("\nDeleting Loans...")
    loans = frappe.get_all("Loan", fields=["name", "docstatus"])
    deleted = errors = 0
    for i in loans:
        try:
            doc = frappe.get_doc("Loan", i.name)
            if doc.docstatus == 1:
                doc.cancel()
            doc.delete()
            deleted += 1
        except Exception as e:
            print(f"  ERROR {i.name}: {e}")
            print(frappe.get_traceback())
            errors += 1
    frappe.db.commit()
    print(f"  Loans deleted: {deleted}, errors: {errors}")

    # ── Loan Applications ─────────────────────────────────────────
    print("\nDeleting Loan Applications...")
    apps = frappe.get_all("Loan Application", fields=["name", "docstatus"])
    deleted = errors = 0
    for i in apps:
        try:
            doc = frappe.get_doc("Loan Application", i.name)
            if doc.docstatus == 1:
                doc.cancel()
            doc.delete()
            deleted += 1
        except Exception as e:
            print(f"  ERROR {i.name}: {e}")
            errors += 1
    frappe.db.commit()
    print(f"  Loan Applications deleted: {deleted}, errors: {errors}")

    # ── Customers ─────────────────────────────────────────────────
    # Only delete customers created by migration (have custom_cnic_no set)
    print("\nDeleting migrated Customers...")
    customers = frappe.get_all(
        "Customer", filters={"custom_cnic_no": ["!=", ""]}, fields=["name"]
    )
    deleted = errors = 0
    for i in customers:
        try:
            frappe.delete_doc("Customer", i.name, force=True, ignore_permissions=True)
            deleted += 1
        except Exception as e:
            print(f"  ERROR {i.name}: {e}")
            errors += 1
    frappe.db.commit()
    print(f"  Customers deleted: {deleted}, errors: {errors}")

    print("\n✅ Deletion complete.")
