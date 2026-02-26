// Copyright (c) 2026, SalmanIrfan88 and contributors
// For license information, please see license.txt

frappe.ui.form.on("Expense Entry", {
    setup: function (frm) {
        // Filter Expense Account - Only Indirect Expense accounts
        frm.set_query("expense_account", function () {
            return {
                filters: {
                    company: frm.doc.company,
                    root_type: "Expense",
                    is_group: 0,
                    account_type: ["in", ["Expense Account", "Indirect Expense", ""]],
                },
            };
        });

        // Filter Payment Account - Only Cash and Bank accounts
        frm.set_query("payment_account", function () {
            return {
                filters: {
                    company: frm.doc.company,
                    account_type: ["in", ["Cash", "Bank"]],
                    is_group: 0,
                },
            };
        });

        frm.set_query("cost_center", function () {
            return {
                filters: {
                    company: frm.doc.company,
                },
            };
        });
        frm.set_query("warehouse", function () {
            return {
                filters: {
                    company: frm.doc.company,
                },
            };
        });
        // Filter Tax Template by Company
        frm.set_query("tax_template", function () {
            return {
                filters: {
                    company: frm.doc.company,
                },
            };
        });
    },

    refresh: function (frm) {
        // Add custom button to view Journal Entry
        if (frm.doc.journal_entry && frm.doc.docstatus === 1) {
            frm.add_custom_button(__("View Journal Entry"), function () {
                frappe.set_route("Form", "Journal Entry", frm.doc.journal_entry);
            });
        }

        // Set default company if only one exists
        if (!frm.doc.company && frm.is_new()) {
            frappe.db.get_list("Company", { limit: 2 }).then((companies) => {
                if (companies.length === 1) {
                    frm.set_value("company", companies[0].name);
                }
            });
        }
    },

    company: function (frm) {
        // Clear accounts when company changes
        if (frm.doc.expense_account) {
            frappe.db.get_value("Account", frm.doc.expense_account, "company").then((r) => {
                if (r.message && r.message.company !== frm.doc.company) {
                    frm.set_value("expense_account", "");
                }
            });
        }

        if (frm.doc.payment_account) {
            frappe.db.get_value("Account", frm.doc.payment_account, "company").then((r) => {
                if (r.message && r.message.company !== frm.doc.company) {
                    frm.set_value("payment_account", "");
                }
            });
        }

        if (frm.doc.tax_template) {
            frappe.db.get_value(
                "Purchase Taxes and Charges Template",
                frm.doc.tax_template,
                "company"
            ).then((r) => {
                if (r.message && r.message.company !== frm.doc.company) {
                    frm.set_value("tax_template", "");
                }
            });
        }
    },

    is_vat_applicable: function (frm) {
        // Clear VAT fields when VAT is disabled
        if (!frm.doc.is_vat_applicable) {
            frm.set_value("vat_type", "");
            frm.set_value("tax_template", "");
            frm.set_value("tax_amount", 0);
        }
        calculate_totals(frm);
    },

    vat_type: function (frm) {
        calculate_totals(frm);
    },

    tax_template: function (frm) {
        calculate_totals(frm);
    },

    amount: function (frm) {
        calculate_totals(frm);
    },
});

function calculate_totals(frm) {
    let amount = flt(frm.doc.amount);
    let tax_amount = 0;
    let total_amount = amount;

    if (frm.doc.is_vat_applicable && frm.doc.tax_template && amount > 0) {
        // Get tax rate from template
        frappe.db
            .get_list("Purchase Taxes and Charges", {
                filters: { parent: frm.doc.tax_template },
                fields: ["rate"],
                order_by: "idx",
                limit: 1,
            })
            .then((taxes) => {
                if (taxes && taxes.length > 0) {
                    let tax_rate = flt(taxes[0].rate);

                    if (frm.doc.vat_type === "Exclusive") {
                        // Tax added on top
                        tax_amount = (amount * tax_rate) / 100;
                        total_amount = amount + tax_amount;
                    } else if (frm.doc.vat_type === "Inclusive") {
                        // Tax included in amount
                        tax_amount = (amount * tax_rate) / (100 + tax_rate);
                        total_amount = amount;
                    }

                    frm.set_value("tax_amount", tax_amount);
                    frm.set_value("total_amount", total_amount);
                }
            });
    } else {
        frm.set_value("tax_amount", 0);
        frm.set_value("total_amount", amount);
    }
}