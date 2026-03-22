import frappe
from frappe.utils import getdate, today, date_diff, flt, cint, add_days
from collections import defaultdict


def update_loan_repayment_statuses():
    active_loans = frappe.db.get_all(
        "Loan",
        filters={"status": "Disbursed", "docstatus": 1, "is_term_loan": 1},
        fields=["name"],
    )

    print(active_loans)
    for loan in active_loans:
        try:
            update_loan_repayment_status(frappe.get_doc("Loan", loan.name))
        except Exception as e:
            print(e)
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

    print(f"schedule_id={schedule_id}")
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

    print(lia_totals)

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

    print(oldest_unpaid_date)
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

    print(overdue_days)
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
        print(loan)
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


@frappe.whitelist()
def backfill_missed_penalties(loan_name=None, to_date=None):
    """Generate daily penalty accrual LIAs for all missed dates.

    This is the primary function for penalty generation — both for testing
    (via the "Generate Penalties" button on the Loan form) and for production
    backfill (catching up after scheduler downtime or data import).

    Creates one Loan Interest Accrual per day per loan with:
      accrual_type = "Penalty"
      penalty_amount = overdue_principal × daily_penalty_rate

    Uses time-aware payment tracking: for each historical eval_date, only
    counts payments made ON or BEFORE that date, so retroactive payments
    don't erase penalties that were valid at the time.

    Args:
        loan_name: Specific loan to process (default: all disbursed loans)
        to_date: End date for backfill (default: today)

    Returns:
        dict with total_created count and per-loan details
    """
    current_date = getdate(to_date or today())
    precision = cint(frappe.db.get_default("currency_precision")) or 2

    filters = {"docstatus": 1, "status": "Disbursed"}
    if loan_name:
        filters["name"] = loan_name

    active_loans = frappe.get_all(
        "Loan",
        filters=filters,
        fields=["name", "loan_product", "company", "penalty_stop_threshold_days"],
    )

    total_created = 0
    results = []

    for loan in active_loans:
        product = frappe.db.get_value(
            "Loan Product",
            loan.loan_product,
            ["grace_period_in_days", "penalty_interest_rate"],
            as_dict=1,
        )

        if not product:
            continue

        grace_period = cint(product.grace_period_in_days)
        daily_penalty_rate = flt(product.penalty_interest_rate)

        if daily_penalty_rate <= 0:
            continue

        # Use status="Active" to skip superseded schedules (post-reschedule)
        schedule_id = frappe.db.get_value(
            "Loan Repayment Schedule",
            {"loan": loan.name, "docstatus": 1, "status": "Active"},
            "name",
        )

        if not schedule_id:
            # Fallback for legacy data without status field
            schedule_id = frappe.db.get_value(
                "Loan Repayment Schedule",
                {"loan": loan.name, "docstatus": 1},
                "name",
                order_by="creation desc",
            )

        if not schedule_id:
            continue

        schedule = frappe.db.get_all(
            "Repayment Schedule",
            filters={"parent": schedule_id},
            fields=["payment_date", "principal_amount"],
            order_by="idx asc",
        )

        if not schedule:
            continue

        earliest_due_date = schedule[0].payment_date
        start_date = getdate(add_days(earliest_due_date, grace_period))

        threshold_days = cint(loan.penalty_stop_threshold_days)
        maturity_date = getdate(schedule[-1].payment_date)

        # Cap end date at penalty stop threshold (or current_date if no stop)
        end_date = current_date
        if threshold_days > 0:
            penalty_stop_date = getdate(add_days(maturity_date, threshold_days))
            if penalty_stop_date < end_date:
                end_date = penalty_stop_date

        if start_date > end_date:
            continue

        # Batch-fetch existing penalty dates (1 query, not N)
        existing_penalty_dates = set()
        existing_penalties = frappe.db.get_all(
            "Loan Interest Accrual",
            filters={
                "loan": loan.name,
                "accrual_type": "Penalty",
                "docstatus": 1,
            },
            fields=["posting_date"],
        )
        for p in existing_penalties:
            existing_penalty_dates.add(getdate(p.posting_date))

        # Build time-aware payment timeline
        payment_events = frappe.db.sql(
            """
            SELECT
                lia.due_date,
                lr.posting_date as payment_date,
                rd.paid_principal_amount
            FROM `tabLoan Repayment Detail` rd
            JOIN `tabLoan Repayment` lr ON lr.name = rd.parent
            JOIN `tabLoan Interest Accrual` lia ON lia.name = rd.loan_interest_accrual
            WHERE lr.against_loan = %s
              AND lr.docstatus = 1
              AND lia.docstatus = 1
            ORDER BY lr.posting_date ASC
            """,
            (loan.name,),
            as_dict=1,
        )

        payment_timeline = defaultdict(list)
        cumulative_by_due = defaultdict(float)

        for evt in payment_events:
            due_dt = getdate(evt.due_date)
            pay_dt = getdate(evt.payment_date)
            cumulative_by_due[due_dt] += flt(evt.paid_principal_amount, precision)
            payment_timeline[due_dt].append((pay_dt, cumulative_by_due[due_dt]))

        def get_paid_principal_as_of(due_date, as_of_date):
            events = payment_timeline.get(getdate(due_date), [])
            if not events:
                return 0.0
            result = 0.0
            for pay_dt, cumulative in events:
                if pay_dt <= as_of_date:
                    result = cumulative
                else:
                    break
            return result

        # Main backfill loop
        days_to_check = date_diff(end_date, start_date)
        count = 0

        for i in range(days_to_check + 1):
            eval_date = getdate(add_days(start_date, i))

            if eval_date in existing_penalty_dates:
                continue

            overdue_principal_total = 0.0

            for sch in schedule:
                due_date_with_grace = getdate(add_days(sch.payment_date, grace_period))

                if eval_date >= due_date_with_grace:
                    paid_as_of = get_paid_principal_as_of(sch.payment_date, eval_date)
                    unpaid_prin = flt(sch.principal_amount, precision) - flt(
                        paid_as_of, precision
                    )

                    if unpaid_prin > 0:
                        overdue_principal_total += unpaid_prin

            if overdue_principal_total > 0:
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
                    count += 1

                    if count % 50 == 0:
                        frappe.db.commit()

        total_created += count

        if count > 0:
            results.append(
                {
                    "loan": loan.name,
                    "penalties_created": count,
                    "from_date": str(start_date),
                    "to_date": str(end_date),
                }
            )

    if total_created > 0:
        frappe.db.commit()

    return {
        "total_created": total_created,
        "loans_processed": len(results),
        "details": results,
    }
