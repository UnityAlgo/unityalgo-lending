"""
PAR (Portfolio at Risk) Report
==============================
Standard microfinance metric. Shows what percentage of the total outstanding
loan portfolio is at risk of default, grouped by days-past-due buckets.

PAR Formula:
  PAR>X = (Outstanding principal of loans overdue > X days) / (Total outstanding principal) x 100

Buckets: Current | 1-30 | 31-60 | 61-90 | 91-180 | 180+

A loan goes into a bucket based on its OLDEST unpaid schedule row.
The ENTIRE outstanding principal of that loan is counted as at-risk,
not just the overdue installment — this is the standard PAR methodology.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, cint, date_diff, today


def execute(filters=None):
    filters = frappe._dict(filters or {})
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
            "fieldname": "applicant",
            "label": _("Borrower"),
            "fieldtype": "Data",
            "width": 150,
        },
        {
            "fieldname": "loan_product",
            "label": _("Product"),
            "fieldtype": "Link",
            "options": "Loan Product",
            "width": 120,
        },
        {
            "fieldname": "branch",
            "label": _("Branch"),
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "fieldname": "sales_person",
            "label": _("CRO"),
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "fieldname": "disbursement_date",
            "label": _("Disbursed"),
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "fieldname": "loan_amount",
            "label": _("Loan Amount"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "outstanding_principal",
            "label": _("Outstanding Principal"),
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "fieldname": "overdue_days",
            "label": _("Days Overdue"),
            "fieldtype": "Int",
            "width": 90,
        },
        {
            "fieldname": "par_bucket",
            "label": _("PAR Bucket"),
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "fieldname": "overdue_principal",
            "label": _("At-Risk Principal"),
            "fieldtype": "Currency",
            "width": 140,
        },
    ]


def get_data(filters):
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    as_of_date = getdate(filters.get("as_of_date") or today())

    buckets = [
        {"label": "Current", "min": 0, "max": 0},
        {"label": "PAR 1-30", "min": 1, "max": 30},
        {"label": "PAR 31-60", "min": 31, "max": 60},
        {"label": "PAR 61-90", "min": 61, "max": 90},
        {"label": "PAR 91-180", "min": 91, "max": 180},
        {"label": "PAR 180+", "min": 181, "max": 999999},
    ]

    loan_filters = {
        "docstatus": 1,
        "status": ("in", ["Disbursed", "Loan Closure Requested"]),
    }

    if filters.get("company"):
        loan_filters["company"] = filters.company
    if filters.get("loan_product"):
        loan_filters["loan_product"] = filters.loan_product
    if filters.get("branch"):
        loan_filters["branch"] = filters.branch

    loans = frappe.db.get_all(
        "Loan",
        filters=loan_filters,
        fields=[
            "name",
            "applicant",
            "applicant_name",
            "loan_product",
            "branch",
            "sales_person",
            "disbursement_date",
            "loan_amount",
            "disbursed_amount",
            "total_payment",
            "total_principal_paid",
            "total_interest_payable",
            "debit_adjustment_amount",
            "credit_adjustment_amount",
            "written_off_amount",
            "refund_amount",
            "status",
            "is_term_loan",
        ],
        order_by="branch asc, name asc",
    )

    data = []
    bucket_totals = {
        b["label"]: {"count": 0, "outstanding": 0.0, "at_risk": 0.0} for b in buckets
    }
    total_outstanding = 0.0

    for loan in loans:
        outstanding = flt(
            flt(loan.total_payment)
            + flt(loan.debit_adjustment_amount)
            - flt(loan.credit_adjustment_amount)
            - flt(loan.total_principal_paid)
            - flt(loan.total_interest_payable)
            - flt(loan.written_off_amount)
            + flt(loan.refund_amount),
            precision,
        )

        if outstanding <= 0:
            continue

        overdue_days = 0
        if loan.is_term_loan:
            overdue_days = get_overdue_days_for_loan(loan.name, as_of_date, precision)

        bucket_label = "Current"
        for b in buckets:
            if b["min"] <= overdue_days <= b["max"]:
                bucket_label = b["label"]
                break

        at_risk = outstanding if overdue_days > 0 else 0.0

        data.append(
            {
                "loan": loan.name,
                "applicant": loan.applicant_name or loan.applicant,
                "loan_product": loan.loan_product,
                "branch": loan.branch or "",
                "sales_person": loan.sales_person or "",
                "disbursement_date": loan.disbursement_date,
                "loan_amount": flt(loan.loan_amount, precision),
                "outstanding_principal": outstanding,
                "overdue_days": overdue_days,
                "par_bucket": bucket_label,
                "overdue_principal": at_risk,
            }
        )

        total_outstanding += outstanding
        bucket_totals[bucket_label]["count"] += 1
        bucket_totals[bucket_label]["outstanding"] += outstanding
        bucket_totals[bucket_label]["at_risk"] += at_risk

    summary = build_summary(bucket_totals, total_outstanding, precision)
    chart = build_chart(bucket_totals, total_outstanding, precision)

    # frappe.msgprint(str(data))
    return data, summary, chart


def get_overdue_days_for_loan(loan_name, as_of_date, precision):
    """Find the oldest unpaid schedule row and return days overdue."""
    schedule_id = frappe.db.get_value(
        "Loan Repayment Schedule",
        {"loan": loan_name, "docstatus": 1, "status": "Active"},
        "name",
    )

    if not schedule_id:
        schedule_id = frappe.db.get_value(
            "Loan Repayment Schedule",
            {"loan": loan_name, "docstatus": 1},
            "name",
            order_by="creation desc",
        )

    if not schedule_id:
        return 0

    schedule_rows = frappe.db.get_all(
        "Repayment Schedule",
        filters={"parent": schedule_id},
        fields=["payment_date", "principal_amount", "interest_amount"],
        order_by="idx asc",
    )

    paid_data = frappe.db.sql(
        """
		SELECT due_date,
			   SUM(paid_principal_amount) as paid_prin,
			   SUM(paid_interest_amount) as paid_int
		FROM `tabLoan Interest Accrual`
		WHERE loan = %s AND docstatus = 1
		GROUP BY due_date
		""",
        (loan_name,),
        as_dict=1,
    )

    paid_by_date = {}
    for row in paid_data:
        paid_by_date[getdate(row.due_date)] = {
            "paid_prin": flt(row.paid_prin, precision),
            "paid_int": flt(row.paid_int, precision),
        }

    for sch in schedule_rows:
        sch_date = getdate(sch.payment_date)
        if sch_date > as_of_date:
            break

        paid = paid_by_date.get(sch_date, {"paid_prin": 0, "paid_int": 0})
        total_due = flt(sch.principal_amount, precision) + flt(
            sch.interest_amount, precision
        )
        total_paid = flt(paid["paid_prin"], precision) + flt(
            paid["paid_int"], precision
        )

        if flt(total_paid, precision) < flt(total_due, precision):
            return max(date_diff(as_of_date, sch_date), 0)

    return 0


def build_summary(bucket_totals, total_outstanding, precision):
    summary = []
    total_at_risk = sum(bucket_totals[b]["at_risk"] for b in bucket_totals)

    summary.append(
        {
            "value": flt(total_outstanding, precision),
            "label": _("Total Outstanding"),
            "datatype": "Currency",
            "indicator": "blue",
        }
    )

    par_pct = (
        flt(total_at_risk / total_outstanding * 100, 2) if total_outstanding else 0
    )
    summary.append(
        {
            "value": flt(total_at_risk, precision),
            "label": _("Total At Risk ({0}%)").format(par_pct),
            "datatype": "Currency",
            "indicator": "red" if par_pct > 5 else "orange" if par_pct > 2 else "green",
        }
    )

    par30 = sum(
        bucket_totals[b]["at_risk"]
        for b in bucket_totals
        if b not in ("Current", "PAR 1-30")
    )
    par30_pct = flt(par30 / total_outstanding * 100, 2) if total_outstanding else 0
    summary.append(
        {
            "value": par30_pct,
            "label": _("PAR > 30"),
            "datatype": "Percent",
            "indicator": "red"
            if par30_pct > 5
            else "orange"
            if par30_pct > 2
            else "green",
        }
    )

    par90 = sum(
        bucket_totals[b]["at_risk"]
        for b in bucket_totals
        if b in ("PAR 91-180", "PAR 180+")
    )
    par90_pct = flt(par90 / total_outstanding * 100, 2) if total_outstanding else 0
    summary.append(
        {
            "value": par90_pct,
            "label": _("PAR > 90"),
            "datatype": "Percent",
            "indicator": "red"
            if par90_pct > 3
            else "orange"
            if par90_pct > 1
            else "green",
        }
    )

    total_loans = sum(bucket_totals[b]["count"] for b in bucket_totals)
    overdue_loans = sum(
        bucket_totals[b]["count"] for b in bucket_totals if b != "Current"
    )
    summary.append(
        {
            "value": total_loans,
            "label": _("Active Loans ({0} overdue)").format(overdue_loans),
            "datatype": "Int",
            "indicator": "blue",
        }
    )

    return summary


def build_chart(bucket_totals, total_outstanding, precision):
    labels = []
    outstanding_values = []
    at_risk_values = []

    for label in [
        "Current",
        "PAR 1-30",
        "PAR 31-60",
        "PAR 61-90",
        "PAR 91-180",
        "PAR 180+",
    ]:
        bt = bucket_totals.get(label, {})
        labels.append(label)
        outstanding_values.append(flt(bt.get("outstanding", 0), precision))
        at_risk_values.append(flt(bt.get("at_risk", 0), precision))

    return {
        "data": {
            "labels": labels,
            "datasets": [
                {"name": _("Outstanding Principal"), "values": outstanding_values},
                {"name": _("At-Risk Principal"), "values": at_risk_values},
            ],
        },
        "type": "bar",
        "colors": ["#318AD8", "#E24B4A"],
        "barOptions": {"stacked": 0},
    }
