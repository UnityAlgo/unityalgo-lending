# Copyright (c) 2026, Enigma Logics
# For license information, please see license.txt
#
# Frappe Script Report: CFS Lending — Advanced Recovery Report
#
# NOTE: Do NOT add `from frappe import _` here.
# Frappe's execute_script_report() uses exec() which shadows the module-level
# `_` binding, turning it into the data list and causing:
#   TypeError: 'list' object is not callable
# Use frappe._() directly everywhere instead.
#
# Tables used:
#   tabLoan                    — main loan record
#   tabLoan Interest Accrual   — interest/penalty accrued per period (LIA)
#   tabLoan Repayment          — actual repayment transactions
#   tabLoan Repayment Schedule — schedule header  (parent of tabRepayment Schedule)
#   tabRepayment Schedule      — per-installment rows
#   tabCustomer                — borrower name + CNIC
#
# LIA fields used:
#   lia.loan                    FK to tabLoan
#   lia.company / lia.branch
#   lia.posting_date            date LIA was created
#   lia.date_of_accrual         actual accrual date (preferred for period filter)
#   lia.accrual_type            'Repayment' | 'Penalty'
#   lia.interest_amount         interest accrued  (0 on Penalty LIAs)
#   lia.penalty_amount          penalty accrued   (0 on Repayment LIAs)
#   lia.payable_principal_amount
#   lia.paid_interest_amount    collected so far (updated by Loan Repayment submit)
#   lia.paid_principal_amount   collected so far
#   lia.docstatus               1 = submitted
#
# Report views:
#   Summary              portfolio KPIs + accrual target + recovery breakdown
#   Recovery Breakdown   one row per repayment, with LIA accrued columns
#   Advance Repayments   only repayments where paid > installment, with LIA columns
#   Month-wise Advance   monthly rollup of advances + LIA accrued
#   Accrual vs Collection one row per loan — accrued vs collected vs gap

import frappe
from frappe.utils import flt, getdate, cint
from collections import defaultdict


def execute(filters=None):
    filters = frappe._dict(filters or {})
    validate_filters(filters)

    columns = get_columns(filters)
    data = get_data(filters)
    chart = get_chart(filters, data)
    summary = get_summary(filters)

    return columns, data, None, chart, summary


# ── Validation ────────────────────────────────────────────────────────────────


def validate_filters(filters):
    if not filters.get("from_date") or not filters.get("to_date"):
        frappe.throw(frappe._("From Date and To Date are mandatory."))
    if getdate(filters.from_date) > getdate(filters.to_date):
        frappe.throw(frappe._("From Date cannot be after To Date."))
    if not filters.get("company"):
        frappe.throw(frappe._("Company is mandatory."))


# ── Columns ───────────────────────────────────────────────────────────────────


def get_columns(filters):
    report_type = filters.get("report_type") or "Summary"

    if report_type == "Recovery Breakdown":
        return [
            {
                "label": frappe._("Loan ID"),
                "fieldname": "loan",
                "fieldtype": "Link",
                "options": "Loan",
                "width": 140,
            },
            {
                "label": frappe._("Customer"),
                "fieldname": "customer_name",
                "fieldtype": "Data",
                "width": 160,
            },
            {
                "label": frappe._("CNIC"),
                "fieldname": "cnic",
                "fieldtype": "Data",
                "width": 130,
            },
            {
                "label": frappe._("Loan Product"),
                "fieldname": "loan_product",
                "fieldtype": "Link",
                "options": "Loan Product",
                "width": 140,
            },
            {
                "label": frappe._("Loan Amount (Rs)"),
                "fieldname": "loan_amount",
                "fieldtype": "Currency",
                "width": 130,
            },
            {
                "label": frappe._("Repayment Date"),
                "fieldname": "posting_date",
                "fieldtype": "Date",
                "width": 110,
            },
            {
                "label": frappe._("Amount Paid (Rs)"),
                "fieldname": "amount_paid",
                "fieldtype": "Currency",
                "width": 130,
            },
            {
                "label": frappe._("Principal Paid (Rs)"),
                "fieldname": "principal_paid",
                "fieldtype": "Currency",
                "width": 130,
            },
            {
                "label": frappe._("Interest Paid (Rs)"),
                "fieldname": "interest_paid",
                "fieldtype": "Currency",
                "width": 130,
            },
            {
                "label": frappe._("Penalty Paid (Rs)"),
                "fieldname": "penalty_paid",
                "fieldtype": "Currency",
                "width": 120,
            },
            {
                "label": frappe._("Interest Accrued (Rs)"),
                "fieldname": "interest_accrued",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Penalty Accrued (Rs)"),
                "fieldname": "penalty_accrued",
                "fieldtype": "Currency",
                "width": 140,
            },
            {
                "label": frappe._("Uncollected Int (Rs)"),
                "fieldname": "uncollected_interest",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Repayment Status"),
                "fieldname": "repayment_status",
                "fieldtype": "Data",
                "width": 130,
            },
        ]

    elif report_type == "Advance Repayments":
        return [
            {
                "label": frappe._("Loan ID"),
                "fieldname": "loan",
                "fieldtype": "Link",
                "options": "Loan",
                "width": 140,
            },
            {
                "label": frappe._("Customer"),
                "fieldname": "customer_name",
                "fieldtype": "Data",
                "width": 160,
            },
            {
                "label": frappe._("CNIC"),
                "fieldname": "cnic",
                "fieldtype": "Data",
                "width": 130,
            },
            {
                "label": frappe._("Loan Product"),
                "fieldname": "loan_product",
                "fieldtype": "Link",
                "options": "Loan Product",
                "width": 140,
            },
            {
                "label": frappe._("Repayment Date"),
                "fieldname": "posting_date",
                "fieldtype": "Date",
                "width": 110,
            },
            {
                "label": frappe._("Installment (Rs)"),
                "fieldname": "installment",
                "fieldtype": "Currency",
                "width": 120,
            },
            {
                "label": frappe._("Amount Paid (Rs)"),
                "fieldname": "amount_paid",
                "fieldtype": "Currency",
                "width": 130,
            },
            {
                "label": frappe._("Excess Paid (Rs)"),
                "fieldname": "excess_paid",
                "fieldtype": "Currency",
                "width": 130,
            },
            {
                "label": frappe._("Excess Ratio"),
                "fieldname": "excess_ratio",
                "fieldtype": "Float",
                "precision": 2,
                "width": 100,
            },
            {
                "label": frappe._("Principal Paid (Rs)"),
                "fieldname": "principal_paid",
                "fieldtype": "Currency",
                "width": 130,
            },
            {
                "label": frappe._("Interest Paid (Rs)"),
                "fieldname": "interest_paid",
                "fieldtype": "Currency",
                "width": 130,
            },
            {
                "label": frappe._("Penalty Paid (Rs)"),
                "fieldname": "penalty_paid",
                "fieldtype": "Currency",
                "width": 120,
            },
            {
                "label": frappe._("Interest Accrued (Rs)"),
                "fieldname": "interest_accrued",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Penalty Accrued (Rs)"),
                "fieldname": "penalty_accrued",
                "fieldtype": "Currency",
                "width": 140,
            },
        ]

    elif report_type == "Month-wise Advance":
        return [
            {
                "label": frappe._("Month"),
                "fieldname": "month_label",
                "fieldtype": "Data",
                "width": 110,
            },
            {
                "label": frappe._("Clients with Advance"),
                "fieldname": "client_count",
                "fieldtype": "Int",
                "width": 150,
            },
            {
                "label": frappe._("Total Advance (Rs)"),
                "fieldname": "total_advance",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Avg Excess/Client (Rs)"),
                "fieldname": "avg_excess",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Largest Advance (Rs)"),
                "fieldname": "largest_advance",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Total Repaid (Rs)"),
                "fieldname": "total_repaid",
                "fieldtype": "Currency",
                "width": 135,
            },
            {
                "label": frappe._("Interest Accrued (Rs)"),
                "fieldname": "interest_accrued",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Penalty Accrued (Rs)"),
                "fieldname": "penalty_accrued",
                "fieldtype": "Currency",
                "width": 140,
            },
            {
                "label": frappe._("Uncollected Int (Rs)"),
                "fieldname": "uncollected_interest",
                "fieldtype": "Currency",
                "width": 145,
            },
        ]

    elif report_type == "Accrual vs Collection":
        return [
            {
                "label": frappe._("Loan ID"),
                "fieldname": "loan",
                "fieldtype": "Link",
                "options": "Loan",
                "width": 140,
            },
            {
                "label": frappe._("Customer"),
                "fieldname": "customer_name",
                "fieldtype": "Data",
                "width": 160,
            },
            {
                "label": frappe._("CNIC"),
                "fieldname": "cnic",
                "fieldtype": "Data",
                "width": 130,
            },
            {
                "label": frappe._("Loan Product"),
                "fieldname": "loan_product",
                "fieldtype": "Link",
                "options": "Loan Product",
                "width": 140,
            },
            {
                "label": frappe._("Repayment Status"),
                "fieldname": "repayment_status",
                "fieldtype": "Data",
                "width": 130,
            },
            {
                "label": frappe._("Interest Accrued (Rs)"),
                "fieldname": "interest_accrued",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Interest Collected (Rs)"),
                "fieldname": "interest_collected",
                "fieldtype": "Currency",
                "width": 150,
            },
            {
                "label": frappe._("Uncollected Int (Rs)"),
                "fieldname": "uncollected_interest",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Penalty Accrued (Rs)"),
                "fieldname": "penalty_accrued",
                "fieldtype": "Currency",
                "width": 140,
            },
            {
                "label": frappe._("Penalty Collected (Rs)"),
                "fieldname": "penalty_collected",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Uncollected Penalty (Rs)"),
                "fieldname": "uncollected_penalty",
                "fieldtype": "Currency",
                "width": 150,
            },
            {
                "label": frappe._("Principal Accrued (Rs)"),
                "fieldname": "principal_accrued",
                "fieldtype": "Currency",
                "width": 145,
            },
            {
                "label": frappe._("Principal Collected (Rs)"),
                "fieldname": "principal_collected",
                "fieldtype": "Currency",
                "width": 150,
            },
            {
                "label": frappe._("Collection Efficiency %"),
                "fieldname": "collection_efficiency",
                "fieldtype": "Percent",
                "width": 150,
            },
        ]

    # Default: Summary
    return [
        {
            "label": frappe._("Metric"),
            "fieldname": "metric",
            "fieldtype": "Data",
            "width": 280,
        },
        {
            "label": frappe._("Value"),
            "fieldname": "value",
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "label": frappe._("Details"),
            "fieldname": "detail",
            "fieldtype": "Data",
            "width": 360,
        },
    ]


# ── Data routing ──────────────────────────────────────────────────────────────


def get_data(filters):
    report_type = filters.get("report_type") or "Summary"
    if report_type == "Recovery Breakdown":
        return get_recovery_breakdown(filters)
    elif report_type == "Advance Repayments":
        return get_advance_repayments(filters)
    elif report_type == "Month-wise Advance":
        return get_monthwise_advance(filters)
    elif report_type == "Accrual vs Collection":
        return get_accrual_vs_collection(filters)
    else:
        return get_summary_rows(filters)


# ── SQL helpers ───────────────────────────────────────────────────────────────


def _loan_conditions(filters, alias="l"):
    parts = [
        "{a}.company = %(company)s".format(a=alias),
        "{a}.docstatus = 1".format(a=alias),
    ]
    if filters.get("branch"):
        parts.append("{a}.branch = %(branch)s".format(a=alias))
    if filters.get("loan_product"):
        parts.append("{a}.loan_product = %(loan_product)s".format(a=alias))
    return " AND ".join(parts)


def _lia_date_expr(alias="lia"):
    """
    date_of_accrual is the authoritative accrual date.
    posting_date is the fallback for migration LIAs where date_of_accrual = posting_date anyway.
    """
    return "IFNULL({a}.date_of_accrual, {a}.posting_date)".format(a=alias)


# Correlated subquery — scheduled installment (principal + interest) for a
# repayment row r against loan l on the active repayment schedule.
_INSTALLMENT_SQ = """
    IFNULL((
        SELECT SUM(rs2.principal_amount + rs2.interest_amount)
        FROM `tabLoan Repayment Schedule` lrs2
        JOIN `tabRepayment Schedule` rs2 ON rs2.parent = lrs2.name
        WHERE lrs2.loan      = l.name
          AND lrs2.docstatus = 1
          AND lrs2.status    = 'Active'
          AND rs2.payment_date = (
              SELECT MIN(rs3.payment_date)
              FROM `tabLoan Repayment Schedule` lrs3
              JOIN `tabRepayment Schedule` rs3 ON rs3.parent = lrs3.name
              WHERE lrs3.loan      = l.name
                AND lrs3.docstatus = 1
                AND lrs3.status    = 'Active'
                AND rs3.payment_date <= DATE_FORMAT(r.posting_date, '%%Y-%%m-%%d')
          )
    ), 0)
"""


# Correlated subquery — interest accrued for loan l in period
def _lia_interest_sq(alias="lia_i"):
    return """
    IFNULL((
        SELECT SUM({a}.interest_amount)
        FROM `tabLoan Interest Accrual` {a}
        WHERE {a}.loan      = l.name
          AND {a}.docstatus = 1
          AND {a}.accrual_type != 'Penalty'
          AND IFNULL({a}.date_of_accrual, {a}.posting_date) BETWEEN %(from_date)s AND %(to_date)s
    ), 0)""".format(a=alias)


# Correlated subquery — penalty accrued for loan l in period
def _lia_penalty_sq(alias="lia_p"):
    return """
    IFNULL((
        SELECT SUM({a}.penalty_amount)
        FROM `tabLoan Interest Accrual` {a}
        WHERE {a}.loan      = l.name
          AND {a}.docstatus = 1
          AND {a}.accrual_type = 'Penalty'
          AND IFNULL({a}.date_of_accrual, {a}.posting_date) BETWEEN %(from_date)s AND %(to_date)s
    ), 0)""".format(a=alias)


# Correlated subquery — uncollected interest (accrued - paid) for loan l in period
def _lia_uncollected_sq(alias="lia_u"):
    return """
    IFNULL((
        SELECT SUM({a}.interest_amount) - SUM({a}.paid_interest_amount)
        FROM `tabLoan Interest Accrual` {a}
        WHERE {a}.loan      = l.name
          AND {a}.docstatus = 1
          AND {a}.accrual_type != 'Penalty'
          AND IFNULL({a}.date_of_accrual, {a}.posting_date) BETWEEN %(from_date)s AND %(to_date)s
    ), 0)""".format(a=alias)


# ── Aggregate stats ───────────────────────────────────────────────────────────


def _get_portfolio_stats(filters):
    cond = _loan_conditions(filters)

    active = frappe.db.sql(
        """
        SELECT COUNT(DISTINCT l.name) AS cnt
        FROM `tabLoan` l
        WHERE {cond}
          AND l.status NOT IN ('Closed', 'Written Off', 'Loan Closure Requested')
    """.format(cond=cond),
        filters,
        as_dict=True,
    )

    outstanding = frappe.db.sql(
        """
        SELECT SUM(
            IFNULL(l.total_payment, 0)
            + IFNULL(l.debit_adjustment_amount, 0)
            - IFNULL(l.credit_adjustment_amount, 0)
            - IFNULL(l.total_principal_paid, 0)
            - IFNULL(l.total_interest_payable, 0)
            - IFNULL(l.written_off_amount, 0)
            + IFNULL(l.refund_amount, 0)
        ) AS total
        FROM `tabLoan` l
        WHERE {cond}
          AND l.status NOT IN ('Closed', 'Written Off')
    """.format(cond=cond),
        filters,
        as_dict=True,
    )

    recovery = frappe.db.sql(
        """
        SELECT
            SUM(r.amount_paid)           AS total_recovered,
            SUM(r.principal_amount_paid) AS principal_recovered,
            SUM(r.total_interest_paid)   AS interest_recovered,
            SUM(r.total_penalty_paid)    AS penalty_recovered
        FROM `tabLoan Repayment` r
        JOIN `tabLoan` l ON l.name = r.against_loan
        WHERE {cond}
          AND r.docstatus = 1
          AND r.posting_date BETWEEN %(from_date)s AND %(to_date)s
    """.format(cond=cond),
        filters,
        as_dict=True,
    )

    r = recovery[0] if recovery else {}
    return {
        "active_clients": cint(active[0].cnt if active else 0),
        "total_outstanding": flt(outstanding[0].total if outstanding else 0),
        "total_recovered": flt(r.get("total_recovered")),
        "principal_recovered": flt(r.get("principal_recovered")),
        "interest_recovered": flt(r.get("interest_recovered")),
        "penalty_recovered": flt(r.get("penalty_recovered")),
    }


def _get_lia_stats(filters):
    """
    Aggregate LIA figures for the period from tabLoan Interest Accrual.
    Filtered by date_of_accrual (falls back to posting_date).

    interest_accrued     = SUM(interest_amount)        on Repayment LIAs
    penalty_accrued      = SUM(penalty_amount)         on Penalty LIAs
    principal_accrued    = SUM(payable_principal_amount) on Repayment LIAs
    interest_collected   = SUM(paid_interest_amount)   on Repayment LIAs
    principal_collected  = SUM(paid_principal_amount)  on Repayment LIAs
    penalty_collected    = SUM(paid_interest_amount)   on Penalty LIAs
                           (paid_interest_amount is repurposed on Penalty LIAs —
                            see migration BUG-F note in data_migration_tool.py)
    monthly_target       = interest_accrued + penalty_accrued
    uncollected_interest = interest_accrued - interest_collected
    collection_efficiency = (interest_collected / interest_accrued) * 100
    """
    cond = _loan_conditions(filters)
    date_exp = _lia_date_expr("lia")

    lia = frappe.db.sql(
        """
        SELECT
            SUM(CASE WHEN lia.accrual_type != 'Penalty'
                     THEN IFNULL(lia.interest_amount, 0)          ELSE 0 END) AS interest_accrued,
            SUM(CASE WHEN lia.accrual_type != 'Penalty'
                     THEN IFNULL(lia.payable_principal_amount, 0) ELSE 0 END) AS principal_accrued,
            SUM(CASE WHEN lia.accrual_type != 'Penalty'
                     THEN IFNULL(lia.paid_interest_amount, 0)     ELSE 0 END) AS interest_collected,
            SUM(CASE WHEN lia.accrual_type != 'Penalty'
                     THEN IFNULL(lia.paid_principal_amount, 0)    ELSE 0 END) AS principal_collected,
            SUM(CASE WHEN lia.accrual_type = 'Penalty'
                     THEN IFNULL(lia.penalty_amount, 0)           ELSE 0 END) AS penalty_accrued,
            SUM(CASE WHEN lia.accrual_type = 'Penalty'
                     THEN IFNULL(lia.paid_interest_amount, 0)     ELSE 0 END) AS penalty_collected
        FROM `tabLoan Interest Accrual` lia
        JOIN `tabLoan` l ON l.name = lia.loan
        WHERE lia.docstatus = 1
          AND {date_exp} BETWEEN %(from_date)s AND %(to_date)s
          AND {loan_cond}
    """.format(date_exp=date_exp, loan_cond=cond),
        filters,
        as_dict=True,
    )

    row = lia[0] if lia else {}
    int_accrued = flt(row.get("interest_accrued"))
    int_collected = flt(row.get("interest_collected"))
    pen_accrued = flt(row.get("penalty_accrued"))
    pen_collected = flt(row.get("penalty_collected"))

    return {
        "interest_accrued": int_accrued,
        "principal_accrued": flt(row.get("principal_accrued")),
        "interest_collected": int_collected,
        "principal_collected": flt(row.get("principal_collected")),
        "penalty_accrued": pen_accrued,
        "penalty_collected": pen_collected,
        "uncollected_interest": round(int_accrued - int_collected, 2),
        "uncollected_penalty": round(pen_accrued - pen_collected, 2),
        "monthly_target": round(int_accrued + pen_accrued, 2),
        "collection_efficiency": round(int_collected / int_accrued * 100, 1)
        if int_accrued
        else 0.0,
    }


def _get_advance_rows(filters):
    """
    Returns (total_advance_float, list_of_advance_dicts).
    Only repayments where amount_paid > scheduled installment.
    Includes LIA interest_accrued and penalty_accrued per loan for the period.
    """
    cond = _loan_conditions(filters)

    rows = frappe.db.sql(
        """
        SELECT
            r.name                  AS repayment,
            r.against_loan          AS loan,
            r.posting_date,
            r.amount_paid,
            r.principal_amount_paid AS principal_paid,
            r.total_interest_paid   AS interest_paid,
            r.total_penalty_paid    AS penalty_paid,
            {inst_sq}               AS scheduled_installment,
            {int_sq}                AS interest_accrued,
            {pen_sq}                AS penalty_accrued
        FROM `tabLoan Repayment` r
        JOIN `tabLoan` l ON l.name = r.against_loan
        WHERE {cond}
          AND r.docstatus = 1
          AND r.posting_date BETWEEN %(from_date)s AND %(to_date)s
    """.format(
            inst_sq=_INSTALLMENT_SQ,
            int_sq=_lia_interest_sq(),
            pen_sq=_lia_penalty_sq(),
            cond=cond,
        ),
        filters,
        as_dict=True,
    )

    total_advance = 0.0
    advance_rows = []
    for row in rows:
        inst = flt(row.scheduled_installment)
        paid = flt(row.amount_paid)
        excess = paid - inst
        if excess > 0:
            total_advance += excess
            row["installment"] = inst
            row["excess_paid"] = round(excess, 2)
            row["excess_ratio"] = round(paid / inst, 2) if inst > 0 else 0.0
            advance_rows.append(row)

    return round(total_advance, 2), advance_rows


# ── Report type: Summary ──────────────────────────────────────────────────────


def get_summary_rows(filters):
    stats = _get_portfolio_stats(filters)
    lia = _get_lia_stats(filters)
    advance_total, dummy = _get_advance_rows(filters)

    total = flt(stats["total_recovered"])

    def pct(part):
        return "{}%".format(round(flt(part) / total * 100, 1)) if total else "0%"

    def eff(collected, accrued):
        return (
            "{}%".format(round(flt(collected) / flt(accrued) * 100, 1))
            if flt(accrued)
            else "0%"
        )

    return [
        # Portfolio
        {"metric": "Portfolio overview", "value": None, "detail": None},
        {
            "metric": "Active clients",
            "value": stats["active_clients"],
            "detail": "Loans with status = Disbursed",
        },
        {
            "metric": "Total outstanding amount",
            "value": stats["total_outstanding"],
            "detail": "OLP: total_payment - principal_paid - interest_payable - written_off + refund",
        },
        {
            "metric": "Total advance amount issued",
            "value": advance_total,
            "detail": "Excess payments (paid > installment)  {} to {}".format(
                filters.from_date, filters.to_date
            ),
        },
        # Accrual target — from LIA
        {
            "metric": "Accrual target (from LIA)",
            "value": None,
            "detail": "Source: tabLoan Interest Accrual  {} to {}".format(
                filters.from_date, filters.to_date
            ),
        },
        {
            "metric": "Interest accrued",
            "value": lia["interest_accrued"],
            "detail": "SUM(interest_amount) — Repayment-type LIAs  →  this is the monthly interest target",
        },
        {
            "metric": "Penalty accrued",
            "value": lia["penalty_accrued"],
            "detail": "SUM(penalty_amount) — Penalty-type LIAs",
        },
        {
            "metric": "Monthly recovery target",
            "value": lia["monthly_target"],
            "detail": "Interest accrued + Penalty accrued = total receivable for the period",
        },
        {
            "metric": "Uncollected interest",
            "value": lia["uncollected_interest"],
            "detail": "interest_amount - paid_interest_amount on LIAs  (receivable gap / phantom credit risk)",
        },
        {
            "metric": "Uncollected penalty",
            "value": lia["uncollected_penalty"],
            "detail": "penalty_amount - paid_interest_amount on Penalty LIAs",
        },
        {
            "metric": "Collection efficiency",
            "value": lia["collection_efficiency"],
            "detail": "paid_interest_amount / interest_amount  x 100  (LIA-level tracking)",
        },
        # Actual recovery — from repayments
        {
            "metric": "Recovery for period",
            "value": None,
            "detail": "{} to {}".format(filters.from_date, filters.to_date),
        },
        {
            "metric": "Total amount recovered",
            "value": stats["total_recovered"],
            "detail": "All submitted Loan Repayments in date range",
        },
        {
            "metric": "  Principal recovered",
            "value": stats["principal_recovered"],
            "detail": "{} of total recovery".format(pct(stats["principal_recovered"])),
        },
        {
            "metric": "  Interest recovered",
            "value": stats["interest_recovered"],
            "detail": "{} of total  |  vs accrued: {}".format(
                pct(stats["interest_recovered"]),
                eff(stats["interest_recovered"], lia["interest_accrued"]),
            ),
        },
        {
            "metric": "  Penalty recovered",
            "value": stats["penalty_recovered"],
            "detail": "{} of total  |  vs accrued: {}".format(
                pct(stats["penalty_recovered"]),
                eff(stats["penalty_recovered"], lia["penalty_accrued"]),
            ),
        },
    ]


# ── Report type: Recovery Breakdown ──────────────────────────────────────────


def get_recovery_breakdown(filters):
    cond = _loan_conditions(filters)
    return frappe.db.sql(
        """
        SELECT
            l.name                  AS loan,
            c.customer_name,
            c.custom_cnic_no        AS cnic,
            l.loan_product,
            l.loan_amount,
            l.repayment_status,
            r.posting_date,
            r.amount_paid,
            r.principal_amount_paid AS principal_paid,
            r.total_interest_paid   AS interest_paid,
            r.total_penalty_paid    AS penalty_paid,
            {int_sq}                AS interest_accrued,
            {pen_sq}                AS penalty_accrued,
            {unc_sq}                AS uncollected_interest
        FROM `tabLoan Repayment` r
        JOIN `tabLoan` l     ON l.name = r.against_loan
        JOIN `tabCustomer` c ON c.name = l.applicant
        WHERE {cond}
          AND r.docstatus = 1
          AND r.posting_date BETWEEN %(from_date)s AND %(to_date)s
        ORDER BY r.posting_date DESC, l.name
    """.format(
            int_sq=_lia_interest_sq(),
            pen_sq=_lia_penalty_sq(),
            unc_sq=_lia_uncollected_sq(),
            cond=cond,
        ),
        filters,
        as_dict=True,
    )


# ── Report type: Advance Repayments ──────────────────────────────────────────


def get_advance_repayments(filters):
    dummy, advance_rows = _get_advance_rows(filters)
    if not advance_rows:
        return []

    loan_names = list({r["loan"] for r in advance_rows})
    placeholders = ", ".join(["%s"] * len(loan_names))

    loan_info = {
        row.name: row
        for row in frappe.db.sql(
            """
            SELECT l.name, l.loan_product, c.customer_name, c.custom_cnic_no AS cnic
            FROM `tabLoan` l
            JOIN `tabCustomer` c ON c.name = l.applicant
            WHERE l.name IN ({ph})
        """.format(ph=placeholders),
            tuple(loan_names),
            as_dict=True,
        )
    }

    result = []
    for row in advance_rows:
        info = loan_info.get(row["loan"]) or frappe._dict()
        result.append(
            {
                "loan": row["loan"],
                "customer_name": info.get("customer_name", ""),
                "cnic": info.get("cnic", ""),
                "loan_product": info.get("loan_product", ""),
                "posting_date": row["posting_date"],
                "installment": flt(row.get("installment")),
                "amount_paid": flt(row["amount_paid"]),
                "excess_paid": flt(row["excess_paid"]),
                "excess_ratio": flt(row.get("excess_ratio")),
                "principal_paid": flt(row["principal_paid"]),
                "interest_paid": flt(row["interest_paid"]),
                "penalty_paid": flt(row["penalty_paid"]),
                "interest_accrued": flt(row.get("interest_accrued")),
                "penalty_accrued": flt(row.get("penalty_accrued")),
            }
        )

    result.sort(key=lambda x: x["excess_paid"], reverse=True)
    return result


# ── Report type: Month-wise Advance ──────────────────────────────────────────


def get_monthwise_advance(filters):
    cond = _loan_conditions(filters)

    rows = frappe.db.sql(
        """
        SELECT
            DATE_FORMAT(r.posting_date, '%%Y-%%m') AS month_key,
            DATE_FORMAT(r.posting_date, '%%b %%Y')  AS month_label,
            r.against_loan                          AS loan,
            r.amount_paid,
            {inst_sq}                               AS scheduled_installment,
            IFNULL((
                SELECT SUM(lia_i.interest_amount)
                FROM `tabLoan Interest Accrual` lia_i
                WHERE lia_i.loan        = l.name
                  AND lia_i.docstatus   = 1
                  AND lia_i.accrual_type != 'Penalty'
                  AND DATE_FORMAT(IFNULL(lia_i.date_of_accrual, lia_i.posting_date), '%%Y-%%m')
                      = DATE_FORMAT(r.posting_date, '%%Y-%%m')
            ), 0) AS interest_accrued,
            IFNULL((
                SELECT SUM(lia_p.penalty_amount)
                FROM `tabLoan Interest Accrual` lia_p
                WHERE lia_p.loan        = l.name
                  AND lia_p.docstatus   = 1
                  AND lia_p.accrual_type = 'Penalty'
                  AND DATE_FORMAT(IFNULL(lia_p.date_of_accrual, lia_p.posting_date), '%%Y-%%m')
                      = DATE_FORMAT(r.posting_date, '%%Y-%%m')
            ), 0) AS penalty_accrued,
            IFNULL((
                SELECT SUM(lia_u.interest_amount) - SUM(lia_u.paid_interest_amount)
                FROM `tabLoan Interest Accrual` lia_u
                WHERE lia_u.loan        = l.name
                  AND lia_u.docstatus   = 1
                  AND lia_u.accrual_type != 'Penalty'
                  AND DATE_FORMAT(IFNULL(lia_u.date_of_accrual, lia_u.posting_date), '%%Y-%%m')
                      = DATE_FORMAT(r.posting_date, '%%Y-%%m')
            ), 0) AS uncollected_interest
        FROM `tabLoan Repayment` r
        JOIN `tabLoan` l ON l.name = r.against_loan
        WHERE {cond}
          AND r.docstatus = 1
          AND r.posting_date BETWEEN %(from_date)s AND %(to_date)s
        ORDER BY month_key ASC
    """.format(inst_sq=_INSTALLMENT_SQ, cond=cond),
        filters,
        as_dict=True,
    )

    months = defaultdict(
        lambda: {
            "month_label": "",
            "client_loans": set(),
            "total_advance": 0.0,
            "total_repaid": 0.0,
            "largest_advance": 0.0,
            "interest_accrued": 0.0,
            "penalty_accrued": 0.0,
            "uncollected_interest": 0.0,
        }
    )

    for row in rows:
        mk = row.month_key
        inst = flt(row.scheduled_installment)
        paid = flt(row.amount_paid)
        excess = paid - inst if inst > 0 else 0.0
        months[mk]["month_label"] = row.month_label
        months[mk]["total_repaid"] += paid
        months[mk]["interest_accrued"] += flt(row.interest_accrued)
        months[mk]["penalty_accrued"] += flt(row.penalty_accrued)
        months[mk]["uncollected_interest"] += flt(row.uncollected_interest)
        if excess > 0:
            months[mk]["client_loans"].add(row.loan)
            months[mk]["total_advance"] += excess
            if excess > months[mk]["largest_advance"]:
                months[mk]["largest_advance"] = excess

    result = []
    for mk in sorted(months.keys()):
        m = months[mk]
        count = len(m["client_loans"])
        result.append(
            {
                "month_label": m["month_label"],
                "client_count": count,
                "total_advance": round(m["total_advance"], 2),
                "avg_excess": round(m["total_advance"] / count, 2) if count else 0.0,
                "largest_advance": round(m["largest_advance"], 2),
                "total_repaid": round(m["total_repaid"], 2),
                "interest_accrued": round(m["interest_accrued"], 2),
                "penalty_accrued": round(m["penalty_accrued"], 2),
                "uncollected_interest": round(m["uncollected_interest"], 2),
            }
        )
    return result


# ── Report type: Accrual vs Collection ───────────────────────────────────────


def get_accrual_vs_collection(filters):
    """
    One row per loan — pure LIA view.
    accrual_type = 'Repayment'  → interest_amount, payable_principal_amount,
                                   paid_interest_amount, paid_principal_amount
    accrual_type = 'Penalty'    → penalty_amount,
                                   paid_interest_amount (repurposed — see migration notes)

    collection_efficiency = paid_interest_amount / interest_amount * 100
    Shows per-loan gap between what was accrued (billed) and what was collected.
    """
    cond = _loan_conditions(filters)
    date_exp = _lia_date_expr("lia")
    lp_extra = (
        "AND l.loan_product = %(loan_product)s" if filters.get("loan_product") else ""
    )

    rows = frappe.db.sql(
        """
        SELECT
            l.name                                                              AS loan,
            c.customer_name,
            c.custom_cnic_no                                                    AS cnic,
            l.loan_product,
            l.repayment_status,
            SUM(CASE WHEN lia.accrual_type != 'Penalty'
                     THEN IFNULL(lia.interest_amount, 0)          ELSE 0 END)  AS interest_accrued,
            SUM(CASE WHEN lia.accrual_type != 'Penalty'
                     THEN IFNULL(lia.paid_interest_amount, 0)     ELSE 0 END)  AS interest_collected,
            SUM(CASE WHEN lia.accrual_type != 'Penalty'
                     THEN IFNULL(lia.payable_principal_amount, 0) ELSE 0 END)  AS principal_accrued,
            SUM(CASE WHEN lia.accrual_type != 'Penalty'
                     THEN IFNULL(lia.paid_principal_amount, 0)    ELSE 0 END)  AS principal_collected,
            SUM(CASE WHEN lia.accrual_type = 'Penalty'
                     THEN IFNULL(lia.penalty_amount, 0)           ELSE 0 END)  AS penalty_accrued,
            SUM(CASE WHEN lia.accrual_type = 'Penalty'
                     THEN IFNULL(lia.paid_interest_amount, 0)     ELSE 0 END)  AS penalty_collected
        FROM `tabLoan Interest Accrual` lia
        JOIN `tabLoan` l     ON l.name = lia.loan
        JOIN `tabCustomer` c ON c.name = l.applicant
        WHERE lia.docstatus = 1
          AND {date_exp} BETWEEN %(from_date)s AND %(to_date)s
          AND {loan_cond}
          {lp_extra}
        GROUP BY
            l.name, c.customer_name, c.custom_cnic_no,
            l.loan_product, l.repayment_status
        ORDER BY interest_accrued DESC
    """.format(date_exp=date_exp, loan_cond=cond, lp_extra=lp_extra),
        filters,
        as_dict=True,
    )

    result = []
    for row in rows:
        int_acc = flt(row.interest_accrued)
        int_col = flt(row.interest_collected)
        pen_acc = flt(row.penalty_accrued)
        pen_col = flt(row.penalty_collected)
        result.append(
            {
                "loan": row.loan,
                "customer_name": row.customer_name,
                "cnic": row.cnic,
                "loan_product": row.loan_product,
                "repayment_status": row.repayment_status,
                "interest_accrued": int_acc,
                "interest_collected": int_col,
                "uncollected_interest": round(int_acc - int_col, 2),
                "penalty_accrued": pen_acc,
                "penalty_collected": pen_col,
                "uncollected_penalty": round(pen_acc - pen_col, 2),
                "principal_accrued": flt(row.principal_accrued),
                "principal_collected": flt(row.principal_collected),
                "collection_efficiency": round(int_col / int_acc * 100, 1)
                if int_acc
                else 0.0,
            }
        )
    return result


# ── Chart ─────────────────────────────────────────────────────────────────────


def get_chart(filters, data):
    report_type = filters.get("report_type") or "Summary"

    if report_type == "Summary":
        stats = _get_portfolio_stats(filters)
        lia = _get_lia_stats(filters)
        return {
            "data": {
                "labels": ["Interest", "Penalty", "Principal"],
                "datasets": [
                    {
                        "name": "Accrued",
                        "values": [
                            round(lia["interest_accrued"], 2),
                            round(lia["penalty_accrued"], 2),
                            round(lia["principal_accrued"], 2),
                        ],
                    },
                    {
                        "name": "Collected",
                        "values": [
                            round(stats["interest_recovered"], 2),
                            round(stats["penalty_recovered"], 2),
                            round(stats["principal_recovered"], 2),
                        ],
                    },
                ],
            },
            "type": "bar",
            "height": 280,
            "colors": ["#B5D4F4", "#1D9E75"],
            "barOptions": {"stacked": False},
        }

    elif report_type == "Recovery Breakdown":
        daily = {}
        for row in data:
            d = str(row.get("posting_date") or "")[:10]
            if d:
                daily[d] = daily.get(d, 0) + flt(row.get("amount_paid"))
        dates = sorted(daily.keys())
        return {
            "data": {
                "labels": dates,
                "datasets": [
                    {
                        "name": "Amount Recovered",
                        "values": [round(daily[d], 2) for d in dates],
                    }
                ],
            },
            "type": "bar",
            "height": 280,
            "colors": ["#1D9E75"],
        }

    elif report_type == "Advance Repayments":
        buckets = {"1x-1.5x": 0, "1.5x-2x": 0, "2x-3x": 0, "3x+": 0}
        for row in data:
            r = flt(row.get("excess_ratio"))
            if r < 1.5:
                buckets["1x-1.5x"] += 1
            elif r < 2:
                buckets["1.5x-2x"] += 1
            elif r < 3:
                buckets["2x-3x"] += 1
            else:
                buckets["3x+"] += 1
        return {
            "data": {
                "labels": list(buckets.keys()),
                "datasets": [{"name": "Clients", "values": list(buckets.values())}],
            },
            "type": "bar",
            "height": 260,
            "colors": ["#378ADD"],
        }

    elif report_type == "Month-wise Advance":
        return {
            "data": {
                "labels": [r["month_label"] for r in data],
                "datasets": [
                    {
                        "name": "Advance Amount",
                        "values": [r["total_advance"] for r in data],
                        "chartType": "bar",
                    },
                    {
                        "name": "Interest Accrued",
                        "values": [r["interest_accrued"] for r in data],
                        "chartType": "bar",
                    },
                    {
                        "name": "Clients",
                        "values": [r["client_count"] for r in data],
                        "chartType": "line",
                    },
                ],
            },
            "type": "axis-mixed",
            "height": 300,
            "colors": ["#378ADD", "#B5D4F4", "#1D9E75"],
            "axisOptions": {"xIsSeries": True},
        }

    elif report_type == "Accrual vs Collection":
        top = sorted(data, key=lambda x: x["uncollected_interest"], reverse=True)[:10]
        return {
            "data": {
                "labels": [r["loan"] for r in top],
                "datasets": [
                    {
                        "name": "Interest Accrued",
                        "values": [r["interest_accrued"] for r in top],
                    },
                    {
                        "name": "Interest Collected",
                        "values": [r["interest_collected"] for r in top],
                    },
                ],
            },
            "type": "bar",
            "height": 300,
            "colors": ["#B5D4F4", "#1D9E75"],
        }

    return None


# ── Summary bar ───────────────────────────────────────────────────────────────


def get_summary(filters):
    stats = _get_portfolio_stats(filters)
    lia = _get_lia_stats(filters)
    advance_total, dummy = _get_advance_rows(filters)
    currency = frappe.defaults.get_global_default("currency") or "PKR"

    return [
        {
            "label": frappe._("Active Clients"),
            "value": stats["active_clients"],
            "datatype": "Int",
            "indicator": "blue",
        },
        {
            "label": frappe._("Monthly Target"),
            "value": lia["monthly_target"],
            "datatype": "Currency",
            "currency": currency,
            "indicator": "blue",
        },
        {
            "label": frappe._("Total Recovered"),
            "value": stats["total_recovered"],
            "datatype": "Currency",
            "currency": currency,
            "indicator": "green",
        },
        {
            "label": frappe._("Uncollected Interest"),
            "value": lia["uncollected_interest"],
            "datatype": "Currency",
            "currency": currency,
            "indicator": "orange",
        },
        {
            "label": frappe._("Collection Efficiency"),
            "value": lia["collection_efficiency"],
            "datatype": "Percent",
            "indicator": "green" if lia["collection_efficiency"] >= 80 else "orange",
        },
        {
            "label": frappe._("Advance Issued"),
            "value": advance_total,
            "datatype": "Currency",
            "currency": currency,
            "indicator": "blue",
        },
    ]
