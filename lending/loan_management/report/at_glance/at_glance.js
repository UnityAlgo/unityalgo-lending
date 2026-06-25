// Copyright (c) 2026, UnityAlgo and contributors
// For license information, please see license.txt

frappe.query_reports["At Glance"] = {
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
			"get_data": function(txt) {
				return frappe.db.get_link_options("Branch", txt);
			}
		}
	],

	"formatter": function(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (!data) return value;

		// Bold section header rows
		const headers = ["", "5", "8", "11", "14"];
		if (headers.includes(data.no)) {
			value = `<b>${value}</b>`;
		}

		// Indent sub-rows
		const subRows = [
			"5a", "5b", "5c", "5d",
			"8a", "8b", "8c", "8d",
			"11a", "11b", "11c",
			"14a", "14b", "14c", "14d"
		];
		if (column.fieldname === "description" && subRows.includes(data.no)) {
			value = `&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;${value}`;
		}

		// Right-align value column
		if (column.fieldname === "value" && data.value && data.value !== "") {
			value = `<span style="float:right">${value}</span>`;
		}

		return value;
	}
};