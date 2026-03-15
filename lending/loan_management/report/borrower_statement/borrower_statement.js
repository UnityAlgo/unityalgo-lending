// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Borrower Statement"] = {
	"filters": [
		{
			"label": __("Customer"),
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			
		},
	]
};
