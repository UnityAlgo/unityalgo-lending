# Copyright (c) 2026, UnityAlgo and contributors
# For license information, please see license.txt

# ─────────────────────────────────────────────────────────────────────────────
# HOW DUE AMOUNTS ARE CALCULATED
# ─────────────────────────────────────────────────────────────────────────────
#
# Due Principal & Interest use a combined LIA + Repayment Schedule strategy:
#
#   CASE 1 — Row is ACCRUED (LIA exists, joined via due_date = payment_date):
#       due_principal = LIA.payable_principal_amount - LIA.paid_principal_amount
#       due_interest  = LIA.interest_amount          - LIA.paid_interest_amount
#       → Reflects actual unpaid balance after partial payments.
#
#   CASE 2 — Row is NOT YET ACCRUED (no LIA for this payment_date):
#       due_principal = sch.principal_amount   (contractual schedule amount)
#       due_interest  = sch.interest_amount
#       → Row is not yet accrued so we show what the schedule expects.
#
# The LEFT JOIN key is: lia.loan = l.name AND lia.due_date = sch.payment_date
# loan_repayment.py always sets lia.due_date = sch.payment_date for scheduled
# and JIT accruals. Closure JIT uses posting_date but loan is closed by then.
#
# GREATEST(..., 0) guards against negatives from advance payments where
# paid_amount > accrued_amount.
#
# Due Penalty  → tabLoan Interest Accrual where accrual_type = 'Penalty'
#                (created by book_unaccrued_penalty in loan_repayment.py)
#
# Due Fees     → tabSales Invoice with loan link, outstanding_amount != 0
#                (same source as get_outstanding_invoices / add_pending_charges)
#
# Paid amounts → tabLoan Repayment: principal_amount_paid, total_interest_paid,
#                total_penalty_paid (set by allocate_amounts / update_paid_amount)
#
# Paid Fees    → tabLoan Repayment Charges child table: allocated_amount
#                (set by allocate_charges in loan_repayment.py)
# ─────────────────────────────────────────────────────────────────────────────

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {
            "label": _("Officer / Row"),
            "fieldname": "officer",
            "fieldtype": "Data",
            "width": 200,
        },
        {
            "label": _("Num Loans Released"),
            "fieldname": "loans_released",
            "fieldtype": "Int",
            "width": 150,
        },
        {
            "label": _("Principal Released"),
            "fieldname": "principal_released",
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "label": _("Principal"),
            "fieldname": "principal",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": _("Interest"),
            "fieldname": "interest",
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "label": _("Fees"),
            "fieldname": "fees",
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "label": _("Penalty"),
            "fieldname": "penalty",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": _("Total"),
            "fieldname": "total",
            "fieldtype": "Currency",
            "width": 140,
        },
    ]


def get_data(filters):
    from_date = filters.get("from_date")
    to_date = filters.get("to_date")
    officer_filter = filters.get("loan_officer")

    # ── 1. Disbursements in the period ────────────────────────────────────────
    # tabLoan Disbursement.disbursed_amount, filtered by disbursement_date.
    disb_cond = "l.docstatus = 1 AND ld.docstatus = 1"
    disb_params = {}
    if from_date:
        disb_cond += " AND ld.disbursement_date >= %(from_date)s"
        disb_params["from_date"] = from_date
    if to_date:
        disb_cond += " AND ld.disbursement_date <= %(to_date)s"
        disb_params["to_date"] = to_date
    if officer_filter:
        disb_cond += " AND l.sales_person = %(officer)s"
        disb_params["officer"] = officer_filter

    disbursements = frappe.db.sql(
        f"""
        SELECT
            l.sales_person                          AS officer,
            COUNT(DISTINCT l.name)                  AS loans_released,
            ROUND(SUM(ld.disbursed_amount), 2)      AS principal_released
        FROM `tabLoan` l
        INNER JOIN `tabLoan Disbursement` ld
            ON ld.against_loan = l.name AND ld.docstatus = 1
        WHERE {disb_cond}
        GROUP BY l.sales_person
        """,
        disb_params,
        as_dict=True,
    )

    # ── 2. Due Principal & Interest (combined LIA + Schedule) ─────────────────
    #
    # Walk every Repayment Schedule row whose payment_date is in range.
    # LEFT JOIN to LIA on (loan, due_date = payment_date).
    #
    #   LIA found    → row has been accrued; use (accrued - paid) as the due balance.
    #   LIA missing  → row is not yet accrued; use the schedule contractual amount.
    #
    # Excludes Penalty-type LIAs from the join — they are handled in Query 4.
    # GREATEST(..., 0) prevents negative dues from advance/prepayments.
    due_cond = "l.docstatus = 1 AND rs.docstatus = 1 AND rs.status = 'Active'"
    due_params = {}
    if from_date:
        due_cond += " AND sch.payment_date >= %(from_date)s"
        due_params["from_date"] = from_date
    if to_date:
        due_cond += " AND sch.payment_date <= %(to_date)s"
        due_params["to_date"] = to_date
    if officer_filter:
        due_cond += " AND l.sales_person = %(officer)s"
        due_params["officer"] = officer_filter

    due_rows = frappe.db.sql(
        f"""
        SELECT
            l.sales_person AS officer,

            ROUND(SUM(
                CASE
                    WHEN lia.name IS NOT NULL THEN
                        GREATEST(
                            COALESCE(lia.payable_principal_amount, 0)
                            - COALESCE(lia.paid_principal_amount, 0),
                        0)
                    ELSE
                        COALESCE(sch.principal_amount, 0)
                END
            ), 2) AS principal,

            ROUND(SUM(
                CASE
                    WHEN lia.name IS NOT NULL THEN
                        GREATEST(
                            COALESCE(lia.interest_amount, 0)
                            - COALESCE(lia.paid_interest_amount, 0),
                        0)
                    ELSE
                        COALESCE(sch.interest_amount, 0)
                END
            ), 2) AS interest

        FROM `tabLoan` l
        INNER JOIN `tabLoan Repayment Schedule` rs
            ON rs.loan     = l.name
            AND rs.docstatus = 1
            AND rs.status  = 'Active'
        INNER JOIN `tabRepayment Schedule` sch
            ON sch.parent  = rs.name
        LEFT JOIN `tabLoan Interest Accrual` lia
            ON  lia.loan      = l.name
            AND lia.due_date  = sch.payment_date
            AND lia.docstatus = 1
            AND lia.accrual_type IN ('Regular', 'Repayment', 'Disbursement')

        WHERE {due_cond}
        GROUP BY l.sales_person
        """,
        due_params,
        as_dict=True,
    )

    # ── 3. Due Fees ───────────────────────────────────────────────────────────
    # tabSales Invoice linked to the loan with remaining outstanding_amount.
    # Mirrors get_outstanding_invoices() called by calculate_amounts() and
    # add_pending_charges() in loan_repayment.py.
    fee_cond = "si.docstatus = 1 AND si.outstanding_amount != 0"
    fee_params = {}
    if from_date:
        fee_cond += " AND si.due_date >= %(from_date)s"
        fee_params["from_date"] = from_date
    if to_date:
        fee_cond += " AND si.due_date <= %(to_date)s"
        fee_params["to_date"] = to_date
    if officer_filter:
        fee_cond += " AND l.sales_person = %(officer)s"
        fee_params["officer"] = officer_filter

    try:
        fee_rows = frappe.db.sql(
            f"""
            SELECT
                l.sales_person                          AS officer,
                ROUND(SUM(si.outstanding_amount), 2)    AS fees
            FROM `tabSales Invoice` si
            INNER JOIN `tabLoan` l ON l.name = si.loan
            WHERE {fee_cond}
            GROUP BY l.sales_person
            """,
            fee_params,
            as_dict=True,
        )
    except Exception:
        fee_rows = []

    # ── 4. Due Penalty ────────────────────────────────────────────────────────
    # tabLoan Interest Accrual where accrual_type = 'Penalty'.
    # book_unaccrued_penalty() creates these during before_submit.
    # penalty_amount is billed; paid_interest_amount tracks what has been paid
    # against it (per update_paid_amount LIA update loop).
    # GREATEST guards against over-payment edge cases.
    pen_cond = "lia.docstatus = 1 AND lia.accrual_type = 'Penalty'"
    pen_params = {}
    if from_date:
        pen_cond += " AND lia.posting_date >= %(from_date)s"
        pen_params["from_date"] = from_date
    if to_date:
        pen_cond += " AND lia.posting_date <= %(to_date)s"
        pen_params["to_date"] = to_date
    if officer_filter:
        pen_cond += " AND l.sales_person = %(officer)s"
        pen_params["officer"] = officer_filter

    penalty_rows = frappe.db.sql(
        f"""
        SELECT
            l.sales_person                                      AS officer,
            ROUND(SUM(
                GREATEST(
                    COALESCE(lia.penalty_amount, 0)
                    - COALESCE(lia.paid_interest_amount, 0),
                0)
            ), 2)                                               AS penalty
        FROM `tabLoan Interest Accrual` lia
        INNER JOIN `tabLoan` l ON l.name = lia.loan
        WHERE {pen_cond}
        GROUP BY l.sales_person
        """,
        pen_params,
        as_dict=True,
    )

    # ── 5. Payments Made ──────────────────────────────────────────────────────
    # tabLoan Repayment (submitted). Fields written by allocate_amounts() and
    # persisted by update_paid_amount() in loan_repayment.py:
    #   principal_amount_paid → allocate_principal_amount_for_term_loans + waterfall
    #   total_interest_paid   → allocate_interest_amount + waterfall
    #   total_penalty_paid    → allocate_penalty
    pay_cond = "lr.docstatus = 1"
    pay_params = {}
    if from_date:
        pay_cond += " AND lr.posting_date >= %(from_date)s"
        pay_params["from_date"] = from_date
    if to_date:
        pay_cond += " AND lr.posting_date <= %(to_date)s"
        pay_params["to_date"] = to_date
    if officer_filter:
        pay_cond += " AND l.sales_person = %(officer)s"
        pay_params["officer"] = officer_filter

    payment_rows = frappe.db.sql(
        f"""
        SELECT
            l.sales_person                              AS officer,
            COUNT(lr.name)                              AS pay_count,
            ROUND(SUM(lr.principal_amount_paid), 2)     AS principal,
            ROUND(SUM(lr.total_interest_paid), 2)       AS interest,
            ROUND(SUM(lr.total_penalty_paid), 2)        AS penalty
        FROM `tabLoan Repayment` lr
        INNER JOIN `tabLoan` l ON l.name = lr.against_loan
        WHERE {pay_cond}
        GROUP BY l.sales_person
        """,
        pay_params,
        as_dict=True,
    )

    # ── 6. Paid Fees ──────────────────────────────────────────────────────────
    # tabLoan Repayment Charges child table, field allocated_amount.
    # allocate_charges() in loan_repayment.py sets charge.allocated_amount per
    # pending_charges row and accumulates self.total_paid_charges.
    pay_fee_cond = "lr.docstatus = 1"
    pay_fee_params = {}
    if from_date:
        pay_fee_cond += " AND lr.posting_date >= %(from_date)s"
        pay_fee_params["from_date"] = from_date
    if to_date:
        pay_fee_cond += " AND lr.posting_date <= %(to_date)s"
        pay_fee_params["to_date"] = to_date
    if officer_filter:
        pay_fee_cond += " AND l.sales_person = %(officer)s"
        pay_fee_params["officer"] = officer_filter

    try:
        pay_fee_rows = frappe.db.sql(
            f"""
            SELECT
                l.sales_person                              AS officer,
                ROUND(SUM(lrc.allocated_amount), 2)         AS fees
            FROM `tabLoan Repayment` lr
            INNER JOIN `tabLoan` l ON l.name = lr.against_loan
            INNER JOIN `tabLoan Repayment Charges` lrc ON lrc.parent = lr.name
            WHERE {pay_fee_cond}
            GROUP BY l.sales_person
            """,
            pay_fee_params,
            as_dict=True,
        )
    except Exception:
        pay_fee_rows = []

    # ── 7. Build officer set from ALL query results ───────────────────────────
    # All six result sets are included so officers who only appear in fees or
    # penalty LIAs are not silently excluded from the report.
    officers = set()
    for row in (
        disbursements + due_rows + fee_rows + penalty_rows + payment_rows + pay_fee_rows
    ):
        if row.get("officer"):
            officers.add(row["officer"])

    def _idx(rows, key="officer"):
        """Index a list of row-dicts by a key field for O(1) lookup."""
        return {r[key]: r for r in rows if r.get(key)}

    disb_idx = _idx(disbursements)
    due_idx = _idx(due_rows)
    fee_idx = _idx(fee_rows)
    pen_idx = _idx(penalty_rows)
    pay_idx = _idx(payment_rows)
    pay_fee_idx = _idx(pay_fee_rows)

    data = []

    # Grand-total accumulators
    gt = {
        "loans": 0,
        "pr": 0,
        "due_p": 0,
        "due_i": 0,
        "due_f": 0,
        "due_pe": 0,
        "pay_p": 0,
        "pay_i": 0,
        "pay_f": 0,
        "pay_pe": 0,
        "pay_count": 0,
    }

    for officer in sorted(officers):
        d = disb_idx.get(officer, {})
        due = due_idx.get(officer, {})
        fee = fee_idx.get(officer, {})
        pen = pen_idx.get(officer, {})
        pay = pay_idx.get(officer, {})
        pf = pay_fee_idx.get(officer, {})

        loans_released = int(d.get("loans_released") or 0)
        principal_released = flt(d.get("principal_released") or 0)

        # Due amounts ──────────────────────────────────────────────────────────
        # principal & interest: unpaid LIA balance OR schedule amount if unaccrued
        # fees: outstanding_amount on Sales Invoices
        # penalty: billed - paid on Penalty-type LIA records
        due_p = flt(due.get("principal") or 0)
        due_i = flt(due.get("interest") or 0)
        due_f = flt(fee.get("fees") or 0)
        due_pe = flt(pen.get("penalty") or 0)
        due_total = due_p + due_i + due_f + due_pe

        # Paid amounts ─────────────────────────────────────────────────────────
        # principal, interest, penalty from tabLoan Repayment buckets
        # fees from tabLoan Repayment Charges child table
        pay_p = flt(pay.get("principal") or 0)
        pay_i = flt(pay.get("interest") or 0)
        pay_f = flt(pf.get("fees") or 0)
        pay_pe = flt(pay.get("penalty") or 0)
        pay_total = pay_p + pay_i + pay_f + pay_pe
        pay_count = int(pay.get("pay_count") or 0)

        # Net Due ──────────────────────────────────────────────────────────────
        # Positive  = still owed this period
        # Negative  = advance payment / overpayment applied to this period
        # Note: due_p and due_i already reflect partial payments via LIA paid
        # fields, so net here captures any *further* remaining balance cleanly.
        net_p = due_p - pay_p
        net_i = due_i - pay_i
        net_f = due_f - pay_f
        net_pe = due_pe - pay_pe
        net_total = due_total - pay_total

        gt["loans"] += loans_released
        gt["pr"] += principal_released
        gt["due_p"] += due_p
        gt["due_i"] += due_i
        gt["due_f"] += due_f
        gt["due_pe"] += due_pe
        gt["pay_p"] += pay_p
        gt["pay_i"] += pay_i
        gt["pay_f"] += pay_f
        gt["pay_pe"] += pay_pe
        gt["pay_count"] += pay_count

        # Officer header row (bold, indent 0)
        data.append(
            {
                "officer": officer,
                "loans_released": loans_released,
                "principal_released": principal_released,
                "principal": None,
                "interest": None,
                "fees": None,
                "penalty": None,
                "total": None,
                "indent": 0,
                "bold": 1,
            }
        )

        # Due row — what is owed this period per LIA/schedule
        data.append(
            {
                "officer": "Due Loans:",
                "loans_released": loans_released,
                "principal_released": principal_released,
                "principal": due_p,
                "interest": due_i,
                "fees": due_f,
                "penalty": due_pe,
                "total": due_total,
                "indent": 1,
                "bold": 0,
            }
        )

        # Payments row — cash actually received per tabLoan Repayment
        data.append(
            {
                "officer": f"Payments ({pay_count}):",
                "loans_released": None,
                "principal_released": None,
                "principal": pay_p,
                "interest": pay_i,
                "fees": pay_f,
                "penalty": pay_pe,
                "total": pay_total,
                "indent": 1,
                "bold": 0,
            }
        )

        # Net Due row — remaining balance; negative = advance/overpayment
        data.append(
            {
                "officer": "Net Due:",
                "loans_released": None,
                "principal_released": None,
                "principal": net_p,
                "interest": net_i,
                "fees": net_f,
                "penalty": net_pe,
                "total": net_total,
                "indent": 1,
                "bold": 0,
            }
        )

    # ── Grand Total rows ──────────────────────────────────────────────────────
    gt_due_total = gt["due_p"] + gt["due_i"] + gt["due_f"] + gt["due_pe"]
    gt_pay_total = gt["pay_p"] + gt["pay_i"] + gt["pay_f"] + gt["pay_pe"]
    gt_net_total = gt_due_total - gt_pay_total

    data.append(
        {
            "officer": "Grand Total — Due Loans",
            "loans_released": gt["loans"],
            "principal_released": gt["pr"],
            "principal": gt["due_p"],
            "interest": gt["due_i"],
            "fees": gt["due_f"],
            "penalty": gt["due_pe"],
            "total": gt_due_total,
            "indent": 0,
            "bold": 1,
        }
    )
    data.append(
        {
            "officer": f"Grand Total — Payments ({gt['pay_count']})",
            "loans_released": None,
            "principal_released": None,
            "principal": gt["pay_p"],
            "interest": gt["pay_i"],
            "fees": gt["pay_f"],
            "penalty": gt["pay_pe"],
            "total": gt_pay_total,
            "indent": 0,
            "bold": 1,
        }
    )
    data.append(
        {
            "officer": "Grand Total — Net Due",
            "loans_released": None,
            "principal_released": None,
            "principal": gt["due_p"] - gt["pay_p"],
            "interest": gt["due_i"] - gt["pay_i"],
            "fees": gt["due_f"] - gt["pay_f"],
            "penalty": gt["due_pe"] - gt["pay_pe"],
            "total": gt_net_total,
            "indent": 0,
            "bold": 1,
        }
    )

    return data
