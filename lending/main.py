import frappe
from frappe.utils import getdate, today, date_diff, flt, cint


def update_loan_repayment_statuses():
    """
    Runs daily to evaluate all Disbursed loans and update their 'repayment_status'
    based on days late, partial payments, and maturity dates.
    """
    active_loans = frappe.get_all(
        "Loan",
        filters={"status": "Disbursed", "docstatus": 1, "is_term_loan": 1},
        fields=["name"],
    )

    for loan in active_loans:
        update_loan_repayment_status(frappe.get_doc("Loan", loan))
    frappe.db.commit()


def update_loan_repayment_status(loan):
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    current_date = getdate(today())
    new_status = "Regular"

    schedule_id = frappe.db.get_value(
        "Loan Repayment Schedule", {"loan": loan.name}, "name"
    )

    if not schedule_id:
        return

    schedule = frappe.db.get_all(
        "Repayment Schedule",
        filters={"parent": schedule_id},
        fields=[
            "name",
            "payment_date",
            "interest_amount",
            "principal_amount",
            "is_accrued",
        ],
        order_by="idx asc",
    )

    if not schedule:
        return

    maturity_date = schedule[-1].payment_date
    past_due_rows = 0
    has_partial_payment = False
    oldest_unpaid_date = None

    for sch in schedule:
        paid_int = flt(
            frappe.db.get_value(
                "Loan Interest Accrual",
                {"loan": loan.name, "due_date": sch.payment_date, "docstatus": 1},
                "sum(paid_interest_amount)",
            )
        )

        paid_prin = flt(
            frappe.db.get_value(
                "Loan Interest Accrual",
                {"loan": loan.name, "due_date": sch.payment_date, "docstatus": 1},
                "sum(paid_principal_amount)",
            )
        )

        total_billed = flt(sch.interest_amount, precision) + flt(
            sch.principal_amount, precision
        )
        total_paid = flt(paid_int, precision) + flt(paid_prin, precision)

        is_fully_paid = flt(total_paid, precision) >= flt(total_billed, precision)

        if getdate(sch.payment_date) < current_date and not is_fully_paid:
            past_due_rows += 1

            if not oldest_unpaid_date:
                oldest_unpaid_date = sch.payment_date

            # If they paid more than 0 but didn't fully pay, it's a partial payment
            if flt(total_paid, precision) > 0:
                has_partial_payment = True

    if oldest_unpaid_date:
        days_late = date_diff(current_date, oldest_unpaid_date)

        # Priority 1: Past Maturity (Loan term ended entirely)
        if current_date > getdate(maturity_date):
            new_status = "Past Maturity"

        # Priority 2: Arrears (They tried to pay something, OR they have accumulated multiple missed months)
        elif has_partial_payment or past_due_rows > 1:
            new_status = "Arrears"

        # Priority 3: Missed Repayment (They paid NOTHING, and it's been 30+ days)
        elif days_late >= 30:
            new_status = "Missed Repayment"

        # Priority 4: Overdue (They paid NOTHING, and it is 1 to 29 days late)
        elif days_late > 0 and days_late < 30:
            new_status = "Overdue"
    else:
        new_status = "Regular"

    if loan.repayment_status != new_status:
        frappe.db.set_value("Loan", loan.name, "repayment_status", new_status)
