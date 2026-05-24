// Copyright (c) 2026, UnityAlgo and contributors
// For license information, please see license.txt

frappe.query_reports["Loan Recovery"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_end(),
			reqd: 1,
		},
		{
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
			on_change: function () {
				frappe.query_report.refresh();
			},
		},
		{
			fieldname: "loan_officer",
			label: __("CRO / Loan Officer"),
			fieldtype: "Link",
			options: "User",
			get_query: function () {
				// Only show users who are sales_person on at least one Loan
				return {
					query: "frappe.client.get_list",
					filters: {
						doctype: "User",
						fields: ["name", "full_name"],
					},
				};
			},
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (!data) return value;

		const label = (data.label || "").toString();

		// Bold and style grand total row
		if (label === "Grand Total") {
			value = `<strong>${value}</strong>`;
		}

		// Bold and teal-tint branch total rows
		if (label.startsWith("Grand Total") || (label.includes("—") && label.includes("Total"))) {
			value = `<strong>${value}</strong>`;
		}

		// Highlight negative net due values in red
		if (column.fieldname === "total" && data.total < 0) {
			value = `<span style="color: var(--red-500)">${value}</span>`;
		}

		return value;
	},
};
