import frappe
from frappe import _
from frappe.utils import flt, getdate, cint


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
                    "custom_data": custom_data,
                }
            )

    return columns, flat_data, None, None, None


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


@frappe.whitelist()
def get_customer_loan_data(customer, loan_filter=None):
    precision = cint(frappe.db.get_default("currency_precision")) or 2
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

        # =============================================================
        # REPAYMENTS
        # =============================================================
        repayments = frappe.get_all(
            "Loan Repayment",
            filters={"against_loan": loan.name, "docstatus": 1},
            fields=[
                "posting_date",
                "amount_paid",
                "owner",
                "name",
                "payment_account",
                "repayment_type",
                "principal_amount_paid",
                "total_interest_paid",
                "total_penalty_paid",
            ],
            order_by="posting_date asc",
        )

        repayment_rows = []
        for rep in repayments:
            collector_name = (
                frappe.db.get_value("User", rep.owner, "full_name") or rep.owner
            )

            pay_method = "Cash"
            if rep.payment_account:
                pa = rep.payment_account.lower()
                if "bank" in pa or "transfer" in pa:
                    pay_method = "Bank Transfer"
                elif "mobile" in pa or "jazz" in pa or "easypaisa" in pa:
                    pay_method = "Mobile Money"

            repayment_rows.append(
                {
                    "loan": loan.name,
                    "borrower_name": customer_doc.customer_name,
                    "repayment_method": pay_method,
                    "collected_by": collector_name,
                    "collection_date": rep.posting_date,
                    "total_paid_amount": flt(rep.amount_paid),
                }
            )

        # =============================================================
        # TRANSACTIONS — from ALL schedules (Active + Superseded)
        # =============================================================
        transactions = []

        all_schedules = frappe.get_all(
            "Loan Repayment Schedule",
            filters={"loan": loan.name, "docstatus": 1},
            fields=["name", "status"],
            order_by="creation asc",
        )

        active_schedule_id = None
        active_sch_rows = []

        for sched in all_schedules:
            schedule_doc = frappe.get_doc("Loan Repayment Schedule", sched.name)
            sch_rows = schedule_doc.get("repayment_schedule", [])

            if sched.status == "Active":
                active_schedule_id = sched.name
                active_sch_rows = sch_rows

            is_superseded = sched.status == "Superseded"

            for index, sch in enumerate(sch_rows):
                is_last_row = index == len(sch_rows) - 1

                if is_superseded:
                    description = "Repayment (old schedule)"
                    if is_last_row:
                        description = "Maturity (old schedule)"
                else:
                    description = "Repayment"
                    if is_last_row:
                        description = "Maturity"

                transactions.append(
                    {
                        "date": sch.payment_date,
                        "description": description,
                        "debit": flt(sch.total_payment),
                        "credit": 0.0,
                        "sort_order": 1,
                    }
                )

        # =============================================================
        # PENALTIES — only non-zero (excludes zeroed reschedule LIAs)
        # =============================================================
        penalties = frappe.get_all(
            "Loan Interest Accrual",
            filters={
                "loan": loan.name,
                "docstatus": 1,
                "accrual_type": "Penalty",
                "penalty_amount": (">", 0),
            },
            fields=["posting_date", "penalty_amount", "pending_principal_amount"],
            order_by="posting_date asc",
        )

        for pen in penalties:
            principal_formatted = "{:,.2f}".format(flt(pen.pending_principal_amount))
            penalty_rate_display = (
                "{:.1f}".format(flt(penalty_rate) * 100)
                if penalty_rate < 1
                else "{:.1f}".format(flt(penalty_rate))
            )
            dynamic_desc = "System Generated Penalty {0}% of {1}".format(
                penalty_rate_display, principal_formatted
            )

            transactions.append(
                {
                    "date": pen.posting_date,
                    "description": dynamic_desc,
                    "debit": flt(pen.penalty_amount),
                    "credit": 0.0,
                    "sort_order": 2,
                }
            )

        # =============================================================
        # PAYMENT entries in transaction list
        # =============================================================
        for rep in repayments:
            if rep.repayment_type == "Loan Closure":
                # Show Loan Closure with breakdown
                parts = []
                if flt(rep.principal_amount_paid):
                    parts.append(
                        "Principal: {:,.2f}".format(flt(rep.principal_amount_paid))
                    )
                if flt(rep.total_interest_paid):
                    parts.append(
                        "Interest: {:,.2f}".format(flt(rep.total_interest_paid))
                    )
                if flt(rep.total_penalty_paid):
                    parts.append("Penalty: {:,.2f}".format(flt(rep.total_penalty_paid)))

                breakdown = " | ".join(parts) if parts else ""
                description = "Early Loan Closure"
                if breakdown:
                    description = "Early Loan Closure ({0})".format(breakdown)

                transactions.append(
                    {
                        "date": rep.posting_date,
                        "description": description,
                        "debit": 0.0,
                        "credit": flt(rep.amount_paid),
                        "sort_order": 3,
                    }
                )
            else:
                # Normal repayment
                transactions.append(
                    {
                        "date": rep.posting_date,
                        "description": "Payment Received",
                        "debit": 0.0,
                        "credit": flt(rep.amount_paid),
                        "sort_order": 3,
                    }
                )

        # =============================================================
        # RESCHEDULE entries
        # =============================================================
        reschedules = frappe.get_all(
            "Loan Reschedule",
            filters={"loan": loan.name, "docstatus": 1},
            fields=[
                "name",
                "posting_date",
                "capitalized_interest",
                "capitalized_penalty",
                "new_principal",
            ],
            order_by="posting_date asc",
        )

        for rsc in reschedules:
            cap_total = flt(rsc.capitalized_interest) + flt(rsc.capitalized_penalty)
            if cap_total > 0:
                transactions.append(
                    {
                        "date": rsc.posting_date,
                        "description": "Loan Rescheduled — interest {0} + penalty {1} capitalized".format(
                            "{:,.2f}".format(flt(rsc.capitalized_interest)),
                            "{:,.2f}".format(flt(rsc.capitalized_penalty)),
                        ),
                        "debit": 0.0,
                        "credit": cap_total,
                        "sort_order": 0,
                    }
                )

        # =============================================================
        # SORT and running balance
        # =============================================================
        transactions = sorted(
            transactions, key=lambda k: (getdate(k["date"]), k["sort_order"])
        )

        running_bal = 0.0
        for tx in transactions:
            running_bal += tx["debit"] - tx["credit"]
            tx["balance"] = flt(running_bal, precision)

        # =============================================================
        # LOAN TERMS — from active schedule
        # =============================================================
        total_penalty = sum(flt(p.penalty_amount) for p in penalties)
        total_due = flt(loan.total_payment) + total_penalty
        balance_amount = flt(total_due - flt(loan.total_amount_paid), precision)

        maturity_date = None
        if active_sch_rows:
            maturity_date = active_sch_rows[-1].payment_date

        non_deductable_fees = flt(
            loan.get("custom_non_deductable_fees") or 0, precision
        )

        # Detect loan closure
        is_closed = loan.status in ("Closed", "Loan Closure Requested")
        closure_date = None
        closure_repayment = None
        if is_closed:
            closure_rep = frappe.db.get_value(
                "Loan Repayment",
                {
                    "against_loan": loan.name,
                    "docstatus": 1,
                    "repayment_type": "Loan Closure",
                },
                ["posting_date", "name", "amount_paid"],
                as_dict=1,
            )
            if closure_rep:
                closure_date = closure_rep.posting_date
                closure_repayment = closure_rep.name

        company_doc = frappe.get_doc("Company", loan.company)

        all_loans_data.append(
            {
                "loan_details": {
                    "name": loan.name,
                    "disbursement_date": loan.disbursement_date,
                    "maturity_date": maturity_date,
                    "repayment_cycle": "Monthly",
                    "principal_amount": flt(loan.loan_amount, precision),
                    "interest_rate": loan.rate_of_interest,
                    "interest_amount": flt(loan.total_interest_payable, precision),
                    "non_deductable_fees": non_deductable_fees,
                    "penalty_amount": flt(total_penalty, precision),
                    "branch": loan.get("branch"),
                    "total_due": flt(total_due, precision),
                    "paid_amount": flt(loan.total_amount_paid, precision),
                    "balance_amount": balance_amount,
                    "status": loan.status,
                    "repayment_status": loan.repayment_status,
                    "is_closed": is_closed,
                    "closure_date": closure_date,
                },
                "customer_details": {
                    "name": customer_doc.customer_name,
                    "branch": customer_doc.get("branch"),
                    "mobile": customer_doc.get("custom_phone_no_1") or "",
                    "cnic": customer_doc.get("custom_cnic_no") or "",
                },
                "company_info": {
                    "name": company_doc.company_name,
                    "logo": company_doc.get("company_logo") or "",
                    "branch": loan.get("branch") or "",
                    "date": frappe.utils.today(),
                },
                "repayment_rows": repayment_rows,
                "transactions": transactions,
                "total_balance": flt(running_bal, precision),
            }
        )

    return all_loans_data
