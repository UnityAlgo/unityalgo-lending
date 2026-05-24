# Copyright (c) 2026, UnityAlgo and contributors
# For license information, please see license.txt

# ─────────────────────────────────────────────────────────────────────────────
# RECOVERY REPORT
# ─────────────────────────────────────────────────────────────────────────────
#
# Shows how much was RECOVERED (actually collected) broken down by head:
#   Principal, Interest, Penalty, Fees — grouped by Branch and/or CRO.
#
# All figures come from submitted tabLoan Repayment records, filtered by
# lr.posting_date in the selected date range.
#
# Source fields (set by allocate_amounts / update_paid_amount in loan_repayment.py):
#   principal_amount_paid  → allocate_principal_amount_for_term_loans + waterfall
#   total_interest_paid    → allocate_interest_amount + waterfall
#   total_penalty_paid     → allocate_penalty
#   Fees                   → tabLoan Repayment Charges child table: allocated_amount
#
# Layout:
#   Branch (bold header)
#     └── CRO name (sub-row with per-head totals)
#   Branch Total (bold)
#   Grand Total (bold)
# ─────────────────────────────────────────────────────────────────────────────

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data    = get_data(filters)
    return columns, data


def get_columns():
    return [
        {
            "label":     _("Branch / CRO"),
            "fieldname": "label",
            "fieldtype": "Data",
            "width":     220,
        },
        {
            "label":     _("No. of Repayments"),
            "fieldname": "repayment_count",
            "fieldtype": "Int",
            "width":     160,
        },
        {
            "label":     _("Principal Recovered"),
            "fieldname": "principal",
            "fieldtype": "Currency",
            "width":     180,
        },
        {
            "label":     _("Interest Recovered"),
            "fieldname": "interest",
            "fieldtype": "Currency",
            "width":     170,
        },
        {
            "label":     _("Penalty Recovered"),
            "fieldname": "penalty",
            "fieldtype": "Currency",
            "width":     160,
        },
        {
            "label":     _("Total Recovered"),
            "fieldname": "total",
            "fieldtype": "Currency",
            "width":     170,
        },
    ]


def get_data(filters):
    from_date      = filters.get("from_date")
    to_date        = filters.get("to_date")
    branch_filter  = filters.get("branch")
    cro_filter     = filters.get("loan_officer")   # sales_person on tabLoan

    # ── 1. Main recovery query ────────────────────────────────────────────────
    # Pulls principal, interest, penalty per repayment, grouped by branch + CRO.
    # Filtered by lr.posting_date (when cash was actually received).
    cond   = "lr.docstatus = 1"
    params = {}

    if from_date:
        cond += " AND lr.posting_date >= %(from_date)s"
        params["from_date"] = from_date
    if to_date:
        cond += " AND lr.posting_date <= %(to_date)s"
        params["to_date"] = to_date
    if branch_filter:
        cond += " AND lr.branch = %(branch)s"
        params["branch"] = branch_filter
    if cro_filter:
        cond += " AND l.sales_person = %(cro)s"
        params["cro"] = cro_filter

    rows = frappe.db.sql(
        f"""
        SELECT
            COALESCE(lr.branch, 'No Branch')        AS branch,
            COALESCE(l.sales_person, 'Unassigned')  AS cro,
            COUNT(lr.name)                           AS repayment_count,
            ROUND(SUM(lr.principal_amount_paid), 2)  AS principal,
            ROUND(SUM(lr.total_interest_paid),   2)  AS interest,
            ROUND(SUM(lr.total_penalty_paid),    2)  AS penalty
        FROM `tabLoan Repayment` lr
        INNER JOIN `tabLoan` l ON l.name = lr.against_loan
        WHERE {cond}
        GROUP BY lr.branch, l.sales_person
        ORDER BY lr.branch, l.sales_person
        """,
        params,
        as_dict=True,
    )

    if not rows:
        return []

    # ── 2. Fees recovery query ─────────────────────────────────────────────────
    # tabLoan Repayment Charges child table, same date + branch + CRO scope.
    # allocated_amount is set by allocate_charges() in loan_repayment.py.
    fee_cond   = "lr.docstatus = 1"
    fee_params = {}

    if from_date:
        fee_cond += " AND lr.posting_date >= %(from_date)s"
        fee_params["from_date"] = from_date
    if to_date:
        fee_cond += " AND lr.posting_date <= %(to_date)s"
        fee_params["to_date"] = to_date
    if branch_filter:
        fee_cond += " AND lr.branch = %(branch)s"
        fee_params["branch"] = branch_filter
    if cro_filter:
        fee_cond += " AND l.sales_person = %(cro)s"
        fee_params["cro"] = cro_filter

    try:
        fee_rows = frappe.db.sql(
            f"""
            SELECT
                COALESCE(lr.branch, 'No Branch')        AS branch,
                COALESCE(l.sales_person, 'Unassigned')  AS cro,
                ROUND(SUM(lrc.allocated_amount), 2)      AS fees
            FROM `tabLoan Repayment` lr
            INNER JOIN `tabLoan` l ON l.name = lr.against_loan
            INNER JOIN `tabLoan Repayment Charges` lrc ON lrc.parent = lr.name
            WHERE {fee_cond}
            GROUP BY lr.branch, l.sales_person
            """,
            fee_params,
            as_dict=True,
        )
        fee_idx = {(r.branch, r.cro): flt(r.fees) for r in fee_rows}
    except Exception:
        fee_idx = {}

    # ── 3. Build branch → CRO hierarchy ───────────────────────────────────────
    # Structure:  { branch: { cro: {principal, interest, penalty, fees, count} } }
    branch_data = {}

    for r in rows:
        branch = r.branch
        cro    = r.cro
        fees   = fee_idx.get((branch, cro), 0.0)

        branch_data.setdefault(branch, {})
        branch_data[branch][cro] = {
            "repayment_count": int(r.repayment_count or 0),
            "principal":       flt(r.principal or 0),
            "interest":        flt(r.interest  or 0),
            "penalty":         flt(r.penalty   or 0),
            "fees":            fees,
        }

    # ── 4. Assemble report rows ────────────────────────────────────────────────
    data = []

    # Grand total accumulators
    gt = {"count": 0, "p": 0, "i": 0, "pe": 0, "f": 0}

    for branch in sorted(branch_data.keys()):
        cros = branch_data[branch]

        # Branch subtotal accumulators
        bt = {"count": 0, "p": 0, "i": 0, "pe": 0, "f": 0}

        # Branch header row (bold, no amounts — totalled below)
        branch_header_idx = len(data)
        data.append({
            "label":            branch,
            "repayment_count":  None,
            "principal":        None,
            "interest":         None,
            "penalty":          None,
            "fees":             None,
            "total":            None,
            "indent":           0,
            "bold":             1,
        })

        # CRO rows under this branch
        for cro in sorted(cros.keys()):
            v = cros[cro]
            total = v["principal"] + v["interest"] + v["penalty"] + v["fees"]

            data.append({
                "label":            cro,
                "repayment_count":  v["repayment_count"],
                "principal":        v["principal"],
                "interest":         v["interest"],
                "penalty":          v["penalty"],
                "fees":             v["fees"],
                "total":            total,
                "indent":           1,
                "bold":             0,
            })

            bt["count"] += v["repayment_count"]
            bt["p"]     += v["principal"]
            bt["i"]     += v["interest"]
            bt["pe"]    += v["penalty"]
            bt["f"]     += v["fees"]

        bt_total = bt["p"] + bt["i"] + bt["pe"] + bt["f"]

        # Fill branch header with subtotals now that we have them
        data[branch_header_idx].update({
            "repayment_count": bt["count"],
            "principal":       bt["p"],
            "interest":        bt["i"],
            "penalty":         bt["pe"],
            "fees":            bt["f"],
            "total":           bt_total,
        })

        # Branch total row (bold separator)
        data.append({
            "label":            f"{branch} — Total",
            "repayment_count":  bt["count"],
            "principal":        bt["p"],
            "interest":         bt["i"],
            "penalty":          bt["pe"],
            "fees":             bt["f"],
            "total":            bt_total,
            "indent":           0,
            "bold":             1,
        })

        gt["count"] += bt["count"]
        gt["p"]     += bt["p"]
        gt["i"]     += bt["i"]
        gt["pe"]    += bt["pe"]
        gt["f"]     += bt["f"]

    # Grand Total row
    gt_total = gt["p"] + gt["i"] + gt["pe"] + gt["f"]
    data.append({
        "label":            "Grand Total",
        "repayment_count":  gt["count"],
        "principal":        gt["p"],
        "interest":         gt["i"],
        "penalty":          gt["pe"],
        "fees":             gt["f"],
        "total":            gt_total,
        "indent":           0,
        "bold":             1,
    })

    return data