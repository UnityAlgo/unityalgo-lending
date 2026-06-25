// Copyright (c) 2026, UnityAlgo and contributors
// For license information, please see license.txt

frappe.query_reports["Loan Pending Due"] = {
	"filters": [
		{
			"fieldname": "from_date",
			"label": __("From Date"),
			"fieldtype": "Date",
			"default": "2020-06-01",
			"reqd": 1
		},
		{
			"fieldname": "to_date",
			"label": __("To Date"),
			"fieldtype": "Date",
			"default": frappe.datetime.get_today(),
			"reqd": 1
		},
		{
			"fieldname": "branch",
			"label": __("Branch"),
			"fieldtype": "MultiSelectList",
			"get_data": function (txt) {
				return frappe.db.get_link_options("Branch", txt);
			}
		},
		// {
		// 	"fieldname": "status",
		// 	"label": __("Status"),
		// 	"fieldtype": "MultiSelectList",
		// 	"get_data": function (txt) {
		// 		const opts = [
		// 			"Current", "Due Today", "Arrears",
		// 			"Missed Repayment", "Past Maturity",
		// 			"Overdue", "Write Off"
		// 		];
		// 		return opts
		// 			.filter(o => !txt || o.toLowerCase().includes(txt.toLowerCase()))
		// 			.map(o => ({ value: o, description: o }));
		// 	}
		// }
	],

	"formatter": function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (!data) return value;

		// Bold totals row
		if (data.borrower_name === "TOTAL") {
			value = `<b>${value}</b>`;
		}

		// Color-code status
		if (column.fieldname === "status" && data.status) {
			const colors = {
				"Current": "green",
				"Due Today": "orange",
				"Arrears": "red",
				"Missed Repayment": "red",
				"Past Maturity": "darkred",
				"Overdue": "orange",
				"Write Off": "gray",
				"Fully Paid": "blue",
			};
			const color = colors[data.status] || "black";
			value = `<span style="color:${color};font-weight:bold">${data.status}</span>`;
		}

		// Highlight past_due > 0
		if (column.fieldname === "past_due" && flt(data.past_due) > 0) {
			value = `<span style="color:red">${value}</span>`;
		}

		// Highlight days_past > 0
		if (column.fieldname === "days_past" && data.days_past > 0) {
			value = `<span style="color:red">${value}</span>`;
		}

		return value;
	}
};