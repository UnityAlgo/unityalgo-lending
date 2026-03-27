// Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt


function calculate_loan_charges(frm) {
	const loan_amount = frm.doc.maximum_loan_amount;

	frm.doc.loan_charges?.forEach(row => {
		const { percentage } = row;
		if (flt(percentage) > 0) {
			let value = (flt(loan_amount) / 100) * flt(percentage)
			frappe.model.set_value(row.doctype, row.name, "amount", value)
			console.log(row, value)
		}
	})
}
frappe.ui.form.on('Loan Product', {
	validate(frm) {
		calculate_loan_charges(frm)
	},
	setup(frm) {
		frm.add_fetch("company", "min_days_bw_disbursement_first_repayment", "min_days_bw_disbursement_first_repayment");
	},
	repayment_method: function (frm) {
		frm.doc.repayment_amount = frm.doc.repayment_periods = "";
	},


	onload: function (frm) {
		$.each(["penalty_income_account", "interest_income_account"], function (i, field) {
			frm.set_query(field, function () {
				return {
					"filters": {
						"company": frm.doc.company,
						"root_type": "Income",
						"is_group": 0
					}
				};
			});
		});

		$.each(["payment_account", "loan_account", "disbursement_account"], function (i, field) {
			frm.set_query(field, function () {
				return {
					"filters": {
						"company": frm.doc.company,
						"root_type": "Asset",
						"is_group": 0
					}
				};
			});
		});
	}
});


frappe.ui.form.on('Loan Charges', {
	percentage(frm, cdt, cdn) {
		calculate_loan_charges(frm)
	},
	charge_type: function (frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "percentage", 0)
		frappe.model.set_value(cdt, cdn, "amount", 0)
	},
});
