# Copyright (c) 2026, UnityAlgo
# For license information, please see license.txt
import frappe
from frappe.utils import flt, getdate, get_first_day, get_last_day


def execute(filters=None):
    filters = filters or {}
    columns = get_columns(filters)
    data = get_data(filters)
    return columns, data


def get_columns(filters):
    period_type = filters.get("period_type", "Daily")
    label = "Date" if period_type == "Daily" else "Month"

    return [
        {
            "label": label,
            "fieldname": "period",
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "label": "Disbursements #",
            "fieldname": "disb_count",
            "fieldtype": "Int",
            "width": 130,
        },
        {
            "label": "Amount Disbursed",
            "fieldname": "disb_amount",
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "label": "Fees Collected",
            "fieldname": "fees_amount",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": "Net Cash Released",
            "fieldname": "net_cash",
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "label": "Collections #",
            "fieldname": "rep_count",
            "fieldtype": "Int",
            "width": 120,
        },
        {
            "label": "Total Collected",
            "fieldname": "rep_amount",
            "fieldtype": "Currency",
            "width": 150,
        },
        {
            "label": "Principal Collected",
            "fieldname": "principal_collected",
            "fieldtype": "Currency",
            "width": 160,
        },
        {
            "label": "Interest Collected",
            "fieldname": "interest_collected",
            "fieldtype": "Currency",
            "width": 155,
        },
        {
            "label": "Penalty Collected",
            "fieldname": "penalty_collected",
            "fieldtype": "Currency",
            "width": 150,
        },
    ]


def get_data(filters):
    period_type = filters.get("period_type", "Daily")
    from_date = filters.get("from_date")
    to_date = filters.get("to_date")
    branch = filters.get("branch")
    loan_product = filters.get("loan_product")
    sales_person = filters.get("sales_person")

    if not from_date or not to_date:
        frappe.throw("Please set From Date and To Date")

    # ── Build WHERE conditions ────────────────────────────────────────
    disb_where = [
        "ld.docstatus = 1",
        "ld.disbursement_date BETWEEN %(from_date)s AND %(to_date)s",
    ]
    rep_where = [
        "lr.docstatus = 1",
        "DATE(lr.posting_date) BETWEEN %(from_date)s AND %(to_date)s",
    ]
    params = {"from_date": from_date, "to_date": to_date}

    if branch:
        disb_where.append("l.branch = %(branch)s")
        rep_where.append("l.branch = %(branch)s")
        params["branch"] = branch

    if sales_person:
        disb_where.append("l.sales_person = %(sales_person)s")
        rep_where.append("l.sales_person = %(sales_person)s")
        params["sales_person"] = sales_person

    if loan_product:
        disb_where.append("l.loan_product = %(loan_product)s")
        rep_where.append("l.loan_product = %(loan_product)s")
        params["loan_product"] = loan_product

    # ── Group by format ───────────────────────────────────────────────
    if period_type == "Daily":
        disb_group = "DATE(ld.disbursement_date)"
        disb_label = "DATE_FORMAT(ld.disbursement_date, '%%d/%%m/%%Y')"
        rep_group = "DATE(lr.posting_date)"
        rep_label = "DATE_FORMAT(lr.posting_date, '%%d/%%m/%%Y')"
        sort_key = "DATE(ld.disbursement_date)"
        rep_sort = "DATE(lr.posting_date)"
    else:
        disb_group = "DATE_FORMAT(ld.disbursement_date, '%%Y-%%m')"
        disb_label = "DATE_FORMAT(ld.disbursement_date, '%%b %%Y')"
        rep_group = "DATE_FORMAT(lr.posting_date, '%%Y-%%m')"
        rep_label = "DATE_FORMAT(lr.posting_date, '%%b %%Y')"
        sort_key = "DATE_FORMAT(ld.disbursement_date, '%%Y-%%m')"
        rep_sort = "DATE_FORMAT(lr.posting_date, '%%Y-%%m')"

    # ── Disbursement query ────────────────────────────────────────────
    disb_sql = f"""
		SELECT
			{disb_group}                          AS period_key,
			{disb_label}                          AS period,
			COUNT(DISTINCT ld.name)               AS disb_count,
			ROUND(SUM(ld.disbursed_amount), 2)    AS disb_amount,
			ROUND(SUM(
				IFNULL((
					SELECT SUM(ldc.amount)
					FROM `tabLoan Disbursement Charge` ldc
					WHERE ldc.parent = ld.name
				), 0)
			), 2)                                  AS fees_amount
		FROM `tabLoan Disbursement` ld
		INNER JOIN `tabLoan` l ON l.name = ld.against_loan
		WHERE {" AND ".join(disb_where)}
		GROUP BY {disb_group}
		ORDER BY {sort_key}
	"""

    # ── Repayment query ───────────────────────────────────────────────
    rep_sql = f"""
		SELECT
			{rep_group}                                    AS period_key,
			{rep_label}                                    AS period,
			COUNT(lr.name)                                 AS rep_count,
			ROUND(SUM(lr.amount_paid), 2)                  AS rep_amount,
			ROUND(SUM(lr.principal_amount_paid), 2)        AS principal_collected,
			ROUND(SUM(lr.total_interest_paid), 2)          AS interest_collected,
			ROUND(SUM(IFNULL(lr.total_penalty_paid, 0)), 2) AS penalty_collected
		FROM `tabLoan Repayment` lr
		INNER JOIN `tabLoan` l ON l.name = lr.against_loan
		WHERE {" AND ".join(rep_where)}
		GROUP BY {rep_group}
		ORDER BY {rep_sort}
	"""

    disb_rows = frappe.db.sql(disb_sql, params, as_dict=True)
    rep_rows = frappe.db.sql(rep_sql, params, as_dict=True)

    # ── Merge by period_key ───────────────────────────────────────────
    disb_map = {r.period_key: r for r in disb_rows}
    rep_map = {r.period_key: r for r in rep_rows}

    all_keys = sorted(set(list(disb_map.keys()) + list(rep_map.keys())))

    data = []
    total = {
        "disb_count": 0,
        "disb_amount": 0,
        "fees_amount": 0,
        "net_cash": 0,
        "rep_count": 0,
        "rep_amount": 0,
        "principal_collected": 0,
        "interest_collected": 0,
        "penalty_collected": 0,
    }

    for key in all_keys:
        d = disb_map.get(key, {})
        r = rep_map.get(key, {})

        period_label = d.get("period") or r.get("period") or key
        disb_amount = flt(d.get("disb_amount"))
        fees_amount = flt(d.get("fees_amount"))
        net_cash = disb_amount - fees_amount

        row = {
            "period": period_label,
            "disb_count": int(d.get("disb_count") or 0),
            "disb_amount": disb_amount,
            "fees_amount": fees_amount,
            "net_cash": net_cash,
            "rep_count": int(r.get("rep_count") or 0),
            "rep_amount": flt(r.get("rep_amount")),
            "principal_collected": flt(r.get("principal_collected")),
            "interest_collected": flt(r.get("interest_collected")),
            "penalty_collected": flt(r.get("penalty_collected")),
        }
        data.append(row)

        for k in total:
            total[k] += row.get(k, 0)

    # ── Totals row ────────────────────────────────────────────────────
    if data:
        data.append(
            {
                "period": "── TOTAL ──",
                "disb_count": total["disb_count"],
                "disb_amount": total["disb_amount"],
                "fees_amount": total["fees_amount"],
                "net_cash": total["net_cash"],
                "rep_count": total["rep_count"],
                "rep_amount": total["rep_amount"],
                "principal_collected": total["principal_collected"],
                "interest_collected": total["interest_collected"],
                "penalty_collected": total["penalty_collected"],
            }
        )

    return data
