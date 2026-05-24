// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

const PAR_BUCKET_COLOURS = {
	"Current": "#27ae60",   // green
	"Watch List": "#f39c12",   // amber
	"OAEM": "#e67e22",   // orange
	"Substandard": "#e74c3c",   // red
	"Doubtful": "#c0392b",   // dark red
	"Loss": "#7b241c",   // very dark red
};


frappe.query_reports["Portfolio at Risk"] = {
	filters: [
		{
			fieldname: "as_of_date",
			label: __("As Of Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "par_bucket",
			label: __("PAR Category"),
			fieldtype: "Select",
			options: "\nCurrent\nWatch List\nOAEM\nSubstandard\nDoubtful\nLoss",
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
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;

		if (column.fieldname === "par_bucket") {
			const colour = PAR_BUCKET_COLOURS[value] || "#555";
			value = `<span style="color:${colour}; font-weight:600;">${value}</span>`;
		}

		if (column.fieldname === "days_past_due") {
			const days = parseInt(data.days_past_due) || 0;
			if (days > 0) {
				value = `<span style="color:#e74c3c; font-weight:600;">${value}</span>`;
			}
		}
		if (column.fieldname === "provision_rate") {
			const rate = parseFloat(data.provision_rate) || 0;
			if (rate === 0) {
				value = `<span style="color:#888;">–</span>`;
			}
		}

		return value;
	},
};
