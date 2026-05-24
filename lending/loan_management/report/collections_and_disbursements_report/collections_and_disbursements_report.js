// Copyright (c) 2026, UnityAlgo.
// For license information, please see license.txt

frappe.query_reports["Collections and Disbursements Report"] = {
	filters: [
		{
			fieldname: "period_type",
			label: __("Period Type"),
			fieldtype: "Select",
			options: "Daily\nMonthly",
			default: "Monthly",
			reqd: 1,
			on_change: function () {
				frappe.query_report.refresh();
			}
		},
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
		},
		        {
            fieldname: "sales_person",
            label: __("Loan Officer (CRO)"),
            fieldtype: "Link",
            options: "Sales Person",
        },

		{
			fieldname: "loan_product",
			label: __("Loan Product"),
			fieldtype: "Link",
			options: "Loan Product",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		// Bold + highlight the TOTAL row
		if (data && data.period && data.period.includes("TOTAL")) {
			value = `<b style="color:#2e4057">${value}</b>`;
			return `<div style="background:#eaf2ff;padding:2px 4px;border-radius:3px">${value}</div>`;
		}

		// Colour net_cash green if positive
		if (column.fieldname === "net_cash" && data && data.net_cash > 0) {
			value = `<span style="color:#28a745;font-weight:600">${value}</span>`;
		}

		// Colour disb_amount blue
		if (column.fieldname === "disb_amount" && data && data.disb_amount > 0) {
			value = `<span style="color:#1565c0">${value}</span>`;
		}

		// Colour rep_amount green
		if (column.fieldname === "rep_amount" && data && data.rep_amount > 0) {
			value = `<span style="color:#2e7d32">${value}</span>`;
		}

		return value;
	},

	get_chart_data: function (columns, result) {
		// Remove the TOTAL row from chart data
		const rows = result.filter(r => r.period && !r.period.includes("TOTAL"));
		if (!rows.length) return null;

		return {
			data: {
				labels: rows.map(r => r.period),
				datasets: [
					{
						name: "Amount Disbursed",
						values: rows.map(r => r.disb_amount || 0),
						chartType: "bar",
					},
					{
						name: "Total Collected",
						values: rows.map(r => r.rep_amount || 0),
						chartType: "bar",
					},
					{
						name: "Net Cash Released",
						values: rows.map(r => r.net_cash || 0),
						chartType: "line",
					},
				]
			},
			type: "axis-mixed",
			height: 300,
			colors: ["#1565c0", "#2e7d32", "#f57c00"],
		};
	},
};