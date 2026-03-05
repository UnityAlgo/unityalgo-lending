// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["Loan Repayment Report"] = {
	"filters": [
		{
			"fieldname": "loan",
			"label": __("Loan"),
			"fieldtype": "Link",
			"options": "Loan",
			"reqd": 1
		}
	],
	"formatter": function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		// Highlight the "Today" and "Payment Received" rows in gray like the image
		if (data && (data.description == "<b>Today</b>" || data.description == "<b>Payment Received</b>")) {
			value = "<div style='background-color: #e5e7e9; padding: 4px; margin: -4px;'>" + value + "</div>";
		}
		// Highlight Maturity row in light red
		if (data && data.description == "Maturity") {
			value = "<div style='background-color: #f9ebea; padding: 4px; margin: -4px;'>" + value + "</div>";
		}
		// Highlight Schedule row in light green
		if (data && data.description == "Repayment") {
			value = "<div style='background-color: #eafaf1; padding: 4px; margin: -4px;'>" + value + "</div>";
		}

		return value;
	}
};
