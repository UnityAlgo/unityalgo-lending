# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import unittest

import frappe

from lending.loan_management.doctype.loan.test_loan import (
	create_loan_accounts,
	create_loan_product,
	set_loan_settings_in_company,
)


class TestLoanProduct(unittest.TestCase):
	def setUp(self):
		set_loan_settings_in_company()
		create_loan_accounts()

	def test_demand_loan_product_does_not_require_repayment_periods(self):
		# Regression: Demand Loan products (is_term_loan=0) used to crash with a
		# ZeroDivisionError in set_repayment_details() because the term-loan
		# amortization math ran unconditionally, even though a Demand Loan has
		# no fixed repayment schedule and never sets repayment_periods.
		create_loan_product(
			"Test Demand Loan Product",
			"Test Demand Loan Product",
			2000000,
			13.5,
			25,
			0,
			5,
			"Cash",
			"Disbursement Account - _TC",
			"Payment Account - _TC",
			"Loan Account - _TC",
			"Interest Income Account - _TC",
			"Penalty Income Account - _TC",
		)
		loan_product = frappe.get_doc("Loan Product", "Test Demand Loan Product")

		self.assertEqual(loan_product.total_payable_interest, 0)
		self.assertEqual(loan_product.total_payable_amount, loan_product.maximum_loan_amount)

	def test_term_loan_product_requires_repayment_periods(self):
		# Regression: a Term Loan product using "Repay Over Number of Periods"
		# with repayment_periods left at its default (0) used to crash with a
		# raw ZeroDivisionError instead of a clear validation error.
		if frappe.db.exists("Loan Product", "Test Term Loan Without Periods"):
			frappe.delete_doc("Loan Product", "Test Term Loan Without Periods", force=True)

		loan_product = frappe.get_doc(
			{
				"doctype": "Loan Product",
				"company": "_Test Company",
				"product_code": "Test Term Loan Without Periods",
				"product_name": "Test Term Loan Without Periods",
				"is_term_loan": 1,
				"maximum_loan_amount": 500000,
				"rate_of_interest": 10,
				"repayment_method": "Repay Over Number of Periods",
				"repayment_periods": 0,
				"mode_of_payment": "Cash",
				"disbursement_account": "Disbursement Account - _TC",
				"payment_account": "Payment Account - _TC",
				"loan_account": "Loan Account - _TC",
				"interest_income_account": "Interest Income Account - _TC",
				"penalty_income_account": "Penalty Income Account - _TC",
			}
		)

		self.assertRaises(frappe.ValidationError, loan_product.insert)
