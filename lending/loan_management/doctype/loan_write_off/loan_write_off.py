# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
#
# ── WRITE-OFF RESTRUCTURE ────────────────────────────────────────────────────
#
# PROBLEM with the original implementation
# ─────────────────────────────────────────
# 1. make_gl_entries() only posted ONE credit entry to the Loan Account
#    (principal receivable).  Outstanding interest and penalty sitting in
#    Interest Receivable and Penalty Receivable were NEVER cleared.
#    After write-off those accounts retained stale balances on the books.
#
# 2. write_off_amount was validated against get_pending_principal_amount()
#    which is a combined P+I figure despite being named "principal".
#    No separate tracking of how much was principal, interest, or penalty.
#
# 3. LIA records (Loan Interest Accrual) were never marked as settled after
#    write-off, leaving unpaid interest accruals as open items.
#
# STRUCTURED WRITE-OFF — priority order: Penalty → Interest → Principal
# ───────────────────────────────────────────────────────────────────────
# This follows the same waterfall as normal repayment collection so that
# the GL correctly reflects which type of receivable is extinguished.
#
# GL entries produced on submit:
#   DR  Write Off Expense account    full write_off_amount
#   CR  Penalty Receivable           penalty portion   (if > 0)
#   CR  Interest Receivable          interest portion  (if > 0)
#   CR  Loan Account (principal)     principal portion (remainder)
#
# New doctype fields required (add via Customize Form):
#   write_off_penalty_amount   Currency  (read-only, auto-calculated)
#   write_off_interest_amount  Currency  (read-only, auto-calculated)
#   write_off_principal_amount Currency  (read-only, auto-calculated)
# ─────────────────────────────────────────────────────────────────────────────

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

import erpnext
from erpnext.accounts.general_ledger import make_gl_entries
from erpnext.controllers.accounts_controller import AccountsController

from lending.loan_management.doctype.loan_repayment.loan_repayment import (
    get_pending_principal_amount,
)


class LoanWriteOff(AccountsController):
    def validate(self):
        self.set_missing_values()
        self.validate_write_off_amount()

    def set_missing_values(self):
        if not self.cost_center:
            self.cost_center = erpnext.get_default_cost_center(self.company)

        # ── Compute the three-way split ───────────────────────────────────────
        # Always recalculate so the breakdown stays in sync with write_off_amount.
        self._compute_write_off_split()

    def _compute_write_off_split(self):
        """
        Split write_off_amount into penalty / interest / principal portions
        in waterfall order.  Stores the result on the document so the user
        can see the breakdown before submitting.

        Priority order  →  Penalty first, then Interest, then Principal.

        Sources:
          Penalty   = SUM(LIA.penalty_amount - LIA.paid_interest_amount)
          (paid_interest_amount is repurposed on Penalty LIAs — paid_penalty_amount does not exist)
                      WHERE accrual_type = 'Penalty' AND docstatus = 1
          Interest  = SUM(LIA.interest_amount - LIA.paid_interest_amount)
                      WHERE accrual_type != 'Penalty' AND docstatus = 1
          Principal = remaining write_off_amount after penalty and interest
        """
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        remaining = flt(self.write_off_amount, precision)

        # ── Unpaid penalty from LIAs ──────────────────────────────────────────
        unpaid_penalty = flt(
            frappe.db.sql(
                """SELECT COALESCE(
                       SUM(penalty_amount - COALESCE(paid_interest_amount, 0)), 0)
                   FROM `tabLoan Interest Accrual`
                   WHERE loan = %s AND docstatus = 1
                     AND accrual_type = 'Penalty'
                     AND penalty_amount > COALESCE(paid_interest_amount, 0)""",
                # paid_penalty_amount does not exist on tabLoan Interest Accrual.
                # Penalty LIAs always have interest_amount=0, so paid_interest_amount
                # is used as the write-off tracking field for penalty amounts.
                (self.loan,),
            )[0][0],
            precision,
        )

        # ── Unpaid interest from LIAs ─────────────────────────────────────────
        unpaid_interest = flt(
            frappe.db.sql(
                """SELECT COALESCE(
                       SUM(interest_amount - COALESCE(paid_interest_amount, 0)), 0)
                   FROM `tabLoan Interest Accrual`
                   WHERE loan = %s AND docstatus = 1
                     AND accrual_type != 'Penalty'
                     AND interest_amount > COALESCE(paid_interest_amount, 0)""",
                (self.loan,),
            )[0][0],
            precision,
        )

        # ── Waterfall allocation ──────────────────────────────────────────────
        penalty_portion = flt(min(unpaid_penalty, remaining), precision)
        remaining = flt(remaining - penalty_portion, precision)

        interest_portion = flt(min(unpaid_interest, remaining), precision)
        remaining = flt(remaining - interest_portion, precision)

        principal_portion = flt(remaining, precision)  # whatever is left

        # Store on the document (fields must exist on the doctype)
        self.write_off_penalty_amount = penalty_portion
        self.write_off_interest_amount = interest_portion
        self.write_off_principal_amount = principal_portion

    def validate_write_off_amount(self):
        """
        Validate that write_off_amount does not exceed the total outstanding
        balance (principal + interest + penalty).
        """
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        loan_details = frappe.get_value(
            "Loan",
            self.loan,
            [
                "total_payment",
                "debit_adjustment_amount",
                "credit_adjustment_amount",
                "refund_amount",
                "total_principal_paid",
                "loan_amount",
                "total_interest_payable",
                "written_off_amount",
                "disbursed_amount",
                "status",
            ],
            as_dict=1,
        )

        # Pending principal from the formula
        pending_principal = flt(get_pending_principal_amount(loan_details), precision)

        # Add outstanding interest and penalty so the cap covers total debt
        outstanding_interest = flt(
            frappe.db.sql(
                """SELECT COALESCE(
                       SUM(interest_amount - COALESCE(paid_interest_amount, 0)), 0)
                   FROM `tabLoan Interest Accrual`
                   WHERE loan = %s AND docstatus = 1
                     AND accrual_type != 'Penalty'
                     AND interest_amount > COALESCE(paid_interest_amount, 0)""",
                (self.loan,),
            )[0][0],
            precision,
        )
        outstanding_penalty = flt(
            frappe.db.sql(
                """SELECT COALESCE(
                       SUM(penalty_amount - COALESCE(paid_interest_amount, 0)), 0)
                   FROM `tabLoan Interest Accrual`
                   WHERE loan = %s AND docstatus = 1
                     AND accrual_type = 'Penalty'
                     AND penalty_amount > COALESCE(paid_interest_amount, 0)""",
                # paid_penalty_amount does not exist on tabLoan Interest Accrual.
                # Penalty LIAs always have interest_amount=0, so paid_interest_amount
                # is used as the write-off tracking field for penalty amounts.
                (self.loan,),
            )[0][0],
            precision,
        )

        total_outstanding = flt(
            pending_principal + outstanding_interest + outstanding_penalty,
            precision,
        )

        if flt(self.write_off_amount, precision) > total_outstanding:
            frappe.throw(
                _(
                    "Write-off amount ({0}) cannot exceed total outstanding balance "
                    "({1}).<br><br>"
                    "Outstanding breakdown:<br>"
                    "  Principal: {2}<br>"
                    "  Interest:  {3}<br>"
                    "  Penalty:   {4}"
                ).format(
                    flt(self.write_off_amount, precision),
                    total_outstanding,
                    pending_principal,
                    outstanding_interest,
                    outstanding_penalty,
                )
            )

    # ── Submit / Cancel ───────────────────────────────────────────────────────

    def update_loan_status(self, cancel=0):
        """
        Set Loan.status and Loan.repayment_status to reflect the write-off.

        On submit:
          status           → "Written Off"  (Frappe Lending standard)
          repayment_status → "Write-Off"    (matches CFS source status)

        On cancel:
          status           → "Disbursed"    (pre-write-off state for most loans)
          repayment_status → ""             (blank — set_repayment_status() will
                                             recalculate the correct value)
        """
        if cancel:
            frappe.db.set_value(
                "Loan",
                self.loan,
                {
                    "status": "Disbursed",
                    "repayment_status": "",
                },
                update_modified=False,
            )
        else:
            frappe.db.set_value(
                "Loan",
                self.loan,
                {
                    "status": "Written Off",
                    "repayment_status": "Write-Off",
                },
                update_modified=False,
            )

    def on_submit(self):
        self.update_outstanding_amount()
        self.make_gl_entries()
        self._settle_written_off_accruals()
        self.update_loan_status()

    def on_cancel(self):
        self.update_outstanding_amount(cancel=1)
        self._unsettle_written_off_accruals()
        self.ignore_linked_doctypes = ["GL Entry", "Payment Ledger Entry"]
        self.make_gl_entries(cancel=1)
        self.update_loan_status(cancel=1)

    # ── Loan outstanding tracking ─────────────────────────────────────────────

    def update_outstanding_amount(self, cancel=0):
        written_off_amount = frappe.db.get_value(
            "Loan", self.loan, "written_off_amount"
        )
        if cancel:
            written_off_amount -= self.write_off_amount
        else:
            written_off_amount += self.write_off_amount
        frappe.db.set_value("Loan", self.loan, "written_off_amount", written_off_amount)

    # ── LIA settlement ────────────────────────────────────────────────────────

    def _settle_written_off_accruals(self):
        """
        Mark LIA records as settled for the written-off interest and penalty
        amounts so they no longer appear as open receivables.

        Uses the same proportional approach as repayment: marks LIAs in
        due_date order (oldest first) until the written-off amount is consumed.
        """
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        # Settle penalty LIAs
        if flt(self.write_off_penalty_amount, precision) > 0:
            self._settle_lias(
                accrual_type="Penalty",
                amount_to_settle=flt(self.write_off_penalty_amount, precision),
                amount_field="penalty_amount",
                paid_field="paid_interest_amount",
                # paid_penalty_amount does not exist on tabLoan Interest Accrual.
                # Penalty LIAs have interest_amount=0 so paid_interest_amount is
                # repurposed as the penalty write-off tracking field.
                precision=precision,
            )

        # Settle interest LIAs
        if flt(self.write_off_interest_amount, precision) > 0:
            self._settle_lias(
                accrual_type="Interest",
                amount_to_settle=flt(self.write_off_interest_amount, precision),
                amount_field="interest_amount",
                paid_field="paid_interest_amount",
                precision=precision,
            )

    def _settle_lias(
        self, accrual_type, amount_to_settle, amount_field, paid_field, precision
    ):
        """Settle LIA records oldest-first up to amount_to_settle."""
        type_filter = (
            "accrual_type = 'Penalty'"
            if accrual_type == "Penalty"
            else "accrual_type != 'Penalty'"
        )
        lias = frappe.db.sql(
            f"""SELECT name,
                       {amount_field}                         AS billed,
                       COALESCE({paid_field}, 0)              AS paid
                FROM `tabLoan Interest Accrual`
                WHERE loan = %s AND docstatus = 1
                  AND {type_filter}
                  AND {amount_field} > COALESCE({paid_field}, 0)
                ORDER BY due_date ASC, name ASC""",
            (self.loan,),
            as_dict=True,
        )

        remaining = flt(amount_to_settle, precision)
        for lia in lias:
            if remaining <= 0:
                break
            unpaid = flt(lia.billed - lia.paid, precision)
            to_settle = flt(min(unpaid, remaining), precision)
            new_paid = flt(lia.paid + to_settle, precision)
            frappe.db.set_value(
                "Loan Interest Accrual",
                lia.name,
                paid_field,
                new_paid,
                update_modified=False,
            )
            remaining = flt(remaining - to_settle, precision)

    def _unsettle_written_off_accruals(self):
        """
        On cancel: reverse the LIA settlements made at write-off submit time.
        Subtracts the written-off amounts from paid fields.
        """
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        if flt(self.write_off_penalty_amount, precision) > 0:
            frappe.db.sql(
                """UPDATE `tabLoan Interest Accrual`
                   SET paid_interest_amount = GREATEST(
                           COALESCE(paid_interest_amount, 0) - %s, 0)
                   WHERE loan = %s AND docstatus = 1
                     AND accrual_type = 'Penalty'""",
                # paid_penalty_amount does not exist — using paid_interest_amount
                # which is repurposed for penalty write-off tracking on Penalty LIAs.
                (flt(self.write_off_penalty_amount, precision), self.loan),
            )

        if flt(self.write_off_interest_amount, precision) > 0:
            frappe.db.sql(
                """UPDATE `tabLoan Interest Accrual`
                   SET paid_interest_amount = GREATEST(
                           COALESCE(paid_interest_amount, 0) - %s, 0)
                   WHERE loan = %s AND docstatus = 1
                     AND accrual_type != 'Penalty'""",
                (flt(self.write_off_interest_amount, precision), self.loan),
            )

    # ── GL Entries ────────────────────────────────────────────────────────────

    def make_gl_entries(self, cancel=0):
        """
        DR  Write Off Expense             full write_off_amount
        CR  Penalty Receivable            write_off_penalty_amount   (if > 0)
        CR  Interest Receivable           write_off_interest_amount  (if > 0)
        CR  Loan Account (principal)      write_off_principal_amount (remainder)
        """
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        gl_entries = []
        loan_details = frappe.get_doc("Loan", self.loan)

        account_details = (
            frappe.db.get_value(
                "Loan Product",
                loan_details.loan_product,
                ["interest_receivable_account", "penalty_receivable_account"],
                as_dict=1,
            )
            or {}
        )

        # ── DR: Write Off Expense (full amount) ───────────────────────────────
        gl_entries.append(
            self.get_gl_dict(
                {
                    "account": self.write_off_account,
                    "against": loan_details.loan_account,
                    "debit": self.write_off_amount,
                    "debit_in_account_currency": self.write_off_amount,
                    "against_voucher_type": "Loan",
                    "against_voucher": self.loan,
                    "remarks": _("Write-off against loan: ") + self.loan,
                    "cost_center": self.cost_center,
                    "posting_date": getdate(self.posting_date),
                }
            )
        )

        # ── CR: Penalty Receivable ─────────────────────────────────────────────
        penalty_amt = flt(self.write_off_penalty_amount, precision)
        if penalty_amt > 0 and account_details.get("penalty_receivable_account"):
            gl_entries.append(
                self.get_gl_dict(
                    {
                        "account": account_details.penalty_receivable_account,
                        "against": self.write_off_account,
                        "credit": penalty_amt,
                        "credit_in_account_currency": penalty_amt,
                        "against_voucher_type": "Loan",
                        "against_voucher": self.loan,
                        "remarks": _("Penalty write-off against loan: ") + self.loan,
                        "cost_center": self.cost_center,
                        "posting_date": getdate(self.posting_date),
                    }
                )
            )

        # ── CR: Interest Receivable ────────────────────────────────────────────
        interest_amt = flt(self.write_off_interest_amount, precision)
        if interest_amt > 0 and account_details.get("interest_receivable_account"):
            gl_entries.append(
                self.get_gl_dict(
                    {
                        "account": account_details.interest_receivable_account,
                        "against": self.write_off_account,
                        "credit": interest_amt,
                        "credit_in_account_currency": interest_amt,
                        "against_voucher_type": "Loan",
                        "against_voucher": self.loan,
                        "remarks": _("Interest write-off against loan: ") + self.loan,
                        "cost_center": self.cost_center,
                        "posting_date": getdate(self.posting_date),
                    }
                )
            )

        # ── CR: Loan Account (principal) ───────────────────────────────────────
        principal_amt = flt(self.write_off_principal_amount, precision)
        if principal_amt > 0:
            gl_entries.append(
                self.get_gl_dict(
                    {
                        "account": loan_details.loan_account,
                        "party_type": loan_details.applicant_type,
                        "party": loan_details.applicant,
                        "against": self.write_off_account,
                        "credit": principal_amt,
                        "credit_in_account_currency": principal_amt,
                        "against_voucher_type": "Loan",
                        "against_voucher": self.loan,
                        "remarks": _("Principal write-off against loan: ") + self.loan,
                        "cost_center": self.cost_center,
                        "posting_date": getdate(self.posting_date),
                    }
                )
            )

        if gl_entries:
            make_gl_entries(gl_entries, cancel=cancel, merge_entries=False)

    # ── Employee loan close (unchanged) ───────────────────────────────────────

    def close_employee_loan(self, cancel=0):
        if not frappe.db.has_column("Loan", "repay_from_salary"):
            return

        loan = frappe.get_value(
            "Loan",
            self.loan,
            [
                "total_payment",
                "total_principal_paid",
                "loan_amount",
                "total_interest_payable",
                "written_off_amount",
                "disbursed_amount",
                "status",
                "is_secured_loan",
                "repay_from_salary",
                "name",
            ],
            as_dict=1,
        )

        if loan.is_secured_loan or not loan.repay_from_salary:
            return

        if not cancel:
            pending_principal_amount = get_pending_principal_amount(loan)
            precision = cint(frappe.db.get_default("currency_precision")) or 2
            if flt(pending_principal_amount, precision) <= 0:
                frappe.db.set_value("Loan", loan.name, "status", "Closed")
                frappe.msgprint(_("Loan {0} closed").format(loan.name))
        else:
            frappe.db.set_value("Loan", self.loan, "status", "Disbursed")
