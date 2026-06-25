import frappe
import re
from lending.loan_management.doctype.data_migration_tool.data_migration_tool import _read_excel

# ── 1. RAM CACHE ──────────────────────────────────────────────────────────────
EXCEL_RAM_CACHE = {}

def get_cached_excel(file_path):
    if file_path not in EXCEL_RAM_CACHE:
        print(f"   ⏳ Reading {file_path.split('/')[-1]} into RAM...")
        EXCEL_RAM_CACHE[file_path] = _read_excel(file_path)
    return EXCEL_RAM_CACHE[file_path]


def cancel_invalid_loans(disbursement_dict):
    TARGET_STATUSES = {"denied", "processing", "rmd approved", "pending"}
    
    total_cancelled = 0
    total_errors = 0
    
    sep = "=" * 65
    print("\n" + sep)
    print(" 🚨 BULK CANCELLATION: DENIED / PROCESSING / RMD APPROVED")
    print(sep)

    for branch, file_url in disbursement_dict.items():
        try:
            rows = get_cached_excel(file_url)
            
            # 1. Extract LoanRefs that need cancellation from Excel
            refs_to_cancel = set()
            for r in rows:
                status = str(r.get("Status") or "").strip().lower()
                loan_no = str(r.get("Loan#") or r.get("Loan #") or "").strip()
                
                if status in TARGET_STATUSES and loan_no:
                    refs_to_cancel.add(loan_no)
                    
            if not refs_to_cancel:
                print(f"🏢 [ {branch} ] : No target statuses found in Excel.")
                continue
                
            # 2. Find active loans in ERP matching these refs
            active_loans = frappe.db.sql(f"""
                SELECT name, description 
                FROM `tabLoan` 
                WHERE branch = %s AND docstatus = 1
            """, (branch,), as_dict=True)
            
            loans_to_cancel = []
            for l in active_loans:
                m = re.search(r"LoanRef:([^\s,]+)", l.description or "")
                if m and m.group(1).strip() in refs_to_cancel:
                    loans_to_cancel.append(l.name)
                    
            if not loans_to_cancel:
                print(f"🏢 [ {branch} ] : {len(refs_to_cancel)} target refs in Excel, but 0 are active in ERP. (Already safe)")
                continue
                
            print(f"🏢 [ {branch} ] : Cancelling {len(loans_to_cancel)} active loans...")
            
            # 3. Formally cancel disbursements then the loan
            branch_cancelled = 0
            for loan_name in loans_to_cancel:
                try:
                    # Cancel all associated disbursements first
                    disbs = frappe.db.sql_list(
                        "SELECT name FROM `tabLoan Disbursement` WHERE against_loan=%s AND docstatus=1",
                        (loan_name,)
                    )
                    for d in disbs:
                        doc = frappe.get_doc("Loan Disbursement", d)
                        doc.flags.ignore_permissions = True
                        doc.cancel()

                    # Cancel the Loan itself
                    loan_doc = frappe.get_doc("Loan", loan_name)
                    loan_doc.flags.ignore_permissions = True
                    loan_doc.cancel()
                    
                    branch_cancelled += 1
                    total_cancelled += 1
                except Exception as e:
                    print(f"   ⚠️ Error cancelling {loan_name}: {str(e)}")
                    total_errors += 1
                    
            frappe.db.commit()
            print(f"   ✅ Successfully cancelled {branch_cancelled} loans.")
            
        except Exception as e:
            print(f"🏢 [ {branch} ] : ERROR: {str(e)}")
            
    print("\n" + sep)
    print(f" 🎉 CANCELLATION COMPLETE | Total Cancelled: {total_cancelled} | Errors: {total_errors}")
    print(sep + "\n")
