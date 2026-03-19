import frappe
from frappe import _
from frappe.utils import flt, getdate, formatdate


def execute(filters=None):
    if not filters or not filters.get("customer"):
        return [], []

    customer = filters.get("customer")
    loan_filter = filters.get("loan")

    columns = get_columns()
    custom_data = get_customer_loan_data(customer, loan_filter)

    flat_data = []
    for loan_data in custom_data:
        for tx in loan_data["transactions"]:
            flat_data.append(
                {
                    "loan": loan_data["loan_details"]["name"],
                    "date": tx["date"],
                    "description": tx["description"],
                    "debit": tx["debit"],
                    "credit": tx["credit"],
                    "balance": tx["balance"],
                }
            )

    return columns, flat_data, None, None, None, custom_data


def get_columns():
    return [
        {
            "fieldname": "loan",
            "label": _("Loan #"),
            "fieldtype": "Link",
            "options": "Loan",
            "width": 150,
        },
        {"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 120},
        {
            "fieldname": "description",
            "label": _("Description"),
            "fieldtype": "Data",
            "width": 300,
        },
        {
            "fieldname": "debit",
            "label": _("Debit"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "credit",
            "label": _("Credit"),
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "fieldname": "balance",
            "label": _("Balance"),
            "fieldtype": "Currency",
            "width": 130,
        },
    ]


def get_customer_loan_data(customer, loan_filter=None):
    customer_doc = frappe.get_doc("Customer", customer)

    loan_filters = {"applicant": customer, "docstatus": 1}
    if loan_filter:
        loan_filters["name"] = loan_filter

    active_loans = frappe.get_all(
        "Loan",
        filters=loan_filters,
        fields=["name"],
        order_by="disbursement_date asc",
    )

    all_loans_data = []

    for i in active_loans:
        loan = frappe.get_doc("Loan", i.name)
        penalty_rate = flt(
            frappe.db.get_value(
                "Loan Product", loan.loan_product, "penalty_interest_rate"
            )
        )

        repayments = frappe.get_all(
            "Loan Repayment",
            filters={"against_loan": loan.name, "docstatus": 1},
            fields=["posting_date", "amount_paid", "owner", "name"],
            order_by="posting_date asc",
        )

        # Get collector name from the owner
        repayment_rows = []
        for rep in repayments:
            collector_name = (
                frappe.db.get_value("User", rep.owner, "full_name") or rep.owner
            )
            repayment_rows.append(
                {
                    "loan": loan.name,
                    "borrower_name": customer_doc.customer_name,
                    "repayment_method": "Cash",
                    "collected_by": collector_name,
                    "collection_date": rep.posting_date,
                    "total_paid_amount": flt(rep.amount_paid),
                }
            )

        transactions = []
        schedule_doc = frappe.db.get_value(
            "Loan Repayment Schedule", {"loan": loan.name, "docstatus": 1}, "name"
        )

        sch_rows = []
        if schedule_doc:
            schedule = frappe.get_doc("Loan Repayment Schedule", schedule_doc)
            sch_rows = schedule.get("repayment_schedule", [])

            for index, sch in enumerate(sch_rows):
                is_last_row = index == len(sch_rows) - 1
                description = "Maturity" if is_last_row else "Repayment"

                transactions.append(
                    {
                        "date": sch.payment_date,
                        "description": description,
                        "debit": flt(sch.total_payment),
                        "credit": 0.0,
                        "sort_order": 1,
                    }
                )

        penalties = frappe.get_all(
            "Loan Interest Accrual",
            filters={"loan": loan.name, "docstatus": 1, "penalty_amount": (">", 0)},
            fields=["posting_date", "penalty_amount", "pending_principal_amount"],
            order_by="posting_date asc",
        )
        for pen in penalties:
            principal_formatted = "{:,.2f}".format(flt(pen.pending_principal_amount))
            # Display the penalty rate as a percentage (e.g., 0.4%)
            penalty_rate_display = (
                "{:.1f}".format(flt(penalty_rate) * 100)
                if penalty_rate < 1
                else "{:.1f}".format(flt(penalty_rate))
            )
            dynamic_desc = f"System Generated Penalty {penalty_rate_display}% of {principal_formatted}"

            transactions.append(
                {
                    "date": pen.posting_date,
                    "description": dynamic_desc,
                    "debit": flt(pen.penalty_amount),
                    "credit": 0.0,
                    "sort_order": 2,
                }
            )

        for rep in repayments:
            transactions.append(
                {
                    "date": rep.posting_date,
                    "description": "Payment Received",
                    "debit": 0.0,
                    "credit": flt(rep.amount_paid),
                    "sort_order": 3,
                }
            )

        transactions = sorted(
            transactions, key=lambda k: (getdate(k["date"]), k["sort_order"])
        )

        running_bal = 0.0
        for tx in transactions:
            running_bal += tx["debit"] - tx["credit"]
            tx["balance"] = running_bal

        total_penalty = sum(flt(p.penalty_amount) for p in penalties)
        total_due = flt(loan.total_payment) + total_penalty
        balance_amount = total_due - flt(loan.total_amount_paid)

        maturity_date = None
        if sch_rows:
            maturity_date = sch_rows[-1].payment_date

        # Get company/branch info
        company_doc = frappe.get_doc("Company", loan.company)

        all_loans_data.append(
            {
                "loan_details": {
                    "name": loan.name,
                    "disbursement_date": loan.disbursement_date,
                    "maturity_date": maturity_date,
                    "repayment_cycle": "Monthly",
                    "principal_amount": loan.loan_amount,
                    "interest_rate": loan.rate_of_interest,
                    "interest_amount": loan.total_interest_payable,
                    "penalty_amount": total_penalty,
                    "total_due": total_due,
                    "paid_amount": loan.total_amount_paid,
                    "balance_amount": balance_amount,
                },
                "customer_details": {
                    "name": customer_doc.customer_name,
                    "mobile": customer_doc.get("mobile_no") or "",
                    "cnic": customer_doc.get("custom_cnic") or "",
                    "address": customer_doc.get("primary_address") or "",
                },
                "company_details": {
                    "name": company_doc.company_name,
                    "branch": loan.get("branch") or "",
                },
                "repayment_rows": repayment_rows,
                "transactions": transactions,
                "total_balance": running_bal,
            }
        )

    return all_loans_data
