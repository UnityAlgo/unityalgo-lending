# Copyright (c) 2026, SalmanIrfan88 and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class ExpenseEntry(Document):
    def validate(self):
        self.validate_accounts()
        self.calculate_tax_and_total()

    def validate_accounts(self):
        """Validate that expense and payment accounts belong to the selected company"""
        if self.expense_account:
            expense_company = frappe.db.get_value(
                "Account", self.expense_account, "company"
            )
            if expense_company != self.company:
                frappe.throw(
                    _("Expense Account {0} does not belong to Company {1}").format(
                        self.expense_account, self.company
                    )
                )

        if self.payment_account:
            payment_company = frappe.db.get_value(
                "Account", self.payment_account, "company"
            )
            if payment_company != self.company:
                frappe.throw(
                    _("Payment Account {0} does not belong to Company {1}").format(
                        self.payment_account, self.company
                    )
                )

    def calculate_tax_and_total(self):
        """Calculate tax amount and total based on VAT settings"""
        self.tax_amount = 0
        self.total_amount = flt(self.amount)

        if self.is_vat_applicable and self.tax_template and self.amount:
            tax_rate = self.get_tax_rate()

            if self.vat_type == "Exclusive":
                # Tax is added on top of the amount
                self.tax_amount = flt(self.amount) * flt(tax_rate) / 100
                self.total_amount = flt(self.amount) + flt(self.tax_amount)
            elif self.vat_type == "Inclusive":
                # Tax is included in the amount
                self.tax_amount = (
                    flt(self.amount) * flt(tax_rate) / (100 + flt(tax_rate))
                )
                self.total_amount = flt(self.amount)

        self.tax_amount = flt(self.tax_amount, self.precision("tax_amount"))
        self.total_amount = flt(self.total_amount, self.precision("total_amount"))

    def get_tax_rate(self):
        """Get tax rate from the selected tax template"""
        if not self.tax_template:
            return 0

        taxes = frappe.get_all(
            "Purchase Taxes and Charges",
            filters={"parent": self.tax_template},
            fields=["rate"],
            order_by="idx",
            limit=1,
        )

        if taxes:
            return flt(taxes[0].rate)
        return 0

    def on_submit(self):
        """Create Journal Entry on submit"""
        self.create_journal_entry()

    def on_cancel(self):
        """Cancel linked Journal Entry"""
        self.cancel_journal_entry()

    def create_journal_entry(self):
        """Create a Journal Entry for this expense"""
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.company = self.company
        je.custom_expense_entry = self.name
        je.posting_date = self.posting_date
        je.user_remark = (
            self.description or self.reference_number or f"Expense Entry: {self.name}"
        )
        je.cheque_no = self.reference_number or self.name
        je.cheque_date = self.posting_date

        # Get the net expense amount (excluding tax for exclusive, base amount for inclusive)
        if self.is_vat_applicable and self.vat_type == "Inclusive":
            expense_amount = flt(self.amount) - flt(self.tax_amount)
        else:
            expense_amount = flt(self.amount)

        # Debit: Expense Account
        je.append(
            "accounts",
            {
                "account": self.expense_account,
                "debit_in_account_currency": flt(expense_amount),
                "credit_in_account_currency": 0,
                "cost_center": self.cost_center,
                "branch": self.branch,
            },
        )

        # Debit: Tax Account (if VAT applicable)
        if self.is_vat_applicable and flt(self.tax_amount) > 0:
            tax_account = self.get_tax_account()
            if tax_account:
                je.append(
                    "accounts",
                    {
                        "account": tax_account,
                        "debit_in_account_currency": flt(self.tax_amount),
                        "credit_in_account_currency": 0,
                        "cost_center": self.cost_center,
                        "branch": self.branch,
                    },
                )

        # Credit: Payment Account (Cash/Bank)
        je.append(
            "accounts",
            {
                "account": self.payment_account,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": flt(self.total_amount),
                "cost_center": self.cost_center,
                "branch": self.branch,
            },
        )

        je.flags.ignore_permissions = True
        je.save()
        je.submit()

        frappe.msgprint(
            _("Journal Entry {0} created successfully").format(
                frappe.get_desk_link("Journal Entry", je.name)
            ),
            alert=True,
        )

    def get_tax_account(self):
        """Get tax account from the tax template"""
        if not self.tax_template:
            return None

        taxes = frappe.get_all(
            "Purchase Taxes and Charges",
            filters={"parent": self.tax_template},
            fields=["account_head"],
            order_by="idx",
            limit=1,
        )

        if taxes:
            return taxes[0].account_head
        return None

    def cancel_journal_entry(self):
        """Cancel the linked Journal Entry"""
        docs = frappe.db.get_all("Journal Entry", {"custom_expense_entry": self.name})
        for d in docs:
            doc = frappe.get_doc("Journal Entry", d.name)
            if doc.docstatus == 1:
                doc.cancel()
            doc.delete()
