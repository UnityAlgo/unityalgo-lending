# Copyright (c) 2026, UnityAlgo and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import flt, nowdate


def execute(filters=None):
    filters = filters or {}

    from_date = filters.get("from_date")
    to_date = filters.get("to_date")
    branches = filters.get("branch") or []
    if from_date is None:
        from_date = "2018-06-01"
    if to_date is None:
        to_date = nowdate()

    if isinstance(branches, str):
        branches = [b.strip() for b in branches.split(",") if b.strip()]

    def branch_loan(alias="l"):
        if not branches:
            return ""
        ph = ",".join(["%s"] * len(branches))
        return f" AND {alias}.branch IN ({ph})"

    def branch_rep(alias="r"):
        if not branches:
            return ""
        ph = ",".join(["%s"] * len(branches))
        return f" AND {alias}.branch IN ({ph})"

    def branch_disb(alias="d"):
        if not branches:
            return ""
        ph = ",".join(["%s"] * len(branches))
        return f" AND {alias}.branch IN ({ph})"

    bv = tuple(branches)  # branch values for params

    columns = [
        {"label": "No.", "fieldname": "no", "fieldtype": "Data", "width": 80},
        {
            "label": "Description",
            "fieldname": "description",
            "fieldtype": "Data",
            "width": 540,
        },
        {"label": "Value", "fieldname": "value", "fieldtype": "Data", "width": 220},
    ]

    # ── 1. Registered Borrowers as at to_date ────────────────────────────
    # Borrowers who had at least one submitted loan on or before to_date
    registered_borrowers = (
        frappe.db.sql(
            f"""
        SELECT COUNT(DISTINCT l.applicant)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (to_date,) + bv,
        )[0][0]
        or 0
    )

    # ── 2. Active Borrowers as at to_date ─────────────────────────────────
    # Disbursed loans that were active on to_date (disbursed before/on to_date,
    # and either still open or closed after to_date)
    active_borrowers = (
        frappe.db.sql(
            f"""
        SELECT COUNT(DISTINCT l.applicant)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.status = 'Disbursed'
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (to_date,) + bv,
        )[0][0]
        or 0
    )

    # ── 3. Principal Portfolio Disbursed (cumulative up to to_date) ───────
    total_disbursed = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(SUM(d.disbursed_amount), 0)
        FROM `tabLoan Disbursement` d
        WHERE d.docstatus = 1
          AND d.disbursement_date >= %s
          AND d.disbursement_date <= %s
        {branch_disb("d")}
    """,
            (from_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── 4. Fully Paid Loans (closed within date range) ────────────────────
    fully_paid_loans = (
        frappe.db.sql(
            f"""
        SELECT COUNT(*)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.status IN ('Closed', 'Write Off')
          AND l.disbursement_date >= %s
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (from_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── 5a. Open Loans as at to_date ──────────────────────────────────────
    open_loans = (
        frappe.db.sql(
            f"""
        SELECT COUNT(*)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.status = 'Disbursed'
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (to_date,) + bv,
        )[0][0]
        or 0
    )

    # ── 5b. Open Loans Created within date range ──────────────────────────
    open_loans_created = (
        frappe.db.sql(
            f"""
        SELECT COUNT(*)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.disbursement_date >= %s
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (from_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── 5c. Opening Balance — outstanding principal as at from_date ────────
    # Sum of (disbursed - principal_paid_on_or_before from_date) for loans
    # that were active as at from_date. Using repayment table to derive paid
    # amount up to (but not including) from_date so the opening snapshot is accurate.
    opening_balance = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(
            SUM(l.disbursed_amount)
            - COALESCE((
                SELECT SUM(r2.principal_amount_paid)
                FROM `tabLoan Repayment` r2
                WHERE r2.docstatus = 1
                  AND r2.against_loan = l.name
                  AND r2.posting_date < %s
            ), 0),
        0)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.disbursement_date < %s
          AND (l.status = 'Disbursed'
               OR (l.status IN ('Closed', 'Write Off')
                   AND EXISTS (
                       SELECT 1 FROM `tabLoan Repayment` r3
                       WHERE r3.docstatus = 1
                         AND r3.against_loan = l.name
                         AND r3.posting_date >= %s
                   )
               ))
        {branch_loan("l")}
    """,
            (from_date, from_date, from_date) + bv,
        )[0][0]
        or 0
    )

    # ── 5d. Closing Balance — outstanding principal as at to_date ─────────
    closing_balance = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(
            SUM(l.disbursed_amount)
            - COALESCE((
                SELECT SUM(r2.principal_amount_paid)
                FROM `tabLoan Repayment` r2
                WHERE r2.docstatus = 1
                  AND r2.against_loan = l.name
                  AND r2.posting_date <= %s
            ), 0),
        0)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.disbursement_date <= %s
          AND l.status = 'Disbursed'
        {branch_loan("l")}
    """,
            (to_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── 6. Total Outstanding Balance (All Loans) as at to_date ───────────
    outstanding_all = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(
            SUM(l.disbursed_amount)
            - COALESCE((
                SELECT SUM(r2.principal_amount_paid)
                FROM `tabLoan Repayment` r2
                WHERE r2.docstatus = 1
                  AND r2.against_loan = l.name
                  AND r2.posting_date <= %s
            ), 0),
        0)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (to_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── 7. Total Outstanding Balance (Open Loans) as at to_date ──────────
    outstanding_open = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(
            SUM(l.disbursed_amount)
            - COALESCE((
                SELECT SUM(r2.principal_amount_paid)
                FROM `tabLoan Repayment` r2
                WHERE r2.docstatus = 1
                  AND r2.against_loan = l.name
                  AND r2.posting_date <= %s
            ), 0),
        0)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.status = 'Disbursed'
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (to_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── 8a-8d. Payments within date range ─────────────────────────────────
    payments = frappe.db.sql(
        f"""
        SELECT
            COALESCE(SUM(r.principal_amount_paid), 0) AS principal,
            COALESCE(SUM(r.total_interest_paid),   0) AS interest,
            COALESCE(SUM(r.total_penalty_paid),    0) AS penalty
        FROM `tabLoan Repayment` r
        WHERE r.docstatus = 1
          AND r.posting_date >= %s
          AND r.posting_date <= %s
        {branch_rep("r")}
    """,
        (from_date, to_date) + bv,
        as_dict=True,
    )[0]

    # ── Non-Deductable Fees within date range ─────────────────────────────
    non_deductable_fees = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(SUM(si.grand_total), 0)
        FROM `tabSales Invoice` si
        INNER JOIN `tabLoan` l ON l.name = si.loan
        WHERE si.docstatus = 1
          AND si.posting_date >= %s
          AND si.posting_date <= %s
        {branch_loan("l")}
    """,
            (from_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── Deductable Fees within date range ─────────────────────────────────
    deductable_fees = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(SUM(dc.amount), 0)
        FROM `tabLoan Disbursement Charge` dc
        INNER JOIN `tabLoan Disbursement` d ON d.name = dc.parent
        WHERE d.docstatus = 1
          AND d.disbursement_date >= %s
          AND d.disbursement_date <= %s
        {branch_disb("d")}
    """,
            (from_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── 9 & 10. Recovery Rate ──────────────────────────────────────────────
    total_paid_all = (
        flt(payments.principal) + flt(payments.interest) + flt(payments.penalty)
    )
    recovery_all = (
        (total_paid_all / flt(total_disbursed) * 100) if total_disbursed else 0
    )

    # Closed/written-off loans disbursed within the date range
    closed_disbursed = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(SUM(l.disbursed_amount), 0)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.status IN ('Closed', 'Write Off')
          AND l.disbursement_date >= %s
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (from_date, to_date) + bv,
        )[0][0]
        or 0
    )

    total_due_open = flt(total_disbursed) - flt(closed_disbursed)
    recovery_open = (total_paid_all / total_due_open * 100) if total_due_open else 0

    # ── 11. Portfolio at Risk (PAR) as at to_date ─────────────────────────
    # Outstanding principal for loans that are past due by X+ days as at to_date
    def get_par(days):
        return (
            frappe.db.sql(
                f"""
            SELECT COALESCE(
                SUM(l.disbursed_amount)
                - COALESCE((
                    SELECT SUM(r2.principal_amount_paid)
                    FROM `tabLoan Repayment` r2
                    WHERE r2.docstatus = 1
                      AND r2.against_loan = l.name
                      AND r2.posting_date <= %s
                ), 0),
            0)
            FROM `tabLoan` l
            WHERE l.docstatus = 1
              AND l.status = 'Disbursed'
              AND l.disbursement_date <= %s
              AND l.days_past_due >= %s
            {branch_loan("l")}
        """,
                (to_date, to_date, days) + bv,
            )[0][0]
            or 0
        )

    par_30 = get_par(30)
    par_60 = get_par(60)
    par_90 = get_par(90)

    # ── 12 & 13. Default Loans within date range ──────────────────────────
    default_loans = (
        frappe.db.sql(
            f"""
        SELECT COUNT(*)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.status = 'Write Off'
          AND l.disbursement_date >= %s
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
            (from_date, to_date) + bv,
        )[0][0]
        or 0
    )

    pct_default = (
        (default_loans / open_loans_created * 100) if open_loans_created else 0
    )

    # ── 14. Default Loans Outstanding as at to_date ───────────────────────
    # Only write-off loans disbursed within the date range
    default_outstanding = frappe.db.sql(
        f"""
        SELECT
            COALESCE(
                SUM(l.disbursed_amount)
                - COALESCE((
                    SELECT SUM(r2.principal_amount_paid)
                    FROM `tabLoan Repayment` r2
                    WHERE r2.docstatus = 1
                      AND r2.against_loan = l.name
                      AND r2.posting_date <= %s
                ), 0),
            0) AS principal,
            COALESCE(SUM(l.total_interest_payable), 0) AS interest
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.status = 'Write Off'
          AND l.disbursement_date >= %s
          AND l.disbursement_date <= %s
        {branch_loan("l")}
    """,
        (to_date, from_date, to_date) + bv,
        as_dict=True,
    )[0]

    default_fees = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(SUM(dc.amount), 0)
        FROM `tabLoan Disbursement Charge` dc
        INNER JOIN `tabLoan Disbursement` d ON d.name = dc.parent
        INNER JOIN `tabLoan` l ON l.name = d.against_loan
        WHERE d.docstatus = 1
          AND d.disbursement_date >= %s
          AND d.disbursement_date <= %s
          AND l.status = 'Write Off'
        {branch_loan("l")}
    """,
            (from_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── 15. Amount of Past Due as at to_date ─────────────────────────────
    # Outstanding principal for loans with any overdue installment as at to_date
    past_due = (
        frappe.db.sql(
            f"""
        SELECT COALESCE(
            SUM(l.disbursed_amount)
            - COALESCE((
                SELECT SUM(r2.principal_amount_paid)
                FROM `tabLoan Repayment` r2
                WHERE r2.docstatus = 1
                  AND r2.against_loan = l.name
                  AND r2.posting_date <= %s
            ), 0),
        0)
        FROM `tabLoan` l
        WHERE l.docstatus = 1
          AND l.status = 'Disbursed'
          AND l.disbursement_date <= %s
          AND l.days_past_due > 0
        {branch_loan("l")}
    """,
            (to_date, to_date) + bv,
        )[0][0]
        or 0
    )

    # ── Format helpers ────────────────────────────────────────────────────
    def fmt_num(val):
        return frappe.format_value(flt(val), {"fieldtype": "Currency"})

    def fmt_pct(val):
        return f"{flt(val):.4f}%"

    def fmt_int(val):
        return str(int(val or 0))

    branch_label = ", ".join(branches) if branches else "All Branches"

    data = [
        {
            "no": "",
            "description": f"Care Financial Services Pvt. Ltd — {branch_label}",
            "value": "",
        },
        {
            "no": "1",
            "description": f"Number of Registered Borrowers as at {to_date}",
            "value": fmt_int(registered_borrowers),
        },
        {
            "no": "2",
            "description": f"Number of Active Borrowers as at {to_date}",
            "value": fmt_int(active_borrowers),
        },
        {
            "no": "3",
            "description": f"Principal Portfolio Disbursed (cumulative) from {from_date} to {to_date}",
            "value": fmt_num(total_disbursed),
        },
        {
            "no": "4",
            "description": f"No. of Fully Paid Loans from {from_date} to {to_date}",
            "value": fmt_int(fully_paid_loans),
        },
        {"no": "5", "description": "Open Loans", "value": ""},
        {
            "no": "5a",
            "description": f"No. of Open Loans as at {to_date}",
            "value": fmt_int(open_loans),
        },
        {
            "no": "5b",
            "description": f"No. of Open Loans Created from {from_date} to {to_date}",
            "value": fmt_int(open_loans_created),
        },
        {
            "no": "5c",
            "description": f"Opening Balance as at {from_date}",
            "value": fmt_num(opening_balance),
        },
        {
            "no": "5d",
            "description": f"Closing Balance as at {to_date}",
            "value": fmt_num(closing_balance),
        },
        {
            "no": "6",
            "description": f"Total Outstanding Balance (All Loans) as at {to_date}",
            "value": fmt_num(outstanding_all),
        },
        {
            "no": "7",
            "description": f"Total Outstanding Balance (Open Loans) as at {to_date}",
            "value": fmt_num(outstanding_open),
        },
        {
            "no": "8",
            "description": f"Payments from {from_date} to {to_date}",
            "value": "",
        },
        {"no": "8a", "description": "Principal", "value": fmt_num(payments.principal)},
        {"no": "8b", "description": "Interest", "value": fmt_num(payments.interest)},
        {
            "no": "8c",
            "description": "Non-Deductable Fees",
            "value": fmt_num(non_deductable_fees),
        },
        {
            "no": "8c",
            "description": "Deductable Fees",
            "value": fmt_num(deductable_fees),
        },
        {"no": "8d", "description": "Penalty", "value": fmt_num(payments.penalty)},
        {
            "no": "9",
            "description": f"Recovery Rate (All Loans) from {from_date} to {to_date}",
            "value": fmt_pct(recovery_all),
        },
        {
            "no": "10",
            "description": f"Recovery Rate (Open Loans) from {from_date} to {to_date}",
            "value": fmt_pct(recovery_open),
        },
        {"no": "11", "description": f"Portfolio at Risk as at {to_date}", "value": ""},
        {"no": "11a", "description": "PAR 30", "value": fmt_num(par_30)},
        {"no": "11b", "description": "PAR 60", "value": fmt_num(par_60)},
        {"no": "11c", "description": "PAR 90", "value": fmt_num(par_90)},
        {
            "no": "12",
            "description": f"No. of Default Loans from {from_date} to {to_date}",
            "value": fmt_int(default_loans),
        },
        {
            "no": "13",
            "description": f"% of Default Loans from {from_date} to {to_date}",
            "value": fmt_pct(pct_default),
        },
        {
            "no": "14",
            "description": f"Default Loans Outstanding as at {to_date}",
            "value": "",
        },
        {
            "no": "14a",
            "description": "Principal",
            "value": fmt_num(default_outstanding.principal),
        },
        {
            "no": "14b",
            "description": "Interest",
            "value": fmt_num(default_outstanding.interest),
        },
        {"no": "14c", "description": "Fees", "value": fmt_num(default_fees)},
        {"no": "14d", "description": "Penalty", "value": "0.00"},
        {
            "no": "15",
            "description": f"Amount of Past Due as at {to_date}",
            "value": fmt_num(past_due),
        },
        {
            "no": "16",
            "description": f"Amount of Portfolio at Risk (PAR 90) as at {to_date}",
            "value": fmt_num(par_90),
        },
    ]

    return columns, data