# Copyright (c) 2026, UnityAlgo and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, today

PAR_BUCKETS = [
    ("Current", 0, 4, 0.00),
    ("Watch List", 5, 29, 0.00),
    ("OAEM", 30, 59, 0.00),
    ("Substandard", 60, 89, 0.25),
    ("Doubtful", 90, 179, 0.50),
    ("Loss", 180, None, 1.00),
]

BUCKET_LABELS = [b[0] for b in PAR_BUCKETS]


def _bucket_for_dpd(dpd):
    """Return (label, provision_rate) for a given days-past-due integer."""
    for label, lo, hi, rate in PAR_BUCKETS:
        if hi is None:
            if dpd >= lo:
                return label, rate
        else:
            if lo <= dpd <= hi:
                return label, rate
    return "Current", 0.00


def execute(filters=None):
    filters = filters or {}
    columns = get_columns()
    data, summary, chart = get_data(filters)
    return columns, data, None, chart, None, summary


def get_columns():
    return [
        {
            "fieldname": "loan",
            "label": _("Loan"),
            "fieldtype": "Link",
            "options": "Loan",
            "width": 160,
        },
        {
            "fieldname": "applicant_name",
            "label": _("Borrower"),
            "fieldtype": "Data",
            "width": 160,
        },
        {
            "fieldname": "branch",
            "label": _("Branch"),
            "fieldtype": "Data",
            "width": 110,
        },
        {
            "fieldname": "loan_officer",
            "label": _("CRO / Officer"),
            "fieldtype": "Data",
            "width": 130,
        },
        {
            "fieldname": "loan_type",
            "label": _("Loan Type"),
            "fieldtype": "Link",
            "options": "Loan Type",
            "width": 120,
        },
        {
            "fieldname": "disbursement_date",
            "label": _("Disbursed On"),
            "fieldtype": "Date",
            "width": 110,
        },
        {
            "fieldname": "loan_amount",
            "label": _("Loan Amount"),
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "fieldname": "disbursed_amount",
            "label": _("Disbursed Amount"),
            "fieldtype": "Currency",
            "width": 145,
        },
        {
            "fieldname": "outstanding_principal",
            "label": _("Outstanding Principal"),
            "fieldtype": "Currency",
            "width": 170,
        },
        {
            "fieldname": "outstanding_interest",
            "label": _("Outstanding Interest"),
            "fieldtype": "Currency",
            "width": 165,
        },
        {
            "fieldname": "outstanding_penalty",
            "label": _("Outstanding Penalty"),
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "fieldname": "total_exposure",
            "label": _("Total Exposure"),
            "fieldtype": "Currency",
            "width": 145,
        },
        {
            "fieldname": "days_past_due",
            "label": _("Days Overdue"),
            "fieldtype": "Int",
            "width": 110,
        },
        {
            "fieldname": "par_bucket",
            "label": _("PAR Category"),
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "fieldname": "provision_rate",
            "label": _("Provision Rate"),
            "fieldtype": "Percent",
            "width": 120,
        },
        {
            "fieldname": "provision_amount",
            "label": _("Provision Amount"),
            "fieldtype": "Currency",
            "width": 150,
        },
    ]


def get_data(filters):
    as_of_date = filters.get("as_of_date") or today()
    par_bucket = filters.get("par_bucket")  # drill-down filter from summary
    branch = filters.get("branch")
    loan_type = filters.get("loan_type")
    loan_officer = filters.get("loan_officer")

    loans = _fetch_active_loans(as_of_date, branch, loan_type, loan_officer)
    if not loans:
        return [], _empty_summary(), None

    loan_names = [l["name"] for l in loans]
    enriched = _enrich_loans(loans, loan_names, as_of_date)

    # Apply bucket filter (from drill-down) after enrichment so DPD is computed
    if par_bucket and par_bucket in BUCKET_LABELS:
        enriched = [e for e in enriched if e["par_bucket"] == par_bucket]

    if not enriched:
        return [], _empty_summary(), None

    data = _build_rows(enriched)
    summary = _build_summary(enriched)
    chart = _build_chart(enriched)

    return data, summary, chart


# ── Loan fetch ────────────────────────────────────────────────────────────────


def _fetch_active_loans(as_of_date, branch, loan_type, loan_officer):
    """
    One row per active loan. disbursed_amount summed from tabLoan Disbursement
    (handles partial-disbursement; same method as get_pending_principal_amount).
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

    return frappe.db.sql(
        f"""
        SELECT
            l.name                                              AS name,
            l.applicant_name                                    AS applicant_name,
            l.loan_type                                         AS loan_type,
            l.branch                                            AS branch,
            l.sales_person                                      AS loan_officer,
            l.disbursement_date                                 AS disbursement_date,
            l.loan_amount                                       AS loan_amount,
            COALESCE(SUM(ld.disbursed_amount), l.loan_amount)  AS disbursed_amount
        FROM `tabLoan` l
        LEFT JOIN `tabLoan Disbursement` ld
            ON  ld.against_loan     = l.name
            AND ld.docstatus        = 1
            AND ld.disbursement_date <= %(as_of_date)s
        WHERE {cond}
        GROUP BY l.name
        ORDER BY l.branch ASC, l.name ASC
        """,
        params,
        as_dict=True,
    )


def _enrich_loans(loans, loan_names, as_of_date):
    """
    4 batch queries identical to par_summary_report._enrich_loans so DPD and
    outstanding amounts are always consistent between the two reports.
    """
    placeholders = ", ".join(["%s"] * len(loan_names))

    # Q1 — Principal paid up to as_of_date
    principal_paid_map = {}
    for r in frappe.db.sql(
        f"""
        SELECT
            lr.against_loan                          AS loan,
            ROUND(SUM(lr.principal_amount_paid), 2)  AS principal_paid
        FROM `tabLoan Repayment` lr
        WHERE lr.docstatus    = 1
          AND lr.against_loan IN ({placeholders})
          AND lr.posting_date <= %s
        GROUP BY lr.against_loan
        """,
        loan_names + [as_of_date],
        as_dict=True,
    ):
        principal_paid_map[r["loan"]] = flt(r["principal_paid"])

    # Q2 — Unpaid interest (non-penalty LIAs)
    interest_map = {}
    for r in frappe.db.sql(
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
        WHERE lia.docstatus    = 1
          AND lia.accrual_type IN ('Regular', 'Repayment', 'Disbursement')
          AND lia.loan         IN ({placeholders})
          AND lia.due_date     <= %s
          AND (lia.interest_amount - COALESCE(lia.paid_interest_amount, 0)) > 0
        GROUP BY lia.loan
        """,
        loan_names + [as_of_date],
        as_dict=True,
    ):
        interest_map[r["loan"]] = flt(r["outstanding_interest"])

    # Q3 — Unpaid penalty (Penalty-type LIAs)
    # paid_interest_amount reused for penalty-paid tracking (Frappe quirk)
    penalty_map = {}
    for r in frappe.db.sql(
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
        WHERE lia.docstatus    = 1
          AND lia.accrual_type = 'Penalty'
          AND lia.loan         IN ({placeholders})
          AND lia.posting_date <= %s
          AND (lia.penalty_amount - COALESCE(lia.paid_interest_amount, 0)) > 0
        GROUP BY lia.loan
        """,
        loan_names + [as_of_date],
        as_dict=True,
    ):
        penalty_map[r["loan"]] = flt(r["outstanding_penalty"])

    # Q4 — Earliest unpaid LIA due_date → DPD
    # MIN(due_date) of rows where principal or interest still unpaid.
    # GREATEST(..., 0) guards against future-dated LIAs.
    dpd_map = {}
    for r in frappe.db.sql(
        f"""
        SELECT
            lia.loan                                    AS loan,
            GREATEST(DATEDIFF(%s, MIN(lia.due_date)), 0) AS days_past_due
        FROM `tabLoan Interest Accrual` lia
        WHERE lia.docstatus    = 1
          AND lia.accrual_type IN ('Regular', 'Repayment', 'Disbursement')
          AND lia.loan         IN ({placeholders})
          AND lia.due_date     <= %s
          AND (
                COALESCE(lia.interest_amount,          0)
                - COALESCE(lia.paid_interest_amount,   0) > 0
              OR
                COALESCE(lia.payable_principal_amount, 0)
                - COALESCE(lia.paid_principal_amount,  0) > 0
          )
        GROUP BY lia.loan
        """,
        [as_of_date] + loan_names + [as_of_date],
        as_dict=True,
    ):
        dpd_map[r["loan"]] = int(r["days_past_due"] or 0)

    # Merge
    enriched = []
    for loan in loans:
        name = loan["name"]
        disbursed = flt(loan["disbursed_amount"])
        paid = principal_paid_map.get(name, 0.0)

        op = max(disbursed - paid, 0.0)
        oi = interest_map.get(name, 0.0)
        ope = penalty_map.get(name, 0.0)
        dpd = dpd_map.get(name, 0)

        bucket_label, rate = _bucket_for_dpd(dpd)

        enriched.append(
            {
                **loan,
                "outstanding_principal": op,
                "outstanding_interest": oi,
                "outstanding_penalty": ope,
                "days_past_due": dpd,
                "par_bucket": bucket_label,
                "provision_rate_raw": rate,
            }
        )

    return enriched


# ── Row builder ───────────────────────────────────────────────────────────────


def _build_rows(enriched):
    rows = []
    for loan in sorted(
        enriched, key=lambda x: (x["par_bucket"], x["branch"] or "", x["name"])
    ):
        op = round(loan["outstanding_principal"], 2)
        oi = round(loan["outstanding_interest"], 2)
        ope = round(loan["outstanding_penalty"], 2)
        tot = round(op + oi + ope, 2)
        rate = loan["provision_rate_raw"]
        prov = round(op * rate, 2)

        rows.append(
            {
                "loan": loan["name"],
                "applicant_name": loan["applicant_name"],
                "branch": loan["branch"] or "",
                "loan_officer": loan["loan_officer"] or "",
                "loan_type": loan["loan_type"] or "",
                "disbursement_date": loan["disbursement_date"],
                "loan_amount": round(flt(loan["loan_amount"]), 2),
                "disbursed_amount": round(flt(loan["disbursed_amount"]), 2),
                "outstanding_principal": op,
                "outstanding_interest": oi,
                "outstanding_penalty": ope,
                "total_exposure": tot,
                "days_past_due": loan["days_past_due"],
                "par_bucket": loan["par_bucket"],
                "provision_rate": round(rate * 100, 2),
                "provision_amount": prov,
            }
        )
    return rows


# ── Summary cards ─────────────────────────────────────────────────────────────


def _build_summary(enriched):
    total_op = sum(e["outstanding_principal"] for e in enriched)
    at_risk_op = sum(
        e["outstanding_principal"] for e in enriched if e["days_past_due"] > 0
    )
    par30_op = sum(
        e["outstanding_principal"] for e in enriched if e["days_past_due"] > 30
    )
    par90_op = sum(
        e["outstanding_principal"] for e in enriched if e["days_past_due"] > 90
    )
    total_prov = sum(
        e["outstanding_principal"] * e["provision_rate_raw"] for e in enriched
    )

    par_pct = round(at_risk_op / total_op * 100, 2) if total_op else 0.0
    par30_pct = round(par30_op / total_op * 100, 2) if total_op else 0.0
    par90_pct = round(par90_op / total_op * 100, 2) if total_op else 0.0

    return [
        {
            "value": round(total_op, 2),
            "label": _("Total Outstanding Principal"),
            "datatype": "Currency",
            "indicator": "blue",
        },
        {
            "value": round(at_risk_op, 2),
            "label": _("At-Risk Principal ({0}%)").format(par_pct),
            "datatype": "Currency",
            "indicator": "red" if par_pct > 5 else "orange" if par_pct > 2 else "green",
        },
        {
            "value": par30_pct,
            "label": _("PAR > 30 Days"),
            "datatype": "Percent",
            "indicator": "red"
            if par30_pct > 5
            else "orange"
            if par30_pct > 2
            else "green",
        },
        {
            "value": par90_pct,
            "label": _("PAR > 90 Days"),
            "datatype": "Percent",
            "indicator": "red"
            if par90_pct > 3
            else "orange"
            if par90_pct > 1
            else "green",
        },
        {
            "value": round(total_prov, 2),
            "label": _("Total Provision Required"),
            "datatype": "Currency",
            "indicator": "orange",
        },
        {
            "value": len(enriched),
            "label": _("Loans in View"),
            "datatype": "Int",
            "indicator": "blue",
        },
    ]


def _empty_summary():
    return [
        {
            "value": 0,
            "label": _("Total Outstanding Principal"),
            "datatype": "Currency",
            "indicator": "blue",
        },
        {
            "value": 0,
            "label": _("At-Risk Principal (0%)"),
            "datatype": "Currency",
            "indicator": "green",
        },
        {
            "value": 0.0,
            "label": _("PAR > 30 Days"),
            "datatype": "Percent",
            "indicator": "green",
        },
        {
            "value": 0.0,
            "label": _("PAR > 90 Days"),
            "datatype": "Percent",
            "indicator": "green",
        },
        {
            "value": 0,
            "label": _("Total Provision Required"),
            "datatype": "Currency",
            "indicator": "orange",
        },
        {
            "value": 0,
            "label": _("Loans in View"),
            "datatype": "Int",
            "indicator": "blue",
        },
    ]


def _build_chart(enriched):
    bucket_op = {label: 0.0 for label, *_ in PAR_BUCKETS}
    for e in enriched:
        bucket_op[e["par_bucket"]] = (
            bucket_op.get(e["par_bucket"], 0.0) + e["outstanding_principal"]
        )

    labels = [b[0] for b in PAR_BUCKETS]
    values = [round(bucket_op.get(l, 0.0), 2) for l in labels]

    return {
        "data": {
            "labels": labels,
            "datasets": [{"name": _("Outstanding Principal"), "values": values}],
        },
        "type": "bar",
        "colors": ["#318AD8"],
    }
