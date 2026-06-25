# Copyright (c) 2026, UnityAlgo
# For license information, please see license.txt


import re
from collections import defaultdict
import csv
import openpyxl
import traceback
import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, nowdate
import decimal

COMMIT_BATCH = 100
LOG_FLUSH = 100

BRANCH_CODE_MAP = {
    "70815": "Area Office",
    "70809": "Shahpur",
    "85559": "Jhanwarian",
    "70813": "Sargodha-3",
    "70812": "Sargodha-2",
    "70811": "Sargodha-1",
    "70807": "Talagang",
    "70814": "SAM",
    "76702": "Lalian",
    "82472": "Bhulwal",
    "69345": "Naushera - NSR",
    "70806": "Khushab - KHB",
    "78282": "Jauharabad - JBD-2",
    "70805": "Jauharabad - JBD-1",
    "70817": "Head Office - HO",
}

_branch_cache = {}


def _resolve_branch(raw_branch):
    if not raw_branch:
        return None

    key = str(raw_branch).strip()
    if not key or key == "0":
        return None

    if key in _branch_cache:
        return _branch_cache[key]

    code_str = key.split(".")[0] if "." in key else key
    if code_str in BRANCH_CODE_MAP:
        branch_name = BRANCH_CODE_MAP[code_str]
        if frappe.db.exists("Branch", branch_name):
            _branch_cache[key] = branch_name
            return branch_name

    if frappe.db.exists("Branch", key):
        _branch_cache[key] = key
        return key

    match = frappe.db.sql(
        "SELECT name FROM `tabBranch` WHERE LOWER(name) = LOWER(%s) LIMIT 1",
        (key,),
        as_list=True,
    )
    if match:
        _branch_cache[key] = match[0][0]
        return match[0][0]

    frappe.log_error(
        f"Branch not found in ERP: raw='{raw_branch}' code='{code_str}'",
        "DataMigration-BranchResolution",
    )
    _branch_cache[key] = None
    return None


def _build_branch_map_from_disbursements(disbursement_rows):
    branch_map = {}
    for r in disbursement_rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if not loan_no:
            continue
        raw_branch = r.get("Branch") or r.get("branch") or ""
        branch = _resolve_branch(str(raw_branch).strip())
        if branch:
            branch_map[loan_no] = branch
    return branch_map


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

        loan_branch_map = _build_branch_map_from_disbursements(disbursement_rows)
        unique_branches = set(loan_branch_map.values())
        _log(doc, f"   Branches resolved from file: {sorted(unique_branches)}")

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
        _ensure_loan_products(doc, disbursement_rows)

        # Step 3: Active Loans
        customer_map = load_all_customers_map()
        if doc.migrate_loans:
            _log(doc, "\n[3/5] Migrating Active Loans...")
            active_map, created, errs = _migrate_loans(
                doc, disbursement_rows, customer_map, loan_branch_map
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
                doc, hist_index, customer_map, loan_branch_map
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

        # Step 6: Post-migration LIA patches
        _log(doc, "\n[6/6] Running post-migration LIA patches...")
        _patch_fix_is_accrued(doc)
        _patch_delete_duplicate_lias(doc)
        _patch_recreate_missing_lias(doc)
        _patch_fix_detail_links(doc)
        _patch_verify_lias(doc)

        doc.db_set("errors_count", errors)
        doc.db_set("status", "Completed")
        _log(
            doc, f"\n[{now_datetime()}] ✅ MIGRATION COMPLETE — Total errors: {errors}"
        )

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
    query = "SELECT name FROM `tabLoan` WHERE description LIKE %s"
    if conds:
        query += f" {conds}"
    return frappe.db.sql_list(query, (f"%MigrationRef:{doc_name}%",))


def _rollback_repayments(doc):
    deleted = errors = 0
    # FIX: scoped to this migration only via MigrationRef
    loan_names = _migrated_loan_names(doc.name)
    print(loan_names)
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
        "SELECT name FROM `tabLoan` WHERE description LIKE %s ORDER BY name DESC",
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
    is_csv = file_url.lower().endswith(".csv")
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

    _KNOWN_HEADERS = {
        "full name",
        "borrower cnic #",
        "loan#",
        "loan #",
        "loan officer",
        "collection date",
        "total paid amount",
        "principal paid",
        "interest paid",
        "penalty paid",
        "fees paid",
        "disbursed date",
        "disbursed",
        "loan product",
        "status",
        "interest rate",
        "outstanding",
        "deductable fees",
        "dob",
        "gender",
        "mobile",
        "marital status",
        "religion",
        "branch",
        "cnic expiry date",
        "parentage/spouse name",
        "parentage/spouse mobile #",
    }

    def _header_score(row):
        return sum(
            1 for cell in row if str(cell or "").strip().lower() in _KNOWN_HEADERS
        )

    score0 = _header_score(rows[0])
    score1 = _header_score(rows[1]) if len(rows) > 1 else 0

    if score1 > score0:
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
    if val is None:
        return default
    s = str(val).strip()
    s = s.replace(",", "")
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
    if not raw:
        return ""
    s = str(raw).strip()
    try:
        s = str(int(decimal.Decimal(s)))
    except Exception:
        pass
    return s.replace("-", "").replace(" ", "").strip()


def _guess_periods(product_name):
    m = re.search(r"(\d+)\s*[Mm]", product_name)
    if m:
        return int(m.group(1))
    m = re.search(r"-0*(\d+)$", product_name)
    if m:
        return int(m.group(1))
    return 6


def get_all_loan_map():
    loan_map = {}
    conds = " AND description LIKE '%LoanRef:%' "

    loans = frappe.db.sql(
        f"SELECT name, description FROM `tabLoan` WHERE docstatus = 1 {conds} ",
        as_dict=True,
    )

    for loan in loans:
        m = re.search(r"LoanRef:([^\s,]+)", loan.description or "")
        if m:
            loan_map[m.group(1).strip()] = loan.name
    return loan_map


def _build_loan_map_from_db(doc=None):
    loan_map = {}
    conds = " AND description LIKE '%LoanRef:%' "

    if doc and getattr(doc, "name"):
        conds = f" AND description LIKE '%MigrationRef:{doc.name}%' "

    loans = frappe.db.sql(
        f"SELECT name, description FROM `tabLoan` WHERE docstatus = 1 {conds} ",
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


# ── Sales Person helper ───────────────────────────────────────────────────────

# Module-level cache: raw "Loan Officer" name → Sales Person docname
_sales_person_cache = {}


def _ensure_sales_person(loan_officer: str):
    """
    Return the Sales Person docname for a given Loan Officer string.

    Resolution order:
      1. Return None for blank / "0" values.
      2. Return cached result if already resolved this session.
      3. Look for an existing Sales Person by sales_person_name (case-insensitive).
      4. Auto-create a new Sales Person under the root group and cache it.

    Safe to call thousands of times per migration — each unique officer name
    hits the DB at most twice (one SELECT + one INSERT on first encounter only).
    """
    if not loan_officer:
        return None
    key = loan_officer.strip()
    if not key or key == "0":
        return None

    if key in _sales_person_cache:
        return _sales_person_cache[key]

    existing = frappe.db.get_value("Sales Person", {"sales_person_name": key}, "name")
    if existing:
        _sales_person_cache[key] = existing
        return existing

    try:
        root_group = (
            frappe.db.get_value(
                "Sales Person",
                {"is_group": 1, "parent_sales_person": ["is", "not set"]},
                "name",
            )
            or "Sales Teams"
        )
        sp = frappe.new_doc("Sales Person")
        sp.sales_person_name = key
        sp.is_group = 0
        sp.parent_sales_person = root_group
        sp.insert(ignore_permissions=True)
        frappe.db.commit()
        _sales_person_cache[key] = sp.name
        return sp.name
    except Exception:
        import traceback

        frappe.log_error(
            f"Could not auto-create Sales Person [{key}]\n{traceback.format_exc()}",
            "DataMigration-SalesPerson",
        )
        _sales_person_cache[key] = None
        return None


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


def load_all_customers_map():
    result = {}
    customers = frappe.db.sql(
        "SELECT name, custom_cnic_no  from `tabCustomer`  ", as_dict=True
    )

    for r in customers:
        result[r.custom_cnic_no] = r.name

    return result


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

            # Branch: resolve from customer row's Branch column, fall back to doc.branch
            raw_branch = str(r.get("Branch") or r.get("branch") or "").strip()
            customer_branch = _resolve_branch(raw_branch) or (
                doc.get("branch") if hasattr(doc, "get") else None
            )

            # Sales Person: resolve "Loan Officer" column, auto-creating if absent
            loan_officer = str(r.get("Loan Officer") or "").strip() or None
            sales_person_name = (
                _ensure_sales_person(loan_officer) if loan_officer else None
            )

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
            if customer_branch:
                c.branch = customer_branch
            if sales_person_name:
                c.sales_person = sales_person_name
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


def _loan_fields(doc, r, product, disb_date, status="Disbursed", branch=None):
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

    # Resolve Sales Person from "Loan Officer" column; fall back to doc-level default.
    loan_officer = str(r.get("Loan Officer") or "").strip() if r else ""
    sales_person = (
        _ensure_sales_person(loan_officer) if loan_officer else None
    ) or doc.sales_person

    # Branch: use passed branch (from disbursement file) → fall back to doc.branch
    resolved_branch = branch or (doc.get("branch") if hasattr(doc, "get") else None)

    loan = frappe.new_doc("Loan")
    loan.applicant_type = "Customer"
    loan.company = doc.company
    loan.branch = resolved_branch
    loan.loan_product = product
    loan.posting_date = disb_date
    loan.disbursement_date = disb_date
    loan.repayment_start_date = disb_date
    loan.sales_person = sales_person
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


def _migrate_loans(doc, rows, cnic_map, loan_branch_map=None):
    loan_map = {}
    created = errors = 0
    existing = _build_loan_map_from_db()
    loan_branch_map = loan_branch_map or {}

    for r in rows:
        raw_loan = r.get("Loan#") or r.get("Loan #")
        loan_no = str(raw_loan).strip()
        cnic = _cnic(r.get("Borrower CNIC #"))
        customer = cnic_map.get(cnic)

        if not loan_no or not customer:
            continue
        if loan_no in existing:
            loan_map[loan_no] = existing[loan_no]
            continue

        product = str(r.get("Loan Product") or "").strip() or None
        disb_date = _safe_date(r.get("Disbursed Date")) or nowdate()
        amount = _safe_float(r.get("Disbursed"))

        # Resolve branch from disbursement file Branch column
        raw_branch = r.get("Branch") or r.get("branch") or ""
        branch = _resolve_branch(str(raw_branch).strip()) or loan_branch_map.get(
            loan_no
        )

        try:
            loan = _loan_fields(doc, r, product, disb_date, "Disbursed", branch=branch)
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


def _migrate_historical_loans(doc, hist_index, cnic_map, loan_branch_map=None):
    loan_map = {}
    created = errors = 0
    existing = _build_loan_map_from_db()
    loan_branch_map = loan_branch_map or {}

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

        # Resolve branch from loan_branch_map (built from disbursement file)
        branch = loan_branch_map.get(loan_no)

        try:
            loan = _loan_fields(
                doc, None, default_product, disb_date, "Closed", branch=branch
            )
            loan.applicant = customer
            loan.loan_amount = amount
            loan.disbursed_amount = amount
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

            disbursed_amt = _safe_float(r.get("Disbursed"))
            deductable_fees = _safe_float(r.get("Deductable Fees"))
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


# ── Raw SQL for Loan Repayment (docstatus=1 directly — bypasses all hooks) ───
_REP_INSERT = """
    INSERT INTO `tabLoan Repayment`
        (name, creation, modified, modified_by, owner, docstatus,
         against_loan, posting_date, amount_paid, repayment_type,
         applicant_type, applicant, company, branch,
         loan_product, is_term_loan, cost_center, sales_person,
         payment_account, loan_account,
         principal_amount_paid, total_interest_paid, total_penalty_paid,
         interest_payable, payable_principal_amount,
         penalty_amount, pending_principal_amount)
    VALUES
        (%s, NOW(), NOW(), %s, %s, 1,
         %s, %s, %s, 'Normal Repayment',
         %s, %s, %s, %s,
         %s, %s, %s, %s,
         %s, %s,
         %s, %s, %s,
         %s, %s,
         %s, %s)
"""

_REP_DETAIL_INSERT = """
    INSERT INTO `tabLoan Repayment Detail`
        (name, creation, modified, modified_by, owner, docstatus,
         parent, parenttype, parentfield, idx,
         loan_interest_accrual, paid_interest_amount,
         paid_principal_amount, accrual_type)
    VALUES
        (%s, NOW(), NOW(), %s, %s, 1,
         %s, 'Loan Repayment', 'repayment_details', 1,
         %s, %s, %s, 'Repayment')
"""


def _insert_repayment_raw(
    rep_name,
    user,
    loan_doc,
    date_str,
    amount,
    applicant_type,
    applicant,
    company,
    branch,
    loan_product,
    is_term_loan,
    cost_center,
    sales_person,
    payment_account,
    loan_account,
    principal_paid,
    interest_paid,
    penalty_paid,
    pending_principal,
    lia_name,
):
    """
    Insert Loan Repayment + Detail row directly via SQL with docstatus=1.

    Skips ALL on_submit hooks intentionally:
      - book_unaccrued_interest  → LIA already inserted via _insert_lia_raw
      - book_unaccrued_penalty   → penalty LIA already inserted
      - update_paid_amount       → handled by _bulk_update_loan_totals after batch
      - make_gl_entries          → run create_missing_accrual_gl_entries post-migration
      - set_repayment_status     → run fix_repayment_statuses post-migration

    Name is pre-generated → eliminates tabSeries SELECT...FOR UPDATE lock.
    """
    frappe.db.sql(
        _REP_INSERT,
        (
            rep_name,
            user,
            user,
            loan_doc,
            date_str,
            amount,
            applicant_type,
            applicant,
            company,
            branch,
            loan_product,
            is_term_loan,
            cost_center,
            sales_person,
            payment_account,
            loan_account,
            principal_paid,
            interest_paid,
            penalty_paid,
            interest_paid,  # interest_payable = interest_paid (matches Excel)
            principal_paid,  # payable_principal_amount = principal_paid
            penalty_paid,
            pending_principal,
        ),
    )

    if lia_name:
        detail_name = frappe.generate_hash(length=10)
        frappe.db.sql(
            _REP_DETAIL_INSERT,
            (
                detail_name,
                user,
                user,
                rep_name,
                lia_name,
                interest_paid,
                principal_paid,
            ),
        )


def _bulk_update_loan_totals(loan_delta_map):
    """
    After each COMMIT_BATCH, apply accumulated principal/interest/penalty/amount
    deltas to tabLoan in one UPDATE per loan.

    Replaces the per-repayment UPDATE that update_paid_amount() was doing
    inside rep.submit() — eliminates the N+1 tabLoan lock contention.

    loan_delta_map: { loan_name → {principal, interest, penalty, amount} }
    """
    for loan_name, delta in loan_delta_map.items():
        frappe.db.sql(
            """
            UPDATE `tabLoan`
            SET
                total_principal_paid = COALESCE(total_principal_paid, 0) + %s,
                total_amount_paid    = COALESCE(total_amount_paid,    0) + %s,
                total_interest_paid  = COALESCE(total_interest_paid,  0) + %s,
                total_penalty_paid   = COALESCE(total_penalty_paid,   0) + %s
            WHERE name = %s
        """,
            (
                delta["principal"],
                delta["amount"],
                delta["interest"],
                delta["penalty"],
                loan_name,
            ),
        )


def _migrate_repayments(doc, rows, disbursement_rows, active_map, hist_map):
    import traceback
    import time as _time

    # Set session lock timeout for the entire migration job
    frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 14400")

    created = errors = 0
    unmatched = set()
    user = frappe.session.user or "Administrator"

    full_map = get_all_loan_map()
    full_map.update(active_map)
    full_map.update(hist_map)

    if not full_map:
        _log(doc, "   No loans found in DB — skipping repayments")
        return created, errors, unmatched

    all_loan_names = list(set(full_map.values()))
    _LOAN_FIELDS = (
        "name, applicant_type, applicant, company, branch, loan_product, "
        "is_term_loan, loan_account, payment_account, "
        "total_payment, debit_adjustment_amount, credit_adjustment_amount, "
        "total_principal_paid, total_interest_payable, written_off_amount, "
        "refund_amount, disbursed_amount, loan_amount, status"
    )
    loan_cache = {}
    CHUNK = 500
    for i in range(0, len(all_loan_names), CHUNK):
        chunk = all_loan_names[i : i + CHUNK]
        rows_db = frappe.db.sql(
            f"SELECT {_LOAN_FIELDS} FROM `tabLoan` "
            f"WHERE name IN ({','.join(['%s'] * len(chunk))})",
            tuple(chunk),
            as_dict=True,
        )
        for lr in rows_db:
            loan_cache[lr.name] = lr

    _log(doc, f"   Pre-fetched {len(loan_cache):,} loan records into memory")

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

    source_status_map = {
        str(dr.get("Loan#") or dr.get("Loan #") or "").strip(): str(
            dr.get("Status") or ""
        ).strip()
        for dr in (disbursement_rows or [])
        if dr.get("Loan#") or dr.get("Loan #")
    }

    fully_paid_loan_nos = set()

    # Sort oldest-first per loan so pending_principal is calculated correctly
    loan_groups = defaultdict(list)
    for r in rows:
        loan_no_key = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        loan_groups[loan_no_key].append(r)

    sorted_rows = []
    for loan_no, reps in loan_groups.items():
        reps.sort(key=lambda x: _safe_date(x.get("Collection Date")) or "9999-99-99")
        sorted_rows.extend(reps)

    # Build dedup set from existing repayments
    existing = set(
        f"{r[0]}_{r[1]}_{int(round(float(r[2] or 0) * 100))}"
        for r in frappe.db.sql(
            "SELECT against_loan, DATE_FORMAT(posting_date,'%Y-%m-%d'), amount_paid "
            "FROM `tabLoan Repayment` WHERE docstatus < 2"
        )
    )

    batch_count = 0
    loan_delta_map = defaultdict(
        lambda: {
            "principal": 0.0,
            "interest": 0.0,
            "penalty": 0.0,
            "amount": 0.0,
        }
    )

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
                f"Repayment row skipped — no valid Collection Date "
                f"Loan[{loan_no}] Amount[{r.get('Total Paid Amount')}]",
                f"DataMigration-{doc.name} RepNoDate",
            )
            errors += 1
            continue

        principal_paid = _safe_float(r.get("Principal Paid"))
        interest_paid = _safe_float(r.get("Interest Paid"))
        penalty_paid = _safe_float(r.get("Penalty Paid"))
        fees_paid = _safe_float(r.get("Fees Paid"))
        sales_person = r.get("Loan Officer") or doc.sales_person

        # Fold fees into principal (matches existing migration behaviour)
        if fees_paid > 0:
            principal_paid += fees_paid

        # Robust total — fallback to component sum if column missing/zero
        amount = _safe_float(r.get("Total Paid Amount"))
        if not amount:
            for col in ("Total Paid", "Amount Paid", "Paid Amount", "Total Payment"):
                amount = _safe_float(r.get(col))
                if amount:
                    break
        if not amount:
            amount = principal_paid + interest_paid + penalty_paid

        # Dedup check
        dupe_key = f"{loan_doc}_{date_str}_{int(round(amount * 100))}"
        if dupe_key in existing:
            continue

        try:
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
            is_term_loan = lr.get("is_term_loan", 1)
            pending_principal = _get_pending_principal(lr)

            # Insert LIA via raw SQL (already fast — keep as-is)
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

            # ── FAST PATH: raw SQL insert — no hooks, no series lock ──────────
            rep_name = f"LR-MIG-{frappe.generate_hash(length=10)}"
            _insert_repayment_raw(
                rep_name,
                user,
                loan_doc,
                date_str,
                amount,
                applicant_type,
                applicant,
                company,
                branch,
                loan_product,
                is_term_loan,
                cost_center,
                sales_person,
                payment_account,
                loan_account,
                principal_paid,
                interest_paid,
                penalty_paid,
                pending_principal,
                lia_name,
            )

            # Update in-memory cache so next repayment for the same loan
            # gets a correct pending_principal without a DB hit
            if loan_doc in loan_cache:
                loan_cache[loan_doc]["total_principal_paid"] = (
                    flt(loan_cache[loan_doc].get("total_principal_paid") or 0)
                    + principal_paid
                )

            # Accumulate deltas for bulk tabLoan update
            loan_delta_map[loan_doc]["principal"] += principal_paid
            loan_delta_map[loan_doc]["interest"] += interest_paid
            loan_delta_map[loan_doc]["penalty"] += penalty_paid
            loan_delta_map[loan_doc]["amount"] += amount

            existing.add(dupe_key)
            created += 1
            batch_count += 1

            # Every COMMIT_BATCH: bulk-update tabLoan totals then commit
            if batch_count % COMMIT_BATCH == 0:
                _bulk_update_loan_totals(loan_delta_map)
                frappe.db.commit()
                loan_delta_map.clear()
                _log(doc, f"   ...{created:,} repayments inserted")

        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} Rep[{loan_no}]",
            )
            errors += 1

    # Final flush for any remainder in the last partial batch
    if loan_delta_map:
        _bulk_update_loan_totals(loan_delta_map)
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


# ── Post-migration LIA patches ────────────────────────────────────────────────
# These run automatically at the end of run_migration_job (Step 6).
# They fix the is_accrued flag, duplicate LIAs, missing LIAs, and
# wrong repayment detail links — issues that arise because _insert_lia_raw
# bypasses all Frappe hooks and never sets repayment_schedule_name.


def _patch_fix_is_accrued(doc):
    """
    Patch 1 — Fix is_accrued flag on Repayment Schedule rows.

    ROOT CAUSE: _insert_lia_raw never sets repayment_schedule_name on the LIA,
    so the standard accrual job never flips is_accrued=1. This causes the
    scheduler to try to re-accrue all migrated loans every night and time out.

    LOGIC: Walk each loan's schedule rows cumulatively. If the sum of LIA
    principal coverage reaches a complete installment boundary → mark is_accrued=1.
    Mid-installment loans (partial payment) are left for the scheduler.
    """
    from frappe.utils import flt, cint, getdate
    from collections import defaultdict

    _log(doc, "   [Patch 1] Fixing is_accrued flags...")
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    BOUNDARY_TOLERANCE = 0.5

    # Only loans from this migration run
    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus=1",
        (f"%MigrationRef:{doc.name}%",),
    )
    if not loan_names:
        _log(doc, "   [Patch 1] No loans found — skipped")
        return

    ph = ",".join(["%s"] * len(loan_names))

    # LIA principal per loan
    lia_rows = frappe.db.sql(
        f"""SELECT lia.loan, SUM(lia.payable_principal_amount) AS lia_prin
            FROM `tabLoan Interest Accrual` lia
            WHERE lia.docstatus=1 AND lia.accrual_type='Repayment'
              AND lia.loan IN ({ph})
            GROUP BY lia.loan""",
        tuple(loan_names),
        as_dict=True,
    )
    lia_map = {r.loan: flt(r.lia_prin, precision) for r in lia_rows}

    if not lia_map:
        _log(doc, "   [Patch 1] No LIAs found — skipped")
        return

    lia_loan_names = list(lia_map.keys())
    lph = ",".join(["%s"] * len(lia_loan_names))

    # Schedule rows
    sched_rows = frappe.db.sql(
        f"""SELECT lrs.loan, rs.name, rs.payment_date,
                   rs.principal_amount, rs.is_accrued
            FROM `tabRepayment Schedule` rs
            INNER JOIN `tabLoan Repayment Schedule` lrs ON lrs.name = rs.parent
            WHERE lrs.docstatus=1 AND lrs.loan IN ({lph})
            ORDER BY lrs.loan, rs.idx ASC""",
        tuple(lia_loan_names),
        as_dict=True,
    )

    sched_map = defaultdict(list)
    for r in sched_rows:
        sched_map[r.loan].append(r)

    to_mark = []
    for loan, lia_prin in lia_map.items():
        rows = sched_map.get(loan, [])
        if not rows:
            continue

        total_sched = flt(
            sum(flt(r.principal_amount, precision) for r in rows), precision
        )

        if lia_prin >= total_sched - BOUNDARY_TOLERANCE:
            # Over-covered or exact — mark all past-due unaccrued rows
            to_mark.extend([r.name for r in rows if not r.is_accrued])
            continue

        # Walk installments cumulatively
        cumul = 0.0
        for r in rows:
            if r.is_accrued:
                cumul = flt(cumul + flt(r.principal_amount, precision), precision)
                continue
            p = flt(r.principal_amount, precision)
            cumul = flt(cumul + p, precision)
            if cumul <= lia_prin + BOUNDARY_TOLERANCE:
                to_mark.append(r.name)
                if abs(lia_prin - cumul) <= BOUNDARY_TOLERANCE:
                    break
            else:
                break  # mid-installment — leave for scheduler

    if not to_mark:
        _log(doc, "   [Patch 1] Nothing to mark — skipped")
        return

    # Bulk update in chunks
    chunk_size = 500
    updated = 0
    for i in range(0, len(to_mark), chunk_size):
        batch = to_mark[i : i + chunk_size]
        bph = ",".join(["%s"] * len(batch))
        frappe.db.sql(
            f"UPDATE `tabRepayment Schedule` SET is_accrued=1 WHERE name IN ({bph})",
            tuple(batch),
        )
        updated += len(batch)

    frappe.db.commit()
    _log(
        doc,
        f"   [Patch 1] ✅ Marked {updated:,} schedule rows is_accrued=1",
        flush=True,
    )


def _patch_delete_duplicate_lias(doc):
    """
    Patch 2 — Delete duplicate Repayment LIAs.

    CAUSE: Migration may run in multiple passes, each inserting LIAs without
    a dedup check (unlike repayments which have a date+amount guard).

    STRATEGY: For each (loan, date) group with count > 1, the LIA linked via
    tabLoan Repayment Detail is the KEEPER. Unlinked duplicates are deleted.
    Repoints detail rows before deleting to avoid orphan links.
    """
    from frappe.utils import flt
    from collections import defaultdict

    _log(doc, "   [Patch 2] Deleting duplicate LIAs...")

    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus=1",
        (f"%MigrationRef:{doc.name}%",),
    )
    if not loan_names:
        _log(doc, "   [Patch 2] No loans — skipped")
        return

    ph = ",".join(["%s"] * len(loan_names))

    # Get all Repayment LIAs for these loans
    lias = frappe.db.sql(
        f"""SELECT name, loan, DATE(due_date) as dt,
                   interest_amount, payable_principal_amount
            FROM `tabLoan Interest Accrual`
            WHERE loan IN ({ph}) AND docstatus=1 AND accrual_type='Repayment'
            ORDER BY loan, due_date""",
        tuple(loan_names),
        as_dict=True,
    )

    # Find groups with > 1 LIA
    groups = defaultdict(list)
    for r in lias:
        groups[(r.loan, str(r.dt))].append(r)

    dupe_groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupe_groups:
        _log(doc, "   [Patch 2] No duplicates found ✅")
        return

    all_lia_names = [r.name for r in lias]
    lph = ",".join(["%s"] * len(all_lia_names))

    # LIAs linked via repayment detail = keepers
    linked = frappe.db.sql(
        f"SELECT DISTINCT loan_interest_accrual FROM `tabLoan Repayment Detail`"
        f" WHERE loan_interest_accrual IN ({lph})",
        tuple(all_lia_names),
        as_list=True,
    )
    linked_set = {r[0] for r in linked}

    to_delete = []
    for (loan, dt), group in dupe_groups.items():
        linked_lias = [r for r in group if r.name in linked_set]
        unlinked_lias = [r for r in group if r.name not in linked_set]

        if linked_lias and unlinked_lias:
            to_delete.extend([r.name for r in unlinked_lias])
        # If all linked or all unlinked — ambiguous, skip safely

    if not to_delete:
        _log(doc, "   [Patch 2] No safe deletions identified ✅")
        return

    # Verify no GL entries
    dph = ",".join(["%s"] * len(to_delete))
    gl_count = frappe.db.sql(
        f"SELECT COUNT(*) FROM `tabGL Entry`"
        f" WHERE voucher_type='Loan Interest Accrual'"
        f" AND voucher_no IN ({dph}) AND is_cancelled=0",
        tuple(to_delete),
    )[0][0]

    if gl_count > 0:
        _log(
            doc,
            f"   [Patch 2] ⚠️  {gl_count} GL entries found on duplicates — skipping deletion",
        )
        return

    frappe.db.sql(
        f"UPDATE `tabLoan Interest Accrual` SET docstatus=2 WHERE name IN ({dph})",
        tuple(to_delete),
    )
    frappe.db.commit()

    deleted = 0
    for name in to_delete:
        frappe.delete_doc(
            "Loan Interest Accrual",
            name,
            force=True,
            ignore_permissions=True,
            delete_permanently=True,
        )
        deleted += 1
        if deleted % 100 == 0:
            frappe.db.commit()

    frappe.db.commit()
    _log(doc, f"   [Patch 2] ✅ Deleted {deleted:,} duplicate LIAs", flush=True)


def _patch_recreate_missing_lias(doc):
    """
    Patch 3 — Recreate LIAs missing for repayments.

    Finds loans where SUM(total_interest_paid) from repayments exceeds
    SUM(interest_amount) from LIAs by more than 1. For each such repayment
    with no matching LIA by (loan, date, int, prin) → recreates the LIA.
    """
    from frappe.utils import flt
    from collections import defaultdict

    _log(doc, "   [Patch 3] Recreating missing LIAs...")

    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus=1",
        (f"%MigrationRef:{doc.name}%",),
    )
    if not loan_names:
        _log(doc, "   [Patch 3] No loans — skipped")
        return

    ph = ",".join(["%s"] * len(loan_names))

    # Find affected loans where repayment interest > LIA interest
    affected = frappe.db.sql(
        f"""SELECT l.name AS loan
            FROM `tabLoan` l
            LEFT JOIN (
                SELECT against_loan, SUM(total_interest_paid) AS rep_int,
                       SUM(principal_amount_paid) AS rep_prin
                FROM `tabLoan Repayment` WHERE docstatus=1
                GROUP BY against_loan
            ) lr ON lr.against_loan = l.name
            LEFT JOIN (
                SELECT loan, SUM(interest_amount) AS lia_int,
                       SUM(payable_principal_amount) AS lia_prin
                FROM `tabLoan Interest Accrual`
                WHERE docstatus=1 AND accrual_type='Repayment'
                GROUP BY loan
            ) lia ON lia.loan = l.name
            WHERE l.name IN ({ph})
              AND COALESCE(lr.rep_int,0) > COALESCE(lia.lia_int,0) + 1""",
        tuple(loan_names),
        as_list=True,
    )

    if not affected:
        _log(doc, "   [Patch 3] No missing LIAs detected ✅")
        return

    aff_names = [r[0] for r in affected]
    aph = ",".join(["%s"] * len(aff_names))

    # Load loan details for _insert_lia_raw
    loan_details = frappe.db.sql(
        f"SELECT name, company, branch, applicant_type, applicant, loan_product, cost_center"
        f" FROM `tabLoan` WHERE name IN ({aph})",
        tuple(aff_names),
        as_dict=True,
    )
    loan_map = {r.name: r for r in loan_details}

    products = list({r.loan_product for r in loan_details if r.loan_product})
    if products:
        pph = ",".join(["%s"] * len(products))
        income_accts = frappe.db.sql(
            f"SELECT name, interest_income_account FROM `tabLoan Product` WHERE name IN ({pph})",
            tuple(products),
            as_dict=True,
        )
        income_map = {r.name: r.interest_income_account for r in income_accts}
    else:
        income_map = {}

    # Repayments for affected loans
    reps = frappe.db.sql(
        f"""SELECT against_loan, DATE(posting_date) as dt,
                   total_interest_paid, principal_amount_paid
            FROM `tabLoan Repayment`
            WHERE against_loan IN ({aph}) AND docstatus=1
              AND (total_interest_paid > 0 OR principal_amount_paid > 0)
            ORDER BY against_loan, posting_date""",
        tuple(aff_names),
        as_dict=True,
    )

    # Existing LIAs by (loan, date, int, prin) signature
    existing_lias = frappe.db.sql(
        f"""SELECT loan, DATE(due_date) as dt,
                   interest_amount, payable_principal_amount
            FROM `tabLoan Interest Accrual`
            WHERE loan IN ({aph}) AND docstatus=1 AND accrual_type='Repayment'""",
        tuple(aff_names),
        as_dict=True,
    )
    existing_set = set()
    for r in existing_lias:
        existing_set.add(
            (
                r.loan,
                str(r.dt),
                round(flt(r.interest_amount), 2),
                round(flt(r.payable_principal_amount), 2),
            )
        )

    user = frappe.session.user or "Administrator"
    created = errors = 0

    for r in reps:
        key = (
            r.against_loan,
            str(r.dt),
            round(flt(r.total_interest_paid), 2),
            round(flt(r.principal_amount_paid), 2),
        )
        if key in existing_set:
            continue

        loan = loan_map.get(r.against_loan)
        if not loan:
            continue
        income_account = (
            income_map.get(loan.loan_product) or doc.interest_income_account
        )

        try:
            _insert_lia_raw(
                r.against_loan,
                loan.company,
                loan.branch,
                loan.cost_center,
                str(r.dt),
                flt(r.total_interest_paid),
                flt(r.principal_amount_paid),
                0.0,
                "Repayment",
                loan.applicant_type,
                loan.applicant,
                income_account,
                user,
            )
            existing_set.add(key)  # prevent re-insert if same loan appears again
            created += 1
        except Exception:
            import traceback

            frappe.log_error(
                traceback.format_exc(), f"Patch3-MissingLIA-{r.against_loan}"
            )
            errors += 1

    frappe.db.commit()
    _log(
        doc,
        f"   [Patch 3] ✅ Created {created:,} missing LIAs  Errors: {errors}",
        flush=True,
    )


def _patch_fix_detail_links(doc):
    """
    Patch 4 — Fix wrong Loan Repayment Detail → LIA links.

    After dedup deletions and recreations, some detail rows may point to
    the wrong LIA. Fix by matching detail's (loan, date, paid_int, paid_prin)
    against existing LIA (loan, date, interest_amount, payable_principal_amount).
    For split repayments (no exact amount match), repoint to any LIA on same date.
    """
    from frappe.utils import flt
    from collections import defaultdict

    _log(doc, "   [Patch 4] Fixing repayment detail links...")

    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus=1",
        (f"%MigrationRef:{doc.name}%",),
    )
    if not loan_names:
        _log(doc, "   [Patch 4] No loans — skipped")
        return

    ph = ",".join(["%s"] * len(loan_names))

    # Get all repayment details for these loans
    details = frappe.db.sql(
        f"""SELECT rd.name, rd.loan_interest_accrual,
                   rd.paid_interest_amount, rd.paid_principal_amount,
                   lr.against_loan, DATE(lr.posting_date) as dt
            FROM `tabLoan Repayment Detail` rd
            INNER JOIN `tabLoan Repayment` lr ON lr.name = rd.parent
            WHERE lr.against_loan IN ({ph}) AND lr.docstatus=1""",
        tuple(loan_names),
        as_dict=True,
    )

    if not details:
        _log(doc, "   [Patch 4] No details found — skipped")
        return

    aff_loans = list({d.against_loan for d in details})
    aph = ",".join(["%s"] * len(aff_loans))

    # LIA lookup by (loan, date, int, prin) → [lia_name]
    lias = frappe.db.sql(
        f"""SELECT name, loan, DATE(due_date) as dt,
                   interest_amount, payable_principal_amount
            FROM `tabLoan Interest Accrual`
            WHERE loan IN ({aph}) AND docstatus=1 AND accrual_type='Repayment'""",
        tuple(aff_loans),
        as_dict=True,
    )

    lia_lookup = defaultdict(list)  # (loan, date, int, prin) → [names]
    date_lookup = defaultdict(list)  # (loan, date) → [names]
    for r in lias:
        key = (
            r.loan,
            str(r.dt),
            round(flt(r.interest_amount), 2),
            round(flt(r.payable_principal_amount), 2),
        )
        lia_lookup[key].append(r.name)
        date_lookup[(r.loan, str(r.dt))].append(r.name)

    fixed = skipped = 0
    for d in details:
        key = (
            d.against_loan,
            str(d.dt),
            round(flt(d.paid_interest_amount), 2),
            round(flt(d.paid_principal_amount), 2),
        )
        candidates = lia_lookup.get(key, [])

        if candidates and d.loan_interest_accrual in candidates:
            skipped += 1
            continue  # already correct

        correct_lia = None
        if candidates:
            correct_lia = candidates[0]
        else:
            # Split repayment — no exact amount match, use any LIA on same date
            date_candidates = date_lookup.get((d.against_loan, str(d.dt)), [])
            if date_candidates:
                correct_lia = date_candidates[0]

        if correct_lia and correct_lia != d.loan_interest_accrual:
            frappe.db.sql(
                "UPDATE `tabLoan Repayment Detail` SET loan_interest_accrual=%s WHERE name=%s",
                (correct_lia, d.name),
            )
            fixed += 1

    frappe.db.commit()
    _log(
        doc,
        f"   [Patch 4] ✅ Fixed {fixed:,} detail links  Already correct: {skipped:,}",
        flush=True,
    )


def _patch_verify_lias(doc):
    """
    Patch 5 — Validate final LIA state and log a summary.
    Checks: mismatch count, duplicate groups, orphan details.
    """
    from frappe.utils import flt

    _log(doc, "   [Patch 5] Validating LIA state...")

    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus=1",
        (f"%MigrationRef:{doc.name}%",),
    )
    if not loan_names:
        _log(doc, "   [Patch 5] No loans — skipped")
        return

    ph = ",".join(["%s"] * len(loan_names))

    # Mismatch count
    mismatch = frappe.db.sql(
        f"""SELECT COUNT(DISTINCT l.name) FROM `tabLoan` l
            LEFT JOIN (
                SELECT against_loan, SUM(total_interest_paid) AS rep_int
                FROM `tabLoan Repayment` WHERE docstatus=1 GROUP BY against_loan
            ) lr ON lr.against_loan = l.name
            LEFT JOIN (
                SELECT loan, SUM(interest_amount) AS lia_int
                FROM `tabLoan Interest Accrual`
                WHERE docstatus=1 AND accrual_type='Repayment' GROUP BY loan
            ) lia ON lia.loan = l.name
            WHERE l.name IN ({ph})
              AND ABS(COALESCE(lia.lia_int,0) - COALESCE(lr.rep_int,0)) > 1""",
        tuple(loan_names),
    )[0][0]

    # Duplicate groups
    dupes = frappe.db.sql(
        f"""SELECT COUNT(*) FROM (
                SELECT loan, DATE(due_date)
                FROM `tabLoan Interest Accrual`
                WHERE docstatus=1 AND accrual_type='Repayment' AND loan IN ({ph})
                GROUP BY loan, DATE(due_date)
                HAVING COUNT(*) > 1
            ) t""",
        tuple(loan_names),
    )[0][0]

    # Orphan details
    orphans = frappe.db.sql(
        f"""SELECT COUNT(*) FROM `tabLoan Repayment Detail` rd
            INNER JOIN `tabLoan Repayment` lr ON lr.name = rd.parent
            LEFT JOIN `tabLoan Interest Accrual` lia ON lia.name = rd.loan_interest_accrual
            WHERE lr.against_loan IN ({ph})
              AND rd.loan_interest_accrual IS NOT NULL
              AND rd.loan_interest_accrual != ''
              AND lia.name IS NULL""",
        tuple(loan_names),
    )[0][0]

    # is_accrued remaining
    remaining_unaccrued = frappe.db.sql(
        f"""SELECT COUNT(*) FROM `tabRepayment Schedule` rs
            INNER JOIN `tabLoan Repayment Schedule` lrs ON lrs.name = rs.parent
            WHERE lrs.loan IN ({ph}) AND lrs.docstatus=1
              AND rs.is_accrued=0 AND rs.payment_date < CURDATE()""",
        tuple(loan_names),
    )[0][0]

    status = "✅" if mismatch == 0 and dupes == 0 and orphans == 0 else "⚠️"
    _log(doc, f"   [Patch 5] {status} LIA Validation:")
    _log(doc, f"             Loans processed         : {len(loan_names):,}")
    _log(doc, f"             LIA vs rep mismatches   : {mismatch:,}  (target: 0)")
    _log(doc, f"             Duplicate LIA groups    : {dupes:,}  (target: 0)")
    _log(doc, f"             Orphan detail rows      : {orphans:,}  (target: 0)")
    _log(
        doc,
        f"             Past-due unaccrued rows : {remaining_unaccrued:,}  (mid-installment — ok)",
        flush=True,
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
    doc = frappe.get_doc("Data Migration Tool", doc_name)
    report = {"ok": True, "errors": []}

    REQUIRED_COLS = {
        "customer_file": [
            "Full Name",
            "Mobile",
            "Borrower CNIC #",
            "Gender",
            "DOB",
            "CNIC Expiry Date",
            "Marital Status",
            "Religion",
            "Parentage/Spouse Name",
            "Parentage/Spouse Mobile #",
            "Loan Officer",
        ],
        "disbursement_file": [
            "Branch",
            "Disbursed Date",
            "Name",
            "Loan Product",
            "Loan #",
            "Disbursed",
            "Borrower CNIC #",
            "Interest Rate",
            "Deductable Fees",
            "Loan Officer",
        ],
        "repayment_file": [
            "Loan #",
            "Borrower Name",
            "Collection Date",
            "Total Paid Amount",
            "Principal Paid",
            "Interest Paid",
            "Penalty Paid",
            "Loan Officer",
        ],
    }

    for field, required_cols in REQUIRED_COLS.items():
        file_url = doc.get(field)
        if not file_url:
            report["errors"].append(f"{field.replace('_', ' ').title()} not uploaded")
            report["ok"] = False
            continue

        try:
            rows = _read_excel(file_url)
            if not rows:
                report["errors"].append(f"{field.replace('_', ' ').title()} is empty")
                report["ok"] = False
                continue

            actual = {str(k).strip().lower() for k in rows[0].keys()}
            missing = [c for c in required_cols if c.strip().lower() not in actual]
            if missing:
                report["ok"] = False
                for col in missing:
                    report["errors"].append(
                        f"{field.replace('_', ' ').title()}: missing column '{col}'"
                    )

        except Exception as e:
            report["errors"].append(
                f"Could not read {field.replace('_', ' ').title()}: {e}"
            )
            report["ok"] = False

    return report


@frappe.whitelist()
def reconcile_loan_paid_amounts(doc_name=None, dry_run=0):
    import traceback

    precision = int(frappe.db.get_default("currency_precision") or 2)
    dry_run = int(dry_run)
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

        if not dry_run and idx % commit_every == 0:
            frappe.db.commit()

    if not dry_run:
        frappe.db.commit()

    return {
        "status": "dry_run" if dry_run else "done",
        "total": len(loan_names),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "details": details[:50],
    }


def _reconcile_single_loan(loan_name, precision, dry_run):
    has_int_paid = frappe.db.has_column("Loan", "total_interest_paid")
    has_pen_paid = frappe.db.has_column("Loan", "total_penalty_paid")

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

    lia_updates = []
    loan_update = {}

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

    if loan_update:
        frappe.db.set_value("Loan", loan_name, loan_update, update_modified=False)

    if lia_updates:
        for paid_int, paid_prin, lia_name in lia_updates:
            frappe.db.sql(
                """
                UPDATE `tabLoan Interest Accrual`
                SET paid_interest_amount  = %s,
                    paid_principal_amount = %s
                WHERE name = %s
                """,
                (paid_int, paid_prin, lia_name),
                auto_commit=False,
            )

    # if lia_updates:

    #     frappe.db.sql(
    #         """
    #         UPDATE `tabLoan Interest Accrual`
    #         SET paid_interest_amount  = %s,
    #             paid_principal_amount = %s
    #         WHERE name = %s
    #         """,
    #         lia_updates,
    #         auto_commit=False,
    #     )

    return result


@frappe.whitelist()
def fix_repayment_statuses(dry_run=0, branch=None):

    import traceback

    dry_run = int(dry_run)
    conds = ""
    if branch is not None:
        conds += f" AND branch = '{branch}' "

    loan_names = frappe.db.sql_list(
        f"SELECT name FROM `tabLoan` WHERE description LIKE '%LoanRef:%' AND docstatus = 1 {conds}",
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
    import traceback
    from collections import defaultdict

    tolerance = float(tolerance)

    doc = frappe.get_doc("Data Migration Tool", doc_name)
    if not doc.repayment_file:
        frappe.throw("No repayment file attached to this migration document.")

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
        "unmatched_ids": unmatched,
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


@frappe.whitelist()
def retry_failed_records_creation(name):
    res = create_failed_loans_entries(name)
    return res


def create_failed_loans_entries(doc_name):
    doc = frappe.get_doc("Data Migration Tool", doc_name)
    lines = []

    def _log(msg):
        print(msg)
        lines.append(msg)

    _log("   Reading Excel files...")
    customer_rows = _read_excel(doc.customer_file)
    disbursement_rows = _read_excel(doc.disbursement_file)
    repayment_rows = _read_excel(doc.repayment_file)

    _log(
        f"Customers: {len(customer_rows):,} | "
        f"Disbursements: {len(disbursement_rows):,} | "
        f"Repayments: {len(repayment_rows):,}"
    )

    cnic_to_customer = {}
    for r in customer_rows:
        cnic = _cnic(r.get("Borrower CNIC #"))
        if cnic:
            name = frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name")
            if name:
                cnic_to_customer[cnic] = name

    _log(f"   CNIC→Customer map: {len(cnic_to_customer):,} entries")
    existing_map = _build_loan_map_from_db(doc)
    _log(f"   Existing loans in DB: {len(existing_map):,}")

    missing_rows = []
    existing_rows = []
    loans_skipped = 0

    for r in disbursement_rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if not loan_no:
            continue
        if loan_no in existing_map:
            existing_rows.append(r)
            loans_skipped += 1
        else:
            missing_rows.append(r)

    _log(f"   Already exist (repayment check): {loans_skipped:,}")
    _log(f"   Missing loans to create:         {len(missing_rows):,}")

    rep_by_loan = defaultdict(list)
    for r in repayment_rows:
        ln = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        rep_by_loan[ln].append(r)

    for ln in rep_by_loan:
        rep_by_loan[ln].sort(
            key=lambda x: _safe_date(x.get("Collection Date")) or "9999-99-99"
        )

    loans_created = disb_created = repayments_created = errors = 0
    new_loan_map = {}
    user = frappe.session.user or "Administrator"

    _cc_cache = {}

    def _get_cc(company):
        if company not in _cc_cache:
            _cc_cache[company] = frappe.db.get_value(
                "Company", company, "cost_center"
            ) or frappe.db.get_value(
                "Cost Center", {"is_group": 0, "company": company}, "name"
            )
        return _cc_cache[company]

    _ia_cache = {}

    def _get_ia(loan_product):
        if loan_product not in _ia_cache:
            _ia_cache[loan_product] = (
                frappe.db.get_value(
                    "Loan Product", loan_product, "interest_income_account"
                )
                if loan_product
                else None
            ) or doc.interest_income_account
        return _ia_cache[loan_product]

    def _create_repayments_for_loan(loan_name, loan_no, loan_cache_row):
        from collections import Counter

        rep_rows = rep_by_loan.get(loan_no, [])
        if not rep_rows:
            _log(f"   ℹ️  No repayments in Excel for {loan_no}")
            return 0, 0

        existing_rep_counter = Counter(
            f"{row[0]}_{row[1]}_{int(round(float(row[2] or 0) * 100))}"
            for row in frappe.db.sql(
                "SELECT against_loan, DATE_FORMAT(posting_date,'%%Y-%%m-%%d'), amount_paid "
                "FROM `tabLoan Repayment` WHERE against_loan=%s AND docstatus < 2",
                (loan_name,),
            )
        )

        created = errs = 0
        # Accumulate deltas — flushed after each loan's repayments
        loan_delta = {"principal": 0.0, "interest": 0.0, "penalty": 0.0, "amount": 0.0}

        for rr in rep_rows:
            col_date = rr.get("Collection Date")
            date_str = (
                col_date.strftime("%Y-%m-%d")
                if hasattr(col_date, "strftime")
                else _safe_date(col_date)
            )
            if not date_str:
                _log(f"   ⚠️  Rep skip {loan_no} — no collection date")
                errs += 1
                continue

            principal_paid = _safe_float(rr.get("Principal Paid"))
            interest_paid = _safe_float(rr.get("Interest Paid"))
            penalty_paid = _safe_float(rr.get("Penalty Paid"))
            fees_paid = _safe_float(rr.get("Fees Paid"))

            if fees_paid > 0:
                principal_paid += fees_paid

            # Robust total — fallback to component sum if column missing/zero
            amount = _safe_float(rr.get("Total Paid Amount"))
            if not amount:
                for col in (
                    "Total Paid",
                    "Amount Paid",
                    "Paid Amount",
                    "Total Payment",
                ):
                    amount = _safe_float(rr.get(col))
                    if amount:
                        break
            if not amount:
                amount = principal_paid + interest_paid + penalty_paid

            dupe_key = f"{loan_name}_{date_str}_{int(round(amount * 100))}"
            if existing_rep_counter[dupe_key] > 0:
                existing_rep_counter[dupe_key] -= 1
                continue

            try:
                company = loan_cache_row.get("company") or doc.company
                branch = loan_cache_row.get("branch") or doc.branch
                loan_product = loan_cache_row.get("loan_product")
                cost_center = _get_cc(company)
                payment_account = (
                    loan_cache_row.get("payment_account") or doc.payment_account
                )
                loan_account = loan_cache_row.get("loan_account") or doc.loan_account
                applicant_type = loan_cache_row.get("applicant_type", "Customer")
                applicant = loan_cache_row.get("applicant")
                income_account = _get_ia(loan_product)
                is_term_loan = loan_cache_row.get("is_term_loan", 1)
                pending_principal = _get_pending_principal(loan_cache_row)

                lia_name = None
                if interest_paid > 0 or principal_paid > 0:
                    lia_name = _insert_lia_raw(
                        loan_name,
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
                        loan_name,
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

                # ── FAST PATH: raw SQL insert — no hooks, no series lock ──────
                rep_name = f"LR-MIG-{frappe.generate_hash(length=10)}"
                _insert_repayment_raw(
                    rep_name,
                    user,
                    loan_name,
                    date_str,
                    amount,
                    applicant_type,
                    applicant,
                    company,
                    branch,
                    loan_product,
                    is_term_loan,
                    cost_center,
                    None,
                    payment_account,
                    loan_account,
                    principal_paid,
                    interest_paid,
                    penalty_paid,
                    pending_principal,
                    lia_name,
                )

                # Update in-memory cache for correct pending_principal next row
                loan_cache_row["total_principal_paid"] = (
                    flt(loan_cache_row.get("total_principal_paid") or 0)
                    + principal_paid
                )

                loan_delta["principal"] += principal_paid
                loan_delta["interest"] += interest_paid
                loan_delta["penalty"] += penalty_paid
                loan_delta["amount"] += amount
                created += 1

                if (repayments_created + created) % COMMIT_BATCH == 0:
                    _bulk_update_loan_totals({loan_name: loan_delta})
                    frappe.db.commit()
                    loan_delta = {
                        "principal": 0.0,
                        "interest": 0.0,
                        "penalty": 0.0,
                        "amount": 0.0,
                    }
                    _log(f"   ...{repayments_created + created:,} repayments inserted")

            except Exception:
                tb = traceback.format_exc()
                frappe.log_error(
                    tb,
                    f"RetryMigration-{doc_name} Rep[{loan_no}] {date_str}",
                )
                _log(
                    f"   ❌ Rep FAILED {loan_no} {date_str}:\n      {tb.splitlines()[-1]}"
                )
                errs += 1

        # Flush remaining deltas for this loan
        if any(v > 0 for v in loan_delta.values()):
            _bulk_update_loan_totals({loan_name: loan_delta})
            frappe.db.commit()

        return created, errs

    if missing_rows:
        _log(f"\n── Pass 1: creating {len(missing_rows):,} missing loans ──")

        missing_loan_nos = [
            str(r.get("Loan#") or r.get("Loan #") or "").strip() for r in missing_rows
        ]
        _log(f"   Loan# list: {missing_loan_nos}")

        for r in missing_rows:
            loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
            cnic = _cnic(r.get("Borrower CNIC #"))
            customer = cnic_to_customer.get(cnic)

            if not customer:
                _log(f"   ⚠️  SKIP {loan_no} — no customer for CNIC {cnic}")
                errors += 1
                continue

            product = str(r.get("Loan Product") or "").strip() or None
            disb_date = _safe_date(r.get("Disbursed Date")) or nowdate()

            raw_amount = _safe_float(r.get("Disbursed"))
            if not raw_amount:
                raw_amount = sum(
                    _safe_float(rr.get("Principal Paid"))
                    for rr in rep_by_loan.get(loan_no, [])
                )

            if not raw_amount:
                _log(f"   ⚠️  SKIP {loan_no} — cannot determine loan amount")
                errors += 1
                continue
            try:
                loan = _loan_fields(doc, r, product, disb_date, "Disbursed")
                loan.applicant = customer
                if not loan.sales_person:  # FIX-A
                    loan.sales_person = doc.sales_person
                loan.loan_amount = raw_amount
                loan.disbursed_amount = 0
                loan.description = (
                    f"Migrated (retry). LoanRef:{loan_no} MigrationRef:{doc_name}"
                )
                loan.insert(ignore_permissions=True)
                loan.submit()
                frappe.db.commit()

                new_loan_map[loan_no] = loan.name
                loans_created += 1
                _log(
                    f"   ✅ Loan created: {loan_no} → {loan.name} (amount={raw_amount})"
                )

            except Exception:
                tb = traceback.format_exc()
                frappe.log_error(tb, f"RetryMigration-{doc_name} Loan[{loan_no}]")
                _log(f"   ❌ Loan FAILED {loan_no}:\n      {tb.splitlines()[-1]}")
                errors += 1
                continue

            # ── Disbursement ───────────────────────────────────────────────────
            try:
                lr_data = frappe.db.get_value(
                    "Loan",
                    loan.name,
                    [
                        "applicant_type",
                        "applicant",
                        "company",
                        "branch",
                        "loan_product",
                    ],
                    as_dict=True,
                )
                disbursed_amt = _safe_float(r.get("Disbursed")) or raw_amount
                deductable_fees = _safe_float(r.get("Deductable Fees"))
                charges_acct = (
                    doc.get("charges_income_account") or doc.interest_income_account
                )

                d = frappe.new_doc("Loan Disbursement")
                d.against_loan = loan.name
                d.disbursement_date = disb_date
                d.disbursed_amount = disbursed_amt
                d.applicant_type = lr_data.applicant_type
                d.applicant = lr_data.applicant
                d.company = lr_data.company
                d.branch = lr_data.branch
                d.loan_product = lr_data.loan_product

                if deductable_fees > 0 and charges_acct:
                    d.append(
                        "loan_disbursement_charges",
                        {
                            "charge": None,
                            "account": charges_acct,
                            "amount": deductable_fees,
                        },
                    )

                d.insert(ignore_permissions=True)
                d.submit()
                frappe.db.commit()
                disb_created += 1
                _log(f"   ✅ Disbursement created: {d.name}")

            except Exception:
                tb = traceback.format_exc()
                frappe.log_error(tb, f"RetryMigration-{doc_name} Disb[{loan_no}]")
                _log(
                    f"   ❌ Disbursement FAILED {loan_no}:\n      {tb.splitlines()[-1]}"
                )
                errors += 1

            # ── Repayments ─────────────────────────────────────────────────────
            loan_cache_row = frappe.db.get_value(
                "Loan",
                loan.name,
                (
                    "name, applicant_type, applicant, company, branch, loan_product, "
                    "is_term_loan, loan_account, payment_account, "
                    "total_payment, debit_adjustment_amount, credit_adjustment_amount, "
                    "total_principal_paid, total_interest_payable, written_off_amount, "
                    "refund_amount, disbursed_amount, loan_amount, status"
                ),
                as_dict=True,
            )
            created, errs = _create_repayments_for_loan(
                loan.name, loan_no, loan_cache_row
            )
            repayments_created += created
            errors += errs
            _log(f"   ↳ {loan_no}: {created} repayments created")

    else:
        _log("\n── Pass 1: no missing loans — skipped ──")
        missing_loan_nos = []

    _log(
        f"\n── Pass 2: checking repayments for {len(existing_rows):,} existing loans ──"
    )
    pass2_loans_checked = pass2_reps_created = pass2_errors = 0

    existing_loan_names = list(
        {
            existing_map[str(r.get("Loan#") or r.get("Loan #") or "").strip()]
            for r in existing_rows
            if str(r.get("Loan#") or r.get("Loan #") or "").strip() in existing_map
        }
    )

    _LOAN_FIELDS = (
        "name, applicant_type, applicant, company, branch, loan_product, "
        "is_term_loan, loan_account, payment_account, "
        "total_payment, debit_adjustment_amount, credit_adjustment_amount, "
        "total_principal_paid, total_interest_payable, written_off_amount, "
        "refund_amount, disbursed_amount, loan_amount, status"
    )
    loan_cache = {}
    CHUNK = 500
    for i in range(0, len(existing_loan_names), CHUNK):
        chunk = existing_loan_names[i : i + CHUNK]
        rows_db = frappe.db.sql(
            f"SELECT {_LOAN_FIELDS} FROM `tabLoan` "
            f"WHERE name IN ({','.join(['%s'] * len(chunk))})",
            tuple(chunk),
            as_dict=True,
        )
        for lr in rows_db:
            loan_cache[lr.name] = lr

    for r in existing_rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        loan_name = existing_map.get(loan_no)
        if not loan_name:
            continue

        loan_cache_row = loan_cache.get(loan_name)
        if not loan_cache_row:
            continue

        excel_count = len(rep_by_loan.get(loan_no, []))
        if not excel_count:
            continue

        created, errs = _create_repayments_for_loan(loan_name, loan_no, loan_cache_row)
        pass2_loans_checked += 1
        pass2_reps_created += created
        pass2_errors += errs

        if created:
            _log(
                f"   ✅ {loan_no} ({loan_name}): {created} missing repayment(s) created"
            )

    repayments_created += pass2_reps_created
    errors += pass2_errors

    _log(
        f"   Pass 2 summary: {pass2_loans_checked:,} loans checked, "
        f"{pass2_reps_created:,} repayments created, {pass2_errors} errors"
    )

    all_touched_loan_nos = set(new_loan_map.keys())
    for r in existing_rows:
        ln = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if ln in existing_map:
            all_touched_loan_nos.add(ln)

    full_map = {**existing_map, **new_loan_map}

    if all_touched_loan_nos:
        _log("\n   Running force-close on all touched loans...")
        source_status_map = {
            str(r.get("Loan#") or r.get("Loan #") or "").strip(): str(
                r.get("Status") or ""
            ).strip()
            for r in disbursement_rows
            if r.get("Loan#") or r.get("Loan #")
        }
        _force_close_fully_paid_loans(
            doc,
            all_touched_loan_nos,
            full_map,
            source_status_map,
        )

    # ── Final summary ─────────────────────────────────────────────────────────
    _log(f"\n[{now_datetime()}] ── create_failed_loans_entries complete ──")
    _log(f"   Loans created:      {loans_created:,}")
    _log(f"   Disbursements:      {disb_created:,}")
    _log(f"   Repayments created: {repayments_created:,}")
    _log(f"   Errors:             {errors}")

    # Update counters on the migration doc
    frappe.db.set_value(
        "Data Migration Tool",
        doc_name,
        {
            "loans_created": (
                (
                    frappe.db.get_value(
                        "Data Migration Tool", doc_name, "loans_created"
                    )
                    or 0
                )
                + loans_created
            ),
            "repayments_created": (
                (
                    frappe.db.get_value(
                        "Data Migration Tool", doc_name, "repayments_created"
                    )
                    or 0
                )
                + repayments_created
            ),
            "errors_count": max(
                0,
                (
                    frappe.db.get_value("Data Migration Tool", doc_name, "errors_count")
                    or 0
                )
                - loans_created,
            ),
        },
        update_modified=False,
    )
    _flush_log(doc, lines)
    frappe.db.commit()

    return {
        "missing_loans": missing_loan_nos,
        "loans_created": loans_created,
        "disb_created": disb_created,
        "repayments_created": repayments_created,
        "loans_skipped": loans_skipped,
        "errors": errors,
        "log": "\n".join(lines),
    }


def _flush_log(doc, lines):
    existing = (
        frappe.db.get_value("Data Migration Tool", doc.name, "migration_log") or ""
    )
    frappe.db.set_value(
        "Data Migration Tool",
        doc.name,
        "migration_log",
        existing + "\n".join(lines) + "\n",
        update_modified=False,
    )
    frappe.db.commit()
