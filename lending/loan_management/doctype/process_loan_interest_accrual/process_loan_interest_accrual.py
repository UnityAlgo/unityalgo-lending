# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe.model.document import Document
from frappe.utils import nowdate

from lending.loan_management.doctype.loan_interest_accrual.loan_interest_accrual import (
    make_accrual_interest_entry_for_demand_loans,
    make_accrual_interest_entry_for_term_loans,
)


class ProcessLoanInterestAccrual(Document):
    def on_submit(self):
        open_loans = []

        if self.loan:
            loan_doc = frappe.get_doc("Loan", self.loan)
            if loan_doc:
                open_loans.append(loan_doc)

        if (
            not self.loan or not loan_doc.is_term_loan
        ) and self.process_type != "Term Loans":
            make_accrual_interest_entry_for_demand_loans(
                self.posting_date,
                self.name,
                open_loans=open_loans,
                loan_product=self.loan_product,
                accrual_type=self.accrual_type,
            )

        if (
            not self.loan or loan_doc.is_term_loan
        ) and self.process_type != "Demand Loans":
            make_accrual_interest_entry_for_term_loans(
                self.posting_date,
                self.name,
                term_loan=self.loan,
                loan_product=self.loan_product,
                accrual_type=self.accrual_type,
            )


def process_loan_interest_accrual_for_demand_loans(
    posting_date=None, loan_product=None, loan=None, accrual_type="Regular"
):
    loan_process = frappe.new_doc("Process Loan Interest Accrual")
    loan_process.posting_date = posting_date or nowdate()
    loan_process.loan_product = loan_product
    loan_process.process_type = "Demand Loans"
    loan_process.loan = loan
    loan_process.accrual_type = accrual_type

    loan_process.submit()

    return loan_process.name


def process_loan_interest_accrual_for_term_loans(
    posting_date=None, loan_product=None, loan=None
):
    target_date = posting_date or nowdate()

    if not term_loan_accrual_pending(target_date, loan=loan):
        return

    if loan or posting_date:
        loan_process = frappe.new_doc("Process Loan Interest Accrual")
        loan_process.posting_date = target_date
        loan_process.loan_product = loan_product
        loan_process.process_type = "Term Loans"
        loan_process.loan = loan
        loan_process.submit()
        return loan_process.name

    due_dates = frappe.db.sql_list(
        """
        SELECT DISTINCT DATE(rs.payment_date)
        FROM `tabRepayment Schedule` rs
        INNER JOIN `tabLoan Repayment Schedule` lrs ON rs.parent = lrs.name
        INNER JOIN `tabLoan` l ON lrs.loan = l.name
        WHERE l.docstatus = 1
          AND l.status = 'Disbursed'
          AND l.is_term_loan = 1
          AND lrs.status = 'Active'
          AND rs.principal_amount > 0
          AND rs.payment_date <= %(date)s
          AND rs.is_accrued = 0
          AND rs.docstatus = 1
        ORDER BY 1
    """,
        {"date": target_date},
    )

    for due_date in due_dates:
        frappe.enqueue(
            "lending.loan_management.doctype.process_loan_interest_accrual"
            ".process_loan_interest_accrual.process_loan_interest_accrual_for_term_loans",
            posting_date=str(due_date),
            loan_product=loan_product,
            queue="long",
            timeout=3600,
            job_id=f"lia_accrual_{due_date}",
            is_async=True,
        )

    frappe.logger().info(
        f"LIA accrual: enqueued {len(due_dates)} jobs ({due_dates[0] if due_dates else '-'} → {due_dates[-1] if due_dates else '-'})"
    )


def term_loan_accrual_pending(date, loan=None):
    filters = {"payment_date": ("<=", date), "is_accrued": 0}

    if loan:
        loan_repayment_schedule = frappe.db.get_value(
            "Loan Repayment Schedule", {"loan": loan, "status": "Active"}
        )
        filters.update({"parent": loan_repayment_schedule})

    pending_accrual = frappe.db.get_value("Repayment Schedule", filters)

    return pending_accrual
