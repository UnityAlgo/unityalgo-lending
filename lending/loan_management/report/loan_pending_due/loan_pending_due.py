# Copyright (c) 2026, UnityAlgo and contributors
# For license information, please see license.txt

import re
from collections import defaultdict

import frappe
from frappe.utils import date_diff, flt, getdate, nowdate


def execute(filters=None):
    filters = filters or {}
    from_date = filters.get("from_date") or "2020-06-01"
    to_date = filters.get("to_date") or nowdate()
    branches = filters.get("branch") or []
    status_f = filters.get("status") or []

    if isinstance(branches, str):
        branches = [b.strip() for b in branches.split(",") if b.strip()]
    if isinstance(status_f, str):
        status_f = [s.strip() for s in status_f.split(",") if s.strip()]

    # Use to_date as the report reference date throughout — not today.
    # All "next installment", "past due", "days past" calculations are
    # relative to to_date so the report is consistent when run for any
    # historical or future date range.
    report_date = getdate(to_date)

    columns = [
        {
            "label": "Name",
            "fieldname": "borrower_name",
            "fieldtype": "Data",
            "width": 160,
        },
        {"label": "Mobile", "fieldname": "mobile", "fieldtype": "Data", "width": 120},
        {"label": "Loan#", "fieldname": "loan_ref", "fieldtype": "Data", "width": 160},
        {
            "label": "Borrower#",
            "fieldname": "borrower_no",
            "fieldtype": "Data",
            "width": 110,
        },
        {"label": "Branch", "fieldname": "branch", "fieldtype": "Data", "width": 130},
        {
            "label": "Released",
            "fieldname": "released",
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "label": "Principal",
            "fieldname": "principal",
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "label": "Interest",
            "fieldname": "interest",
            "fieldtype": "Currency",
            "width": 110,
        },
        {"label": "Product", "fieldname": "product", "fieldtype": "Data", "width": 130},
        {
            "label": "Principal Paid",
            "fieldname": "principal_paid",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": "Interest Paid",
            "fieldname": "interest_paid",
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "label": "Penalty Paid",
            "fieldname": "penalty_paid",
            "fieldtype": "Currency",
            "width": 110,
        },
        {"label": "Paid", "fieldname": "paid", "fieldtype": "Currency", "width": 110},
        {
            "label": "Penalty",
            "fieldname": "penalty",
            "fieldtype": "Currency",
            "width": 90,
        },
        {
            "label": "Past Due",
            "fieldname": "past_due",
            "fieldtype": "Int",
            "width": 100,
        },
        {
            "label": "Next Installment",
            "fieldname": "next_installment",
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "label": "Pending Due",
            "fieldname": "pending_due",
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "label": "Last Payment",
            "fieldname": "last_payment",
            "fieldtype": "Data",
            "width": 190,
        },
        {
            "label": "Principal Balance",
            "fieldname": "principal_balance",
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "label": "Interest Balance",
            "fieldname": "interest_balance",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": "Penalty Balance",
            "fieldname": "penalty_balance",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": "Balance",
            "fieldname": "balance",
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "label": "Maturity",
            "fieldname": "maturity",
            "fieldtype": "Date",
            "width": 100,
        },
        {
            "label": "Loan Officer",
            "fieldname": "loan_officer",
            "fieldtype": "Data",
            "width": 140,
        },
        {
            "label": "Days Past",
            "fieldname": "days_past",
            "fieldtype": "Int",
            "width": 90,
        },
        {
            "label": "Days To Maturity",
            "fieldname": "days_to_maturity",
            "fieldtype": "Int",
            "width": 130,
        },
        {
            "label": "Next Due",
            "fieldname": "next_due",
            "fieldtype": "Date",
            "width": 100,
        },
    ]

    branch_cond = ""
    branch_vals = ()
    if branches:
        ph = ",".join(["%s"] * len(branches))
        branch_cond = f" AND l.branch IN ({ph})"
        branch_vals = tuple(branches)

    status_cond = ""
    status_vals = ()
    if status_f:
        ph = ",".join(["%s"] * len(status_f))
        status_cond = f" AND l.status IN ({ph})"
        status_vals = tuple(status_f)

    # ── 1. Loans ──────────────────────────────────────────────────────────────
    loans = frappe.db.sql(
        f"""
        SELECT
            l.name,
            l.applicant,
            l.branch,
            l.loan_product,
            l.disbursement_date,
            l.disbursed_amount,
            l.loan_amount,
            l.total_principal_paid,
            l.total_interest_payable,
            l.total_payment,
            l.sales_person,
            l.status,
            l.description,
            c.customer_name,
            c.custom_phone_no_1  AS mobile,
            c.custom_cnic_no     AS cnic
        FROM `tabLoan` l
        LEFT JOIN `tabCustomer` c ON c.name = l.applicant
        WHERE l.docstatus = 1
          AND l.status IN ('Disbursed', 'Write Off', 'Loan Closure Requested')
          AND l.disbursement_date >= %s
          AND l.disbursement_date <= %s
        {branch_cond}
        {status_cond}
        ORDER BY l.branch, c.customer_name
        """,
        (from_date, to_date) + branch_vals + status_vals,
        as_dict=True,
    )

    if not loans:
        return columns, []

    loan_names = [l.name for l in loans]
    ph = ",".join(["%s"] * len(loan_names))

    # ── 2. Total scheduled interest per loan ──────────────────────────────────
    interest_rows = frappe.db.sql(
        f"""
        SELECT lrs.loan, COALESCE(SUM(rs.interest_amount), 0) AS total_interest
        FROM `tabRepayment Schedule` rs
        INNER JOIN `tabLoan Repayment Schedule` lrs ON lrs.name = rs.parent
        WHERE lrs.loan IN ({ph})
          AND lrs.docstatus = 1
          AND lrs.status = 'Active'
        GROUP BY lrs.loan
        """,
        tuple(loan_names),
        as_dict=True,
    )
    interest_map = {r.loan: flt(r.total_interest) for r in interest_rows}

    # ── 3. Outstanding penalty per loan ───────────────────────────────────────
    penalty_rows = frappe.db.sql(
        f"""
        SELECT loan,
               COALESCE(SUM(penalty_amount - COALESCE(paid_interest_amount, 0)), 0) AS penalty
        FROM `tabLoan Interest Accrual`
        WHERE loan IN ({ph})
          AND docstatus = 1
          AND accrual_type = 'Penalty'
          AND penalty_amount > COALESCE(paid_interest_amount, 0)
        GROUP BY loan
        """,
        tuple(loan_names),
        as_dict=True,
    )
    penalty_map = {r.loan: max(flt(r.penalty), 0) for r in penalty_rows}

    # ── 4. Repayment totals per loan ──────────────────────────────────────────
    rep_rows = frappe.db.sql(
        f"""
        SELECT
            against_loan,
            COALESCE(SUM(principal_amount_paid), 0) AS p,
            COALESCE(SUM(total_interest_paid),   0) AS i,
            COALESCE(SUM(total_penalty_paid),    0) AS n,
            COALESCE(SUM(amount_paid),           0) AS a,
            MAX(posting_date)                        AS last_date
        FROM `tabLoan Repayment`
        WHERE against_loan IN ({ph}) AND docstatus = 1
        GROUP BY against_loan
        """,
        tuple(loan_names),
        as_dict=True,
    )
    rep_map = {r.against_loan: r for r in rep_rows}

    # ── 5. Last repayment amount ───────────────────────────────────────────────
    last_amt_rows = frappe.db.sql(
        f"""
        SELECT r1.against_loan, r1.amount_paid, r1.posting_date
        FROM `tabLoan Repayment` r1
        INNER JOIN (
            SELECT against_loan, MAX(posting_date) AS max_date
            FROM `tabLoan Repayment`
            WHERE against_loan IN ({ph}) AND docstatus = 1
            GROUP BY against_loan
        ) r2 ON r2.against_loan = r1.against_loan
             AND r1.posting_date = r2.max_date
        WHERE r1.against_loan IN ({ph}) AND r1.docstatus = 1
        """,
        tuple(loan_names) * 2,
        as_dict=True,
    )
    last_amt_map = {}
    for r in last_amt_rows:
        if r.against_loan not in last_amt_map:
            last_amt_map[r.against_loan] = flt(r.amount_paid)

    # ── 6. Next unaccrued installment as of report_date ───────────────────────
    # FIX 1: use report_date (to_date) not today — so the report correctly
    #         shows the next due installment as of the selected date range end.
    # FIX 2: added lrs.status = 'Active' to exclude superseded schedules
    #         from restructured loans.
    next_sched_rows = frappe.db.sql(
        f"""
        SELECT t.loan, t.payment_date, t.total_payment
        FROM (
            SELECT lrs.loan, rs.payment_date, rs.total_payment,
                   ROW_NUMBER() OVER (
                       PARTITION BY lrs.loan
                       ORDER BY rs.payment_date ASC
                   ) AS rn
            FROM `tabRepayment Schedule` rs
            INNER JOIN `tabLoan Repayment Schedule` lrs ON lrs.name = rs.parent
            WHERE lrs.loan IN ({ph})
              AND lrs.docstatus = 1
              AND lrs.status = 'Active'
              AND rs.is_accrued = 0
              AND rs.payment_date >= %s
        ) t
        WHERE t.rn = 1
        """,
        tuple(loan_names) + (str(report_date),),
        as_dict=True,
    )
    next_sched_map = {r.loan: r for r in next_sched_rows}

    # ── 7. Maturity date per loan ─────────────────────────────────────────────
    maturity_rows = frappe.db.sql(
        f"""
        SELECT lrs.loan, MAX(rs.payment_date) AS maturity
        FROM `tabRepayment Schedule` rs
        INNER JOIN `tabLoan Repayment Schedule` lrs ON lrs.name = rs.parent
        WHERE lrs.loan IN ({ph})
          AND lrs.docstatus = 1
          AND lrs.status = 'Active'
        GROUP BY lrs.loan
        """,
        tuple(loan_names),
        as_dict=True,
    )
    maturity_map = {r.loan: r.maturity for r in maturity_rows}

    # ── 8. Build report rows ──────────────────────────────────────────────────
    data = []

    for l in loans:
        loan_name = l.name

        m = re.search(r"LoanRef:([^\s,]+)", l.description or "")
        loan_ref = m.group(1).strip() if m else loan_name
        borrower_no = loan_ref.rsplit("-", 1)[0] if "-" in loan_ref else loan_ref

        total_interest = round(interest_map.get(loan_name, 0), 0)
        penalty = round(penalty_map.get(loan_name, 0), 0)

        rep = rep_map.get(loan_name)
        principal_paid = round(flt(rep.p if rep else 0), 0)
        interest_paid = round(flt(rep.i if rep else 0), 0)
        penalty_paid = round(flt(rep.n if rep else 0), 0)
        total_paid = round(flt(rep.a if rep else 0), 0)
        last_date = rep.last_date if rep else None
        last_amt = last_amt_map.get(loan_name, 0)

        last_payment = ""
        if last_date:
            last_payment = (
                f"{getdate(last_date).strftime('%d/%m/%Y')} "
                f"{frappe.format_value(last_amt, {'fieldtype': 'Currency'})}"
            )

        principal_balance = round(flt(l.disbursed_amount) - principal_paid, 0)
        interest_balance = round(total_interest - interest_paid, 0)
        penalty_balance = penalty
        balance = round(principal_balance + interest_balance + penalty_balance, 0)

        # Skip fully paid loans
        if balance <= 0:
            continue

        next_s = next_sched_map.get(loan_name)
        next_installment = round(flt(next_s.total_payment), 0) if next_s else 0
        next_due_date = next_s.payment_date if next_s else None

        maturity = maturity_map.get(loan_name)
        days_to_maturity = date_diff(maturity, report_date) if maturity else 0

        # FIX 3: past_due and days_past use report_date not today.
        # A negative value means the next installment is in the future
        # relative to to_date — not yet due, so 0.
        if next_due_date:
            past_due = date_diff(report_date, getdate(next_due_date))
        else:
            past_due = 0

        days_past = max(past_due, 0)

        # FIX 4: pending_due is the outstanding installment amount only.
        # The old formula added days (int) to currency which was meaningless.
        # Pending due = next installment amount if it is overdue as of
        # report_date, otherwise 0.
        pending_due = next_installment if past_due > 0 else 0

        data.append(
            {
                "borrower_name": l.customer_name or l.applicant,
                "mobile": l.mobile or "",
                "loan_ref": loan_ref,
                "borrower_no": borrower_no,
                "branch": l.branch,
                "released": l.disbursement_date,
                "principal": round(flt(l.disbursed_amount), 0),
                "interest": total_interest,
                "product": l.loan_product,
                "principal_paid": principal_paid,
                "interest_paid": interest_paid,
                "penalty_paid": penalty_paid,
                "paid": total_paid,
                "penalty": penalty,
                "past_due": past_due,
                "next_installment": next_installment,
                "pending_due": pending_due,
                "last_payment": last_payment,
                "principal_balance": principal_balance,
                "interest_balance": interest_balance,
                "penalty_balance": penalty_balance,
                "balance": balance,
                "maturity": maturity,
                "loan_officer": l.sales_person or "",
                "days_past": days_past,
                "days_to_maturity": days_to_maturity,
                "next_due": next_due_date,
                "status": "",
            }
        )

    # ── 9. Totals row ─────────────────────────────────────────────────────────
    if data:
        tot = defaultdict(float)
        for row in data:
            for k in [
                "principal",
                "interest",
                "principal_paid",
                "interest_paid",
                "penalty_paid",
                "paid",
                "penalty",
                "pending_due",
                "principal_balance",
                "interest_balance",
                "penalty_balance",
                "balance",
            ]:
                tot[k] += flt(row[k])
            if flt(row["past_due"]) > 0:
                tot["past_due"] += flt(row["past_due"])

        data.append(
            {
                "borrower_name": "TOTAL",
                "mobile": "",
                "loan_ref": "",
                "borrower_no": "",
                "branch": "",
                "released": None,
                "principal": round(tot["principal"], 0),
                "interest": round(tot["interest"], 0),
                "product": "",
                "principal_paid": round(tot["principal_paid"], 0),
                "interest_paid": round(tot["interest_paid"], 0),
                "penalty_paid": round(tot["penalty_paid"], 0),
                "paid": round(tot["paid"], 0),
                "penalty": round(tot["penalty"], 0),
                "past_due": round(tot["past_due"], 0),
                "next_installment": None,
                "pending_due": round(tot["pending_due"], 0),
                "last_payment": "",
                "principal_balance": round(tot["principal_balance"], 0),
                "interest_balance": round(tot["interest_balance"], 0),
                "penalty_balance": round(tot["penalty_balance"], 0),
                "balance": round(tot["balance"], 0),
                "maturity": None,
                "loan_officer": "",
                "days_past": None,
                "days_to_maturity": None,
                "next_due": None,
                "status": "",
            }
        )

    return columns, data
