import frappe
from frappe.utils import getdate, today, date_diff, flt, cint, add_days
from collections import defaultdict

TOLERANCE = 1.0


def update_loan_repayment_statuses():
    """
    Regular — all past-due installments are fully paid (or no installments are past due yet)
    Overdue — 1 unpaid installment, 0 < days_late < 30, no partial payment
    Arrears — either a partial payment exists on an unpaid installment, OR more than 1 installment is unpaid (and days_late < 30)
    Missed Repayment — oldest unpaid installment is >= 30 days overdue (and not past maturity)
    Past Maturity — current_date > maturity_date (loan end date has passed and still unpaid)
    """
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    current_date = getdate(today())

    loans = frappe.db.sql(
        """
        SELECT name, repayment_status, overdue_days
        FROM `tabLoan`
        WHERE docstatus = 1
          AND status = 'Disbursed'
          AND is_term_loan = 1
        """,
        as_dict=True,
    )

    if not loans:
        return

    loan_names = [l.name for l in loans]
    ph = ",".join(["%s"] * len(loan_names))

    schedule_rows = frappe.db.sql(
        f"""
        SELECT loan, name AS schedule_id
        FROM `tabLoan Repayment Schedule`
        WHERE docstatus = 1 AND status = 'Active'
          AND loan IN ({ph})
        """,
        tuple(loan_names),
        as_dict=True,
    )

    loan_to_schedule = {r.loan: r.schedule_id for r in schedule_rows}
    schedule_ids = list(loan_to_schedule.values())
    if not schedule_ids:
        return

    sph = ",".join(["%s"] * len(schedule_ids))
    schedule_entries = frappe.db.sql(
        f"""
        SELECT parent, payment_date, interest_amount, principal_amount
        FROM `tabRepayment Schedule`
        WHERE parent IN ({sph})
        ORDER BY parent, idx ASC
        """,
        tuple(schedule_ids),
        as_dict=True,
    )

    schedule_map = defaultdict(list)
    for row in schedule_entries:
        schedule_map[row.parent].append(row)

    lia_rows = frappe.db.sql(
        f"""
        SELECT loan,
               SUM(paid_interest_amount)  AS paid_int,
               SUM(paid_principal_amount) AS paid_prin
        FROM `tabLoan Interest Accrual`
        WHERE docstatus = 1 AND loan IN ({ph})
        GROUP BY loan
        """,
        tuple(loan_names),
        as_dict=True,
    )

    loan_total_paid = {
        r.loan: flt(r.paid_int, precision) + flt(r.paid_prin, precision)
        for r in lia_rows
    }

    updates = []
    for loan in loans:
        schedule_id = loan_to_schedule.get(loan.name)
        if not schedule_id:
            continue

        schedule = schedule_map.get(schedule_id, [])
        if not schedule:
            continue

        maturity_date = getdate(schedule[-1].payment_date)
        total_paid = loan_total_paid.get(loan.name, 0.0)

        past_due_rows = 0
        has_partial_payment = False
        oldest_unpaid_date = None
        total_billed_so_far = 0.0

        for sch in schedule:
            sch_date = getdate(sch.payment_date)
            if sch_date >= current_date:
                break

            total_billed_so_far += flt(sch.interest_amount, precision) + flt(
                sch.principal_amount, precision
            )

            if (
                flt(total_paid, precision)
                >= flt(total_billed_so_far, precision) - TOLERANCE
            ):
                continue

            past_due_rows += 1
            if oldest_unpaid_date is None:
                oldest_unpaid_date = sch_date
            if flt(total_paid, precision) > 0:
                has_partial_payment = True

        if oldest_unpaid_date:
            days_late = date_diff(current_date, oldest_unpaid_date)
            overdue_days = days_late
            if current_date > maturity_date:
                new_status = "Past Maturity"
            elif days_late >= 30:
                new_status = "Missed Repayment"
            elif has_partial_payment or past_due_rows > 1:
                new_status = "Arrears"
            elif days_late > 0:
                new_status = "Overdue"
            else:
                new_status = "Regular"
        else:
            new_status = "Regular"
            overdue_days = 0

        if (
            loan.repayment_status != new_status
            or flt(loan.overdue_days) != overdue_days
        ):
            updates.append((loan.name, new_status, overdue_days))

    if not updates:
        return

    chunk_size = 500
    updated = 0
    for i in range(0, len(updates), chunk_size):
        batch = updates[i : i + chunk_size]

        when_clause = " ".join(["WHEN %s THEN %s"] * len(batch))
        name_ph = ",".join(["%s"] * len(batch))

        names = [u[0] for u in batch]
        status_params = [x for u in batch for x in (u[0], u[1])]
        days_params = [x for u in batch for x in (u[0], u[2])]

        frappe.db.sql(
            "UPDATE `tabLoan` "
            "SET repayment_status = CASE name " + when_clause + " END, "
            "overdue_days = CASE name " + when_clause + " END "
            "WHERE name IN (" + name_ph + ")",
            tuple(status_params + days_params + names),
        )

        frappe.db.commit()
        updated += len(batch)

    frappe.log_error("Repayment status job completed", f"{updated} loans updated")


def process_daily_penalties():
    current_date = getdate(today())
    precision = cint(frappe.db.get_default("currency_precision")) or 2
    loans = frappe.db.sql(
        """
        SELECT
            l.name, l.company, l.branch, l.cost_center,
            l.applicant, l.applicant_type,
            l.loan_product, l.is_npa,
            l.loan_account, l.interest_income_account,
            lp.grace_period_in_days,
            lp.penalty_interest_rate,
            lp.penalty_stop_threshold_days
        FROM `tabLoan` l
        INNER JOIN `tabLoan Product` lp ON lp.name = l.loan_product
        WHERE l.docstatus = 1
          AND l.status = 'Disbursed'
          AND l.is_term_loan = 1
          AND lp.penalty_interest_rate > 0
    """,
        as_dict=True,
    )

    if not loans:
        return

    loan_names = [l.name for l in loans]
    ph = ",".join(["%s"] * len(loan_names))

    loan_to_schedule = {
        r.loan: r.schedule_id
        for r in frappe.db.sql(
            f"""
            SELECT loan, name AS schedule_id
            FROM `tabLoan Repayment Schedule`
            WHERE docstatus = 1 AND loan IN ({ph})
        """,
            tuple(loan_names),
            as_dict=True,
        )
    }

    schedule_ids = list(loan_to_schedule.values())
    if not schedule_ids:
        return

    sph = ",".join(["%s"] * len(schedule_ids))
    schedule_map = defaultdict(list)
    for row in frappe.db.sql(
        f"""
        SELECT parent, payment_date, principal_amount
        FROM `tabRepayment Schedule`
        WHERE parent IN ({sph})
        ORDER BY parent, idx ASC
    """,
        tuple(schedule_ids),
        as_dict=True,
    ):
        schedule_map[row.parent].append(row)

    paid_map = defaultdict(dict)
    for row in frappe.db.sql(
        f"""
        SELECT loan, due_date, SUM(paid_principal_amount) AS paid_prin
        FROM `tabLoan Interest Accrual`
        WHERE docstatus = 1 AND loan IN ({ph})
        GROUP BY loan, due_date
    """,
        tuple(loan_names),
        as_dict=True,
    ):
        paid_map[row.loan][getdate(row.due_date)] = flt(row.paid_prin, precision)

    already_done = {
        r.loan
        for r in frappe.db.sql(
            f"""
        SELECT DISTINCT loan
        FROM `tabLoan Interest Accrual`
        WHERE docstatus = 1
          AND accrual_type = 'Penalty'
          AND posting_date = %s
          AND loan IN ({ph})
    """,
            tuple([str(current_date)] + loan_names),
            as_dict=True,
        )
    }

    created = errors = 0
    for loan in loans:
        if loan.name in already_done:
            continue

        schedule_id = loan_to_schedule.get(loan.name)
        if not schedule_id:
            continue

        schedule = schedule_map.get(schedule_id, [])
        if not schedule:
            continue

        grace_period = cint(loan.grace_period_in_days)
        daily_rate = flt(loan.penalty_interest_rate) / 100.0
        stop_threshold = cint(loan.penalty_stop_threshold_days)
        maturity_date = getdate(schedule[-1].payment_date)

        if stop_threshold > 0:
            if current_date > getdate(add_days(maturity_date, stop_threshold)):
                continue

        loan_paid = paid_map.get(loan.name, {})
        overdue_principal = 0.0

        for sch in schedule:
            due_date = getdate(sch.payment_date)
            grace_due_date = getdate(add_days(sch.payment_date, grace_period))

            if current_date < grace_due_date:
                continue

            unpaid = flt(sch.principal_amount, precision) - flt(
                loan_paid.get(due_date, 0.0), precision
            )
            if unpaid > 0:
                overdue_principal += unpaid

        if overdue_principal <= 0:
            continue

        penalty_amount = flt(overdue_principal * daily_rate, precision)
        if penalty_amount <= 0:
            continue

        try:
            lia = frappe.new_doc("Loan Interest Accrual")
            lia.loan = loan.name
            lia.company = loan.company
            lia.branch = loan.branch
            lia.cost_center = loan.cost_center
            lia.applicant = loan.applicant
            lia.applicant_type = loan.applicant_type
            lia.interest_income_account = loan.interest_income_account
            lia.loan_account = loan.loan_account
            lia.is_npa = loan.is_npa
            lia.posting_date = current_date
            lia.due_date = current_date
            lia.date_of_accrual = current_date
            lia.accrual_type = "Penalty"
            lia.penalty_amount = penalty_amount
            lia.pending_principal_amount = overdue_principal
            lia.interest_amount = 0.0
            lia.payable_principal_amount = 0.0
            lia.insert(ignore_permissions=True)
            lia.submit()
            created += 1
        except Exception as e:
            errors += 1
            frappe.log_error(
                f"Failed to create penalty LIA for {loan.name}: {e}",
                "Daily Penalty Job",
            )

    frappe.db.commit()
    frappe.logger().info(f"Daily Penalty Job — created={created} errors={errors}")
