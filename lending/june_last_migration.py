import frappe
from lending.loan_management.doctype.data_migration_tool.data_migration_tool import (
    _read_excel,
    _safe_date,
    _safe_float,
    _cnic,
    _ensure_sales_person,
    _insert_lia_raw,
    _insert_repayment_raw,
    _get_pending_principal,
    _bulk_update_loan_totals,
)
from lending.correct_repayments import _get_cc, _get_ia
from lending.loan_management.doctype.data_migration_tool.data_migration_tool import (
    reconcile_loan_paid_amounts,
    fix_repayment_statuses,
)
from collections import defaultdict
from frappe.utils import flt, nowdate
import re

EXCEL_RAM_CACHE = {}


def get_cached_excel(file_path):
    if file_path not in EXCEL_RAM_CACHE:
        print(f"   ⏳ Reading {file_path.split('/')[-1]} into RAM...")
        EXCEL_RAM_CACHE[file_path] = _read_excel(file_path)
    return EXCEL_RAM_CACHE[file_path]


MARITAL_MAP = {
    "married": "Married",
    "single": "Single",
    "unmarried": "Single",
    "un married": "Single",
    "divorced": "Divorced",
    "widowed": "Widow",
    "widow": "Widow",
}
RELIGION_MAP = {
    "islam": "Islam",
    "muslim": "Islam",
    "christianity": "Christianity",
    "hinduism": "Hindu",
    "sikhism": "Other",
}


def _get_allowed(fieldname):
    try:
        meta = frappe.get_meta("Customer")
        for f in meta.fields:
            if f.fieldname == fieldname and f.options:
                return set(o.strip() for o in f.options.split("\n") if o.strip())
    except:
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


# ── 1. IMPORT BORROWERS ───────────────────────────────────────────────────────


def import_borrowers(branch, file_url):
    rows = get_cached_excel(file_url)
    allowed_marital = _get_allowed("custom_marital_status")
    allowed_rel = _get_allowed("custom_religion")
    created = 0

    for r in rows:
        name = str(r.get("Full Name") or "").strip()
        cnic = _cnic(r.get("Borrower CNIC #"))
        if not name or not cnic or cnic == "0":
            continue

        status_raw = str(r.get("Status") or "").strip().lower()
        if status_raw in ("denied", "processing", "rmd approved"):
            continue

        if frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name"):
            continue

        resolved_branch = branch


        gender = str(r.get("Gender") or "").strip()
        if gender and not frappe.db.exists("Gender", gender):
            frappe.get_doc({"doctype": "Gender", "gender": gender}).insert(
                ignore_permissions=True
            )

        loan_officer = str(r.get("Loan Officer") or "").strip()
        sp = _ensure_sales_person(loan_officer) if loan_officer else None

        c = frappe.new_doc("Customer")
        c.customer_name = name
        c.customer_type = "Individual"
        c.customer_group = "Individual"
        c.territory = "All Territories"
        c.gender = gender or None
        c.custom_cnic_no = cnic
        c.custom_spouse_name = "-"
        c.custom_spouse_mobile = "-"
        c.custom_father_name = "-"
        c.custom_father_mobile = "-"
        c.custom_date_of_birth = _safe_date(r.get("DOB"))
        c.custom_cnic_expiry_date = _safe_date(r.get("CNIC Expiry Date"))
        c.custom_marital_status = _safe_select(
            r.get("Marital Status"), MARITAL_MAP, allowed_marital
        )
        c.custom_religion = _safe_select(r.get("Religion"), RELIGION_MAP, allowed_rel)
        c.custom_phone_no_1 = str(r.get("Mobile") or "").strip() or None
        c.branch = resolved_branch
        if sp:
            c.sales_person = sp

        c.insert(ignore_permissions=True)
        created += 1

    frappe.db.commit()
    return created


# ── 2. IMPORT DISBURSEMENTS ───────────────────────────────────────────────────


def import_disbursements(branch, file_url, company="Care Financial Services Pvt. Ltd"):
    rows = get_cached_excel(file_url)
    created = 0

    SKIP_STATUSES = {"denied", "processing", "rmd approved"}

    for r in rows:
        ref = str(r.get("Loan#") or "").strip()
        if not ref or ref.lower() in ("total", "grand total", "subtotal"):
            continue

        status_raw = str(r.get("Status") or "").strip().lower()
        if status_raw in SKIP_STATUSES:
            continue

        # Skip if already exists
        if frappe.db.sql(
            "SELECT name FROM `tabLoan` WHERE description LIKE %s AND docstatus=1",
            (f"%LoanRef:{ref}%",),
        ):
            continue

        cnic = _cnic(r.get("Borrower CNIC #"))
        customer_doc = frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name")
        if not customer_doc:
            print(f"  ⚠️  Customer not found for CNIC {cnic} (ref {ref}) — skipping")
            continue

        loan_product = str(r.get("Loan Product") or "Standard Loan").strip()
        disbursed_date = _safe_date(r.get("Disbursed Date")) or nowdate()
        loan_amount = _safe_float(r.get("Disbursed"))
        fee_amount = _safe_float(r.get("Deductable Fees"))

        officer_name = str(r.get("LoanOfficer") or r.get("Loan Officer") or "").strip()
        sp = _ensure_sales_person(officer_name) if officer_name else None
        lp = frappe.get_doc("Loan Product", loan_product)

        loan = frappe.new_doc("Loan")
        loan.applicant_type = "Customer"
        loan.applicant = customer_doc
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
        loan.description = f"Migrated Delta. LoanRef:{ref}"

        loan.insert(ignore_permissions=True)
        loan.submit()

        disb = frappe.new_doc("Loan Disbursement")
        disb.against_loan = loan.name
        disb.disbursement_date = disbursed_date
        disb.disbursed_amount = loan_amount
        disb.applicant_type = "Customer"
        disb.applicant = customer_doc
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

        created += 1

    frappe.db.commit()
    return created


# ── 3. IMPORT REPAYMENTS ──────────────────────────────────────────────────────


def import_repayments(branch, file_url, company="Care Financial Services Pvt. Ltd"):
    rows = get_cached_excel(file_url)
    user = frappe.session.user or "Administrator"
    created = 0

    loans_db = frappe.db.sql(
        "SELECT name, description, applicant_type, applicant, loan_product, "
        "is_term_loan, payment_account, loan_account, total_payment, "
        "debit_adjustment_amount, credit_adjustment_amount, "
        "total_principal_paid, total_interest_payable, written_off_amount, "
        "refund_amount, disbursed_amount, loan_amount "
        "FROM `tabLoan` WHERE branch=%s AND docstatus=1",
        (branch,),
        as_dict=True,
    )

    ref_map = {}
    for l in loans_db:
        m = re.search(r"LoanRef:([^\s,]+)", l.description or "")
        if m:
            ref_map[m.group(1).strip()] = l

    loan_deltas = defaultdict(
        lambda: {"principal": 0.0, "interest": 0.0, "penalty": 0.0, "amount": 0.0}
    )

    for r in rows:
        ref = str(r.get("Loan#") or "").strip()
        col_date = _safe_date(r.get("Collection Date"))
        if not ref or not col_date:
            continue

        lr = ref_map.get(ref)
        if not lr:
            print(f"  ⚠️  Loan not found in ERP for ref {ref} — skipping")
            continue

        x_prin = _safe_float(r.get("Principal Paid"))
        x_int = _safe_float(r.get("Interest Paid"))
        x_pen = _safe_float(r.get("Penalty Paid"))
        x_fees = _safe_float(r.get("Fees Paid"))
        if x_fees > 0:
            x_prin += x_fees

        x_amt = (
            _safe_float(r.get("Amount"))
            or _safe_float(r.get("Total Paid Amount"))
            or (x_prin + x_int + x_pen)
        )

        # Dedup check
        dupe = frappe.db.sql(
            "SELECT name FROM `tabLoan Repayment` "
            "WHERE against_loan=%s AND DATE(posting_date)=%s "
            "AND ABS(amount_paid - %s) < 0.1",
            (lr.name, col_date, x_amt),
        )
        if dupe:
            continue

        cc = _get_cc(company)
        ia = _get_ia(lr.loan_product)

        lia_name = None
        if x_int > 0 or x_prin > 0:
            lia_name = _insert_lia_raw(
                lr.name,
                company,
                branch,
                cc,
                col_date,
                x_int,
                x_prin,
                0.0,
                "Repayment",
                lr.applicant_type,
                lr.applicant,
                ia,
                user,
            )
        if x_pen > 0:
            _insert_lia_raw(
                lr.name,
                company,
                branch,
                cc,
                col_date,
                0.0,
                0.0,
                x_pen,
                "Penalty",
                lr.applicant_type,
                lr.applicant,
                ia,
                user,
            )

        rep_name = f"LR-MIG-{frappe.generate_hash(length=10)}"
        pending_prin = _get_pending_principal(lr)

        _insert_repayment_raw(
            rep_name,
            user,
            lr.name,
            col_date,
            x_amt,
            lr.applicant_type,
            lr.applicant,
            company,
            branch,
            lr.loan_product,
            lr.is_term_loan,
            cc,
            None,
            lr.payment_account,
            lr.loan_account,
            x_prin,
            x_int,
            x_pen,
            pending_prin,
            lia_name,
        )

        lr.total_principal_paid = flt(lr.total_principal_paid) + x_prin
        loan_deltas[lr.name]["principal"] += x_prin
        loan_deltas[lr.name]["interest"] += x_int
        loan_deltas[lr.name]["penalty"] += x_pen
        loan_deltas[lr.name]["amount"] += x_amt
        created += 1

    if loan_deltas:
        _bulk_update_loan_totals(loan_deltas)
        frappe.db.commit()

    return created


# ── 4. FILE MAPS ──────────────────────────────────────────────────────────────

# Single file for all branches — branch resolved via CNIC/LoanRef
BORROWERS_FILE = "/private/files/7th june to 10th june borrowers.xlsx"
REPAYMENTS_FILE = "/private/files/7th june to 10th june repayments.xlsx"
DISBURSEMENTS_FILE = "/private/files/7th june to 10th june disbursement.xlsx"
BRANCH_CODE_MAP = {
    "70805": "Jauharabad - JBD-1",  # ← added
    "70806": "Khushab - KHB",
    "70807": "Talagang",
    "70808": "Jhanwarian",
    "70809": "Shahpur",
    "70810": "Naushera - NSR",
    "70811": "Sargodha-1",
    "70812": "Sargodha-2",
    "70813": "Sargodha-3",
    "76702": "Lalian",
    "69345": "Naushera - NSR",
    "78282": "Jauharabad - JBD-2",
    "82472": "Bhulwal",
    "85559": "Jhanwarian",  # ← added
}


# ── 5. MASTER RUN ─────────────────────────────────────────────────────────────


def run_june_7_10_migration():
    sep = "=" * 60
    print("\n" + sep)
    print("🚀 JUNE 7-10 DELTA MIGRATION")
    print(sep)

    # Borrowers — single file, no branch loop needed
    # Branch will be resolved per row via loan officer / CNIC
    print("\n[1/3] Importing Borrowers...")
    # We pass None as branch since borrowers file has no branch column
    # import_borrowers will skip branch assignment
    created_b = import_borrowers(None, BORROWERS_FILE)
    print(f"  ➔ {created_b} new customers created")

    # Disbursements — read file once, group by branch code
    print("\n[2/3] Importing Disbursements...")
    disb_rows = get_cached_excel(DISBURSEMENTS_FILE)
    branch_disb = defaultdict(list)
    for r in disb_rows:
        code = str(r.get("Branch") or "").strip()
        branch = BRANCH_CODE_MAP.get(code)
        if branch:
            branch_disb[branch].append(r)
        else:
            print(f"  ⚠️  Unknown branch code: {code}")

    total_disb = 0
    for branch, b_rows in branch_disb.items():
        # Write rows to temp cache
        EXCEL_RAM_CACHE[f"__disb_{branch}"] = b_rows
        c = import_disbursements(branch, f"__disb_{branch}")
        print(f"  ➔ {branch}: {c} new loans created")
        total_disb += c
    print(f"  ➔ Total: {total_disb} new loans")

    # Repayments — read file once, group by branch via loan ref lookup
    print("\n[3/3] Importing Repayments...")
    rep_rows = get_cached_excel(REPAYMENTS_FILE)

    # Build ref -> branch map from ERP
    erp_loans = frappe.db.sql(
        """
        SELECT name, branch, description FROM `tabLoan`
        WHERE docstatus = 1
    """,
        as_dict=True,
    )

    ref_to_branch = {}
    for l in erp_loans:
        m = re.search(r"LoanRef:([^\s,]+)", l.description or "")
        if m:
            ref_to_branch[m.group(1).strip()] = l.branch

    # Group repayment rows by branch
    branch_reps = defaultdict(list)
    for r in rep_rows:
        ref = str(r.get("Loan#") or "").strip()
        branch = ref_to_branch.get(ref)
        if branch:
            branch_reps[branch].append(r)
        else:
            print(f"  ⚠️  Loan ref {ref} not found in ERP — skipping")

    total_reps = 0
    for branch, b_rows in branch_reps.items():
        EXCEL_RAM_CACHE[f"__rep_{branch}"] = b_rows
        c = import_repayments(branch, f"__rep_{branch}")
        print(f"  ➔ {branch}: {c} new repayments created")
        total_reps += c
    print(f"  ➔ Total: {total_reps} new repayments")

    # affected_branches = set(branch_disb.keys()) | set(branch_reps.keys())
    # for branch in affected_branches:
    #     reconcile_loan_paid_amounts(branch)
    #     fix_repayment_statuses(branch)
    #     print(f"  ✅ {branch} reconciled")

    print("\n" + sep)
    print("🎉 JUNE 7-10 MIGRATION COMPLETE")
    print(sep + "\n")


# run_june_7_10_migration()
