import frappe
from frappe.utils import getdate, today, date_diff, flt, cint, add_days


def update_loan_repayment_statuses():
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
    overdue_days = 0  # Initialize overdue_days to 0

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
        overdue_days = days_late  # Capture the Days Past Due

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
        overdue_days = 0  # Reset to 0 if fully paid up

    # Update if EITHER the status changed OR the overdue_days changed (so the count ticks up daily)
    if (
        loan.repayment_status != new_status
        or flt(loan.get("overdue_days")) != overdue_days
    ):
        frappe.db.set_value(
            "Loan",
            loan.name,
            {"repayment_status": new_status, "overdue_days": overdue_days},
        )


def process_daily_penalties():
    current_date = getdate(today())
    precision = cint(frappe.db.get_default("currency_precision")) or 2

    active_loans = frappe.db.get_all(
        "Loan",
        filters={"docstatus": 1, "status": "Disbursed"},
        fields=["name", "loan_product", "company"],
    )

    for loan in active_loans:
        print(f"Processing daily penalty for loan: {loan.name}")

        product = frappe.db.get_value(
            "Loan Product",
            loan.loan_product,
            ["grace_period_in_days", "penalty_interest_rate"],
            as_dict=1,
        )

        grace_period = cint(product.grace_period_in_days)
        daily_penalty_rate = flt(product.penalty_interest_rate)

        if daily_penalty_rate <= 0:
            continue

        overdue_principal_total = 0.0
        schedule_id = frappe.db.get_value(
            "Loan Repayment Schedule",
            {"loan": loan.name, "docstatus": ["!=", 2]},
            "name",
        )

        if not schedule_id:
            continue

        schedule = frappe.db.get_all(
            "Repayment Schedule",
            filters={"parent": schedule_id},
            fields=["payment_date", "principal_amount"],
        )

        for sch in schedule:
            due_date_with_grace = add_days(sch.payment_date, grace_period)
            if current_date >= getdate(due_date_with_grace):
                acc_prin_paid = flt(
                    frappe.db.get_value(
                        "Loan Interest Accrual",
                        {
                            "loan": loan.name,
                            "due_date": sch.payment_date,
                            "docstatus": 1,
                        },
                        "sum(paid_principal_amount)",
                    )
                )
                unpaid_prin = flt(sch.principal_amount, precision) - acc_prin_paid

                if unpaid_prin > 0:
                    overdue_principal_total += unpaid_prin

        if overdue_principal_total > 0:
            existing_penalty = frappe.db.exists(
                "Loan Interest Accrual",
                {
                    "loan": loan.name,
                    "posting_date": current_date,
                    "accrual_type": "Penalty",
                    "docstatus": 1,
                },
            )

            if not existing_penalty:
                daily_penalty_amount = flt(
                    overdue_principal_total * daily_penalty_rate, precision
                )
                if daily_penalty_amount > 0:
                    create_penalty_accrual(
                        loan,
                        current_date,
                        daily_penalty_amount,
                        overdue_principal_total,
                    )


def create_penalty_accrual(
    loan, posting_date, penalty_amount, pending_principal_amount
):
    try:
        lia = frappe.new_doc("Loan Interest Accrual")
        lia.loan = loan.name
        lia.company = loan.company
        lia.posting_date = posting_date
        lia.date_of_accrual = posting_date
        lia.due_date = posting_date
        lia.pending_principal_amount = pending_principal_amount
        lia.interest_amount = 0.0
        lia.payable_principal_amount = 0.0
        lia.penalty_amount = penalty_amount
        lia.accrual_type = "Penalty"
        lia.insert(ignore_permissions=True)
        lia.submit()

        frappe.db.commit()
        print(f"Successfully created Daily Penalty LIA for {loan.name}")

    except Exception as e:
        print(f"Error creating LIA: {str(e)}")
        frappe.log_error(
            f"Failed to create daily penalty for {loan.name}: {str(e)}",
            "Daily Penalty Job",
        )


def backfill_missed_penalties(loan_name=None):
    current_date = getdate(today())
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    filters = {"docstatus": 1, "status": "Disbursed"}
    if loan_name:
        filters["name"] = loan_name

    active_loans = frappe.get_all(
        "Loan",
        filters=filters,
        fields=["name", "loan_product", "company"],
    )

    for loan in active_loans:
        print(f"Checking historical penalties for loan: {loan.name}")

        product = frappe.db.get_value(
            "Loan Product",
            loan.loan_product,
            ["grace_period_in_days", "penalty_interest_rate"],
            as_dict=1,
        )

        grace_period = cint(product.grace_period_in_days)
        daily_penalty_rate = flt(product.penalty_interest_rate)

        if daily_penalty_rate <= 0:
            continue

        schedule_id = frappe.db.get_value(
            "Loan Repayment Schedule",
            {"loan": loan.name, "docstatus": ["!=", 2]},
            "name",
        )

        if not schedule_id:
            continue

        schedule = frappe.db.get_all(
            "Repayment Schedule",
            filters={"parent": schedule_id},
            fields=["payment_date", "principal_amount"],
            order_by="payment_date asc",
        )

        if not schedule:
            continue

        earliest_due_date = schedule[0].payment_date
        start_date = add_days(earliest_due_date, grace_period)

        if getdate(start_date) > current_date:
            continue

        days_to_check = date_diff(current_date, start_date)

        for i in range(days_to_check + 1):
            eval_date = add_days(start_date, i)
            overdue_principal_total = 0.0

            for sch in schedule:
                due_date_with_grace = add_days(sch.payment_date, grace_period)

                if getdate(eval_date) >= getdate(due_date_with_grace):
                    acc_prin_paid = flt(
                        frappe.db.get_value(
                            "Loan Interest Accrual",
                            {
                                "loan": loan.name,
                                "due_date": sch.payment_date,
                                "docstatus": 1,
                            },
                            "sum(paid_principal_amount)",
                        )
                    )

                    unpaid_prin = flt(sch.principal_amount, precision) - acc_prin_paid

                    if unpaid_prin > 0:
                        overdue_principal_total += unpaid_prin

            if overdue_principal_total > 0:
                existing_penalty = frappe.db.exists(
                    "Loan Interest Accrual",
                    {
                        "loan": loan.name,
                        "posting_date": eval_date,
                        "accrual_type": "Penalty",
                        "docstatus": 1,
                    },
                )

                if not existing_penalty:
                    daily_penalty_amount = flt(
                        overdue_principal_total * daily_penalty_rate, precision
                    )

                    print(
                        f"Catching up missed penalty for {eval_date}: {daily_penalty_amount} on Overdue Principal: {overdue_principal_total}"
                    )

                    if daily_penalty_amount > 0:
                        create_penalty_accrual(
                            loan,
                            eval_date,
                            daily_penalty_amount,
                            overdue_principal_total,
                        )
