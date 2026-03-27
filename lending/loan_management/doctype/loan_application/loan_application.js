// Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

lending.common.setup_filters("Loan Application");


const ALLOWED_ROLES = ["System Manager"];

frappe.ui.form.on('Loan Application', {
	validate: function (frm) {
		const { latitude, longitude } = frm.doc;


		if (latitude && longitude) return;
		if (!("geolocation" in navigator)) {
			if (
				frappe.session.user !== "Administrator" &&
				!ALLOWED_ROLES.some(role => frappe.user.has_role(role))
			) {
				frappe.throw(__("Geolocation is not supported by your browser."));
			}
			return;
		}

		frappe.validated = false;
		navigator.geolocation.getCurrentPosition(
			(position) => {
				frm.set_value("latitude", position.coords.latitude);
				frm.set_value("longitude", position.coords.longitude);
				frm.save();
			},
			() => {
				frappe.throw(__(
					"Could not fetch geolocation. Please allow location access and save the document again."
				));
			}
		);
	},
	setup: function (frm) {

		if (frm.doc.latitude && frm.doc.longitude) {
			// ÷÷			frm.set_df_property('title', 'reqd', 1)

			frm.set_df_property("map_display", "options", `
  <div style="width:100%;">
    <iframe
      width="100%"
      height="500"
      style="border:0;"
      loading="lazy"
      allowfullscreen
      src="https://maps.google.com/maps?q=${frm.doc.latitude},${frm.doc.longitude}&z=15&output=embed">
    </iframe>
  </div>
`)
		}
		// if (frm.is_new() || !frm.doc.guarantor.length == 2) {
		// 	frm.doc.guarantor = []
		// 	frm.add_child("guarantor", {})
		// 	frm.add_child("guarantor", {})

		// 	frm.get_field("guarantor").grid.only_sortable();
		// 	frm.get_field("guarantor").grid.cannot_add_rows = true;

		// 	frm.refresh_field("guarantor")
		// }

		frm.make_methods = {
			'Loan': function () { frm.trigger('create_loan') },
			'Loan Security Pledge': function () { frm.trigger('create_loan_security_pledge') },
		}
	},
	refresh: function (frm) {
		frm.trigger("toggle_fields");
		frm.trigger("add_toolbar_buttons");
		frm.set_query('loan_product', () => {
			return {
				filters: {
					company: frm.doc.company
				}
			};
		});

		frm.set_query("guarantor", "guarantor", function () {
			const applicants = new Set([frm.doc.applicant]);

			frm.doc.guarantor.forEach(row => {
				if (row.guarantor) {
					applicants.add(row.guarantor)
				}
			})


			return {
				filters: {
					"name": ["NOT IN", [...applicants]]
				}
			}
		})
	},
	loan_product(frm) {
		const { loan_product } = frm.doc;
		if (loan_product) {
			frappe.db.get_doc("Loan Product", loan_product).then(doc => {
				frm.set_value("repayment_method", doc.repayment_method);
				frm.set_value("repayment_amount", doc.repayment_amount);
				frm.set_value("repayment_periods", doc.repayment_periods);
			})
		} else {
			frm.set_value("repayment_method", "");
			frm.set_value("repayment_amount", 0);
			frm.set_value("repayment_periods", 0);
		}
	},
	repayment_method: function (frm) {
		// frm.doc.repayment_amount = frm.doc.repayment_periods = "";
		// frm.trigger("toggle_fields");
		// frm.trigger("toggle_required");
	},
	toggle_fields: function (frm) {
		frm.toggle_enable("repayment_amount", frm.doc.repayment_method == "Repay Fixed Amount per Period")
		frm.toggle_enable("repayment_periods", frm.doc.repayment_method == "Repay Over Number of Periods")
	},
	toggle_required: function (frm) {
		frm.toggle_reqd("repayment_amount", cint(frm.doc.repayment_method == 'Repay Fixed Amount per Period'))
		frm.toggle_reqd("repayment_periods", cint(frm.doc.repayment_method == 'Repay Over Number of Periods'))
	},
	add_toolbar_buttons: function (frm) {
		if (frm.doc.status == "Approved") {

			if (frm.doc.is_secured_loan) {
				frappe.db.get_value("Loan Security Pledge", { "loan_application": frm.doc.name, "docstatus": 1 }, "name", (r) => {
					if (Object.keys(r).length === 0) {
						frm.add_custom_button(__('Loan Security Pledge'), function () {
							frm.trigger('create_loan_security_pledge');
						}, __('Create'))
					}
				});
			}

			frappe.db.get_value("Loan", { "loan_application": frm.doc.name, "docstatus": 1 }, "name", (r) => {
				if (Object.keys(r).length === 0) {
					frm.add_custom_button(__('Loan'), function () {
						frm.trigger('create_loan');
					}, __('Create'))
				} else {
					frm.set_df_property('status', 'read_only', 1);
				}
			});
		}
	},
	create_loan: function (frm) {
		if (frm.doc.status != "Approved") {
			frappe.throw(__("Cannot create loan until application is approved"));
		}

		frappe.model.open_mapped_doc({
			method: 'lending.loan_management.doctype.loan_application.loan_application.create_loan',
			frm: frm
		});
	},
	create_loan_security_pledge: function (frm) {

		if (!frm.doc.is_secured_loan) {
			frappe.throw(__("Loan Security Pledge can only be created for secured loans"));
		}

		frappe.call({
			method: "lending.loan_management.doctype.loan_application.loan_application.create_pledge",
			args: {
				loan_application: frm.doc.name
			},
			callback: function (r) {
				frappe.set_route("Form", "Loan Security Pledge", r.message);
			}
		})
	},
	is_term_loan: function (frm) {
		frm.set_df_property('repayment_method', 'hidden', 1 - frm.doc.is_term_loan);
		frm.set_df_property('repayment_method', 'reqd', frm.doc.is_term_loan);
	},
	is_secured_loan: function (frm) {
		frm.set_df_property('proposed_pledges', 'reqd', frm.doc.is_secured_loan);
	},

	calculate_amounts: function (frm, cdt, cdn) {
		let row = locals[cdt][cdn];
		if (row.qty) {
			frappe.model.set_value(cdt, cdn, 'amount', row.qty * row.loan_security_price);
			frappe.model.set_value(cdt, cdn, 'post_haircut_amount', cint(row.amount - (row.amount * row.haircut / 100)));
		} else if (row.amount) {
			frappe.model.set_value(cdt, cdn, 'qty', cint(row.amount / row.loan_security_price));
			frappe.model.set_value(cdt, cdn, 'amount', row.qty * row.loan_security_price);
			frappe.model.set_value(cdt, cdn, 'post_haircut_amount', cint(row.amount - (row.amount * row.haircut / 100)));
		}

		let maximum_amount = 0;

		$.each(frm.doc.proposed_pledges || [], function (i, item) {
			maximum_amount += item.post_haircut_amount;
		});

		if (flt(maximum_amount)) {
			frm.set_value('maximum_loan_amount', flt(maximum_amount));
		}
	}
});


frappe.ui.form.on("Loan Guarantor", {
	guarantor: (frm, cdt, cdn) => {
		const { guarantor } = frm.doc;

		frappe.model.set_value(cdt, cdn, "outstanding_balance", 0);

		if (guarantor) {
			frappe.call({
				method: "erpnext.accounts.utils.get_balance_on",
				args: {
					date: frappe.datetime.now_date(),
					party_type: 'Customer',
					party: guarantor
				},
				callback: function (r) {
					frappe.model.set_value(cdt, cdn, "outstanding_balance", flt(r.message))
				}
			});
		}
	}
})


frappe.ui.form.on("Proposed Pledge", {
	loan_security: function (frm, cdt, cdn) {
		let row = locals[cdt][cdn];

		if (row.loan_security) {
			frappe.call({
				method: "lending.loan_management.doctype.loan_security_price.loan_security_price.get_loan_security_price",
				args: {
					loan_security: row.loan_security
				},
				callback: function (r) {
					frappe.model.set_value(cdt, cdn, 'loan_security_price', r.message);
					frm.events.calculate_amounts(frm, cdt, cdn);
				}
			})
		}
	},

	amount: function (frm, cdt, cdn) {
		frm.events.calculate_amounts(frm, cdt, cdn);
	},

	qty: function (frm, cdt, cdn) {
		frm.events.calculate_amounts(frm, cdt, cdn);
	},
})
