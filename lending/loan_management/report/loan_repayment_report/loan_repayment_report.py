import frappe
from frappe.utils import flt, getdate, today


def execute(filters=None):
    if not filters or not filters.get("loan"):
        return [], []

    columns = get_columns()
    data = get_data(filters)

    return columns, data


def get_columns():
    return [
        {"fieldname": "date", "label": "Date", "fieldtype": "Date", "width": 100},
        {
            "fieldname": "description",
            "label": "Description",
            "fieldtype": "Data",
            "width": 150,
        },
        {
            "fieldname": "principal",
            "label": "Principal",
            "fieldtype": "Currency",
            "width": 110,
        },
        {
            "fieldname": "interest",
            "label": "Interest",
            "fieldtype": "Currency",
            "width": 110,
        },
        {"fieldname": "fees", "label": "Fees", "fieldtype": "Currency", "width": 90},
        {
            "fieldname": "penalty",
            "label": "Penalty",
            "fieldtype": "Currency",
            "width": 90,
        },
        {"fieldname": "due", "label": "Due", "fieldtype": "Currency", "width": 110},
        {"fieldname": "paid", "label": "Paid", "fieldtype": "Currency", "width": 110},
        {
            "fieldname": "pending_due",
            "label": "Pending Due",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "total_due",
            "label": "Total Due",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "principal_due",
            "label": "Principal Due",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "principal_balance",
            "label": "Principal Balance",
            "fieldtype": "Currency",
            "width": 130,
        },
    ]


def get_data(filters):
    loan_doc = frappe.get_doc("Loan", filters.get("loan"))
    repayment_id = frappe.db.get_value(
        "Loan Repayment Schedule",
        {
            "loan": loan_doc.name,
        },
        "name",
    )

    events = []
    schedules = frappe.db.get_all(
        "Repayment Schedule",
        filters={"parent": repayment_id},
        fields=[
            "idx",
            "payment_date",
            "principal_amount",
            "interest_amount",
            "total_payment",
        ],
        order_by="payment_date asc",
    )

    for sch in schedules:
        desc = "Maturity" if sch.idx == len(schedules) else "Repayment"
        events.append(
            {
                "type": "schedule",
                "idx_col": sch.idx,
                "date": sch.payment_date,
                "description": desc,
                "principal": flt(sch.principal_amount),
                "interest": flt(sch.interest_amount),
                "fees": 0.0,
                "penalty": 0.0,
                "due": flt(sch.total_payment),
                "paid": 0.0,
            }
        )

    repayments = frappe.get_all(
        "Loan Repayment",
        filters={"against_loan": loan_doc.name, "docstatus": 1},
        fields=[
            "posting_date",
            "amount_paid",
            "principal_amount_paid",
            "penalty_amount",
            "total_paid_charges",
        ],
        order_by="posting_date asc",
    )

    for rep in repayments:
        events.append(
            {
                "type": "payment",
                "idx_col": "",
                "date": getdate(rep.posting_date),
                "description": "Payment Received",
                "principal": 0.0,
                "interest": 0.0,
                "fees": flt(rep.total_paid_charges),
                "penalty": flt(rep.penalty_amount),
                "due": 0.0,
                "paid": flt(rep.amount_paid),
                "principal_paid": flt(rep.principal_amount_paid),
            }
        )

    current_date = getdate(today())
    events.append(
        {
            "type": "today",
            "idx_col": "",
            "date": current_date,
            "description": "Today",
            "principal": 0.0,
            "interest": 0.0,
            "fees": 0.0,
            "penalty": 0.0,
            "due": 0.0,
            "paid": 0.0,
        }
    )

    events.sort(
        key=lambda e: (
            getdate(e["date"]),
            0 if e["type"] == "payment" else (1 if e["type"] == "today" else 2),
        )
    )

    data = []
    running_pending_due = 0.0
    running_total_due = 0.0
    running_principal_due = 0.0
    principal_balance = flt(loan_doc.loan_amount)

    tot_principal = 0.0
    tot_interest = 0.0
    tot_due = 0.0
    tot_paid = 0.0

    data.append(
        {
            "idx_col": "",
            "date": "",
            "description": "Opening Balance",
            "principal_balance": principal_balance,
        }
    )

    for e in events:
        if e["type"] == "schedule":
            running_pending_due += e["due"]
            running_total_due += e["due"]
            running_principal_due += e["principal"]

            # As per image logic: if schedule is future, reduce principal balance
            if getdate(e["date"]) > current_date:
                principal_balance -= e["principal"]

            tot_principal += e["principal"]
            tot_interest += e["interest"]
            tot_due += e["due"]

        elif e["type"] == "payment":
            running_pending_due -= e["paid"]
            running_principal_due -= e.get("principal_paid", 0.0)

            # As per image logic: if payment is past/today, reduce principal balance by actual paid principal
            if getdate(e["date"]) <= current_date:
                principal_balance -= e.get("principal_paid", 0.0)
            tot_paid += e["paid"]

        row = {
            "idx_col": e["idx_col"],
            "date": e["date"],
            "description": f"<b>{e['description']}</b>"
            if e["type"] in ["today", "payment"]
            else e["description"],
            "principal": e["principal"] if e["principal"] else "",
            "interest": e["interest"] if e["interest"] else "",
            "fees": e["fees"] if e["fees"] else "",
            "penalty": e["penalty"] if e["penalty"] else "",
            "due": e["due"] if e["due"] else "",
            "paid": e["paid"] if e["paid"] else "",
            "pending_due": running_pending_due if e["type"] != "today" else "",
            "total_due": running_total_due if e["type"] == "schedule" else "",
            "principal_due": running_principal_due if e["type"] == "schedule" else "",
            "principal_balance": principal_balance if e["type"] != "payment" else "",
        }
        data.append(row)

    # 6. Append Summary Rows at the bottom
    data.append({})  # Empty separator
    data.append(
        {
            "description": "<b>Total Due</b>",
            "principal": tot_principal,
            "interest": tot_interest,
            "due": "<b>" + str(tot_due) + "</b>",
            "paid": "<b>" + str(tot_paid) + "</b>",
            "pending_due": "<b>" + str(running_pending_due) + "</b>",
            "principal_due": "<b>" + str(running_principal_due) + "</b>",
        }
    )

    data.append(
        {
            "description": "<b>Total Paid</b>",
            "principal": sum(
                flt(p.get("principal_amount_paid", 0)) for p in repayments
            ),
            "interest": sum(
                flt(p.get("amount_paid", 0)) - flt(p.get("principal_amount_paid", 0))
                for p in repayments
            ),
        }
    )

    data.append(
        {
            "description": "<b>Total Pending Due</b>",
            "principal": running_principal_due,
            "interest": running_total_due - tot_paid - running_principal_due,
        }
    )

    return data
