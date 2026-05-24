// Copyright (c) 2026, UnityAlgo and contributors
// For license information, please see license.txt

frappe.query_reports["Loan Officer Report"] = {
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
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "loan_officer",
			label: __("Loan Officer"),
			fieldtype: "Link",
			options: "Sales Person",
			get_query() {
				return {
					query: "frappe.client.get_list",
					filters: { doctype: "Sales Person" },
				};
			},
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;

		const label = (data.officer || "").trim();

		if (data.bold && !label.startsWith("Grand Total")) {
			value = `<span style="font-weight:600">${value}</span>`;
			return value;
		}

		if (label.startsWith("Grand Total")) {
			value = `<span style="font-weight:600">${value}</span>`;
			return value;
		}

		if (label === "Due Loans:") {
			if (
				column.fieldname !== "officer" &&
				value !== "" &&
				value !== null
			) {
				value = `<span style="color: var(--red-500, #e24b4a)">${value}</span>`;
			}
			return value;
		}

		// Payments — green text on numeric columns
		if (label.startsWith("Payments")) {
			if (
				column.fieldname !== "officer" &&
				value !== "" &&
				value !== null
			) {
				value = `<span style="color: var(--green-600, #3b6d11)">${value}</span>`;
			}
			return value;
		}

		// Net Due — red if negative, green if positive
		if (label === "Net Due:") {
			if (
				column.fieldname !== "officer" &&
				value !== "" &&
				value !== null
			) {
				const raw = parseFloat(
					String(value).replace(/[^0-9.\-]/g, "")
				);
				if (!isNaN(raw)) {
					const color =
						raw < 0
							? "var(--red-500, #e24b4a)"
							: raw > 0
								? "var(--green-600, #3b6d11)"
								: "inherit";
					value = `<span style="color:${color}">${value}</span>`;
				}
			}
			return value;
		}

		return value;
	},

	get_datatable_options(options) {
		return Object.assign(options, {
			checkboxColumn: false,
			getRowHTML(row, options) {
				// indent sub-rows (Due / Payments / Net) with left padding
				const data = row[0] && row[0].content;
				if (data && data.indent === 1) {
					options.columns[0].width = 220;
				}
				return null; // use default rendering
			},
		});
	},

	after_datatable_render(datatable) {
		$(datatable.wrapper)
			.find(".dt-row")
			.each(function () {
				const firstCell = $(this).find(".dt-cell:first .dt-cell__content").text().trim();

				if (!firstCell) return;

				if (firstCell.startsWith("Grand Total")) {
					$(this).css("background", "var(--highlight-color, #f0f4e8)");
				} else if (
					!["Due Loans:", "Net Due:"].includes(firstCell) &&
					!firstCell.startsWith("Payments")
				) {
					// Officer header rows
					$(this).css({
						background: "var(--subtle-fg, #f7f7f4)",
						fontWeight: "600",
					});
				}
			});
	},
};
