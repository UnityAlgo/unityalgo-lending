// Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and Contributors
// License: GNU General Public License v3. See license.txt

frappe.listview_settings['Loan'] = {
	get_indicator: function (doc) {
		var status_color = {
			"Draft": "red",
			"Sanctioned": "blue",
			"Disbursed": "orange",
			"Partially Disbursed": "yellow",
			"Loan Closure Requested": "green",
			"Closed": "green"
		};
		return [__(doc.status), status_color[doc.status], "status,=," + doc.status];
	},
	onload: function (listview) {
		listview.page.add_inner_button(__("Disburse Loan"), function () {
			const selected = listview.get_checked_items()
			const loans = selected.map(i => i.name).filter(Boolean)

			if (!loans.length) {
				frappe.throw("No loans selected for disbursement")
			}

			frappe.call({
				method: "lending.loan_management.doctype.loan.loan.make_bulk_disbursement",
				args: {
					loans: JSON.stringify(loans)
				},
				callback(res) {
					listview.refresh()
					frappe.msgprint(res.message)
				}
			})
			console.log()
		});
	}
};
