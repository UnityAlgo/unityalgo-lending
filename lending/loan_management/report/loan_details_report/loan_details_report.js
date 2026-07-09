// Copyright (c) 2026, UnityAlgo and contributors
// For license information, please see license.txt

frappe.query_reports["Loan Details Report"] = {
"filters": [
        {
            "fieldname": "company",
            "label": __("Company"),
            "fieldtype": "Link",
            "options": "Company",
            "default": frappe.defaults.get_user_default("Company"),
            "reqd": 1
        },
        {
            "fieldname": "branch",
            "label": __("Branch"),
            "fieldtype": "Link",
            "options": "Branch"
        },
        {
            "fieldname": "loan_product",
            "label": __("Loan Product"),
            "fieldtype": "Link",
            "options": "Loan Product"
        },
        {
            "fieldname": "status",
            "label": __("Status"),
            "fieldtype": "Select",
            "options": "\nDisbursed\nClosed\nLoan Closure Requested",
            "default": "Disbursed"
        },
        {
            "fieldname": "from_date",
            "label": __("From Date"),
            "fieldtype": "Date"
        },
        {
            "fieldname": "to_date",
            "label": __("To Date"),
            "fieldtype": "Date"
        }
    ]
};
