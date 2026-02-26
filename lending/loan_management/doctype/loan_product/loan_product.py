# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.model.document import Document
from lending.loan_management.doctype.loan_repayment_schedule.loan_repayment_schedule import (
    get_monthly_repayment_amount,
)
from frappe.utils import flt, rounded, cint
import math


class LoanProduct(Document):
    def before_validate(self):
        self.set_missing_values()

    def set_repayment_details(self):
        loan_amount = flt(self.minimum_loan_amount, 2)
        balance_amount = loan_amount
        self.total_payable_interest = 0

        if self.repayment_method == "Repay Over Number of Periods":
            self.repayment_amount = get_monthly_repayment_amount(
                loan_amount, self.rate_of_interest, cint(self.repayment_periods)
            )

        if self.repayment_method == "Repay Fixed Amount per Period":
            monthly_interest_rate = flt(self.rate_of_interest) / (12 * 100)

            if monthly_interest_rate:
                min_repayment_amount = loan_amount * monthly_interest_rate

                if self.repayment_amount - min_repayment_amount <= 0:
                    frappe.throw(
                        _(
                            "Repayment Amount must be greater than "
                            + str(flt(min_repayment_amount, 2))
                        )
                    )
                self.repayment_periods = math.ceil(
                    (
                        math.log(self.repayment_amount)
                        - math.log(self.repayment_amount - min_repayment_amount)
                    )
                    / (math.log(1 + monthly_interest_rate))
                )
            else:
                self.repayment_periods = loan_amount / self.repayment_amount

        self.total_payable_interest = 0
        while balance_amount > 0:
            interest_amount = rounded(
                balance_amount * flt(self.rate_of_interest) / (12 * 100)
            )
            balance_amount = rounded(
                balance_amount + interest_amount - self.repayment_amount
            )

            self.total_payable_interest += interest_amount

        self.total_payable_amount = loan_amount + self.total_payable_interest

    def validate(self):
        self.validate_accounts()
        self.validate_rates()

        self.set_repayment_details()

    def set_missing_values(self):
        company_min_days_bw_disbursement_first_repayment = frappe.get_cached_value(
            "Company", self.company, "min_days_bw_disbursement_first_repayment"
        )
        if (
            self.min_days_bw_disbursement_first_repayment is None
            and company_min_days_bw_disbursement_first_repayment
        ):
            self.min_days_bw_disbursement_first_repayment = (
                company_min_days_bw_disbursement_first_repayment
            )

    def validate_accounts(self):
        for fieldname in [
            "payment_account",
            "loan_account",
            "interest_income_account",
            "penalty_income_account",
        ]:
            company = frappe.get_value("Account", self.get(fieldname), "company")

            if company and company != self.company:
                frappe.throw(
                    _("Account {0} does not belong to company {1}").format(
                        frappe.bold(self.get(fieldname)), frappe.bold(self.company)
                    )
                )

        if self.get("loan_account") == self.get("payment_account"):
            frappe.throw(_("Loan Account and Payment Account cannot be same"))

    def validate_rates(self):
        for field in ["rate_of_interest", "penalty_interest_rate"]:
            if self.get(field) and self.get(field) < 0:
                frappe.throw(_("{0} cannot be negative").format(frappe.unscrub(field)))


@frappe.whitelist()
def get_default_charge_accounts(charge_type, company):
    default_charge_accounts = frappe.db.get_value(
        "Item Default",
        {"parent": charge_type, "company": company},
        [
            "income_account",
            "default_receivable_account",
            "default_waiver_account",
            "default_write_off_account",
            "default_suspense_account",
        ],
        as_dict=True,
    )
    out = {
        "income_account": default_charge_accounts.income_account,
        "receivable_account": default_charge_accounts.default_receivable_account,
        "waiver_account": default_charge_accounts.default_waiver_account,
        "write_off_account": default_charge_accounts.default_write_off_account,
        "suspense_account": default_charge_accounts.default_suspense_account,
    }

    return out
