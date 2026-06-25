import frappe
import re
from collections import defaultdict
from lending.loan_management.doctype.data_migration_tool.data_migration_tool import (
    _read_excel,
    _safe_float,
    _safe_date,
    _insert_lia_raw,
    _insert_repayment_raw,
    _get_pending_principal,
    _bulk_update_loan_totals,
    _ensure_sales_person,
    _cnic,
)


def _get_cc(company):
    return "Main - CFS"


def _get_ia(loan_product):
    return "4130 - Interest Income - CFS"


def validate_and_fix_repayments(branch, repayment_files):
    if isinstance(repayment_files, str):
        repayment_files = [repayment_files]

    if not repayment_files:
        frappe.throw("Pass repayment_files=['/private/files/...xlsx', ...]")

    rep_by_ref = {}
    total_excel_rows = 0

    # Financial accumulators
    excel_totals = {"principal": 0.0, "interest": 0.0, "penalty": 0.0, "amount": 0.0}
    erp_totals = {"principal": 0.0, "interest": 0.0, "penalty": 0.0, "amount": 0.0}

    # ── 1. Read Excel & Accumulate Totals ─────────────────────────────────
    for fpath in repayment_files:
        rows = _read_excel(fpath)
        for r in rows:
            loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
            if not loan_no or loan_no.lower() in ("total", "grand total", "subtotal"):
                continue
            date_str = _safe_date(r.get("Collection Date"))
            if not date_str:
                continue

            rep_by_ref.setdefault(loan_no, []).append(r)
            total_excel_rows += 1

            # Accumulate Excel Totals
            x_prin = _safe_float(r.get("Principal Paid"))
            x_int = _safe_float(r.get("Interest Paid"))
            x_pen = _safe_float(r.get("Penalty Paid"))

            x_amt = _safe_float(r.get("Amount") or r.get("Total Paid Amount"))
            if not x_amt:
                for col in (
                    "Total Paid",
                    "Amount Paid",
                    "Paid Amount",
                    "Total Payment",
                ):
                    x_amt = _safe_float(r.get(col))
                    if x_amt:
                        break
            if not x_amt:
                x_amt = x_prin + x_int + x_pen

            excel_totals["principal"] += x_prin
            excel_totals["interest"] += x_int
            excel_totals["penalty"] += x_pen
            excel_totals["amount"] += x_amt

    for ln in rep_by_ref:
        rep_by_ref[ln].sort(
            key=lambda x: _safe_date(x.get("Collection Date")) or "9999-99-99"
        )

    print(f"Client files     : {len(repayment_files)}")
    print(f"Excel rep rows   : {total_excel_rows:,}")
    print(f"Unique loan refs : {len(rep_by_ref):,}")

    _LOAN_FIELDS = (
        "name, description, applicant_type, applicant, company, branch, loan_product, "
        "is_term_loan, loan_account, payment_account, total_payment, debit_adjustment_amount, "
        "credit_adjustment_amount, total_principal_paid, total_interest_payable, "
        "written_off_amount, refund_amount, disbursed_amount, loan_amount, status"
    )

    loan_rows = frappe.db.sql(
        f"SELECT {_LOAN_FIELDS} FROM `tabLoan` WHERE branch = %s AND docstatus = 1",
        (branch,),
        as_dict=True,
    )

    ref_to_loan = {}
    loan_cache = {}
    for l in loan_rows:
        loan_cache[l.name] = l
        m = re.search(r"LoanRef:([^\s,]+)", l.description or "")
        if m:
            ref_to_loan[m.group(1).strip()] = l.name

    if not ref_to_loan:
        print("No active ERP loans found for this branch")
        return

    loan_names = list(ref_to_loan.values())
    placeholders = ",".join(["%s"] * len(loan_names))

    # ── 2. Read ERP & Accumulate Totals ───────────────────────────────────
    erp_reps = frappe.db.sql(
        f"SELECT name, against_loan, DATE_FORMAT(posting_date,'%%Y-%%m-%%d') AS dt, "
        f"amount_paid, principal_amount_paid, total_interest_paid, total_penalty_paid "
        f"FROM `tabLoan Repayment` "
        f"WHERE against_loan IN ({placeholders}) AND docstatus = 1",
        tuple(loan_names),
        as_dict=True,
    )

    loan_to_ref = {v: k for k, v in ref_to_loan.items()}
    erp_by_ref = {}
    for r in erp_reps:
        ref = loan_to_ref.get(r.against_loan)
        if ref:
            erp_by_ref.setdefault(ref, []).append(
                {
                    "rep_name": r.name,
                    "dt": r.dt,
                    "amount": _safe_float(r.amount_paid),
                    "_matched": False,
                }
            )

            # Accumulate ERP Totals
            erp_totals["principal"] += _safe_float(r.principal_amount_paid)
            erp_totals["interest"] += _safe_float(r.total_interest_paid)
            erp_totals["penalty"] += _safe_float(r.total_penalty_paid)
            erp_totals["amount"] += _safe_float(r.amount_paid)

    total_erp_rows = sum(len(v) for v in erp_by_ref.values())
    print(f"ERP rep rows     : {total_erp_rows:,}\n")

    TOLERANCE = 1.0
    matched = 0
    missing_in_erp = []
    extra_in_erp = []

    all_refs = set(rep_by_ref) | set(erp_by_ref)

    # ── 3. Matching Logic ─────────────────────────────────────────────────
    for ref in sorted(all_refs):
        excel_rows = rep_by_ref.get(ref, [])
        erp_rows = list(erp_by_ref.get(ref, []))

        for xr in excel_rows:
            x_date = _safe_date(xr.get("Collection Date"))

            x_amt = _safe_float(xr.get("Amount") or xr.get("Total Paid Amount"))
            if not x_amt:
                for col in (
                    "Total Paid",
                    "Amount Paid",
                    "Paid Amount",
                    "Total Payment",
                ):
                    x_amt = _safe_float(xr.get(col))
                    if x_amt:
                        break
            if not x_amt:
                x_amt = (
                    _safe_float(xr.get("Principal Paid"))
                    + _safe_float(xr.get("Interest Paid"))
                    + _safe_float(xr.get("Penalty Paid"))
                )

            best = None
            best_diff = float("inf")
            for i, er in enumerate(erp_rows):
                if er["_matched"] or er["dt"] != x_date:
                    continue
                diff = abs(er["amount"] - x_amt)
                if diff <= TOLERANCE and diff < best_diff:
                    best_diff = diff
                    best = i

            if best is not None:
                erp_rows[best]["_matched"] = True
                matched += 1
            else:
                missing_in_erp.append(
                    {
                        "ref": ref,
                        "erp_loan": ref_to_loan.get(ref, "-"),
                        "date": x_date,
                        "amount": x_amt,
                        "principal": _safe_float(xr.get("Principal Paid")),
                        "interest": _safe_float(xr.get("Interest Paid")),
                        "penalty": _safe_float(xr.get("Penalty Paid")),
                        "fees": _safe_float(xr.get("Fees Paid")),
                    }
                )

        for er in erp_rows:
            if not er["_matched"]:
                extra_in_erp.append(
                    {
                        "ref": ref,
                        "erp_loan": ref_to_loan.get(ref, "-"),
                        "rep_name": er["rep_name"],
                        "date": er["dt"],
                        "amount": er["amount"],
                    }
                )

    # ── 4. Print Summary ──────────────────────────────────────────────────
    sep = "─" * 70
    print(sep)
    print(f"  REPAYMENT VALIDATION & CORRECTION — Branch: {branch}")
    print(sep)
    print(f"  Excel rows          : {total_excel_rows:,}")
    print(f"  ERP rows            : {total_erp_rows:,}")
    print(f"  ✅ Matched          : {matched:,}")
    print(f"  🔍 Missing in ERP   : {len(missing_in_erp):,}")
    print(f"  🔍 Extra in ERP     : {len(extra_in_erp):,}")
    print(sep)

    # Financial Totals Block
    print(f"  💰 FINANCIAL TOTALS")
    print(sep)
    print(
        f"  Source   | {'Total Amount':>14} | {'Principal':>14} | {'Interest':>12} | {'Penalty':>10}"
    )
    print(f"  {'-' * 8} | {'-' * 14} | {'-' * 14} | {'-' * 12} | {'-' * 10}")
    print(
        f"  EXCEL    | {excel_totals['amount']:>14,.2f} | {excel_totals['principal']:>14,.2f} | {excel_totals['interest']:>12,.2f} | {excel_totals['penalty']:>10,.2f}"
    )
    print(
        f"  ERP      | {erp_totals['amount']:>14,.2f} | {erp_totals['principal']:>14,.2f} | {erp_totals['interest']:>12,.2f} | {erp_totals['penalty']:>10,.2f}"
    )

    diff_amt = excel_totals["amount"] - erp_totals["amount"]
    diff_prin = excel_totals["principal"] - erp_totals["principal"]
    diff_int = excel_totals["interest"] - erp_totals["interest"]
    diff_pen = excel_totals["penalty"] - erp_totals["penalty"]

    print(f"  {'-' * 8} | {'-' * 14} | {'-' * 14} | {'-' * 12} | {'-' * 10}")
    print(
        f"  DIFF     | {diff_amt:>14,.2f} | {diff_prin:>14,.2f} | {diff_int:>12,.2f} | {diff_pen:>10,.2f}"
    )
    print(sep)

    # ── 5. Auto-Correction Execution ──────────────────────────────────────
    if extra_in_erp:
        print(f"\n[{len(extra_in_erp)}] Deleting Extra / Incorrect ERP Repayments...")
        deleted_count = 0
        for r in extra_in_erp:
            rep_name = r["rep_name"]
            try:
                lia_names = frappe.db.sql_list(
                    "SELECT loan_interest_accrual FROM `tabLoan Repayment Detail` WHERE parent=%s",
                    (rep_name,),
                )

                rep_doc = frappe.get_doc("Loan Repayment", rep_name)
                if rep_doc.docstatus == 1:
                    rep_doc.flags.ignore_permissions = True
                    rep_doc.cancel()
                frappe.delete_doc(
                    "Loan Repayment", rep_name, force=True, ignore_permissions=True
                )

                for lia in lia_names:
                    if lia:
                        lia_doc = frappe.get_doc("Loan Interest Accrual", lia)
                        if lia_doc.docstatus == 1:
                            lia_doc.flags.ignore_permissions = True
                            lia_doc.cancel()
                        frappe.delete_doc(
                            "Loan Interest Accrual",
                            lia,
                            force=True,
                            ignore_permissions=True,
                        )
                deleted_count += 1
            except Exception as e:
                print(f"  ⚠️ Error deleting {rep_name}: {str(e)}")

        frappe.db.commit()
        print(f"  ✅ Successfully cleaned up {deleted_count} ghost repayments.")

    if missing_in_erp:
        print(f"\n[{len(missing_in_erp)}] Inserting Missing / Corrected Repayments...")
        user = frappe.session.user or "Administrator"
        inserted_count = 0
        loan_deltas = defaultdict(
            lambda: {"principal": 0.0, "interest": 0.0, "penalty": 0.0, "amount": 0.0}
        )

        for r in missing_in_erp:
            loan_name = r["erp_loan"]
            if loan_name == "-":
                print(f"  ⚠️ Skip {r['ref']} — ERP Loan not found.")
                continue

            lr = loan_cache.get(loan_name)
            if not lr:
                continue

            company = lr.get("company")
            branch_val = lr.get("branch")
            product = lr.get("loan_product")
            cc = _get_cc(company)
            ia = _get_ia(product)

            date_str = r["date"]
            prin = r["principal"] + r.get("fees", 0.0)
            int_amt = r["interest"]
            pen_amt = r["penalty"]
            amt = r["amount"]

            try:
                lia_name = None
                if int_amt > 0 or prin > 0:
                    lia_name = _insert_lia_raw(
                        loan_name,
                        company,
                        branch_val,
                        cc,
                        date_str,
                        int_amt,
                        prin,
                        0.0,
                        "Repayment",
                        lr.get("applicant_type", "Customer"),
                        lr.get("applicant"),
                        ia,
                        user,
                    )

                if pen_amt > 0:
                    _insert_lia_raw(
                        loan_name,
                        company,
                        branch_val,
                        cc,
                        date_str,
                        0.0,
                        0.0,
                        pen_amt,
                        "Penalty",
                        lr.get("applicant_type", "Customer"),
                        lr.get("applicant"),
                        ia,
                        user,
                    )

                rep_name = f"LR-MIG-{frappe.generate_hash(length=10)}"
                pending_prin = _get_pending_principal(lr)

                _insert_repayment_raw(
                    rep_name,
                    user,
                    loan_name,
                    date_str,
                    amt,
                    lr.get("applicant_type", "Customer"),
                    lr.get("applicant"),
                    company,
                    branch_val,
                    product,
                    lr.get("is_term_loan", 1),
                    cc,
                    None,
                    lr.get("payment_account"),
                    lr.get("loan_account"),
                    prin,
                    int_amt,
                    pen_amt,
                    pending_prin,
                    lia_name,
                )

                lr["total_principal_paid"] = (
                    float(lr.get("total_principal_paid") or 0) + prin
                )

                loan_deltas[loan_name]["principal"] += prin
                loan_deltas[loan_name]["interest"] += int_amt
                loan_deltas[loan_name]["penalty"] += pen_amt
                loan_deltas[loan_name]["amount"] += amt
                inserted_count += 1

            except Exception as e:
                print(f"  ⚠️ Error inserting for {r['ref']}: {str(e)}")

        if loan_deltas:
            _bulk_update_loan_totals(loan_deltas)
            frappe.db.commit()

        print(f"  ✅ Successfully inserted {inserted_count} missing repayments.")

    if not extra_in_erp and not missing_in_erp:
        print("✅ All data perfectly synchronized. No fixes required.")

    print(f"\n{sep}\n")
    return {
        "matched": matched,
        "missing_in_erp": missing_in_erp,
        "extra_in_erp": extra_in_erp,
    }


def run_repayments_corrections():
    repayments_data = {
        "Bhulwal": "/private/files/BWL Repayment till 31st may.xlsx",
        "Jauharabad - JBD-2": "/private/files/JBD2 Repayment till 31st May.xlsx",
        "Jauharabad - JBD-1": "/private/files/JBD Repayment till 31st May.xlsx",
        "Jhanwarian": "/private/files/JWR Repayment till 31st May.xlsx",
        "Khushab - KHB": "/private/files/KHB Repayment till 31st may.xlsx",
        "Lalian": "/private/files/LLN Repayment till 31st May.xlsx",
        "Naushera - NSR": "/private/files/NSR Repayment till 31st may.xlsx",
        "SAM": "/private/files/SAM Repayment till 31st may.xlsx",
        "Shahpur": "/private/files/SHP Repayment till 31st May.xlsx",
        "Sargodha-3": "/private/files/SRG3 Repayment till 31st may.xlsx",
        "Sargodha-2": "/private/files/SRG2 Repayment till 31st may.xlsx",
        "Sargodha-1": "/private/files/SRG 1 Repayment till 31st may.xlsx",
        "Talagang": "/private/files/TLG Repayment till 31st May.xlsx",
    }
    for branch, file in repayments_data.items():
        validate_and_fix_repayments(branch, file)


def validate_and_fix_disbursements(
    branch, disbursement_file, company="Care Financial Services Pvt. Ltd"
):
    print(f"Reading client file: {disbursement_file}...")

    # ── 1. Read Excel File ────────────────────────────────────────────────
    excel_rows = _read_excel(disbursement_file)
    valid_excel_rows = []

    for r in excel_rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if not loan_no or loan_no.lower() in ("total", "grand total", "subtotal"):
            continue
        valid_excel_rows.append(r)

    print(f"Excel rows (valid) : {len(valid_excel_rows):,}")

    # ── 2. Fetch ERP Loans & Disbursements ────────────────────────────────
    erp_loans_raw = frappe.db.sql(
        """
        SELECT name, description, docstatus, loan_amount, status 
        FROM `tabLoan` 
        WHERE branch = %s
        """,
        (branch,),
        as_dict=True,
    )

    # Active-Row-Preferred Map
    erp_map = {}
    for loan in erp_loans_raw:
        m = re.search(r"LoanRef:([^\s,]+)", loan.description or "")
        if not m:
            continue
        ref = m.group(1).strip()
        prev = erp_map.get(ref)

        if prev is None or (int(loan.docstatus) == 1 and int(prev.docstatus) != 1):
            erp_map[ref] = loan

    print(f"ERP loans mapped   : {len(erp_map):,}\n")

    # ── 3. Categorization & Validation ────────────────────────────────────
    TOLERANCE = 1.0
    CANCELLED_STATUSES = {"Denied", "Processing", "RMD Approved", "Pending"}

    matched = 0
    mismatch_amounts = []
    should_be_cancelled = []
    should_not_be_cancelled = []
    missing_in_erp = []
    extra_in_erp = []
    seen_refs = set()

    # Financial Accumulators
    excel_live_amt = 0.0
    excel_cancelled_amt = 0.0
    erp_live_amt = 0.0
    erp_cancelled_amt = 0.0

    # Calculate ERP Totals from mapped loans
    for erp_loan in erp_map.values():
        amt = _safe_float(erp_loan.loan_amount)
        if int(erp_loan.docstatus) == 1:
            erp_live_amt += amt
        elif int(erp_loan.docstatus) == 2:
            erp_cancelled_amt += amt

    # Process Excel Rows & Compare
    for r in valid_excel_rows:
        ref = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        seen_refs.add(ref)

        excel_amt = _safe_float(r.get("Disbursed"))
        if not excel_amt:
            excel_amt = _safe_float(r.get("Outstanding"))

        excel_status = str(r.get("Status") or "").strip()

        # Accumulate Excel Totals
        if excel_status in CANCELLED_STATUSES:
            excel_cancelled_amt += excel_amt
        else:
            excel_live_amt += excel_amt

        erp_loan = erp_map.get(ref)

        if not erp_loan:
            missing_in_erp.append({"ref": ref, "excel_row": r})
            continue

        erp_amt = _safe_float(erp_loan.loan_amount)
        docstatus = int(erp_loan.docstatus)

        # Rule A: Should be cancelled (Excel says denied/pending, ERP says active)
        if excel_status in CANCELLED_STATUSES and docstatus == 1:
            should_be_cancelled.append({"ref": ref, "erp_name": erp_loan.name})
            continue

        # Rule B: Should NOT be cancelled (Excel is live, ERP is cancelled)
        if excel_status not in CANCELLED_STATUSES and docstatus == 2:
            should_not_be_cancelled.append(
                {"ref": ref, "erp_name": erp_loan.name, "excel_row": r}
            )
            continue

        # Rule C: Amount mismatch on live loans
        diff = abs(excel_amt - erp_amt)
        if docstatus == 1 and diff > TOLERANCE:
            mismatch_amounts.append(
                {"ref": ref, "erp_name": erp_loan.name, "excel_amt": excel_amt}
            )
            continue

        matched += 1

    for ref, loan in erp_map.items():
        if ref not in seen_refs and int(loan.docstatus) == 1:
            extra_in_erp.append({"ref": ref, "erp_name": loan.name})

    # ── 4. Print Pre-Action Summary ───────────────────────────────────────
    sep = "─" * 75
    print(sep)
    print(f"  DISBURSEMENT VALIDATION & FIX — Branch: {branch}")
    print(sep)
    print(f"  ✅ OK (Matched)          : {matched:,}")
    print(f"  💰 Amount Mismatches     : {len(mismatch_amounts):,}")
    print(f"  ❌ To Be Cancelled       : {len(should_be_cancelled):,}")
    print(f"  ⚠️ Stubs to Delete       : {len(should_not_be_cancelled):,}")
    print(f"  🔍 Missing (To Create)   : {len(missing_in_erp):,}")
    print(f"  👻 Extras (To Delete)    : {len(extra_in_erp):,}")
    print(sep)

    print(f"  💰 FINANCIAL RECONCILIATION")
    print(sep)
    print(
        f"  Category       | {'Excel Amount':>16} | {'ERP Amount':>16} | {'Difference':>12}"
    )
    print(f"  {'-' * 14} | {'-' * 16} | {'-' * 16} | {'-' * 12}")

    diff_live = excel_live_amt - erp_live_amt
    print(
        f"  Live / Active  | {excel_live_amt:>16,.2f} | {erp_live_amt:>16,.2f} | {diff_live:>12,.2f}"
    )

    diff_canc = excel_cancelled_amt - erp_cancelled_amt
    print(
        f"  Cancelled      | {excel_cancelled_amt:>16,.2f} | {erp_cancelled_amt:>16,.2f} | {diff_canc:>12,.2f}"
    )
    print(sep)

    # ── 5. Auto-Correction Execution ──────────────────────────────────────
    
    # FIX 1: Delete Extra ERP Loans (Ghost loans)
    if extra_in_erp:
        print(f"\n[{len(extra_in_erp)}] Deleting Extra ERP Loans...")
        for e in extra_in_erp:
            _cancel_and_delete_loan(e["erp_name"])
        print("  ✅ Extras deleted.")

    # FIX 2: Cancel Loans that should be Denied/Pending
    if should_be_cancelled:
        print(f"\n[{len(should_be_cancelled)}] Cancelling Denied/Pending Loans...")
        for c in should_be_cancelled:
            _cancel_loan_only(c["erp_name"])
        print("  ✅ Loans cancelled.")

    # FIX 3: Delete Cancelled Stubs that should be Live, then route to recreation
    if should_not_be_cancelled:
        print(
            f"\n[{len(should_not_be_cancelled)}] Deleting dead stubs for recreation..."
        )
        for s in should_not_be_cancelled:
            frappe.delete_doc(
                "Loan", s["erp_name"], force=True, ignore_permissions=True
            )
            missing_in_erp.append(
                {"ref": s["ref"], "excel_row": s["excel_row"]}
            )  # Route to creation
        frappe.db.commit()
        print("  ✅ Stubs deleted and queued for recreation.")

    # FIX 4: Fix Amount Mismatches via SQL
    if mismatch_amounts:
        print(f"\n[{len(mismatch_amounts)}] Fixing Amount Mismatches...")
        for m in mismatch_amounts:
            frappe.db.sql(
                "UPDATE `tabLoan` SET loan_amount=%s WHERE name=%s",
                (m["excel_amt"], m["erp_name"]),
            )
            frappe.db.sql(
                "UPDATE `tabLoan Disbursement` SET disbursed_amount=%s WHERE against_loan=%s",
                (m["excel_amt"], m["erp_name"]),
            )
        frappe.db.commit()
        print("  ✅ Amounts corrected.")

    # FIX 5: Create Missing Loans, Disbursements & Customers
    if missing_in_erp:
        print(f"\n[{len(missing_in_erp)}] Creating Missing Loans & Disbursements...")
        for m in missing_in_erp:
            try:
                _create_missing_loan_package(m["ref"], m["excel_row"], branch, company)
            except Exception as ex:
                print(f"  ⚠️ Error creating {m['ref']}: {str(ex)}")
                frappe.db.rollback()
        print("  ✅ Missing loans created.")

    if (
        not extra_in_erp
        and not should_be_cancelled
        and not should_not_be_cancelled
        and not mismatch_amounts
        and not missing_in_erp
    ):
        print("\n✅ All data perfectly synchronized. No fixes required.")

    print(f"\n{sep}\n")



def _cancel_and_delete_loan(loan_name):
    disbs = frappe.db.sql_list(
        "SELECT name FROM `tabLoan Disbursement` WHERE against_loan=%s", (loan_name,)
    )
    for d in disbs:
        doc = frappe.get_doc("Loan Disbursement", d)
        if doc.docstatus == 1:
            doc.flags.ignore_permissions = True
            doc.cancel()
        frappe.delete_doc("Loan Disbursement", d, force=True, ignore_permissions=True)

    loan = frappe.get_doc("Loan", loan_name)
    if loan.docstatus == 1:
        loan.flags.ignore_permissions = True
        loan.cancel()
    frappe.delete_doc("Loan", loan_name, force=True, ignore_permissions=True)
    frappe.db.commit()


def _cancel_loan_only(loan_name):
    disbs = frappe.db.sql_list(
        "SELECT name FROM `tabLoan Disbursement` WHERE against_loan=%s AND docstatus=1",
        (loan_name,),
    )
    for d in disbs:
        doc = frappe.get_doc("Loan Disbursement", d)
        doc.flags.ignore_permissions = True
        doc.cancel()

    loan = frappe.get_doc("Loan", loan_name)
    if loan.docstatus == 1:
        loan.flags.ignore_permissions = True
        loan.cancel()
    frappe.db.commit()


def _create_missing_loan_package(ref, row, branch, company):
    raw_cnic = str(row.get("Borrower CNIC #") or "").strip()
    norm_cnic = _cnic(raw_cnic)
    customer_name = str(row.get("Name") or row.get("Full Name") or "Unknown").strip()

    customer_doc_name = frappe.db.get_value(
        "Customer", {"custom_cnic_no": norm_cnic}, "name"
    )
    if not customer_doc_name:
        c = frappe.new_doc("Customer")
        c.customer_name = customer_name
        c.customer_group = "Individual"
        c.customer_type = "Individual"
        c.territory = "All Territories"
        c.custom_cnic_no = norm_cnic
        c.insert(ignore_permissions=True)
        frappe.db.commit()
        customer_doc_name = c.name

    loan_product = str(row.get("Loan Product") or "Standard Loan").strip()
    disbursed_date = _safe_date(row.get("Disbursed Date")) or frappe.utils.nowdate()
    loan_amount = _safe_float(row.get("Disbursed"))
    if not loan_amount:
        loan_amount = _safe_float(row.get("Outstanding"))
    fee_amount = _safe_float(row.get("Deductable Fees"))
    officer_name = str(row.get("LoanOfficer") or row.get("Loan Officer") or "").strip()
    sp = _ensure_sales_person(officer_name) if officer_name else None

    lp = frappe.get_doc("Loan Product", loan_product)

    loan = frappe.new_doc("Loan")
    loan.applicant_type = "Customer"
    loan.applicant = customer_doc_name
    loan.company = company
    loan.branch = branch
    loan.loan_product = loan_product
    loan.loan_amount = loan_amount
    loan.posting_date = disbursed_date
    loan.disbursement_date = disbursed_date
    loan.repayment_start_date = disbursed_date
    loan.is_term_loan = lp.is_term_loan
    loan.repayment_method = lp.repayment_method
    loan.mode_of_payment = lp.mode_of_payment
    loan.payment_account = lp.payment_account
    loan.loan_account = lp.loan_account
    loan.interest_income_account = lp.interest_income_account
    loan.penalty_income_account = lp.penalty_income_account
    if sp:
        loan.sales_person = sp
    loan.description = f"Migrated auto-recovery. LoanRef:{ref}"

    loan.insert(ignore_permissions=True)
    loan.submit()
    frappe.db.commit()

    disb = frappe.new_doc("Loan Disbursement")
    disb.against_loan = loan.name
    disb.disbursement_date = disbursed_date
    disb.disbursed_amount = loan_amount
    disb.applicant_type = "Customer"
    disb.applicant = customer_doc_name
    disb.company = company
    disb.branch = branch
    disb.loan_product = loan_product

    if fee_amount > 0 and lp.interest_income_account:
        disb.append(
            "loan_disbursement_charges",
            {
                "charge": None,
                "account": lp.interest_income_account,
                "amount": fee_amount,
            },
        )

    disb.insert(ignore_permissions=True)
    disb.submit()
    frappe.db.commit()


def run_disbursements_corrections():
    disbursement_data = {
        "Lalian": "/private/files/LLN till 31st May.xlsx",
        "SAM": "/private/files/SAM till 31st May.xlsx",
        "Sargodha-3": "/private/files/SRG 3 till 31st may.xlsx",
        "Jhanwarian": "/private/files/JWR  till 31st may.xlsx",
        "Bhulwal": "/private/files/BWL till 31st may.xlsx",
        "Sargodha-1": "/private/files/SRG 1 31 May4383d2.xlsx",
        "Sargodha-2": "/private/files/SRG 2 till 31st may.xlsx",
        "Jauharabad - JBD-2": "/private/files/Jbd 2 Branch Disbursement Report start date to 31 Mayf832fa.xlsx",
        "Jauharabad - JBD-1": "/private/files/Jauharabad 1 Branch Disbursement Report start date to 31 May.xlsx",
        "Shahpur": "/private/files/shahpur 31st may.xlsx",
        "Talagang": "/private/files/Talagang  Branch Disbursement Report start date to 31 May.xlsx",
        "Naushera - NSR": "/private/files/Naushera Branch Disbursement Report start date to 31 May.xlsx",
        "Khushab - KHB": "/private/files/khusab disbursements 2023 to till 31 May 2026 (3).xlsx"
    }

    for branch, file in disbursement_data.items():
        validate_and_fix_disbursements(branch, file)
