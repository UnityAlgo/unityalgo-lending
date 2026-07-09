# Copyright (c) 2026, UnityAlgo and contributors
# For license information, please see license.txt


import frappe
from frappe import _
import re
# m = re.search(r"LoanRef:([^\s,]+)", loan.description or "")
#         if m:
#             loan_map[m.group(1).strip()] = loan.name
def execute(filters=None):
	if not filters:
		filters = {}

	columns = get_columns()
	data = get_data(filters)

	return columns, data

def get_columns():
	return [
				{"label": _("ID"), "fieldname": "name", "fieldtype": "Link", "options": "Loan", "width": 170},

		{"label": _("Branch"), "fieldname": "branch", "fieldtype": "Link", "options": "Branch", "width": 170},
		{"label": _("Customer Name"), "fieldname": "applicant_name", "fieldtype": "Data", "width": 180},
		{"label": _("Disbursement Date"), "fieldname": "disbursement_date", "fieldtype": "Date", "width": 160},
		{"label": _("Loan #"), "fieldname": "loan_number", "fieldtype": "Data", "width": 170},
		{"label": _("Disbursed"), "fieldname": "disbursed_amount", "fieldtype": "Currency", "width": 140},
			{"label": _("Oustanding"), "fieldname": "outstanding_amount", "fieldtype": "Currency", "width": 140},

		{"label": _("Loan Product"), "fieldname": "loan_product", "fieldtype": "Link", "options": "Loan Product", "width": 150},
		{"label": _("Loan Officer"), "fieldname": "loan_officer", "fieldtype": "Link", "options": "Sales Person", "width": 150},
		# {"label": _("Total Payment"), "fieldname": "total_payment", "fieldtype": "Currency", "width": 140},
		{"label": _("Total Principal Paid"), "fieldname": "total_principal_paid", "fieldtype": "Currency", "width": 150},
		{"label": _("Total Interest Paid"), "fieldname": "total_interest_paid", "fieldtype": "Currency", "width": 160},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 120},
	]

def get_conditions(filters):
	conditions = ""
	if filters.get("company"):
		conditions += " AND company = %(company)s"
	if filters.get("branch"):
		conditions += " AND branch = %(branch)s"
	if filters.get("loan_product"):
		conditions += " AND loan_product = %(loan_product)s"
	if filters.get("status"):
		conditions += " AND status = %(status)s"
	if filters.get("from_date"):
		conditions += " AND disbursement_date >= %(from_date)s"
	if filters.get("to_date"):
		conditions += " AND disbursement_date <= %(to_date)s"
	return conditions

def get_data(filters):
	conditions = get_conditions(filters)
	
	data = frappe.db.sql(f"""
		SELECT 
			description as loan_number,
			name,
			disbursement_date,
			applicant_name,
			branch,
			loan_product,
			repayment_method,
			sales_person as loan_officer,
			applicant_type,
			status,
			total_payment,
			disbursed_amount,
			(COALESCE(total_payment, 0) - (COALESCE(total_principal_paid, 0) + (COALESCE(total_interest_paid, 0)))) as outstanding_amount,
			total_principal_paid,
			total_interest_payable,
			total_interest_paid,
			interest_income_account
		FROM `tabLoan`
		WHERE docstatus = 1 
		{conditions}
		ORDER BY disbursement_date DESC
	""", filters, as_dict=1)
	for i in data:
		if i.get("loan_number"):
			m = re.search(r"LoanRef:([^\s,]+)", i["loan_number"] or "")
			if m:
				i["loan_number"] = m.group(1).strip()

	return data