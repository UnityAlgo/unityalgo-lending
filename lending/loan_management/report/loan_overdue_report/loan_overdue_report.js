// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Loan Overdue Report"] = {
  "filters": [
    {
      "fieldname": "as_of_date",
      "label": "As Of Date",
      "fieldtype": "Date",
      "default": "Today",
      "reqd": 1
    },
    {
      "fieldname": "branch",
      "label": "Branch",
      "fieldtype": "Link",
      "options": "Branch",
      "reqd": 0
    },
    {
      "fieldname": "loan_product",
      "label": "Loan Product",
      "fieldtype": "Link",
      "options": "Loan Product",
      "reqd": 0
    },
    {
      "fieldname": "loan_officer",
      "label": "Loan Officer",
      "fieldtype": "Link",
      "options": "User",
      "reqd": 0
    },
    {
      "fieldname": "repayment_status",
      "label": "Repayment Status",
      "fieldtype": "Select",
      "options": "\nOverdue\nMissed Repayment\nArrears\nPast Maturity",
      "reqd": 0
    },
    {
      "fieldname": "aging_bucket",
      "label": "Aging Bucket",
      "fieldtype": "Select",
      "options": "\n1-30 Days\n31-60 Days\n61-90 Days\n91-180 Days\n180+ Days",
      "reqd": 0
    },
    {
      "fieldname": "npa_only",
      "label": "NPA Only",
      "fieldtype": "Check",
      "default": 0,
      "reqd": 0
    }
  ]
};
