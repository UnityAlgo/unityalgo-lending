// Copyright (c) 2026, UnityAlgo and contributors
// For license information, please see license.txt

frappe.query_reports["PAR Summary Report"] = {

	filters: [
		{
			fieldname: "as_of_date",
			label: __("As Of Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
			reqd: 0,
		},
		{
			fieldname: "loan_type",
			label: __("Loan Type"),
			fieldtype: "Link",
			options: "Loan Type",
			reqd: 0,
		},
		{
			fieldname: "loan_officer",
			label: __("CRO / Officer"),
			fieldtype: "Link",
			options: "Sales Person",
			reqd: 0,
		},
	],


	get_drilldown_data(filters, row) {
		if (!row || row.category === "Grand Total") return;

		return {
			report_name: "Portfolio at Risk",
			filters: {
				as_of_date: filters.as_of_date,
				par_bucket: row.category,
				branch: filters.branch || "",
				loan_type: filters.loan_type || "",
				loan_officer: filters.loan_officer || "",
			},
		};
	},
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;

		const category = data.category || "";

		if (data.bold) {
			value = `<b>${value}</b>`;
		}

		const atRisk = ["Substandard", "Doubtful", "Loss"];
		if (column.fieldname === "category" && atRisk.includes(category)) {
			value = `<span style="color:#c0392b; font-weight:600;">${value}</span>`;
		}

		if (column.fieldname === "pct_portfolio" && category !== "Grand Total") {
			const pct = parseFloat(data.pct_portfolio) || 0;
			let colour = "#27ae60";          // green  — < 5%
			if (pct > 20) colour = "#e74c3c";  // red    — > 20%
			else if (pct > 5) colour = "#e67e22";  // amber  — 5–20%
			value = `<span style="color:${colour}; font-weight:600;">${value}</span>`;
		}

		if (column.fieldname === "provision_rate") {
			const rate = parseFloat(data.provision_rate) || 0;
			if (category === "Grand Total") {
				value = "";
			} else if (rate === 0) {
				value = `<span style="color:#888;">–</span>`;
			} else {
				value = `<span style="text-decoration:underline; cursor:pointer;">${value}</span>`;
			}
		}

		return value;
	},
};
