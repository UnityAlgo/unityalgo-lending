# Copyright (c) 2024, Your Organization
# Loan Overdue Report
#
# Aligned to customized lending.loan_management.doctype.loan_repayment.loan_repayment
#
# Overdue detection logic mirrors update_loan_repayment_status() in loan_repayment.py:
#   - Overdue        : 1  – 29 days past due (single installment, no partial payment)
#   - Missed Repayment: 30+ days past due (single installment missed)
#   - Arrears        : 2+ unpaid installments OR any partial payment exists
#   - Past Maturity  : current date > final maturity date of repayment schedule
#
# Overdue interest/principal are derived from Loan Interest Accrual (LIA) records:
#   overdue = SUM(interest_amount - paid_interest_amount)  WHERE due_date < as_of_date
#   overdue_principal = SUM(payable_principal_amount - paid_principal_amount) WHERE due_date < as_of_date
#
# Penalty is fetched from Loan Interest Accrual WHERE accrual_type = 'Penalty'.
#
# Aging buckets are computed from l.days_past_due (maintained by set_repayment_status
# and on_cancel via frappe.db.set_value("Loan", ..., "days_past_due", ...)).
#
# Only loans with docstatus=1 and repayment_status NOT IN ('Regular', '') are shown.

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, nowdate


def execute(filters=None):
    filters = frappe._dict(filters or {})
    if not filters.as_of_date:
        filters.as_of_date = nowdate()

    columns = get_columns()
    data = get_data(filters)
    summary = get_summary(data)

    return columns, data, None, None, summary


# ─────────────────────────────────────────────────────────────────────────────
# COLUMNS
# ─────────────────────────────────────────────────────────────────────────────
def get_columns():
    return [
        # ── Identifiers ──
        {
            "label": _("Branch"),
            "fieldname": "branch",
            "fieldtype": "Link",
            "options": "Branch",
            "width": 130,
        },
        {
            "label": _("Loan Officer"),
            "fieldname": "loan_officer",
            "fieldtype": "Link",
            "options": "User",
            "width": 150,
        },
        {
            "label": _("Loan Product"),
            "fieldname": "loan_product",
            "fieldtype": "Link",
            "options": "Loan Product",
            "width": 140,
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
        # ── Repayment health ──
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
            "label": _("Aging Bucket"),
            "fieldname": "aging_bucket",
            "fieldtype": "Data",
            "width": 130,
        },
        # ── Overdue amounts ──
        {
            "label": _("Outstanding Principal (OLP)"),
            "fieldname": "outstanding_principal",
            "fieldtype": "Currency",
            "width": 190,
        },
        {
            "label": _("Overdue Principal"),
            "fieldname": "overdue_principal",
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "label": _("Overdue Interest"),
            "fieldname": "overdue_interest",
            "fieldtype": "Currency",
            "width": 150,
        },
        {
            "label": _("Overdue Penalty"),
            "fieldname": "overdue_penalty",
            "fieldtype": "Currency",
            "width": 150,
        },
        {
            "label": _("Total Overdue Amount"),
            "fieldname": "total_overdue",
            "fieldtype": "Currency",
            "width": 170,
        },
        # ── Risk flags ──
        {
            "label": _("NPA"),
            "fieldname": "is_npa",
            "fieldtype": "Check",
            "width": 70,
        },
        {
            "label": _("Loan Status"),
            "fieldname": "status",
            "fieldtype": "Data",
            "width": 160,
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────────────────────
def get_data(filters):
    loans = fetch_overdue_loans(filters)
    enrich_overdue_amounts(loans, filters.as_of_date)
    assign_aging_buckets(loans)
    return build_grouped_rows(loans)


# ── Step 1: fetch all non-regular loans ──────────────────────────────────────
def fetch_overdue_loans(filters):
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    conditions = get_conditions(filters)

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

            -- OLP mirrors get_pending_principal_amount() from loan_repayment.py
            ROUND(
                CASE
                    WHEN l.status IN ('Disbursed', 'Closed')
                         OR IFNULL(l.disbursed_amount, 0) >= IFNULL(l.loan_amount, 0)
                    THEN (
                          IFNULL(l.total_payment, 0)
                        + IFNULL(l.debit_adjustment_amount, 0)
                        - IFNULL(l.credit_adjustment_amount, 0)
                        - IFNULL(l.total_principal_paid, 0)
                        - IFNULL(l.total_interest_payable, 0)
                        - IFNULL(l.written_off_amount, 0)
                        + IFNULL(l.refund_amount, 0)
                    )
                    ELSE (
                          IFNULL(l.disbursed_amount, 0)
                        + IFNULL(l.debit_adjustment_amount, 0)
                        - IFNULL(l.credit_adjustment_amount, 0)
                        - IFNULL(l.total_principal_paid, 0)
                        - IFNULL(l.total_interest_payable, 0)
                        - IFNULL(l.written_off_amount, 0)
                        + IFNULL(l.refund_amount, 0)
                    )
                END,
                {precision}
            ) AS outstanding_principal

        FROM `tabLoan` l

        WHERE
            l.docstatus = 1
            AND l.status IN ('Disbursed', 'Partially Disbursed', 'Loan Closure Requested')
            AND l.repayment_status IN (
                'Overdue',
                'Missed Repayment',
                'Arrears',
                'Past Maturity'
            )
            AND (
                l.disbursement_date IS NULL
                OR l.disbursement_date <= %(as_of_date)s
            )
            {conditions}

        ORDER BY
            l.branch,
            l.loan_product,
            l.days_past_due DESC,
            l.applicant_name
    """.format(precision=precision, conditions=conditions)

    return frappe.db.sql(sql, filters, as_dict=True)


def get_conditions(filters):
    conditions = []

    if filters.get("branch"):
        conditions.append("AND l.branch = %(branch)s")

    if filters.get("loan_product"):
        conditions.append("AND l.loan_product = %(loan_product)s")

    if filters.get("loan_officer"):
        conditions.append("AND l.sales_person = %(loan_officer)s")

    if filters.get("repayment_status"):
        conditions.append("AND l.repayment_status = %(repayment_status)s")

    if filters.get("aging_bucket"):
        # Map UI bucket label → days_past_due range for SQL pre-filter
        bucket_map = {
            "1-30 Days": "AND l.days_past_due BETWEEN 1 AND 30",
            "31-60 Days": "AND l.days_past_due BETWEEN 31 AND 60",
            "61-90 Days": "AND l.days_past_due BETWEEN 61 AND 90",
            "91-180 Days": "AND l.days_past_due BETWEEN 91 AND 180",
            "180+ Days": "AND l.days_past_due > 180",
        }
        bucket_cond = bucket_map.get(filters.aging_bucket)
        if bucket_cond:
            conditions.append(bucket_cond)

    if filters.get("npa_only"):
        conditions.append("AND (l.is_npa = 1 OR l.manual_npa = 1)")

    return " ".join(conditions)


# ── Step 2: enrich with overdue amounts from LIA ─────────────────────────────
def enrich_overdue_amounts(loans, as_of_date):
    """
    For each loan, pull unpaid LIA rows where due_date <= as_of_date.
    This mirrors get_accrued_interest_entries() in loan_repayment.py.

    Overdue interest   = SUM(interest_amount      - paid_interest_amount)   WHERE due_date <= as_of_date
    Overdue principal  = SUM(payable_principal_amount - paid_principal_amount) WHERE due_date <= as_of_date
    Overdue penalty    = SUM(penalty_amount - IFNULL(paid_penalty, 0))       WHERE accrual_type = 'Penalty'
    """
    if not loans:
        return

    precision = cint(frappe.db.get_default("currency_precision")) or 2
    loan_names = [l.name for l in loans]

    # ── Overdue interest & principal from regular LIA rows ──
    lia_rows = frappe.db.sql(
        """
        SELECT
            loan,
            SUM(GREATEST(interest_amount - paid_interest_amount, 0))           AS overdue_interest,
            SUM(GREATEST(payable_principal_amount - paid_principal_amount, 0))  AS overdue_principal
        FROM `tabLoan Interest Accrual`
        WHERE
            loan IN %(loans)s
            AND docstatus = 1
            AND accrual_type != 'Penalty'
            AND due_date <= %(as_of_date)s
            AND (
                interest_amount      - paid_interest_amount      > 0
                OR payable_principal_amount - paid_principal_amount > 0
            )
        GROUP BY loan
        """,
        {"loans": loan_names, "as_of_date": as_of_date},
        as_dict=True,
    )

    # ── Overdue penalty from penalty LIA rows ──
    penalty_rows = frappe.db.sql(
        """
        SELECT
            loan,
            SUM(GREATEST(penalty_amount - IFNULL(paid_interest_amount, 0), 0)) AS overdue_penalty
        FROM `tabLoan Interest Accrual`
        WHERE
            loan IN %(loans)s
            AND docstatus = 1
            AND accrual_type = 'Penalty'
            AND due_date <= %(as_of_date)s
        GROUP BY loan
        """,
        {"loans": loan_names, "as_of_date": as_of_date},
        as_dict=True,
    )

    # Build lookup maps
    lia_map = {r.loan: r for r in lia_rows}
    penalty_map = {r.loan: r for r in penalty_rows}

    for loan in loans:
        lia = lia_map.get(loan.name)
        penalty = penalty_map.get(loan.name)

        loan["overdue_interest"] = flt(lia.overdue_interest, precision) if lia else 0.0
        loan["overdue_principal"] = (
            flt(lia.overdue_principal, precision) if lia else 0.0
        )
        loan["overdue_penalty"] = (
            flt(penalty.overdue_penalty, precision) if penalty else 0.0
        )
        loan["total_overdue"] = flt(
            loan["overdue_principal"]
            + loan["overdue_interest"]
            + loan["overdue_penalty"],
            precision,
        )


# ── Step 3: assign aging bucket labels ───────────────────────────────────────
def assign_aging_buckets(loans):
    """
    Bucket by days_past_due — the field maintained by update_loan_repayment_status()
    and restored by on_cancel() via frappe.db.set_value("Loan", ..., "days_past_due", ...).
    """
    for loan in loans:
        dpd = cint(loan.get("days_past_due") or 0)
        if dpd <= 0:
            loan["aging_bucket"] = "Current"
        elif dpd <= 30:
            loan["aging_bucket"] = "1-30 Days"
        elif dpd <= 60:
            loan["aging_bucket"] = "31-60 Days"
        elif dpd <= 90:
            loan["aging_bucket"] = "61-90 Days"
        elif dpd <= 180:
            loan["aging_bucket"] = "91-180 Days"
        else:
            loan["aging_bucket"] = "180+ Days"


# ─────────────────────────────────────────────────────────────────────────────
# GROUPING & SUBTOTALS
# Branch > Loan Product > Detail rows > Product subtotal > Branch total
# ─────────────────────────────────────────────────────────────────────────────
def build_grouped_rows(loans):
    from itertools import groupby

    result = []

    loans_sorted = sorted(
        loans,
        key=lambda r: (
            r.branch or "",
            r.loan_product or "",
            -(cint(r.days_past_due or 0)),  # most overdue first within group
            r.applicant_name or "",
        ),
    )

    for branch, branch_iter in groupby(loans_sorted, key=lambda r: r.branch or ""):
        branch_rows = list(branch_iter)

        b_olp = b_od_prin = b_od_int = b_od_pen = b_total_od = 0
        b_clients = 0

        for product, product_iter in groupby(
            branch_rows, key=lambda r: r.loan_product or ""
        ):
            product_rows = list(product_iter)

            p_olp = sum(flt(r.outstanding_principal) for r in product_rows)
            p_od_prin = sum(flt(r.overdue_principal) for r in product_rows)
            p_od_int = sum(flt(r.overdue_interest) for r in product_rows)
            p_od_pen = sum(flt(r.overdue_penalty) for r in product_rows)
            p_total_od = sum(flt(r.total_overdue) for r in product_rows)
            p_clients = len(product_rows)

            for row in product_rows:
                result.append(row)

            # ── Loan Product subtotal ──
            result.append(
                frappe._dict(
                    {
                        "applicant_name": _("Subtotal — {0}").format(
                            product or _("No Product")
                        ),
                        "outstanding_principal": p_olp,
                        "overdue_principal": p_od_prin,
                        "overdue_interest": p_od_int,
                        "overdue_penalty": p_od_pen,
                        "total_overdue": p_total_od,
                        "bold": 1,
                        "_is_subtotal": 1,
                        "_active_clients": p_clients,
                    }
                )
            )

            b_olp += p_olp
            b_od_prin += p_od_prin
            b_od_int += p_od_int
            b_od_pen += p_od_pen
            b_total_od += p_total_od
            b_clients += p_clients

        # ── Branch total ──
        result.append(
            frappe._dict(
                {
                    "applicant_name": _("Branch Total — {0}").format(
                        branch or _("No Branch")
                    ),
                    "outstanding_principal": b_olp,
                    "overdue_principal": b_od_prin,
                    "overdue_interest": b_od_int,
                    "overdue_penalty": b_od_pen,
                    "total_overdue": b_total_od,
                    "bold": 1,
                    "_is_branch_total": 1,
                    "_active_clients": b_clients,
                }
            )
        )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY CARDS
# ─────────────────────────────────────────────────────────────────────────────
def get_summary(data):
    detail_rows = [r for r in data if r.get("name")]

    total_overdue_clients = len(detail_rows)
    total_olp = sum(flt(r.outstanding_principal) for r in detail_rows)
    total_od_principal = sum(flt(r.overdue_principal) for r in detail_rows)
    total_od_interest = sum(flt(r.overdue_interest) for r in detail_rows)
    total_od_penalty = sum(flt(r.overdue_penalty) for r in detail_rows)
    total_overdue = sum(flt(r.total_overdue) for r in detail_rows)
    npa_clients = sum(1 for r in detail_rows if r.get("is_npa") or r.get("manual_npa"))

    # Bucket-wise OLP at risk
    bucket_180_plus = sum(
        flt(r.outstanding_principal)
        for r in detail_rows
        if cint(r.get("days_past_due") or 0) > 180
    )
    bucket_91_180 = sum(
        flt(r.outstanding_principal)
        for r in detail_rows
        if 91 <= cint(r.get("days_past_due") or 0) <= 180
    )

    # Past maturity count
    past_maturity = sum(
        1 for r in detail_rows if r.get("repayment_status") == "Past Maturity"
    )

    return [
        {
            "value": total_overdue_clients,
            "label": _("Overdue Clients"),
            "datatype": "Int",
            "indicator": "red",
        },
        {
            "value": total_overdue,
            "label": _("Total Overdue Amount"),
            "datatype": "Currency",
            "indicator": "red",
        },
        {
            "value": total_od_principal,
            "label": _("Overdue Principal"),
            "datatype": "Currency",
            "indicator": "orange",
        },
        {
            "value": total_od_interest,
            "label": _("Overdue Interest"),
            "datatype": "Currency",
            "indicator": "orange",
        },
        {
            "value": total_od_penalty,
            "label": _("Overdue Penalty"),
            "datatype": "Currency",
            "indicator": "orange",
        },
        {
            "value": total_olp,
            "label": _("OLP of Overdue Loans"),
            "datatype": "Currency",
            "indicator": "red",
        },
        {
            "value": bucket_91_180,
            "label": _("OLP — 91 to 180 Days"),
            "datatype": "Currency",
            "indicator": "red",
        },
        {
            "value": bucket_180_plus,
            "label": _("OLP — 180+ Days (High Risk)"),
            "datatype": "Currency",
            "indicator": "red",
        },
        {
            "value": npa_clients,
            "label": _("NPA Clients"),
            "datatype": "Int",
            "indicator": "red",
        },
        {
            "value": past_maturity,
            "label": _("Past Maturity Loans"),
            "datatype": "Int",
            "indicator": "red",
        },
    ]
