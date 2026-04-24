# Copyright (c) 2026, UnityAlgo
# For license information, please see license.txt

import frappe
from frappe.utils import date_diff


def execute(filters=None):
    if filters is None:
        filters = {}

    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_data(filters=None):
    if filters is None:
        filters = {}

    conditions = []

    if filters.get("company"):
        conditions.append("l.company = %(company)s")

    if filters.get("branch"):
        conditions.append("l.branch = %(branch)s")

    if filters.get("loan_product"):
        conditions.append("l.loan_product = %(loan_product)s")

    if filters.get("sales_person"):
        conditions.append("l.sales_person = %(sales_person)s")

    # closure_date is a subquery alias — it cannot be used in WHERE directly.
    # We repeat the subquery inside the WHERE clause for date range filtering.
    if filters.get("from_date"):
        conditions.append(
            """(
                SELECT MAX(DATE(lr.posting_date))
                FROM `tabLoan Repayment` lr
                WHERE lr.against_loan = l.name
                  AND lr.docstatus = 1
            ) >= %(from_date)s"""
        )

    if filters.get("to_date"):
        conditions.append(
            """(
                SELECT MAX(DATE(lr.posting_date))
                FROM `tabLoan Repayment` lr
                WHERE lr.against_loan = l.name
                  AND lr.docstatus = 1
            ) <= %(to_date)s"""
        )

    sql_conditions = ("AND " + " AND ".join(conditions)) if conditions else ""

    loans = frappe.db.sql(
        f"""
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

            (
                SELECT MAX(DATE(lr.posting_date))
                FROM `tabLoan Repayment` lr
                WHERE lr.against_loan = l.name
                  AND lr.docstatus = 1
            ) AS closure_date,

            (
                SELECT MAX(rs.payment_date)
                FROM `tabLoan Repayment Schedule` lrs
                INNER JOIN `tabRepayment Schedule` rs ON rs.parent = lrs.name
                WHERE lrs.loan = l.name
                  AND lrs.docstatus != 2
                  AND lrs.status = 'Active'
            ) AS maturity_date

        FROM `tabLoan` l
        WHERE
            l.docstatus = 1
            AND l.status IN ('Closed', 'Loan Closure Requested')
            {sql_conditions}
        ORDER BY
            closure_date DESC
        """,
        filters,
        as_dict=True,
    )

    # Compute past_maturity_days for each row
    # Positive → paid BEFORE maturity (early closure)
    # Negative → paid AFTER  maturity (late  closure)
    for row in loans:
        maturity_date = row.get("maturity_date")
        closure_date = row.get("closure_date")

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
            "label": "Loan #",
            "fieldname": "loan",
            "fieldtype": "Link",
            "options": "Loan",
            "width": 160,
        },
        {
            "label": "Customer",
            "fieldname": "customer",
            "fieldtype": "Link",
            "options": "Customer",
            "width": 130,
        },
        {
            "label": "Customer Name",
            "fieldname": "customer_name",
            "fieldtype": "Data",
            "width": 180,
        },
        # ── Loan Info ────────────────────────────────────────────────────────
        {
            "label": "Branch",
            "fieldname": "branch",
            "fieldtype": "Link",
            "options": "Branch",
            "width": 120,
        },
        {
            "label": "Company",
            "fieldname": "company",
            "fieldtype": "Link",
            "options": "Company",
            "width": 130,
        },
        {
            "label": "Loan Product",
            "fieldname": "loan_product",
            "fieldtype": "Link",
            "options": "Loan Product",
            "width": 140,
        },
        {
            "label": "Sales Person",
            "fieldname": "sales_person",
            "fieldtype": "Link",
            "options": "Sales Person",
            "width": 130,
        },
        # ── Amounts ──────────────────────────────────────────────────────────
        {
            "label": "Loan Amount",
            "fieldname": "loan_amount",
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "label": "Disbursed Amount",
            "fieldname": "disbursed_amount",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": "Rate of Interest (%)",
            "fieldname": "rate_of_interest",
            "fieldtype": "Percent",
            "width": 130,
        },
        {
            "label": "Repayment Periods (Months)",
            "fieldname": "repayment_periods",
            "fieldtype": "Int",
            "width": 140,
        },
        # ── Paid Totals ───────────────────────────────────────────────────────
        {
            "label": "Total Amount Paid",
            "fieldname": "total_amount_paid",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": "Total Principal Paid",
            "fieldname": "total_principal_paid",
            "fieldtype": "Currency",
            "width": 145,
        },
        {
            "label": "Total Interest Paid",
            "fieldname": "total_interest_paid",
            "fieldtype": "Currency",
            "width": 135,
        },
        {
            "label": "Total Penalty Paid",
            "fieldname": "total_penalty_paid",
            "fieldtype": "Currency",
            "width": 130,
        },
        # ── Dates ────────────────────────────────────────────────────────────
        {
            "label": "Disbursement Date",
            "fieldname": "disbursement_date",
            "fieldtype": "Date",
            "width": 130,
        },
        {
            "label": "Repayment Start Date",
            "fieldname": "repayment_start_date",
            "fieldtype": "Date",
            "width": 140,
        },
        {
            "label": "Past Maturity Date",
            "fieldname": "maturity_date",
            "fieldtype": "Date",
            "width": 130,
        },
        {
            "label": "Closure Date",
            "fieldname": "closure_date",
            "fieldtype": "Date",
            "width": 120,
        },
        {
            "label": "Past Maturity Days",
            "fieldname": "past_maturity_days",
            "fieldtype": "Int",
            # Positive = early (good), Negative = late (overdue)
            "width": 140,
        },
        {
            "label": "Status",
            "fieldname": "status",
            "fieldtype": "Data",
            "width": 160,
        },
        {
            "label": "Repayment Status",
            "fieldname": "repayment_status",
            "fieldtype": "Data",
            "width": 140,
        },
        {
            "label": "Overdue Days",
            "fieldname": "overdue_days",
            "fieldtype": "Int",
            "width": 110,
        },
    ]
