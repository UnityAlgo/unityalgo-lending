# Copyright (c) 2026, UnityAlgo

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate


def execute(filters=None):
    filters = frappe._dict(filters or {})
    if not filters.as_of_date:
        filters.as_of_date = nowdate()

    columns = get_columns()
    data = get_data(filters)
    summary = get_summary(data)

    return columns, data, None, None, summary


def get_columns():
    return [
        {
            "label": _("Branch"),
            "fieldname": "branch",
            "fieldtype": "Link",
            "options": "Branch",
            "width": 140,
        },
        {
            "label": _("Loan Officer"),
            "fieldname": "loan_officer",
            "fieldtype": "Link",
            "options": "User",
            "width": 160,
        },
        {
            "label": _("Loan Product"),
            "fieldname": "loan_product",
            "fieldtype": "Link",
            "options": "Loan Product",
            "width": 150,
        },
        {
            "label": _("Client Name"),
            "fieldname": "applicant_name",
            "fieldtype": "Data",
            "width": 180,
        },
        {
            "label": _("Loan Account"),
            "fieldname": "name",
            "fieldtype": "Link",
            "options": "Loan",
            "width": 160,
        },
        {
            "label": _("Disbursement Date"),
            "fieldname": "disbursement_date",
            "fieldtype": "Date",
            "width": 140,
        },
        {
            "label": _("Loan Amount"),
            "fieldname": "loan_amount",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": _("Disbursed Amount"),
            "fieldname": "disbursed_amount",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": _("Total Principal Paid"),
            "fieldname": "total_principal_paid",
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "label": _("Written Off"),
            "fieldname": "written_off_amount",
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "label": _("Outstanding Principal (OLP)"),
            "fieldname": "outstanding_principal",
            "fieldtype": "Currency",
            "width": 200,
        },
        {
            "label": _("Loan Status"),
            "fieldname": "status",
            "fieldtype": "Data",
            "width": 160,
        },
        {
            "label": _("Repayment Status"),
            "fieldname": "repayment_status",
            "fieldtype": "Data",
            "width": 160,
        },
        {
            "label": _("Days Past Due"),
            "fieldname": "days_past_due",
            "fieldtype": "Int",
            "width": 120,
        },
        {
            "label": _("NPA"),
            "fieldname": "is_npa",
            "fieldtype": "Check",
            "width": 80,
        },
    ]


def get_data(filters):
    conditions = get_conditions(filters)
    loans = fetch_loans(filters, conditions)
    return build_grouped_rows(loans)


def fetch_loans(filters, conditions):
    precision = cint(frappe.db.get_default("currency_precision")) or 2

    # ── OLP formula mirrors get_pending_principal_amount() in loan_repayment.py ──
    #
    # Fully disbursed branch  (status IN ('Disbursed','Closed') OR disbursed >= loan_amount):
    #   total_payment + debit_adjustment_amount - credit_adjustment_amount
    #   - total_principal_paid - total_interest_payable - written_off_amount + refund_amount
    #
    # Partially disbursed branch:
    #   disbursed_amount + debit_adjustment_amount - credit_adjustment_amount
    #   - total_principal_paid - total_interest_payable - written_off_amount + refund_amount
    #
    # 'Loan Closure Requested' status is INCLUDED — principal is nearly 0 but
    # the loan is not formally closed; it awaits manual approval per on_submit logic.

    sql = """
        SELECT
            l.name,
            l.applicant_name,
            l.applicant_type,
            l.loan_product,
            l.branch,
            l.sales_person as loan_officer,
            l.disbursement_date,
            l.loan_amount,
            l.disbursed_amount,
            l.total_payment,
            l.total_principal_paid,
            l.total_interest_payable,
            l.written_off_amount,
            l.debit_adjustment_amount,
            l.credit_adjustment_amount,
            l.refund_amount,
            l.status,
            l.repayment_status,
            l.days_past_due,
            l.is_npa,
            l.manual_npa,

            -- OLP mirrors get_pending_principal_amount() exactly
            ROUND(
                (
                      IFNULL(l.disbursed_amount, 0)
                    + IFNULL(l.debit_adjustment_amount, 0)
                    - IFNULL(l.credit_adjustment_amount, 0)
                    - IFNULL(l.total_principal_paid, 0)
                    - IFNULL(l.written_off_amount, 0)
                    + IFNULL(l.refund_amount, 0)
                ),
                {precision}
            ) AS outstanding_principal

        FROM `tabLoan` l
        WHERE
            l.docstatus = 1
            AND l.status IN (
                'Disbursed',
                'Partially Disbursed',
                'Loan Closure Requested'
            )
            -- AND l.disbursement_date <= %(as_of_date)s
            {conditions}
        ORDER BY
            l.branch,
            l.loan_product,
            l.applicant_name
    """.format(
        precision=precision,
        conditions=conditions,
    )

    return frappe.db.sql(sql, filters, as_dict=True, debug=True)


def get_conditions(filters):
    conditions = []

    # --- ADD THIS BLOCK ---
    if filters.get("loan"):
        conditions.append("AND l.name = %(loan)s")

    if filters.get("branch"):
        conditions.append("AND l.branch = %(branch)s")

    if filters.get("loan_product"):
        conditions.append("AND l.loan_product = %(loan_product)s")

    if filters.get("loan_officer"):
        conditions.append("AND l.sales_person = %(loan_officer)s")

    # Repayment health filter — values set by update_loan_repayment_status():
    #   Regular / Overdue / Missed Repayment / Arrears / Past Maturity
    if filters.get("repayment_status"):
        conditions.append("AND l.repayment_status = %(repayment_status)s")

    # NPA only — mirrors is_npa / manual_npa flags used in on_cancel NPA logic
    if filters.get("npa_only"):
        conditions.append("AND (l.is_npa = 1 OR l.manual_npa = 1)")

    return " ".join(conditions)


def build_grouped_rows(loans):
    from itertools import groupby

    result = []

    loans_sorted = sorted(
        loans,
        key=lambda r: (r.branch or "", r.loan_product or "", r.applicant_name or ""),
    )

    for branch, branch_iter in groupby(loans_sorted, key=lambda r: r.branch or ""):
        branch_rows = list(branch_iter)
        branch_olp = 0
        branch_loan_amount = 0
        branch_disbursed = 0
        branch_principal_paid = 0
        branch_written_off = 0
        branch_clients = 0

        for product, product_iter in groupby(
            branch_rows, key=lambda r: r.loan_product or ""
        ):
            product_rows = list(product_iter)

            product_olp = sum(flt(r.outstanding_principal) for r in product_rows)
            product_loan_amount = sum(flt(r.loan_amount) for r in product_rows)
            product_disbursed = sum(flt(r.disbursed_amount) for r in product_rows)
            product_principal_paid = sum(
                flt(r.total_principal_paid) for r in product_rows
            )
            product_written_off = sum(flt(r.written_off_amount) for r in product_rows)
            product_clients = len(product_rows)

            for row in product_rows:
                if row.status == "Loan Closure Requested":
                    row["_closure_pending"] = 1
                result.append(row)

            result.append(
                frappe._dict(
                    {
                        "applicant_name": _("Subtotal — {0}").format(
                            product or _("No Product")
                        ),
                        "outstanding_principal": product_olp,
                        "loan_amount": product_loan_amount,
                        "disbursed_amount": product_disbursed,
                        "total_principal_paid": product_principal_paid,
                        "written_off_amount": product_written_off,
                        "bold": 1,
                        "_is_subtotal": 1,
                        "_active_clients": product_clients,
                    }
                )
            )

            branch_olp += product_olp
            branch_loan_amount += product_loan_amount
            branch_disbursed += product_disbursed
            branch_principal_paid += product_principal_paid
            branch_written_off += product_written_off
            branch_clients += product_clients

        result.append(
            frappe._dict(
                {
                    "applicant_name": _("Branch Total — {0}").format(
                        branch or _("No Branch")
                    ),
                    "outstanding_principal": branch_olp,
                    "loan_amount": branch_loan_amount,
                    "disbursed_amount": branch_disbursed,
                    "total_principal_paid": branch_principal_paid,
                    "written_off_amount": branch_written_off,
                    "bold": 1,
                    "_is_branch_total": 1,
                    "_active_clients": branch_clients,
                }
            )
        )

    return result


def get_summary(data):
    detail_rows = [r for r in data if r.get("name")]

    total_olp = sum(flt(r.outstanding_principal) for r in detail_rows)
    total_disbursed = sum(flt(r.disbursed_amount) for r in detail_rows)
    total_principal_paid = sum(flt(r.total_principal_paid) for r in detail_rows)
    total_written_off = sum(flt(r.written_off_amount) for r in detail_rows)
    active_clients = len(detail_rows)

    # Closure pending — 'Loan Closure Requested' (non-employee loans after full repayment)
    closure_pending = sum(
        1 for r in detail_rows if r.get("status") == "Loan Closure Requested"
    )

    # OLP at risk — any repayment_status set by update_loan_repayment_status()
    # that indicates the client is behind: Overdue, Missed Repayment, Arrears, Past Maturity
    at_risk_statuses = {
        "Overdue",
        "Missed Repayment",
        "Arrears",
        "Past Maturity",
        "Due Today",
    }
    overdue_olp = sum(
        flt(r.outstanding_principal)
        for r in detail_rows
        if r.get("repayment_status") in at_risk_statuses
    )

    return [
        {
            "value": active_clients,
            "label": _("Active Clients"),
            "datatype": "Int",
            "indicator": "blue",
        },
        {
            "value": total_olp,
            "label": _("Total OLP (Outstanding Principal)"),
            "datatype": "Currency",
            "indicator": "orange",
        },
        {
            "value": total_disbursed,
            "label": _("Total Disbursed Amount"),
            "datatype": "Currency",
            "indicator": "green",
        },
        {
            "value": total_principal_paid,
            "label": _("Total Principal Recovered"),
            "datatype": "Currency",
            "indicator": "green",
        },
        {
            "value": total_written_off,
            "label": _("Total Written Off"),
            "datatype": "Currency",
            "indicator": "red",
        },
        {
            "value": overdue_olp,
            "label": _("OLP at Risk (Overdue / Arrears / Past Maturity)"),
            "datatype": "Currency",
            "indicator": "red",
        },
        {
            "value": closure_pending,
            "label": _("Closure Pending Approval"),
            "datatype": "Int",
            "indicator": "yellow",
        },
    ]
