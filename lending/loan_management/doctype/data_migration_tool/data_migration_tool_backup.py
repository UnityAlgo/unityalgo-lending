#  OLD one 
# Copyright (c) 2026, Enigma Logics
# For license information, please see license.txt
#
# ── FIXES ─────────────────────────────────────────────────────────────────────
#
# FIX-1  _strip_installment() removed from repayment matching.
# FIX-2  Test run picks loans that HAVE repayments (not newest disbursements).
# FIX-3  Interest rate "80%/Year" parsed to numeric 80.0 via regex.
# FIX-4  Loan Disbursement submitted for GL entries.
# FIX-5  Loan Repayment submitted for GL entries.
# FIX-6  lia.submit() / plia.submit() replaced with direct docstatus=1 DB set.
#        LIA.on_submit → make_gl_entries() was trying to book UBL Bank
#        (payment account) with party=Customer — invalid for a bank account.
#        The Loan Repayment's own make_gl_entries() handles all GL correctly.
#        Setting docstatus=1 directly makes book_unaccrued_interest() and
#        book_unaccrued_penalty() find the records without triggering GL creation.
# FIX-7  Rollback now cancels and deletes Loan Interest Accruals created during
#        repayment migration (inserted between repayment deletions and loan
#        deletions to respect FK order).
#
# ─────────────────────────────────────────────────────────────────────────────

import re
from collections import defaultdict

import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, nowdate


class DataMigrationTool(Document):

    def validate(self):
        if not self.title:
            self.title = f"Migration {now_datetime().strftime('%Y-%m-%d %H:%M')}"
        if self.status not in ("Draft",):
            return
        for field, label in [
            ("customer_file",     "Customer Data File"),
            ("disbursement_file", "Disbursement Data File"),
            ("repayment_file",    "Repayment Data File"),
        ]:
            if not self.get(field):
                frappe.throw(f"{label} is required before submitting.")

    def on_submit(self):
        """Triggered on Submit button — enqueue background migration job."""
        self.db_set("status", "Queued")
        self.db_set("migration_log", f"[{now_datetime()}] Queued by {frappe.session.user}\n")
        frappe.db.commit()

        frappe.enqueue(
            "lending.loan_management.doctype.data_migration_tool.data_migration_tool.run_migration_job",
            queue="long",
            timeout=7200,
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
        # if self.status == "Running":
        #     frappe.throw("Cannot cancel a running migration. Wait for it to complete.")

        self.db_set("status", "Rollback Queued")
        self.db_set(
            "migration_log",
            (frappe.db.get_value("Data Migration Tool", self.name, "migration_log") or "")
            + f"\n[{now_datetime()}] Rollback queued by {frappe.session.user}\n",
        )
        frappe.db.commit()

        frappe.enqueue(
            "lending.loan_management.doctype.data_migration_tool.data_migration_tool.run_rollback_job",
            queue="long",
            timeout=7200,
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


# ── Background job entry point ────────────────────────────────────────────────

def run_migration_job(doc_name):
    """Entry point called by the background worker."""
    import traceback as tb_mod

    doc = frappe.get_doc("Data Migration Tool", doc_name)
    doc.db_set("status", "Running")
    frappe.db.commit()

    _log(doc, f"[{now_datetime()}] Migration started (Job: DataMigration-{doc_name})")

    errors           = 0
    active_map       = {}
    hist_map         = {}
    cnic_to_customer = {}

    try:
        # ── Read files ────────────────────────────────────────────────
        _log(doc, "\n── Reading Excel files...")
        customer_rows     = _read_excel(doc.customer_file)
        disbursement_rows = _read_excel(doc.disbursement_file)
        repayment_rows    = _read_excel(doc.repayment_file)
        _log(doc,
             f"   Customers: {len(customer_rows):,} | "
             f"Disbursements: {len(disbursement_rows):,} | "
             f"Repayments: {len(repayment_rows):,}")

        # ── FIX-2: Test run — pick loans that HAVE repayments ─────────
        if doc.test_run:
            TEST_LIMIT = 50
            _log(doc, f"\n   ⚠️  TEST RUN — limiting to first {TEST_LIMIT} loans WITH repayments")

            rep_loan_nos = {
                str(r.get("Loan#") or "").strip()
                for r in repayment_rows
                if r.get("Loan#")
            }

            seen_set    = set()
            sliced_disb = []
            for r in disbursement_rows:
                ln = str(r.get("Loan#") or "").strip()
                if not ln:
                    continue
                if ln in rep_loan_nos:
                    if ln not in seen_set:
                        if len(seen_set) >= TEST_LIMIT:
                            break
                        seen_set.add(ln)
                    sliced_disb.append(r)

            disbursement_rows = sliced_disb
            test_loan_set     = seen_set

            test_cnics    = {_cnic(r.get("Borrower CNIC #")) for r in disbursement_rows}
            customer_rows = [r for r in customer_rows if _cnic(r.get("Borrower CNIC #")) in test_cnics]

            repayment_rows = [
                r for r in repayment_rows
                if str(r.get("Loan#") or "").strip() in test_loan_set
            ]

            _log(doc,
                 f"   Sliced — customers: {len(customer_rows)} | "
                 f"disbursements: {len(disbursement_rows)} | "
                 f"repayments: {len(repayment_rows)}")

        disbursement_loan_nos = {str(r.get("Loan#") or "").strip() for r in disbursement_rows}
        existing_map = _build_loan_map_from_db()
        hist_index   = _build_historical_loan_index(repayment_rows, disbursement_loan_nos, existing_map)
        _log(doc, f"   Historical closed loans to create: {len(hist_index):,}")

        # ── Step 1: Customers ─────────────────────────────────────────
        if doc.migrate_customers:
            _log(doc, "\n[1/5] Migrating Customers...")
            cnic_to_customer, created, errs = _migrate_customers(doc, customer_rows)
            doc.db_set("customers_created", created)
            errors += errs
            _log(doc, f"   ✅ Created: {created:,}  Errors: {errs}", flush=True)
        else:
            cnic_to_customer = _load_existing_customers(customer_rows)
            _log(doc, "\n[1/5] Customers skipped (option unchecked)")

        # ── Step 2: Loan Products ─────────────────────────────────────
        _log(doc, "\n[2/5] Ensuring Loan Products...")
        _ensure_loan_products(doc, disbursement_rows)

        # ── Step 3: Active Loans ──────────────────────────────────────
        if doc.migrate_loans:
            _log(doc, "\n[3/5] Migrating Active Loans...")
            active_map, created, errs = _migrate_loans(doc, disbursement_rows, cnic_to_customer)
            doc.db_set("loans_created", created)
            errors += errs
            _log(doc, f"   ✅ Created: {created:,}  Errors: {errs}")
        else:
            active_map = _build_loan_map_from_db()
            _log(doc, "\n[3/5] Active Loans skipped")

        # ── Step 4: Historical Loans ──────────────────────────────────
        if doc.migrate_historical_loans:
            _log(doc, "\n[4/5] Migrating Historical (Closed) Loans...")
            hist_map, created, errs = _migrate_historical_loans(doc, hist_index, cnic_to_customer)
            doc.db_set("historical_loans_created", created)
            errors += errs
            _log(doc, f"   ✅ Created: {created:,}  Errors: {errs}")
        else:
            _log(doc, "\n[4/5] Historical Loans skipped")

        # ── Step 5a: Disbursements ────────────────────────────────────
        if doc.migrate_disbursements:
            _log(doc, "\n[5a] Migrating Loan Disbursements...")
            created, errs = _migrate_disbursements(doc, disbursement_rows, active_map)
            errors += errs
            _log(doc, f"   ✅ Submitted: {created:,}  Errors: {errs}", flush=True)

        # ── Step 5b: Repayments ───────────────────────────────────────
        if doc.migrate_repayments:
            _log(doc, "\n[5b] Migrating Loan Repayments...")
            created, errs, unmatched = _migrate_repayments(
                doc, repayment_rows, disbursement_rows, active_map, hist_map
            )
            doc.db_set("repayments_created", created)
            errors += errs
            _log(doc,
                 f"   ✅ Submitted: {created:,}  Errors: {errs}  "
                 f"Unmatched: {len(unmatched):,}")
            if unmatched:
                _log(doc, f"   Unmatched samples: {sorted(unmatched)[:8]}")

        doc.db_set("errors_count", errors)
        doc.db_set("status", "Completed")
        _log(doc, f"\n[{now_datetime()}] ✅ MIGRATION COMPLETE — Total errors: {errors}")

        # ── Close fully paid migrated loans ───────────────────────────────
        # Scope strictly to migrated loans (description LIKE 'LoanRef:') to
        # avoid accidentally closing any pre-existing loans.
        # 'filters' is the correct frappe.get_all parameter (not 'filter').
        try:
            from lending.loan_management.doctype.loan.loan import close_unsecured_term_loan
            migrated_closure_loans = frappe.db.sql_list(
                "SELECT name FROM `tabLoan` "
                "WHERE description LIKE %s "
                "AND status = 'Loan Closure Requested' "
                "AND docstatus = 1",
                ("%LoanRef:%",),
            )
            closed_count = 0
            for loan_name in migrated_closure_loans:
                try:
                    close_unsecured_term_loan(loan_name)
                    closed_count += 1
                except Exception:
                    pass  # Non-fatal — loan may already be closed or have pending items
            if migrated_closure_loans:
                _log(doc, f"   Closed {closed_count:,} / {len(migrated_closure_loans):,} fully paid loans")
            frappe.db.commit()
        except Exception:
            import traceback as _tb
            _log(doc, f"   ⚠️  close_unsecured_term_loan step failed (non-fatal):\n{_tb.format_exc()}")

    except Exception:
        tb = tb_mod.format_exc()
        _log(doc, f"\n[{now_datetime()}] ❌ MIGRATION FAILED:\n{tb}")
        doc.db_set("status",       "Failed")
        doc.db_set("errors_count", errors)
        frappe.log_error(tb, f"DataMigration-{doc_name}")

    finally:
        frappe.db.commit()


# ── Rollback job ──────────────────────────────────────────────────────────────

def run_rollback_job(doc_name):
    """
    Cancels and deletes every record created by this migration.

    Deletion order (strict reverse of creation, respecting FK constraints):
        1. Loan Repayment          (references Loan + LIA)
        2. Loan Interest Accrual   (references Loan)      ← FIX-7: added
        3. Loan Disbursement       (references Loan)
        4. Loan                    (references Customer, Loan Product)
        5. Customer                (only if migrate_customers was checked)
        6. Loan Product            (only auto-created products)
    """
    import traceback as tb_mod

    doc = frappe.get_doc("Data Migration Tool", doc_name)
    doc.db_set("status", "Rolling Back")
    frappe.db.commit()

    _log(doc, f"\n[{now_datetime()}] ── Rollback started ──")

    total_deleted = 0
    total_errors  = 0

    try:
        _log(doc, "\n[Rollback 1/6] Cancelling & deleting Loan Repayments...")
        deleted, errors = _rollback_repayments(doc)
        total_deleted += deleted
        total_errors  += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        # FIX-7: delete migration LIAs after repayments (repayment_details
        # reference them) but before loans (LIAs reference the loan)
        _log(doc, "\n[Rollback 2/6] Cancelling & deleting Loan Interest Accruals...")
        deleted, errors = _rollback_loan_interest_accruals(doc)
        total_deleted += deleted
        total_errors  += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        _log(doc, "\n[Rollback 3/6] Cancelling & deleting Loan Disbursements...")
        deleted, errors = _rollback_disbursements(doc)
        total_deleted += deleted
        total_errors  += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        _log(doc, "\n[Rollback 4/6] Cancelling & deleting Loans...")
        deleted, errors, _ = _rollback_loans(doc)
        total_deleted += deleted
        total_errors  += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        if doc.migrate_customers:
            _log(doc, "\n[Rollback 5/6] Deleting Customers...")
            deleted, errors = _rollback_customers(doc)
            total_deleted += deleted
            total_errors  += errors
            _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")
        else:
            _log(doc, "\n[Rollback 5/6] Customers skipped (were not migrated)")

        _log(doc, "\n[Rollback 6/6] Deleting auto-created Loan Products...")
        deleted, errors = _rollback_loan_products(doc)
        total_deleted += deleted
        total_errors  += errors
        _log(doc, f"   Deleted: {deleted:,}  Errors: {errors}")

        doc.db_set("status", "Rolled Back")
        _log(doc,
             f"\n[{now_datetime()}] ✅ ROLLBACK COMPLETE — "
             f"Deleted: {total_deleted:,}  Errors: {total_errors}")

    except Exception:
        tb = tb_mod.format_exc()
        _log(doc, f"\n[{now_datetime()}] ❌ ROLLBACK FAILED:\n{tb}")
        doc.db_set("status", "Rollback Failed")
        frappe.log_error(tb, f"DataMigrationRollback-{doc_name}")

    finally:
        frappe.db.commit()


# ── Rollback helpers ──────────────────────────────────────────────────────────

def _cancel_and_delete(doctype, name, doc_name_for_log):
    """
    Cancel a submitted doc (docstatus=1) then delete it permanently.
    Drafts (docstatus=0) are deleted directly. Already-cancelled docs skip cancel.
    Returns True on success, False on any error.
    """
    try:
        d = frappe.get_doc(doctype, name)
        if d.docstatus == 1:
            d.flags.ignore_permissions = True
            d.cancel()
            frappe.db.commit()
        frappe.delete_doc(
            doctype, name,
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


def _migrated_loan_names():
    """Return names of all loans created by any migration (description has LoanRef:)."""
    return frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus != 2",
        ("%LoanRef:%",),
    )


def _rollback_repayments(doc):
    deleted = errors = 0
    loan_names = _migrated_loan_names()
    if not loan_names:
        return deleted, errors

    repayments = frappe.db.sql_list(
        f"SELECT name FROM `tabLoan Repayment` "
        f"WHERE against_loan IN ({','.join(['%s'] * len(loan_names))}) "
        f"AND docstatus < 2 ORDER BY posting_date DESC, name DESC",
        tuple(loan_names),
    )
    _log(doc, f"   Found {len(repayments):,} repayment(s) to delete")

    for name in repayments:
        if _cancel_and_delete("Loan Repayment", name, doc.name):
            deleted += 1
            if deleted % 500 == 0:
                _log(doc, f"   ...{deleted:,} repayments deleted")
        else:
            errors += 1
    return deleted, errors


def _rollback_loan_interest_accruals(doc):
    """
    FIX-7: Delete Loan Interest Accrual records tied to migrated loans.

    Deletion strategy — three categories:

    a) docstatus=0 (draft)
       Never submitted — just delete directly.

    b) docstatus=1, HAS GL entries (normal LIAs or post-GL-fix LIAs)
       Cancel first (reverses GL) then delete.

    c) docstatus=1, NO GL entries (migration LIAs set via direct DB write)
       MUST NOT call cancel() — on_cancel() → make_gl_entries(cancel=1)
       would post phantom REVERSE entries for amounts never booked.
       Set docstatus=2 directly (no hooks) then delete.

    d) docstatus=2 (already cancelled)
       Skip cancel, just delete — cleans up leftover cancelled records
       from partial previous rollback attempts.
       Note: _migrated_loan_names() uses 'docstatus != 2' so loan lookup
       still works; we separately fetch all LIA docstatus values here.
    """
    deleted = errors = 0
    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s",
        ("%LoanRef:%",),
    )
    if not loan_names:
        return deleted, errors

    # Fetch ALL LIA records including already-cancelled (docstatus=2)
    # so we can clean up leftover records from partial rollbacks
    lias = frappe.db.sql(
        f"SELECT name, docstatus FROM `tabLoan Interest Accrual` "
        f"WHERE loan IN ({','.join(['%s'] * len(loan_names))}) "
        f"ORDER BY posting_date DESC, name DESC",
        tuple(loan_names),
        as_dict=True,
    )
    _log(doc, f"   Found {len(lias):,} LIA(s) to delete")

    for lia in lias:
        name     = lia.name
        docstatus = lia.docstatus
        try:
            if docstatus == 1:
                # Check whether this LIA has real GL entries
                has_gl = frappe.db.sql(
                    "SELECT COUNT(*) FROM `tabGL Entry` "
                    "WHERE voucher_type='Loan Interest Accrual' "
                    "AND voucher_no=%s AND is_cancelled=0",
                    (name,),
                )[0][0]

                if has_gl:
                    # Normal cancel — reverses GL entries correctly
                    d = frappe.get_doc("Loan Interest Accrual", name)
                    d.flags.ignore_permissions = True
                    d.cancel()
                    frappe.db.commit()
                else:
                    # Migration LIA — no GL entries, bypass on_cancel()
                    # to avoid posting phantom reverse entries
                    frappe.db.set_value(
                        "Loan Interest Accrual", name, "docstatus", 2,
                        update_modified=False,
                    )
                    frappe.db.commit()

            # Delete (works for docstatus 0, 1-now-cancelled, or 2)
            frappe.delete_doc(
                "Loan Interest Accrual", name,
                force=True, ignore_permissions=True, delete_permanently=True,
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
    loan_names = _migrated_loan_names()
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
    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus != 2 ORDER BY name DESC",
        ("%LoanRef:%",),
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
                "Customer", name,
                force=True, ignore_permissions=True, delete_permanently=True,
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
    product_names = list({
        str(r.get("Loan Product") or "").strip()
        for r in disbursement_rows if r.get("Loan Product")
    })
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
                "Loan Product", name,
                force=True, ignore_permissions=True, delete_permanently=True,
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

# In-memory log buffer — flushed to DB periodically.
# Avoids get_value + set_value + commit on every log call (critical for 8407 rows).
_log_buffer = {}

def _log(doc, msg, flush=False):
    print(msg)
    key = doc.name
    cnt_key = key + "_count"
    if key not in _log_buffer:
        _log_buffer[key] = frappe.db.get_value(
            "Data Migration Tool", doc.name, "migration_log"
        ) or ""
    _log_buffer[key] += msg + "\n"
    _log_buffer[cnt_key] = _log_buffer.get(cnt_key, 0) + 1

    if flush or _log_buffer[cnt_key] % 50 == 0:
        frappe.db.set_value(
            "Data Migration Tool", doc.name, "migration_log",
            _log_buffer[key], update_modified=False,
        )
        frappe.db.commit()


def _read_excel(file_url):
    import openpyxl
    file_doc  = frappe.get_doc("File", {"file_url": file_url})
    file_path = file_doc.get_full_path()
    wb        = openpyxl.load_workbook(file_path, data_only=True)
    ws        = wb.active
    rows      = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    first = str(rows[0][0] or "").strip()
    if len(first) > 15 or any(w in first for w in ["Ltd", "Services", "Admin", "Pvt", "Care"]):
        header_row, data_rows = rows[1], rows[2:]
    else:
        header_row, data_rows = rows[0], rows[1:]
    headers = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(header_row)]
    result  = []
    for row in data_rows:
        if all(v is None for v in row):
            continue
        result.append(dict(zip(headers, row)))
    return result


def _safe_date(val):
    """
    Robust date parser — handles all formats that openpyxl and Excel produce:
      - datetime / date objects  (openpyxl returns these for date cells)
      - "DD/MM/YYYY"             (Pakistani date format in Excel text cells)
      - "YYYY-MM-DD"             (ISO format, frappe standard)
      - "YYYY-MM-DD HH:MM:SS"   (datetime strings)
    Returns "YYYY-MM-DD" string or None.
    """
    if not val:
        return None
    # openpyxl datetime / date objects
    if hasattr(val, "date"):
        return str(val.date())
    s = str(val).strip()
    if not s:
        return None
    # Already ISO format: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    # Pakistani DD/MM/YYYY format
    if "/" in s:
        try:
            from datetime import datetime as _dt
            return str(_dt.strptime(s, "%d/%m/%Y").date())
        except Exception:
            pass
    # Fallback — let frappe handle it
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
        ("%LoanRef:%",), as_dict=True,
    )
    for loan in loans:
        m = re.search(r"LoanRef:([^\s,]+)", loan.description or "")
        if m:
            loan_map[m.group(1).strip()] = loan.name
    return loan_map


def _build_historical_loan_index(repayment_rows, disbursement_loan_nos, existing_loan_map):
    """FIX-1: direct Loan# match — no _strip_installment."""
    known   = set(disbursement_loan_nos) | set(existing_loan_map.keys())
    grouped = defaultdict(lambda: {
        "cnic": None, "total_amount": 0.0, "total_principal": 0.0,
        "earliest_date": None, "latest_date": None,
    })
    for r in repayment_rows:
        loan_no = str(r.get("Loan#") or "").strip()
        if loan_no in known:
            continue
        cnic  = _cnic(r.get("Borrower CNIC #"))
        date  = _safe_date(r.get("Collection Date"))
        entry = grouped[loan_no]
        if not entry["cnic"] and cnic:
            entry["cnic"] = cnic
        entry["total_amount"]    += _safe_float(r.get("Amount"))
        entry["total_principal"] += _safe_float(r.get("Principal Paid"))
        if date:
            if not entry["earliest_date"] or date < entry["earliest_date"]:
                entry["earliest_date"] = date
            if not entry["latest_date"] or date > entry["latest_date"]:
                entry["latest_date"] = date
    return dict(grouped)


# ── Select field maps ─────────────────────────────────────────────────────────

MARITAL_MAP = {
    "married"   : "Married",  "single"    : "Single",   "unmarried" : "Single",
    "un married": "Single",   "divorced"  : "Divorced", "divorce"   : "Divorced",
    "widowed"   : "Widow",    "widow"     : "Widow",    "separated" : "Divorced",
    "other"     : None,
}
RELIGION_MAP = {
    "islam"       : "Islam",         "muslim"    : "Islam",
    "christianity": "Christianity",  "christian" : "Christianity",
    "hinduism"    : "Hindu",         "hindu"     : "Hindu",
    "sikhism"     : "Other",         "sikh"      : "Other",
    "other"       : "Other",
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
    allowed_marital  = _get_allowed("custom_marital_status")
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
                g        = frappe.new_doc("Gender")
                g.gender = gender
                g.insert(ignore_permissions=True)

            c                         = frappe.new_doc("Customer")
            c.customer_name           = name
            c.customer_type           = "Individual"
            c.customer_group          = "Individual"
            c.territory               = "All Territories"
            c.gender                  = gender or None
            c.custom_cnic_no          = cnic
            c.custom_date_of_birth    = _safe_date(r.get("DOB"))
            c.custom_cnic_expiry_date = _safe_date(r.get("CNIC Expiry Date"))
            c.custom_marital_status   = _safe_select(r.get("Marital Status"), MARITAL_MAP, allowed_marital)
            c.custom_religion         = _safe_select(r.get("Religion"),        RELIGION_MAP, allowed_religion)
            c.custom_spouse_name      = str(r.get("Parentage/Spouse Name")     or "").strip() or None
            c.custom_spouse_mobile    = str(r.get("Parentage/Spouse Mobile #") or "").strip() or None
            c.custom_phone_no_1       = str(r.get("Mobile") or "").strip() or None
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
        p    = str(r.get("Loan Product") or "").strip()
        rate = _safe_float(r.get("Interest Rate"))
        if p:
            products.setdefault(p, [])
            if rate:
                products[p].append(rate)

    accts = {
        "disbursement_account":        doc.disbursement_account,
        "payment_account":             doc.payment_account,
        "loan_account":                doc.loan_account,
        "interest_income_account":     doc.interest_income_account,
        "interest_receivable_account": doc.interest_receivable_account,
        "penalty_income_account":      doc.penalty_income_account,
        "penalty_receivable_account":  doc.penalty_receivable_account,
        "mode_of_payment":             doc.mode_of_payment,
    }

    for name, rates in sorted(products.items()):
        avg  = sum(rates) / len(rates) if rates else 0.0
        perd = _guess_periods(name)

        if frappe.db.exists("Loan Product", name):
            if not frappe.db.get_value("Loan Product", name, "disbursement_account"):
                frappe.db.set_value("Loan Product", name, accts, update_modified=False)
                frappe.db.commit()
                _log(doc, f"   Patched accounts: {name}")
            continue

        try:
            p                                          = frappe.new_doc("Loan Product")
            p.product_code                             = name
            p.product_name                             = name
            p.company                                  = doc.company
            p.is_term_loan                             = 1
            p.rate_of_interest                         = avg
            p.repayment_method                         = "Repay Over Number of Periods"
            p.repayment_periods                        = perd
            p.repayment_schedule_type                  = "Monthly as per repayment start date"
            p.min_days_bw_disbursement_first_repayment = 30
            p.min_auto_closure_tolerance_amount        = 0
            p.max_auto_closure_tolerance_amount        = 100
            p.penalty_interest_method                  = "Rate"
            p.penalty_interest_rate                    = 0.0
            p.penalty_stop_threshold_days              = 90
            for k, v in accts.items():
                setattr(p, k, v)
            p.insert(ignore_permissions=True)
            frappe.db.commit()
            _log(doc, f"   Created Loan Product: {name} (rate={avg:.1f}% periods={perd}m)")
        except Exception:
            import traceback
            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} LoanProduct[{name}]",
            )


def _loan_fields(doc, r, product, disb_date, status="Disbursed"):
    prod_data = {}
    if product and frappe.db.exists("Loan Product", product):
        prod_data = frappe.db.get_value(
            "Loan Product", product,
            ["disbursement_account", "payment_account", "loan_account",
             "interest_income_account", "penalty_income_account", "mode_of_payment",
             "rate_of_interest", "penalty_stop_threshold_days",
             "repayment_method", "repayment_periods", "is_term_loan"],
            as_dict=True,
        ) or {}

    raw_rate = _safe_float(r.get("Interest Rate") if r else 0)

    loan                             = frappe.new_doc("Loan")
    loan.applicant_type              = "Customer"
    loan.company                     = doc.company
    loan.branch                      = doc.branch
    loan.loan_product                = product
    loan.posting_date                = disb_date
    loan.disbursement_date           = disb_date
    loan.repayment_start_date        = disb_date
    loan.sales_person                = doc.sales_person
    loan.status                      = status
    loan.rate_of_interest            = prod_data.get("rate_of_interest") or raw_rate
    loan.penalty_stop_threshold_days = prod_data.get("penalty_stop_threshold_days") or 90
    loan.is_term_loan                = prod_data.get("is_term_loan", 1)
    loan.repayment_method            = prod_data.get("repayment_method") or "Repay Over Number of Periods"
    loan.repayment_periods           = prod_data.get("repayment_periods") or _guess_periods(product or "")
    loan.mode_of_payment             = prod_data.get("mode_of_payment") or doc.mode_of_payment
    loan.disbursement_account        = prod_data.get("disbursement_account") or doc.disbursement_account
    loan.payment_account             = prod_data.get("payment_account") or doc.payment_account
    loan.loan_account                = prod_data.get("loan_account") or doc.loan_account
    loan.interest_income_account     = prod_data.get("interest_income_account") or doc.interest_income_account
    loan.penalty_income_account      = prod_data.get("penalty_income_account") or doc.penalty_income_account
    return loan


def _migrate_loans(doc, rows, cnic_map):
    loan_map = {}
    created = errors = 0
    existing = _build_loan_map_from_db()

    for r in rows:
        loan_no  = str(r.get("Loan#") or "").strip()
        cnic     = _cnic(r.get("Borrower CNIC #"))
        customer = cnic_map.get(cnic)
        if not loan_no or not customer:
            continue
        if loan_no in existing:
            loan_map[loan_no] = existing[loan_no]
            continue

        product   = str(r.get("Loan Product") or "").strip() or None
        disb_date = _safe_date(r.get("Disbursed Date")) or nowdate()

        # 10 rows have Disbursed=0 but Principal>0 — fall back to Principal
        amount = _safe_float(r.get("Disbursed")) or _safe_float(r.get("Principal"))
        if not amount:
            frappe.log_error(
                f"Loan {loan_no}: both Disbursed and Principal are 0 — skipped",
                f"DataMigration-{doc.name} Loan[{loan_no}]",
            )
            errors += 1
            continue

        try:
            loan              = _loan_fields(doc, r, product, disb_date, "Disbursed")
            loan.applicant    = customer
            loan.loan_amount      = amount
            loan.disbursed_amount = 0        # Leave 0 — Loan Disbursement sets it
            loan.description      = f"Migrated. LoanRef:{loan_no}"
            loan.insert(ignore_permissions=True)
            loan.submit()
            frappe.db.commit()

            # Set total_interest_payable from the Interest Paid column.
            # get_pending_principal_amount() uses:
            #   pending = total_payment - total_principal_paid - total_interest_payable
            # total_payment = loan_amount + total_interest (from repayment schedule).
            # Without this, pending > 0 even after full repayment → loan never closes.
            interest_paid_so_far = _safe_float(r.get("Interest Paid"))
            principal_paid_so_far = _safe_float(r.get("Principal Paid"))
            # Set total_interest_payable only — NOT total_principal_paid.
            # total_principal_paid must NOT be pre-set here because
            # update_paid_amount() in loan_repayment.on_submit() does:
            #   total_principal_paid += self.principal_amount_paid
            # Starting from 0, it accumulates correctly across all repayments.
            # Pre-setting would cause double-counting for fully paid loans.
            #
            # total_interest_payable IS pre-set because our migration LIAs
            # bypass on_submit (FIX-6), so Frappe never auto-updates this field.
            # It must reflect total accrued interest from the source system.
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

        cnic     = info.get("cnic", "")
        customer = cnic_map.get(cnic) or frappe.db.get_value(
            "Customer", {"custom_cnic_no": cnic}, "name"
        )
        if not customer:
            continue

        amount    = info["total_principal"] or info["total_amount"] or 1.0
        disb_date = info["earliest_date"] or nowdate()

        try:
            loan              = _loan_fields(doc, None, default_product, disb_date, "Closed")
            loan.applicant    = customer
            loan.loan_amount  = amount
            loan.disbursed_amount = amount
            loan.description  = f"Migrated historical. LoanRef:{loan_no}"
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
    Lock timeout retry with exponential back-off (ERROR-2 fix).
    """
    import time
    created   = errors = 0
    MAX_RETRIES = 3

    for r in rows:
        loan_no  = str(r.get("Loan#") or "").strip()
        loan_doc = loan_map.get(loan_no)
        if not loan_doc:
            continue

        if frappe.db.get_value(
            "Loan Disbursement",
            {"against_loan": loan_doc, "docstatus": ["!=", 2]},
            "name",
        ):
            continue

        try:
            lr = frappe.db.get_value(
                "Loan", loan_doc,
                ["applicant_type", "applicant", "company", "branch", "loan_product"],
                as_dict=True,
            ) or {}

            d                   = frappe.new_doc("Loan Disbursement")
            d.against_loan      = loan_doc
            d.disbursement_date = _safe_date(r.get("Disbursed Date")) or nowdate()
            disbursed_amt      = _safe_float(r.get("Disbursed")) or _safe_float(r.get("Principal"))
            deductable_fees    = _safe_float(r.get("Deductable Fees"))
            # Skip rows where Disbursed = 0 — do not create a zero-amount disbursement.
            if not disbursed_amt:
                continue
            # Note: set_missing_values() in validate() will override disbursed_amount
            # to loan_amount (full principal). This is correct per the disbursement
            # GL architecture: DR Loan (full) / CR Bank (net) / CR Charge Income (fees)
            d.disbursed_amount  = disbursed_amt
            d.applicant_type    = lr.get("applicant_type", "Customer")
            d.applicant         = lr.get("applicant")
            d.company           = lr.get("company", doc.company)
            d.branch            = lr.get("branch", doc.branch)
            d.loan_product      = lr.get("loan_product")

            # ── Add deductable fees as disbursement charges ────────────────
            # loan_disbursement.make_gl_entries() uses loan_disbursement_charges
            # to split the bank credit:
            #   DR Loan Account:          disbursed_amount  (full principal)
            #   CR Disbursement Account:  disbursed - fees  (net cash to borrower)
            #   CR Charge Income:         fees              (processing fee income)
            # Without this, bank is overcredited by the fee amount.
            charges_income_account = doc.get("charges_income_account") or doc.interest_income_account
            if deductable_fees > 0 and charges_income_account:
                d.append("loan_disbursement_charges", {
                    "charge"  : None,           # charge type not required for migration
                    "account" : charges_income_account,
                    "amount"  : deductable_fees,
                })

            d.insert(ignore_permissions=True)

            # Commit before submit releases any pending lock on the parent Loan row
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
                        time.sleep(2 ** attempt)
                        frappe.db.rollback()
                        d.reload()
                    else:
                        raise

            if submitted:
                created += 1

        except Exception:
            import traceback
            frappe.log_error(
                traceback.format_exc(),
                f"DataMigration-{doc.name} Disb[{loan_no}]",
            )
            errors += 1

    return created, errors


def _migrate_repayments(doc, rows, disbursement_rows, active_map, hist_map):
    """
    Repayment migration fully aligned to LoanRepayment controller.

    Key fixes:
      BUG-A  ignore_validate=True — Excel amounts preserved, allocate_amounts() skipped.
      BUG-B  Rows sorted oldest-first per loan — avoids check_future_entries() throw.
      BUG-C/D Penalty/interest-only rows handled correctly via ignore_validate.
      BUG-E  Pre-create LIA + set interest_payable=interest_paid → book_unaccrued_interest no-op.
      BUG-F  Pre-create penalty LIA → book_unaccrued_penalty no-op.
      BUG-G  Fees folded into principal (no Sales Invoice available).
      BUG-H  posting_date as "YYYY-MM-DD" not datetime string.
      FIX-6  LIAs set to docstatus=1 via direct DB write — bypasses on_submit →
             make_gl_entries() which would try to post UBL Bank with party=Customer
             (invalid: party only allowed on Receivable/Payable accounts).
             Loan Repayment's own make_gl_entries() handles all GL entries correctly.
    """
    import traceback

    created   = errors = 0
    unmatched = set()

    full_map = _build_loan_map_from_db()
    full_map.update(active_map)
    full_map.update(hist_map)

    # ── Pre-build caches to avoid per-row DB lookups ──────────────────────────
    # cost_center: fetched once per company, reused for all loans in that company
    _cost_center_cache = {}
    def _get_cost_center(company):
        if company not in _cost_center_cache:
            _cost_center_cache[company] = (
                frappe.db.get_value("Company", company, "cost_center")
                or frappe.db.get_value("Cost Center", {"is_group": 0, "company": company}, "name")
            )
        return _cost_center_cache[company]

    # interest_income_account: fetched once per loan_product
    _income_acct_cache = {}
    def _get_income_acct(loan_product):
        if loan_product not in _income_acct_cache:
            _income_acct_cache[loan_product] = (
                frappe.db.get_value("Loan Product", loan_product, "interest_income_account")
                or doc.interest_income_account
            )
        return _income_acct_cache[loan_product]

    # Build source status map from disbursement file.
    # Used to force-close loans whose source status was "Fully Paid" even if
    # the repayment file has incomplete history (e.g. partial 2025-only export).
    # 9 of 2,154 fully paid loans have fewer repayment rows than their full
    # principal — total_principal_paid after migration would be < loan_amount
    # and the standard principal check would never trigger closure for them.
    source_status_map = {}
    for dr in (disbursement_rows or []):
        ln  = str(dr.get("Loan#") or "").strip()
        st  = str(dr.get("Status") or "").strip()
        if ln:
            source_status_map[ln] = st

    fully_paid_loan_nos = set()

    # BUG-B FIX: sort oldest-first per loan
    loan_groups = defaultdict(list)
    for r in rows:
        loan_no_key = str(r.get("Loan#") or "").strip()
        loan_groups[loan_no_key].append(r)

    sorted_rows = []
    for loan_no, reps in loan_groups.items():
        reps.sort(key=lambda x: _safe_date(x.get("Collection Date")) or "9999-99-99")
        sorted_rows.extend(reps)

    existing = set(
        f"{r[0]}_{r[1]}_{r[2]}"
        for r in frappe.db.sql(
            "SELECT against_loan, DATE(posting_date), amount_paid "
            "FROM `tabLoan Repayment` WHERE docstatus < 2"
        )
    )

    for r in sorted_rows:
        loan_no  = str(r.get("Loan#") or "").strip()
        loan_doc = full_map.get(loan_no)
        if loan_doc:
            fully_paid_loan_nos.add(loan_no)   # track all processed loans
        if not loan_doc:
            unmatched.add(loan_no)
            continue

        col_date = r.get("Collection Date")
        date_str = (
            col_date.strftime("%Y-%m-%d")
            if hasattr(col_date, "strftime")
            else _safe_date(col_date)
        )
        # Skip repayment rows with no valid Collection Date — using nowdate()
        # as fallback would post all repayments on today's date which is wrong.
        if not date_str:
            frappe.log_error(
                f"Repayment row skipped — no valid Collection Date  "
                f"Loan[{loan_no}]  Amount[{r.get('Amount')}]",
                f"DataMigration-{doc.name} RepNoDate",
            )
            errors += 1
            continue

        amount         = _safe_float(r.get("Amount"))
        principal_paid = _safe_float(r.get("Principal Paid"))
        interest_paid  = _safe_float(r.get("Interest Paid"))
        penalty_paid   = _safe_float(r.get("Penalty Paid"))
        fees_paid      = _safe_float(r.get("Fees Paid"))

        # BUG-G FIX: fold fees into principal
        if fees_paid > 0:
            principal_paid += fees_paid

        dupe_key = f"{loan_doc}_{date_str}_{amount}"
        if dupe_key in existing:
            continue

        try:
            lr = frappe.db.get_value(
                "Loan", loan_doc,
                ["applicant_type", "applicant", "company", "branch",
                 "loan_product", "is_term_loan",
                 "loan_account", "payment_account",
                 "total_payment", "debit_adjustment_amount",
                 "credit_adjustment_amount", "total_principal_paid",
                 "total_interest_payable", "written_off_amount",
                 "refund_amount", "disbursed_amount", "loan_amount", "status"],
                as_dict=True,
            ) or {}

            company      = lr.get("company") or doc.company
            branch       = lr.get("branch")  or doc.branch
            loan_product = lr.get("loan_product")
            cost_center  = _get_cost_center(company)
            payment_account = lr.get("payment_account") or doc.payment_account
            loan_account    = lr.get("loan_account")    or doc.loan_account
            pending_principal = _get_pending_principal(lr)

            # ── BUG-E FIX: pre-create interest+principal LIA ──────────
            # FIX-6: set docstatus=1 directly — no on_submit → no GL from LIA
            lia_name = None
            if interest_paid > 0 or principal_paid > 0:
                lia = frappe.new_doc("Loan Interest Accrual")
                lia.loan                     = loan_doc
                lia.company                  = company
                lia.branch                   = branch
                lia.cost_center              = cost_center
                lia.posting_date             = date_str
                lia.date_of_accrual          = date_str
                lia.due_date                 = date_str
                lia.interest_amount          = interest_paid
                lia.payable_principal_amount = principal_paid
                lia.pending_principal_amount = 0.0
                lia.accrual_type             = "Repayment"
                # Required for make_gl_entries() to post correct party entries
                # and to find the income account for the CR side of the GL
                lia.applicant_type           = lr.get("applicant_type", "Customer")
                lia.applicant                = lr.get("applicant")
                lia.interest_income_account  = (
                    _get_income_acct(loan_product)
                    or doc.interest_income_account
                )
                lia.insert(ignore_permissions=True)
                # FIX-6: bypass on_submit → make_gl_entries() (wrong account error)
                # docstatus=1 is all that book_unaccrued_interest() and
                # update_paid_amount() need to find this record.
                frappe.db.set_value(
                    "Loan Interest Accrual", lia.name, "docstatus", 1,
                    update_modified=False,
                )
                frappe.db.commit()
                lia_name = lia.name

            # ── BUG-F FIX: pre-create penalty LIA ────────────────────
            # FIX-6: same — docstatus=1 directly, no GL from LIA
            if penalty_paid > 0:
                plia = frappe.new_doc("Loan Interest Accrual")
                plia.loan                     = loan_doc
                plia.company                  = company
                plia.branch                   = branch
                plia.cost_center              = cost_center
                plia.posting_date             = date_str
                plia.date_of_accrual          = date_str
                plia.due_date                 = date_str
                plia.interest_amount          = 0.0
                plia.payable_principal_amount = 0.0
                plia.penalty_amount           = penalty_paid
                plia.pending_principal_amount = 0.0
                plia.accrual_type             = "Penalty"
                # Required for make_gl_entries() party validation
                plia.applicant_type           = lr.get("applicant_type", "Customer")
                plia.applicant                = lr.get("applicant")
                plia.interest_income_account  = (
                    _get_income_acct(loan_product)
                    or doc.interest_income_account
                )
                plia.insert(ignore_permissions=True)
                frappe.db.set_value(
                    "Loan Interest Accrual", plia.name, "docstatus", 1,
                    update_modified=False,
                )
                frappe.db.commit()

            # ── Create Loan Repayment ─────────────────────────────────
            rep = frappe.new_doc("Loan Repayment")
            rep.flags.ignore_validate  = True   # BUG-A FIX
            rep.flags.ignore_mandatory = True

            rep.against_loan             = loan_doc
            rep.posting_date             = date_str   # BUG-H FIX
            rep.amount_paid              = amount
            rep.repayment_type           = "Normal Repayment"
            rep.applicant_type           = lr.get("applicant_type", "Customer")
            rep.applicant                = lr.get("applicant")
            rep.company                  = company
            rep.branch                   = branch
            rep.loan_product             = loan_product
            rep.is_term_loan             = lr.get("is_term_loan", 1)
            rep.cost_center              = cost_center
            rep.payment_account          = payment_account
            rep.loan_account             = loan_account
            rep.principal_amount_paid    = principal_paid
            rep.total_interest_paid      = interest_paid
            rep.total_penalty_paid       = penalty_paid
            rep.interest_payable         = interest_paid   # BUG-E FIX
            rep.payable_principal_amount = principal_paid
            rep.penalty_amount           = penalty_paid    # BUG-C/F FIX
            rep.pending_principal_amount = pending_principal

            if lia_name:
                rep.append("repayment_details", {
                    "loan_interest_accrual": lia_name,
                    "paid_interest_amount":  interest_paid,
                    "paid_principal_amount": principal_paid,
                    "accrual_type":          "Repayment",
                })

            rep.insert(ignore_permissions=True)

            try:
                rep.submit()
                frappe.db.commit()
                existing.add(dupe_key)
                created += 1
                if created % 500 == 0:
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

    # Force-close loans that were fully paid in the source system
    # These won't auto-close because total_interest_payable=0 in migration
    # makes pending_principal = total_payment - total_principal_paid > 0
    if fully_paid_loan_nos:
        _force_close_fully_paid_loans(doc, fully_paid_loan_nos, full_map, source_status_map)

    return created, errors, unmatched


def _force_close_fully_paid_loans(doc, fully_paid_loan_nos, full_map, source_status_map=None):
    closed = errors = 0
    for loan_no in fully_paid_loan_nos:
        loan_doc = full_map.get(loan_no)
        if not loan_doc:
            continue
        try:
            loan_data = frappe.db.get_value(
                "Loan", loan_doc,
                ["loan_amount", "disbursed_amount", "total_principal_paid", "status"],
                as_dict=True,
            )
            if not loan_data:
                continue
            # Already closed — skip
            if loan_data.status in ("Loan Closure Requested", "Closed"):
                continue

            source_status = (source_status_map or {}).get(loan_no, "")
            is_fully_paid_source = (source_status == "Fully Paid")

            # Standard check: all principal repaid via migration repayments
            principal_fully_repaid = (
                flt(loan_data.total_principal_paid) >= flt(loan_data.loan_amount)
            )

            # For fully paid source loans with incomplete repayment history
            # (repayment file only covers 2025 — some loans were partly paid earlier):
            # force total_principal_paid to loan_amount so pending_principal = 0
            if is_fully_paid_source and not principal_fully_repaid:
                frappe.db.set_value(
                    "Loan", loan_doc,
                    {"total_principal_paid": flt(loan_data.loan_amount)},
                    update_modified=False,
                )
                frappe.db.commit()

            elif not is_fully_paid_source and not principal_fully_repaid:
                # Not fully paid in source and principal not covered — skip
                continue

            # Sum actual interest paid from submitted repayments
            totals = frappe.db.sql(
                "SELECT SUM(total_interest_paid) as interest "
                "FROM `tabLoan Repayment` WHERE against_loan=%s AND docstatus=1",
                (loan_doc,), as_dict=True,
            )
            total_paid_interest = flt(totals[0].interest) if totals else 0.0

            # Setting total_interest_payable = total_paid_interest makes
            # get_pending_principal_amount() return 0 (fully settled)
            update = {
                "status"               : "Loan Closure Requested",
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
    is_fully_disbursed = (
        loan.get("status") in ("Disbursed", "Closed")
        or _safe_float(loan.get("disbursed_amount")) >= _safe_float(loan.get("loan_amount"))
    )
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
        "status"                  : doc.status,
        "customers_created"       : doc.customers_created or 0,
        "loans_created"           : doc.loans_created or 0,
        "historical_loans_created": doc.historical_loans_created or 0,
        "repayments_created"      : doc.repayments_created or 0,
        "errors_count"            : doc.errors_count or 0,
        "log"                     : doc.migration_log or "",
    }


@frappe.whitelist()
def validate_migration(doc_name):
    """Pre-flight validation — returns a report dict, touches no data."""
    doc    = frappe.get_doc("Data Migration Tool", doc_name)
    report = {"config_ok": True, "config_errors": []}

    for label, dt, val in [
        ("Company",                 "Company",      doc.company),
        ("Branch",                  "Branch",       doc.branch),
        ("Sales Person",            "Sales Person", doc.sales_person),
        ("Disbursement Account",    "Account",      doc.disbursement_account),
        ("Payment Account",         "Account",      doc.payment_account),
        ("Loan Account",            "Account",      doc.loan_account),
        ("Interest Income Account", "Account",      doc.interest_income_account),
        ("Interest Receivable",     "Account",      doc.interest_receivable_account),
        ("Penalty Income Account",  "Account",      doc.penalty_income_account),
        ("Penalty Receivable",      "Account",      doc.penalty_receivable_account),
    ]:
        if not val:
            report["config_errors"].append(f"{label} is empty")
            report["config_ok"] = False
        elif not frappe.db.exists(dt, val):
            report["config_errors"].append(f"{label}: '{val}' not found")
            report["config_ok"] = False

    # Charges Income Account is optional — falls back to interest_income_account
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
            report["config_errors"].append(f"{field.replace('_',' ').title()} not uploaded")
            report["config_ok"] = False

    if not report["config_ok"]:
        return report

    customer_rows     = _read_excel(doc.customer_file)
    disbursement_rows = _read_excel(doc.disbursement_file)
    repayment_rows    = _read_excel(doc.repayment_file)

    report["row_counts"] = {
        "customers"    : len(customer_rows),
        "disbursements": len(disbursement_rows),
        "repayments"   : len(repayment_rows),
    }

    allowed_marital  = _get_allowed("custom_marital_status")
    allowed_religion = _get_allowed("custom_religion")
    cust_new = cust_existing = cust_skip = 0
    mar_bad  = {}
    rel_bad  = {}
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
        rel = str(r.get("Religion")       or "").strip()
        if mar and _safe_select(mar, MARITAL_MAP, allowed_marital) is None:
            mar_bad[mar] = mar_bad.get(mar, 0) + 1
        if rel and _safe_select(rel, RELIGION_MAP, allowed_religion) is None:
            rel_bad[rel] = rel_bad.get(rel, 0) + 1
    report["customers"] = {
        "will_create"      : cust_new,
        "already_exist"    : cust_existing,
        "invalid"          : cust_skip,
        "marital_unmapped" : mar_bad,
        "religion_unmapped": rel_bad,
    }

    prods = set(
        str(r.get("Loan Product") or "").strip()
        for r in disbursement_rows if r.get("Loan Product")
    )
    report["loan_products"] = {
        "in_excel"    : len(prods),
        "existing"    : len([p for p in prods if frappe.db.exists("Loan Product", p)]),
        "will_create" : sorted(p for p in prods if not frappe.db.exists("Loan Product", p)),
        "need_patch"  : sorted(
            p for p in prods
            if frappe.db.exists("Loan Product", p)
            and not frappe.db.get_value("Loan Product", p, "disbursement_account")
        ),
    }

    existing_map  = _build_loan_map_from_db()
    disb_loan_nos = {str(r.get("Loan#") or "").strip() for r in disbursement_rows}
    all_cnics     = set(_cnic(r.get("Borrower CNIC #")) for r in customer_rows)
    all_cnics    |= set(
        str(row[0]) for row in frappe.db.sql(
            "SELECT custom_cnic_no FROM `tabCustomer` "
            "WHERE custom_cnic_no IS NOT NULL AND custom_cnic_no != ''"
        )
    )
    ln_new = ln_exist = ln_no_cust = 0
    for r in disbursement_rows:
        ln   = str(r.get("Loan#") or "").strip()
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
        "will_create"  : ln_new,
        "already_exist": ln_exist,
        "no_customer"  : ln_no_cust,
    }

    hist_index = _build_historical_loan_index(repayment_rows, disb_loan_nos, existing_map)
    all_ln     = disb_loan_nos | set(existing_map.keys())
    ra = rh = rnc = ru = 0
    for r in repayment_rows:
        ln = str(r.get("Loan#") or "").strip()
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
        "total"                     : len(repayment_rows),
        "matched_active"            : ra,
        "matched_historical"        : rh,
        "historical_loans_to_create": len(hist_index),
        "no_customer"               : rnc,
        "unmatched"                 : ru,
        "total_importable"          : ra + rh,
    }

    report["current_counts"] = {}
    for dt in ["Customer", "Loan Product", "Loan", "Loan Disbursement", "Loan Repayment"]:
        try:
            report["current_counts"][dt] = frappe.db.count(dt)
        except Exception:
            report["current_counts"][dt] = 0

    # ── Financial summary ─────────────────────────────────────────────────────
    # Disbursement totals — from disbursement file
    total_disbursed        = 0.0
    total_deductable_fees  = 0.0
    total_net_cash         = 0.0   # Disbursed - Fees (actual cash to borrower)
    total_interest_expected= 0.0   # Total scheduled interest (Interest column)
    total_outstanding      = 0.0   # Outstanding balance (principal + interest remaining)
    total_principal_balance= 0.0
    total_interest_balance = 0.0
    total_penalty_balance  = 0.0
    total_already_paid     = 0.0   # Paid column (cash already collected)
    total_prin_paid_disb   = 0.0   # Principal already paid
    total_int_paid_disb    = 0.0   # Interest already paid
    total_pen_paid_disb    = 0.0   # Penalty already paid
    count_by_status        = {}

    for r in disbursement_rows:
        disbursed   = _safe_float(r.get("Disbursed")) or _safe_float(r.get("Principal"))
        fees        = _safe_float(r.get("Deductable Fees"))
        interest    = _safe_float(r.get("Interest"))
        outstanding = _safe_float(r.get("Outstanding"))
        paid        = _safe_float(r.get("Paid"))
        prin_paid   = _safe_float(r.get("Principal Paid"))
        int_paid    = _safe_float(r.get("Interest Paid"))
        pen_paid    = _safe_float(r.get("Penalty Paid"))
        prin_bal    = _safe_float(r.get("Principal Balance"))
        int_bal     = _safe_float(r.get("Interest Balance"))
        pen_bal     = _safe_float(r.get("Penalty Balance"))
        status      = str(r.get("Status") or "").strip()

        total_disbursed         += disbursed
        total_deductable_fees   += fees
        total_net_cash          += (disbursed - fees)
        total_interest_expected += interest
        total_outstanding       += outstanding
        total_already_paid      += paid
        total_prin_paid_disb    += prin_paid
        total_int_paid_disb     += int_paid
        total_pen_paid_disb     += pen_paid
        total_principal_balance += prin_bal
        total_interest_balance  += int_bal
        total_penalty_balance   += pen_bal
        count_by_status[status]  = count_by_status.get(status, 0) + 1

    report["financial_summary"] = {
        "disbursement": {
            "total_loans"             : len(disbursement_rows),
            "total_disbursed"         : round(total_disbursed, 2),
            "total_deductable_fees"   : round(total_deductable_fees, 2),
            "total_net_cash_released" : round(total_net_cash, 2),
            "total_interest_expected" : round(total_interest_expected, 2),
            "total_payable_principal_plus_interest": round(total_disbursed + total_interest_expected, 2),
        },
        "already_collected": {
            "total_paid"              : round(total_already_paid, 2),
            "principal_paid"          : round(total_prin_paid_disb, 2),
            "interest_paid"           : round(total_int_paid_disb, 2),
            "penalty_paid"            : round(total_pen_paid_disb, 2),
        },
        "balance_remaining": {
            "total_outstanding"       : round(total_outstanding, 2),
            "principal_balance"       : round(total_principal_balance, 2),
            "interest_balance"        : round(total_interest_balance, 2),
            "penalty_balance"         : round(total_penalty_balance, 2),
        },
        "loans_by_status"             : count_by_status,
    }

    # Repayment file totals — from repayment file (individual transactions)
    rep_total_amount    = 0.0
    rep_principal       = 0.0
    rep_interest        = 0.0
    rep_penalty         = 0.0
    rep_fees            = 0.0
    rep_loan_nos        = set()

    for r in repayment_rows:
        rep_total_amount += _safe_float(r.get("Amount"))
        rep_principal    += _safe_float(r.get("Principal Paid"))
        rep_interest     += _safe_float(r.get("Interest Paid"))
        rep_penalty      += _safe_float(r.get("Penalty Paid"))
        rep_fees         += _safe_float(r.get("Fees Paid"))
        ln = str(r.get("Loan#") or "").strip()
        if ln:
            rep_loan_nos.add(ln)

    # Cross-check: repayment file totals should match disbursement file paid totals
    # (They won't be exact because disbursement is a snapshot while repayments are transactions)
    report["financial_summary"]["repayment_transactions"] = {
        "total_transactions"          : len(repayment_rows),
        "unique_loans"                : len(rep_loan_nos),
        "total_amount_collected"      : round(rep_total_amount, 2),
        "principal_collected"         : round(rep_principal, 2),
        "interest_collected"          : round(rep_interest, 2),
        "penalty_collected"           : round(rep_penalty, 2),
        "fees_collected"              : round(rep_fees, 2),
        "cross_check": {
            "disbursement_file_principal_paid" : round(total_prin_paid_disb, 2),
            "repayment_file_principal_total"   : round(rep_principal, 2),
            "difference"                       : round(abs(total_prin_paid_disb - rep_principal), 2),
            "disbursement_file_interest_paid"  : round(total_int_paid_disb, 2),
            "repayment_file_interest_total"    : round(rep_interest, 2),
            "interest_difference"              : round(abs(total_int_paid_disb - rep_interest), 2),
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
    after migration so that:

        - Loan.total_principal_paid   = SUM(Repayment.principal_amount_paid)
        - Loan.total_interest_paid    = SUM(Repayment.total_interest_paid)
        - Loan.total_penalty_paid     = SUM(Repayment.total_penalty_paid)
        - Loan.total_amount_paid      = SUM(Repayment.amount_paid)
        - Loan.total_interest_payable = SUM(LIA.interest_amount)   ← used by OLP formula
        - LIA.paid_interest_amount    = LIA.interest_amount         ← closure guard needs 0
        - LIA.paid_principal_amount   = LIA.payable_principal_amount
        - Loan.status                 = "Loan Closure Requested" where principal fully paid

    Why each field can diverge after migration
    ------------------------------------------
    DISCREPANCY-1  total_interest_payable was pre-set from the disbursement file
                   snapshot ("Interest Paid" column).  The actual repayments imported
                   may have collected a different total.
                   get_pending_principal_amount() subtracts this → wrong OLP.

    DISCREPANCY-2  total_principal_paid is accumulated by update_paid_amount() +=
                   for each repayment.  If any repayment stayed as draft (submit
                   failed), the running total under-counts.

    DISCREPANCY-3  LIA.paid_interest_amount / paid_principal_amount start at 0 and
                   are incremented by update_paid_amount() SQL +=.  If the repayment
                   interest/principal amounts differ slightly from the LIA amounts,
                   (interest_amount - paid_interest_amount) > 0 → closure guard
                   blocks the loan from closing.

    DISCREPANCY-4  total_amount_paid drifts if any repayment failed mid-submit.

    Parameters
    ----------
    doc_name : str, optional
        If provided, only process loans created by this Data Migration Tool doc.
        If omitted, processes ALL migrated loans (description LIKE 'LoanRef:%').
    dry_run  : int (0/1)
        If 1, compute diffs and return a report without writing anything to DB.

    Returns
    -------
    dict  Summary with counts of loans updated and any errors.
    """
    import traceback

    precision   = frappe.db.get_default("currency_precision") or 2
    precision   = int(precision)
    dry_run     = int(dry_run)

    # ── Identify target loans ─────────────────────────────────────────────────
    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus = 1",
        ("%LoanRef:%",),
    )
    if not loan_names:
        return {"status": "no_loans", "updated": 0, "errors": 0, "details": []}

    updated = errors = skipped = 0
    details = []

    for loan_name in loan_names:
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

    if not dry_run:
        frappe.db.commit()

    return {
        "status"   : "dry_run" if dry_run else "done",
        "total"    : len(loan_names),
        "updated"  : updated,
        "skipped"  : skipped,
        "errors"   : errors,
        "details"  : details[:50],   # cap to avoid huge response
    }


def _reconcile_single_loan(loan_name, precision, dry_run):
    """
    Reconcile one loan.  Returns a dict describing what changed (or would change).
    """
    # ── 1. Ground truth from submitted Loan Repayments ────────────────────────
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
    rep_interest  = flt(rep.get("interest_paid")  or 0, precision)
    rep_penalty   = flt(rep.get("penalty_paid")   or 0, precision)
    rep_amount    = flt(rep.get("amount_paid")    or 0, precision)

    # ── 2. LIA totals for this loan ───────────────────────────────────────────
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
    lia_int_accrued   = flt(lia.get("total_interest_accrued")    or 0, precision)
    lia_prin_accrued  = flt(lia.get("total_principal_accrued")   or 0, precision)
    lia_int_paid      = flt(lia.get("total_interest_paid_lia")   or 0, precision)
    lia_prin_paid     = flt(lia.get("total_principal_paid_lia")  or 0, precision)

    # ── 3. Current values on the Loan record ──────────────────────────────────
    loan_data = frappe.db.get_value(
        "Loan", loan_name,
        ["total_principal_paid", "total_interest_payable",
         "total_amount_paid", "loan_amount", "disbursed_amount",
         "status", "applicant_type",
         "total_interest_paid" if frappe.db.has_column("Loan", "total_interest_paid") else "loan_amount",
         "total_penalty_paid"  if frappe.db.has_column("Loan", "total_penalty_paid")  else "loan_amount"],
        as_dict=True,
    ) or {}

    cur_principal_paid    = flt(loan_data.get("total_principal_paid")    or 0, precision)
    cur_interest_payable  = flt(loan_data.get("total_interest_payable")  or 0, precision)
    cur_amount_paid       = flt(loan_data.get("total_amount_paid")       or 0, precision)
    cur_interest_paid     = flt(loan_data.get("total_interest_paid")     or 0, precision)
    cur_penalty_paid      = flt(loan_data.get("total_penalty_paid")      or 0, precision)
    cur_status            = loan_data.get("status", "")
    loan_amount           = flt(loan_data.get("loan_amount")             or 0, precision)

    # ── 4. Determine correct values ───────────────────────────────────────────
    has_repayments = rep_amount > 0

    # total_interest_payable must only be reconciled for loans that HAVE repayments.
    # For loans with NO repayments (new active loans), total_interest_payable was
    # pre-set from the disbursement file as expected total interest — correct, do not
    # overwrite with 0 just because no LIA rows exist yet.
    # get_pending_principal_amount() uses: total_payment - total_principal_paid
    #                                      - total_interest_payable
    # Setting it to 0 on a new active loan would inflate OLP = total_payment (wrong).
    if has_repayments and lia_int_accrued > 0:
        correct_interest_payable = lia_int_accrued
    else:
        correct_interest_payable = cur_interest_payable   # leave unchanged

    # Loan-level paid amounts come from submitted repayments (authoritative)
    correct_principal_paid   = rep_principal
    correct_interest_paid    = rep_interest
    correct_penalty_paid     = rep_penalty
    correct_amount_paid      = rep_amount

    # ── 5. Determine correct loan status ──────────────────────────────────────
    # Loan should close if:
    #   total_principal_paid >= loan_amount AND
    #   no unpaid LIA interest remaining (after LIA paid_* reconciliation)
    correct_status = cur_status
    if (
        correct_principal_paid >= loan_amount
        and loan_amount > 0
        and cur_status not in ("Loan Closure Requested", "Closed")
    ):
        correct_status = (
            "Closed" if loan_data.get("applicant_type") == "Employee"
            else "Loan Closure Requested"
        )

    # ── 6. Check if anything needs updating ──────────────────────────────────
    loan_update   = {}
    lia_fix_needed = False

    if abs(cur_principal_paid  - correct_principal_paid)   > 0.01:
        loan_update["total_principal_paid"]  = correct_principal_paid
    if abs(cur_interest_payable - correct_interest_payable) > 0.01:
        loan_update["total_interest_payable"] = correct_interest_payable
    if abs(cur_amount_paid      - correct_amount_paid)      > 0.01:
        loan_update["total_amount_paid"]      = correct_amount_paid
    if frappe.db.has_column("Loan", "total_interest_paid"):
        if abs(cur_interest_paid  - correct_interest_paid)  > 0.01:
            loan_update["total_interest_paid"] = correct_interest_paid
    if frappe.db.has_column("Loan", "total_penalty_paid"):
        if abs(cur_penalty_paid   - correct_penalty_paid)    > 0.01:
            loan_update["total_penalty_paid"]   = correct_penalty_paid
    if correct_status != cur_status:
        loan_update["status"] = correct_status

    # LIA paid_* should equal LIA accrued amounts (i.e. fully settled)
    # Closure guard: interest_amount - paid_interest_amount must be 0
    if abs(lia_int_accrued  - lia_int_paid)  > 0.01:
        lia_fix_needed = True
    if abs(lia_prin_accrued - lia_prin_paid) > 0.01:
        lia_fix_needed = True

    changed = bool(loan_update) or lia_fix_needed

    result = {
        "loan"          : loan_name,
        "changed"       : changed,
        "loan_update"   : loan_update,
        "lia_fix_needed": lia_fix_needed,
        "before": {
            "total_principal_paid"  : cur_principal_paid,
            "total_interest_payable": cur_interest_payable,
            "total_interest_paid"   : cur_interest_paid,
            "total_amount_paid"     : cur_amount_paid,
            "status"                : cur_status,
            "lia_int_paid"          : lia_int_paid,
            "lia_prin_paid"         : lia_prin_paid,
        },
        "after": {
            "total_principal_paid"  : correct_principal_paid,
            "total_interest_payable": correct_interest_payable,
            "total_interest_paid"   : correct_interest_paid,
            "total_amount_paid"     : correct_amount_paid,
            "status"                : correct_status,
            "lia_int_paid"          : lia_int_accrued,
            "lia_prin_paid"         : lia_prin_accrued,
        },
    }

    if dry_run or not changed:
        return result

    # ── 7. Apply Loan record updates ──────────────────────────────────────────
    if loan_update:
        frappe.db.set_value("Loan", loan_name, loan_update, update_modified=False)

    # ── 8. Fix LIA paid_* amounts ─────────────────────────────────────────────
    # Set paid_interest_amount = interest_amount and
    # paid_principal_amount = payable_principal_amount for ALL non-penalty LIAs.
    # This marks them as fully settled so the closure guard passes.
    if lia_fix_needed:
        frappe.db.sql(
            """
            UPDATE `tabLoan Interest Accrual`
            SET
                paid_interest_amount   = interest_amount,
                paid_principal_amount  = payable_principal_amount
            WHERE loan = %s
              AND docstatus = 1
              AND accrual_type != 'Penalty'
            """,
            (loan_name,),
        )

    return result



# ── Post-migration repayment status fix ───────────────────────────────────────

@frappe.whitelist()
def fix_repayment_statuses(dry_run=0):
    """
    After migration, repayment_status on all loans is blank or wrong because
    set_repayment_status() was never called during import.

    Gap between CFS source data and Frappe after migration
    -------------------------------------------------------
    CFS "Fully Paid"   (2154) → showing as Past Maturity in Frappe
        Root cause: loan.status was set to 'Loan Closure Requested' but
        set_repayment_status() was never called, so repayment_status stayed blank.
        Fix: call set_repayment_status() — it immediately sets 'Fully Paid'
        for any loan with status in ('Closed', 'Loan Closure Requested').

    CFS "Current"      (438)  → should be Regular in Frappe
        active loans with no past-due schedule rows.

    CFS "Due Today"    (20)   → Regular in Frappe (correct)
        Frappe uses sch_date < current_date (strictly less than), so a payment
        due today is not yet overdue. These will correctly show as Regular.

    CFS "Arrears"      (7)    → Arrears in Frappe
        partial payment or more than 1 past-due schedule row.

    CFS "Past Maturity" (21)  → Past Maturity in Frappe
        current_date > maturity_date.

    CFS "Overdue"      (0)    → Overdue in Frappe (1–29 days late)

    CFS "Missed Repayment" (0) → Missed Repayment (30+ days, 1 full cycle missed)

    CFS "Restructured"  (10)  → calculated by Frappe (no direct equivalent)
        We call set_repayment_status() and let Frappe determine the correct
        status from the actual repayment schedule position.

    CFS "Write-Off"    (1)    → skipped (user instruction)

    Parameters
    ----------
    dry_run : int (0/1)
        If 1, simulate and return counts without writing to DB.

    Returns
    -------
    dict  with before/after counts keyed by status, plus sample changes.
    """
    import traceback

    dry_run    = int(dry_run)
    loan_names = frappe.db.sql_list(
        "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus = 1",
        ("%LoanRef:%",),
    )
    if not loan_names:
        return {"status": "no_loans", "total": 0}

    before_counts = {}
    after_counts  = {}
    updated = errors = skipped = 0
    sample_changes = []

    for loan_name in loan_names:
        try:
            # Fetch current state
            loan_doc = frappe.get_doc("Loan", loan_name)
            old_status = loan_doc.repayment_status or ""
            before_counts[old_status] = before_counts.get(old_status, 0) + 1

            if dry_run:
                new_status = _simulate_repayment_status(loan_doc)
            else:
                loan_doc.set_repayment_status()
                frappe.db.commit()
                new_status = frappe.db.get_value(
                    "Loan", loan_name, "repayment_status"
                ) or ""

            after_counts[new_status] = after_counts.get(new_status, 0) + 1

            if old_status != new_status:
                updated += 1
                if len(sample_changes) < 30:
                    sample_changes.append({
                        "loan"  : loan_name,
                        "before": old_status,
                        "after" : new_status,
                        "loan_status": loan_doc.status,
                    })
            else:
                skipped += 1

        except Exception:
            errors += 1
            frappe.log_error(
                traceback.format_exc(),
                f"FixRepaymentStatus-{loan_name}",
            )

    return {
        "status"        : "dry_run" if dry_run else "done",
        "total"         : len(loan_names),
        "updated"       : updated,
        "skipped"       : skipped,
        "errors"        : errors,
        "before_counts" : dict(sorted(before_counts.items(), key=lambda x: -x[1])),
        "after_counts"  : dict(sorted(after_counts.items(),  key=lambda x: -x[1])),
        "sample_changes": sample_changes,
    }


def _simulate_repayment_status(loan_doc):
    """
    Mirrors the FIXED set_repayment_status() logic for dry_run mode.
    Uses cumulative position-based approach — not LIA due_date grouping.

    The old date-based approach broke for migrated loans because
    LIA.due_date = collection_date ≠ schedule.payment_date, making
    every past-due row look unpaid → Arrears/Past Maturity for everything.

    Fix: use Loan.total_principal_paid and Loan.total_interest_paid
    (authoritative cumulative totals) and walk the schedule in order,
    consuming from the pool to determine which rows are satisfied.
    """
    from frappe.utils import cint, date_diff, flt, getdate

    # ── Fully Paid ────────────────────────────────────────────────────────────
    if loan_doc.status in ("Closed", "Loan Closure Requested"):
        return "Fully Paid"

    precision    = cint(frappe.db.get_default("currency_precision")) or 2
    current_date = getdate()

    # ── Active repayment schedule ─────────────────────────────────────────────
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

    # ── Cumulative paid totals from Loan record ───────────────────────────────
    loan_totals = frappe.db.get_value(
        "Loan", loan_doc.name,
        ["total_principal_paid", "total_interest_paid"],
        as_dict=True,
    ) or {}
    remaining_prin = flt(loan_totals.get("total_principal_paid") or 0, precision)
    remaining_int  = flt(loan_totals.get("total_interest_paid")  or 0, precision)

    # ── Walk schedule: consume from cumulative pool ───────────────────────────
    past_due_rows = 0
    has_partial   = False
    oldest_unpaid = None

    for sch in schedule:
        sch_date = getdate(sch.payment_date)
        sch_prin = flt(sch.principal_amount, precision)
        sch_int  = flt(sch.interest_amount,  precision)

        paid_prin      = min(sch_prin, remaining_prin)
        remaining_prin = flt(remaining_prin - paid_prin, precision)
        paid_int       = min(sch_int, remaining_int)
        remaining_int  = flt(remaining_int - paid_int, precision)

        billed       = flt(sch_prin + sch_int,  precision)
        paid         = flt(paid_prin + paid_int, precision)
        is_fully_paid = paid >= billed

        if sch_date < current_date and not is_fully_paid:
            past_due_rows += 1
            if not oldest_unpaid:
                oldest_unpaid = sch.payment_date
            if paid > 0:
                has_partial = True

    # ── Determine status ──────────────────────────────────────────────────────
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

    # No past-due rows — check Due Today
    remaining_prin_chk = flt(loan_totals.get("total_principal_paid") or 0, precision)
    remaining_int_chk  = flt(loan_totals.get("total_interest_paid")  or 0, precision)
    for sch in schedule:
        sch_date  = getdate(sch.payment_date)
        sch_prin  = flt(sch.principal_amount, precision)
        sch_int   = flt(sch.interest_amount,  precision)
        paid_prin = min(sch_prin, remaining_prin_chk)
        remaining_prin_chk = flt(remaining_prin_chk - paid_prin, precision)
        paid_int  = min(sch_int, remaining_int_chk)
        remaining_int_chk  = flt(remaining_int_chk - paid_int,  precision)
        billed    = flt(sch_prin + sch_int,  precision)
        paid      = flt(paid_prin + paid_int, precision)
        if sch_date == current_date and paid < billed:
            return "Due Today"

    return "Regular"


# ── Post-migration GL correction ──────────────────────────────────────────────

@frappe.whitelist()
def create_missing_accrual_gl_entries(doc_name=None, dry_run=0):
    """
    Fix phantom credit balances on Interest Receivable and Penalty Receivable
    caused by FIX-6 in the migration.

    Root cause
    ----------
    Migration LIAs were set docstatus=1 via direct DB write — on_submit()
    never fired so make_gl_entries() was never called.  The real reason
    on_submit() failed was that the LIA was missing:
      - applicant_type / applicant  → needed for party entry on DR side
      - interest_income_account     → needed for CR side (income account)

    Without these fields, make_gl_entries() would fail with account=None
    on the income entry.

    This function:
      1. Patches each LIA with the missing fields (fetched from the Loan)
      2. Calls lia_doc.make_gl_entries() — the proper LIA GL path that
         sets party_type/party correctly on the receivable account DR entry
      3. Skips LIAs that already have GL entries (idempotent)

    Parameters
    ----------
    dry_run : int (0/1)  If 1, report what would be created without writing.
    """
    import traceback

    dry_run   = int(dry_run)
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

    total_lias     = len(lias)
    created        = skipped = errors = 0
    total_interest = 0.0
    total_penalty  = 0.0
    details        = []

    for lia in lias:
        try:
            interest_amt = flt(lia.interest_amount or 0, precision)
            penalty_amt  = flt(lia.penalty_amount  or 0, precision)

            # ── Skip if GL entries already exist ─────────────────────────────
            existing_gl = frappe.db.sql(
                """SELECT COUNT(*) FROM `tabGL Entry`
                   WHERE voucher_type = 'Loan Interest Accrual'
                     AND voucher_no   = %s AND is_cancelled = 0""",
                (lia.name,),
            )[0][0]

            if existing_gl > 0:
                skipped += 1
                continue

            # ── Resolve missing fields from Loan/Loan Product ─────────────────
            applicant_type      = lia.applicant_type  or lia.loan_applicant_type or "Customer"
            applicant           = lia.applicant       or lia.loan_applicant
            interest_income_acc = (lia.interest_income_account
                                   or lia.loan_interest_income
                                   or lia.lp_interest_income)

            if dry_run:
                if interest_amt > 0:
                    total_interest += interest_amt
                    details.append({
                        "lia"         : lia.name,
                        "loan"        : lia.loan,
                        "posting_date": str(lia.date_of_accrual or lia.posting_date),
                        "type"        : "Interest",
                        "amount"      : interest_amt,
                        "dr_account"  : lia.interest_receivable_account,
                        "cr_account"  : interest_income_acc,
                    })
                if penalty_amt > 0:
                    total_penalty += penalty_amt
                    details.append({
                        "lia"         : lia.name,
                        "loan"        : lia.loan,
                        "posting_date": str(lia.date_of_accrual or lia.posting_date),
                        "type"        : "Penalty",
                        "amount"      : penalty_amt,
                        "dr_account"  : lia.penalty_receivable_account,
                        "cr_account"  : lia.penalty_income_account,
                    })
                created += 1
                continue

            # ── Patch LIA with missing fields then call make_gl_entries() ─────
            # Update DB fields so make_gl_entries() can read them correctly
            patch = {}
            if not lia.applicant_type and applicant_type:
                patch["applicant_type"] = applicant_type
            if not lia.applicant and applicant:
                patch["applicant"] = applicant
            if not lia.interest_income_account and interest_income_acc:
                patch["interest_income_account"] = interest_income_acc

            if patch:
                frappe.db.set_value(
                    "Loan Interest Accrual", lia.name, patch,
                    update_modified=False,
                )

            # Use the LIA controller — correct party handling, get_gl_dict,
            # balanced entry validation, same as a normal LIA submission
            lia_doc = frappe.get_doc("Loan Interest Accrual", lia.name)
            lia_doc.make_gl_entries()
            frappe.db.commit()

            if interest_amt > 0: total_interest += interest_amt
            if penalty_amt  > 0: total_penalty  += penalty_amt
            created += 1

        except Exception:
            errors += 1
            frappe.log_error(traceback.format_exc(), f"MigrationGLFix-{lia.name}")

    return {
        "status"                  : "dry_run" if dry_run else "done",
        "total_lias"              : total_lias,
        "created"                 : created,
        "skipped_existing"        : skipped,
        "errors"                  : errors,
        "total_interest_corrected": round(total_interest, precision),
        "total_penalty_corrected" : round(total_penalty,  precision),
        "total_corrected"         : round(total_interest + total_penalty, precision),
        "details"                 : details[:30],
    }