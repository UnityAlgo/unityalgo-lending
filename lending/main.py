import frappe
from frappe.utils import getdate, today, date_diff, flt, cint, add_days


def update_loan_repayment_statuses():
    active_loans = frappe.db.get_all(
        "Loan",
        filters={"status": "Disbursed", "docstatus": 1, "is_term_loan": 1},
        fields=["name"],
    )

    for loan in active_loans:
        try:
            update_loan_repayment_status(frappe.get_doc("Loan", loan.name))
        except Exception as e:
            frappe.log_error(
                f"Failed to update repayment status for {loan.name}: {str(e)}",
                "Loan Repayment Status Update",
            )

    frappe.db.commit()


def update_loan_repayment_status(loan):
    if loan.status == "Closed":
        return

    precision = cint(frappe.db.get_default("currency_precision")) or 2
    current_date = getdate(today())
    new_status = "Regular"
    overdue_days = 0

    schedule_id = frappe.db.get_value(
        "Loan Repayment Schedule",
        {"loan": loan.name, "docstatus": 1, "status": "Active"},
        "name",
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

    lia_totals = frappe.db.sql(
        """
        SELECT
            due_date,
            SUM(paid_interest_amount) as total_paid_int,
            SUM(paid_principal_amount) as total_paid_prin
        FROM `tabLoan Interest Accrual`
        WHERE loan = %s AND docstatus = 1
        GROUP BY due_date
        """,
        (loan.name,),
        as_dict=1,
    )

    paid_by_date = {}
    for row in lia_totals:
        paid_by_date[getdate(row.due_date)] = {
            "paid_int": flt(row.total_paid_int, precision),
            "paid_prin": flt(row.total_paid_prin, precision),
        }

    for sch in schedule:
        sch_date = getdate(sch.payment_date)
        paid_data = paid_by_date.get(sch_date, {"paid_int": 0, "paid_prin": 0})

        total_billed = flt(sch.interest_amount, precision) + flt(
            sch.principal_amount, precision
        )
        total_paid = flt(paid_data["paid_int"], precision) + flt(
            paid_data["paid_prin"], precision
        )

        is_fully_paid = flt(total_paid, precision) >= flt(total_billed, precision)

        if sch_date < current_date and not is_fully_paid:
            past_due_rows += 1

            if not oldest_unpaid_date:
                oldest_unpaid_date = sch.payment_date

            if flt(total_paid, precision) > 0:
                has_partial_payment = True

    if oldest_unpaid_date:
        days_late = date_diff(current_date, oldest_unpaid_date)
        overdue_days = days_late

        if current_date > getdate(maturity_date):
            new_status = "Past Maturity"

        elif has_partial_payment or past_due_rows > 1:
            new_status = "Arrears"

        elif days_late >= 30:
            new_status = "Missed Repayment"

        elif days_late > 0 and days_late < 30:
            new_status = "Overdue"
    else:
        new_status = "Regular"
        overdue_days = 0

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
        filters={"docstatus": 1, "status": "Disbursed", "is_term_loan": 1},
        fields=["name", "loan_product", "company"],
    )

    for loan in active_loans:
        try:
            _process_penalty_for_loan(loan, current_date, precision)
        except Exception as e:
            frappe.log_error(
                f"Failed to create daily penalty for {loan.name}: {str(e)}",
                "Daily Penalty Job",
            )
    frappe.db.commit()


def _process_penalty_for_loan(loan, current_date, precision):
    """Process daily penalty for a single loan. Extracted for clarity and error isolation."""

    product = frappe.db.get_value(
        "Loan Product",
        loan.loan_product,
        ["grace_period_in_days", "penalty_interest_rate"],
        as_dict=1,
    )

    if not product:
        return

    grace_period = cint(product.grace_period_in_days)
    daily_penalty_rate = flt(product.penalty_interest_rate)

    if daily_penalty_rate <= 0:
        return

    schedule_id = frappe.db.get_value(
        "Loan Repayment Schedule",
        {"loan": loan.name, "docstatus": 1},  # FIX: Explicit docstatus=1 instead of !=2
        "name",
    )

    if not schedule_id:
        return

    schedule = frappe.db.get_all(
        "Repayment Schedule",
        filters={"parent": schedule_id},
        fields=["payment_date", "principal_amount"],
        order_by="idx asc",  # FIX: Added explicit ordering (was missing)
    )

    if not schedule:
        return

    # =================================================================
    # FIX: penalty_stop_threshold_days
    #
    # OLD BUG: The code did `loan.penalty_stop_threshold_days` but the
    # get_all query only fetched ["name", "loan_product", "company"].
    # This field was never fetched, so it was always None/0.
    #
    # Additionally, the logic was INVERTED:
    #   OLD: if penalty_stop_date > current_date: continue
    #   This means: "if the stop date is in the future, SKIP this loan"
    #   which is backwards — you should SKIP when PAST the stop date.
    #
    # FIX: Fetch the field properly from the Loan Product (or Loan doc),
    # and invert the logic so penalties STOP after the threshold.
    # =================================================================
    penalty_stop_threshold_days = cint(
        frappe.db.get_value("Loan", loan.name, "penalty_stop_threshold_days")
    )

    maturity_date = getdate(schedule[-1].payment_date)

    if penalty_stop_threshold_days > 0:
        penalty_stop_date = add_days(maturity_date, penalty_stop_threshold_days)
        # FIX: Stop charging penalties AFTER the threshold date
        if current_date > getdate(penalty_stop_date):
            return

    lia_paid_prin = frappe.db.sql(
        """
        SELECT
            due_date,
            SUM(paid_principal_amount) as total_paid_prin
        FROM `tabLoan Interest Accrual`
        WHERE loan = %s AND docstatus = 1
        GROUP BY due_date
        """,
        (loan.name,),
        as_dict=1,
    )

    paid_prin_by_date = {}
    for row in lia_paid_prin:
        paid_prin_by_date[getdate(row.due_date)] = flt(row.total_paid_prin, precision)

    overdue_principal_total = 0.0

    for sch in schedule:
        due_date_with_grace = add_days(sch.payment_date, grace_period)
        if current_date >= getdate(due_date_with_grace):
            acc_prin_paid = paid_prin_by_date.get(getdate(sch.payment_date), 0)
            unpaid_prin = flt(sch.principal_amount, precision) - flt(
                acc_prin_paid, precision
            )

            if unpaid_prin > 0:
                overdue_principal_total += unpaid_prin

    if overdue_principal_total <= 0:
        return

    # Check if penalty already created for today
    existing_penalty = frappe.db.exists(
        "Loan Interest Accrual",
        {
            "loan": loan.name,
            "posting_date": current_date,
            "accrual_type": "Penalty",
            "docstatus": 1,
        },
    )

    if existing_penalty:
        return

    daily_penalty_amount = flt(overdue_principal_total * daily_penalty_rate, precision)

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
    """Create a single penalty LIA record."""
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


def backfill_missed_penalties(loan_name="ACC-LOAN-2026-00008"):
    current_date = getdate(today())
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    filters = {"docstatus": 1, "status": "Disbursed"}
    if loan_name:
        filters["name"] = loan_name

    active_loans = frappe.get_all(
        "Loan",
        filters=filters,
        fields=["name", "loan_product", "company", "penalty_stop_threshold_days"],
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
        print(daily_penalty_rate)
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
        threshold_days = loan.penalty_stop_threshold_days
        maturity_date = schedule[-1].payment_date
        penalty_stop_date = add_days(maturity_date, threshold_days)

        print(
            f"{start_date}, {penalty_stop_date}\n current_date:{current_date} start_date:{start_date} {penalty_stop_date > current_date}"
        )

        if getdate(penalty_stop_date) < getdate(current_date):
            continue

        if start_date > current_date:
            continue

        days_to_check = date_diff(current_date, start_date)

        print(days_to_check)

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

            print(f"overdue_principal_total = {overdue_principal_total}")
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
                    if daily_penalty_amount > 0:
                        create_penalty_accrual(
                            loan,
                            eval_date,
                            daily_penalty_amount,
                            overdue_principal_total,
                        )
