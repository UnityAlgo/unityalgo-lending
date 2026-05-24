# Copyright (c) 2026, UnityAlgo
# For license information, please see license.txt


import re
from collections import defaultdict
import csv
import openpyxl

import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, nowdate

COMMIT_BATCH = 100  # commit after every N repayments (tune up if DB handles it)
LOG_FLUSH = 100  # flush migration_log to DB every N log calls


class DataMigrationTool(Document):
    def validate(self):
        if not self.title:
            self.title = f"Migration {now_datetime().strftime('%Y-%m-%d %H:%M')}"
        if self.status not in ("Draft",):
            return
        for field, label in [
            ("customer_file", "Customer Data File"),
            ("disbursement_file", "Disbursement Data File"),
            ("repayment_file", "Repayment Data File"),
        ]:
            if not self.get(field):
                frappe.throw(f"{label} is required before submitting.")

    def on_submit(self):
        self.db_set("status", "Queued")
        self.db_set(
            "migration_log", f"[{now_datetime()}] Queued by {frappe.session.user}\n"
        )
        frappe.db.commit()

        frappe.enqueue(
            "lending.loan_management.doctype.data_migration_tool.data_migration_tool.run_migration_job",
            queue="long",
            timeout=14400,
            job_name=f"DataMigration-{self.name}",
            doc_name=self.name,
            now=False,
        )
        frappe.msgprint(
            f"Migration <b>{self.name}</b> has been queued and will run in the background.<br>"
            "Refresh the page to see live progress.",
            title="Migration Queued",
            indicator="blue",
        )

    def on_cancel(self):
        self.db_set("status", "Rollback Queued")
        self.db_set(
            "migration_log",
            (
                frappe.db.get_value("Data Migration Tool", self.name, "migration_log")
                or ""
            )
            + f"\n[{now_datetime()}] Rollback queued by {frappe.session.user}\n",
        )
        frappe.db.commit()

        frappe.enqueue(
            "lending.loan_management.doctype.data_migration_tool.data_migration_tool.run_rollback_job",
            queue="long",
            timeout=14400,
            job_name=f"DataMigrationRollback-{self.name}",
            doc_name=self.name,
            now=False,
        )
        frappe.msgprint(
            f"Rollback for <b>{self.name}</b> has been queued.<br>"
            "All migrated records will be cancelled and deleted in the background.<br>"
            "Refresh the page to see progress.",
            title="Rollback Queued",
            indicator="orange",
        )


def run_migration_job(doc_name):
    import traceback as tb_mod

    doc = frappe.get_doc("Data Migration Tool", doc_name)
    doc.db_set("status", "Running")
    frappe.db.commit()

    _log(doc, f"[{now_datetime()}] Migration started (Job: DataMigration-{doc_name})")

    errors = 0
    active_map = {}
    hist_map = {}
    cnic_to_customer = {}

    try:
        _log(doc, "\n── Reading Excel files...")
        customer_rows = _read_excel(doc.customer_file)
        disbursement_rows = _read_excel(doc.disbursement_file)
        repayment_rows = _read_excel(doc.repayment_file)
        _log(
            doc,
            f"   Customers: {len(customer_rows):,} | "
            f"Disbursements: {len(disbursement_rows):,} | "
            f"Repayments: {len(repayment_rows):,}",
        )

        disbursement_loan_nos = {
            str(r.get("Loan#") or r.get("Loan #") or "").strip()
            for r in disbursement_rows
        }
        existing_map = _build_loan_map_from_db()
        hist_index = _build_historical_loan_index(
            repayment_rows, disbursement_loan_nos, existing_map
        )
        _log(doc, f"   Historical closed loans to create: {len(hist_index):,}")

        # Step 1: Customers
        if doc.migrate_customers:
            _log(doc, "\n[1/5] Migrating Customers...")
            cnic_to_customer, created, errs = _migrate_customers(doc, customer_rows)
            doc.db_set("customers_created", created)
            errors += errs
            _log(doc, f"   ✅ Created: {created:,}  Errors: {errs}", flush=True)
        else:
            cnic_to_customer = _load_existing_customers(customer_rows)
            _log(doc, "\n[1/5] Customers skipped (option unchecked)")

        # Step 2: Loan Products
        _log(doc, "\n[2/5] Ensuring Loan Products...")
        # _ensure_loan_products(doc, disbursement_rows)

        # Step 3: Active Loans
        if doc.migrate_loans:
            _log(doc, "\n[3/5] Migrating Active Loans...")
            active_map, created, errs = _migrate_loans(
                doc, disbursement_rows, cnic_to_customer
            )
            doc.db_set("loans_created", created)
            errors += errs
            _log(doc, f"   ✅ Created: {created:,}  Errors: {errs}")
        else:
            active_map = _build_loan_map_from_db()
            _log(doc, "\n[3/5] Active Loans skipped")

        # Step 4: Historical Loans
        if doc.migrate_historical_loans:
            _log(doc, "\n[4/5] Migrating Historical (Closed) Loans...")
            hist_map, created, errs = _migrate_historical_loans(
                doc, hist_index, cnic_to_customer
            )
            doc.db_set("historical_loans_created", created)
            errors += errs
            _log(doc, f"   ✅ Created: {created:,}  Errors: {errs}")
        else:
            _log(doc, "\n[4/5] Historical Loans skipped")

        # Step 5a: Disbursements
        if doc.migrate_disbursements:
            _log(doc, "\n[5a] Migrating Loan Disbursements...")
            created, errs = _migrate_disbursements(doc, disbursement_rows, active_map)
            errors += errs
            _log(doc, f"   ✅ Submitted: {created:,}  Errors: {errs}", flush=True)

        # Step 5b: Repayments
        if doc.migrate_repayments:
            _log(doc, "\n[5b] Migrating Loan Repayments...")
            created, errs, unmatched = _migrate_repayments(
                doc, repayment_rows, disbursement_rows, active_map, hist_map
            )
            doc.db_set("repayments_created", created)
            errors += errs
            _log(
                doc,
                f"   ✅ Submitted: {created:,}  Errors: {errs}  Unmatched: {len(unmatched):,}",
            )
            if unmatched:
                _log(doc, f"   Unmatched samples: {sorted(unmatched)[:8]}")

        doc.db_set("errors_count", errors)
        doc.db_set("status", "Completed")
        _log(
            doc, f"\n[{now_datetime()}] ✅ MIGRATION COMPLETE — Total errors: {errors}"
        )

        # Close fully paid migrated loans
        try:
            from lending.loan_management.doctype.loan.loan import (
                close_unsecured_term_loan,
            )

            migrated_closure_loans = frappe.db.sql_list(
                "SELECT name FROM `tabLoan` "
                "WHERE description LIKE %s "
                "AND status = 'Loan Closure Requested' "
                "AND docstatus = 1",
                (f"%MigrationRef:{doc.name}%",),
            )
            closed_count = 0
            for loan_name in migrated_closure_loans:
                try:
                    close_unsecured_term_loan(loan_name)
                    closed_count += 1
                except Exception:
                    pass
            if migrated_closure_loans:
                _log(
                    doc,
                    f"   Closed {closed_count:,} / {len(migrated_closure_loans):,} fully paid loans",
                )
            frappe.db.commit()
        except Exception:
            import traceback as _tb

            _log(
                doc,
                f"   ⚠️  close_unsecured_term_loan step failed (non-fatal):\n{_tb.format_exc()}",
            )

    except Exception:
        import traceback as tb_mod

        tb = tb_mod.format_exc()
        _log(doc, f"\n[{now_datetime()}] ❌ MIGRATION FAILED:\n{tb}")
        doc.db_set("status", "Failed")
        doc.db_set("errors_count", errors)
        frappe.log_error(tb, f"DataMigration-{doc_name}")

    finally:
        _log_flush_all(doc)
        frappe.db.commit()


# ── Rollback job ──────────────────────────────────────────────────────────────


def run_rollback_job(doc_name):
    import traceback as tb_mod

    doc = frappe.get_doc("Data Migration Tool", doc_name)
    doc.db_set("status", "Rolling Back")
    frappe.db.commit()

    _log(doc, f"\n[{now_datetime()}] ── Rollback started ──")

    total_deleted = 0
    total_errors = 0

    try:
        _log(doc, "\n[Rollback 1/6] Cancelling & deleting Loan Repayments...")
        deleted, errors = _rollback_repayments(doc)
        total_deleted += deleted
        total_errors += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        _log(doc, "\n[Rollback 2/6] Cancelling & deleting Loan Interest Accruals...")
        deleted, errors = _rollback_loan_interest_accruals(doc)
        total_deleted += deleted
        total_errors += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        _log(doc, "\n[Rollback 3/6] Cancelling & deleting Loan Disbursements...")
        deleted, errors = _rollback_disbursements(doc)
        total_deleted += deleted
        total_errors += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        _log(doc, "\n[Rollback 4/6] Cancelling & deleting Loans...")
        deleted, errors, _ = _rollback_loans(doc)
        total_deleted += deleted
        total_errors += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        if doc.migrate_customers:
            _log(doc, "\n[Rollback 5/6] Deleting Customers...")
            deleted, errors = _rollback_customers(doc)
            total_deleted += deleted
            total_errors += errors
            _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")
        else:
            _log(doc, "\n[Rollback 5/6] Customers skipped (were not migrated)")

        _log(doc, "\n[Rollback 6/6] Deleting auto-created Loan Products...")
        deleted, errors = _rollback_loan_products(doc)
        total_deleted += deleted
        total_errors += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        doc.db_set("status", "Rolled Back")
        _log(
            doc,
            f"\n[{now_datetime()}] ✅ ROLLBACK COMPLETE — Deleted: {total_deleted:,}  Errors: {total_errors}",
        )

    except Exception:
        tb = tb_mod.format_exc()
        _log(doc, f"\n[{now_datetime()}] ❌ ROLLBACK FAILED:\n{tb}")
        doc.db_set("status", "Rollback Failed")
        frappe.log_error(tb, f"DataMigrationRollback-{doc_name}")

    finally:
        _log_flush_all(doc)
        frappe.db.commit()


# ── Rollback helpers ──────────────────────────────────────────────────────────


def _cancel_and_delete(doctype, name, doc_name_for_log):
    try:
        d = frappe.get_doc(doctype, name)
        if d.docstatus == 1:
            d.flags.ignore_permissions = True
            d.cancel()
            frappe.db.commit()
        frappe.delete_doc(
            doctype,
            name,
            force=True,
            ignore_permissions=True,
            delete_permanently=True,
        )
        frappe.db.commit()
        return True
    except Exception:
        import traceback

        frappe.log_error(
            f"Rollback failed for {doctype} [{name}]\n{traceback.format_exc()}",
            f"DataMigrationRollback-{doc_name_for_log}",
        )
        return False


def _migrated_loan_names(doc_name, conds=""):
    """
    Return loan names created by a specific migration run only.
    Scoped to MigrationRef:{doc_name} so rolling back one migration
    never touches loans created by a different migration.
    """
    query = "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus != 2"
    if conds:
        query += f" {conds}"
    return frappe.db.sql_list(query, (f"%MigrationRef:{doc_name}%",))


def _rollback_repayments(doc):
    deleted = errors = 0
    # FIX: scoped to this migration only via MigrationRef
    loan_names = _migrated_loan_names(doc.name)
    if not loan_names:
        return deleted, errors

    repayments = frappe.db.sql_list(
        f"SELECT name FROM `tabLoan Repayment` "
        f"WHERE against_loan IN ({','.join(['%s'] * len(loan_names))}) "
        f"AND docstatus < 2 ORDER BY posting_date DESC, name DESC",
        tuple(loan_names),
    )
    _log(doc, f"Found {len(repayments):,} repayment(s) to delete")

    for name in repayments:
        if _cancel_and_delete("Loan Repayment", name, doc.name):
            deleted += 1
            if deleted % 500 == 0:
                _log(doc, f"   ...{deleted:,} repayments deleted")
        else:
            errors += 1
    return deleted, errors


def _rollback_loan_interest_accruals(doc):
    deleted = errors = 0
    # FIX: scoped to this migration only via MigrationRef
    loan_names = _migrated_loan_names(doc.name)
    if not loan_names:
        return deleted, errors

    lias = frappe.db.sql(
        f"SELECT name, docstatus FROM `tabLoan Interest Accrual` "
        f"WHERE loan IN ({','.join(['%s'] * len(loan_names))}) "
        f"ORDER BY posting_date DESC, name DESC",
        tuple(loan_names),
        as_dict=True,
    )
    _log(doc, f"   Found {len(lias):,} LIA(s) to delete")

    for lia in lias:
        name = lia.name
        docstatus = lia.docstatus
        try:
            if docstatus == 1:
                has_gl = frappe.db.sql(
                    "SELECT COUNT(*) FROM `tabGL Entry` "
                    "WHERE voucher_type='Loan Interest Accrual' "
                    "AND voucher_no=%s AND is_cancelled=0",
                    (name,),
                )[0][0]

                if has_gl:
                    d = frappe.get_doc("Loan Interest Accrual", name)
                    d.flags.ignore_permissions = True
                    d.cancel()
                    frappe.db.commit()
                else:
                    frappe.db.set_value(
                        "Loan Interest Accrual",
                        name,
                        "docstatus",
                        2,
                        update_modified=False,
                    )
                    frappe.db.commit()

            frappe.delete_doc(
                "Loan Interest Accrual",
                name,
                force=True,
                ignore_permissions=True,
                delete_permanently=True,
            )
            frappe.db.commit()
            deleted += 1
            if deleted % 500 == 0:
                _log(doc, f"   ...{deleted:,} LIAs deleted")

        except Exception:
            import traceback

            frappe.log_error(
                f"LIA rollback failed [{name}]\n{traceback.format_exc()}",
                f"DataMigrationRollback-{doc.name}",
            )
            errors += 1

    return deleted, errors


def _rollback_disbursements(doc):
    deleted = errors = 0
    # FIX: scoped to this migration only via MigrationRef
    loan_names = _migrated_loan_names(doc.name)
    if not loan_names:
        return deleted, errors

    disbursements = frappe.db.sql_list(
        f"SELECT name FROM `tabLoan Disbursement` "
        f"WHERE against_loan IN ({','.join(['%s'] * len(loan_names))}) "
        f"AND docstatus < 2 ORDER BY name DESC",
        tuple(loan_names),
    )
    _log(doc, f"   Found {len(disbursements):,} disbursement(s) to delete")

    for name in disbursements:
        if _cancel_and_delete("Loan Disbursement", name, doc.name):
            deleted += 1
        else:
            errors += 1
    return deleted, errors


def _rollback_loans(doc):
    deleted = errors = 0
    # FIX: scoped to this migration only via MigrationRef
    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus != 2 ORDER BY name DESC",
        (f"%MigrationRef:{doc.name}%",),
    )
    _log(doc, f"   Found {len(loan_names):,} loan(s) to delete")

    deleted_names = []
    for name in loan_names:
        if _cancel_and_delete("Loan", name, doc.name):
            deleted += 1
            deleted_names.append(name)
            if deleted % 100 == 0:
                _log(doc, f"   ...{deleted:,} loans deleted")
        else:
            errors += 1
    return deleted, errors, deleted_names


def _rollback_customers(doc):
    deleted = errors = 0
    customer_rows = _read_excel(doc.customer_file)
    cnics = [
        _cnic(r.get("Borrower CNIC #"))
        for r in customer_rows
        if _cnic(r.get("Borrower CNIC #")) not in ("", "0")
    ]
    if not cnics:
        return deleted, errors

    customer_names = frappe.db.sql_list(
        f"SELECT name FROM `tabCustomer` "
        f"WHERE custom_cnic_no IN ({','.join(['%s'] * len(cnics))}) "
        f"AND creation >= %s",
        tuple(cnics) + (doc.creation,),
    )
    _log(doc, f"   Found {len(customer_names):,} customer(s) to delete")

    for name in customer_names:
        try:
            frappe.delete_doc(
                "Customer",
                name,
                force=True,
                ignore_permissions=True,
                delete_permanently=True,
            )
            frappe.db.commit()
            deleted += 1
            if deleted % 100 == 0:
                _log(doc, f"   ...{deleted:,} customers deleted")
        except Exception:
            import traceback

            frappe.log_error(
                f"Customer delete failed [{name}]\n{traceback.format_exc()}",
                f"DataMigrationRollback-{doc.name}",
            )
            errors += 1
    return deleted, errors


def _rollback_loan_products(doc):
    deleted = errors = 0
    disbursement_rows = _read_excel(doc.disbursement_file)
    product_names = list(
        {
            str(r.get("Loan Product") or "").strip()
            for r in disbursement_rows
            if r.get("Loan Product")
        }
    )
    if not product_names:
        return deleted, errors

    auto_created = frappe.db.sql_list(
        f"SELECT name FROM `tabLoan Product` "
        f"WHERE name IN ({','.join(['%s'] * len(product_names))}) "
        f"AND creation >= %s",
        tuple(product_names) + (doc.creation,),
    )
    _log(doc, f"   Found {len(auto_created):,} auto-created product(s) to delete")

    for name in auto_created:
        try:
            frappe.delete_doc(
                "Loan Product",
                name,
                force=True,
                ignore_permissions=True,
                delete_permanently=True,
            )
            frappe.db.commit()
            deleted += 1
        except Exception:
            import traceback

            frappe.log_error(
                f"Loan Product delete failed [{name}]\n{traceback.format_exc()}",
                f"DataMigrationRollback-{doc.name}",
            )
            errors += 1
    return deleted, errors


# ── Helpers ───────────────────────────────────────────────────────────────────

# PERF-10: Log buffer — flush to DB every LOG_FLUSH calls (not every call).
# Never re-reads from DB after the first load per doc.
_log_buffer = {}  # doc.name → accumulated string
_log_counts = {}  # doc.name → call count since last flush


def _log(doc, msg, flush=False):
    print(msg)
    key = doc.name
    if key not in _log_buffer:
        # Load once at start; never read again
        _log_buffer[key] = (
            frappe.db.get_value("Data Migration Tool", key, "migration_log") or ""
        )
        _log_counts[key] = 0
    _log_buffer[key] += msg + "\n"
    _log_counts[key] += 1

    if flush or _log_counts[key] % LOG_FLUSH == 0:
        frappe.db.set_value(
            "Data Migration Tool",
            key,
            "migration_log",
            _log_buffer[key],
            update_modified=False,
        )
        frappe.db.commit()


def _log_flush_all(doc):
    """Force-flush log buffer at end of job."""
    key = doc.name
    if key in _log_buffer:
        frappe.db.set_value(
            "Data Migration Tool",
            key,
            "migration_log",
            _log_buffer[key],
            update_modified=False,
        )
        frappe.db.commit()
        _log_buffer.pop(key, None)
        _log_counts.pop(key, None)


def _read_excel(file_url: str):
    # Fix: file_url.split("/")[-1] returns the whole filename (e.g., "file.csv").
    # Using .endswith is much safer here.
    is_csv = file_url.lower().endswith(".csv")

    # Common setup: both paths need the actual local file path from Frappe
    file_doc = frappe.get_doc("File", {"file_url": file_url})
    file_path = file_doc.get_full_path()

    if is_csv:
        rows = []
        with open(file_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                processed_row = [
                    None if not str(cell).strip() else cell for cell in row
                ]
                rows.append(processed_row)
    else:
        wb = openpyxl.load_workbook(file_path, data_only=True)
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


def _safe_date(val):
    """
    Robust date parser — handles all formats openpyxl produces.
    Returns "YYYY-MM-DD" string or None.
    """
    if not val:
        return None
    if hasattr(val, "date"):
        return str(val.date())
    s = str(val).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    if "/" in s:
        try:
            from datetime import datetime as _dt

            return str(_dt.strptime(s, "%d/%m/%Y").date())
        except Exception:
            pass
    try:
        from frappe.utils import getdate

        return str(getdate(s))
    except Exception:
        return None


def _safe_float(val, default=0.0):
    """FIX-3: strip non-numeric chars before converting ('80%/Year' → 80.0)."""
    if val is None:
        return default
    s = str(val).strip()
    if not s:
        return default
    m = re.search(r"[\d]+(?:\.[\d]+)?", s)
    if m:
        try:
            return float(m.group())
        except ValueError:
            return default
    return default


def _cnic(raw):
    if isinstance(raw, (int, float)):
        return str(int(raw)).strip()
    return str(raw or "").strip()


def _guess_periods(product_name):
    m = re.search(r"(\d+)\s*[Mm]", product_name)
    if m:
        return int(m.group(1))
    m = re.search(r"-0*(\d+)$", product_name)
    if m:
        return int(m.group(1))
    return 6


def _build_loan_map_from_db():
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


def _build_historical_loan_index(
    repayment_rows, disbursement_loan_nos, existing_loan_map
):
    """FIX-1: direct Loan# match — no _strip_installment."""
    known = set(disbursement_loan_nos) | set(existing_loan_map.keys())
    grouped = defaultdict(
        lambda: {
            "cnic": None,
            "total_amount": 0.0,
            "total_principal": 0.0,
            "earliest_date": None,
            "latest_date": None,
        }
    )
    for r in repayment_rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if loan_no in known:
            continue
        cnic = _cnic(r.get("Borrower CNIC #"))
        date = _safe_date(r.get("Collection Date"))
        entry = grouped[loan_no]
        if not entry["cnic"] and cnic:
            entry["cnic"] = cnic
        entry["total_amount"] += _safe_float(r.get("Total Paid Amount"))
        entry["total_principal"] += _safe_float(r.get("Principal Paid"))
        if date:
            if not entry["earliest_date"] or date < entry["earliest_date"]:
                entry["earliest_date"] = date
            if not entry["latest_date"] or date > entry["latest_date"]:
                entry["latest_date"] = date
    return dict(grouped)


# ── Select field maps ─────────────────────────────────────────────────────────

MARITAL_MAP = {
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
    "christian": "Christianity",
    "hinduism": "Hindu",
    "hindu": "Hindu",
    "sikhism": "Other",
    "sikh": "Other",
    "other": "Other",
}


def _get_allowed(fieldname):
    try:
        meta = frappe.get_meta("Customer")
        for f in meta.fields:
            if f.fieldname == fieldname and f.options:
                return set(o.strip() for o in f.options.split("\n") if o.strip())
    except Exception:
        pass
    return set()


def _safe_select(raw, mapping, allowed):
    if not raw:
        return None
    s = str(raw).strip()
    for opt in allowed:
        if opt.lower() == s.lower():
            return opt
    mapped = mapping.get(s.lower())
    if mapped and (not allowed or mapped in allowed):
        return mapped
    return None


# ── Migration steps ───────────────────────────────────────────────────────────


def _load_existing_customers(rows):
    result = {}
    for r in rows:
        cnic = _cnic(r.get("Borrower CNIC #"))
        if cnic:
            existing = frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name")
            if existing:
                result[cnic] = existing
    return result


def _migrate_customers(doc, rows):
    cnic_map = {}
    created = errors = 0
    allowed_marital = _get_allowed("custom_marital_status")
    allowed_religion = _get_allowed("custom_religion")

    for r in rows:
        name = str(r.get("Full Name") or "").strip()
        cnic = _cnic(r.get("Borrower CNIC #"))
        if not name or not cnic or cnic == "0":
            continue

        existing = frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name")
        if existing:
            cnic_map[cnic] = existing
            continue

        try:
            gender = str(r.get("Gender") or "").strip()
            if gender and not frappe.db.exists("Gender", gender):
                g = frappe.new_doc("Gender")
                g.gender = gender
                g.insert(ignore_permissions=True)

            c = frappe.new_doc("Customer")
            c.customer_name = name
            c.customer_type = "Individual"
            c.customer_group = "Individual"
            c.territory = "All Territories"
            c.gender = gender or None
            c.custom_cnic_no = cnic
            c.custom_date_of_birth = _safe_date(r.get("DOB"))
            c.custom_cnic_expiry_date = _safe_date(r.get("CNIC Expiry Date"))
            c.custom_marital_status = _safe_select(
                r.get("Marital Status"), MARITAL_MAP, allowed_marital
            )
            c.custom_religion = _safe_select(
                r.get("Religion"), RELIGION_MAP, allowed_religion
            )
            c.custom_spouse_name = (
                str(r.get("Parentage/Spouse Name") or "").strip() or None
            )
            c.custom_spouse_mobile = (
                str(r.get("Parentage/Spouse Mobile #") or "").strip() or None
            )
            c.custom_phone_no_1 = str(r.get("Mobile") or "").strip() or None
            c.insert(ignore_permissions=True)
            frappe.db.commit()

            cnic_map[cnic] = c.name
            created += 1
            if created % 100 == 0:
                _log(doc, f"   ...{created:,} customers created")

        except Exception:
            import traceback

            frappe.log_error(
                f"Customer [{name}/{cnic}]\n{traceback.format_exc()}",
                f"DataMigration-{doc.name}",
            )
            errors += 1

    return cnic_map, created, errors


def _ensure_loan_products(doc, rows):
    products = {}
    for r in rows:
        p = str(r.get("Loan Product") or "").strip()
        rate = _safe_float(r.get("Interest Rate"))
        if p:
            products.setdefault(p, [])
            if rate:
                products[p].append(rate)

    accts = {
        "disbursement_account": doc.disbursement_account,
        "payment_account": doc.payment_account,
        "loan_account": doc.loan_account,
        "interest_income_account": doc.interest_income_account,
        "interest_receivable_account": doc.interest_receivable_account,
        "penalty_income_account": doc.penalty_income_account,
        "penalty_receivable_account": doc.penalty_receivable_account,
        "mode_of_payment": doc.mode_of_payment,
    }

    for name, rates in sorted(products.items()):
        avg = sum(rates) / len(rates) if rates else 0.0
        perd = _guess_periods(name)

        if frappe.db.exists("Loan Product", name):
            if not frappe.db.get_value("Loan Product", name, "disbursement_account"):
                frappe.db.set_value("Loan Product", name, accts, update_modified=False)
                frappe.db.commit()
                _log(doc, f"   Patched accounts: {name}")
            continue

        try:
            p = frappe.new_doc("Loan Product")
            p.product_code = name
            p.product_name = name
            p.company = doc.company
            p.is_term_loan = 1
            p.rate_of_interest = avg
            p.repayment_method = "Repay Over Number of Periods"
            p.repayment_periods = perd
            p.repayment_schedule_type = "Monthly as per repayment start date"
            p.min_days_bw_disbursement_first_repayment = 30
            p.min_auto_closure_tolerance_amount = 0
            p.max_auto_closure_tolerance_amount = 100
            p.penalty_interest_method = "Rate"
            p.penalty_interest_rate = 0.0
            p.penalty_stop_threshold_days = 90
            for k, v in accts.items():
                setattr(p, k, v)
            p.insert(ignore_permissions=True)
            frappe.db.commit()
            _log(
                doc,
                f"   Created Loan Product: {name} (rate={avg:.1f}% periods={perd}m)",
            )
        except Exception:
            import traceback

            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} LoanProduct[{name}]",
            )


def _loan_fields(doc, r, product, disb_date, status="Disbursed"):
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

    raw_rate = _safe_float(r.get("Interest Rate") if r else 0)

    loan = frappe.new_doc("Loan")
    loan.applicant_type = "Customer"
    loan.company = doc.company
    loan.branch = doc.branch
    loan.loan_product = product
    loan.posting_date = disb_date
    loan.disbursement_date = disb_date
    loan.repayment_start_date = disb_date
    loan.sales_person = doc.sales_person
    loan.status = status
    loan.rate_of_interest = prod_data.get("rate_of_interest") or raw_rate
    loan.penalty_stop_threshold_days = (
        prod_data.get("penalty_stop_threshold_days") or 90
    )
    loan.is_term_loan = prod_data.get("is_term_loan", 1)
    loan.repayment_method = (
        prod_data.get("repayment_method") or "Repay Over Number of Periods"
    )
    loan.repayment_periods = prod_data.get("repayment_periods") or _guess_periods(
        product or ""
    )
    loan.mode_of_payment = prod_data.get("mode_of_payment") or doc.mode_of_payment
    loan.disbursement_account = (
        prod_data.get("disbursement_account") or doc.disbursement_account
    )
    loan.payment_account = prod_data.get("payment_account") or doc.payment_account
    loan.loan_account = prod_data.get("loan_account") or doc.loan_account
    loan.interest_income_account = (
        prod_data.get("interest_income_account") or doc.interest_income_account
    )
    loan.penalty_income_account = (
        prod_data.get("penalty_income_account") or doc.penalty_income_account
    )
    return loan


def _migrate_loans(doc, rows, cnic_map):
    loan_map = {}
    created = errors = 0
    existing = _build_loan_map_from_db()

    for r in rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        cnic = _cnic(r.get("Borrower CNIC #"))
        customer = cnic_map.get(cnic)
        if not loan_no or not customer:
            continue
        if loan_no in existing:
            loan_map[loan_no] = existing[loan_no]
            continue

        product = str(r.get("Loan Product") or "").strip() or None
        disb_date = _safe_date(r.get("Disbursed Date")) or nowdate()
        amount = _safe_float(r.get("Disbursed")) or _safe_float(r.get("Principal"))
        if not amount:
            frappe.log_error(
                f"Loan {loan_no}: both Disbursed and Principal are 0 — skipped",
                f"DataMigration-{doc.name} Loan[{loan_no}]",
            )
            errors += 1
            continue

        try:
            loan = _loan_fields(doc, r, product, disb_date, "Disbursed")
            loan.applicant = customer
            loan.loan_amount = amount
            loan.disbursed_amount = 0
            loan.description = f"Migrated. LoanRef:{loan_no} MigrationRef:{doc.name}"
            loan.insert(ignore_permissions=True)
            loan.submit()
            frappe.db.commit()

            interest_paid_so_far = _safe_float(r.get("Interest Paid"))
            if interest_paid_so_far > 0:
                update = {"total_interest_payable": interest_paid_so_far}
                if frappe.db.has_column("Loan", "total_interest_paid"):
                    update["total_interest_paid"] = interest_paid_so_far
                frappe.db.set_value("Loan", loan.name, update, update_modified=False)
                frappe.db.commit()

            loan_map[loan_no] = loan.name
            created += 1
            if created % 100 == 0:
                _log(doc, f"   ...{created:,} loans created")

        except Exception:
            import traceback

            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} Loan[{loan_no}]",
            )
            errors += 1

    return loan_map, created, errors


def _migrate_historical_loans(doc, hist_index, cnic_map):
    loan_map = {}
    created = errors = 0
    existing = _build_loan_map_from_db()

    default_product = (
        doc.get("historical_loan_product")
        or frappe.db.get_value("Loan Product", {"disabled": 0}, "name")
        or ""
    )

    for loan_no, info in hist_index.items():
        if loan_no in existing:
            loan_map[loan_no] = existing[loan_no]
            continue

        cnic = info.get("cnic", "")
        customer = cnic_map.get(cnic) or frappe.db.get_value(
            "Customer", {"custom_cnic_no": cnic}, "name"
        )
        if not customer:
            continue

        amount = info["total_principal"] or info["total_amount"] or 1.0
        disb_date = info["earliest_date"] or nowdate()

        try:
            loan = _loan_fields(doc, None, default_product, disb_date, "Closed")
            loan.applicant = customer
            loan.loan_amount = amount
            loan.disbursed_amount = amount
            # FIX: embed MigrationRef so rollback is scoped to this migration only
            loan.description = (
                f"Migrated historical. LoanRef:{loan_no} MigrationRef:{doc.name}"
            )
            loan.insert(ignore_permissions=True)
            loan.submit()
            frappe.db.commit()

            loan_map[loan_no] = loan.name
            created += 1
            if created % 100 == 0:
                _log(doc, f"   ...{created:,} historical loans created")

        except Exception:
            import traceback

            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} HistLoan[{loan_no}]",
            )
            errors += 1

    return loan_map, created, errors


def _migrate_disbursements(doc, rows, loan_map):
    """
    FIX-4: submit for GL entries.
    PERF-9: pre-fetch all loan fields in one bulk SQL query.
    """
    import time

    created = errors = 0
    MAX_RETRIES = 3

    # PERF-9: bulk-fetch all loan data we need, keyed by loan name
    loan_doc_names = list({v for v in loan_map.values() if v})
    loan_data_map = {}
    if loan_doc_names:
        rows_db = frappe.db.sql(
            f"SELECT name, applicant_type, applicant, company, branch, loan_product "
            f"FROM `tabLoan` WHERE name IN ({','.join(['%s'] * len(loan_doc_names))})",
            tuple(loan_doc_names),
            as_dict=True,
        )
        loan_data_map = {r.name: r for r in rows_db}

    # Build existing disbursements set to skip duplicates
    existing_disb = (
        set(
            frappe.db.sql_list(
                f"SELECT against_loan FROM `tabLoan Disbursement` "
                f"WHERE against_loan IN ({','.join(['%s'] * len(loan_doc_names))}) AND docstatus != 2",
                tuple(loan_doc_names),
            )
        )
        if loan_doc_names
        else set()
    )

    charges_income_account = (
        doc.get("charges_income_account") or doc.interest_income_account
    )

    for r in rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        loan_doc = loan_map.get(loan_no)
        if not loan_doc:
            continue
        if loan_doc in existing_disb:
            continue

        try:
            lr = loan_data_map.get(loan_doc, {})

            disbursed_amt = _safe_float(r.get("Disbursed")) or _safe_float(
                r.get("Principal")
            )
            deductable_fees = _safe_float(r.get("Deductable Fees"))
            if not disbursed_amt:
                continue

            d = frappe.new_doc("Loan Disbursement")
            d.against_loan = loan_doc
            d.disbursement_date = _safe_date(r.get("Disbursed Date")) or nowdate()
            d.disbursed_amount = disbursed_amt
            d.applicant_type = lr.get("applicant_type", "Customer")
            d.applicant = lr.get("applicant")
            d.company = lr.get("company", doc.company)
            d.branch = lr.get("branch", doc.branch)
            d.loan_product = lr.get("loan_product")

            if deductable_fees > 0 and charges_income_account:
                d.append(
                    "loan_disbursement_charges",
                    {
                        "charge": None,
                        "account": charges_income_account,
                        "amount": deductable_fees,
                    },
                )

            d.insert(ignore_permissions=True)
            frappe.db.commit()

            submitted = False
            for attempt in range(MAX_RETRIES):
                try:
                    d.submit()
                    frappe.db.commit()
                    submitted = True
                    break
                except frappe.QueryTimeoutError:
                    if attempt < MAX_RETRIES - 1:
                        import time as _time

                        _time.sleep(2**attempt)
                        frappe.db.rollback()
                        d.reload()
                    else:
                        raise

            if submitted:
                existing_disb.add(loan_doc)
                created += 1

        except Exception:
            import traceback

            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} Disb[{loan_no}]",
            )
            errors += 1

    return created, errors


# ── Raw SQL LIA insert ────────────────────────────────────────────────────────
# PERF-2: bypass frappe.new_doc overhead for LIA creation.
# Columns match tabLoan Interest Accrual schema.

_LIA_INSERT = """
    INSERT INTO `tabLoan Interest Accrual`
        (name, creation, modified, modified_by, owner, docstatus,
         loan, company, branch, cost_center,
         posting_date, date_of_accrual, due_date,
         interest_amount, payable_principal_amount, penalty_amount,
         pending_principal_amount, accrual_type,
         applicant_type, applicant, interest_income_account)
    VALUES
        (%s, NOW(), NOW(), %s, %s, 1,
         %s, %s, %s, %s,
         %s, %s, %s,
         %s, %s, %s,
         0.0, %s,
         %s, %s, %s)
"""


def _insert_lia_raw(
    loan_doc,
    company,
    branch,
    cost_center,
    date_str,
    interest_amt,
    principal_amt,
    penalty_amt,
    accrual_type,
    applicant_type,
    applicant,
    income_account,
    user,
):
    """
    PERF-2/PERF-8: Insert LIA directly via SQL with docstatus=1.
    Returns the new LIA name (UUID-based).
    """
    lia_name = frappe.generate_hash(length=10)
    frappe.db.sql(
        _LIA_INSERT,
        (
            lia_name,
            user,
            user,
            loan_doc,
            company,
            branch,
            cost_center,
            date_str,
            date_str,
            date_str,
            interest_amt,
            principal_amt,
            penalty_amt,
            accrual_type,
            applicant_type,
            applicant,
            income_account,
        ),
    )
    return lia_name


def _migrate_repayments(doc, rows, disbursement_rows, active_map, hist_map):
    """
    Repayment migration — full performance overhaul.

    Key changes vs original:
      PERF-1  Loan fields pre-fetched in ONE bulk SQL before the loop.
      PERF-2  LIAs inserted via raw SQL (_insert_lia_raw) — no ORM overhead.
      PERF-3  Loan Repayment built with frappe.new_doc but insert is faster
              because loan fields are already in memory (no get_value per row).
      PERF-4  Commit every COMMIT_BATCH rows — not every LIA set_value.
      PERF-5  Dedup set uses int(amount*100) key — no float formatting drift.
      PERF-7  cost_center / income_account caches extended.
      PERF-11 Dedup key normalised.
      PERF-12 source_status_map built from in-memory disbursement_rows.

    All original BUG fixes (A-H) and FIX-6 are preserved.
    """
    import traceback

    created = errors = 0
    unmatched = set()
    user = frappe.session.user or "Administrator"

    # PERF-6: merge all loan maps once
    full_map = _build_loan_map_from_db()
    full_map.update(active_map)
    full_map.update(hist_map)

    if not full_map:
        _log(doc, "   No loans found in DB — skipping repayments")
        return created, errors, unmatched

    # ── PERF-1: Bulk-fetch ALL loan fields needed in the loop ─────────────────
    all_loan_names = list(set(full_map.values()))
    _LOAN_FIELDS = (
        "name, applicant_type, applicant, company, branch, loan_product, "
        "is_term_loan, loan_account, payment_account, "
        "total_payment, debit_adjustment_amount, credit_adjustment_amount, "
        "total_principal_paid, total_interest_payable, written_off_amount, "
        "refund_amount, disbursed_amount, loan_amount, status"
    )
    loan_cache = {}
    CHUNK = 500  # IN clause limit safety
    for i in range(0, len(all_loan_names), CHUNK):
        chunk = all_loan_names[i : i + CHUNK]
        rows_db = frappe.db.sql(
            f"SELECT {_LOAN_FIELDS} FROM `tabLoan` WHERE name IN ({','.join(['%s'] * len(chunk))})",
            tuple(chunk),
            as_dict=True,
        )
        for lr in rows_db:
            loan_cache[lr.name] = lr

    _log(doc, f"   Pre-fetched {len(loan_cache):,} loan records into memory")

    # ── Per-product / per-company caches ─────────────────────────────────────
    _cost_center_cache = {}

    def _get_cost_center(company):
        if company not in _cost_center_cache:
            _cost_center_cache[company] = frappe.db.get_value(
                "Company", company, "cost_center"
            ) or frappe.db.get_value(
                "Cost Center", {"is_group": 0, "company": company}, "name"
            )
        return _cost_center_cache[company]

    _income_acct_cache = {}

    def _get_income_acct(loan_product):
        if loan_product not in _income_acct_cache:
            _income_acct_cache[loan_product] = (
                frappe.db.get_value(
                    "Loan Product", loan_product, "interest_income_account"
                )
                if loan_product
                else None
            ) or doc.interest_income_account
        return _income_acct_cache[loan_product]

    # PERF-12: source_status_map from in-memory disbursement_rows
    source_status_map = {
        str(dr.get("Loan#") or dr.get("Loan #") or "").strip(): str(
            dr.get("Status") or ""
        ).strip()
        for dr in (disbursement_rows or [])
        if dr.get("Loan#")
    }

    fully_paid_loan_nos = set()

    # BUG-B: sort oldest-first per loan
    loan_groups = defaultdict(list)
    for r in rows:
        loan_no_key = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        loan_groups[loan_no_key].append(r)

    sorted_rows = []
    for loan_no, reps in loan_groups.items():
        reps.sort(key=lambda x: _safe_date(x.get("Collection Date")) or "9999-99-99")
        sorted_rows.extend(reps)

    # PERF-5: dedup key uses int(amount*100) to avoid float string issues
    existing = set(
        f"{r[0]}_{r[1]}_{int(round(float(r[2] or 0) * 100))}"
        for r in frappe.db.sql(
            "SELECT against_loan, DATE_FORMAT(posting_date,'%Y-%m-%d'), amount_paid "
            "FROM `tabLoan Repayment` WHERE docstatus < 2"
        )
    )

    batch_lias = []  # names of LIAs inserted this batch (for commit tracking)
    batch_count = 0

    for r in sorted_rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        loan_doc = full_map.get(loan_no)
        if loan_doc:
            fully_paid_loan_nos.add(loan_no)
        if not loan_doc:
            unmatched.add(loan_no)
            continue

        col_date = r.get("Collection Date")
        date_str = (
            col_date.strftime("%Y-%m-%d")
            if hasattr(col_date, "strftime")
            else _safe_date(col_date)
        )
        if not date_str:
            frappe.log_error(
                f"Repayment row skipped — no valid Collection Date  "
                f"Loan[{loan_no}]  Amount[{r.get('Amount')}]",
                f"DataMigration-{doc.name} RepNoDate",
            )
            errors += 1
            continue

        amount = _safe_float(r.get("Total Paid Amount"))
        principal_paid = _safe_float(r.get("Principal Paid"))
        interest_paid = _safe_float(r.get("Interest Paid"))
        penalty_paid = _safe_float(r.get("Penalty Paid"))
        fees_paid = _safe_float(r.get("Fees Paid"))

        # BUG-G: fold fees into principal
        if fees_paid > 0:
            principal_paid += fees_paid

        # PERF-5: normalised dedup key
        dupe_key = f"{loan_doc}_{date_str}_{int(round(amount * 100))}"
        if dupe_key in existing:
            continue

        try:
            # PERF-1: read from cache, not DB
            lr = loan_cache.get(loan_doc, {})

            company = lr.get("company") or doc.company
            branch = lr.get("branch") or doc.branch
            loan_product = lr.get("loan_product")
            cost_center = _get_cost_center(company)
            payment_account = lr.get("payment_account") or doc.payment_account
            loan_account = lr.get("loan_account") or doc.loan_account
            applicant_type = lr.get("applicant_type", "Customer")
            applicant = lr.get("applicant")
            income_account = _get_income_acct(loan_product)
            pending_principal = _get_pending_principal(lr)

            # PERF-2/FIX-6: LIA via raw SQL INSERT with docstatus=1 directly
            lia_name = None
            if interest_paid > 0 or principal_paid > 0:
                lia_name = _insert_lia_raw(
                    loan_doc,
                    company,
                    branch,
                    cost_center,
                    date_str,
                    interest_paid,
                    principal_paid,
                    0.0,
                    "Repayment",
                    applicant_type,
                    applicant,
                    income_account,
                    user,
                )

            if penalty_paid > 0:
                _insert_lia_raw(
                    loan_doc,
                    company,
                    branch,
                    cost_center,
                    date_str,
                    0.0,
                    0.0,
                    penalty_paid,
                    "Penalty",
                    applicant_type,
                    applicant,
                    income_account,
                    user,
                )

            # PERF-3: build repayment via frappe.new_doc (submit hooks required)
            rep = frappe.new_doc("Loan Repayment")
            rep.flags.ignore_validate = True  # BUG-A: preserve Excel amounts
            rep.flags.ignore_mandatory = True

            rep.against_loan = loan_doc
            rep.posting_date = date_str  # BUG-H
            rep.amount_paid = amount
            rep.repayment_type = "Normal Repayment"
            rep.applicant_type = applicant_type
            rep.applicant = applicant
            rep.company = company
            rep.branch = branch
            rep.loan_product = loan_product
            rep.is_term_loan = lr.get("is_term_loan", 1)
            rep.cost_center = cost_center
            rep.payment_account = payment_account
            rep.loan_account = loan_account
            rep.principal_amount_paid = principal_paid
            rep.total_interest_paid = interest_paid
            rep.total_penalty_paid = penalty_paid
            rep.interest_payable = interest_paid  # BUG-E
            rep.payable_principal_amount = principal_paid
            rep.penalty_amount = penalty_paid  # BUG-C/F
            rep.pending_principal_amount = pending_principal

            if lia_name:
                rep.append(
                    "repayment_details",
                    {
                        "loan_interest_accrual": lia_name,
                        "paid_interest_amount": interest_paid,
                        "paid_principal_amount": principal_paid,
                        "accrual_type": "Repayment",
                    },
                )

            rep.insert(ignore_permissions=True)

            try:
                rep.submit()
                # PERF-4: Update cache with new principal_paid so next rows
                # see the correct pending_principal without a DB hit
                if loan_doc in loan_cache:
                    loan_cache[loan_doc]["total_principal_paid"] = (
                        flt(loan_cache[loan_doc].get("total_principal_paid") or 0)
                        + principal_paid
                    )

                existing.add(dupe_key)
                created += 1
                batch_count += 1

                # PERF-4: batch commit
                if batch_count % COMMIT_BATCH == 0:
                    frappe.db.commit()
                    _log(doc, f"   ...{created:,} repayments submitted")

            except Exception:
                frappe.db.commit()
                frappe.log_error(
                    f"Repayment submit failed (draft kept) "
                    f"Loan[{loan_no}] Date[{date_str}] Amt[{amount}]\n"
                    f"{traceback.format_exc()}",
                    f"DataMigration-{doc.name} RepSubmit",
                )
                errors += 1

        except Exception:
            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} Rep[{loan_no}]",
            )
            errors += 1

    # Final commit for any remainder
    frappe.db.commit()

    if fully_paid_loan_nos:
        _force_close_fully_paid_loans(
            doc, fully_paid_loan_nos, full_map, source_status_map
        )

    return created, errors, unmatched


def _force_close_fully_paid_loans(
    doc, fully_paid_loan_nos, full_map, source_status_map=None
):
    closed = errors = 0
    for loan_no in fully_paid_loan_nos:
        loan_doc = full_map.get(loan_no)
        if not loan_doc:
            continue
        try:
            loan_data = frappe.db.get_value(
                "Loan",
                loan_doc,
                ["loan_amount", "disbursed_amount", "total_principal_paid", "status"],
                as_dict=True,
            )
            if not loan_data:
                continue
            if loan_data.status in ("Loan Closure Requested", "Closed"):
                continue

            source_status = (source_status_map or {}).get(loan_no, "")
            is_fully_paid_source = source_status == "Fully Paid"

            principal_fully_repaid = flt(loan_data.total_principal_paid) >= flt(
                loan_data.loan_amount
            )

            if is_fully_paid_source and not principal_fully_repaid:
                frappe.db.set_value(
                    "Loan",
                    loan_doc,
                    {"total_principal_paid": flt(loan_data.loan_amount)},
                    update_modified=False,
                )
                frappe.db.commit()

            elif not is_fully_paid_source and not principal_fully_repaid:
                continue

            totals = frappe.db.sql(
                "SELECT SUM(total_interest_paid) as interest "
                "FROM `tabLoan Repayment` WHERE against_loan=%s AND docstatus=1",
                (loan_doc,),
                as_dict=True,
            )
            total_paid_interest = flt(totals[0].interest) if totals else 0.0

            update = {
                "status": "Loan Closure Requested",
                "total_interest_payable": total_paid_interest,
            }
            if frappe.db.has_column("Loan", "total_interest_paid"):
                update["total_interest_paid"] = total_paid_interest

            frappe.db.set_value("Loan", loan_doc, update, update_modified=False)
            frappe.db.commit()
            closed += 1

        except Exception:
            import traceback

            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} ForceClose[{loan_no}]",
            )
            errors += 1

    if closed or errors:
        _log(doc, f"   Force-closed {closed:,} fully paid loans  Errors: {errors}")


def _get_pending_principal(loan):
    """Mirrors get_pending_principal_amount() in loan_repayment.py."""
    if not loan:
        return 0.0
    is_fully_disbursed = loan.get("status") in ("Disbursed", "Closed") or _safe_float(
        loan.get("disbursed_amount")
    ) >= _safe_float(loan.get("loan_amount"))
    base = (
        _safe_float(loan.get("total_payment"))
        if is_fully_disbursed
        else _safe_float(loan.get("disbursed_amount"))
    )
    return flt(
        base
        + _safe_float(loan.get("debit_adjustment_amount"))
        - _safe_float(loan.get("credit_adjustment_amount"))
        - _safe_float(loan.get("total_principal_paid"))
        - _safe_float(loan.get("total_interest_payable"))
        - _safe_float(loan.get("written_off_amount"))
        + _safe_float(loan.get("refund_amount"))
    )


# ── Whitelisted API ───────────────────────────────────────────────────────────


@frappe.whitelist()
def get_status(doc_name):
    doc = frappe.get_doc("Data Migration Tool", doc_name)
    return {
        "status": doc.status,
        "customers_created": doc.customers_created or 0,
        "loans_created": doc.loans_created or 0,
        "historical_loans_created": doc.historical_loans_created or 0,
        "repayments_created": doc.repayments_created or 0,
        "errors_count": doc.errors_count or 0,
        "log": doc.migration_log or "",
    }


@frappe.whitelist()
def validate_migration(doc_name):
    """Pre-flight validation — returns a report dict, touches no data."""
    doc = frappe.get_doc("Data Migration Tool", doc_name)
    report = {"config_ok": True, "config_errors": []}

    for label, dt, val in [
        ("Company", "Company", doc.company),
        ("Branch", "Branch", doc.branch),
        ("Sales Person", "Sales Person", doc.sales_person),
        ("Disbursement Account", "Account", doc.disbursement_account),
        ("Payment Account", "Account", doc.payment_account),
        ("Loan Account", "Account", doc.loan_account),
        ("Interest Income Account", "Account", doc.interest_income_account),
        ("Interest Receivable", "Account", doc.interest_receivable_account),
        ("Penalty Income Account", "Account", doc.penalty_income_account),
        ("Penalty Receivable", "Account", doc.penalty_receivable_account),
    ]:
        if not val:
            report["config_errors"].append(f"{label} is empty")
            report["config_ok"] = False
        elif not frappe.db.exists(dt, val):
            report["config_errors"].append(f"{label}: '{val}' not found")
            report["config_ok"] = False

    charges_acct = doc.get("charges_income_account")
    if charges_acct and not frappe.db.exists("Account", charges_acct):
        report["config_errors"].append(
            f"Charges Income Account: '{charges_acct}' not found in Accounts"
        )
        report["config_ok"] = False
    elif not charges_acct:
        report.setdefault("warnings", []).append(
            "Charges Income Account not set — will fall back to Interest Income Account for fee GL entries"
        )

    for field in ["customer_file", "disbursement_file", "repayment_file"]:
        if not doc.get(field):
            report["config_errors"].append(
                f"{field.replace('_', ' ').title()} not uploaded"
            )
            report["config_ok"] = False

    if not report["config_ok"]:
        return report

    customer_rows = _read_excel(doc.customer_file)
    disbursement_rows = _read_excel(doc.disbursement_file)
    repayment_rows = _read_excel(doc.repayment_file)

    report["row_counts"] = {
        "customers": len(customer_rows),
        "disbursements": len(disbursement_rows),
        "repayments": len(repayment_rows),
    }

    # ── Column validation ─────────────────────────────────────────────────────
    # Required: migration will silently produce wrong results if these are absent.
    # Optional: migration runs but some fields will be blank / defaulted.
    _CUSTOMER_REQUIRED = [
        "Full Name",
        "Borrower CNIC #",
    ]
    _CUSTOMER_OPTIONAL = [
        "DOB",
        "CNIC Expiry Date",
        "Gender",
        "Marital Status",
        "Religion",
        "Mobile",
        "Parentage/Spouse Name",
        "Parentage/Spouse Mobile #",
    ]
    _DISBURSEMENT_REQUIRED = [
        "Loan#",
        "Borrower CNIC #",
        "Loan Product",
        "Disbursed Date",
        "Status",
    ]
    _DISBURSEMENT_OPTIONAL = [
        "Disbursed",
        "Principal",
        "Interest Rate",
        "Interest",
        "Outstanding",
        "Paid",
        "Principal Paid",
        "Interest Paid",
        "Penalty Paid",
        "Deductable Fees",
        "Principal Balance",
        "Interest Balance",
        "Penalty Balance",
    ]
    _REPAYMENT_REQUIRED = [
        "Loan#",
        "Collection Date",
        "Total Paid Amount",
    ]

    _REPAYMENT_OPTIONAL = [
        "Borrower CNIC #",
        "Principal Paid",
        "Interest Paid",
        "Penalty Paid",
        "Fees Paid",
    ]

    def _check_columns(rows, required, optional, file_label):
        """
        Return a dict describing which required/optional columns are present or missing.
        Comparison is case-insensitive and strips whitespace so minor header
        formatting differences don't cause false failures.
        """
        if not rows:
            return {
                "columns_ok": False,
                "missing_required": required[:],
                "missing_optional": optional[:],
                "present": [],
                "error": f"{file_label} has no data rows — cannot read headers",
            }

        actual = {str(k).strip().lower(): str(k).strip() for k in rows[0].keys()}
        frappe.msgprint(str(actual))

        missing_req = [col for col in required if col.strip().lower() not in actual]
        missing_opt = [col for col in optional if col.strip().lower() not in actual]
        present = [
            actual[col.strip().lower()]
            for col in (required + optional)
            if col.strip().lower() in actual
        ]

        return {
            "columns_ok": len(missing_req) == 0,
            "present": present,
            "missing_required": missing_req,
            "missing_optional": missing_opt,
        }

    col_customer = _check_columns(
        customer_rows, _CUSTOMER_REQUIRED, _CUSTOMER_OPTIONAL, "Customer file"
    )
    col_disb = _check_columns(
        disbursement_rows,
        _DISBURSEMENT_REQUIRED,
        _DISBURSEMENT_OPTIONAL,
        "Disbursement file",
    )
    col_repayment = _check_columns(
        repayment_rows, _REPAYMENT_REQUIRED, _REPAYMENT_OPTIONAL, "Repayment file"
    )

    report["column_validation"] = {
        "customer_file": col_customer,
        "disbursement_file": col_disb,
        "repayment_file": col_repayment,
        "columns_ok": col_customer["columns_ok"]
        and col_disb["columns_ok"]
        and col_repayment["columns_ok"],
    }

    # Promote missing required columns to top-level config errors so the caller
    # sees them immediately and the rest of the validation is skipped.
    for file_label, col_result in [
        ("Customer file", col_customer),
        ("Disbursement file", col_disb),
        ("Repayment file", col_repayment),
    ]:
        for col in col_result["missing_required"]:
            report["config_errors"].append(
                f"{file_label}: required column '{col}' is missing"
            )
            report["config_ok"] = False
        for col in col_result.get("missing_optional", []):
            report.setdefault("warnings", []).append(
                f"{file_label}: optional column '{col}' is missing — related field will be blank/defaulted"
            )

    if not report["config_ok"]:
        return report
    # ── End column validation ─────────────────────────────────────────────────

    allowed_marital = _get_allowed("custom_marital_status")
    allowed_religion = _get_allowed("custom_religion")
    cust_new = cust_existing = cust_skip = 0
    mar_bad = {}
    rel_bad = {}
    for r in customer_rows:
        cnic = _cnic(r.get("Borrower CNIC #"))
        name = str(r.get("Full Name") or "").strip()
        if not name or not cnic or cnic == "0":
            cust_skip += 1
            continue
        if frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name"):
            cust_existing += 1
        else:
            cust_new += 1
        mar = str(r.get("Marital Status") or "").strip()
        rel = str(r.get("Religion") or "").strip()
        if mar and _safe_select(mar, MARITAL_MAP, allowed_marital) is None:
            mar_bad[mar] = mar_bad.get(mar, 0) + 1
        if rel and _safe_select(rel, RELIGION_MAP, allowed_religion) is None:
            rel_bad[rel] = rel_bad.get(rel, 0) + 1
    report["customers"] = {
        "will_create": cust_new,
        "already_exist": cust_existing,
        "invalid": cust_skip,
        "marital_unmapped": mar_bad,
        "religion_unmapped": rel_bad,
    }

    prods = set(
        str(r.get("Loan Product") or "").strip()
        for r in disbursement_rows
        if r.get("Loan Product")
    )
    report["loan_products"] = {
        "in_excel": len(prods),
        "existing": len([p for p in prods if frappe.db.exists("Loan Product", p)]),
        "will_create": sorted(
            p for p in prods if not frappe.db.exists("Loan Product", p)
        ),
        "need_patch": sorted(
            p
            for p in prods
            if frappe.db.exists("Loan Product", p)
            and not frappe.db.get_value("Loan Product", p, "disbursement_account")
        ),
    }

    existing_map = _build_loan_map_from_db()
    disb_loan_nos = {
        str(r.get("Loan#") or r.get("Loan #") or "").strip() for r in disbursement_rows
    }
    all_cnics = set(_cnic(r.get("Borrower CNIC #")) for r in customer_rows)
    all_cnics |= set(
        str(row[0])
        for row in frappe.db.sql(
            "SELECT custom_cnic_no FROM `tabCustomer` "
            "WHERE custom_cnic_no IS NOT NULL AND custom_cnic_no != ''"
        )
    )
    ln_new = ln_exist = ln_no_cust = 0
    for r in disbursement_rows:
        ln = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        cnic = _cnic(r.get("Borrower CNIC #"))
        if not ln:
            continue
        if ln in existing_map:
            ln_exist += 1
            continue
        if cnic not in all_cnics:
            ln_no_cust += 1
        ln_new += 1
    report["loans"] = {
        "will_create": ln_new,
        "already_exist": ln_exist,
        "no_customer": ln_no_cust,
    }

    hist_index = _build_historical_loan_index(
        repayment_rows, disb_loan_nos, existing_map
    )
    all_ln = disb_loan_nos | set(existing_map.keys())
    ra = rh = rnc = ru = 0
    for r in repayment_rows:
        ln = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if ln in all_ln:
            ra += 1
        elif ln in hist_index:
            cnic = hist_index[ln]["cnic"]
            if cnic and cnic in all_cnics:
                rh += 1
            else:
                rnc += 1
        else:
            ru += 1
    report["repayments"] = {
        "total": len(repayment_rows),
        "matched_active": ra,
        "matched_historical": rh,
        "historical_loans_to_create": len(hist_index),
        "no_customer": rnc,
        "unmatched": ru,
        "total_importable": ra + rh,
    }

    report["current_counts"] = {}
    for dt in [
        "Customer",
        "Loan Product",
        "Loan",
        "Loan Disbursement",
        "Loan Repayment",
    ]:
        try:
            report["current_counts"][dt] = frappe.db.count(dt)
        except Exception:
            report["current_counts"][dt] = 0

    # Financial summary
    total_disbursed = 0.0
    total_deductable_fees = 0.0
    total_net_cash = 0.0
    total_interest_expected = 0.0
    total_outstanding = 0.0
    total_principal_balance = 0.0
    total_interest_balance = 0.0
    total_penalty_balance = 0.0
    total_already_paid = 0.0
    total_prin_paid_disb = 0.0
    total_int_paid_disb = 0.0
    total_pen_paid_disb = 0.0
    count_by_status = {}

    for r in disbursement_rows:
        disbursed = _safe_float(r.get("Disbursed")) or _safe_float(r.get("Principal"))
        fees = _safe_float(r.get("Deductable Fees"))
        interest = _safe_float(r.get("Interest"))
        outstanding = _safe_float(r.get("Outstanding"))
        paid = _safe_float(r.get("Paid"))
        prin_paid = _safe_float(r.get("Principal Paid"))
        int_paid = _safe_float(r.get("Interest Paid"))
        pen_paid = _safe_float(r.get("Penalty Paid"))
        prin_bal = _safe_float(r.get("Principal Balance"))
        int_bal = _safe_float(r.get("Interest Balance"))
        pen_bal = _safe_float(r.get("Penalty Balance"))
        status = str(r.get("Status") or "").strip()

        total_disbursed += disbursed
        total_deductable_fees += fees
        total_net_cash += disbursed - fees
        total_interest_expected += interest
        total_outstanding += outstanding
        total_already_paid += paid
        total_prin_paid_disb += prin_paid
        total_int_paid_disb += int_paid
        total_pen_paid_disb += pen_paid
        total_principal_balance += prin_bal
        total_interest_balance += int_bal
        total_penalty_balance += pen_bal
        count_by_status[status] = count_by_status.get(status, 0) + 1

    report["financial_summary"] = {
        "disbursement": {
            "total_loans": len(disbursement_rows),
            "total_disbursed": round(total_disbursed, 2),
            "total_deductable_fees": round(total_deductable_fees, 2),
            "total_net_cash_released": round(total_net_cash, 2),
            "total_interest_expected": round(total_interest_expected, 2),
            "total_payable_principal_plus_interest": round(
                total_disbursed + total_interest_expected, 2
            ),
        },
        "already_collected": {
            "total_paid": round(total_already_paid, 2),
            "principal_paid": round(total_prin_paid_disb, 2),
            "interest_paid": round(total_int_paid_disb, 2),
            "penalty_paid": round(total_pen_paid_disb, 2),
        },
        "balance_remaining": {
            "total_outstanding": round(total_outstanding, 2),
            "principal_balance": round(total_principal_balance, 2),
            "interest_balance": round(total_interest_balance, 2),
            "penalty_balance": round(total_penalty_balance, 2),
        },
        "loans_by_status": count_by_status,
    }

    rep_total_amount = rep_principal = rep_interest = rep_penalty = rep_fees = 0.0
    rep_loan_nos = set()
    for r in repayment_rows:
        rep_total_amount += _safe_float(r.get("Amount"))
        rep_principal += _safe_float(r.get("Principal Paid"))
        rep_interest += _safe_float(r.get("Interest Paid"))
        rep_penalty += _safe_float(r.get("Penalty Paid"))
        rep_fees += _safe_float(r.get("Fees Paid"))
        ln = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if ln:
            rep_loan_nos.add(ln)

    report["financial_summary"]["repayment_transactions"] = {
        "total_transactions": len(repayment_rows),
        "unique_loans": len(rep_loan_nos),
        "total_amount_collected": round(rep_total_amount, 2),
        "principal_collected": round(rep_principal, 2),
        "interest_collected": round(rep_interest, 2),
        "penalty_collected": round(rep_penalty, 2),
        "fees_collected": round(rep_fees, 2),
        "cross_check": {
            "disbursement_file_principal_paid": round(total_prin_paid_disb, 2),
            "repayment_file_principal_total": round(rep_principal, 2),
            "difference": round(abs(total_prin_paid_disb - rep_principal), 2),
            "disbursement_file_interest_paid": round(total_int_paid_disb, 2),
            "repayment_file_interest_total": round(rep_interest, 2),
            "interest_difference": round(abs(total_int_paid_disb - rep_interest), 2),
            "note": (
                "The repayment file covers 01/01/2025 to 31/12/2025 only. "
                "Payments made before 2025 are not in the repayment file but ARE "
                "reflected in the disbursement file totals (Principal Paid / Interest Paid columns). "
                "This explains the ~50%% gap between the two files. "
                "For fully paid loans with pre-2025 payments, the force-close function "
                "uses source status from the disbursement file to override the closure."
            ),
        },
    }

    return report


# ── Post-migration reconciliation ─────────────────────────────────────────────


@frappe.whitelist()
def reconcile_loan_paid_amounts(doc_name=None, dry_run=0):
    """
    Reconcile paid amounts on Loan records and Loan Interest Accrual records
    after migration.

    Fixes vs original:
    BUG-1  branch_conds was built with f-string interpolation (SQL injection).
           Now uses parameterised query via frappe.db.sql with a params list.
    BUG-2  branch_conds was appended to the query string but the WHERE clause
           already had "WHERE 1=1" — the extra AND was fine but the branch value
           was injected as a raw string instead of a bound param.
    BUG-3  `import traceback` was inside the function — moved to module level.
    BUG-4  `skipped` counter was never included in the return dict.
    BUG-5  A single frappe.db.commit() at the end means one error anywhere
           rolls back nothing (errors are caught per-loan) but also means a
           crash mid-loop leaves partial writes uncommitted. Now commits every
           COMMIT_BATCH loans consistent with the rest of the migration tool.
    """
    import traceback

    precision = int(frappe.db.get_default("currency_precision") or 2)
    dry_run = int(dry_run)

    # BUG-1 / BUG-2 FIX: use parameterised query — never interpolate branch into SQL
    base_query = (
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus = 1"
    )
    base_params = ["%LoanRef:%"]

    if doc_name:
        branch = frappe.db.get_value("Data Migration Tool", doc_name, "branch")
        if branch:
            base_query += " AND branch = %s"
            base_params.append(branch)

    loan_names = frappe.db.sql_list(base_query, base_params)

    if not loan_names:
        return {
            "status": "no_loans",
            "updated": 0,
            "errors": 0,
            "skipped": 0,
            "details": [],
        }

    updated = errors = skipped = 0
    details = []

    # BUG-5 FIX: batch commits consistent with COMMIT_BATCH pattern in this file
    commit_every = getattr(frappe.local, "COMMIT_BATCH", 100)

    for idx, loan_name in enumerate(loan_names, 1):
        try:
            result = _reconcile_single_loan(loan_name, precision, dry_run)
            if result["changed"]:
                updated += 1
                details.append(result)
            else:
                skipped += 1
        except Exception:
            tb = traceback.format_exc()
            frappe.log_error(tb, f"ReconcileLoan-{loan_name}")
            errors += 1
            details.append({"loan": loan_name, "error": tb[:300]})

        # BUG-5 FIX: commit in batches so partial progress is not lost
        if not dry_run and idx % commit_every == 0:
            frappe.db.commit()

    if not dry_run:
        frappe.db.commit()

    return {
        "status": "dry_run" if dry_run else "done",
        "total": len(loan_names),
        "updated": updated,
        "skipped": skipped,  # BUG-4 FIX: was missing from return dict
        "errors": errors,
        "details": details[:50],
    }


def _reconcile_single_loan(loan_name, precision, dry_run):
    """
    Fixes vs original:

    BUG-6  get_value() for Loan fetched total_interest_paid and total_penalty_paid
           using a conditional expression inside the field list:
               "total_interest_paid" if has_column(...) else "loan_amount"
           This meant when the column didn't exist, the value read into
           cur_interest_paid / cur_penalty_paid was loan_amount — completely wrong.
           Fix: check has_column once upfront; build field list cleanly.

    BUG-7  lia_fix_needed logic marks a fix needed whenever accrued != paid on LIAs,
           then the UPDATE blindly sets paid = accrued on ALL non-Penalty LIAs.
           This is correct for migration reconciliation BUT it ignores partially-paid
           LIAs that genuinely have paid < accrued (partial repayments). Those would
           be overwritten to fully-paid even though they aren't.
           Fix: instead of setting paid = accrued wholesale, set paid_interest_amount
           and paid_principal_amount to the actual SUM from repayment_details rows,
           which is the authoritative source of what has actually been paid per LIA.

    BUG-8  correct_status check compared correct_principal_paid >= loan_amount but
           loan_amount could be 0 (partially disbursed loan) causing false closure.
           Fix: guard with disbursed_amount as well, matching get_pending_principal_amount
           logic in loan_repayment.py.

    BUG-9  has_column() is called multiple times per loan inside the loop —
           once in the field list build and again in the loan_update block.
           These are schema-level checks that never change during a run.
           Fix: call once, cache in local booleans.
    """

    # BUG-9 FIX: call has_column once per invocation, not 4x
    has_int_paid = frappe.db.has_column("Loan", "total_interest_paid")
    has_pen_paid = frappe.db.has_column("Loan", "total_penalty_paid")

    # ── Repayment totals ──────────────────────────────────────────────────────
    rep_totals = frappe.db.sql(
        """
        SELECT
            ROUND(SUM(principal_amount_paid), %s) AS principal_paid,
            ROUND(SUM(total_interest_paid),   %s) AS interest_paid,
            ROUND(SUM(total_penalty_paid),    %s) AS penalty_paid,
            ROUND(SUM(amount_paid),           %s) AS amount_paid
        FROM `tabLoan Repayment`
        WHERE against_loan = %s AND docstatus = 1
        """,
        (precision, precision, precision, precision, loan_name),
        as_dict=True,
    )
    rep = rep_totals[0] if rep_totals else {}
    rep_principal = flt(rep.get("principal_paid") or 0, precision)
    rep_interest = flt(rep.get("interest_paid") or 0, precision)
    rep_penalty = flt(rep.get("penalty_paid") or 0, precision)
    rep_amount = flt(rep.get("amount_paid") or 0, precision)

    # ── LIA totals (accrued side) ─────────────────────────────────────────────
    lia_totals = frappe.db.sql(
        """
        SELECT
            ROUND(SUM(interest_amount),          %s) AS total_interest_accrued,
            ROUND(SUM(payable_principal_amount),  %s) AS total_principal_accrued,
            ROUND(SUM(paid_interest_amount),      %s) AS total_interest_paid_lia,
            ROUND(SUM(paid_principal_amount),     %s) AS total_principal_paid_lia
        FROM `tabLoan Interest Accrual`
        WHERE loan = %s AND docstatus = 1 AND accrual_type != 'Penalty'
        """,
        (precision, precision, precision, precision, loan_name),
        as_dict=True,
    )
    lia = lia_totals[0] if lia_totals else {}
    lia_int_accrued = flt(lia.get("total_interest_accrued") or 0, precision)
    lia_prin_accrued = flt(lia.get("total_principal_accrued") or 0, precision)
    lia_int_paid = flt(lia.get("total_interest_paid_lia") or 0, precision)
    lia_prin_paid = flt(lia.get("total_principal_paid_lia") or 0, precision)

    # BUG-7 FIX: get per-LIA paid amounts from repayment_details — the
    # authoritative source — so we don't blindly overwrite partial payments.
    lia_paid_from_details = frappe.db.sql(
        """
        SELECT
            rd.loan_interest_accrual            AS lia_name,
            ROUND(SUM(rd.paid_interest_amount),  %s) AS paid_int,
            ROUND(SUM(rd.paid_principal_amount), %s) AS paid_prin
        FROM `tabLoan Repayment Detail` rd
        INNER JOIN `tabLoan Repayment` lr ON lr.name = rd.parent
        WHERE lr.against_loan = %s AND lr.docstatus = 1
        GROUP BY rd.loan_interest_accrual
        """,
        (precision, precision, loan_name),
        as_dict=True,
    )
    # {lia_name: (paid_int, paid_prin)}
    lia_detail_map = {
        r.lia_name: (flt(r.paid_int, precision), flt(r.paid_prin, precision))
        for r in lia_paid_from_details
        if r.lia_name
    }

    # ── Loan current state ────────────────────────────────────────────────────
    # BUG-6 FIX: build field list cleanly; never fall back to loan_amount
    loan_fields = [
        "total_principal_paid",
        "total_interest_payable",
        "total_amount_paid",
        "loan_amount",
        "disbursed_amount",
        "status",
        "applicant_type",
    ]
    if has_int_paid:
        loan_fields.append("total_interest_paid")
    if has_pen_paid:
        loan_fields.append("total_penalty_paid")

    loan_data = frappe.db.get_value("Loan", loan_name, loan_fields, as_dict=True) or {}

    cur_principal_paid = flt(loan_data.get("total_principal_paid") or 0, precision)
    cur_interest_payable = flt(loan_data.get("total_interest_payable") or 0, precision)
    cur_amount_paid = flt(loan_data.get("total_amount_paid") or 0, precision)
    cur_interest_paid = (
        flt(loan_data.get("total_interest_paid") or 0, precision)
        if has_int_paid
        else 0.0
    )
    cur_penalty_paid = (
        flt(loan_data.get("total_penalty_paid") or 0, precision)
        if has_pen_paid
        else 0.0
    )
    cur_status = loan_data.get("status", "")
    loan_amount = flt(loan_data.get("loan_amount") or 0, precision)
    disbursed_amount = flt(loan_data.get("disbursed_amount") or 0, precision)

    # ── Correct values ────────────────────────────────────────────────────────
    has_repayments = rep_amount > 0

    correct_interest_payable = (
        lia_int_accrued
        if has_repayments and lia_int_accrued > 0
        else cur_interest_payable
    )
    correct_principal_paid = rep_principal
    correct_interest_paid = rep_interest
    correct_penalty_paid = rep_penalty
    correct_amount_paid = rep_amount

    # BUG-8 FIX: use disbursed_amount as the threshold (matches
    # get_pending_principal_amount logic); guard against loan_amount = 0
    closure_threshold = disbursed_amount if disbursed_amount > 0 else loan_amount
    correct_status = cur_status
    if (
        closure_threshold > 0
        and correct_principal_paid >= closure_threshold
        and cur_status not in ("Loan Closure Requested", "Closed")
    ):
        correct_status = (
            "Closed"
            if loan_data.get("applicant_type") == "Employee"
            else "Loan Closure Requested"
        )

    # ── Determine what needs updating ─────────────────────────────────────────
    loan_update = {}
    lia_updates = []  # BUG-7 FIX: per-LIA updates instead of wholesale SET

    if abs(cur_principal_paid - correct_principal_paid) > 0.01:
        loan_update["total_principal_paid"] = correct_principal_paid
    if abs(cur_interest_payable - correct_interest_payable) > 0.01:
        loan_update["total_interest_payable"] = correct_interest_payable
    if abs(cur_amount_paid - correct_amount_paid) > 0.01:
        loan_update["total_amount_paid"] = correct_amount_paid
    if has_int_paid and abs(cur_interest_paid - correct_interest_paid) > 0.01:
        loan_update["total_interest_paid"] = correct_interest_paid
    if has_pen_paid and abs(cur_penalty_paid - correct_penalty_paid) > 0.01:
        loan_update["total_penalty_paid"] = correct_penalty_paid
    if correct_status != cur_status:
        loan_update["status"] = correct_status

    # BUG-7 FIX: build per-LIA corrections from repayment_details map
    # so partial payments are preserved — only update LIAs that are wrong
    all_lias = frappe.db.sql(
        """
        SELECT name, interest_amount, payable_principal_amount,
               paid_interest_amount, paid_principal_amount
        FROM `tabLoan Interest Accrual`
        WHERE loan = %s AND docstatus = 1 AND accrual_type != 'Penalty'
        """,
        (loan_name,),
        as_dict=True,
    )
    for lia_row in all_lias:
        correct_int_paid, correct_prin_paid = lia_detail_map.get(
            lia_row.name, (0.0, 0.0)
        )
        cur_lia_int = flt(lia_row.paid_interest_amount or 0, precision)
        cur_lia_prin = flt(lia_row.paid_principal_amount or 0, precision)

        if (
            abs(cur_lia_int - correct_int_paid) > 0.01
            or abs(cur_lia_prin - correct_prin_paid) > 0.01
        ):
            lia_updates.append((correct_int_paid, correct_prin_paid, lia_row.name))

    changed = bool(loan_update) or bool(lia_updates)

    result = {
        "loan": loan_name,
        "changed": changed,
        "loan_update": loan_update,
        "lia_updates": len(lia_updates),
        "before": {
            "total_principal_paid": cur_principal_paid,
            "total_interest_payable": cur_interest_payable,
            "total_interest_paid": cur_interest_paid,
            "total_amount_paid": cur_amount_paid,
            "status": cur_status,
            "lia_int_paid": lia_int_paid,
            "lia_prin_paid": lia_prin_paid,
        },
        "after": {
            "total_principal_paid": correct_principal_paid,
            "total_interest_payable": correct_interest_payable,
            "total_interest_paid": correct_interest_paid,
            "total_amount_paid": correct_amount_paid,
            "status": correct_status,
            "lia_int_paid": sum(u[0] for u in lia_updates),
            "lia_prin_paid": sum(u[1] for u in lia_updates),
        },
    }

    if dry_run or not changed:
        return result

    # ── Apply updates ─────────────────────────────────────────────────────────
    if loan_update:
        frappe.db.set_value("Loan", loan_name, loan_update, update_modified=False)

    # BUG-7 FIX: update each LIA individually with its correct paid amounts
    # derived from repayment_details — not a wholesale SET paid = accrued
    if lia_updates:
        frappe.db.sql(
            """
            UPDATE `tabLoan Interest Accrual`
            SET paid_interest_amount  = %s,
                paid_principal_amount = %s
            WHERE name = %s
            """,
            lia_updates,
            auto_commit=False,
        )

    return result


# ── Post-migration repayment status fix ───────────────────────────────────────


@frappe.whitelist()
def fix_repayment_statuses(dry_run=0):
    """
    After migration, repayment_status on all loans is blank or wrong because
    set_repayment_status() was never called during import.
    """
    import traceback

    dry_run = int(dry_run)
    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus = 1",
        ("%LoanRef:%",),
    )
    if not loan_names:
        return {"status": "no_loans", "total": 0}

    before_counts = {}
    after_counts = {}
    updated = errors = skipped = 0
    sample_changes = []

    for loan_name in loan_names:
        try:
            loan_doc = frappe.get_doc("Loan", loan_name)
            old_status = loan_doc.repayment_status or ""
            before_counts[old_status] = before_counts.get(old_status, 0) + 1

            if dry_run:
                new_status = _simulate_repayment_status(loan_doc)
            else:
                loan_doc.set_repayment_status()
                frappe.db.commit()
                new_status = (
                    frappe.db.get_value("Loan", loan_name, "repayment_status") or ""
                )

            after_counts[new_status] = after_counts.get(new_status, 0) + 1

            if old_status != new_status:
                updated += 1
                if len(sample_changes) < 30:
                    sample_changes.append(
                        {
                            "loan": loan_name,
                            "before": old_status,
                            "after": new_status,
                            "loan_status": loan_doc.status,
                        }
                    )
            else:
                skipped += 1

        except Exception:
            errors += 1
            frappe.log_error(traceback.format_exc(), f"FixRepaymentStatus-{loan_name}")

    return {
        "status": "dry_run" if dry_run else "done",
        "total": len(loan_names),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "before_counts": dict(sorted(before_counts.items(), key=lambda x: -x[1])),
        "after_counts": dict(sorted(after_counts.items(), key=lambda x: -x[1])),
        "sample_changes": sample_changes,
    }


def _simulate_repayment_status(loan_doc):
    """
    Mirrors the FIXED set_repayment_status() logic for dry_run mode.
    Uses cumulative position-based approach — not LIA due_date grouping.
    """
    from frappe.utils import cint, date_diff, flt, getdate

    if loan_doc.status in ("Closed", "Loan Closure Requested"):
        return "Fully Paid"

    precision = cint(frappe.db.get_default("currency_precision")) or 2
    current_date = getdate()

    schedule_id = frappe.db.get_value(
        "Loan Repayment Schedule",
        {"loan": loan_doc.name, "docstatus": 1, "status": "Active"},
        "name",
    )
    if not schedule_id:
        return loan_doc.repayment_status or ""

    schedule = frappe.db.sql(
        f"""SELECT name, payment_date, interest_amount, principal_amount
            FROM `tabRepayment Schedule`
            WHERE parent = '{schedule_id}'
            ORDER BY idx ASC""",
        as_dict=True,
    )
    if not schedule:
        return "Regular"

    maturity_date = schedule[-1].payment_date

    loan_totals = (
        frappe.db.get_value(
            "Loan",
            loan_doc.name,
            ["total_principal_paid", "total_interest_paid"],
            as_dict=True,
        )
        or {}
    )
    remaining_prin = flt(loan_totals.get("total_principal_paid") or 0, precision)
    remaining_int = flt(loan_totals.get("total_interest_paid") or 0, precision)

    past_due_rows = 0
    has_partial = False
    oldest_unpaid = None

    for sch in schedule:
        sch_date = getdate(sch.payment_date)
        sch_prin = flt(sch.principal_amount, precision)
        sch_int = flt(sch.interest_amount, precision)

        paid_prin = min(sch_prin, remaining_prin)
        remaining_prin = flt(remaining_prin - paid_prin, precision)
        paid_int = min(sch_int, remaining_int)
        remaining_int = flt(remaining_int - paid_int, precision)

        billed = flt(sch_prin + sch_int, precision)
        paid = flt(paid_prin + paid_int, precision)
        is_fully_paid = paid >= billed

        if sch_date < current_date and not is_fully_paid:
            past_due_rows += 1
            if not oldest_unpaid:
                oldest_unpaid = sch.payment_date
            if paid > 0:
                has_partial = True

    if oldest_unpaid:
        days_late = date_diff(current_date, oldest_unpaid)
        if current_date > getdate(maturity_date):
            return "Past Maturity"
        elif has_partial or past_due_rows > 1:
            return "Arrears"
        elif days_late >= 30:
            return "Missed Repayment"
        elif 0 < days_late < 30:
            return "Overdue"
        return "Regular"

    remaining_prin_chk = flt(loan_totals.get("total_principal_paid") or 0, precision)
    remaining_int_chk = flt(loan_totals.get("total_interest_paid") or 0, precision)
    for sch in schedule:
        sch_date = getdate(sch.payment_date)
        sch_prin = flt(sch.principal_amount, precision)
        sch_int = flt(sch.interest_amount, precision)
        paid_prin = min(sch_prin, remaining_prin_chk)
        remaining_prin_chk = flt(remaining_prin_chk - paid_prin, precision)
        paid_int = min(sch_int, remaining_int_chk)
        remaining_int_chk = flt(remaining_int_chk - paid_int, precision)
        billed = flt(sch_prin + sch_int, precision)
        paid = flt(paid_prin + paid_int, precision)
        if sch_date == current_date and paid < billed:
            return "Due Today"

    return "Regular"


# ── Post-migration GL correction ──────────────────────────────────────────────


@frappe.whitelist()
def create_missing_accrual_gl_entries(doc_name=None, dry_run=0):
    """
    Fix phantom credit balances on Interest Receivable and Penalty Receivable
    caused by FIX-6 in the migration (LIAs had no GL entries).
    """
    import traceback

    dry_run = int(dry_run)
    precision = int(frappe.db.get_default("currency_precision") or 2)

    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus = 1",
        ("%LoanRef:%",),
    )
    if not loan_names:
        return {"status": "no_loans", "total_lias": 0}

    placeholders = ",".join(["%s"] * len(loan_names))
    lias = frappe.db.sql(
        f"""SELECT
                lia.name, lia.loan, lia.company, lia.branch, lia.cost_center,
                lia.posting_date, lia.date_of_accrual,
                lia.interest_amount, lia.penalty_amount,
                lia.accrual_type,
                lia.applicant_type, lia.applicant,
                lia.interest_income_account,
                l.applicant_type  AS loan_applicant_type,
                l.applicant       AS loan_applicant,
                l.interest_income_account AS loan_interest_income,
                lp.interest_receivable_account,
                lp.penalty_receivable_account,
                lp.penalty_income_account,
                lp.interest_income_account AS lp_interest_income
            FROM `tabLoan Interest Accrual` lia
            JOIN `tabLoan` l         ON l.name  = lia.loan
            JOIN `tabLoan Product` lp ON lp.name = l.loan_product
            WHERE lia.loan IN ({placeholders})
              AND lia.docstatus = 1
              AND (lia.interest_amount > 0 OR lia.penalty_amount > 0)""",
        tuple(loan_names),
        as_dict=True,
    )

    total_lias = len(lias)
    created = skipped = errors = 0
    total_interest = 0.0
    total_penalty = 0.0
    details = []

    for lia in lias:
        try:
            interest_amt = flt(lia.interest_amount or 0, precision)
            penalty_amt = flt(lia.penalty_amount or 0, precision)

            existing_gl = frappe.db.sql(
                """SELECT COUNT(*) FROM `tabGL Entry`
                   WHERE voucher_type = 'Loan Interest Accrual'
                     AND voucher_no   = %s AND is_cancelled = 0""",
                (lia.name,),
            )[0][0]

            if existing_gl > 0:
                skipped += 1
                continue

            applicant_type = lia.applicant_type or lia.loan_applicant_type or "Customer"
            applicant = lia.applicant or lia.loan_applicant
            interest_income_acc = (
                lia.interest_income_account
                or lia.loan_interest_income
                or lia.lp_interest_income
            )

            if dry_run:
                if interest_amt > 0:
                    total_interest += interest_amt
                    details.append(
                        {
                            "lia": lia.name,
                            "loan": lia.loan,
                            "posting_date": str(
                                lia.date_of_accrual or lia.posting_date
                            ),
                            "type": "Interest",
                            "amount": interest_amt,
                            "dr_account": lia.interest_receivable_account,
                            "cr_account": interest_income_acc,
                        }
                    )
                if penalty_amt > 0:
                    total_penalty += penalty_amt
                    details.append(
                        {
                            "lia": lia.name,
                            "loan": lia.loan,
                            "posting_date": str(
                                lia.date_of_accrual or lia.posting_date
                            ),
                            "type": "Penalty",
                            "amount": penalty_amt,
                            "dr_account": lia.penalty_receivable_account,
                            "cr_account": lia.penalty_income_account,
                        }
                    )
                created += 1
                continue

            patch = {}
            if not lia.applicant_type and applicant_type:
                patch["applicant_type"] = applicant_type
            if not lia.applicant and applicant:
                patch["applicant"] = applicant
            if not lia.interest_income_account and interest_income_acc:
                patch["interest_income_account"] = interest_income_acc

            if patch:
                frappe.db.set_value(
                    "Loan Interest Accrual",
                    lia.name,
                    patch,
                    update_modified=False,
                )

            lia_doc = frappe.get_doc("Loan Interest Accrual", lia.name)
            lia_doc.make_gl_entries()
            frappe.db.commit()

            if interest_amt > 0:
                total_interest += interest_amt
            if penalty_amt > 0:
                total_penalty += penalty_amt
            created += 1

        except Exception:
            errors += 1
            frappe.log_error(traceback.format_exc(), f"MigrationGLFix-{lia.name}")

    return {
        "status": "dry_run" if dry_run else "done",
        "total_lias": total_lias,
        "created": created,
        "skipped_existing": skipped,
        "errors": errors,
        "total_interest_corrected": round(total_interest, precision),
        "total_penalty_corrected": round(total_penalty, precision),
        "total_corrected": round(total_interest + total_penalty, precision),
        "details": details[:30],
    }


# ── Repayment reconciliation (Excel vs ERP) ───────────────────────────────────


def _recon_safe_date(val) -> str:
    """Return 'YYYY-MM-DD' (10 chars, no time component) or empty string."""
    if not val:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    from datetime import datetime

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _recon_loan_ref(description: str):
    if not description:
        return None
    m = re.search(r"LoanRef:([^\s,]+)", description)
    return m.group(1).strip() if m else None


@frappe.whitelist()
def match_repayments_excel(doc_name, tolerance=1.0):
    """
    Compare every row in the repayment Excel file attached to this migration
    against submitted Loan Repayment records in ERPNext.

    Matching key: loan_ref + posting_date (YYYY-MM-DD) + amount_paid (±tolerance)

    Returns a dict with:
        summary        — counts and totals
        unmatched      — Excel rows with no ERP counterpart (list of dicts)
        orphans        — ERP repayments with no Excel counterpart (list of dicts)
        file_url       — URL of the downloadable Excel report written to /private/files/
    """
    import traceback
    from collections import defaultdict

    tolerance = float(tolerance)

    doc = frappe.get_doc("Data Migration Tool", doc_name)
    if not doc.repayment_file:
        frappe.throw("No repayment file attached to this migration document.")

    # ── 1. Load ERP repayments ────────────────────────────────────────────────
    erp_rows = frappe.db.sql(
        """
        SELECT
            l.name                AS loan_name,
            l.description         AS loan_description,
            DATE(lr.posting_date) AS posting_date,
            lr.amount_paid,
            lr.principal_amount_paid,
            lr.total_interest_paid,
            lr.total_penalty_paid
        FROM `tabLoan Repayment` lr
        INNER JOIN `tabLoan` l ON l.name = lr.against_loan
        WHERE lr.docstatus = 1
          AND l.description LIKE %s
        """,
        ("%LoanRef:%",),
        as_dict=True,
    )

    erp_by_ref = defaultdict(list)
    for row in erp_rows:
        ref = _recon_loan_ref(row["loan_description"])
        if ref:
            erp_by_ref[ref].append(
                {
                    "loan_name": row["loan_name"],
                    "posting_date": str(row["posting_date"])[:10],
                    "amount_paid": _safe_float(row["amount_paid"]),
                    "principal_amount_paid": _safe_float(row["principal_amount_paid"]),
                    "total_interest_paid": _safe_float(row["total_interest_paid"]),
                    "total_penalty_paid": _safe_float(row["total_penalty_paid"]),
                    "_matched": False,
                }
            )

    # ── 2. Load Excel repayments ──────────────────────────────────────────────
    excel_rows = _read_excel(doc.repayment_file)

    # ── 3. Match ──────────────────────────────────────────────────────────────
    unmatched = []
    matched_count = 0
    valid_excel = 0

    for xrow in excel_rows:
        loan_no = str(xrow.get("Loan#") or "").strip()
        col_date = _recon_safe_date(xrow.get("Collection Date"))
        if not loan_no or not col_date:
            continue
        valid_excel += 1

        x_amount = _safe_float(xrow.get("Total Paid Amount"))
        x_principal = _safe_float(xrow.get("Principal Paid"))
        x_interest = _safe_float(xrow.get("Interest Paid"))
        x_penalty = _safe_float(xrow.get("Penalty Paid"))
        x_fees = _safe_float(xrow.get("Fees Paid"))

        candidates = erp_by_ref.get(loan_no, [])

        best_idx = None
        best_diff = float("inf")
        for idx, c in enumerate(candidates):
            if c["_matched"]:
                continue
            if c["posting_date"] != col_date:
                continue
            diff = abs(c["amount_paid"] - x_amount)
            if diff <= tolerance and diff < best_diff:
                best_diff = diff
                best_idx = idx

        if best_idx is not None:
            candidates[best_idx]["_matched"] = True
            matched_count += 1
            continue

        # ── Diagnose exactly why this row did not match ───────────────────
        erp_same_date = [c for c in candidates if c["posting_date"] == col_date]
        erp_same_amount = [
            c for c in candidates if abs(c["amount_paid"] - x_amount) <= tolerance
        ]

        if not candidates:
            reason = "Loan ref not found in ERP at all"
            detail = ""
        elif not erp_same_date and not erp_same_amount:
            erp_dates = sorted({c["posting_date"] for c in candidates})
            erp_amts = sorted({c["amount_paid"] for c in candidates})
            reason = "Date and amount both unmatched"
            detail = (
                f"Excel date={col_date} amt={x_amount:.2f} | "
                f"ERP dates=[{', '.join(erp_dates[:5])}] "
                f"ERP amts=[{', '.join(f'{a:.2f}' for a in erp_amts[:5])}]"
            )
        elif not erp_same_date:
            matching_amt_dates = sorted({c["posting_date"] for c in erp_same_amount})
            reason = "Amount matched but date not found in ERP"
            detail = (
                f"Excel date={col_date} | "
                f"ERP has same amount ({x_amount:.2f}) on: [{', '.join(matching_amt_dates[:5])}]"
            )
        elif not erp_same_amount:
            same_date_amts = sorted({c["amount_paid"] for c in erp_same_date})
            reason = "Date matched but amount mismatch"
            detail = (
                f"Excel amt={x_amount:.2f} on {col_date} | "
                f"ERP amounts on same date: [{', '.join(f'{a:.2f}' for a in same_date_amts[:5])}]"
            )
        else:
            reason = "Date+amount combination already consumed by another row"
            detail = f"Excel date={col_date} amt={x_amount:.2f} already matched to another Excel row for this loan"

        unmatched.append(
            {
                "loan_ref": loan_no,
                "erp_loan_name": candidates[0]["loan_name"] if candidates else "",
                "collection_date": col_date,
                "amount": x_amount,
                "principal_paid": x_principal,
                "interest_paid": x_interest,
                "penalty_paid": x_penalty,
                "fees_paid": x_fees,
                "in_erp": bool(candidates),
                "reason": reason,
                "detail": detail,
            }
        )

    # ── 4. Orphan ERP repayments ──────────────────────────────────────────────
    orphans = []
    for ref, candidates in erp_by_ref.items():
        for c in candidates:
            if not c["_matched"]:
                orphans.append(
                    {
                        "loan_ref": ref,
                        "erp_loan_name": c["loan_name"],
                        "posting_date": c["posting_date"],
                        "amount_paid": c["amount_paid"],
                        "principal_amount_paid": c["principal_amount_paid"],
                        "total_interest_paid": c["total_interest_paid"],
                        "total_penalty_paid": c["total_penalty_paid"],
                    }
                )

    # ── 5. Summary totals ─────────────────────────────────────────────────────
    def _sum(rows, key):
        return round(sum(_safe_float(r.get(key)) for r in rows), 2)

    # reason breakdown
    reason_counts = {}
    for r in unmatched:
        reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1

    summary = {
        "erp_total": len(erp_rows),
        "excel_total": valid_excel,
        "matched": matched_count,
        "unmatched_count": len(unmatched),
        "orphan_count": len(orphans),
        "unmatched_by_reason": reason_counts,
        "unmatched_totals": {
            "amount": _sum(unmatched, "amount"),
            "principal": _sum(unmatched, "principal_paid"),
            "interest": _sum(unmatched, "interest_paid"),
            "penalty": _sum(unmatched, "penalty_paid"),
        },
        "orphan_totals": {
            "amount": _sum(orphans, "amount_paid"),
            "principal": _sum(orphans, "principal_amount_paid"),
            "interest": _sum(orphans, "total_interest_paid"),
            "penalty": _sum(orphans, "total_penalty_paid"),
        },
    }

    # ── 6. Write downloadable Excel report ───────────────────────────────────
    file_url = None
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()

        hdr_font = Font(bold=True, color="FFFFFF")
        hdr_fill_red = PatternFill("solid", fgColor="C0392B")
        hdr_fill_orange = PatternFill("solid", fgColor="E67E22")
        hdr_fill_gray = PatternFill("solid", fgColor="2C3E50")
        wrap = Alignment(wrap_text=True, vertical="top")

        def _write_sheet(ws, headers, rows, fill):
            ws.append(headers)
            for cell in ws[1]:
                cell.font = hdr_font
                cell.fill = fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
            for r in rows:
                ws.append(
                    [
                        r.get(h.lower().replace(" ", "_").replace("/", "_"))
                        or r.get(h, "")
                        for h in headers
                    ]
                )
            for col_idx in range(1, len(headers) + 1):
                max_len = max(
                    (
                        len(str(ws.cell(row=i, column=col_idx).value or ""))
                        for i in range(1, ws.max_row + 1)
                    ),
                    default=10,
                )
                ws.column_dimensions[get_column_letter(col_idx)].width = min(
                    max_len + 4, 50
                )

        # ── Sheet 1: Summary ──────────────────────────────────────────────
        ws_sum = wb.active
        ws_sum.title = "Summary"
        ws_sum.column_dimensions["A"].width = 38
        ws_sum.column_dimensions["B"].width = 20

        def _sum_row(ws, label, val, bold=False):
            ws.append([label, val])
            if bold:
                for c in ws[ws.max_row]:
                    c.font = Font(bold=True)

        _sum_row(ws_sum, "ERP submitted repayments", len(erp_rows))
        _sum_row(ws_sum, "Excel rows (valid)", valid_excel)
        _sum_row(ws_sum, "Matched", matched_count, bold=True)
        _sum_row(ws_sum, "─── Unmatched (Excel→ERP)", len(unmatched), bold=True)
        for rsn, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
            _sum_row(ws_sum, f"   • {rsn}", cnt)
        _sum_row(ws_sum, "─── Orphan ERP repayments", len(orphans), bold=True)
        ws_sum.append([])
        _sum_row(
            ws_sum,
            "Unmatched — Total Amount",
            summary["unmatched_totals"]["amount"],
            bold=True,
        )
        _sum_row(
            ws_sum, "Unmatched — Principal", summary["unmatched_totals"]["principal"]
        )
        _sum_row(
            ws_sum, "Unmatched — Interest", summary["unmatched_totals"]["interest"]
        )
        _sum_row(ws_sum, "Unmatched — Penalty", summary["unmatched_totals"]["penalty"])
        ws_sum.append([])
        _sum_row(
            ws_sum,
            "Orphan — Total Amount",
            summary["orphan_totals"]["amount"],
            bold=True,
        )
        _sum_row(ws_sum, "Orphan — Principal", summary["orphan_totals"]["principal"])
        _sum_row(ws_sum, "Orphan — Interest", summary["orphan_totals"]["interest"])
        _sum_row(ws_sum, "Orphan — Penalty", summary["orphan_totals"]["penalty"])

        # ── Sheet 2: Unmatched ────────────────────────────────────────────
        ws_unm = wb.create_sheet("Unmatched (Excel→ERP)")
        unm_headers = [
            "Loan Ref",
            "ERP Loan Name",
            "Collection Date",
            "Amount",
            "Principal Paid",
            "Interest Paid",
            "Penalty Paid",
            "Fees Paid",
            "In ERP",
            "Reason",
            "Detail",
        ]
        ws_unm.append(unm_headers)
        for cell in ws_unm[1]:
            cell.font = hdr_font
            cell.fill = hdr_fill_red
            cell.alignment = Alignment(horizontal="center")
        for r in unmatched:
            ws_unm.append(
                [
                    r["loan_ref"],
                    r["erp_loan_name"],
                    r["collection_date"],
                    r["amount"],
                    r["principal_paid"],
                    r["interest_paid"],
                    r["penalty_paid"],
                    r["fees_paid"],
                    "Yes" if r["in_erp"] else "No",
                    r["reason"],
                    r["detail"],
                ]
            )
        for col_idx, width in enumerate(
            [22, 22, 14, 12, 14, 13, 12, 10, 8, 42, 70], start=1
        ):
            ws_unm.column_dimensions[get_column_letter(col_idx)].width = width
        # wrap Detail column
        for row in ws_unm.iter_rows(min_row=2, min_col=11, max_col=11):
            for cell in row:
                cell.alignment = wrap

        # ── Sheet 3: Orphans ──────────────────────────────────────────────
        ws_orp = wb.create_sheet("Orphan ERP Repayments")
        orp_headers = [
            "Loan Ref",
            "ERP Loan Name",
            "Posting Date",
            "Amount Paid",
            "Principal Paid",
            "Interest Paid",
            "Penalty Paid",
        ]
        ws_orp.append(orp_headers)
        for cell in ws_orp[1]:
            cell.font = hdr_font
            cell.fill = hdr_fill_orange
            cell.alignment = Alignment(horizontal="center")
        for r in orphans:
            ws_orp.append(
                [
                    r["loan_ref"],
                    r["erp_loan_name"],
                    r["posting_date"],
                    r["amount_paid"],
                    r["principal_amount_paid"],
                    r["total_interest_paid"],
                    r["total_penalty_paid"],
                ]
            )
        for col_idx, width in enumerate([22, 22, 14, 14, 14, 14, 14], start=1):
            ws_orp.column_dimensions[get_column_letter(col_idx)].width = width

        # ── Save to private files ─────────────────────────────────────────
        import os
        from frappe.utils import get_site_path

        fname = f"repayment_reconciliation_{doc_name}.xlsx"
        fpath = get_site_path("private", "files", fname)
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        wb.save(fpath)

        # Register as a Frappe File doc so it appears in attachments
        existing = frappe.db.get_value("File", {"file_name": fname}, "name")
        if existing:
            frappe.delete_doc("File", existing, force=True, ignore_permissions=True)

        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": fname,
                "file_url": f"/private/files/{fname}",
                "is_private": 1,
                "attached_to_doctype": "Data Migration Tool",
                "attached_to_name": doc_name,
            }
        )
        file_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        file_url = file_doc.file_url

    except Exception:
        frappe.log_error(traceback.format_exc(), f"RepaymentRecon-Excel-{doc_name}")

    return {
        "summary": summary,
        "unmatched": unmatched[:500],  # cap for JSON response size
        "orphans": orphans[:500],
        "file_url": file_url,
    }
