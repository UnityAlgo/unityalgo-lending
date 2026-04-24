// Copyright (c) 2026, UnityAlgo

frappe.query_reports["Advance Recovery Report"] = {

	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.month_start(),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.month_end(),
		},
		{
			fieldname: "loan_product",
			label: __("Loan Product"),
			fieldtype: "Link",
			options: "Loan Product",
		},
		{
			fieldname: "report_type",
			label: __("Report View"),
			fieldtype: "Select",
			options: [
				"Summary",
				"Recovery Breakdown",
				"Advance Repayments",
				"Month-wise Advance",
			].join("\n"),
			default: "Summary",
			on_change: () => frappe.query_report.refresh(),
		},
	],

	// ── Dynamic formatter ─────────────────────────────────────────────────────
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (!data) return value;

		// Highlight section headers in Summary view
		if (column.fieldname === "metric" && typeof data.metric === "string" && data.metric.startsWith("──")) {
			return `<span style="font-weight:500; color:var(--text-muted); font-size:11px; letter-spacing:0.04em;">${data.metric}</span>`;
		}

		// Excess ratio coloring in Advance Repayments
		if (column.fieldname === "excess_ratio" && data.excess_ratio) {
			const r = parseFloat(data.excess_ratio);
			let color = "#378ADD";
			let label = r.toFixed(2) + "×";
			if (r >= 3) { color = "#D85A30"; }
			else if (r >= 2) { color = "#BA7517"; }
			return `<span style="font-weight:500; color:${color};">${label}</span>`;
		}

		// Excess paid — always green
		if (column.fieldname === "excess_paid" && parseFloat(data.excess_paid) > 0) {
			return `<span style="color:#0F6E56; font-weight:500;">${value}</span>`;
		}

		// Repayment status badge
		if (column.fieldname === "repayment_status" && data.repayment_status) {
			const statusColors = {
				"Fully Paid": ["#EAF3DE", "#3B6D11"],
				"Regular": ["#E6F1FB", "#185FA5"],
				"Due Today": ["#FAEEDA", "#854F0B"],
				"Overdue": ["#FAEEDA", "#854F0B"],
				"Arrears": ["#FAECE7", "#993C1D"],
				"Missed Repayment": ["#FCEBEB", "#A32D2D"],
				"Past Maturity": ["#FCEBEB", "#A32D2D"],
			};
			const [bg, fg] = statusColors[data.repayment_status] || ["#F1EFE8", "#5F5E5A"];
			return `<span style="background:${bg}; color:${fg}; font-size:11px; font-weight:500;
                         padding:2px 8px; border-radius:4px; white-space:nowrap;">
                        ${data.repayment_status}
                    </span>`;
		}

		return value;
	},

	// ── onload: set month defaults ────────────────────────────────────────────
	onload(report) {
		// report.page.add_inner_button(__("Export to Excel"), () => {
		// 	frappe.query_report.export_report("Excel");
		// });

		// Quick-jump buttons for common ranges
		report.page.add_inner_button(__("This Month"), () => {
			frappe.query_report.set_filter_value("from_date", frappe.datetime.month_start());
			frappe.query_report.set_filter_value("to_date", frappe.datetime.month_end());
			frappe.query_report.refresh();
		});

		report.page.add_inner_button(__("Last Month"), () => {
			const d = frappe.datetime.add_months(frappe.datetime.month_start(), -1);
			frappe.query_report.set_filter_value("from_date", frappe.datetime.month_start(d));
			frappe.query_report.set_filter_value("to_date", frappe.datetime.month_end(d));
			frappe.query_report.refresh();
		});

		report.page.add_inner_button(__("This Year"), () => {
			const y = frappe.datetime.now_date().substring(0, 4);
			frappe.query_report.set_filter_value("from_date", y + "-01-01");
			frappe.query_report.set_filter_value("to_date", y + "-12-31");
			frappe.query_report.refresh();
		});
	},
};