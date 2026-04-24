# Copyright (c) 2026, UnityAlgo
# For license information, please see license.txt
#
# Performance notes
# ─────────────────
# Previous version ran 3 correlated subqueries PER ROW which means
# 10 000 loans → ~30 000 extra round-trips inside MySQL.
#
# This version replaces every correlated subquery with a pre-aggregated
# derived table (one scan each) that is then LEFT JOINed to the main
# query.  MySQL executes each derived table exactly once regardless of
# how many loans are returned.
#
# Recommended indexes (run once on your site):
#   ALTER TABLE `tabLoan Repayment`
#       ADD INDEX IF NOT EXISTS idx_lr_against_loan_docstatus (against_loan, docstatus, posting_date);
#
#   ALTER TABLE `tabLoan Repayment Schedule`
#       ADD INDEX IF NOT EXISTS idx_lrs_loan_status_docstatus (loan, status, docstatus);
#
#   ALTER TABLE `tabRepayment Schedule`
#       ADD INDEX IF NOT EXISTS idx_rs_parent_payment_date (parent, payment_date);

import frappe
from frappe.utils import date_diff


def execute(filters=None):
    if filters is None:
        filters = {}

    columns = get_columns()
    data    = get_data(filters)
    return columns, data


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def get_data(filters=None):
    if filters is None:
        filters = {}

    # ── Static WHERE conditions (applied on tabLoan columns directly) ────────
    loan_conditions = [
        "l.docstatus = 1",
        "l.status IN ('Closed', 'Loan Closure Requested')",
    ]

    if filters.get("company"):
        loan_conditions.append("l.company = %(company)s")

    if filters.get("branch"):
        loan_conditions.append("l.branch = %(branch)s")

    if filters.get("loan_product"):
        loan_conditions.append("l.loan_product = %(loan_product)s")

    if filters.get("sales_person"):
        loan_conditions.append("l.sales_person = %(sales_person)s")

    # ── Date filters: applied on the pre-aggregated alias (HAVING / outer WHERE)
    # We wrap the whole query in an outer SELECT so we can filter on the alias.
    date_having = []

    if filters.get("from_date"):
        date_having.append("closure_date >= %(from_date)s")

    if filters.get("to_date"):
        date_having.append("closure_date <= %(to_date)s")

    where_clause   = " AND ".join(loan_conditions)
    having_clause  = ("WHERE " + " AND ".join(date_having)) if date_having else ""

    sql = f"""
        SELECT *
        FROM (
            SELECT
                l.name                  AS loan,
                l.applicant             AS customer,
                l.applicant_name        AS customer_name,
                l.branch                AS branch,
                l.company               AS company,
                l.loan_product          AS loan_product,
                l.loan_amount           AS loan_amount,
                l.disbursed_amount      AS disbursed_amount,
                l.rate_of_interest      AS rate_of_interest,
                l.repayment_periods     AS repayment_periods,
                l.disbursement_date     AS disbursement_date,
                l.repayment_start_date  AS repayment_start_date,
                l.status                AS status,
                l.repayment_status      AS repayment_status,
                l.total_amount_paid     AS total_amount_paid,
                l.total_principal_paid  AS total_principal_paid,
                l.total_interest_paid   AS total_interest_paid,
                l.total_penalty_paid    AS total_penalty_paid,
                l.sales_person          AS sales_person,
                l.overdue_days          AS overdue_days,

                -- ── Closure date: last repayment posting date ──────────────
                -- Pre-aggregated via LEFT JOIN (one scan, not N scans)
                lr_agg.closure_date     AS closure_date,

                -- ── Maturity date: last scheduled payment date ────────────
                -- Pre-aggregated via LEFT JOIN (one scan, not N scans)
                rs_agg.maturity_date    AS maturity_date

            FROM `tabLoan` l

            -- ── Derived table 1: max repayment posting date per loan ───────
            LEFT JOIN (
                SELECT
                    lr.against_loan,
                    MAX(DATE(lr.posting_date)) AS closure_date
                FROM `tabLoan Repayment` lr
                WHERE lr.docstatus = 1
                GROUP BY lr.against_loan
            ) AS lr_agg ON lr_agg.against_loan = l.name

            -- ── Derived table 2: max scheduled payment date per loan ───────
            LEFT JOIN (
                SELECT
                    lrs.loan,
                    MAX(rs.payment_date) AS maturity_date
                FROM `tabLoan Repayment Schedule` lrs
                INNER JOIN `tabRepayment Schedule` rs ON rs.parent = lrs.name
                WHERE lrs.docstatus != 2
                  AND lrs.status = 'Active'
                GROUP BY lrs.loan
            ) AS rs_agg ON rs_agg.loan = l.name

            WHERE {where_clause}

        ) AS base
        {having_clause}
        ORDER BY closure_date DESC
    """

    loans = frappe.db.sql(sql, filters, as_dict=True)

    # ── Python-side calculation (no extra DB hit) ────────────────────────────
    # Positive → paid BEFORE maturity (early closure)
    # Negative → paid AFTER  maturity (late  closure)
    for row in loans:
        maturity_date = row.get("maturity_date")
        closure_date  = row.get("closure_date")

        if maturity_date and closure_date:
            row["past_maturity_days"] = date_diff(maturity_date, closure_date)
        else:
            row["past_maturity_days"] = None

    return loans


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

def get_columns():
    return [
        # ── Identifiers ──────────────────────────────────────────────────────
        {
            "label":     "Loan #",
            "fieldname": "loan",
            "fieldtype": "Link",
            "options":   "Loan",
            "width":     160,
        },
        {
            "label":     "Customer",
            "fieldname": "customer",
            "fieldtype": "Link",
            "options":   "Customer",
            "width":     130,
        },
        {
            "label":     "Customer Name",
            "fieldname": "customer_name",
            "fieldtype": "Data",
            "width":     180,
        },
        # ── Loan Info ────────────────────────────────────────────────────────
        {
            "label":     "Branch",
            "fieldname": "branch",
            "fieldtype": "Link",
            "options":   "Branch",
            "width":     120,
        },
        {
            "label":     "Company",
            "fieldname": "company",
            "fieldtype": "Link",
            "options":   "Company",
            "width":     130,
        },
        {
            "label":     "Loan Product",
            "fieldname": "loan_product",
            "fieldtype": "Link",
            "options":   "Loan Product",
            "width":     140,
        },
        {
            "label":     "Sales Person",
            "fieldname": "sales_person",
            "fieldtype": "Link",
            "options":   "Sales Person",
            "width":     130,
        },
        # ── Amounts ──────────────────────────────────────────────────────────
        {
            "label":     "Loan Amount",
            "fieldname": "loan_amount",
            "fieldtype": "Currency",
            "width":     130,
        },
        {
            "label":     "Disbursed Amount",
            "fieldname": "disbursed_amount",
            "fieldtype": "Currency",
            "width":     140,
        },
        {
            "label":     "Rate of Interest (%)",
            "fieldname": "rate_of_interest",
            "fieldtype": "Percent",
            "width":     130,
        },
        {
            "label":     "Repayment Periods (Months)",
            "fieldname": "repayment_periods",
            "fieldtype": "Int",
            "width":     140,
        },
        # ── Paid Totals ───────────────────────────────────────────────────────
        {
            "label":     "Total Amount Paid",
            "fieldname": "total_amount_paid",
            "fieldtype": "Currency",
            "width":     140,
        },
        {
            "label":     "Total Principal Paid",
            "fieldname": "total_principal_paid",
            "fieldtype": "Currency",
            "width":     145,
        },
        {
            "label":     "Total Interest Paid",
            "fieldname": "total_interest_paid",
            "fieldtype": "Currency",
            "width":     135,
        },
        {
            "label":     "Total Penalty Paid",
            "fieldname": "total_penalty_paid",
            "fieldtype": "Currency",
            "width":     130,
        },
        # ── Dates ────────────────────────────────────────────────────────────
        {
            "label":     "Disbursement Date",
            "fieldname": "disbursement_date",
            "fieldtype": "Date",
            "width":     130,
        },
        {
            "label":     "Repayment Start Date",
            "fieldname": "repayment_start_date",
            "fieldtype": "Date",
            "width":     140,
        },
        {
            "label":     "Past Maturity Date",
            "fieldname": "maturity_date",
            "fieldtype": "Date",
            "width":     130,
        },
        {
            "label":     "Closure Date",
            "fieldname": "closure_date",
            "fieldtype": "Date",
            "width":     120,
        },
        # ── Key Derived Column ────────────────────────────────────────────────
        {
            "label":     "Past Maturity Days",
            "fieldname": "past_maturity_days",
            "fieldtype": "Int",
            # Positive = early (good), Negative = late (overdue)
            "width":     140,
        },
        # ── Status ────────────────────────────────────────────────────────────
        {
            "label":     "Status",
            "fieldname": "status",
            "fieldtype": "Data",
            "width":     160,
        },
        {
            "label":     "Repayment Status",
            "fieldname": "repayment_status",
            "fieldtype": "Data",
            "width":     140,
        },
        {
            "label":     "Overdue Days",
            "fieldname": "overdue_days",
            "fieldtype": "Int",
            "width":     110,
        },
    ]