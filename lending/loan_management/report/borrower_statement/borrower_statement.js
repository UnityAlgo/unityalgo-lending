// Copyright (c) 2026, Your Company and contributors
// For license information, please see license.txt

frappe.query_reports["Borrower Statement"] = {
	filters: [
		{
			label: __("Customer"),
			fieldname: "customer",
			fieldtype: "Link",
			options: "Customer",
			reqd: 1,
			on_change: function () {
				frappe.query_report.set_filter_value("loan", "");
			}
		},
		{
			label: __("Loan"),
			fieldname: "loan",
			fieldtype: "Link",
			options: "Loan",
			get_query: function () {
				let customer = frappe.query_report.get_filter_value("customer");
				return {
					filters: {
						applicant: customer,
						docstatus: 1
					}
				};
			}
		}
	]
};