# Copyright (c) 2023, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
import math

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, add_months, flt, get_last_day, getdate, cint


class LoanRepaymentSchedule(Document):
    def validate(self):
        self.validate_repayment_method()
        self.set_missing_fields()
        self.make_repayment_schedule()
        self.set_repayment_period()

    def set_missing_fields(self):
        if self.repayment_method == "Repay Over Number of Periods":
            self.monthly_repayment_amount = get_monthly_repayment_amount(
                self.loan_amount, self.rate_of_interest, self.repayment_periods
            )

    def set_repayment_period(self):
        if self.repayment_method == "Repay Fixed Amount per Period":
            repayment_periods = len(self.repayment_schedule)
            self.repayment_periods = repayment_periods

    def make_repayment_schedule(self):
        if not self.repayment_start_date:
            frappe.throw(_("Repayment Start Date is mandatory for term loans"))

        if self.repayment_start_date != frappe.db.get_value(
            self.doctype, self.name, "repayment_start_date"
        ):
            self.repayment_schedule = []

        if self.repayment_schedule:
            return

        schedule_type_details = frappe.db.get_value(
            "Loan Product",
            self.loan_product,
            ["repayment_schedule_type", "repayment_date_on"],
            as_dict=1,
        )

        self.repayment_schedule = []
        payment_date = self.repayment_start_date
        balance_amount = self.loan_amount
        total_interest_precise = 0.0

        while balance_amount > 0:
            interest_amount, principal_amount, balance_amount, total_payment, days = (
                self.get_amounts(
                    balance_amount,
                )
            )
            
            total_interest_precise += interest_amount

            if (
                schedule_type_details.repayment_schedule_type
                == "Pro-rated calendar months"
            ):
                next_payment_date = get_last_day(payment_date)
                if schedule_type_details.repayment_date_on == "Start of the next month":
                    next_payment_date = add_days(next_payment_date, 1)

                payment_date = next_payment_date

            self.add_repayment_schedule_row(
                payment_date,
                principal_amount,
                interest_amount,
                total_payment,
                balance_amount,
                days,
            )

            if (
                self.repayment_method == "Repay Over Number of Periods"
                and len(self.get("repayment_schedule")) >= self.repayment_periods
            ):
                balance_amount = 0

            if (
                schedule_type_details.repayment_schedule_type
                in ["Monthly as per repayment start date", "Monthly as per cycle date"]
                or schedule_type_details.repayment_date_on == "End of the current month"
            ):
                next_payment_date = add_single_month(payment_date)
                payment_date = next_payment_date

        # Adjust last period to ensure APR-consistent totals
        self._adjust_schedule_for_rounding(total_interest_precise)

    def validate_repayment_method(self):
        if (
            self.repayment_method == "Repay Over Number of Periods"
            and not self.repayment_periods
        ):
            frappe.throw(_("Please enter Repayment Periods"))

        if self.repayment_method == "Repay Fixed Amount per Period":
            if not self.monthly_repayment_amount:
                frappe.throw(_("Please enter repayment Amount"))
            if self.monthly_repayment_amount > self.loan_amount:
                frappe.throw(
                    _("Monthly Repayment Amount cannot be greater than Loan Amount")
                )

    def get_amounts(self, balance_amount):
        """Calculate periodic payment breakdown using APR methodology.

        Interest is calculated as: outstanding_balance × (APR / 12)
        where APR (Annual Percentage Rate) is the nominal annual rate and
        12 represents monthly compounding frequency.

        Values are returned at full precision to minimize cumulative rounding
        drift across periods. Rounding to display precision is handled when
        storing values in the repayment schedule rows.
        """
        # APR-based monthly interest: balance × (annual_rate / 12)
        interest_amount = balance_amount * flt(self.rate_of_interest) / (12 * 100)
        principal_amount = self.monthly_repayment_amount - interest_amount
        balance_amount = balance_amount - principal_amount

        if balance_amount < 0 or abs(balance_amount) < 0.10:
            principal_amount += balance_amount
            balance_amount = 0.0

        total_payment = principal_amount + interest_amount
        return interest_amount, principal_amount, balance_amount, total_payment, 30

    # def get_amounts(
    #     self,
    #     payment_date,
    #     balance_amount,
    #     schedule_type,
    #     repayment_date_on,
    #     additional_days,
    #     carry_forward_interest=0,
    # ):
    #     if schedule_type == "Monthly as per repayment start date":
    #         days = 1
    #         months = 12
    #     else:
    #         expected_payment_date = get_last_day(payment_date)
    #         if repayment_date_on == "Start of the next month":
    #             expected_payment_date = add_days(expected_payment_date, 1)

    #         if schedule_type == "Monthly as per cycle date":
    #             days = date_diff(payment_date, add_months(payment_date, -1))
    #             if additional_days < 0:
    #                 days = date_diff(self.repayment_start_date, self.posting_date)
    #                 additional_days = 0

    #             months = 365
    #             if additional_days:
    #                 days += additional_days
    #                 additional_days = 0
    #         elif expected_payment_date == payment_date:
    #             # using 30 days for calculating interest for all full months
    #             days = 30
    #             months = 365
    #         else:
    #             days = date_diff(get_last_day(payment_date), payment_date)
    #             months = 365

    #     interest_amount = flt(
    #         balance_amount * flt(self.rate_of_interest) * days / (months * 100)
    #     )
    #     principal_amount = self.monthly_repayment_amount - flt(interest_amount)
    #     balance_amount = flt(
    #         balance_amount + interest_amount - self.monthly_repayment_amount
    #     )
    #     if balance_amount < 0:
    #         principal_amount += balance_amount
    #         balance_amount = 0.0

    #     if carry_forward_interest:
    #         interest_amount += carry_forward_interest
    #     total_payment = principal_amount + interest_amount
    #     return interest_amount, principal_amount, balance_amount, total_payment, days

    def _adjust_schedule_for_rounding(self, total_interest_precise):
        """Adjust the last period to account for cumulative rounding differences.

        When individual period amounts are rounded for display/storage, the sum
        of rounded values may differ from the rounded sum of precise values.
        This method adjusts the last period so that:
        - Total principal paid exactly equals the loan amount
        - Total interest matches the APR-based precise calculation
        - Last period balance is exactly zero
        """
        if not self.repayment_schedule:
            return

        precision = cint(frappe.db.get_default("currency_precision")) or 2
        last_row = self.repayment_schedule[-1]

        # Ensure total principal equals loan amount
        total_principal_others = flt(
            sum(row.principal_amount for row in self.repayment_schedule[:-1]), precision
        )
        last_row.principal_amount = flt(
            self.loan_amount - total_principal_others, precision
        )

        # Adjust interest to match precise APR calculation
        displayed_total_interest = sum(
            row.interest_amount for row in self.repayment_schedule
        )
        expected_total_interest = flt(total_interest_precise, precision)
        interest_diff = flt(
            expected_total_interest - displayed_total_interest, precision
        )

        if interest_diff:
            last_row.interest_amount = flt(
                last_row.interest_amount + interest_diff, precision
            )

        # Recalculate last row total payment and zero out balance
        last_row.total_payment = flt(
            last_row.principal_amount + last_row.interest_amount, precision
        )
        last_row.balance_loan_amount = 0

    def add_repayment_schedule_row(
        self,
        payment_date,
        principal_amount,
        interest_amount,
        total_payment,
        balance_loan_amount,
        days,
    ):
        """Add a row to the repayment schedule, rounding monetary values for display."""
        precision = cint(frappe.db.get_default("currency_precision")) or 2
        self.append(
            "repayment_schedule",
            {
                "number_of_days": days,
                "payment_date": payment_date,
                "principal_amount": flt(principal_amount, precision),
                "interest_amount": flt(interest_amount, precision),
                "total_payment": flt(total_payment, precision),
                "balance_loan_amount": flt(balance_loan_amount, precision),
            },
        )


def add_single_month(date):
    if getdate(date) == get_last_day(date):
        return get_last_day(add_months(date, 1))
    else:
        return add_months(date, 1)


def get_monthly_repayment_amount(loan_amount, rate_of_interest, repayment_periods):
    """Calculate the fixed monthly repayment amount using APR methodology.

    Uses the standard amortization formula:
        PMT = P × r × (1 + r)^n / ((1 + r)^n - 1)

    where:
        P = loan principal (loan_amount)
        r = monthly interest rate = APR / 12
        n = number of repayment periods

    This formula assumes monthly compounding consistent with APR calculations.
    The result matches standard APR calculators (e.g., calculator.net/apr-calculator).
    """
    precision = cint(frappe.db.get_default("currency_precision")) or 2

    if rate_of_interest:
        monthly_interest_rate = rate_of_interest / (12 * 100)

        monthly_repayment_amount = flt(
            (
                loan_amount
                * monthly_interest_rate
                * (1 + monthly_interest_rate) ** repayment_periods
            )
            / ((1 + monthly_interest_rate) ** repayment_periods - 1),
            2,
        )
    else:
        monthly_repayment_amount = flt(flt(loan_amount) / repayment_periods, precision)

    return monthly_repayment_amount
