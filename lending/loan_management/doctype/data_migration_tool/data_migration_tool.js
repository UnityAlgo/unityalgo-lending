// Copyright (c) 2026, Enigma Logics
// For license information, please see license.txt

frappe.ui.form.on("Data Migration Tool", {

    refresh(frm) {
        frm._poll_timer && clearInterval(frm._poll_timer);

        // ── Buttons ───────────────────────────────────────────────────
        if (frm.doc.docstatus === 0) {
            frm.add_custom_button(__("Validate Before Submit"), () => {
                frm.trigger("run_validation");
            }).addClass("btn-primary");
        }

        if (frm.doc.docstatus === 1) {
            frm.add_custom_button(__("Refresh Status"), () => {
                frm.trigger("poll_status");
            });

            if (frm.doc.errors_count > 0) {
                frm.add_custom_button(__("View Errors"), () => {
                    frappe.set_route("List", "Error Log", {
                        method: ["like", `%DataMigration-${frm.doc.name}%`]
                    });
                }).addClass("btn-danger");
            }

            // ── Post-migration tools (only after migration completed) ──
            if (frm.doc.status === "Completed") {
                frm.add_custom_button(
                    __("Reconcile Paid Amounts"),
                    () => frm.trigger("run_reconcile"),
                    __("Post-Migration Tools")
                );

                frm.add_custom_button(
                    __("Fix Repayment Statuses"),
                    () => frm.trigger("run_fix_statuses"),
                    __("Post-Migration Tools")
                );

                frm.add_custom_button(
                    __("Fix Interest/Penalty GL"),
                    () => frm.trigger("run_fix_accrual_gl"),
                    __("Post-Migration Tools")
                );
            }
        }

        // ── Status banner ─────────────────────────────────────────────
        frm.trigger("render_status_banner");

        // ── Test run warning ─────────────────────────────────────────
        if (frm.doc.test_run) {
            frm.set_intro(
                `<i class="fa fa-flask"></i> &nbsp;
                 <b>Test Run is ON</b> — only the first 50 loans will be imported.
                 Uncheck before running a full migration.`,
                "orange"
            );
        } else {
            frm.set_intro("");
        }

        // ── Auto-poll when running ────────────────────────────────────
        if (frm.doc.status === "Running" || frm.doc.status === "Queued") {
            frm._poll_timer = setInterval(() => frm.trigger("poll_status"), 4000);
        }

        // ── Account field filters ─────────────────────────────────────
        if (frm.doc.company) {
            frm.trigger("set_account_filters");
        }
    },

    // ── Status banner ─────────────────────────────────────────────────
    render_status_banner(frm) {
        frm.dashboard.clear_headline();
        const s = frm.doc.status;

        const banners = {
            "Draft":     null,
            "Queued":    ["blue",   "fa-clock-o",        "Migration is queued — waiting for background worker..."],
            "Running":   ["orange", "fa-spinner fa-spin", "Migration is running in the background..."],
            "Completed": ["green",  "fa-check-circle",
                `Migration complete &nbsp;·&nbsp;
                 Customers: <b>${frm.doc.customers_created||0}</b> &nbsp;·&nbsp;
                 Active Loans: <b>${frm.doc.loans_created||0}</b> &nbsp;·&nbsp;
                 Historical: <b>${frm.doc.historical_loans_created||0}</b> &nbsp;·&nbsp;
                 Repayments: <b>${frm.doc.repayments_created||0}</b> &nbsp;·&nbsp;
                 Errors: <b>${frm.doc.errors_count||0}</b>`
            ],
            "Failed":    ["red",    "fa-times-circle",   "Migration failed — check the log and Error Log for details."],
        };

        const b = banners[s];
        if (!b) return;
        const [color, icon, text] = b;
        frm.dashboard.set_headline_alert(
            `<div class="alert alert-${color === "orange" ? "warning" : color === "blue" ? "info" : color}">
                <i class="fa ${icon}"></i>&nbsp; ${text}
            </div>`
        );
    },

    // ── Live poll ─────────────────────────────────────────────────────
    poll_status(frm) {
        frappe.call({
            method: "lending.loan_management.doctype.data_migration_tool.data_migration_tool.get_status",
            args: { doc_name: frm.doc.name },
            callback(r) {
                if (!r.message) return;
                const s = r.message;
                frm.set_value("status",                   s.status);
                frm.set_value("customers_created",        s.customers_created);
                frm.set_value("loans_created",            s.loans_created);
                frm.set_value("historical_loans_created", s.historical_loans_created);
                frm.set_value("repayments_created",       s.repayments_created);
                frm.set_value("errors_count",             s.errors_count);
                frm.set_value("migration_log",            s.log);
                frm.trigger("render_status_banner");

                if (s.status !== "Running" && s.status !== "Queued") {
                    clearInterval(frm._poll_timer);
                    frm.refresh();
                }
            }
        });
    },

    // ── Validation dialog ─────────────────────────────────────────────
    run_validation(frm) {
        if (!frm.doc.customer_file || !frm.doc.disbursement_file || !frm.doc.repayment_file) {
            frappe.msgprint(__("Please attach all three Excel files before validating."));
            return;
        }
        if (!frm.doc.company || !frm.doc.branch || !frm.doc.sales_person) {
            frappe.msgprint(__("Please fill Company, Branch and Sales Person before validating."));
            return;
        }

        const d = new frappe.ui.Dialog({
            title: __("Pre-Migration Validation Report"),
            size:  "extra-large",
        });
        d.fields_dict = {};
        d.$body.html(
            `<div id="val-report" class="p-3 text-center">
                <i class="fa fa-spinner fa-spin fa-2x"></i>
                <p class="mt-2 text-muted">Running validation...</p>
            </div>`
        );
        d.show();

        frappe.call({
            method: "lending.loan_management.doctype.data_migration_tool.data_migration_tool.validate_migration",
            args:   { doc_name: frm.doc.name },
            callback(r) {
                if (r.exc) {
                    d.$body.find("#val-report").html(
                        `<div class="alert alert-danger"><pre>${r.exc}</pre></div>`
                    );
                    return;
                }
                d.$body.find("#val-report").html(render_validation_html(r.message));
            },
        });

        frm.save("Save").then(() => {});
    },

    // ── Reconcile Paid Amounts ─────────────────────────────────────────
    run_reconcile(frm) {
        // Step 1: dry run dialog
        const d = new frappe.ui.Dialog({
            title: __("Reconcile Loan Paid Amounts"),
            size:  "large",
        });
        d.$body.html(`
            <div class="p-3">
                <div class="alert alert-info">
                    <b>What this does:</b><br>
                    Fixes four fields on every migrated loan so they match the
                    actual submitted repayment records:<br><br>
                    &nbsp;• <code>total_principal_paid</code> — from submitted repayments<br>
                    &nbsp;• <code>total_interest_paid</code> — from submitted repayments<br>
                    &nbsp;• <code>total_interest_payable</code> — from Loan Interest Accruals<br>
                    &nbsp;• <code>total_amount_paid</code> — from submitted repayments<br>
                    &nbsp;• LIA <code>paid_interest_amount</code> / <code>paid_principal_amount</code>
                      — set to fully settled so the closure guard passes<br>
                    &nbsp;• Loan <code>status</code> — set to <i>Loan Closure Requested</i>
                      for fully paid loans
                </div>
                <div id="reconcile-result" class="text-center mt-3">
                    <i class="fa fa-spinner fa-spin fa-2x"></i>
                    <p class="text-muted mt-2">Running dry run...</p>
                </div>
            </div>
        `);
        d.show();

        // Run dry run immediately when dialog opens
        frappe.call({
            method: "lending.loan_management.doctype.data_migration_tool.data_migration_tool.reconcile_loan_paid_amounts",
            args:   { dry_run: 1 },
            callback(r) {
                if (r.exc || !r.message) {
                    d.$body.find("#reconcile-result").html(
                        `<div class="alert alert-danger">Dry run failed.<br><pre>${r.exc || "No response"}</pre></div>`
                    );
                    return;
                }
                const res = r.message;
                d.$body.find("#reconcile-result").html(
                    render_reconcile_result(res, true)
                );

                // Add Apply button only if there are changes
                if (res.updated > 0) {
                    d.set_primary_action(__("Apply Changes"), () => {
                        d.$body.find("#reconcile-result").html(`
                            <div class="text-center p-3">
                                <i class="fa fa-spinner fa-spin fa-2x"></i>
                                <p class="text-muted mt-2">Applying changes to ${res.updated} loans...</p>
                            </div>
                        `);
                        d.disable_primary_action();

                        frappe.call({
                            method: "lending.loan_management.doctype.data_migration_tool.data_migration_tool.reconcile_loan_paid_amounts",
                            args:   { dry_run: 0 },
                            callback(r2) {
                                if (r2.exc || !r2.message) {
                                    d.$body.find("#reconcile-result").html(
                                        `<div class="alert alert-danger">Apply failed.<br><pre>${r2.exc || "No response"}</pre></div>`
                                    );
                                    return;
                                }
                                d.$body.find("#reconcile-result").html(
                                    render_reconcile_result(r2.message, false)
                                );
                                d.hide_primary_action();
                                frappe.show_alert({
                                    message: __(`Reconciliation complete — ${r2.message.updated} loans updated`),
                                    indicator: "green"
                                });
                            }
                        });
                    });
                } else {
                    d.$body.find("#reconcile-result").prepend(
                        `<div class="alert alert-success mb-3">
                            <i class="fa fa-check"></i> All paid amounts are already correct. No changes needed.
                        </div>`
                    );
                }
            }
        });
    },

    // ── Fix Repayment Statuses ─────────────────────────────────────────
    run_fix_statuses(frm) {
        const d = new frappe.ui.Dialog({
            title: __("Fix Repayment Statuses"),
            size:  "large",
        });
        d.$body.html(`
            <div class="p-3">
                <div class="alert alert-info">
                    <b>What this does:</b><br>
                    Calls <code>set_repayment_status()</code> on every migrated loan
                    to set the correct <code>repayment_status</code> field:<br><br>
                    &nbsp;• <b>Fully Paid</b> — Closed / Loan Closure Requested loans<br>
                    &nbsp;• <b>Past Maturity</b> — active loans past final due date<br>
                    &nbsp;• <b>Arrears</b> — partial payment or &gt;1 past-due row<br>
                    &nbsp;• <b>Missed Repayment</b> — 30+ days since oldest unpaid row<br>
                    &nbsp;• <b>Overdue</b> — 1–29 days since oldest unpaid row<br>
                    &nbsp;• <b>Due Today</b> — payment due exactly today, unpaid<br>
                    &nbsp;• <b>Regular</b> — no past-due rows
                </div>
                <div id="status-result" class="text-center mt-3">
                    <i class="fa fa-spinner fa-spin fa-2x"></i>
                    <p class="text-muted mt-2">Running dry run...</p>
                </div>
            </div>
        `);
        d.show();

        // Run dry run immediately
        frappe.call({
            method: "lending.loan_management.doctype.data_migration_tool.data_migration_tool.fix_repayment_statuses",
            args:   { dry_run: 1 },
            callback(r) {
                if (r.exc || !r.message) {
                    d.$body.find("#status-result").html(
                        `<div class="alert alert-danger">Dry run failed.<br><pre>${r.exc || "No response"}</pre></div>`
                    );
                    return;
                }
                const res = r.message;
                d.$body.find("#status-result").html(
                    render_status_result(res, true)
                );

                if (res.updated > 0) {
                    d.set_primary_action(__("Apply Changes"), () => {
                        d.$body.find("#status-result").html(`
                            <div class="text-center p-3">
                                <i class="fa fa-spinner fa-spin fa-2x"></i>
                                <p class="text-muted mt-2">Updating ${res.updated} loan statuses...</p>
                            </div>
                        `);
                        d.disable_primary_action();

                        frappe.call({
                            method: "lending.loan_management.doctype.data_migration_tool.data_migration_tool.fix_repayment_statuses",
                            args:   { dry_run: 0 },
                            callback(r2) {
                                if (r2.exc || !r2.message) {
                                    d.$body.find("#status-result").html(
                                        `<div class="alert alert-danger">Apply failed.<br><pre>${r2.exc || "No response"}</pre></div>`
                                    );
                                    return;
                                }
                                d.$body.find("#status-result").html(
                                    render_status_result(r2.message, false)
                                );
                                d.hide_primary_action();
                                frappe.show_alert({
                                    message: __(`Repayment statuses updated — ${r2.message.updated} loans changed`),
                                    indicator: "green"
                                });
                            }
                        });
                    });
                } else {
                    d.$body.find("#status-result").prepend(
                        `<div class="alert alert-success mb-3">
                            <i class="fa fa-check"></i> All repayment statuses are already correct.
                        </div>`
                    );
                }
            }
        });
    },

    // ── Fix Interest/Penalty GL ───────────────────────────────────────────────
    run_fix_accrual_gl(frm) {
        const d = new frappe.ui.Dialog({
            title: __("Fix Interest / Penalty GL Entries"),
            size:  "large",
        });
        d.$body.html(`
            <div class="p-3">
                <div class="alert alert-warning">
                    <b>What this does:</b><br>
                    During migration, Loan Interest Accrual records were saved directly
                    to <code>docstatus=1</code> without running <code>on_submit()</code>.
                    This means the accrual GL entries were <b>never created</b>:<br><br>
                    &nbsp;• <b>Missing:</b> DR Interest Receivable / CR Interest Income<br>
                    &nbsp;• <b>Missing:</b> DR Penalty Receivable / CR Penalty Income<br><br>
                    Repayments DID credit these accounts, leaving phantom credit balances.
                    This correction creates the missing debit entries so the accounts zero out.
                </div>
                <div id="gl-fix-result" class="text-center mt-3">
                    <i class="fa fa-spinner fa-spin fa-2x"></i>
                    <p class="text-muted mt-2">Running dry run...</p>
                </div>
            </div>
        `);
        d.show();

        frappe.call({
            method: "lending.loan_management.doctype.data_migration_tool.data_migration_tool.create_missing_accrual_gl_entries",
            args:   { dry_run: 1 },
            callback(r) {
                if (r.exc || !r.message) {
                    d.$body.find("#gl-fix-result").html(
                        `<div class="alert alert-danger">Dry run failed.<br><pre>${r.exc || "No response"}</pre></div>`
                    );
                    return;
                }
                const res = r.message;
                const fmt = n => Number(n || 0).toLocaleString();
                const cur = n => "\u20a8 " + Number(n || 0).toLocaleString("en-PK", {maximumFractionDigits: 0});

                let html = `
                <div class="alert alert-warning mb-3">
                    <i class="fa fa-eye"></i> <b>DRY RUN \u2014 no changes written</b><br>
                    LIAs checked: <b>${fmt(res.total_lias)}</b>
                    &nbsp;\u00b7&nbsp; Would create GL for: <b>${fmt(res.created)}</b>
                    &nbsp;\u00b7&nbsp; Already have GL (skip): <b>${fmt(res.skipped_existing)}</b>
                    &nbsp;\u00b7&nbsp; Errors: <b>${fmt(res.errors)}</b>
                </div>
                <table class="table table-sm table-bordered mb-3">
                    <tr><td>Total Interest to correct</td><td><b>${cur(res.total_interest_corrected)}</b></td></tr>
                    <tr><td>Total Penalty to correct</td><td><b>${cur(res.total_penalty_corrected)}</b></td></tr>
                    <tr class="success"><td><b>Total GL correction</b></td><td><b>${cur(res.total_corrected)}</b></td></tr>
                </table>`;

                if ((res.details || []).length > 0) {
                    html += `<p class="text-muted mb-1"><small>Sample entries (first 10):</small></p>
                    <table class="table table-sm table-bordered" style="font-size:11px">
                        <thead><tr><th>LIA</th><th>Loan</th><th>Date</th><th>Type</th><th>Amount</th><th>DR</th><th>CR</th></tr></thead>
                        <tbody>`;
                    res.details.slice(0, 10).forEach(function(d2) {
                        html += `<tr>
                            <td>${d2.lia}</td>
                            <td><a href="/app/loan/${d2.loan}" target="_blank">${d2.loan}</a></td>
                            <td>${d2.posting_date}</td>
                            <td>${d2.type}</td>
                            <td>${cur(d2.amount)}</td>
                            <td><small>${d2.dr_account}</small></td>
                            <td><small>${d2.cr_account}</small></td>
                        </tr>`;
                    });
                    html += `</tbody></table>`;
                }

                if (res.created > 0) {
                    html += `<p class="text-muted mt-2">
                        <i class="fa fa-info-circle"></i>
                        Click <b>Apply Correction</b> to create the missing GL entries.
                    </p>`;
                }

                d.$body.find("#gl-fix-result").html(html);

                if (res.created > 0) {
                    d.set_primary_action(__("Apply Correction"), function() {
                        d.$body.find("#gl-fix-result").html(
                            '<div class="text-center p-3"><i class="fa fa-spinner fa-spin fa-2x"></i>' +
                            '<p class="text-muted mt-2">Creating GL entries for ' + fmt(res.created) + ' accruals...</p></div>'
                        );
                        d.disable_primary_action();

                        frappe.call({
                            method: "lending.loan_management.doctype.data_migration_tool.data_migration_tool.create_missing_accrual_gl_entries",
                            args:   { dry_run: 0 },
                            callback(r2) {
                                if (r2.exc || !r2.message) {
                                    d.$body.find("#gl-fix-result").html(
                                        `<div class="alert alert-danger">Apply failed.<br><pre>${r2.exc || "No response"}</pre></div>`
                                    );
                                    return;
                                }
                                const res2 = r2.message;
                                d.$body.find("#gl-fix-result").html(
                                    '<div class="alert alert-success">' +
                                    '<i class="fa fa-check-circle"></i> <b>GL correction complete</b><br>' +
                                    'Accruals corrected: <b>' + fmt(res2.created) + '</b>' +
                                    ' &nbsp;\u00b7&nbsp; Interest: <b>' + cur(res2.total_interest_corrected) + '</b>' +
                                    ' &nbsp;\u00b7&nbsp; Penalty: <b>' + cur(res2.total_penalty_corrected) + '</b>' +
                                    ' &nbsp;\u00b7&nbsp; Errors: <b>' + fmt(res2.errors) + '</b></div>'
                                );
                                d.hide_primary_action();
                                frappe.show_alert({ message: __("GL correction complete"), indicator: "green" });
                            }
                        });
                    });
                } else {
                    d.$body.find("#gl-fix-result").prepend(
                        '<div class="alert alert-success mb-3">' +
                        '<i class="fa fa-check"></i> All LIAs already have GL entries. No correction needed.</div>'
                    );
                }
            }
        });
    },

    // ── Account filters ───────────────────────────────────────────────
    company(frm) {
        frm.trigger("set_account_filters");
    },

    set_account_filters(frm) {
        if (!frm.doc.company) return;
        [
            "disbursement_account", "payment_account", "loan_account",
            "interest_income_account", "interest_receivable_account",
            "penalty_income_account",  "penalty_receivable_account",
        ].forEach(f => {
            frm.set_query(f, () => ({
                filters: { company: frm.doc.company, is_group: 0 }
            }));
        });
    },

    // ── On Submit (before submit confirmation) ────────────────────────
    before_submit(frm) {
        return new Promise((resolve, reject) => {
            const testWarning = frm.doc.test_run
                ? `<div class="alert alert-warning mt-2">
                       <i class="fa fa-flask"></i>
                       <b>Test Run is ON</b> — only the first 50 loans will be imported.
                   </div>`
                : "";

            frappe.confirm(
                `<b>Submitting this document will start the migration.</b><br><br>
                 The job runs in the background — you can close this page safely.<br>
                 Existing records will be skipped (idempotent).<br><br>
                 Have you run <b>Validate Before Submit</b> first?
                 ${testWarning}`,
                resolve,
                reject
            );
        });
    },
});


// ── Result renderers ──────────────────────────────────────────────────────────

function render_reconcile_result(res, is_dry_run) {
    const fmt = n => Number(n || 0).toLocaleString();
    const tag = is_dry_run ? "DRY RUN — no changes written" : "Applied";
    const cls = is_dry_run ? "alert-warning" : "alert-success";
    const ico = is_dry_run ? "fa-eye"        : "fa-check-circle";

    let h = `
    <div class="alert ${cls} mb-3">
        <i class="fa ${ico}"></i> <b>${tag}</b>
        &nbsp;·&nbsp; Total: <b>${fmt(res.total)}</b>
        &nbsp;·&nbsp; ${is_dry_run ? "Would update" : "Updated"}: <b>${fmt(res.updated)}</b>
        &nbsp;·&nbsp; Already correct: <b>${fmt(res.skipped)}</b>
        &nbsp;·&nbsp; Errors: <b>${fmt(res.errors)}</b>
    </div>`;

    // Sample changes
    if ((res.details || []).length > 0) {
        const changed = res.details.filter(d => d.loan_update && Object.keys(d.loan_update).length > 0);
        if (changed.length > 0) {
            h += `<p class="text-muted mb-1"><small>Sample changes (first ${Math.min(changed.length, 10)}):</small></p>
            <table class="table table-sm table-bordered" style="font-size:11px">
                <thead>
                    <tr>
                        <th>Loan</th>
                        <th>Field</th>
                        <th>Before</th>
                        <th>After</th>
                    </tr>
                </thead>
                <tbody>`;
            changed.slice(0, 10).forEach(d => {
                Object.entries(d.loan_update || {}).forEach(([field, after_val]) => {
                    const before_val = (d.before || {})[field] ?? "—";
                    h += `<tr>
                        <td><a href="/app/loan/${d.loan}" target="_blank">${d.loan}</a></td>
                        <td><code>${field}</code></td>
                        <td>${before_val}</td>
                        <td><b>${after_val}</b></td>
                    </tr>`;
                });
            });
            h += `</tbody></table>`;
        }
    }

    if (is_dry_run && res.updated > 0) {
        h += `<p class="text-muted mt-2">
            <i class="fa fa-info-circle"></i>
            Click <b>Apply Changes</b> to write these updates to the database.
        </p>`;
    }
    return h;
}


function render_status_result(res, is_dry_run) {
    const fmt = n => Number(n || 0).toLocaleString();
    const tag = is_dry_run ? "DRY RUN — no changes written" : "Applied";
    const cls = is_dry_run ? "alert-warning" : "alert-success";
    const ico = is_dry_run ? "fa-eye"        : "fa-check-circle";

    const statusColor = {
        "Fully Paid"      : "success",
        "Regular"         : "primary",
        "Due Today"       : "warning",
        "Overdue"         : "warning",
        "Arrears"         : "danger",
        "Missed Repayment": "danger",
        "Past Maturity"   : "danger",
    };

    let h = `
    <div class="alert ${cls} mb-3">
        <i class="fa ${ico}"></i> <b>${tag}</b>
        &nbsp;·&nbsp; Total: <b>${fmt(res.total)}</b>
        &nbsp;·&nbsp; ${is_dry_run ? "Would update" : "Updated"}: <b>${fmt(res.updated)}</b>
        &nbsp;·&nbsp; Unchanged: <b>${fmt(res.skipped)}</b>
        &nbsp;·&nbsp; Errors: <b>${fmt(res.errors)}</b>
    </div>`;

    // Before / After status counts side by side
    const before = res.before_counts || {};
    const after  = res.after_counts  || {};
    const allStatuses = [...new Set([...Object.keys(before), ...Object.keys(after)])];

    if (allStatuses.length > 0) {
        h += `<table class="table table-sm table-bordered mb-3">
            <thead>
                <tr>
                    <th>Repayment Status</th>
                    <th>Before</th>
                    <th>${is_dry_run ? "Would Be" : "After"}</th>
                    <th>Change</th>
                </tr>
            </thead>
            <tbody>`;

        allStatuses.sort((a, b) => (after[b] || 0) - (after[a] || 0)).forEach(s => {
            const b  = before[s] || 0;
            const a  = after[s]  || 0;
            const d  = a - b;
            const color = statusColor[s] || "secondary";
            const badge = `<span class="badge badge-${color}">${s || "(blank)"}</span>`;
            const diff  = d === 0 ? `<span class="text-muted">—</span>`
                        : d > 0  ? `<span class="text-success">+${fmt(d)}</span>`
                                  : `<span class="text-danger">${fmt(d)}</span>`;
            h += `<tr>
                <td>${badge}</td>
                <td>${fmt(b)}</td>
                <td><b>${fmt(a)}</b></td>
                <td>${diff}</td>
            </tr>`;
        });
        h += `</tbody></table>`;
    }

    // Sample changes
    if ((res.sample_changes || []).length > 0) {
        h += `<p class="text-muted mb-1"><small>Sample changes (first ${Math.min(res.sample_changes.length, 15)}):</small></p>
        <table class="table table-sm table-bordered" style="font-size:11px">
            <thead><tr><th>Loan</th><th>Loan Status</th><th>Before</th><th>After</th></tr></thead>
            <tbody>`;
        res.sample_changes.slice(0, 15).forEach(d => {
            const bc = statusColor[d.before] || "secondary";
            const ac = statusColor[d.after]  || "secondary";
            h += `<tr>
                <td><a href="/app/loan/${d.loan}" target="_blank">${d.loan}</a></td>
                <td><small>${d.loan_status || ""}</small></td>
                <td><span class="badge badge-${bc}">${d.before || "(blank)"}</span></td>
                <td><span class="badge badge-${ac}"><b>${d.after}</b></span></td>
            </tr>`;
        });
        h += `</tbody></table>`;
    }

    if (is_dry_run && res.updated > 0) {
        h += `<p class="text-muted mt-2">
            <i class="fa fa-info-circle"></i>
            Click <b>Apply Changes</b> to update repayment statuses in the database.
        </p>`;
    }
    return h;
}


// ── Validation HTML renderer ──────────────────────────────────────────────────

function render_validation_html(report) {
    const ok   = `<span style="color:green">✅</span>`;
    const warn = `<span style="color:orange">⚠️</span>`;
    const err  = `<span style="color:red">❌</span>`;

    const fmt  = n  => Number(n || 0).toLocaleString();
    const cur  = n  => "₨ " + Number(n || 0).toLocaleString("en-PK", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    const pct  = (a, b) => b ? ((a / b) * 100).toFixed(1) + "%" : "—";
    const diff_badge = (d) => {
        if (!d && d !== 0) return "";
        const n = Number(d);
        return n === 0
            ? `<span class="badge badge-success">Match</span>`
            : `<span class="badge badge-warning">${cur(n)} gap</span>`;
    };

    const row  = (label, val, cls = "") =>
        `<tr${cls ? ` class="${cls}"` : ""}><td>${label}</td><td><b>${val}</b></td></tr>`;

    let h = `<div style="font-size:13px;line-height:1.9;padding:8px">`;

    // ── 1. Configuration ──────────────────────────────────────────────────────
    h += `<h5>Configuration</h5>`;
    if (report.config_ok) {
        h += `<p>${ok} All configuration checks passed.</p>`;
    } else {
        (report.config_errors || []).forEach(e => h += `<p>${err} ${e}</p>`);
        h += `<div class="alert alert-danger">Fix the above errors before submitting.</div></div>`;
        return h;
    }

    if ((report.warnings || []).length) {
        report.warnings.forEach(w => h += `<p>${warn} ${w}</p>`);
    }

    // ── 2. File Row Counts ────────────────────────────────────────────────────
    const rc = report.row_counts || {};
    h += `<h5 class="mt-3">File Row Counts</h5>
    <table class="table table-sm table-bordered">
        ${row("Customers",     fmt(rc.customers))}
        ${row("Disbursements", fmt(rc.disbursements))}
        ${row("Repayments",    fmt(rc.repayments))}
    </table>`;

    // ── 3. Customers ──────────────────────────────────────────────────────────
    const c = report.customers || {};
    h += `<h5 class="mt-3">Customers</h5>
    <table class="table table-sm table-bordered">
        ${row("Will Create",          fmt(c.will_create),  "success")}
        ${row("Already Exist (skip)", fmt(c.already_exist))}
        ${row("Invalid / Skipped",    c.invalid || 0)}
    </table>`;
    h += Object.keys(c.marital_unmapped || {}).length
        ? `<p>${warn} Unmapped marital status (left blank): <code>${JSON.stringify(c.marital_unmapped)}</code></p>`
        : `<p>${ok} All marital status values map correctly.</p>`;
    h += Object.keys(c.religion_unmapped || {}).length
        ? `<p>${warn} Unmapped religion (left blank): <code>${JSON.stringify(c.religion_unmapped)}</code></p>`
        : `<p>${ok} All religion values map correctly.</p>`;

    // ── 4. Loan Products ──────────────────────────────────────────────────────
    const lp = report.loan_products || {};
    h += `<h5 class="mt-3">Loan Products</h5>
    <table class="table table-sm table-bordered">
        ${row("In Excel",           lp.in_excel || 0)}
        ${row("Already in Frappe",  lp.existing  || 0)}
        ${row("Need Account Patch", (lp.need_patch  || []).join(", ") || "None")}
        ${row("Will Create",        (lp.will_create || []).join(", ") || "None")}
    </table>`;

    // ── 5. Active Loans ───────────────────────────────────────────────────────
    const l = report.loans || {};
    h += `<h5 class="mt-3">Active Loans</h5>
    <table class="table table-sm table-bordered">
        ${row("Will Create",          fmt(l.will_create),  "success")}
        ${row("Already Exist (skip)", fmt(l.already_exist))}
        ${l.no_customer ? row("No Customer Match", l.no_customer, "warning") : ""}
    </table>`;
    h += l.no_customer
        ? `<p>${warn} ${l.no_customer} loans have no matching customer CNIC.</p>`
        : `<p>${ok} All loans have matching customers.</p>`;

    // ── 6. Repayments ─────────────────────────────────────────────────────────
    const rep = report.repayments || {};
    h += `<h5 class="mt-3">Repayments</h5>
    <table class="table table-sm table-bordered">
        ${row("Total Rows",                 fmt(rep.total))}
        ${row("Matched → Active Loans",     fmt(rep.matched_active))}
        ${row("Matched → Historical Loans", fmt(rep.matched_historical))}
        ${row("Historical Loans to Create", fmt(rep.historical_loans_to_create))}
        ${rep.no_customer ? row("No CNIC Match", rep.no_customer, "warning") : ""}
        ${rep.unmatched   ? row("Unresolvable",  rep.unmatched,   "warning") : ""}
        ${row("Total to Import", fmt(rep.total_importable), "success")}
    </table>`;

    // ── 7. Current DB Counts ──────────────────────────────────────────────────
    const cc = report.current_counts || {};
    h += `<h5 class="mt-3">Current Data in Frappe</h5>
    <table class="table table-sm table-bordered">`;
    Object.entries(cc).forEach(([dt, n]) => {
        h += `<tr><td>${dt}</td><td>${fmt(n)}</td></tr>`;
    });
    h += `</table>`;

    // ── 8. Financial Summary ──────────────────────────────────────────────────
    const fs   = report.financial_summary   || {};
    const disb = fs.disbursement            || {};
    const coll = fs.already_collected       || {};
    const bal  = fs.balance_remaining       || {};
    const byS  = fs.loans_by_status         || {};
    const rept = fs.repayment_transactions  || {};
    const xck  = rept.cross_check           || {};

    const statusColor = {
        "Fully Paid"   : "success",  "Current"      : "primary",
        "Due Today"    : "warning",  "Arrears"      : "danger",
        "Past Maturity": "danger",   "Restructured" : "info",
        "Write-Off"    : "dark",
    };

    h += `<h5 class="mt-3">💰 Disbursement File — Financial Totals</h5>
    <table class="table table-sm table-bordered">
        ${row("Total Loans",                         fmt(disb.total_loans))}
        ${row("Total Disbursed (Principal)",         cur(disb.total_disbursed))}
        ${row("Total Deductable Fees",               cur(disb.total_deductable_fees))}
        ${row("Net Cash Released to Borrowers",      cur(disb.total_net_cash_released))}
        ${row("Total Interest Expected",             cur(disb.total_interest_expected))}
        ${row("Total Payable (Principal + Interest)",cur(disb.total_payable_principal_plus_interest))}
    </table>`;

    h += `<h5 class="mt-3">✅ Already Collected (from Disbursement File)</h5>
    <table class="table table-sm table-bordered">
        <thead><tr><th>Component</th><th>Amount</th><th>% of Expected</th></tr></thead>
        <tbody>
        <tr><td>Total Paid</td><td><b>${cur(coll.total_paid)}</b></td>
            <td>${pct(coll.total_paid, disb.total_payable_principal_plus_interest)}</td></tr>
        <tr><td>Principal Paid</td><td>${cur(coll.principal_paid)}</td>
            <td>${pct(coll.principal_paid, disb.total_disbursed)}</td></tr>
        <tr><td>Interest Paid</td><td>${cur(coll.interest_paid)}</td>
            <td>${pct(coll.interest_paid, disb.total_interest_expected)}</td></tr>
        <tr><td>Penalty Paid</td><td>${cur(coll.penalty_paid)}</td><td>—</td></tr>
        </tbody>
    </table>`;

    h += `<h5 class="mt-3">📊 Balance Remaining (Active Portfolio)</h5>
    <table class="table table-sm table-bordered">
        <thead><tr><th>Component</th><th>Amount</th><th>% of Expected</th></tr></thead>
        <tbody>
        <tr><td><b>Total Outstanding</b></td><td><b>${cur(bal.total_outstanding)}</b></td>
            <td>${pct(bal.total_outstanding, disb.total_payable_principal_plus_interest)}</td></tr>
        <tr><td>Principal Balance</td><td>${cur(bal.principal_balance)}</td>
            <td>${pct(bal.principal_balance, disb.total_disbursed)}</td></tr>
        <tr><td>Interest Balance</td><td>${cur(bal.interest_balance)}</td>
            <td>${pct(bal.interest_balance, disb.total_interest_expected)}</td></tr>
        <tr><td>Penalty Balance</td><td>${cur(bal.penalty_balance)}</td><td>—</td></tr>
        </tbody>
    </table>`;

    h += `<h5 class="mt-3">📋 Loans by Source Status</h5>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px">`;
    Object.entries(byS).sort((a, b) => b[1] - a[1]).forEach(([status, count]) => {
        const color = statusColor[status] || "secondary";
        h += `<span class="badge badge-${color}" style="font-size:12px;padding:6px 10px">
                ${status || "(blank)"}: ${fmt(count)}
              </span>`;
    });
    h += `</div>`;

    h += `<h5 class="mt-3">🧾 Repayment File — Transaction Totals</h5>
    <table class="table table-sm table-bordered">
        ${row("Total Transactions",     fmt(rept.total_transactions))}
        ${row("Unique Loans",           fmt(rept.unique_loans))}
        ${row("Total Amount Collected", cur(rept.total_amount_collected))}
        ${row("Principal",              cur(rept.principal_collected))}
        ${row("Interest",               cur(rept.interest_collected))}
        ${row("Penalty",                cur(rept.penalty_collected))}
        ${rept.fees_collected ? row("Fees", cur(rept.fees_collected)) : ""}
    </table>`;

    h += `<h5 class="mt-3">🔍 Cross-Check: Disbursement File vs Repayment File</h5>
    <table class="table table-sm table-bordered">
        <thead><tr><th>Metric</th><th>Disbursement File</th><th>Repayment File</th><th>Gap</th></tr></thead>
        <tbody>
        <tr><td>Principal Paid</td>
            <td>${cur(xck.disbursement_file_principal_paid)}</td>
            <td>${cur(xck.repayment_file_principal_total)}</td>
            <td>${diff_badge(xck.difference)}</td></tr>
        <tr><td>Interest Paid</td>
            <td>${cur(xck.disbursement_file_interest_paid)}</td>
            <td>${cur(xck.repayment_file_interest_total)}</td>
            <td>${diff_badge(xck.interest_difference)}</td></tr>
        </tbody>
    </table>`;

    const prinDiff = Number(xck.difference || 0);
    const intDiff  = Number(xck.interest_difference || 0);
    const gapClass = (prinDiff > 0 || intDiff > 0) ? "alert-warning" : "alert-success";
    h += `<div class="alert ${gapClass}" style="font-size:12px">
        ${warn} <b>Note:</b> ${xck.note || ""}
    </div>`;

    const total_loans_after = (l.will_create || 0) + (l.already_exist || 0) + (rep.historical_loans_to_create || 0);
    h += `<div class="alert alert-info mt-3">
        <b>Expected after migration:</b><br>
        Customers: ~${fmt((c.will_create || 0) + (c.already_exist || 0))}
        &nbsp;|&nbsp; Loans: ~${fmt(total_loans_after)}
        &nbsp;|&nbsp; Repayments: ~${fmt(rep.total_importable)} (submitted + GL posted)
        <br><br>
        <b>Portfolio being migrated:</b><br>
        Total Principal: ${cur(disb.total_disbursed)}
        &nbsp;|&nbsp; Outstanding: ${cur(bal.total_outstanding)}
        &nbsp;|&nbsp; Fees to book: ${cur(disb.total_deductable_fees)}
    </div>`;

    h += `</div>`;
    return h;
}