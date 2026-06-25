import frappe
import re
from lending.loan_management.doctype.data_migration_tool.data_migration_tool import (
    _read_excel,
    _safe_float,
)
from frappe.utils import flt


def validate_disbursement_amounts(
    branch="Jauharabad - JBD-2",
    disbursement_file="/private/files/Jbd 2 Branch Disbursement Report start date to 31 May.xlsx",
):
    CANCEL_STATUSES = {"Denied", "Processing", "RMD Approved"}

    if not disbursement_file:
        frappe.throw("Pass disbursement_file=/files/<client file>.xlsx")

    excel_rows = _read_excel(disbursement_file)
    if not excel_rows:
        frappe.throw("Disbursement file is empty or could not be read.")

    valid_rows = []
    for r in excel_rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if not loan_no or loan_no.lower() in ("total", "grand total", "subtotal"):
            continue
        valid_rows.append(r)

    erp_loans_raw = frappe.db.sql(
        """
        SELECT name, description, loan_amount, disbursed_amount, status, docstatus
        FROM `tabLoan`
        WHERE branch = %s
        """,
        (branch,),
        as_dict=True,
    )

    # active-row-preferred map (so a cancelled stub never shadows a live loan)
    erp_map = {}
    for loan in erp_loans_raw:
        m = re.search(r"LoanRef:([^\s,]+)", loan.description or "")
        if not m:
            continue
        ref = m.group(1).strip()
        prev = erp_map.get(ref)
        if prev is None or (int(loan.docstatus) == 1 and int(prev.docstatus) != 1):
            erp_map[ref] = loan

    precision = int(frappe.db.get_default("currency_precision") or 2)

    rows_ok = []
    amount_mismatch = []
    should_be_cancelled = []
    should_not_be_cancelled = []
    missing_in_erp = []

    matched_refs = set()
    # amount-diff accumulators, split by ERP docstatus
    diff_submitted = 0.0
    diff_cancelled = 0.0

    for r in valid_rows:
        loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        excel_status = str(r.get("Status") or "").strip()
        excel_disbursed = _safe_float(r.get("Disbursed"))
        borrower_name = str(r.get("Name") or "").strip()

        erp = erp_map.get(loan_no)

        if not erp:
            missing_in_erp.append(
                {
                    "loan_no": loan_no,
                    "borrower_name": borrower_name,
                    "excel_status": excel_status,
                    "excel_disbursed": excel_disbursed,
                }
            )
            continue

        matched_refs.add(loan_no)

        erp_loan_amount = flt(erp.get("loan_amount") or 0, precision)
        erp_docstatus = int(erp.get("docstatus") or 0)
        erp_status = str(erp.get("status") or "")
        erp_cancelled = erp_docstatus == 2
        should_cancel = excel_status in CANCEL_STATUSES
        amount_diff = round(erp_loan_amount - excel_disbursed, precision)
        amount_ok = abs(amount_diff) <= 1.0

        # accumulate amount diff, split by ERP status
        if erp_cancelled:
            diff_cancelled += amount_diff
        else:
            diff_submitted += amount_diff

        base = {
            "loan_no": loan_no,
            "borrower_name": borrower_name,
            "erp_loan": erp.get("name"),
            "excel_status": excel_status,
            "erp_status": erp_status,
            "erp_docstatus": erp_docstatus,
            "excel_disbursed": excel_disbursed,
            "erp_loan_amount": erp_loan_amount,
            "amount_diff": amount_diff,
        }

        issues = []

        if should_cancel and not erp_cancelled:
            issues.append(
                f"Should be cancelled (Excel='{excel_status}') but ERP docstatus={erp_docstatus}"
            )
            should_be_cancelled.append({**base, "issue": issues[-1]})

        elif not should_cancel and erp_cancelled:
            issues.append(
                f"ERP is cancelled but Excel status='{excel_status}' does not require cancellation"
            )
            should_not_be_cancelled.append({**base, "issue": issues[-1]})

        if not erp_cancelled and not amount_ok:
            issues.append(
                f"Amount mismatch — Excel={excel_disbursed} ERP={erp_loan_amount} diff={amount_diff}"
            )
            amount_mismatch.append({**base, "issue": issues[-1]})

        if not issues:
            rows_ok.append(base)

    # extra in ERP — refs present in ERP but not matched to any Excel row
    extra_in_erp = []
    for ref, loan in erp_map.items():
        if ref not in matched_refs:
            extra_in_erp.append(
                {
                    "loan_no": ref,
                    "erp_loan": loan.get("name"),
                    "erp_status": str(loan.get("status") or ""),
                    "erp_docstatus": int(loan.get("docstatus") or 0),
                    "erp_loan_amount": flt(loan.get("loan_amount") or 0, precision),
                }
            )

    sep = "─" * 70

    print(f"\n{sep}")
    print(f"  DISBURSEMENT VALIDATION — Branch: {branch}")
    print(sep)
    print(f"  Excel rows (valid)         : {len(valid_rows):,}")
    print(f"  ERP loans (this branch)    : {len(erp_map):,}")
    print(f"  ✅ OK                      : {len(rows_ok):,}")
    print(f"  ❌ Amount mismatch         : {len(amount_mismatch):,}")
    print(f"  ⚠️  Should be cancelled    : {len(should_be_cancelled):,}")
    print(f"  ⚠️  Should NOT be cancelled: {len(should_not_be_cancelled):,}")
    print(f"  🔍 Missing in ERP (in Excel, not ERP) : {len(missing_in_erp):,}")
    print(f"  🔍 Extra in ERP (in ERP, not Excel)   : {len(extra_in_erp):,}")
    print(sep)
    print(f"  Amount diff — submitted loans (ERP - Excel): {diff_submitted:>+15,.2f}")
    print(f"  Amount diff — cancelled loans (ERP - Excel): {diff_cancelled:>+15,.2f}")
    print(
        f"  Amount diff — TOTAL                        : {diff_submitted + diff_cancelled:>+15,.2f}"
    )
    print(sep)

    if amount_mismatch:
        print(f"\n{'─' * 30} AMOUNT MISMATCH ({len(amount_mismatch)}) {'─' * 30}")
        for row in amount_mismatch:
            print(
                f"  {row['loan_no']:<30} {row['borrower_name']:<25} "
                f"Excel={row['excel_disbursed']:>12,.2f}  ERP={row['erp_loan_amount']:>12,.2f}  "
                f"Diff={row['amount_diff']:>10,.2f}  ERP_Status={row['erp_status']}"
            )

    if should_be_cancelled:
        print(
            f"\n{'─' * 30} SHOULD BE CANCELLED ({len(should_be_cancelled)}) {'─' * 30}"
        )
        for row in should_be_cancelled:
            print(
                f"  {row['loan_no']:<30} {row['borrower_name']:<25} "
                f"Excel='{row['excel_status']}'  ERP={row['erp_loan']}  ERP_Status={row['erp_status']}  docstatus={row['erp_docstatus']}"
            )

    if should_not_be_cancelled:
        print(
            f"\n{'─' * 30} SHOULD NOT BE CANCELLED ({len(should_not_be_cancelled)}) {'─' * 30}"
        )
        for row in should_not_be_cancelled:
            print(
                f"  {row['loan_no']:<30} {row['borrower_name']:<25} "
                f"Excel='{row['excel_status']}'  ERP={row['erp_loan']}  docstatus={row['erp_docstatus']}"
            )

    if missing_in_erp:
        print(f"\n{'─' * 30} MISSING IN ERP ({len(missing_in_erp)}) {'─' * 30}")
        for row in missing_in_erp:
            print(
                f"  {row['loan_no']:<30} {row['borrower_name']:<25} "
                f"Excel='{row['excel_status']}'  Excel_Disbursed={row['excel_disbursed']:>12,.2f}"
            )

    if extra_in_erp:
        print(f"\n{'─' * 30} EXTRA IN ERP ({len(extra_in_erp)}) {'─' * 30}")
        for row in extra_in_erp:
            print(
                f"  {row['loan_no']:<30} ERP={row['erp_loan']}  "
                f"ERP_Status={row['erp_status']}  docstatus={row['erp_docstatus']}  "
                f"Amount={row['erp_loan_amount']:>12,.2f}"
            )

    print(f"\n{sep}\n")
    res = {
        "summary": {
            "excel_total": len(valid_rows),
            "erp_total": len(erp_map),
            "ok": len(rows_ok),
            "amount_mismatch": len(amount_mismatch),
            "should_be_cancelled": len(should_be_cancelled),
            "should_not_be_cancelled": len(should_not_be_cancelled),
            "missing_in_erp": len(missing_in_erp),
            "extra_in_erp": len(extra_in_erp),
            "diff_submitted": round(diff_submitted, precision),
            "diff_cancelled": round(diff_cancelled, precision),
            "diff_total": round(diff_submitted + diff_cancelled, precision),
        },
        "amount_mismatch": amount_mismatch,
        "should_be_cancelled": should_be_cancelled,
        "should_not_be_cancelled": should_not_be_cancelled,
        "missing_in_erp": missing_in_erp,
        "extra_in_erp": extra_in_erp,
    }
    frappe.log_error("Disbursement Validation", str(res))
    return res
