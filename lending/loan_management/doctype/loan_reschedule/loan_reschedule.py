# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe import _
from frappe.utils import add_months, cint, date_diff, flt, getdate, today, add_days

from lending.loan_management.doctype.loan_repayment_schedule.loan_repayment_schedule import (
    get_monthly_repayment_amount,
    add_single_month,
)


class LoanReschedule(Document):
    def validate(self):
        self.validate_loan_eligibility()
        self.snapshot_outstanding_balances()
        self.compute_new_principal()
        self.generate_schedule_preview()

    def on_submit(self):
        self.execute_reschedule()

    def on_cancel(self):
        self.reverse_reschedule()

    def validate_loan_eligibility(self):
        """Ensure the loan is actually eligible for rescheduling."""
        loan = frappe.get_doc("Loan", self.loan)

        if loan.status == "Closed":
            frappe.throw(_("Cannot reschedule a closed loan."))

        if not loan.is_term_loan:
            frappe.throw(_("Rescheduling is only supported for term loans."))

        # Check for existing active reschedule (prevent double-reschedule)
        existing = frappe.db.exists(
            "Loan Reschedule",
            {
                "loan": self.loan,
                "docstatus": 1,
                "name": ["!=", self.name],
            },
        )
        if existing:
            frappe.throw(
                _(
                    "Loan {0} already has an active reschedule: {1}. Cancel it first."
                ).format(self.loan, existing)
            )

        # Find the active schedule
        schedule_id = frappe.db.get_value(
            "Loan Repayment Schedule",
            {"loan": self.loan, "docstatus": 1},
            "name",
        )
        if not schedule_id:
            frappe.throw(
                _("No active repayment schedule found for loan {0}.").format(self.loan)
            )

        self.old_repayment_schedule = schedule_id

    def snapshot_outstanding_balances(self):
        """Capture exactly what's owed right now — principal, interest, penalty."""
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        loan = frappe.get_doc("Loan", self.loan)

        # 1. Outstanding principal — from loan doc
        self.outstanding_principal = flt(get_pending_principal_amount(loan), precision)

        # 2. Outstanding interest — sum of unpaid interest from LIA records
        total_interest_accrued = flt(
            frappe.db.get_value(
                "Loan Interest Accrual",
                {"loan": self.loan, "docstatus": 1, "accrual_type": ["!=", "Penalty"]},
                "sum(interest_amount)",
            )
            or 0,
            precision,
        )
        total_interest_paid = flt(
            frappe.db.get_value(
                "Loan Interest Accrual",
                {"loan": self.loan, "docstatus": 1, "accrual_type": ["!=", "Penalty"]},
                "sum(paid_interest_amount)",
            )
            or 0,
            precision,
        )
        self.outstanding_interest = flt(
            total_interest_accrued - total_interest_paid, precision
        )
        if self.outstanding_interest < 0:
            self.outstanding_interest = 0

        # 3. Outstanding penalty — sum of unpaid penalty from LIA records
        total_penalty_accrued = flt(
            frappe.db.get_value(
                "Loan Interest Accrual",
                {"loan": self.loan, "docstatus": 1, "accrual_type": "Penalty"},
                "sum(penalty_amount)",
            )
            or 0,
            precision,
        )
        total_penalty_paid = flt(
            frappe.db.get_value(
                "Loan Repayment",
                {"against_loan": self.loan, "docstatus": 1},
                "sum(total_penalty_paid)",
            )
            or 0,
            precision,
        )
        self.outstanding_penalty = flt(
            total_penalty_accrued - total_penalty_paid, precision
        )
        if self.outstanding_penalty < 0:
            self.outstanding_penalty = 0

    def compute_new_principal(self):
        """Capitalize interest and penalty into new principal."""
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        self.capitalized_interest = flt(self.outstanding_interest, precision)
        self.capitalized_penalty = flt(self.outstanding_penalty, precision)

        self.new_principal = flt(
            self.outstanding_principal
            + self.capitalized_interest
            + self.capitalized_penalty,
            precision,
        )

    def generate_schedule_preview(self):
        """Build the amortization table for preview before submission."""
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        self.set("schedule_preview", [])

        balance = flt(self.new_principal, precision)
        rate = flt(self.new_rate_of_interest)
        periods = cint(self.new_repayment_periods)
        start_date = getdate(self.new_repayment_start_date)

        if balance <= 0 or periods <= 0:
            return

        if self.new_repayment_method == "Repay Fixed Amount per Period":
            if (
                not self.new_monthly_repayment_amount
                or self.new_monthly_repayment_amount <= 0
            ):
                frappe.throw(_("Monthly repayment amount must be greater than zero."))
            monthly_repayment = flt(self.new_monthly_repayment_amount, precision)
        else:
            monthly_repayment = flt(
                get_monthly_repayment_amount(balance, rate, periods), precision
            )

        payment_date = start_date

        for i in range(periods):
            interest_amount = flt(balance * rate / (12 * 100), precision)
            principal_amount = flt(monthly_repayment - interest_amount, precision)

            # Last row adjustment
            if principal_amount > balance:
                principal_amount = balance

            balance = flt(balance - principal_amount, precision)
            if balance < 0:
                principal_amount += balance
                balance = 0.0

            total_payment = flt(principal_amount + interest_amount, precision)

            self.append(
                "schedule_preview",
                {
                    "payment_date": payment_date,
                    "principal_amount": principal_amount,
                    "interest_amount": interest_amount,
                    "total_payment": total_payment,
                    "balance_loan_amount": balance,
                },
            )

            payment_date = add_single_month(payment_date)

            if balance <= 0:
                break

    def execute_reschedule(self):
        """The main operation — expire old schedule, create new one, update loan."""
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        # ===================================
        # STEP 1: Cancel the old schedule
        # ===================================
        old_schedule = frappe.get_doc(
            "Loan Repayment Schedule", self.old_repayment_schedule
        )
        old_schedule.flags.ignore_permissions = True
        old_schedule.cancel()

        # ===================================
        # STEP 2: Create capitalization entries
        # ===================================
        # Create adjustment LIAs to mark the outstanding interest/penalty as "settled"
        # (they're being rolled into principal, not actually paid)
        if self.capitalized_interest > 0:
            self._create_capitalization_lia(
                interest_amount=self.capitalized_interest,
                penalty_amount=0,
                label="Interest Capitalization",
            )

        if self.capitalized_penalty > 0:
            self._create_capitalization_lia(
                interest_amount=0,
                penalty_amount=self.capitalized_penalty,
                label="Penalty Capitalization",
            )

        # ===================================
        # STEP 3: Create new repayment schedule
        # ===================================
        new_schedule = frappe.new_doc("Loan Repayment Schedule")
        new_schedule.loan = self.loan
        new_schedule.loan_product = self.loan_product
        new_schedule.repayment_method = self.new_repayment_method
        new_schedule.repayment_periods = self.new_repayment_periods
        new_schedule.rate_of_interest = self.new_rate_of_interest
        new_schedule.repayment_start_date = self.new_repayment_start_date

        if self.new_repayment_method == "Repay Fixed Amount per Period":
            new_schedule.monthly_repayment_amount = self.new_monthly_repayment_amount

        # Copy rows from preview into the actual schedule
        for row in self.get("schedule_preview"):
            new_schedule.append(
                "repayment_schedule",
                {
                    "payment_date": row.payment_date,
                    "principal_amount": row.principal_amount,
                    "interest_amount": row.interest_amount,
                    "total_payment": row.total_payment,
                    "balance_loan_amount": row.balance_loan_amount,
                    "is_accrued": 0,
                },
            )

        new_schedule.flags.ignore_permissions = True
        new_schedule.insert()
        new_schedule.submit()

        self.db_set("new_repayment_schedule", new_schedule.name)

        # ===================================
        # STEP 4: Update the Loan document
        # ===================================
        new_total_payment = sum(
            flt(r.total_payment, precision) for r in self.get("schedule_preview")
        )
        new_total_interest = sum(
            flt(r.interest_amount, precision) for r in self.get("schedule_preview")
        )

        loan_update = {
            "total_payment": flt(new_total_payment, precision),
            "total_interest_payable": flt(new_total_interest, precision),
            "rate_of_interest": self.new_rate_of_interest,
            "repayment_periods": self.new_repayment_periods,
            "repayment_method": self.new_repayment_method,
            "loan_amount": flt(self.new_principal, precision),
            "disbursed_amount": flt(self.new_principal, precision),
            "total_principal_paid": 0,
            "total_amount_paid": 0,
            "status": "Disbursed",
            "repayment_status": "Regular",
            "overdue_days": 0,
        }

        # Reset interest/penalty paid tracking if those columns exist
        if frappe.db.has_column("Loan", "total_interest_paid"):
            loan_update["total_interest_paid"] = 0
        if frappe.db.has_column("Loan", "total_penalty_paid"):
            loan_update["total_penalty_paid"] = 0

        if self.new_repayment_method == "Repay Fixed Amount per Period":
            loan_update["monthly_repayment_amount"] = self.new_monthly_repayment_amount

        frappe.db.set_value("Loan", self.loan, loan_update)

        frappe.msgprint(
            _("Loan {0} has been rescheduled successfully. New schedule: {1}").format(
                self.loan, new_schedule.name
            ),
            alert=True,
            indicator="green",
        )

    def _create_capitalization_lia(self, interest_amount, penalty_amount, label):
        """Create an LIA that marks outstanding amounts as 'settled via capitalization'.

        These are fully-paid-on-creation LIAs — the interest/penalty is not being
        paid with cash, it's being absorbed into the new principal. We create them
        with paid_amount = accrued_amount so they don't show as outstanding.
        """
        lia = frappe.new_doc("Loan Interest Accrual")
        lia.loan = self.loan
        lia.company = self.company
        lia.posting_date = self.posting_date
        lia.date_of_accrual = self.posting_date
        lia.due_date = self.posting_date
        lia.interest_amount = flt(interest_amount)
        lia.paid_interest_amount = flt(interest_amount)  # Fully settled
        lia.payable_principal_amount = 0
        lia.paid_principal_amount = 0
        lia.penalty_amount = flt(penalty_amount)
        lia.accrual_type = "Capitalization"
        lia.flags.ignore_permissions = True
        lia.insert()
        lia.submit()

    def reverse_reschedule(self):
        """Undo the reschedule — restore old schedule, cancel new one."""

        # 1. Cancel the new schedule if it exists
        if self.new_repayment_schedule:
            new_schedule = frappe.get_doc(
                "Loan Repayment Schedule", self.new_repayment_schedule
            )
            if new_schedule.docstatus == 1:
                new_schedule.flags.ignore_permissions = True
                new_schedule.cancel()

        # 2. Restore (amend) the old schedule
        if self.old_repayment_schedule:
            old_schedule = frappe.get_doc(
                "Loan Repayment Schedule", self.old_repayment_schedule
            )
            if old_schedule.docstatus == 2:
                # Re-create from the cancelled version
                amended = frappe.copy_doc(old_schedule)
                amended.amended_from = old_schedule.name
                amended.flags.ignore_permissions = True
                amended.insert()
                amended.submit()

        # 3. Cancel capitalization LIAs created by this reschedule
        cap_lias = frappe.get_all(
            "Loan Interest Accrual",
            filters={
                "loan": self.loan,
                "posting_date": self.posting_date,
                "accrual_type": "Capitalization",
                "docstatus": 1,
            },
            fields=["name"],
        )
        for lia in cap_lias:
            lia_doc = frappe.get_doc("Loan Interest Accrual", lia.name)
            lia_doc.flags.ignore_permissions = True
            lia_doc.cancel()

        # 4. Restore original loan values
        # We need to recalculate from the restored schedule
        loan = frappe.get_doc("Loan", self.loan)
        restored_schedule_id = frappe.db.get_value(
            "Loan Repayment Schedule",
            {"loan": self.loan, "docstatus": 1},
            "name",
        )

        if restored_schedule_id:
            rows = frappe.get_all(
                "Repayment Schedule",
                filters={"parent": restored_schedule_id},
                fields=["principal_amount", "interest_amount", "total_payment"],
            )
            original_total_payment = sum(flt(r.total_payment) for r in rows)
            original_total_interest = sum(flt(r.interest_amount) for r in rows)

            frappe.db.set_value(
                "Loan",
                self.loan,
                {
                    "total_payment": original_total_payment,
                    "total_interest_payable": original_total_interest,
                    "status": "Disbursed",
                    "repayment_status": "Past Maturity",
                },
            )

        frappe.msgprint(
            _("Reschedule for loan {0} has been reversed.").format(self.loan),
            alert=True,
            indicator="orange",
        )


def get_pending_principal_amount(loan):
    """Reuse the same logic from loan_repayment.py"""
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    if (
        loan.status in ("Disbursed", "Closed")
        or loan.disbursed_amount >= loan.loan_amount
    ):
        pending = (
            flt(loan.total_payment)
            + flt(loan.debit_adjustment_amount)
            - flt(loan.credit_adjustment_amount)
            - flt(loan.total_principal_paid)
            - flt(loan.total_interest_payable)
            - flt(loan.written_off_amount)
            + flt(loan.refund_amount)
        )
    else:
        pending = (
            flt(loan.disbursed_amount)
            + flt(loan.debit_adjustment_amount)
            - flt(loan.credit_adjustment_amount)
            - flt(loan.total_principal_paid)
            - flt(loan.total_interest_payable)
            - flt(loan.written_off_amount)
            + flt(loan.refund_amount)
        )
    return flt(pending, precision)


# =============================================================================
# WHITELISTED API: Called from the Loan form button
# =============================================================================
@frappe.whitelist()
def create_loan_reschedule(loan_name):
    """Create a draft Loan Reschedule document from a Loan.
    Called by the 'Reschedule Loan' button on the Loan form.
    """
    loan = frappe.get_doc("Loan", loan_name)

    if loan.status == "Closed":
        frappe.throw(_("Cannot reschedule a closed loan."))

    if not loan.is_term_loan:
        frappe.throw(_("Rescheduling is only supported for term loans."))

    # Pre-fill with current loan terms as defaults
    rsc = frappe.new_doc("Loan Reschedule")
    rsc.loan = loan.name
    rsc.applicant_type = loan.applicant_type
    rsc.applicant = loan.applicant
    rsc.company = loan.company
    rsc.loan_product = loan.loan_product
    rsc.posting_date = today()

    # Default new terms from current loan
    rsc.new_rate_of_interest = loan.rate_of_interest
    rsc.new_repayment_periods = 12  # Default to 12 months, user can adjust
    rsc.new_repayment_start_date = add_months(getdate(today()), 1)
    rsc.new_repayment_method = loan.repayment_method or "Repay Over Number of Periods"

    rsc.insert()

    return rsc.name


# =============================================================================
# BATCH UTILITY: Find all loans eligible for rescheduling
# =============================================================================
@frappe.whitelist()
def get_loans_eligible_for_reschedule():
    """Return loans that are past maturity + penalty_stop_threshold_days."""
    current_date = getdate(today())

    eligible_loans = []

    active_loans = frappe.get_all(
        "Loan",
        filters={"docstatus": 1, "status": "Disbursed", "is_term_loan": 1},
        fields=[
            "name",
            "applicant",
            "loan_amount",
            "disbursed_amount",
            "total_principal_paid",
            "penalty_stop_threshold_days",
            "repayment_status",
            "loan_product",
        ],
    )

    for loan in active_loans:
        # Find the maturity date from the schedule
        schedule_id = frappe.db.get_value(
            "Loan Repayment Schedule",
            {"loan": loan.name, "docstatus": 1},
            "name",
        )
        if not schedule_id:
            continue

        last_payment_date = frappe.db.get_value(
            "Repayment Schedule",
            {"parent": schedule_id},
            "max(payment_date)",
        )
        if not last_payment_date:
            continue

        maturity_date = getdate(last_payment_date)
        threshold = cint(loan.penalty_stop_threshold_days)
        cutoff_date = (
            add_days(maturity_date, threshold) if threshold > 0 else maturity_date
        )

        if current_date > getdate(cutoff_date):
            loan["maturity_date"] = maturity_date
            loan["days_past_maturity"] = date_diff(current_date, maturity_date)
            eligible_loans.append(loan)

    return eligible_loans
