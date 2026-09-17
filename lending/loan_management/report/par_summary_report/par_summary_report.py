# Copyright (c) 2026, UnityAlgo and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.utils import cint, flt, today


PAR_BUCKETS = [
    ("Current", 0, 4, 0.00),
    ("Watch List", 5, 29, 0.00),
    ("OAEM", 30, 59, 0.00),
    ("Substandard", 60, 89, 0.25),
    ("Doubtful", 90, 179, 0.50),
    ("Loss", 180, None, 1.00),
]


def _bucket_for_dpd(dpd):
    """Return (label, provision_rate) for a given days-past-due value."""
    for label, lo, hi, rate in PAR_BUCKETS:
        if hi is None:
            if dpd >= lo:
                return label, rate
        else:
            if lo <= dpd <= hi:
                return label, rate
    return ("Current", 0.00)


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {
            "label": _("Category"),
            "fieldname": "category",
            "fieldtype": "Data",
            "width": 160,
        },
        {
            "label": _("Days Overdue"),
            "fieldname": "dpd_range",
            "fieldtype": "Data",
            "width": 140,
        },
        {
            "label": _("No. of Loans"),
            "fieldname": "loan_count",
            "fieldtype": "Int",
            "width": 120,
        },
        {
            "label": _("Outstanding Principal"),
            "fieldname": "outstanding_principal",
            "fieldtype": "Currency",
            "width": 180,
        },
        {
            "label": _("Outstanding Interest"),
            "fieldname": "outstanding_interest",
            "fieldtype": "Currency",
            "width": 170,
        },
        {
            "label": _("Outstanding Penalty"),
            "fieldname": "outstanding_penalty",
            "fieldtype": "Currency",
            "width": 165,
        },
        {
            "label": _("Total Exposure"),
            "fieldname": "total_exposure",
            "fieldtype": "Currency",
            "width": 155,
        },
        {
            "label": _("Provision Rate"),
            "fieldname": "provision_rate",
            "fieldtype": "Percent",
            "width": 130,
        },
        {
            "label": _("Provision Amount"),
            "fieldname": "provision_amount",
            "fieldtype": "Currency",
            "width": 165,
        },
        {
            "label": _("% of Portfolio"),
            "fieldname": "pct_portfolio",
            "fieldtype": "Percent",
            "width": 130,
        },
    ]


# ── Main data builder ─────────────────────────────────────────────────────────


def get_data(filters):
    as_of_date = filters.get("as_of_date") or today()
    branch = filters.get("branch")
    loan_type = filters.get("loan_type")
    loan_officer = filters.get("loan_officer")

    loans = _fetch_active_loans(as_of_date, branch, loan_type, loan_officer)
    if not loans:
        return _empty_rows()

    loan_names = [l["name"] for l in loans]
    enriched = _enrich_loans(loans, loan_names, as_of_date)
    return _aggregate_into_buckets(enriched)


def _build_loan_conditions(as_of_date, branch, loan_type, loan_officer):
    """
    Build WHERE clause + params dict for the base loan query.

    Loans eligible for PAR:
      - docstatus = 1 (submitted)
      - status IN ('Disbursed', 'Partially Disbursed', 'Loan Closure Requested')
        ('Closed' loans have outstanding_principal = 0 and should not appear)
      - disbursement_date <= as_of_date  (loan existed by the report date)
    """
    cond = (
        "l.docstatus = 1 "
        "AND l.status IN ('Disbursed', 'Partially Disbursed', 'Loan Closure Requested') "
        "AND l.disbursement_date <= %(as_of_date)s"
    )
    params = {"as_of_date": as_of_date}

    if branch:
        cond += " AND l.branch = %(branch)s"
        params["branch"] = branch

    if loan_type:
        cond += " AND l.loan_type = %(loan_type)s"
        params["loan_type"] = loan_type

    if loan_officer:
        cond += " AND l.sales_person = %(loan_officer)s"
        params["loan_officer"] = loan_officer

    return cond, params


def _fetch_active_loans(as_of_date, branch, loan_type, loan_officer):
    """
    Returns one row per active loan with basic identity + disbursed_amount.
    disbursed_amount is summed from tabLoan Disbursement so it reflects
    partial-disbursement situations correctly (same as get_pending_principal_amount).
    """
    cond, params = _build_loan_conditions(as_of_date, branch, loan_type, loan_officer)

    return frappe.db.sql(
        f"""
        SELECT
            l.name                                          AS name,
            l.applicant_name                                AS applicant_name,
            l.branch                                        AS branch,
            l.sales_person                                  AS loan_officer,
            COALESCE(SUM(ld.disbursed_amount), l.loan_amount)
                                                            AS disbursed_amount
        FROM `tabLoan` l
        LEFT JOIN `tabLoan Disbursement` ld
            ON  ld.against_loan = l.name
            AND ld.docstatus    = 1
            AND ld.disbursement_date <= %(as_of_date)s
        WHERE {cond}
        GROUP BY l.name
        """,
        params,
        as_dict=True,
    )


def _enrich_loans(loans, loan_names, as_of_date):
    """
    Runs 4 batch queries (principal paid, interest accrued, penalty accrued,
    earliest unpaid LIA due_date) and attaches results to each loan dict.
    Returns list of dicts with: outstanding_principal, outstanding_interest,
    outstanding_penalty, days_past_due.
    """
    if not loan_names:
        return []

    # Frappe placeholders for IN clause
    placeholders = ", ".join(["%s"] * len(loan_names))

    # ── Q1. Principal paid up to as_of_date ───────────────────────────────────
    principal_paid_map = {}
    rows = frappe.db.sql(
        f"""
        SELECT
            lr.against_loan                         AS loan,
            ROUND(SUM(lr.principal_amount_paid), 2) AS principal_paid
        FROM `tabLoan Repayment` lr
        WHERE lr.docstatus     = 1
          AND lr.against_loan  IN ({placeholders})
          AND lr.posting_date  <= %s
        GROUP BY lr.against_loan
        """,
        loan_names + [as_of_date],
        as_dict=True,
    )
    for r in rows:
        principal_paid_map[r["loan"]] = flt(r["principal_paid"])

    # ── Q2. Unpaid interest from LIAs (non-penalty) ───────────────────────────
    # Uses the same LIA − paid formula as the OLP report.
    interest_map = {}
    rows = frappe.db.sql(
        f"""
        SELECT
            lia.loan                                    AS loan,
            ROUND(SUM(
                GREATEST(
                    COALESCE(lia.interest_amount, 0)
                    - COALESCE(lia.paid_interest_amount, 0),
                0)
            ), 2)                                       AS outstanding_interest
        FROM `tabLoan Interest Accrual` lia
        WHERE lia.docstatus      = 1
          AND lia.accrual_type   IN ('Regular', 'Repayment', 'Disbursement')
          AND lia.loan           IN ({placeholders})
          AND lia.due_date       <= %s
          AND (lia.interest_amount - COALESCE(lia.paid_interest_amount, 0)) > 0
        GROUP BY lia.loan
        """,
        loan_names + [as_of_date],
        as_dict=True,
    )
    for r in rows:
        interest_map[r["loan"]] = flt(r["outstanding_interest"])

    # ── Q3. Unpaid penalty from Penalty-type LIAs ─────────────────────────────
    # penalty_amount is billed; paid_interest_amount tracks what was paid
    # against the penalty (the field naming is a Frappe quirk — same field
    # reused for penalty paid tracking in update_paid_amount).
    penalty_map = {}
    rows = frappe.db.sql(
        f"""
        SELECT
            lia.loan                                    AS loan,
            ROUND(SUM(
                GREATEST(
                    COALESCE(lia.penalty_amount, 0)
                    - COALESCE(lia.paid_interest_amount, 0),
                0)
            ), 2)                                       AS outstanding_penalty
        FROM `tabLoan Interest Accrual` lia
        WHERE lia.docstatus      = 1
          AND lia.accrual_type   = 'Penalty'
          AND lia.loan           IN ({placeholders})
          AND lia.posting_date   <= %s
          AND (lia.penalty_amount - COALESCE(lia.paid_interest_amount, 0)) > 0
        GROUP BY lia.loan
        """,
        loan_names + [as_of_date],
        as_dict=True,
    )
    for r in rows:
        penalty_map[r["loan"]] = flt(r["outstanding_penalty"])

    # ── Q4. Earliest unpaid LIA due_date → DPD ────────────────────────────────
    # DPD = as_of_date − MIN(due_date) where at least one head (principal or
    # interest) still has an unpaid balance.
    # DATEDIFF returns an integer number of days; negative is guarded to 0.
    dpd_map = {}
    rows = frappe.db.sql(
        f"""
        SELECT
            lia.loan                                        AS loan,
            GREATEST(
                DATEDIFF(%s, MIN(lia.due_date)),
            0)                                              AS days_past_due
        FROM `tabLoan Interest Accrual` lia
        WHERE lia.docstatus      = 1
          AND lia.accrual_type   IN ('Regular', 'Repayment', 'Disbursement')
          AND lia.loan           IN ({placeholders})
          AND lia.due_date       <= %s
          AND (
                COALESCE(lia.interest_amount, 0)
                - COALESCE(lia.paid_interest_amount, 0) > 0
              OR
                COALESCE(lia.payable_principal_amount, 0)
                - COALESCE(lia.paid_principal_amount, 0)  > 0
          )
        GROUP BY lia.loan
        """,
        [as_of_date] + loan_names + [as_of_date],
        as_dict=True,
    )
    for r in rows:
        dpd_map[r["loan"]] = int(r["days_past_due"] or 0)

    # ── Merge ─────────────────────────────────────────────────────────────────
    enriched = []
    for loan in loans:
        name = loan["name"]
        disbursed = flt(loan["disbursed_amount"])
        principal_paid = principal_paid_map.get(name, 0.0)

        outstanding_principal = max(disbursed - principal_paid, 0.0)
        outstanding_interest = interest_map.get(name, 0.0)
        outstanding_penalty = penalty_map.get(name, 0.0)
        days_past_due = dpd_map.get(name, 0)

        enriched.append(
            {
                **loan,
                "outstanding_principal": outstanding_principal,
                "outstanding_interest": outstanding_interest,
                "outstanding_penalty": outstanding_penalty,
                "days_past_due": days_past_due,
            }
        )

    return enriched


# ── Aggregation ───────────────────────────────────────────────────────────────


def _aggregate_into_buckets(enriched):
    """
    Groups enriched loan rows into PAR buckets.
    Returns Frappe report rows: one data row per bucket + a Grand Total row.
    Rows with zero exposure in a bucket are still shown (zero-fill) so the
    table always has all 6 categories, matching the client's summary table.
    """
    # Initialise accumulators keyed by bucket label
    buckets = {
        label: {
            "dpd_range": _dpd_range_label(lo, hi),
            "provision_rate_raw": rate,
            "loan_count": 0,
            "outstanding_principal": 0.0,
            "outstanding_interest": 0.0,
            "outstanding_penalty": 0.0,
        }
        for label, lo, hi, rate in PAR_BUCKETS
    }

    total_portfolio_principal = 0.0

    for loan in enriched:
        dpd = loan["days_past_due"]
        label, _ = _bucket_for_dpd(dpd)
        b = buckets[label]

        b["loan_count"] += 1
        b["outstanding_principal"] += loan["outstanding_principal"]
        b["outstanding_interest"] += loan["outstanding_interest"]
        b["outstanding_penalty"] += loan["outstanding_penalty"]

        total_portfolio_principal += loan["outstanding_principal"]

    # Build output rows
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    data = []
    gt_count = 0
    gt_principal = 0.0
    gt_interest = 0.0
    gt_penalty = 0.0
    gt_provision = 0.0

    for label, lo, hi, rate in PAR_BUCKETS:
        b = buckets[label]

        op = round(b["outstanding_principal"], precision)
        oi = round(b["outstanding_interest"], precision)
        ope = round(b["outstanding_penalty"], precision)
        tot = round(op + oi + ope, precision)
        prov = round(op * rate, precision)
        pct = (
            round((op / total_portfolio_principal * 100), 2)
            if total_portfolio_principal
            else 0.0
        )

        data.append(
            {
                "category": label,
                "dpd_range": b["dpd_range"],
                "loan_count": b["loan_count"],
                "outstanding_principal": op,
                "outstanding_interest": oi,
                "outstanding_penalty": ope,
                "total_exposure": tot,
                "provision_rate": round(rate * 100, 2),
                "provision_amount": prov,
                "pct_portfolio": pct,
                "bold": 0,
                "indent": 0,
            }
        )

        gt_count += b["loan_count"]
        gt_principal += op
        gt_interest += oi
        gt_penalty += ope
        gt_provision += prov

    # Grand Total row
    gt_total = round(gt_principal + gt_interest + gt_penalty, precision)
    data.append(
        {
            "category": "Grand Total",
            "dpd_range": "",
            "loan_count": gt_count,
            "outstanding_principal": round(gt_principal, precision),
            "outstanding_interest": round(gt_interest, precision),
            "outstanding_penalty": round(gt_penalty, precision),
            "total_exposure": gt_total,
            "provision_rate": None,
            "provision_amount": round(gt_provision, precision),
            "pct_portfolio": 100.0 if total_portfolio_principal else 0.0,
            "bold": 1,
            "indent": 0,
        }
    )

    return data


# ── Utility helpers ───────────────────────────────────────────────────────────


def _dpd_range_label(lo, hi):
    if hi is None:
        return f"{lo}+ Days"
    if lo == 0:
        return f"0 – {hi} Days"
    return f"{lo} – {hi} Days"


def _empty_rows():
    """Return zero-filled bucket rows when no loans match the filters."""
    data = []
    for label, lo, hi, rate in PAR_BUCKETS:
        data.append(
            {
                "category": label,
                "dpd_range": _dpd_range_label(lo, hi),
                "loan_count": 0,
                "outstanding_principal": 0.0,
                "outstanding_interest": 0.0,
                "outstanding_penalty": 0.0,
                "total_exposure": 0.0,
                "provision_rate": round(rate * 100, 2),
                "provision_amount": 0.0,
                "pct_portfolio": 0.0,
                "bold": 0,
                "indent": 0,
            }
        )
    data.append(
        {
            "category": "Grand Total",
            "dpd_range": "",
            "loan_count": 0,
            "outstanding_principal": 0.0,
            "outstanding_interest": 0.0,
            "outstanding_penalty": 0.0,
            "total_exposure": 0.0,
            "provision_rate": None,
            "provision_amount": 0.0,
            "pct_portfolio": 0.0,
            "bold": 1,
            "indent": 0,
        }
    )
    return data
