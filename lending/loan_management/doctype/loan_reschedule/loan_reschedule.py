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
        loan = frappe.get_doc("Loan", self.loan)

        if loan.status == "Closed":
            frappe.throw(_("Cannot reschedule a closed loan."))

        if not loan.is_term_loan:
            frappe.throw(_("Rescheduling is only supported for term loans."))

        existing = frappe.db.exists(
            "Loan Reschedule",
            {"loan": self.loan, "docstatus": 1, "name": ["!=", self.name]},
        )
        if existing:
            frappe.throw(
                _(
                    "Loan {0} already has an active reschedule: {1}. Cancel it first."
                ).format(self.loan, existing)
            )

        schedule_id = get_active_schedule(self.loan)
        if not schedule_id:
            frappe.throw(
                _("No active repayment schedule found for loan {0}.").format(self.loan)
            )

        self.old_repayment_schedule = schedule_id

    def snapshot_outstanding_balances(self):
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        loan = frappe.get_doc("Loan", self.loan)
        self.outstanding_principal = flt(get_pending_principal_amount(loan), precision)
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
        self.outstanding_interest = max(
            flt(total_interest_accrued - total_interest_paid, precision), 0
        )

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
        self.outstanding_penalty = max(
            flt(total_penalty_accrued - total_penalty_paid, precision), 0
        )

    def compute_new_principal(self):
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
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        # ===================================
        # STEP 1: Mark old schedule as "Superseded"
        # ===================================
        frappe.db.set_value(
            "Loan Repayment Schedule",
            self.old_repayment_schedule,
            "status",
            "Superseded",
        )

        # ===================================
        # STEP 2: Create capitalization Journal Entry
        # ===================================
        # This is the KEY accounting entry. It reclassifies the outstanding
        # interest and penalty from their receivable accounts into the
        # Loan Account, effectively converting them into principal.
        #
        # Before:
        #   Loan Account DR 32,000 (borrower owes principal)
        #   Interest Receivable DR 2,710 (borrower owes interest)
        #   Penalty Receivable DR 60,000 (borrower owes penalty)
        #
        # After this JV:
        #   Loan Account DR 94,710 (all debt consolidated into principal)
        #   Interest Receivable DR 0 (cleared)
        #   Penalty Receivable DR 0 (cleared)
        #
        # Journal Entry:
        #   DR Loan Account           62,710 (increase principal)
        #   CR Interest Receivable      2,710 (clear interest debt)
        #   CR Penalty Receivable      60,000 (clear penalty debt)
        capitalization_total = flt(
            self.capitalized_interest + self.capitalized_penalty, precision
        )

        if capitalization_total > 0:
            jv = self._create_capitalization_jv()
            self.db_set("capitalization_jv", jv.name)

        # ===================================
        # STEP 3: Mark outstanding LIA interest/penalty as paid
        # ===================================
        # The JV cleared the GL balances. Now update the LIA records
        # so get_amounts() and allocate_amounts() don't see them as outstanding.
        if self.capitalized_interest > 0:
            self._settle_outstanding_interest()

        if self.capitalized_penalty > 0:
            self._settle_outstanding_penalty()

        # ===================================
        # STEP 4: Update the Loan document FIRST
        # ===================================
        # Must happen BEFORE creating the new schedule because
        # Loan Repayment Schedule.set_missing_fields() reads loan_amount
        # from the Loan doc to calculate monthly_repayment_amount.
        new_total_payment = sum(
            flt(r.total_payment, precision) for r in self.get("schedule_preview")
        )
        new_total_interest = sum(
            flt(r.interest_amount, precision) for r in self.get("schedule_preview")
        )

        # Calculate the monthly repayment from the preview
        num_rows = len(self.get("schedule_preview"))
        monthly_repayment = (
            flt(new_total_payment / num_rows, precision) if num_rows else 0
        )

        loan_update = {
            "total_payment": flt(new_total_payment, precision),
            "total_interest_payable": flt(new_total_interest, precision),
            "rate_of_interest": self.new_rate_of_interest,
            "repayment_periods": self.new_repayment_periods,
            "repayment_method": self.new_repayment_method,
            "loan_amount": flt(self.new_principal, precision),
            "disbursed_amount": flt(self.new_principal, precision),
            "monthly_repayment_amount": monthly_repayment,
            "total_principal_paid": 0,
            "total_amount_paid": 0,
            "status": "Disbursed",
            "repayment_status": "Regular",
            "overdue_days": 0,
        }

        if frappe.db.has_column("Loan", "total_interest_paid"):
            loan_update["total_interest_paid"] = 0
        if frappe.db.has_column("Loan", "total_penalty_paid"):
            loan_update["total_penalty_paid"] = 0

        if self.new_repayment_method == "Repay Fixed Amount per Period":
            loan_update["monthly_repayment_amount"] = self.new_monthly_repayment_amount

        frappe.db.set_value("Loan", self.loan, loan_update)

        # ===================================
        # STEP 5: Create new repayment schedule
        # ===================================
        # Now the Loan doc has the correct loan_amount, so
        # set_missing_fields() won't fail.
        new_schedule = frappe.new_doc("Loan Repayment Schedule")
        new_schedule.loan = self.loan
        new_schedule.loan_product = self.loan_product
        new_schedule.repayment_method = self.new_repayment_method
        new_schedule.repayment_periods = self.new_repayment_periods
        new_schedule.rate_of_interest = self.new_rate_of_interest
        new_schedule.repayment_start_date = self.new_repayment_start_date
        new_schedule.status = "Active"
        new_schedule.loan_amount = flt(self.new_principal, precision)
        new_schedule.monthly_repayment_amount = monthly_repayment

        if self.new_repayment_method == "Repay Fixed Amount per Period":
            new_schedule.monthly_repayment_amount = self.new_monthly_repayment_amount

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

        frappe.db.set_value(
            "Loan", self.loan, {"repayment_schedule_name": new_schedule.name}
        )
        frappe.msgprint(
            _("Loan {0} rescheduled. New schedule: {1}").format(
                self.loan, new_schedule.name
            ),
            alert=True,
            indicator="green",
        )

    def _create_capitalization_jv(self):
        """Create a Journal Entry that reclassifies interest + penalty into principal.

        DR Loan Account              (capitalized_interest + capitalized_penalty)
        CR Interest Receivable       (capitalized_interest)
        CR Penalty Receivable        (capitalized_penalty)
        """
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        loan_doc = frappe.get_doc("Loan", self.loan)
        loan_product = frappe.db.get_value(
            "Loan Product",
            self.loan_product,
            ["interest_receivable_account", "penalty_receivable_account"],
            as_dict=1,
        )

        jv = frappe.new_doc("Journal Entry")
        jv.voucher_type = "Journal Entry"
        jv.posting_date = self.posting_date
        jv.company = self.company
        jv.remark = _(
            "Loan Reschedule: Capitalization of interest and penalty into principal for {0}"
        ).format(self.loan)
        jv.loan = self.loan

        capitalization_total = flt(
            self.capitalized_interest + self.capitalized_penalty, precision
        )

        # DR Loan Account (increase principal balance)
        jv.append(
            "accounts",
            {
                "account": loan_doc.loan_account,
                "debit_in_account_currency": capitalization_total,
                "credit_in_account_currency": 0,
                "party_type": self.applicant_type,
                "party": self.applicant,
                "against_account": loan_product.interest_receivable_account,
                "reference_type": "Loan",
                "reference_name": self.loan,
            },
        )

        # CR Interest Receivable (clear outstanding interest)
        if self.capitalized_interest > 0:
            jv.append(
                "accounts",
                {
                    "account": loan_product.interest_receivable_account,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": flt(
                        self.capitalized_interest, precision
                    ),
                    "party_type": self.applicant_type,
                    "party": self.applicant,
                    "against_account": loan_doc.loan_account,
                    "reference_type": "Loan",
                    "reference_name": self.loan,
                },
            )

        # CR Penalty Receivable (clear outstanding penalty)
        if self.capitalized_penalty > 0:
            jv.append(
                "accounts",
                {
                    "account": loan_product.penalty_receivable_account,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": flt(
                        self.capitalized_penalty, precision
                    ),
                    "party_type": self.applicant_type,
                    "party": self.applicant,
                    "against_account": loan_doc.loan_account,
                    "reference_type": "Loan",
                    "reference_name": self.loan,
                },
            )

        jv.flags.ignore_permissions = True
        jv.insert()
        jv.submit()

        return jv

    def _settle_outstanding_interest(self):
        """Mark ALL non-penalty LIAs as fully paid so they don't appear as outstanding.

        get_accrued_interest_entries() returns LIAs where EITHER:
          interest_amount - paid_interest_amount > 0
          OR payable_principal_amount - paid_principal_amount > 0

        We must settle BOTH sides. Otherwise the old LIAs still appear as
        pending and allocate_amounts() maps new repayments against them
        instead of the new schedule.

        We store originals for reversal.
        """
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        unsettled_lias = frappe.db.sql(
            """
            SELECT name, interest_amount, paid_interest_amount,
                   payable_principal_amount, paid_principal_amount
            FROM `tabLoan Interest Accrual`
            WHERE loan = %s AND docstatus = 1
              AND accrual_type != 'Penalty'
              AND (
                ROUND(interest_amount - IFNULL(paid_interest_amount, 0), %s) > 0
                OR ROUND(IFNULL(payable_principal_amount, 0) - IFNULL(paid_principal_amount, 0), %s) > 0
              )
            """,
            (self.loan, precision, precision),
            as_dict=1,
        )

        # Store originals for reversal
        originals = {}
        for lia in unsettled_lias:
            originals[lia.name] = {
                "paid_interest_amount": flt(lia.paid_interest_amount, precision),
                "paid_principal_amount": flt(lia.paid_principal_amount, precision),
            }

        self.db_set("settled_interest_snapshot", frappe.as_json(originals))

        for lia in unsettled_lias:
            frappe.db.set_value(
                "Loan Interest Accrual",
                lia.name,
                {
                    "paid_interest_amount": flt(lia.interest_amount, precision),
                    "paid_principal_amount": flt(
                        lia.payable_principal_amount, precision
                    ),
                },
            )

    def _settle_outstanding_penalty(self):
        """Zero out penalty_amount on old penalty LIAs so get_amounts() stops counting them.

        The GL side is handled by the capitalization JV (CR Penalty Receivable).
        But get_amounts() reads penalty_amount from LIA records directly.
        We zero them out here and store the originals as JSON for reversal.
        """
        precision = cint(frappe.db.get_default("currency_precision")) or 2

        penalty_lias = frappe.db.sql(
            """
            SELECT name, penalty_amount
            FROM `tabLoan Interest Accrual`
            WHERE loan = %s AND docstatus = 1
              AND accrual_type = 'Penalty'
              AND ROUND(penalty_amount, %s) > 0
            """,
            (self.loan, precision),
            as_dict=1,
        )

        # Store originals for reversal
        originals = {
            lia.name: flt(lia.penalty_amount, precision) for lia in penalty_lias
        }
        self.db_set("settled_penalty_snapshot", frappe.as_json(originals))

        for lia in penalty_lias:
            frappe.db.set_value(
                "Loan Interest Accrual",
                lia.name,
                "penalty_amount",
                0,
            )

    def _restore_penalty_amounts(self):
        """Restore penalty_amount on LIAs that were zeroed during reschedule."""
        snapshot_json = self.get("settled_penalty_snapshot")
        if not snapshot_json:
            return

        originals = frappe.parse_json(snapshot_json)
        for lia_name, original_amount in originals.items():
            if frappe.db.exists("Loan Interest Accrual", lia_name):
                frappe.db.set_value(
                    "Loan Interest Accrual",
                    lia_name,
                    "penalty_amount",
                    flt(original_amount),
                )

    def reverse_reschedule(self):
        # 1. Cancel the capitalization JV
        if self.get("capitalization_jv"):
            jv_doc = frappe.get_doc("Journal Entry", self.capitalization_jv)
            if jv_doc.docstatus == 1:
                jv_doc.flags.ignore_permissions = True
                jv_doc.cancel()

        # 2. Cancel new schedule
        if self.new_repayment_schedule:
            new_sched_status = frappe.db.get_value(
                "Loan Repayment Schedule", self.new_repayment_schedule, "docstatus"
            )
            if new_sched_status == 1:
                new_schedule = frappe.get_doc(
                    "Loan Repayment Schedule", self.new_repayment_schedule
                )
                new_schedule.ignore_linked_doctypes = [
                    "Loan",
                    "Loan Interest Accrual",
                    "Loan Repayment",
                ]
                new_schedule.flags.ignore_permissions = True
                new_schedule.cancel()

        # 3. Restore old schedule to Active
        if self.old_repayment_schedule:
            if frappe.db.exists("Loan Repayment Schedule", self.old_repayment_schedule):
                frappe.db.set_value(
                    "Loan Repayment Schedule",
                    self.old_repayment_schedule,
                    "status",
                    "Active",
                )

        # 4. Restore LIA paid amounts and penalty amounts
        self._restore_lia_paid_amounts()
        self._restore_penalty_amounts()

        # 5. Restore loan values from the old schedule
        restored_schedule_id = get_active_schedule(self.loan)

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

    def _restore_lia_paid_amounts(self):
        """Restore paid_interest_amount and paid_principal_amount from snapshot."""
        snapshot_json = self.get("settled_interest_snapshot")
        if not snapshot_json:
            return

        originals = frappe.parse_json(snapshot_json)
        for lia_name, original_vals in originals.items():
            if frappe.db.exists("Loan Interest Accrual", lia_name):
                frappe.db.set_value(
                    "Loan Interest Accrual",
                    lia_name,
                    {
                        "paid_interest_amount": flt(
                            original_vals.get("paid_interest_amount", 0)
                        ),
                        "paid_principal_amount": flt(
                            original_vals.get("paid_principal_amount", 0)
                        ),
                    },
                )


def get_active_schedule(loan_name):
    """Find the active (non-superseded) repayment schedule for a loan."""
    schedule_id = frappe.db.get_value(
        "Loan Repayment Schedule",
        {"loan": loan_name, "docstatus": 1, "status": "Active"},
        "name",
    )

    if schedule_id:
        return schedule_id

    # Fallback for legacy data without status
    schedule_id = frappe.db.get_value(
        "Loan Repayment Schedule",
        {"loan": loan_name, "docstatus": 1},
        "name",
        order_by="creation desc",
    )

    return schedule_id


def get_pending_principal_amount(loan):
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


@frappe.whitelist()
def create_loan_reschedule(loan_name):
    loan = frappe.get_doc("Loan", loan_name)

    if loan.status == "Closed":
        frappe.throw(_("Cannot reschedule a closed loan."))
    if not loan.is_term_loan:
        frappe.throw(_("Rescheduling is only supported for term loans."))

    rsc = frappe.new_doc("Loan Reschedule")
    rsc.loan = loan.name
    rsc.applicant_type = loan.applicant_type
    rsc.applicant = loan.applicant
    rsc.company = loan.company
    rsc.loan_product = loan.loan_product
    rsc.posting_date = today()
    rsc.new_rate_of_interest = loan.rate_of_interest
    rsc.new_repayment_periods = 12
    rsc.new_repayment_start_date = add_months(getdate(today()), 1)
    rsc.new_repayment_method = loan.repayment_method or "Repay Over Number of Periods"
    rsc.insert()
    return rsc.name


@frappe.whitelist()
def get_loans_eligible_for_reschedule():
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
        schedule_id = get_active_schedule(loan.name)
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
