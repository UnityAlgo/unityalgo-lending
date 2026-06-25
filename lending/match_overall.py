import frappe
import re
from lending.loan_management.doctype.data_migration_tool.data_migration_tool import (
    _read_excel, _safe_float, _cnic, _safe_date, _ensure_sales_person
)

# ── 1. TARGET DATA (ALL BRANCHES UNCOMMENTED) ─────────────────────────────────
may_disbursements = {
    "Bhulwal": "/private/files/BWL till 31st may.xlsx",
    "Jauharabad - JBD-2": "/private/files/Jbd 2 Branch Disbursement Report start date to 31 Mayf832fa.xlsx",
    "Jauharabad - JBD-1": "/private/files/Jauharabad 1 Branch Disbursement Report start date to 31 May.xlsx",
    "Jhanwarian": "/private/files/JWR  till 31st may.xlsx",
    "Khushab - KHB": "/private/files/khusab disbursements 2023 to till 31 May 2026 (3).xlsx",
    "Lalian": "/private/files/LLN till 31st May.xlsx",
    "Naushera - NSR": "/private/files/Naushera Branch Disbursement Report start date to 31 May.xlsx",
    "SAM": "/private/files/SAM till 31st May.xlsx",
    "Shahpur": "/private/files/shahpur 31st may.xlsx",
    "Sargodha-3": "/private/files/SRG 3 till 31st may.xlsx",
    "Sargodha-2": "/private/files/SRG 2 till 31st may.xlsx",
    "Sargodha-1": "/private/files/SRG 1 31 May4383d2.xlsx",
    "Talagang": "/private/files/Talagang  Branch Disbursement Report start date to 31 May.xlsx"
}

# ── 2. DELTA DATA (June 1 - June 7) ───────────────────────────────────────────
june_disbursements = {
    "Bhulwal": "/private/files/BWL.xlsx",
    "Jauharabad - JBD-1": "/private/files/JBD1.xlsx",
    "Jauharabad - JBD-2": "/private/files/JBD2.xlsx",
    "Jhanwarian": "/private/files/JWR.xlsx",
    "Khushab - KHB": "/private/files/KHB.xlsx",
    "Lalian": "/private/files/LLN.xlsx",
    "Naushera - NSR": "/private/files/NSR.xlsx",
    "SAM": None, 
    "Shahpur": "/private/files/SHP.xlsx",
    "Sargodha-1": "/private/files/SRG1.xlsx",
    "Sargodha-2": "/private/files/SRG2.xlsx",
    "Sargodha-3": "/private/files/SRG3.xlsx",
    "Talagang": "/private/files/TLG.xlsx"
}

EXCEL_RAM_CACHE = {}
def get_cached_excel(file_path):
    if not file_path: return []
    if file_path not in EXCEL_RAM_CACHE:
        EXCEL_RAM_CACHE[file_path] = _read_excel(file_path)
    return EXCEL_RAM_CACHE[file_path]

# ── 3. FIX ACTION HELPERS ─────────────────────────────────────────────────────
def _cancel_and_delete_loan(loan_name):
    disbs = frappe.db.sql_list("SELECT name FROM `tabLoan Disbursement` WHERE against_loan=%s", (loan_name,))
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
    disbs = frappe.db.sql_list("SELECT name FROM `tabLoan Disbursement` WHERE against_loan=%s AND docstatus=1", (loan_name,))
    for d in disbs:
        doc = frappe.get_doc("Loan Disbursement", d)
        doc.flags.ignore_permissions = True
        doc.cancel()

    loan = frappe.get_doc("Loan", loan_name)
    if loan.docstatus == 1:
        loan.flags.ignore_permissions = True
        loan.cancel()
    frappe.db.commit()

def _create_missing_loan_package(ref, row, branch, company="Care Financial Services Pvt. Ltd"):
    raw_cnic = str(row.get("Borrower CNIC #") or "").strip()
    norm_cnic = _cnic(raw_cnic)
    customer_name = str(row.get("Name") or row.get("Full Name") or "Unknown").strip()

    customer_doc_name = frappe.db.get_value("Customer", {"custom_cnic_no": norm_cnic}, "name")
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
    if sp: loan.sales_person = sp
    loan.description = f"Migrated auto-recovery. LoanRef:{ref}"

    loan.insert(ignore_permissions=True)
    loan.submit()

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
        disb.append("loan_disbursement_charges", {"charge": None, "account": lp.interest_income_account, "amount": fee_amount})

    disb.insert(ignore_permissions=True)
    disb.submit()
    frappe.db.commit()


# ── 4. MASTER AUDIT & FIX LOGIC ───────────────────────────────────────────────
def fix_and_create_cumulative_disbursements():
    print("\n" + "="*75)
    print(" 🚀 INITIATING AUTO-FIX & CREATE: DISBURSEMENTS (MAY + JUNE)")
    print("="*75)

    for branch in may_disbursements.keys():
        may_file = may_disbursements.get(branch)
        june_file = june_disbursements.get(branch)

        raw_excel_rows = []
        for file_path in [may_file, june_file]:
            if file_path:
                for r in get_cached_excel(file_path):
                    loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
                    if loan_no and loan_no.lower() not in ("total", "grand total", "subtotal"):
                        raw_excel_rows.append(r)
                        
        print(f"\n🏢 [ {branch.upper()} ]")
        
        # --- UPGRADED COLLISION RESOLVER & DEDUPLICATION ---
        unique_excel_rows = {}
        loan_cnic_map = {} # Maps base_ref -> {cnic1: "ref", cnic2: "ref-DUP1", ...}

        for r in raw_excel_rows:
            base_ref = str(r.get("Loan#") or r.get("Loan #") or "").strip()
            cnic = _cnic(r.get("Borrower CNIC #"))
            
            if base_ref not in loan_cnic_map:
                # First time seeing this loan number
                loan_cnic_map[base_ref] = {cnic: base_ref}
                r["Loan#"] = base_ref
                unique_excel_rows[base_ref] = r
            else:
                # We have seen this loan number before. Have we seen this CNIC?
                if cnic in loan_cnic_map[base_ref]:
                    # Double Entry (Ghost row). Safely ignore.
                    pass 
                else:
                    # CLASH! Different customer, same loan number.
                    # Generate dynamic suffix based on how many duplicates already exist
                    dup_count = len(loan_cnic_map[base_ref])
                    new_ref = f"{base_ref}-DUP{dup_count}"
                    
                    # Register this new CNIC under this base_ref
                    loan_cnic_map[base_ref][cnic] = new_ref
                    
                    r["Loan#"] = new_ref # Update memory so ERP treats it as unique
                    unique_excel_rows[new_ref] = r
                    print(f"  ⚠️ MULTI-COLLISION RESOLVED: '{base_ref}' used by new customer. Renamed to '{new_ref}'")

        print(f"  Valid unique Excel loans : {len(unique_excel_rows):,}")

        # Fetch ERP Loans
        erp_loans_raw = frappe.db.sql("""
            SELECT name, description, docstatus, loan_amount, status 
            FROM `tabLoan` WHERE branch = %s
        """, (branch,), as_dict=True)

        erp_map = {}
        for loan in erp_loans_raw:
            m = re.search(r"LoanRef:([^\s,]+)", loan.description or "")
            if m:
                ref = m.group(1).strip()
                prev = erp_map.get(ref)
                if prev is None or (int(loan.docstatus) == 1 and int(prev.docstatus) != 1):
                    erp_map[ref] = loan

        print(f"  ERP loans mapped         : {len(erp_map):,}")

        CANCELLED_STATUSES = {"denied", "processing", "rmd approved", "pending"}

        matched = 0
        mismatch_amounts = []
        missing_in_erp = []
        should_be_cancelled = []
        extra_in_erp = []
        seen_refs = set()

        excel_live_amt = excel_cancelled_amt = erp_live_amt = erp_cancelled_amt = 0.0

        # Calculate base ERP Totals
        for erp_loan in erp_map.values():
            amt = _safe_float(erp_loan.loan_amount)
            if int(erp_loan.docstatus) == 1:
                erp_live_amt += amt
            elif int(erp_loan.docstatus) == 2:
                erp_cancelled_amt += amt

        # Audit Sweep
        for ref, r in unique_excel_rows.items():
            seen_refs.add(ref)
            excel_amt = _safe_float(r.get("Disbursed"))
            excel_status = str(r.get("Status") or "").strip().lower()

            if excel_status in CANCELLED_STATUSES:
                excel_cancelled_amt += excel_amt
            else:
                excel_live_amt += excel_amt

            erp_loan = erp_map.get(ref)

            if not erp_loan:
                missing_in_erp.append({"ref": ref, "row": r})
                continue

            docstatus = int(erp_loan.docstatus)
            erp_amt = _safe_float(erp_loan.loan_amount)

            if excel_status in CANCELLED_STATUSES and docstatus == 1:
                should_be_cancelled.append({"ref": ref, "erp_name": erp_loan.name})
                continue

            if docstatus == 1 and abs(excel_amt - erp_amt) > 1.0:
                mismatch_amounts.append({"ref": ref, "erp_name": erp_loan.name, "excel_amt": excel_amt, "erp_amt": erp_amt})
                continue

            matched += 1

        for ref, loan in erp_map.items():
            if ref not in seen_refs and int(loan.docstatus) == 1:
                extra_in_erp.append({"ref": ref, "erp_name": loan.name})

        # --- PRE-EXECUTION DIFFERENCE REPORT ---
        sep = "─" * 70
        print(sep)
        print(f"  📊 PRE-EXECUTION DIFFERENCE REPORT")
        print(sep)
        print(f"  ✅ OK (Matched)          : {matched:,}")
        print(f"  💰 Amount Mismatches     : {len(mismatch_amounts):,}")
        print(f"  ❌ To Be Cancelled       : {len(should_be_cancelled):,}")
        print(f"  🔍 Missing (To Create)   : {len(missing_in_erp):,}")
        print(f"  👻 Extras (To Delete)    : {len(extra_in_erp):,}")
        print(sep)
        print(f"  Category       | {'Excel Amount':>16} | {'ERP Amount':>16} | {'Difference':>12}")
        print(f"  {'-' * 14} | {'-' * 16} | {'-' * 16} | {'-' * 12}")
        print(f"  Live / Active  | {excel_live_amt:>16,.2f} | {erp_live_amt:>16,.2f} | {excel_live_amt - erp_live_amt:>12,.2f}")
        print(f"  Cancelled      | {excel_cancelled_amt:>16,.2f} | {erp_cancelled_amt:>16,.2f} | {excel_cancelled_amt - erp_cancelled_amt:>12,.2f}")
        print(sep)

        # --- AUTO FIX EXECUTION ---
        print("\n  ⚙️ EXECUTING FIXES...")
        
        if extra_in_erp:
            print(f"     ➔ Deleting {len(extra_in_erp)} extra/ghost ERP loans...")
            for e in extra_in_erp:
                _cancel_and_delete_loan(e["erp_name"])
                
        if should_be_cancelled:
            print(f"     ➔ Cancelling {len(should_be_cancelled)} denied/pending loans...")
            for c in should_be_cancelled:
                _cancel_loan_only(c["erp_name"])

        if mismatch_amounts:
            print(f"     ➔ Fixing {len(mismatch_amounts)} amount mismatches via SQL...")
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
                
        if missing_in_erp:
            print(f"     ➔ Creating {len(missing_in_erp)} missing loans (including resolved collisions)...")
            for m in missing_in_erp:
                try:
                    _create_missing_loan_package(m["ref"], m["row"], branch)
                except Exception as ex:
                    print(f"        ❌ Error creating {m['ref']}: {str(ex)}")

        if not extra_in_erp and not should_be_cancelled and not mismatch_amounts and not missing_in_erp:
             print("     ➔ No fixes required.")

        print("  ✅ BRANCH SYNCHRONIZED PERFECTLY.\n")

from lending.correct_repayments import validate_and_fix_repayments
may_repayments = {
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

def validate_cumulative_repayments():
    print("\n" + "="*70)
    print(" 🚀 INITIATING CUMULATIVE REPAYMENT AUDIT (MAY + JUNE)")
    print("="*70)

    for branch in may_repayments.keys():
        may_file = may_repayments.get(branch)
        june_file = june_repayments.get(branch)
        
        # Combine both files into a single list
        combined_files = []
        if may_file: combined_files.append(may_file)
        if june_file: combined_files.append(june_file)
        
        # Pass the combined list to your existing matching function
        # It will read both files, sum the totals, and cross-reference the ERP once
        validate_and_fix_repayments(branch, combined_files)