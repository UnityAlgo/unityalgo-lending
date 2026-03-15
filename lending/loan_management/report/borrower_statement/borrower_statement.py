import frappe
from frappe import _
from frappe.utils import flt, getdate


def execute(filters=None):
    if not filters or not filters.get("customer"):
        return [], []

    customer = filters.get("customer")
    columns = get_columns()

    # 1. Fetch structured data for the HTML Print Format
    custom_data = get_customer_loan_data(customer)

    # 2. Flatten the data for the standard ERPNext Script Report Grid
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

    # Return BOTH the flat data for the grid and the custom_data for the HTML view
    return columns, flat_data, custom_data, None


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


def get_customer_loan_data(customer):
    customer_doc = frappe.get_doc("Customer", customer)
    active_loans = frappe.get_all(
        "Loan", filters={"applicant": customer, "docstatus": 1}
    )

    all_loans_data = []

    for i in active_loans:
        loan = frappe.get_doc("Loan", i.name)
        loan_product = frappe.db.get_value(
            "Loan Product", loan.loan_product, "penalty_interest_rate"
        )
        penalty_rate = flt(loan_product)

        repayments = frappe.get_all(
            "Loan Repayment",
            filters={"against_loan": loan.name, "docstatus": 1},
            fields=["posting_date", "amount_paid", "owner"],
        )

        transactions = []
        schedule_doc = frappe.db.get_value(
            "Loan Repayment Schedule", {"loan": loan.name, "docstatus": 1}, "name"
        )

        if schedule_doc:
            schedule = frappe.get_doc("Loan Repayment Schedule", schedule_doc)
            sch_rows = schedule.get("repayment_schedule", [])

            for index, sch in enumerate(sch_rows):
                # FIX 1: Tag the final row as 'Maturity' just like the PDF
                is_last_row = index == len(sch_rows) - 1
                description = "Maturity" if is_last_row else "Repayment"

                transactions.append(
                    {
                        "date": sch.payment_date,
                        "description": description,
                        "debit": flt(sch.total_payment),
                        "credit": 0.0,
                        "sort_order": 1,  # Standard bills load first
                    }
                )

        penalties = frappe.get_all(
            "Loan Interest Accrual",
            filters={"loan": loan.name, "docstatus": 1, "penalty_amount": (">", 0)},
            fields=["posting_date", "penalty_amount", "pending_principal_amount"],
        )
        for pen in penalties:
            # FIX 2: Dynamically build the Penalty string with rate and principal
            principal_formatted = "{:,.2f}".format(flt(pen.pending_principal_amount))
            dynamic_desc = (
                f"System Generated Penalty {penalty_rate}% of {principal_formatted}"
            )

            transactions.append(
                {
                    "date": pen.posting_date,
                    "description": dynamic_desc,
                    "debit": flt(pen.penalty_amount),
                    "credit": 0.0,
                    "sort_order": 2,  # Penalties load second
                }
            )

        for rep in repayments:
            transactions.append(
                {
                    "date": rep.posting_date,
                    "description": "Payment Received",
                    "debit": 0.0,
                    "credit": flt(rep.amount_paid),
                    "sort_order": 3,  # Payments clear the debt last
                }
            )

        # FIX 3: Sort chronologically, then by the priority sort_order for same-day overlaps
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
        if schedule_doc and sch_rows:
            maturity_date = sch_rows[-1].payment_date

        # Package the specific loan
        all_loans_data.append(
            {
                "loan_details": {
                    "name": loan.name,
                    "disbursement_date": loan.disbursement_date,
                    "maturity_date": maturity_date,
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
                    "address": customer_doc.primary_address or "",
                },
                "repayments": repayments,
                "transactions": transactions,
            }
        )

    return all_loans_data
