// # Copyright (c) 2026, UnityAlgo
// # For license information, please see license.txt

frappe.query_reports["Closed Loan Clients"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 0,
		},
		{
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
			reqd: 0,
		},
		{
			fieldname: "loan_product",
			label: __("Loan Product"),
			fieldtype: "Link",
			options: "Loan Product",
			reqd: 0,
		},
		{
			fieldname: "sales_person",
			label: __("Sales Person"),
			fieldtype: "Link",
			options: "Sales Person",
			reqd: 0,
		},
		{
			fieldname: "from_date",
			label: __("Closure From Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 0,
		},
		{
			fieldname: "to_date",
			label: __("Closure To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (column.fieldname === "past_maturity_days" && data) {
			const days = data.past_maturity_days;

			if (days === null || days === undefined || days === "") {
				return value;
			}

			if (days > 0) {
				// Paid early — green
				value = `<span style="color: #2ecc71; font-weight: bold;">+${days}</span>`;
			} else if (days < 0) {
				// Paid late — red
				value = `<span style="color: #e74c3c; font-weight: bold;">${days}</span>`;
			} else {
				// Paid exactly on maturity date — neutral
				value = `<span style="color: #95a5a6; font-weight: bold;">0</span>`;
			}
		}

		if (column.fieldname === "status" && data) {
			if (data.status === "Closed") {
				value = `<span style="color: #27ae60; font-weight: bold;">${data.status}</span>`;
			} else if (data.status === "Loan Closure Requested") {
				value = `<span style="color: #e67e22; font-weight: bold;">${data.status}</span>`;
			}
		}

		if (column.fieldname === "repayment_status" && data) {
			const status_colors = {
				"Fully Paid": "#27ae60",
				"Regular": "#2980b9",
				"Overdue": "#e74c3c",
				"Missed Repayment": "#c0392b",
				"Arrears": "#e67e22",
				"Past Maturity": "#8e44ad",
				"Due Today": "#f39c12",
				"Write-Off": "#7f8c8d",
			};
			const color = status_colors[data.repayment_status];
			if (color) {
				value = `<span style="color: ${color}; font-weight: bold;">${data.repayment_status}</span>`;
			}
		}

		return value;
	},
};
