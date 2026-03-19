// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Loan Reschedule', {
    setup(frm) {
        frm.set_query('loan', function () {
            return {
                filters: {
                    docstatus: 1,
                    is_term_loan: 1,
                    status: ['not in', ['Closed', 'Loan Closure Requested']],
                }
            };
        });
    },

    loan: function (frm) {
        if (frm.doc.loan) {
            frappe.db.get_value('Loan', frm.doc.loan, [
                'rate_of_interest', 'repayment_method',
                'repayment_periods', 'loan_product'
            ]).then(r => {
                if (r.message) {
                    frm.set_value('new_rate_of_interest', r.message.rate_of_interest);
                    frm.set_value('new_repayment_method', r.message.repayment_method || 'Repay Over Number of Periods');
                    frm.set_value('loan_product', r.message.loan_product);
                }
            });
        }
    },

    new_rate_of_interest: function (frm) {
        frm.trigger('refresh_preview');
    },

    new_repayment_periods: function (frm) {
        frm.trigger('refresh_preview');
    },

    new_repayment_start_date: function (frm) {
        frm.trigger('refresh_preview');
    },

    new_repayment_method: function (frm) {
        frm.trigger('refresh_preview');
    },

    new_monthly_repayment_amount: function (frm) {
        frm.trigger('refresh_preview');
    },

    refresh_preview: function (frm) {
        if (frm.doc.loan
            && frm.doc.new_rate_of_interest
            && frm.doc.new_repayment_periods
            && frm.doc.new_repayment_start_date) {
            // Re-validate to regenerate preview
            frm.dirty();
        }
    },

    refresh: function (frm) {
        if (frm.doc.docstatus === 0 && frm.doc.new_principal > 0) {
            frm.add_custom_button(__('Refresh Preview'), function () {
                frm.save();
            });
        }
    }
});