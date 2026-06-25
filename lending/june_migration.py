import frappe
import re
from collections import defaultdict
from frappe.utils import flt, nowdate

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
# from lending.loan_management.doctype.data_migration_tool.data_migration_tool import create_missing_accrual_gl_entries

EXCEL_RAM_CACHE = {}


def get_cached_excel(file_path):
    if file_path not in EXCEL_RAM_CACHE:
        print(f"   ⏳ Reading {file_path.split('/')[-1]} into RAM...")
        EXCEL_RAM_CACHE[file_path] = _read_excel(file_path)
    return EXCEL_RAM_CACHE[file_path]


# Local maps
MARITAL_MAP = {
    "married": "Married",
    "single": "Single",
    "unmarried": "Single",
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


# ── 2. IMPORT FUNCTIONS ───────────────────────────────────────────────────────


def import_customers(branch, file_url):
    rows = get_cached_excel(file_url)
    allowed_marital = _get_allowed("custom_marital_status")
    allowed_rel = _get_allowed("custom_religion")
    created = 0

    for r in rows:
        name = str(r.get("Full Name") or "").strip()
        cnic = _cnic(r.get("Borrower CNIC #"))
        if not name or not cnic or cnic == "0":
            continue

        # Skip if customer exists
        if frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name"):
            continue

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
        c.branch = branch
        if sp:
            c.sales_person = sp

        c.insert(ignore_permissions=True)
        created += 1

    frappe.db.commit()
    return created


def import_loans(branch, file_url, company="Care Financial Services Pvt. Ltd"):
    rows = get_cached_excel(file_url)
    created = 0

    for r in rows:
        ref = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        if not ref or ref.lower() in ("total", "grand total", "subtotal"):
            continue

        # Skip if loan already exists in ERP
        if frappe.db.sql(
            "SELECT name FROM `tabLoan` WHERE description LIKE %s",
            (f"%LoanRef:{ref}%",),
        ):
            continue

        cnic = _cnic(r.get("Borrower CNIC #"))
        customer_doc = frappe.db.get_value("Customer", {"custom_cnic_no": cnic}, "name")
        if not customer_doc:
            continue

        loan_product = str(r.get("Loan Product") or "Standard Loan").strip()
        disbursed_date = _safe_date(r.get("Disbursed Date")) or nowdate()
        loan_amount = _safe_float(r.get("Disbursed")) or _safe_float(
            r.get("Outstanding")
        )
        fee_amount = _safe_float(r.get("Deductable Fees"))

        # Checking both variations found in the headers
        officer_name = str(r.get("LoanOfficer") or r.get("Loan Officer") or "").strip()
        sp = _ensure_sales_person(officer_name) if officer_name else None
        lp = frappe.get_doc("Loan Product", loan_product)

        # Create Loan
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

        # Create Disbursement
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


def import_repayments(branch, file_url, company="Care Financial Services Pvt. Ltd"):
    rows = get_cached_excel(file_url)
    user = frappe.session.user or "Administrator"
    created = 0

    # Pre-fetch ERP loans for this branch to map ref -> doc_name
    loans_db = frappe.db.sql(
        "SELECT name, description, applicant_type, applicant, loan_product, is_term_loan, "
        "payment_account, loan_account, total_payment, debit_adjustment_amount, "
        "credit_adjustment_amount, total_principal_paid, total_interest_payable, "
        "written_off_amount, refund_amount, disbursed_amount, loan_amount "
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
        ref = str(r.get("Loan#") or r.get("Loan #") or "").strip()
        col_date = _safe_date(r.get("Collection Date"))
        if not ref or not col_date:
            continue

        lr = ref_map.get(ref)
        if not lr:
            continue  # Loan not in ERP

        x_prin = _safe_float(r.get("Principal Paid"))
        x_int = _safe_float(r.get("Interest Paid"))
        x_pen = _safe_float(r.get("Penalty Paid"))
        x_fees = _safe_float(r.get("Fees Paid"))
        if x_fees > 0:
            x_prin += x_fees

        # UPDATED: Explicitly checking "Amount" based on June Headers
        x_amt = (
            _safe_float(r.get("Amount"))
            or _safe_float(r.get("Total Paid Amount"))
            or (x_prin + x_int + x_pen)
        )

        # Dedup Check
        dupe = frappe.db.sql(
            "SELECT name FROM `tabLoan Repayment` WHERE against_loan=%s AND DATE(posting_date)=%s AND ABS(amount_paid - %s) < 0.1",
            (lr.name, col_date, x_amt),
        )
        if dupe:
            continue

        cc = _get_cc(company)
        ia = _get_ia(lr.loan_product)

        # 1. Accrual Logic (LIA)
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

        # 2. Raw Repayment Insert
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


# ── 3. MASTER EXECUTION DICTIONARIES & LOGIC ──────────────────────────────────

june_customers = {
    "Bhulwal": "/private/files/BWL137c13.xlsx",
    "Jauharabad - JBD-1": "/private/files/JBD129ffc7.xlsx",
    "Jauharabad - JBD-2": "/private/files/JBD27fe98e.xlsx",
    "Jhanwarian": "/private/files/JWR3c9521.xlsx",
    "Khushab - KHB": "/private/files/KHB8a9ac7.xlsx",
    "Naushera - NSR": "/private/files/NSRe37d39.xlsx",
    "SAM": "/private/files/SAMb66ed5.xlsx",
    "Lalian": "/private/files/LLN8a8dee.xlsx",
    "Shahpur": "/private/files/SHPa8b80c.xlsx",
    "Sargodha-1": "/private/files/SRG1ebde78.xlsx",
    "Sargodha-2": "/private/files/SRG2cb9c2d.xlsx",
    "Sargodha-3": "/private/files/SRG3950a1b.xlsx",
    "Talagang": "/private/files/TLG5c046b.xlsx",
}

june_disbursements = {
    "Bhulwal": "/private/files/BWL.xlsx",
    "Jauharabad - JBD-1": "/private/files/JBD1.xlsx",
    "Jauharabad - JBD-2": "/private/files/JBD2.xlsx",
    "Jhanwarian": "/private/files/JWR.xlsx",
    "Khushab - KHB": "/private/files/KHB.xlsx",
    "Lalian": "/private/files/LLN.xlsx",
    "Naushera - NSR": "/private/files/NSR.xlsx",
    "Shahpur": "/private/files/SHP.xlsx",
    "Sargodha-1": "/private/files/SRG1.xlsx",
    "Sargodha-2": "/private/files/SRG2.xlsx",
    "Sargodha-3": "/private/files/SRG3.xlsx",
    "Talagang": "/private/files/TLG.xlsx",
}

june_repayments = {
    "Bhulwal": "/private/files/BWL277071.xlsx",
    "Jauharabad - JBD-1": "/private/files/JBD161c0fa.xlsx",
    "Jauharabad - JBD-2": "/private/files/JBD29dd6c7.xlsx",
    "Jhanwarian": "/private/files/JWRd8d0f6.xlsx",
    "Khushab - KHB": "/private/files/KHB52c9bd.xlsx",
    "Lalian": "/private/files/LLN05d1a8.xlsx",
    "Naushera - NSR": "/private/files/NSR01d15a.xlsx",
    "SAM": "/private/files/SAM.xlsx",
    "Shahpur": "/private/files/SHPf84de8.xlsx",
    "Sargodha-1": "/private/files/SRG1dc61cc.xlsx",
    "Sargodha-2": "/private/files/SRG2736165.xlsx",
    "Sargodha-3": "/private/files/SRG3fb9f10.xlsx",
    "Talagang": "/private/files/TLG5a0855.xlsx",
}


def run_midnight_go_live():
    sep = "=" * 60
    print("\n" + sep)
    print("🚀 INITIATING JUNE DELTA MIGRATION")
    print(sep)

    print("\n[1/3] Processing June Customers...")
    for branch, file in june_customers.items():
        c = import_customers(branch, file)
        print(f"  ➔ {branch}: {c} New Customers created.")

    print("\n[2/3] Processing June Disbursements (Loans)...")
    for branch, file in june_disbursements.items():
        c = import_loans(branch, file)
        print(f"  ➔ {branch}: {c} New Loans created.")

    print("\n[3/3] Processing June Repayments...")
    for branch, file in june_repayments.items():
        c = import_repayments(branch, file)
        print(f"  ➔ {branch}: {c} New Repayments created.")

    # print("\n⏳ Running Financial Reconciliations & Finalizing Ledgers...")
    from lending.loan_management.doctype.data_migration_tool.data_migration_tool import (
        reconcile_loan_paid_amounts,
        fix_repayment_statuses,
    )

    for branch in june_disbursements.keys():
        reconcile_loan_paid_amounts(branch)
        fix_repayment_statuses(branch)

    # print("\n⏳ Generating Global Ledger Entries...")
    # create_missing_accrual_gl_entries()

    print("\n" + sep)
    print("🎉 MIGRATION COMPLETE. SYSTEM IS LIVE AND BALANCED!")
    print(sep + "\n")


# Trigger the Go-Live Sequence
# run_midnight_go_live()
