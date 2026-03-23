# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, get_datetime, getdate, today

import erpnext
from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.controllers.accounts_controller import AccountsController

from lending.loan_management.doctype.loan.loan import (
    update_all_linked_loan_customer_npa_status,
)
from lending.loan_management.doctype.loan_interest_accrual.loan_interest_accrual import (
    get_last_accrual_date,
    get_per_day_interest,
)
from lending.loan_management.doctype.loan_security_shortfall.loan_security_shortfall import (
    update_shortfall_status,
)
from lending.loan_management.doctype.process_loan_classification.process_loan_classification import (
    create_process_loan_classification,
)
from lending.loan_management.doctype.process_loan_interest_accrual.process_loan_interest_accrual import (
    process_loan_interest_accrual_for_demand_loans,
)


class LoanRepayment(AccountsController):
    def validate(self):
        amounts = calculate_amounts(
            self.against_loan, self.posting_date, self.repayment_type
        )
        self.add_pending_charges()
        self.set_missing_values(amounts)
        self.check_future_entries()
        self.validate_amount()
        self.allocate_amounts(amounts)

    def before_submit(self):
        self.book_unaccrued_interest()
        self.book_unaccrued_penalty()

    def on_submit(self):

        # if self.repayment_type == "Normal Repayment":
        #     create_process_loan_classification(
        #         posting_date=self.posting_date,
        #         loan_product=self.loan_product,
        #         loan=self.against_loan,
        #         payment_reference=self.name,
        #     )

        self.update_paid_amount()
        if self.repayment_type == "Charges Waiver":
            self.make_credit_note()

        loan_doc = frappe.get_doc("Loan", self.against_loan)
        self.make_gl_entries()
        loan_doc.set_repayment_status()

    def on_cancel(self):
        self.check_future_accruals()
        if self.repayment_type in ("Normal Repayment", "Loan Closure"):
            self.mark_as_unpaid()
            if self.is_npa or self.manual_npa:
                update_all_linked_loan_customer_npa_status(
                    self.is_npa,
                    self.manual_npa,
                    self.applicant_type,
                    self.applicant,
                    self.posting_date,
                )

            frappe.db.set_value(
                "Loan", self.against_loan, "days_past_due", self.days_past_due
            )

        self.ignore_linked_doctypes = [
            "GL Entry",
            "Payment Ledger Entry",
            "Process Loan Classification",
        ]
        self.make_gl_entries(cancel=1)

        loan = frappe.db.get_value(
            "Loan",
            self.against_loan,
            ["status", "disbursed_amount", "total_principal_paid"],
            as_dict=1,
        )
        if (
            loan.status == "Closed"
            and loan.disbursed_amount > loan.total_principal_paid
        ):
            frappe.db.set_value("Loan", self.against_loan, "status", "Disbursed")

    def book_unaccrued_penalty(self):
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        if flt(self.total_penalty_paid, precision) <= 0:
            return

        billed_penalty = flt(
            frappe.db.get_value(
                "Loan Interest Accrual",
                {"loan": self.against_loan, "docstatus": 1, "accrual_type": "Penalty"},
                "sum(penalty_amount)",
            )
        )

        paid_penalty = flt(
            frappe.db.get_value(
                "Loan Repayment",
                {
                    "against_loan": self.against_loan,
                    "docstatus": 1,
                    "name": ["!=", self.name],
                },
                "sum(total_penalty_paid)",
            )
        )

        new_total_paid = paid_penalty + flt(self.total_penalty_paid, precision)
        missing_penalty = flt(new_total_paid - billed_penalty, precision)

        if missing_penalty > 0:
            lia = frappe.new_doc("Loan Interest Accrual")
            lia.loan = self.against_loan
            lia.company = self.company
            lia.branch = self.get("branch")
            lia.cost_center = self.cost_center
            lia.posting_date = self.posting_date
            lia.date_of_accrual = self.posting_date
            lia.due_date = self.posting_date

            lia.interest_amount = 0.0
            lia.payable_principal_amount = 0.0
            lia.pending_principal_amount = flt(self.pending_principal_amount, precision)

            lia.penalty_amount = missing_penalty
            lia.accrual_type = "Penalty"

            lia.insert(ignore_permissions=True)
            lia.submit()

    def update_loan_repayment_status(loan):
        current_date = getdate(today())
        new_status = "Regular"
        schedule = frappe.db.get_all(
            "Repayment Schedule",
            filters={"parent": loan.name},
            fields=[
                "name",
                "payment_date",
                "interest_amount",
                "principal_amount",
                "is_accrued",
            ],
            order_by="idx asc",
        )

        if not schedule:
            return

        final_maturity_date = schedule[-1].payment_date
        past_due_rows = 0
        has_partial_payment = False
        oldest_unpaid_date = None

        for sch in schedule:
            acc_int = (
                frappe.db.get_value(
                    "Loan Interest Accrual",
                    {"loan": loan.name, "due_date": sch.payment_date, "docstatus": 1},
                    "sum(interest_amount)",
                )
                or 0
            )
            acc_prin = (
                frappe.db.get_value(
                    "Loan Interest Accrual",
                    {"loan": loan.name, "due_date": sch.payment_date, "docstatus": 1},
                    "sum(payable_principal_amount)",
                )
                or 0
            )

            total_billed = flt(sch.interest_amount) + flt(sch.principal_amount)
            total_paid = flt(acc_int) + flt(acc_prin)

            is_fully_paid = total_paid >= total_billed

            if getdate(sch.payment_date) < current_date and not is_fully_paid:
                past_due_rows += 1

                if not oldest_unpaid_date:
                    oldest_unpaid_date = sch.payment_date

                if total_paid > 0:
                    has_partial_payment = True

        if oldest_unpaid_date:
            days_late = date_diff(current_date, oldest_unpaid_date)

            if current_date > getdate(final_maturity_date):
                new_status = "Past Maturity"

            # Rule 3: Arrears (Accumulated unpaid installments > 1 OR Partial Payments)
            elif past_due_rows > 1 or has_partial_payment:
                new_status = "Arrears"

            # Rule 2: Missed Repayment (30+ days late, meaning a full cycle was missed)
            elif days_late >= 30:
                new_status = "Missed Repayment"

            # Rule 1: Overdue (1 to 29 days late)
            elif days_late > 0 and days_late < 30:
                new_status = "Overdue"
        else:
            new_status = "Regular"

        if loan.repayment_status != new_status:
            frappe.db.set_value("Loan", loan.name, "repayment_status", new_status)

    def make_credit_note(self):
        item_details = frappe.db.get_value(
            "Loan Product", self.loan_product, ["charges_waiver_item"], as_dict=1
        )
        charges_waiver_item_income_account = frappe.db.get_value(
            "Loan Charges",
            {
                "charge_type": item_details.charges_waiver_item,
                "parent": self.loan_product,
            },
            "income_account",
        )

        for invoice in self.get("pending_charges"):
            if invoice.sales_invoice:
                si = frappe.new_doc("Sales Invoice")
                si.customer = self.applicant
                si.append(
                    "items",
                    {
                        "item_code": item_details.charges_waiver_item,
                        "qty": -1,
                        "rate": invoice.allocated_amount,
                        "income_account": charges_waiver_item_income_account,
                    },
                )
                si.set_missing_values()
                si.is_return = 1
                si.loan = self.against_loan
                si.save()
                for tax in si.get("taxes"):
                    tax.included_in_print_rate = 1
                si.save()
                si.submit()

    def set_missing_values(self, amounts):
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        if not self.posting_date:
            self.posting_date = get_datetime()

        if not self.cost_center:
            self.cost_center = erpnext.get_default_cost_center(self.company)

        # Removed 'if not self.interest_payable' guard to ensure stale UI data is overwritten properly
        self.interest_payable = flt(amounts["interest_amount"], precision)
        self.penalty_amount = flt(amounts["penalty_amount"], precision)
        self.pending_principal_amount = flt(
            amounts["pending_principal_amount"], precision
        )
        self.payable_amount = flt(amounts["payable_amount"], precision)

        if not self.payable_principal_amount and self.is_term_loan:
            self.payable_principal_amount = flt(
                amounts["payable_principal_amount"], precision
            )

        if not self.get("pending_charges"):
            for d in amounts.get("charges"):
                self.append(
                    "pending_charges",
                    {
                        "sales_invoice": d.sales_invoice,
                        "pending_charge_amount": d.pending_charge_amount,
                    },
                )

        self.total_charges_payable = flt(amounts["total_charges_payable"], precision)

        shortfall_amount = flt(
            frappe.db.get_value(
                "Loan Security Shortfall",
                {"loan": self.against_loan, "status": "Pending"},
                "shortfall_amount",
            )
        )

        if shortfall_amount:
            self.shortfall_amount = shortfall_amount

        if amounts.get("due_date"):
            self.due_date = amounts.get("due_date")

        if hasattr(self, "repay_from_salary") and hasattr(
            self, "payroll_payable_account"
        ):
            if self.repay_from_salary and not self.payroll_payable_account:
                frappe.throw(_("Please set Payroll Payable Account in Loan Repayment"))
            elif not self.repay_from_salary and self.payroll_payable_account:
                self.repay_from_salary = 1

    def check_future_entries(self):
        future_repayment_date = frappe.db.get_value(
            "Loan Repayment",
            {
                "posting_date": (">", self.posting_date),
                "docstatus": 1,
                "against_loan": self.against_loan,
            },
            "posting_date",
        )

        if future_repayment_date:
            frappe.throw(
                "Repayment already made till date {0}".format(
                    get_datetime(future_repayment_date)
                )
            )

    def validate_amount(self):
        if not self.amount_paid:
            frappe.throw(_("Amount paid cannot be zero"))

    def book_unaccrued_interest(self):
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        principal_mapped = sum(
            flt(d.paid_principal_amount) for d in self.get("repayment_details")
        )
        unmapped_principal = flt(
            self.principal_amount_paid - principal_mapped, precision
        )

        unaccrued_interest_paid = flt(
            self.total_interest_paid - self.interest_payable, precision
        )
        has_unaccrued_interest = unaccrued_interest_paid > 0

        if has_unaccrued_interest or unmapped_principal > 0:
            if not self.is_term_loan:
                # --- STANDARD DEMAND LOAN LOGIC ---
                last_accrual_date = get_last_accrual_date(
                    self.against_loan, self.posting_date
                )
                per_day_interest = get_per_day_interest(
                    self.pending_principal_amount,
                    self.rate_of_interest,
                    self.company,
                    self.posting_date,
                )
                no_of_days = flt(unaccrued_interest_paid / per_day_interest, 0) - 1
                posting_date = add_days(last_accrual_date, no_of_days)

                process = process_loan_interest_accrual_for_demand_loans(
                    posting_date=posting_date,
                    loan=self.against_loan,
                    accrual_type="Repayment",
                )

                lia = frappe.db.get_value(
                    "Loan Interest Accrual",
                    {"process_loan_interest_accrual": process},
                    ["name", "interest_amount", "payable_principal_amount"],
                    as_dict=1,
                )

                if lia:
                    accrual_row = self.append(
                        "repayment_details",
                        {
                            "loan_interest_accrual": lia.name,
                            "paid_interest_amount": unaccrued_interest_paid,
                            "paid_principal_amount": unmapped_principal,
                            "accrual_type": "Repayment",
                        },
                    )
                    accrual_row.db_insert()
            else:
                unmapped_int_remaining = unaccrued_interest_paid
                unmapped_prin_remaining = unmapped_principal

                # EARLY CLOSURE JIT INTERCEPT
                if self.repayment_type == "Loan Closure":
                    # For Loan Closure, interest_payable already includes unaccrued interest
                    # (set by calculate_amounts with payment_type="Loan Closure").
                    # The formula unaccrued_interest_paid = total_interest_paid - interest_payable
                    # yields 0 because they're equal. But the interest IS unaccrued — no LIA exists.
                    #
                    # So for closure, we use the ACTUAL allocated amounts directly:
                    #   - total_interest_paid = what allocate_amounts assigned to interest
                    #   - unmapped_principal = principal_amount_paid minus what's already in repayment_details
                    closure_int = flt(self.total_interest_paid, precision)
                    closure_prin = unmapped_principal  # already = principal_amount_paid - principal_mapped

                    # Only subtract interest already mapped to existing repayment_details rows
                    interest_already_mapped = sum(
                        flt(d.paid_interest_amount)
                        for d in self.get("repayment_details")
                    )
                    closure_int = flt(closure_int - interest_already_mapped, precision)

                    if closure_int > 0 or closure_prin > 0:
                        lia = frappe.new_doc("Loan Interest Accrual")
                        lia.loan = self.against_loan
                        lia.company = self.company
                        lia.branch = self.get("branch")
                        lia.cost_center = self.cost_center
                        lia.posting_date = self.posting_date
                        lia.date_of_accrual = self.posting_date
                        lia.due_date = self.posting_date
                        lia.interest_amount = closure_int if closure_int > 0 else 0.0
                        lia.payable_principal_amount = (
                            closure_prin if closure_prin > 0 else 0.0
                        )
                        lia.accrual_type = "Repayment"
                        lia.insert(ignore_permissions=True)
                        lia.submit()

                        accrual_row = self.append(
                            "repayment_details",
                            {
                                "loan_interest_accrual": lia.name,
                                "paid_interest_amount": lia.interest_amount,
                                "paid_principal_amount": lia.payable_principal_amount,
                                "accrual_type": "Repayment",
                            },
                        )
                        accrual_row.db_insert()
                else:
                    schedule_id = frappe.db.get_value(
                        "Loan Repayment Schedule",
                        {"loan": self.against_loan, "docstatus": 1},
                        "name",
                    )
                    next_schedules = frappe.db.get_all(
                        "Repayment Schedule",
                        filters={"parent": schedule_id},
                        fields=["interest_amount", "principal_amount", "payment_date"],
                        order_by="idx asc",
                    )

                    # Loop through schedule and create an LIA for each row satisfied
                    is_first_unaccrued_row = True

                    # Loop through schedule and create an LIA for each row satisfied
                    for sch in next_schedules:
                        if unmapped_int_remaining <= 0 and unmapped_prin_remaining <= 0:
                            break

                        acc_int = flt(
                            frappe.db.get_value(
                                "Loan Interest Accrual",
                                {
                                    "loan": self.against_loan,
                                    "due_date": sch.payment_date,
                                    "docstatus": 1,
                                },
                                "sum(interest_amount)",
                            )
                            or 0,
                            precision,
                        )

                        acc_prin = flt(
                            frappe.db.get_value(
                                "Loan Interest Accrual",
                                {
                                    "loan": self.against_loan,
                                    "due_date": sch.payment_date,
                                    "docstatus": 1,
                                },
                                "sum(payable_principal_amount)",
                            )
                            or 0,
                            precision,
                        )

                        pending_int = flt(
                            flt(sch.interest_amount, precision) - acc_int, precision
                        )
                        pending_prin = flt(
                            flt(sch.principal_amount, precision) - acc_prin, precision
                        )

                        int_to_book = 0.0
                        prin_to_book = 0.0

                        # --- THE FLIPPING PRIORITY LOGIC ---
                        if is_first_unaccrued_row:
                            # REPAYMENT MONTH: Map Interest first, then Principal
                            if pending_int > 0 and unmapped_int_remaining > 0:
                                int_to_book = min(pending_int, unmapped_int_remaining)
                                unmapped_int_remaining = flt(
                                    unmapped_int_remaining - int_to_book, precision
                                )

                            if pending_prin > 0 and unmapped_prin_remaining > 0:
                                prin_to_book = min(
                                    pending_prin, unmapped_prin_remaining
                                )
                                unmapped_prin_remaining = flt(
                                    unmapped_prin_remaining - prin_to_book, precision
                                )
                        else:
                            # SUBSEQUENT MONTHS: Map Principal first, then Interest
                            if pending_prin > 0 and unmapped_prin_remaining > 0:
                                prin_to_book = min(
                                    pending_prin, unmapped_prin_remaining
                                )
                                unmapped_prin_remaining = flt(
                                    unmapped_prin_remaining - prin_to_book, precision
                                )

                            if pending_int > 0 and unmapped_int_remaining > 0:
                                int_to_book = min(pending_int, unmapped_int_remaining)
                                unmapped_int_remaining = flt(
                                    unmapped_int_remaining - int_to_book, precision
                                )

                        # If this row required mapping, flip the switch for the next rows
                        if pending_int > 0 or pending_prin > 0:
                            is_first_unaccrued_row = False

                        if int_to_book > 0 or prin_to_book > 0:
                            lia = frappe.new_doc("Loan Interest Accrual")
                            lia.loan = self.against_loan
                            lia.company = self.company
                            lia.branch = self.branch
                            lia.cost_center = self.cost_center
                            lia.posting_date = self.posting_date
                            lia.date_of_accrual = self.posting_date
                            lia.due_date = sch.payment_date
                            lia.interest_amount = int_to_book
                            lia.payable_principal_amount = prin_to_book
                            lia.accrual_type = "Repayment"
                            lia.insert(ignore_permissions=True)
                            lia.submit()

                            accrual_row = self.append(
                                "repayment_details",
                                {
                                    "loan_interest_accrual": lia.name,
                                    "paid_interest_amount": int_to_book,
                                    "paid_principal_amount": prin_to_book,
                                    "accrual_type": "Repayment",
                                },
                            )
                            accrual_row.db_insert()

                    # Safety Net: If cash exceeds entire schedule length
                    if unmapped_int_remaining > 0 or unmapped_prin_remaining > 0:
                        lia = frappe.new_doc("Loan Interest Accrual")
                        lia.loan = self.against_loan
                        lia.company = self.company
                        lia.posting_date = self.posting_date
                        lia.date_of_accrual = self.posting_date
                        lia.due_date = self.posting_date
                        lia.branch = self.branch
                        lia.cost_center = self.cost_center
                        lia.interest_amount = (
                            unmapped_int_remaining
                            if unmapped_int_remaining > 0
                            else 0.0
                        )
                        lia.payable_principal_amount = (
                            unmapped_prin_remaining
                            if unmapped_prin_remaining > 0
                            else 0.0
                        )
                        lia.accrual_type = "Repayment"
                        lia.insert(ignore_permissions=True)
                        lia.submit()

                        accrual_row = self.append(
                            "repayment_details",
                            {
                                "loan_interest_accrual": lia.name,
                                "paid_interest_amount": lia.interest_amount,
                                "paid_principal_amount": lia.payable_principal_amount,
                                "accrual_type": "Repayment",
                            },
                        )
                        accrual_row.db_insert()

    def add_pending_charges(self):
        self.set("pending_charges", [])
        invoices = get_outstanding_invoices(self.against_loan, self.posting_date)
        for d in invoices:
            self.append(
                "pending_charges",
                {
                    "sales_invoice": d.voucher_no,
                    "pending_charge_amount": d.outstanding_amount,
                },
            )

    def update_paid_amount(self):
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        loan_fields = [
            "total_amount_paid",
            "total_principal_paid",
            "status",
            "is_secured_loan",
            "total_payment",
            "debit_adjustment_amount",
            "credit_adjustment_amount",
            "refund_amount",
            "loan_amount",
            "disbursed_amount",
            "total_interest_payable",
            "written_off_amount",
            "applicant_type",
        ]

        # 1. Safely fetch extra tracking fields if they exist in your ERPNext version
        has_total_int_paid = frappe.db.has_column("Loan", "total_interest_paid")
        has_total_pen_paid = frappe.db.has_column("Loan", "total_penalty_paid")

        if has_total_int_paid:
            loan_fields.append("total_interest_paid")
        if has_total_pen_paid:
            loan_fields.append("total_penalty_paid")

        loan = frappe.get_value("Loan", self.against_loan, loan_fields, as_dict=1)
        loan["total_amount_paid"] = flt(loan.total_amount_paid) + flt(self.amount_paid)
        loan["total_principal_paid"] = flt(loan.total_principal_paid) + flt(
            self.principal_amount_paid
        )

        if has_total_int_paid:
            loan["total_interest_paid"] = flt(loan.total_interest_paid) + flt(
                self.total_interest_paid
            )
        if has_total_pen_paid:
            loan["total_penalty_paid"] = flt(loan.total_penalty_paid) + flt(
                self.total_penalty_paid
            )

        # ==========================================
        # FIX: Update the Accruals in the DB *FIRST*
        # ==========================================
        for payment in self.repayment_details:
            frappe.db.sql(
                """ UPDATE `tabLoan Interest Accrual`
                SET paid_principal_amount = `paid_principal_amount` + %s,
                    paid_interest_amount = `paid_interest_amount` + %s
                WHERE name = %s""",
                (
                    flt(payment.paid_principal_amount, precision),
                    flt(payment.paid_interest_amount, precision),
                    payment.loan_interest_accrual,
                ),
            )

        # ==========================================
        # CLOSURE SAFETY GUARD: Now check the DB
        # ==========================================
        pending_principal_amount = get_pending_principal_amount(loan)

        unpaid_accruals = frappe.db.sql(
            """SELECT name FROM `tabLoan Interest Accrual`
               WHERE loan = %s AND docstatus = 1
               AND (ROUND(interest_amount - paid_interest_amount, %s) > 0
                    OR ROUND(payable_principal_amount - paid_principal_amount, %s) > 0)
            """,
            (self.against_loan, precision, precision),
        )
        _, pending_penalty = get_penalty_details(self.against_loan)

        # Only close if Principal = 0 AND No Unpaid Interest AND No Unpaid Penalties
        if (
            not loan.is_secured_loan
            and pending_principal_amount <= 0
            and not unpaid_accruals
            and flt(pending_penalty, precision) <= 0
        ):
            status = (
                "Closed"
                if loan.applicant_type == "Employee"
                else "Loan Closure Requested"
            )
            loan["status"] = status

        if self.repayment_type in ("Normal Repayment", "Loan Closure"):
            update_query = "UPDATE `tabLoan` SET total_amount_paid = %s, total_principal_paid = %s, status = %s"
            update_values = [
                loan.total_amount_paid,
                loan.total_principal_paid,
                loan.status,
            ]

            if has_total_int_paid:
                update_query += ", total_interest_paid = %s"
                update_values.append(loan.total_interest_paid)
            if has_total_pen_paid:
                update_query += ", total_penalty_paid = %s"
                update_values.append(loan.total_penalty_paid)

            update_query += " WHERE name = %s"
            update_values.append(self.against_loan)

            frappe.db.sql(update_query, tuple(update_values))
            update_shortfall_status(self.against_loan, self.principal_amount_paid)

        if self.is_term_loan:
            schedule_id = frappe.db.get_value(
                "Loan Repayment Schedule",
                {"loan": self.against_loan, "docstatus": 1},
                "name",
            )
            if schedule_id:
                unaccrued_rows = frappe.db.get_all(
                    "Repayment Schedule",
                    filters={"parent": schedule_id, "is_accrued": 0},
                    fields=[
                        "name",
                        "interest_amount",
                        "principal_amount",
                        "payment_date",
                    ],
                )
                for sch in unaccrued_rows:
                    acc_int = (
                        frappe.db.get_value(
                            "Loan Interest Accrual",
                            {
                                "loan": self.against_loan,
                                "due_date": sch.payment_date,
                                "docstatus": 1,
                            },
                            "sum(interest_amount)",
                        )
                        or 0
                    )
                    acc_prin = (
                        frappe.db.get_value(
                            "Loan Interest Accrual",
                            {
                                "loan": self.against_loan,
                                "due_date": sch.payment_date,
                                "docstatus": 1,
                            },
                            "sum(payable_principal_amount)",
                        )
                        or 0
                    )

                    # If this payment fully satisfied the scheduled bucket, mark it accrued!
                    if flt(acc_int, precision) >= flt(
                        sch.interest_amount, precision
                    ) and flt(acc_prin, precision) >= flt(
                        sch.principal_amount, precision
                    ):
                        frappe.db.set_value(
                            "Repayment Schedule", sch.name, "is_accrued", 1
                        )

    def mark_as_unpaid(self):
        loan = frappe.get_value(
            "Loan",
            self.against_loan,
            [
                "total_amount_paid",
                "total_principal_paid",
                "status",
                "is_secured_loan",
                "total_payment",
                "loan_amount",
                "disbursed_amount",
                "total_interest_payable",
                "written_off_amount",
            ],
            as_dict=1,
        )

        no_of_repayments = len(self.repayment_details)

        loan.update(
            {
                "total_amount_paid": loan.total_amount_paid - self.amount_paid,
                "total_principal_paid": loan.total_principal_paid
                - self.principal_amount_paid,
            }
        )

        if loan.status == "Loan Closure Requested":
            if loan.disbursed_amount >= loan.loan_amount:
                loan["status"] = "Disbursed"
            else:
                loan["status"] = "Partially Disbursed"

        for payment in self.repayment_details:
            frappe.db.sql(
                """ UPDATE `tabLoan Interest Accrual`
                SET paid_principal_amount = `paid_principal_amount` - %s,
                    paid_interest_amount = `paid_interest_amount` - %s
                WHERE name = %s""",
                (
                    payment.paid_principal_amount,
                    payment.paid_interest_amount,
                    payment.loan_interest_accrual,
                ),
            )

            if payment.accrual_type == "Repayment" and payment.idx == no_of_repayments:
                lia_doc = frappe.get_doc(
                    "Loan Interest Accrual", payment.loan_interest_accrual
                )
                lia_doc.cancel()

        frappe.db.sql(
            """ UPDATE `tabLoan`
            SET total_amount_paid = %s, total_principal_paid = %s, status = %s
            WHERE name = %s """,
            (
                loan.total_amount_paid,
                loan.total_principal_paid,
                loan.status,
                self.against_loan,
            ),
        )

    def check_future_accruals(self):
        if self.is_term_loan:
            return

        future_accrual_date = frappe.db.get_value(
            "Loan Interest Accrual",
            {
                "due_date": (">", self.posting_date),
                "docstatus": 1,
                "loan": self.against_loan,
            },
            "due_date",
        )

        if future_accrual_date:
            frappe.throw(
                "Cannot cancel. Interest accruals already processed till {0}".format(
                    get_datetime(future_accrual_date)
                )
            )

    def update_repayment_schedule(self, cancel=0):
        if (
            self.is_term_loan
            and self.principal_amount_paid > self.payable_principal_amount
        ):
            regenerate_repayment_schedule(self.against_loan, cancel)

    def allocate_amounts(self, repayment_details):
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        self.set("repayment_details", [])
        self.principal_amount_paid = 0
        self.total_penalty_paid = 0
        self.total_interest_paid = 0  # FIX: Initialize total_interest_paid here
        interest_paid = self.amount_paid

        if self.shortfall_amount and self.amount_paid > self.shortfall_amount:
            self.principal_amount_paid = self.shortfall_amount
        elif self.shortfall_amount:
            self.principal_amount_paid = self.amount_paid

        interest_paid -= self.principal_amount_paid

        if interest_paid > 0 and not self.is_term_loan:
            if self.penalty_amount and interest_paid > self.penalty_amount:
                self.total_penalty_paid = flt(self.penalty_amount, precision)
            elif self.penalty_amount:
                self.total_penalty_paid = flt(interest_paid, precision)

            interest_paid -= self.total_penalty_paid

            interest_paid, updated_entries = self.allocate_interest_amount(
                interest_paid, repayment_details, {}
            )
            self.allocate_excess_payment_for_demand_loans(
                interest_paid, repayment_details
            )

        if self.is_term_loan:
            if self.repayment_type == "Normal Repayment":
                if self.offset_based_on_npa:
                    self.offset_repayment_based_on_npa(interest_paid, repayment_details)
                else:
                    # 1. BUCKET: Past Due Accrued Interest
                    interest_paid, updated_entries = self.allocate_interest_amount(
                        interest_paid, repayment_details
                    )

                    # 2. BUCKET: Penalty
                    interest_paid = self.allocate_penalty(interest_paid)

                    # 3. BUCKET: Charges
                    self.allocate_charges(interest_paid)
                    interest_paid -= flt(self.total_paid_charges, precision)

                    # 4. BUCKET: Past Due Accrued Principal
                    interest_paid, updated_entries = (
                        self.allocate_principal_amount_for_term_loans(
                            interest_paid, repayment_details, updated_entries
                        )
                    )

                    # 5. NEW CASCADING WATERFALL: Future Interest & Principal
                    if interest_paid > 0:
                        schedule_id = frappe.db.get_value(
                            "Loan Repayment Schedule",
                            {"loan": self.against_loan, "docstatus": 1},
                            "name",
                        )
                        next_schedules = frappe.db.get_all(
                            "Repayment Schedule",
                            filters={"parent": schedule_id},
                            fields=[
                                "interest_amount",
                                "principal_amount",
                                "payment_date",
                            ],
                            order_by="idx asc",
                        )

                        # NEW: Flag to track if we are in the repayment month vs subsequent months
                        is_first_unaccrued_row = True

                        for sch in next_schedules:
                            if interest_paid <= 0:
                                break

                            acc_int = flt(
                                frappe.db.get_value(
                                    "Loan Interest Accrual",
                                    {
                                        "loan": self.against_loan,
                                        "due_date": sch.payment_date,
                                        "docstatus": 1,
                                    },
                                    "sum(interest_amount)",
                                )
                                or 0,
                                precision,
                            )

                            acc_prin = flt(
                                frappe.db.get_value(
                                    "Loan Interest Accrual",
                                    {
                                        "loan": self.against_loan,
                                        "due_date": sch.payment_date,
                                        "docstatus": 1,
                                    },
                                    "sum(payable_principal_amount)",
                                )
                                or 0,
                                precision,
                            )

                            # FIX: Also account for what was already paid against these accruals
                            paid_int = flt(
                                frappe.db.get_value(
                                    "Loan Interest Accrual",
                                    {
                                        "loan": self.against_loan,
                                        "due_date": sch.payment_date,
                                        "docstatus": 1,
                                    },
                                    "sum(paid_interest_amount)",
                                )
                                or 0,
                                precision,
                            )

                            paid_prin = flt(
                                frappe.db.get_value(
                                    "Loan Interest Accrual",
                                    {
                                        "loan": self.against_loan,
                                        "due_date": sch.payment_date,
                                        "docstatus": 1,
                                    },
                                    "sum(paid_principal_amount)",
                                )
                                or 0,
                                precision,
                            )

                            # Pending = what schedule expects minus what's already been
                            # accrued AND paid for this due_date
                            pending_int = flt(
                                flt(sch.interest_amount, precision)
                                - max(acc_int, paid_int),
                                precision,
                            )
                            pending_prin = flt(
                                flt(sch.principal_amount, precision)
                                - max(acc_prin, paid_prin),
                                precision,
                            )

                            # Ensure non-negative
                            if pending_int < 0:
                                pending_int = 0
                            if pending_prin < 0:
                                pending_prin = 0

                            # --- THE FLIPPING PRIORITY LOGIC ---
                            if is_first_unaccrued_row:
                                # REPAYMENT MONTH: Interest First, then Principal
                                if pending_int > 0 and interest_paid > 0:
                                    alloc_int = min(pending_int, interest_paid)
                                    self.total_interest_paid += alloc_int
                                    interest_paid -= alloc_int

                                if pending_prin > 0 and interest_paid > 0:
                                    alloc_prin = min(pending_prin, interest_paid)
                                    self.principal_amount_paid += alloc_prin
                                    interest_paid -= alloc_prin
                            else:
                                # SUBSEQUENT MONTHS: Principal First, then Interest
                                if pending_prin > 0 and interest_paid > 0:
                                    alloc_prin = min(pending_prin, interest_paid)
                                    self.principal_amount_paid += alloc_prin
                                    interest_paid -= alloc_prin

                                if pending_int > 0 and interest_paid > 0:
                                    alloc_int = min(pending_int, interest_paid)
                                    self.total_interest_paid += alloc_int
                                    interest_paid -= alloc_int

                            # If this row had any pending amounts to clear, the next row is officially a subsequent month
                            if pending_int > 0 or pending_prin > 0:
                                is_first_unaccrued_row = False

                        # 6. Safety Net: Any leftover cash beyond entire schedule length goes to principal
                        if interest_paid > 0:
                            self.principal_amount_paid += interest_paid

            elif self.repayment_type in (
                "Principal Adjustment",
                "Principal Capitalization",
            ):
                self.allocate_principal_amount_for_term_loans(
                    interest_paid, repayment_details, {}
                )
            elif self.repayment_type in (
                "Interest Waiver",
                "Interest Capitalization",
                "Interest Adjustment",
                "Interest Carry Forward",
            ):
                self.allocate_interest_amount(interest_paid, repayment_details)
            elif self.repayment_type in ("Penalty Waiver", "Penalty Capitalization"):
                self.allocate_penalty(interest_paid)
            elif self.repayment_type in ("Charges Waiver", "Charges Capitalization"):
                self.allocate_charges(interest_paid)

            elif self.repayment_type == "Loan Closure":
                # =============================================================
                # LOAN CLOSURE ALLOCATION
                # =============================================================
                # For early closure, most interest is UNACCRUED (no LIA exists yet).
                # The amounts are pre-calculated by calculate_amounts with
                # payment_type="Loan Closure", which sets:
                #   interest_amount = accrued interest + unaccrued interest
                #   payable_principal_amount = pending_principal_amount (full balance)
                #
                # We must explicitly split the payment into interest vs principal
                # so that:
                #   a) make_gl_entries books interest to Interest Receivable (not Loan Account)
                #   b) book_unaccrued_interest detects the unaccrued portion and creates JIT LIA
                #   c) update_paid_amount sees correct totals for the closure guard
                # =============================================================

                # 1. Accrued Interest (from existing LIA records, if any)
                interest_paid, updated_entries = self.allocate_interest_amount(
                    interest_paid, repayment_details
                )

                # 2. Penalty
                interest_paid = self.allocate_penalty(interest_paid)

                # 3. Charges
                self.allocate_charges(interest_paid)
                interest_paid -= flt(self.total_paid_charges, precision)

                # 4. Accrued Principal (from existing LIA records, if any)
                interest_paid, updated_entries = (
                    self.allocate_principal_amount_for_term_loans(
                        interest_paid, repayment_details, updated_entries
                    )
                )

                # 5. CRITICAL: Split remaining cash into unaccrued interest + principal
                # interest_payable here = total interest (accrued + unaccrued) from calculate_amounts
                # total_interest_paid so far = only accrued interest allocated above
                # The gap = unaccrued interest that needs a JIT accrual
                remaining_interest = flt(
                    self.interest_payable - self.total_interest_paid, precision
                )

                if interest_paid > 0 and remaining_interest > 0:
                    # Allocate the unaccrued interest portion
                    unaccrued_int_alloc = min(remaining_interest, interest_paid)
                    self.total_interest_paid += unaccrued_int_alloc
                    interest_paid -= unaccrued_int_alloc

                # 6. ALL remaining cash goes to principal
                if interest_paid > 0:
                    self.principal_amount_paid += interest_paid

    def offset_repayment_based_on_npa(self, interest_paid, repayment_details):
        if interest_paid > 0:
            offset_base_on = frappe.db.get_value(
                "Company",
                self.company,
                [
                    "collection_offset_logic_based_on",
                    "days_past_due_threshold",
                    "collection_offset_sequence_for_standard_asset",
                    "collection_offset_sequence_for_sub_standard_asset",
                ],
                as_dict=1,
            )

            if (
                offset_base_on.collection_offset_logic_based_on == "NPA Flag"
                and (self.is_npa or self.manual_npa)
            ) or (
                offset_base_on.collection_offset_logic_based_on == "Days Past Due"
                and self.days_past_due > cint(offset_base_on.days_past_due_threshold)
            ):
                if (
                    offset_base_on.collection_offset_sequence_for_sub_standard_asset
                    == "PPP...III...CCC"
                ):
                    self.allocate_as_per_npa(interest_paid, repayment_details)
                else:
                    self.allocate_as_per_non_npa(interest_paid, repayment_details)
            else:
                if (
                    offset_base_on.collection_offset_sequence_for_standard_asset
                    == "IP...IP...IP...CCC"
                ):
                    self.allocate_as_per_non_npa(interest_paid, repayment_details)
                else:
                    self.allocate_as_per_npa(interest_paid, repayment_details)

    def allocate_as_per_non_npa(self, interest_paid, repayment_details):
        self.total_interest_paid = 0
        for lia, amounts in repayment_details.get(
            "pending_accrual_entries", []
        ).items():
            interest_amount = 0
            principal_amount = 0
            if amounts["interest_amount"] <= interest_paid:
                interest_amount = amounts["interest_amount"]
                interest_paid -= interest_amount
                self.total_interest_paid += amounts["interest_amount"]
                if amounts["payable_principal_amount"] <= interest_paid:
                    principal_amount = amounts["payable_principal_amount"]
                    interest_paid -= principal_amount
                    self.principal_amount_paid += principal_amount
                elif interest_paid:
                    principal_amount = interest_paid
                    interest_paid = 0
                    self.principal_amount_paid += principal_amount
            elif interest_paid:
                interest_amount = interest_paid
                interest_paid = 0
                self.total_interest_paid += interest_amount

            if interest_amount or principal_amount:
                self.append(
                    "repayment_details",
                    {
                        "loan_interest_accrual": lia,
                        "paid_principal_amount": principal_amount,
                        "paid_interest_amount": interest_amount,
                    },
                )
        interest_paid = self.allocate_penalty(interest_paid)
        self.allocate_charges(interest_paid)

    def allocate_as_per_npa(self, interest_paid, repayment_details):
        interest_paid, updated_entries = self.allocate_principal_amount_for_term_loans(
            interest_paid, repayment_details, {}
        )
        interest_paid, updated_entries = self.allocate_interest_amount(
            interest_paid, repayment_details, updated_entries
        )
        interest_paid = self.allocate_penalty(interest_paid)
        self.allocate_charges(interest_paid)

    def allocate_interest_amount(
        self, interest_paid, repayment_details, updated_entries=None
    ):
        self.total_interest_paid = 0
        idx = 1
        if not updated_entries:
            updated_entries = {}

        if interest_paid > 0:
            for lia, amounts in repayment_details.get(
                "pending_accrual_entries", []
            ).items():
                interest_amount = 0
                if amounts["interest_amount"] <= interest_paid:
                    interest_amount = amounts["interest_amount"]
                    self.total_interest_paid += interest_amount
                    interest_paid -= interest_amount
                elif interest_paid:
                    if interest_paid >= amounts["interest_amount"]:
                        interest_amount = amounts["interest_amount"]
                        self.total_interest_paid += interest_amount
                        interest_paid = 0
                    else:
                        interest_amount = interest_paid
                        self.total_interest_paid += interest_amount
                        interest_paid = 0

                if updated_entries.get(lia):
                    idx = updated_entries.get(lia)
                    self.get("repayment_details")[
                        idx - 1
                    ].paid_interest_amount += interest_amount
                if interest_amount:
                    self.append(
                        "repayment_details",
                        {
                            "loan_interest_accrual": lia,
                            "paid_interest_amount": interest_amount,
                            "paid_principal_amount": 0,
                        },
                    )
                    updated_entries[lia] = idx
                    idx += 1

        return interest_paid, updated_entries

    def allocate_principal_amount_for_term_loans(
        self, interest_paid, repayment_details, updated_entries
    ):
        if interest_paid > 0:
            for lia, amounts in repayment_details.get(
                "pending_accrual_entries", []
            ).items():
                paid_principal = 0
                if amounts["payable_principal_amount"] <= interest_paid:
                    paid_principal = amounts["payable_principal_amount"]
                    self.principal_amount_paid += paid_principal
                    interest_paid -= paid_principal
                elif interest_paid:
                    if interest_paid >= amounts["payable_principal_amount"]:
                        paid_principal = amounts["payable_principal_amount"]
                        self.principal_amount_paid += paid_principal
                        interest_paid = 0
                    else:
                        paid_principal = interest_paid
                        self.principal_amount_paid += paid_principal
                        interest_paid = 0

                if updated_entries.get(lia):
                    idx = updated_entries.get(lia)
                    self.get("repayment_details")[
                        idx - 1
                    ].paid_principal_amount += paid_principal
                else:
                    self.append(
                        "repayment_details",
                        {
                            "loan_interest_accrual": lia,
                            "paid_interest_amount": 0,
                            "paid_principal_amount": paid_principal,
                        },
                    )

        return interest_paid, updated_entries

    def allocate_penalty(self, interest_paid):
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        if interest_paid > 0:
            if self.penalty_amount and interest_paid > self.penalty_amount:
                self.total_penalty_paid = flt(self.penalty_amount, precision)
                interest_paid -= self.penalty_amount
            elif self.penalty_amount:
                self.total_penalty_paid = flt(interest_paid, precision)
                interest_paid = 0

        return interest_paid

    def allocate_charges(self, interest_paid):
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        self.total_paid_charges = 0
        if interest_paid > 0:
            for charge in self.get("pending_charges"):
                charge.allocated_amount = 0
                if (
                    charge.pending_charge_amount
                    and interest_paid > charge.pending_charge_amount
                ):
                    charge.allocated_amount = charge.pending_charge_amount
                    interest_paid -= charge.pending_charge_amount
                    self.total_paid_charges += charge.allocated_amount
                elif charge.pending_charge_amount:
                    charge.allocated_amount = interest_paid
                    interest_paid = 0
                    self.total_paid_charges += charge.allocated_amount

                charge.allocated_amount = flt(charge.allocated_amount, precision)

    def allocate_excess_payment_for_demand_loans(
        self, interest_paid, repayment_details
    ):
        if repayment_details["unaccrued_interest"] and interest_paid > 0:
            if interest_paid > repayment_details["unaccrued_interest"]:
                interest_paid -= repayment_details["unaccrued_interest"]
                self.total_interest_paid += repayment_details["unaccrued_interest"]
            else:
                per_day_interest = get_per_day_interest(
                    self.pending_principal_amount,
                    self.rate_of_interest,
                    self.company,
                    self.posting_date,
                )

                no_of_days = cint(interest_paid / per_day_interest)
                self.total_interest_paid += no_of_days * per_day_interest
                interest_paid -= no_of_days * per_day_interest

        if interest_paid > 0:
            self.principal_amount_paid += interest_paid

    def make_gl_entries(self, cancel=0, adv_adj=0):
        gle_map = []
        remarks = self.get_remarks()
        payment_account = self.get_payment_account()
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        branch = self.get("branch")

        account_details = frappe.db.get_value(
            "Loan Product",
            self.loan_product,
            [
                "interest_receivable_account",
                "suspense_interest_receivable",
                "suspense_interest_income",
                "interest_income_account",
                "penalty_receivable_account",
            ],
            as_dict=1,
        )

        if self.total_penalty_paid:
            gle_map.append(
                self.get_gl_dict(
                    {
                        "account": payment_account,
                        "against": account_details.penalty_receivable_account,
                        "debit": self.total_penalty_paid,
                        "debit_in_account_currency": self.total_penalty_paid,
                        "against_voucher_type": "Loan",
                        "against_voucher": self.against_loan,
                        "remarks": _("Penalty payment against loan:")
                        + self.against_loan,
                        "cost_center": self.cost_center,
                        "branch": branch,
                        "posting_date": getdate(self.posting_date),
                    },
                    item=self,
                )
            )

            gle_map.append(
                self.get_gl_dict(
                    {
                        "account": account_details.penalty_receivable_account,
                        "against": payment_account,
                        "credit": self.total_penalty_paid,
                        "credit_in_account_currency": self.total_penalty_paid,
                        "against_voucher_type": "Loan",
                        "against_voucher": self.against_loan,
                        "remarks": _("Penalty payment against loan:")
                        + self.against_loan,
                        "cost_center": self.cost_center,
                        "branch": branch,
                        "posting_date": getdate(self.posting_date),
                    },
                    item=self,
                )
            )

        for repayment in self.get("repayment_details"):
            if repayment.paid_interest_amount:
                gle_map.append(
                    self.get_gl_dict(
                        {
                            "account": payment_account,
                            "against": account_details.interest_receivable_account
                            + ", "
                            + getattr(self, "penalty_income_account", ""),
                            "debit": repayment.paid_interest_amount,
                            "debit_in_account_currency": repayment.paid_interest_amount,
                            "against_voucher_type": "Loan",
                            "against_voucher": self.against_loan,
                            "remarks": _(remarks),
                            "cost_center": self.cost_center,
                            "branch": branch,
                            "posting_date": getdate(self.posting_date),
                        },
                        item=self,
                    )
                )

                gle_map.append(
                    self.get_gl_dict(
                        {
                            "account": account_details.interest_receivable_account,
                            "against": payment_account,
                            "credit": repayment.paid_interest_amount,
                            "credit_in_account_currency": repayment.paid_interest_amount,
                            "against_voucher_type": "Loan",
                            "against_voucher": self.against_loan,
                            "remarks": _(remarks),
                            "cost_center": self.cost_center,
                            "branch": branch,
                            "posting_date": getdate(self.posting_date),
                        },
                        item=self,
                    )
                )

                if self.is_npa:
                    gle_map.append(
                        self.get_gl_dict(
                            {
                                "account": account_details.interest_receivable_account,
                                "against": account_details.suspense_interest_receivable,
                                "debit": repayment.paid_interest_amount,
                                "debit_in_account_currency": repayment.paid_interest_amount,
                                "against_voucher_type": "Loan",
                                "against_voucher": self.against_loan,
                                "cost_center": self.cost_center,
                                "branch": branch,
                                "posting_date": getdate(self.posting_date),
                            },
                            item=self,
                        )
                    )

                    gle_map.append(
                        self.get_gl_dict(
                            {
                                "account": account_details.suspense_interest_receivable,
                                "against": account_details.interest_receivable_account,
                                "credit": repayment.paid_interest_amount,
                                "credit_in_account_currency": repayment.paid_interest_amount,
                                "against_voucher_type": "Loan",
                                "against_voucher": self.against_loan,
                                "cost_center": self.cost_center,
                                "branch": branch,
                                "posting_date": getdate(self.posting_date),
                            },
                            item=self,
                        )
                    )

                    gle_map.append(
                        self.get_gl_dict(
                            {
                                "account": account_details.interest_income_account,
                                "credit_in_account_currency": repayment.paid_interest_amount,
                                "credit": repayment.paid_interest_amount,
                                "cost_center": self.cost_center,
                                "branch": branch,
                                "against": account_details.suspense_interest_income,
                            },
                            item=self,
                        )
                    )

                    gle_map.append(
                        self.get_gl_dict(
                            {
                                "account": account_details.suspense_interest_income,
                                "debit": repayment.paid_interest_amount,
                                "debit_in_account_currency": repayment.paid_interest_amount,
                                "cost_center": self.cost_center,
                                "branch": branch,
                                "against": account_details.interest_income_account,
                            },
                            item=self,
                        )
                    )

            if repayment.paid_principal_amount:
                gle_map.append(
                    self.get_gl_dict(
                        {
                            "account": payment_account,
                            "against": account_details.interest_receivable_account
                            + ", "
                            + getattr(self, "penalty_income_account", ""),
                            "debit": repayment.paid_principal_amount,
                            "debit_in_account_currency": repayment.paid_principal_amount,
                            "against_voucher_type": "Loan",
                            "against_voucher": self.against_loan,
                            "remarks": _(remarks),
                            "cost_center": self.cost_center,
                            "branch": branch,
                            "posting_date": getdate(self.posting_date),
                        },
                        item=self,
                    )
                )

                gle_map.append(
                    self.get_gl_dict(
                        {
                            "account": self.loan_account,
                            "against": payment_account,
                            "credit": repayment.paid_principal_amount,
                            "credit_in_account_currency": repayment.paid_principal_amount,
                            "against_voucher_type": "Loan",
                            "against_voucher": self.against_loan,
                            "remarks": _(remarks),
                            "cost_center": self.cost_center,
                            "branch": branch,
                            "posting_date": getdate(self.posting_date),
                        },
                        item=self,
                    )
                )

        if (
            not self.get("repayment_details")
            and flt(self.pending_principal_amount, precision) > 0
        ):
            # For Loan Closure, amount_paid includes interest + penalty + principal,
            # so it legitimately exceeds pending_principal_amount. Only block non-closure types.
            if self.repayment_type != "Loan Closure" and flt(
                self.amount_paid, precision
            ) > flt(self.pending_principal_amount, precision):
                frappe.throw(
                    _(
                        "The amount paid ({0}) cannot be more than the pending principal amount ({1})."
                    ).format(
                        flt(self.amount_paid, precision),
                        flt(self.pending_principal_amount, precision),
                    )
                )

            # For closure fallback, only book the principal portion to the loan account
            gl_principal = (
                flt(min(self.amount_paid, self.pending_principal_amount), precision)
                if self.repayment_type == "Loan Closure"
                else flt(self.amount_paid, precision)
            )

            gle_map.append(
                self.get_gl_dict(
                    {
                        "account": payment_account,
                        "against": self.loan_account,
                        "debit": gl_principal,
                        "debit_in_account_currency": gl_principal,
                        "against_voucher_type": "Loan",
                        "against_voucher": self.against_loan,
                        "remarks": _(remarks),
                        "cost_center": self.cost_center,
                        "branch": branch,
                        "posting_date": getdate(self.posting_date),
                    },
                    item=self,
                )
            )

            gle_map.append(
                self.get_gl_dict(
                    {
                        "account": self.loan_account,
                        "against": payment_account,
                        "credit": gl_principal,
                        "credit_in_account_currency": gl_principal,
                        "against_voucher_type": "Loan",
                        "against_voucher": self.against_loan,
                        "remarks": _(remarks),
                        "cost_center": self.cost_center,
                        "branch": branch,
                        "posting_date": getdate(self.posting_date),
                    },
                    item=self,
                )
            )
        if gle_map:
            make_gl_entries(
                gle_map, cancel=cancel, adv_adj=adv_adj, merge_entries=False
            )

    def get_payment_account(self):
        payment_account_field_map = {
            "Interest Waiver": "interest_waiver_account",
            "Penalty Waiver": "penalty_waiver_account",
            "Charges Waiver": "charges_waiver_account",
            "Principal Capitalization": "loan_account",
            "Interest Capitalization": "loan_account",
            "Charges Capitalization": "loan_account",
            "Penalty Capitalization": "loan_account",
            "Principal Adjustment": "security_deposit_account",
            "Interest Adjustment": "security_deposit_account",
            "Interest Carry Forward": "interest_income_account",
        }

        if self.repayment_type in ("Normal Repayment", "Loan Closure"):
            # Loan Closure uses the same payment account as Normal Repayment
            # (the bank account where money is received)
            if hasattr(self, "repay_from_salary") and self.repay_from_salary:
                payment_account = self.payroll_payable_account
            else:
                payment_account = self.payment_account
        else:
            payment_account = frappe.db.get_value(
                "Loan Product",
                self.loan_product,
                payment_account_field_map.get(self.repayment_type),
            )

        return payment_account

    def get_remarks(self):
        if self.shortfall_amount and self.amount_paid > self.shortfall_amount:
            remarks = (
                "Shortfall repayment of {0}.<br>Repayment against loan {1}".format(
                    self.shortfall_amount, self.against_loan
                )
            )
        elif self.shortfall_amount:
            remarks = "Shortfall repayment of {0} against loan {1}".format(
                self.shortfall_amount, self.against_loan
            )
        else:
            remarks = "Repayment against loan " + self.against_loan

        if self.reference_number:
            remarks += " with reference no. {}".format(self.reference_number)

        return remarks


def create_repayment_entry(
    loan,
    applicant,
    company,
    posting_date,
    loan_product,
    payment_type,
    interest_payable,
    payable_principal_amount,
    amount_paid,
    penalty_amount=None,
    payroll_payable_account=None,
    process_payroll_accounting_entry_based_on_employee=0,
):
    lr = frappe.get_doc(
        {
            "doctype": "Loan Repayment",
            "against_loan": loan,
            "payment_type": payment_type,
            "company": company,
            "posting_date": posting_date,
            "applicant": applicant,
            "penalty_amount": penalty_amount,
            "interest_payable": interest_payable,
            "payable_principal_amount": payable_principal_amount,
            "amount_paid": amount_paid,
            "loan_product": loan_product,
            "payroll_payable_account": payroll_payable_account,
            "process_payroll_accounting_entry_based_on_employee": process_payroll_accounting_entry_based_on_employee,
        }
    ).insert()

    return lr


def get_accrued_interest_entries(against_loan, posting_date=None):
    if not posting_date:
        posting_date = getdate()

    precision = cint(frappe.db.get_default("currency_precision")) or 2

    unpaid_accrued_entries = frappe.db.sql(
        """
            SELECT name, due_date, interest_amount - paid_interest_amount as interest_amount,
                payable_principal_amount - paid_principal_amount as payable_principal_amount,
                accrual_type
            FROM
                `tabLoan Interest Accrual`
            WHERE
                loan = %s
            AND due_date <= %s
            AND (interest_amount - paid_interest_amount > 0 OR
                payable_principal_amount - paid_principal_amount > 0)
            AND
                docstatus = 1
            ORDER BY due_date
        """,
        (against_loan, posting_date),
        as_dict=1,
    )

    unpaid_accrued_entries = [
        d
        for d in unpaid_accrued_entries
        if flt(d.interest_amount, precision) > 0
        or flt(d.payable_principal_amount, precision) > 0
    ]

    return unpaid_accrued_entries


def get_penalty_details(against_loan):
    penalty_details = frappe.db.sql(
        """
        SELECT posting_date, sum(penalty_amount - total_penalty_paid) as pending_penalty_amount
        FROM `tabLoan Repayment` where posting_date >= (SELECT MAX(posting_date) from `tabLoan Repayment`
        where against_loan = %s) and docstatus = 1 and against_loan = %s
    """,
        (against_loan, against_loan),
    )

    if penalty_details:
        return penalty_details[0][0], flt(penalty_details[0][1])
    else:
        return None, 0


def regenerate_repayment_schedule(loan, cancel=0):
    from lending.loan_management.doctype.loan.loan import (
        add_single_month,
        get_monthly_repayment_amount,
    )

    precision = cint(frappe.db.get_default("currency_precision")) or 2
    loan_doc = frappe.get_doc("Loan", loan)
    next_accrual_date = None
    accrued_entries = 0
    last_repayment_amount = None
    last_balance_amount = None

    for term in reversed(loan_doc.get("repayment_schedule")):
        if not term.is_accrued:
            next_accrual_date = term.payment_date
            loan_doc.remove(term)
        else:
            accrued_entries += 1
            if last_repayment_amount is None:
                last_repayment_amount = term.total_payment
            if last_balance_amount is None:
                last_balance_amount = term.balance_loan_amount

    loan_doc.save()

    balance_amount = get_pending_principal_amount(loan_doc)

    if loan_doc.repayment_method == "Repay Fixed Amount per Period":
        monthly_repayment_amount = flt(
            balance_amount / len(loan_doc.get("repayment_schedule")) - accrued_entries
        )
    else:
        repayment_period = loan_doc.repayment_periods - accrued_entries
        if not cancel and repayment_period > 0:
            monthly_repayment_amount = get_monthly_repayment_amount(
                balance_amount, loan_doc.rate_of_interest, repayment_period
            )
        else:
            monthly_repayment_amount = last_repayment_amount
            balance_amount = last_balance_amount

    payment_date = next_accrual_date

    while flt(balance_amount, precision) > 0:
        interest_amount = flt(
            balance_amount * flt(loan_doc.rate_of_interest) / (12 * 100)
        )
        principal_amount = monthly_repayment_amount - interest_amount
        balance_amount = flt(
            balance_amount + interest_amount - monthly_repayment_amount
        )
        if balance_amount < 0:
            principal_amount += balance_amount
            balance_amount = 0.0

        total_payment = principal_amount + interest_amount
        loan_doc.append(
            "repayment_schedule",
            {
                "payment_date": payment_date,
                "principal_amount": principal_amount,
                "interest_amount": interest_amount,
                "total_payment": total_payment,
                "balance_loan_amount": balance_amount,
            },
        )
        next_payment_date = add_single_month(payment_date)
        payment_date = next_payment_date

    loan_doc.save()


def get_pending_principal_amount(loan):
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    if (
        loan.status in ("Disbursed", "Closed")
        or loan.disbursed_amount >= loan.loan_amount
    ):
        pending_principal_amount = (
            flt(loan.total_payment)
            + flt(loan.debit_adjustment_amount)
            - flt(loan.credit_adjustment_amount)
            - flt(loan.total_principal_paid)
            - flt(loan.total_interest_payable)
            - flt(loan.written_off_amount)
            + flt(loan.refund_amount)
        )
    else:
        pending_principal_amount = (
            flt(loan.disbursed_amount)
            + flt(loan.debit_adjustment_amount)
            - flt(loan.credit_adjustment_amount)
            - flt(loan.total_principal_paid)
            - flt(loan.total_interest_payable)
            - flt(loan.written_off_amount)
            + flt(loan.refund_amount)
        )

    return flt(pending_principal_amount, precision)


def get_amounts(amounts, against_loan, posting_date, with_loan_details=False):
    precision = cint(frappe.db.get_default("currency_precision")) or 2

    against_loan_doc = frappe.get_doc("Loan", against_loan)
    loan_product_details = frappe.get_doc("Loan Product", against_loan_doc.loan_product)

    payable_principal_amount = 0.0
    total_pending_interest = 0.0
    penalty_amount = 0.0
    unaccrued_interest = 0.0
    final_due_date = ""

    pending_principal_amount = get_pending_principal_amount(against_loan_doc)
    pending_accrual_entries = {}

    if against_loan_doc.is_term_loan:
        total_prin_paid = flt(against_loan_doc.total_principal_paid, precision)
        total_int_paid = flt(against_loan_doc.total_interest_paid, precision)

        total_pen_paid = flt(
            frappe.db.get_value(
                "Loan Repayment",
                {"against_loan": against_loan, "docstatus": 1},
                "sum(total_penalty_paid)",
            ),
            precision,
        )

        schedule_id = frappe.db.get_value(
            "Loan Repayment Schedule",
            {"loan": against_loan, "docstatus": 1, "status": "Active"},
            "name",
        )

        if schedule_id:
            schedules = frappe.db.get_all(
                "Repayment Schedule",
                filters={"parent": schedule_id},
                fields=["name", "interest_amount", "principal_amount", "payment_date"],
                order_by="idx asc",
            )

            cumulative_prin = 0.0
            cumulative_int = 0.0

            for sch in schedules:
                if getdate(sch.payment_date) > getdate(posting_date):
                    break

                cumulative_prin += flt(sch.principal_amount, precision)
                cumulative_int += flt(sch.interest_amount, precision)
                final_due_date = sch.payment_date

            payable_principal_amount = flt(cumulative_prin - total_prin_paid, precision)
            if payable_principal_amount < 0:
                payable_principal_amount = 0.0

            total_pending_interest = flt(cumulative_int - total_int_paid, precision)
            if total_pending_interest < 0:
                total_pending_interest = 0.0

            daily_penalty_rate = flt(loan_product_details.penalty_interest_rate)
            grace_period = cint(loan_product_details.grace_period_in_days)

            billed_penalty = flt(
                frappe.db.get_value(
                    "Loan Interest Accrual",
                    {
                        "loan": against_loan,
                        "docstatus": 1,
                        "accrual_type": "Penalty",
                        "penalty_amount": [">", 0],
                    },
                    "sum(penalty_amount)",
                )
            )

            last_penalty_date = frappe.db.get_value(
                "Loan Interest Accrual",
                {
                    "loan": against_loan,
                    "docstatus": 1,
                    "accrual_type": "Penalty",
                    "penalty_amount": [">", 0],
                },
                "max(posting_date)",
            )

            unbilled_penalty = 0.0

            if daily_penalty_rate > 0:
                penalty_start_date = add_days(schedules[0].payment_date, grace_period)
                if last_penalty_date:
                    penalty_start_date = add_days(last_penalty_date, 1)

                days_to_check = date_diff(posting_date, penalty_start_date)
                if days_to_check >= 0:
                    remaining_paid_prin = total_prin_paid
                    for sch in schedules:
                        if remaining_paid_prin >= flt(sch.principal_amount, precision):
                            sch.unpaid_prin = 0.0
                            remaining_paid_prin -= flt(sch.principal_amount, precision)
                        else:
                            sch.unpaid_prin = (
                                flt(sch.principal_amount, precision)
                                - remaining_paid_prin
                            )
                            remaining_paid_prin = 0.0

                    # Walk forward day-by-day calculating unbilled penalty
                    for i in range(days_to_check + 1):
                        eval_date = add_days(penalty_start_date, i)
                        overdue_principal_total = 0.0

                        for sch in schedules:
                            due_date_with_grace = add_days(
                                sch.payment_date, grace_period
                            )
                            if date_diff(eval_date, due_date_with_grace) >= 0:
                                if getattr(sch, "unpaid_prin", 0.0) > 0:
                                    overdue_principal_total += sch.unpaid_prin

                        if overdue_principal_total > 0:
                            unbilled_penalty += (
                                overdue_principal_total * daily_penalty_rate
                            )

            total_penalty_owed = billed_penalty + unbilled_penalty
            penalty_amount = flt(total_penalty_owed - total_pen_paid, precision)

            if penalty_amount < 0:
                penalty_amount = 0.0

            unpaid_lias = get_accrued_interest_entries(against_loan, posting_date)
            for entry in unpaid_lias:
                pending_accrual_entries[entry.name] = {
                    "interest_amount": flt(entry.interest_amount, precision),
                    "payable_principal_amount": flt(
                        entry.payable_principal_amount, precision
                    ),
                }

            last_accrued_date = None
            if final_due_date:
                last_accrued_date = getdate(final_due_date)
            else:
                last_accrued_date = getdate(
                    against_loan_doc.disbursement_date or against_loan_doc.posting_date
                )

            if last_accrued_date and getdate(posting_date) > last_accrued_date:
                pending_days = date_diff(posting_date, last_accrued_date)
                # frappe.msgprint(str(pending_days))
                if pending_days > 0 and pending_principal_amount > 0:
                    unaccrued_interest = flt(
                        pending_days
                        * get_per_day_interest(
                            flt(pending_principal_amount, precision),
                            loan_product_details.rate_of_interest,
                            against_loan_doc.company,
                            posting_date,
                        ),
                        precision,
                    )

    else:
        # * Unused code because we are using demand loans
        accrued_interest_entries = get_accrued_interest_entries(
            against_loan_doc.name, posting_date
        )
        computed_penalty_date, pending_penalty_amount = get_penalty_details(
            against_loan
        )
        last_entry_due_date = None
        for entry in accrued_interest_entries:
            due_date_after_grace_period = add_days(
                entry.due_date, loan_product_details.grace_period_in_days
            )
            if computed_penalty_date and getdate(computed_penalty_date) >= getdate(
                due_date_after_grace_period
            ):
                due_date_after_grace_period = computed_penalty_date

            no_of_late_days = date_diff(posting_date, due_date_after_grace_period)

            if (
                no_of_late_days > 0
                and entry.accrual_type == "Regular"
                and not getattr(against_loan_doc, "repay_from_salary", False)
            ):
                penalty_amount += (
                    flt(pending_principal_amount, precision)
                    * (loan_product_details.penalty_interest_rate / 100)
                    * no_of_late_days
                ) / 365

            total_pending_interest += entry.interest_amount
            payable_principal_amount += entry.payable_principal_amount

            pending_accrual_entries.setdefault(
                entry.name,
                {
                    "interest_amount": flt(entry.interest_amount, precision),
                    "payable_principal_amount": flt(
                        entry.payable_principal_amount, precision
                    ),
                },
            )
            last_entry_due_date = entry.due_date
            if entry.due_date and not final_due_date:
                final_due_date = add_days(
                    entry.due_date, loan_product_details.grace_period_in_days
                )

        if last_entry_due_date:
            pending_days = date_diff(posting_date, last_entry_due_date)
            if pending_days > 0:
                unaccrued_interest += pending_days * get_per_day_interest(
                    flt(pending_principal_amount, precision),
                    loan_product_details.rate_of_interest,
                    loan_product_details.company,
                    posting_date,
                )

        penalty_amount += pending_penalty_amount

    amounts["pending_principal_amount"] = flt(pending_principal_amount, precision)
    amounts["payable_principal_amount"] = flt(payable_principal_amount, precision)
    amounts["interest_amount"] = flt(total_pending_interest, precision)
    amounts["penalty_amount"] = flt(penalty_amount, precision)

    amounts["payable_amount"] = flt(
        payable_principal_amount
        + total_pending_interest
        + penalty_amount
        + unaccrued_interest,
        precision,
    )

    amounts["pending_accrual_entries"] = pending_accrual_entries
    amounts["unaccrued_interest"] = flt(unaccrued_interest, precision)
    amounts["written_off_amount"] = flt(against_loan_doc.written_off_amount, precision)

    if final_due_date:
        amounts["due_date"] = final_due_date

    if with_loan_details:
        return amounts, against_loan_doc.as_dict()
    else:
        return amounts


@frappe.whitelist()
def calculate_amounts(
    against_loan, posting_date, payment_type="", with_loan_details=False
):
    amounts = {
        "penalty_amount": 0.0,
        "interest_amount": 0.0,
        "pending_principal_amount": 0.0,
        "payable_principal_amount": 0.0,
        "payable_amount": 0.0,
        "unaccrued_interest": 0.0,
        "due_date": "",
        "total_charges_payable": 0.0,
        "available_security_deposit": 0.0,
    }

    if with_loan_details:
        amounts, loan_details = get_amounts(
            amounts, against_loan, posting_date, with_loan_details
        )
    else:
        amounts = get_amounts(amounts, against_loan, posting_date)

    charges = []
    invoices = get_outstanding_invoices(against_loan, posting_date)
    for d in invoices:
        charges.append(
            {
                "sales_invoice": d.voucher_no,
                "pending_charge_amount": d.outstanding_amount,
            }
        )
        amounts["total_charges_payable"] += d.outstanding_amount

    amounts["charges"] = charges
    amounts["payable_amount"] += amounts["total_charges_payable"]
    amounts["available_security_deposit"] = frappe.db.get_value(
        "Loan Security Deposit",
        {"loan": against_loan},
        "sum(deposit_amount - allocated_amount)",
    )

    if payment_type == "Loan Closure":
        amounts["payable_principal_amount"] = amounts["pending_principal_amount"]
        amounts["interest_amount"] += amounts["unaccrued_interest"]
        amounts["payable_amount"] = (
            amounts["payable_principal_amount"]
            + amounts["interest_amount"]
            + amounts["penalty_amount"]
        )

    if with_loan_details:
        return {"amounts": amounts, "loan_details": loan_details}
    else:
        return amounts


def get_outstanding_invoices(loan, posting_date):
    return frappe.db.get_all(
        "Sales Invoice",
        filters={
            "loan": loan,
            "outstanding_amount": ("!=", 0),
            "docstatus": 1,
            "due_date": ("<=", posting_date),
        },
        fields=["name as voucher_no", "outstanding_amount"],
    )
