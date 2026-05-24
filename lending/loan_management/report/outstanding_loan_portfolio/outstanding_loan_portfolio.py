# Copyright (c) 2026, UnityAlgo and contributors
# For license information, please see license.txt

# ─────────────────────────────────────────────────────────────────────────────
# OLP REPORT — FULLY POINT-IN-TIME
# ─────────────────────────────────────────────────────────────────────────────
#
# Every value — amounts, status, active clients, repayment_status, days_past_due
# — is reconstructed from transactional records filtered by as_of_date.
#
# KEY FIX in this version (vs previous):
#   l.status is a live field. A loan closed today shows status='Closed' even
#   when running the report as-of yesterday. The previous version still read
#   l.status for the SQL status_clause filter AND for active_clients, so:
#     - A loan closed today was excluded entirely from an as-of-yesterday report
#     - active_clients count was wrong for any past date
#
#   Fix: SQL fetches ALL submitted, disbursed-ever loans regardless of current
#   status. Python reconstructs point-in-time status from outstanding_principal
#   and unpaid accruals as of as_of_date, mirroring the exact closure conditions
#   in update_paid_amount() in loan_repayment.py:
#       status = Closed/Closure Requested only if:
#           outstanding_principal <= 0
#           AND no unpaid LIA accruals as of date
#           AND no unpaid penalty as of date
#   Everything else = Disbursed or Partially Disbursed (derived from disbursed_amount).
# ─────────────────────────────────────────────────────────────────────────────

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, nowdate


def execute(filters=None):
    filters = frappe._dict(filters or {})
    if not filters.as_of_date:
        filters.as_of_date = nowdate()

    columns = get_columns()
    data    = get_data(filters)
    summary = get_summary(data)

    return columns, data, None, None, summary


def get_columns():
    return [
        {
            "label":     _("Branch"),
            "fieldname": "branch",
            "fieldtype": "Link",
            "options":   "Branch",
            "width":     140,
        },
        {
            "label":     _("Loan Officer"),
            "fieldname": "loan_officer",
            "fieldtype": "Link",
            "options":   "User",
            "width":     160,
        },
        {
            "label":     _("Loan Product"),
            "fieldname": "loan_product",
            "fieldtype": "Link",
            "options":   "Loan Product",
            "width":     150,
        },
        {
            "label":     _("Client Name"),
            "fieldname": "applicant_name",
            "fieldtype": "Data",
            "width":     180,
        },
        {
            "label":     _("Loan Account"),
            "fieldname": "name",
            "fieldtype": "Link",
            "options":   "Loan",
            "width":     160,
        },
        {
            "label":     _("Disbursement Date"),
            "fieldname": "disbursement_date",
            "fieldtype": "Date",
            "width":     140,
        },
        {
            "label":     _("Loan Amount"),
            "fieldname": "loan_amount",
            "fieldtype": "Currency",
            "width":     140,
        },
        {
            "label":     _("Disbursed Amount"),
            "fieldname": "disbursed_amount",
            "fieldtype": "Currency",
            "width":     140,
        },
        {
            "label":     _("Principal Paid (as of date)"),
            "fieldname": "total_principal_paid",
            "fieldtype": "Currency",
            "width":     180,
        },
        {
            "label":     _("Written Off"),
            "fieldname": "written_off_amount",
            "fieldtype": "Currency",
            "width":     130,
        },
        {
            "label":     _("Outstanding Interest (as of date)"),
            "fieldname": "outstanding_interest",
            "fieldtype": "Currency",
            "width":     200,
        },
        {
            "label":     _("Outstanding Penalty (as of date)"),
            "fieldname": "outstanding_penalty",
            "fieldtype": "Currency",
            "width":     190,
        },
        {
            "label":     _("Outstanding Principal / OLP (as of date)"),
            "fieldname": "outstanding_principal",
            "fieldtype": "Currency",
            "width":     220,
        },
        {
            "label":     _("Loan Status (as of date)"),
            "fieldname": "status",
            "fieldtype": "Data",
            "width":     180,
        },
        {
            "label":     _("Repayment Status (as of date)"),
            "fieldname": "repayment_status",
            "fieldtype": "Data",
            "width":     190,
        },
        {
            "label":     _("Days Past Due (as of date)"),
            "fieldname": "days_past_due",
            "fieldtype": "Int",
            "width":     160,
        },
        {
            "label":     _("NPA"),
            "fieldname": "is_npa",
            "fieldtype": "Check",
            "width":     80,
        },
    ]


def get_data(filters):
    conditions = get_conditions(filters)
    loans      = fetch_loans(filters, conditions)
    return build_grouped_rows(loans, filters)


def fetch_loans(filters, conditions):
    """
    Fetches ALL submitted loans that were ever disbursed — regardless of
    current live status. Point-in-time status is computed in Python after
    enrichment so loans closed today still appear correctly on past-date reports.
    """
    precision = cint(frappe.db.get_default("currency_precision")) or 2

    loans = frappe.db.sql(
        """
        SELECT
            l.name,
            l.applicant_name,
            l.applicant_type,
            l.loan_product,
            l.branch,
            l.sales_person              AS loan_officer,
            l.disbursement_date,
            l.loan_amount,
            l.disbursed_amount,
            l.total_payment,
            l.written_off_amount,
            l.debit_adjustment_amount,
            l.credit_adjustment_amount,
            l.refund_amount,
            l.is_npa,
            l.manual_npa,
            l.status                    AS current_status
        FROM `tabLoan` l
        WHERE
            l.docstatus = 1
            AND l.disbursement_date IS NOT NULL
            AND l.disbursement_date <= %(as_of_date)s
            {conditions}
        ORDER BY
            l.branch,
            l.loan_product,
            l.applicant_name
        """.format(conditions=conditions),
        filters,
        as_dict=True,
    )

    if not loans:
        return []

    enrich_with_point_in_time_amounts(loans, filters.as_of_date, precision)

    # Post-filter by status if requested — uses computed point-in-time status
    include_closed = filters.get("include_closed_loan")
    status_filter  = filters.get("status_filter")

    loans = [
        l for l in loans
        if _should_include(l, include_closed, status_filter)
    ]

    return loans


def _should_include(loan, include_closed, status_filter):
    """
    Filter loans by their point-in-time status.
    By default excludes loans that were already Closed as of the report date.
    """
    aod_status = loan.get("status", "")

    if not include_closed and aod_status in ("Closed", "Loan Closure Requested"):
        # Only exclude if ALSO closed in real life — if the loan was open
        # as-of the report date but closed later, it must appear.
        # We use outstanding_principal as the deciding factor: if OLP > 0
        # as of the date, include it regardless of computed status.
        if flt(loan.get("outstanding_principal", 0)) <= 0:
            return False

    if status_filter and aod_status != status_filter:
        return False

    return True


def enrich_with_point_in_time_amounts(loans, as_of_date, precision):
    """
    Computes all amounts and status as they stood on as_of_date.

    Point-in-time status mirrors the closure conditions from
    update_paid_amount() in loan_repayment.py:
        Closed / Loan Closure Requested:
            outstanding_principal <= 0
            AND no unpaid LIA accruals as of date
            AND no unpaid penalty as of date
        Partially Disbursed: disbursed_amount < loan_amount
        Disbursed: everything else
    """
    if not loans:
        return

    loan_names  = [l.name for l in loans]
    ph          = ", ".join(["%s"] * len(loan_names))

    # ── 1. Repayments posted <= as_of_date ────────────────────────────────────
    paid_rows = frappe.db.sql(
        """
        SELECT
            lr.against_loan                             AS loan,
            ROUND(SUM(lr.principal_amount_paid), %s)    AS principal_paid,
            ROUND(SUM(lr.total_interest_paid),   %s)    AS interest_paid,
            ROUND(SUM(lr.total_penalty_paid),    %s)    AS penalty_paid
        FROM `tabLoan Repayment` lr
        WHERE lr.docstatus = 1
          AND lr.posting_date <= %s
          AND lr.against_loan IN ({ph})
        GROUP BY lr.against_loan
        """.format(ph=ph),
        tuple([precision, precision, precision, as_of_date] + loan_names),
        as_dict=True,
    )
    paid_map = {r.loan: r for r in paid_rows}

    # ── 2. Interest accrued <= as_of_date (non-Penalty LIAs) ─────────────────
    int_rows = frappe.db.sql(
        """
        SELECT
            lia.loan                                    AS loan,
            ROUND(SUM(lia.interest_amount), %s)         AS interest_accrued
        FROM `tabLoan Interest Accrual` lia
        WHERE lia.docstatus = 1
          AND lia.accrual_type != 'Penalty'
          AND lia.due_date <= %s
          AND lia.loan IN ({ph})
        GROUP BY lia.loan
        """.format(ph=ph),
        tuple([precision, as_of_date] + loan_names),
        as_dict=True,
    )
    accrued_int_map = {r.loan: flt(r.interest_accrued) for r in int_rows}

    # ── 3. Penalty accrued <= as_of_date ─────────────────────────────────────
    pen_rows = frappe.db.sql(
        """
        SELECT
            lia.loan                                    AS loan,
            ROUND(SUM(lia.penalty_amount), %s)          AS penalty_accrued
        FROM `tabLoan Interest Accrual` lia
        WHERE lia.docstatus = 1
          AND lia.accrual_type = 'Penalty'
          AND lia.posting_date <= %s
          AND lia.loan IN ({ph})
        GROUP BY lia.loan
        """.format(ph=ph),
        tuple([precision, as_of_date] + loan_names),
        as_dict=True,
    )
    accrued_pen_map = {r.loan: flt(r.penalty_accrued) for r in pen_rows}

    # ── 4. Unpaid LIA count as of date (for closure detection) ───────────────
    # A loan is only considered closed if ALL its LIAs are fully paid as of date.
    # This mirrors the unpaid_accruals check in update_paid_amount().
    unpaid_lia_rows = frappe.db.sql(
        """
        SELECT
            lia.loan                                    AS loan,
            COUNT(lia.name)                             AS unpaid_count
        FROM `tabLoan Interest Accrual` lia
        WHERE lia.docstatus = 1
          AND lia.accrual_type != 'Penalty'
          AND lia.due_date <= %s
          AND (
              ROUND(lia.interest_amount - IFNULL(lia.paid_interest_amount, 0), %s) > 0
              OR ROUND(lia.payable_principal_amount - IFNULL(lia.paid_principal_amount, 0), %s) > 0
          )
          AND lia.loan IN ({ph})
        GROUP BY lia.loan
        """.format(ph=ph),
        tuple([as_of_date, precision, precision] + loan_names),
        as_dict=True,
    )
    unpaid_lia_map = {r.loan: int(r.unpaid_count) for r in unpaid_lia_rows}

    # ── 5. Earliest unpaid installment as of date (for days_past_due) ─────────
    earliest_rows = frappe.db.sql(
        """
        SELECT
            lia.loan                                    AS loan,
            MIN(lia.due_date)                           AS earliest_unpaid_due
        FROM `tabLoan Interest Accrual` lia
        WHERE lia.docstatus = 1
          AND lia.accrual_type != 'Penalty'
          AND lia.due_date <= %s
          AND (lia.interest_amount - IFNULL(lia.paid_interest_amount, 0)) > 0.01
          AND lia.loan IN ({ph})
        GROUP BY lia.loan
        """.format(ph=ph),
        tuple([as_of_date] + loan_names),
        as_dict=True,
    )
    earliest_due_map = {r.loan: r.earliest_unpaid_due for r in earliest_rows}

    # ── 6. Enrich each loan ───────────────────────────────────────────────────
    for loan in loans:
        name = loan.name
        paid = paid_map.get(name, {})

        principal_paid_aod = flt(paid.get("principal_paid") or 0, precision)
        interest_paid_aod  = flt(paid.get("interest_paid")  or 0, precision)
        penalty_paid_aod   = flt(paid.get("penalty_paid")   or 0, precision)

        loan["total_principal_paid"] = principal_paid_aod

        # OLP: use total_payment for fully disbursed, disbursed_amount otherwise
        disbursed  = flt(loan.disbursed_amount or 0, precision)
        loan_amt   = flt(loan.loan_amount or 0, precision)
        is_fully   = disbursed >= loan_amt
        base_amount = flt(loan.total_payment or 0, precision) if is_fully else disbursed

        outstanding_principal = round(
            base_amount
            + flt(loan.debit_adjustment_amount  or 0, precision)
            - flt(loan.credit_adjustment_amount or 0, precision)
            - principal_paid_aod
            - flt(loan.written_off_amount       or 0, precision)
            + flt(loan.refund_amount            or 0, precision),
            precision,
        )
        loan["outstanding_principal"] = outstanding_principal

        # Outstanding interest and penalty as of date
        int_accrued = accrued_int_map.get(name, 0.0)
        pen_accrued = accrued_pen_map.get(name, 0.0)
        outstanding_interest = round(max(int_accrued - interest_paid_aod, 0.0), precision)
        outstanding_penalty  = round(max(pen_accrued - penalty_paid_aod,  0.0), precision)
        loan["outstanding_interest"] = outstanding_interest
        loan["outstanding_penalty"]  = outstanding_penalty

        # ── Point-in-time status ──────────────────────────────────────────────
        # Mirrors update_paid_amount() closure conditions exactly:
        #   closed only if principal=0 AND no unpaid LIAs AND no unpaid penalty
        unpaid_lia_count = unpaid_lia_map.get(name, 0)
        if (
            outstanding_principal <= 0
            and unpaid_lia_count == 0
            and outstanding_penalty <= 0
        ):
            # Loan was effectively closed as of this date
            aod_status = (
                "Closed"
                if loan.applicant_type == "Employee"
                else "Loan Closure Requested"
            )
        elif disbursed < loan_amt:
            aod_status = "Partially Disbursed"
        else:
            aod_status = "Disbursed"

        loan["status"] = aod_status

        # ── Days past due and repayment status as of date ─────────────────────
        earliest = earliest_due_map.get(name)
        if earliest and aod_status == "Disbursed":
            loan["days_past_due"]    = max(date_diff(as_of_date, earliest), 0)
            loan["repayment_status"] = _compute_repayment_status(
                loan["days_past_due"]
            )
        else:
            loan["days_past_due"]    = 0
            loan["repayment_status"] = (
                "Closed" if aod_status in ("Closed", "Loan Closure Requested")
                else "Regular"
            )


def _compute_repayment_status(days_past_due):
    """
    Mirrors update_loan_repayment_status() logic from loan_repayment.py.
    Thresholds: 0=Regular, ≤30=Overdue, ≤60=Missed, ≤90=Arrears, >90=Past Maturity
    """
    if days_past_due == 0:
        return "Regular"
    if days_past_due <= 30:
        return "Overdue"
    if days_past_due <= 60:
        return "Missed Repayment"
    if days_past_due <= 90:
        return "Arrears"
    return "Past Maturity"


def get_conditions(filters):
    conditions = []

    if filters.get("loan"):
        conditions.append("AND l.name = %(loan)s")
    if filters.get("branch"):
        conditions.append("AND l.branch = %(branch)s")
    if filters.get("loan_product"):
        conditions.append("AND l.loan_product = %(loan_product)s")
    if filters.get("loan_officer"):
        conditions.append("AND l.sales_person = %(loan_officer)s")
    # repayment_status and status are point-in-time computed — filtered in Python

    return " ".join(conditions)


def build_grouped_rows(loans, filters):
    from itertools import groupby

    # Post-filter by repayment_status if requested
    repayment_status_filter = filters.get("repayment_status")
    if repayment_status_filter:
        loans = [
            l for l in loans
            if l.get("repayment_status") == repayment_status_filter
        ]

    result = []
    loans_sorted = sorted(
        loans,
        key=lambda r: (r.branch or "", r.loan_product or "", r.applicant_name or ""),
    )

    for branch, branch_iter in groupby(
        loans_sorted, key=lambda r: r.branch or ""
    ):
        branch_rows = list(branch_iter)
        b = {k: 0 for k in ("olp", "loan_amount", "disbursed", "principal_paid",
                             "written_off", "interest", "penalty", "clients")}

        for product, product_iter in groupby(
            branch_rows, key=lambda r: r.loan_product or ""
        ):
            product_rows = list(product_iter)
            p = {
                "olp":            sum(flt(r.outstanding_principal) for r in product_rows),
                "loan_amount":    sum(flt(r.loan_amount)           for r in product_rows),
                "disbursed":      sum(flt(r.disbursed_amount)      for r in product_rows),
                "interest":       sum(flt(r.outstanding_interest)  for r in product_rows),
                "penalty":        sum(flt(r.outstanding_penalty)   for r in product_rows),
                "principal_paid": sum(flt(r.total_principal_paid)  for r in product_rows),
                "written_off":    sum(flt(r.written_off_amount)    for r in product_rows),
                "clients":        len(product_rows),
            }

            for row in product_rows:
                result.append(row)

            result.append(frappe._dict({
                "applicant_name":        _("Subtotal — {0}").format(product or _("No Product")),
                "outstanding_principal": p["olp"],
                "loan_amount":           p["loan_amount"],
                "disbursed_amount":      p["disbursed"],
                "outstanding_interest":  p["interest"],
                "outstanding_penalty":   p["penalty"],
                "total_principal_paid":  p["principal_paid"],
                "written_off_amount":    p["written_off"],
                "bold":                  1,
                "_is_subtotal":          1,
                "_active_clients":       p["clients"],
            }))

            for k in b:
                b[k] += p[k]

        result.append(frappe._dict({
            "applicant_name":        _("Branch Total — {0}").format(branch or _("No Branch")),
            "outstanding_principal": b["olp"],
            "loan_amount":           b["loan_amount"],
            "disbursed_amount":      b["disbursed"],
            "outstanding_interest":  b["interest"],
            "outstanding_penalty":   b["penalty"],
            "total_principal_paid":  b["principal_paid"],
            "written_off_amount":    b["written_off"],
            "bold":                  1,
            "_is_branch_total":      1,
            "_active_clients":       b["clients"],
        }))

    return result


def get_summary(data):
    detail_rows = [r for r in data if r.get("name")]

    # active = disbursed as of the report date (excludes closed-as-of-date)
    active_statuses = {"Disbursed", "Partially Disbursed"}
    active_clients  = sum(
        1 for r in detail_rows if r.get("status") in active_statuses
    )
    closure_pending = sum(
        1 for r in detail_rows if r.get("status") == "Loan Closure Requested"
    )

    total_olp            = sum(flt(r.outstanding_principal) for r in detail_rows)
    total_disbursed      = sum(flt(r.disbursed_amount)      for r in detail_rows)
    total_principal_paid = sum(flt(r.total_principal_paid)  for r in detail_rows)
    total_written_off    = sum(flt(r.written_off_amount)    for r in detail_rows)
    total_interest       = sum(flt(r.outstanding_interest)  for r in detail_rows)
    total_penalty        = sum(flt(r.outstanding_penalty)   for r in detail_rows)

    at_risk_statuses = {"Overdue", "Missed Repayment", "Arrears", "Past Maturity"}
    overdue_olp = sum(
        flt(r.outstanding_principal)
        for r in detail_rows
        if r.get("repayment_status") in at_risk_statuses
    )

    return [
        {"value": active_clients,       "label": _("Active Clients"),                              "datatype": "Int",      "indicator": "blue"},
        {"value": total_interest,       "label": _("Total Outstanding Interest"),                  "datatype": "Currency", "indicator": "orange"},
        {"value": total_penalty,        "label": _("Total Outstanding Penalty"),                   "datatype": "Currency", "indicator": "red"},
        {"value": total_olp,            "label": _("Total OLP (Outstanding Principal)"),           "datatype": "Currency", "indicator": "orange"},
        {"value": total_disbursed,      "label": _("Total Disbursed Amount"),                      "datatype": "Currency", "indicator": "green"},
        {"value": total_principal_paid, "label": _("Total Principal Recovered"),                   "datatype": "Currency", "indicator": "green"},
        {"value": total_written_off,    "label": _("Total Written Off"),                           "datatype": "Currency", "indicator": "red"},
        {"value": overdue_olp,          "label": _("OLP at Risk (Overdue / Arrears / Past Maturity)"), "datatype": "Currency", "indicator": "red"},
        {"value": closure_pending,      "label": _("Closure Pending Approval"),                    "datatype": "Int",      "indicator": "yellow"},
    ]


# # Copyright (c) 2026, UnityAlgo

# import frappe
# from frappe import _
# from frappe.utils import cint, flt, nowdate


# def execute(filters=None):
#     filters = frappe._dict(filters or {})
#     if not filters.as_of_date:
#         filters.as_of_date = nowdate()

#     columns = get_columns()
#     data = get_data(filters)
#     summary = get_summary(data)

#     return columns, data, None, None, summary


# def get_columns():
#     return [
#         {
#             "label": _("Branch"),
#             "fieldname": "branch",
#             "fieldtype": "Link",
#             "options": "Branch",
#             "width": 140,
#         },
#         {
#             "label": _("Loan Officer"),
#             "fieldname": "loan_officer",
#             "fieldtype": "Link",
#             "options": "User",
#             "width": 160,
#         },
#         {
#             "label": _("Loan Product"),
#             "fieldname": "loan_product",
#             "fieldtype": "Link",
#             "options": "Loan Product",
#             "width": 150,
#         },
#         {
#             "label": _("Client Name"),
#             "fieldname": "applicant_name",
#             "fieldtype": "Data",
#             "width": 180,
#         },
#         {
#             "label": _("Loan Account"),
#             "fieldname": "name",
#             "fieldtype": "Link",
#             "options": "Loan",
#             "width": 160,
#         },
#         {
#             "label": _("Disbursement Date"),
#             "fieldname": "disbursement_date",
#             "fieldtype": "Date",
#             "width": 140,
#         },
#         {
#             "label": _("Loan Amount"),
#             "fieldname": "loan_amount",
#             "fieldtype": "Currency",
#             "width": 140,
#         },
#         {
#             "label": _("Disbursed Amount"),
#             "fieldname": "disbursed_amount",
#             "fieldtype": "Currency",
#             "width": 140,
#         },
#         {
#             "label": _("Total Principal Paid"),
#             "fieldname": "total_principal_paid",
#             "fieldtype": "Currency",
#             "width": 160,
#         },
#         {
#             "label": _("Written Off"),
#             "fieldname": "written_off_amount",
#             "fieldtype": "Currency",
#             "width": 130,
#         },
#         {
#             "label": _("Outstanding Interest"),
#             "fieldname": "outstanding_interest",
#             "fieldtype": "Currency",
#             "width": 170,
#         },
#         {
#             "label": _("Outstanding Penalty"),
#             "fieldname": "outstanding_penalty",
#             "fieldtype": "Currency",
#             "width": 160,
#         },
#         {
#             "label": _("Outstanding Principal (OLP)"),
#             "fieldname": "outstanding_principal",
#             "fieldtype": "Currency",
#             "width": 200,
#         },
#         {
#             "label": _("Loan Status"),
#             "fieldname": "status",
#             "fieldtype": "Data",
#             "width": 160,
#         },
#         {
#             "label": _("Repayment Status"),
#             "fieldname": "repayment_status",
#             "fieldtype": "Data",
#             "width": 160,
#         },
#         {
#             "label": _("Days Past Due"),
#             "fieldname": "days_past_due",
#             "fieldtype": "Int",
#             "width": 120,
#         },
#         {
#             "label": _("NPA"),
#             "fieldname": "is_npa",
#             "fieldtype": "Check",
#             "width": 80,
#         },
#     ]


# def get_data(filters):
#     conditions = get_conditions(filters)
#     loans = fetch_loans(filters, conditions)
#     return build_grouped_rows(loans)


# def fetch_loans(filters, conditions):
#     precision = cint(frappe.db.get_default("currency_precision")) or 2

#     # ── OLP formula mirrors get_pending_principal_amount() in loan_repayment.py ──
#     #
#     # Fully disbursed branch  (status IN ('Disbursed','Closed') OR disbursed >= loan_amount):
#     #   total_payment + debit_adjustment_amount - credit_adjustment_amount
#     #   - total_principal_paid - total_interest_payable - written_off_amount + refund_amount
#     #
#     # Partially disbursed branch:
#     #   disbursed_amount + debit_adjustment_amount - credit_adjustment_amount
#     #   - total_principal_paid - total_interest_payable - written_off_amount + refund_amount
#     #
#     # 'Loan Closure Requested' status is INCLUDED — principal is nearly 0 but
#     # the loan is not formally closed; it awaits manual approval per on_submit logic.

#     status_clause = """ 
#         AND l.status IN (
#                         'Disbursed',
#                         'Partially Disbursed',
#                         'Loan Closure Requested'
#                     )
#         """
#     if filters.get("include_closed_loan"):
#         status_clause = """ AND l.status IN ('Disbursed','Partially Disbursed','Loan Closure Requested','Closed') """
#         ...

#     sql = """
#         SELECT
#             l.name,
#             l.applicant_name,
#             l.applicant_type,
#             l.loan_product,
#             l.branch,
#             l.sales_person as loan_officer,
#             l.disbursement_date,
#             l.loan_amount,
#             l.disbursed_amount,
#             l.total_payment,
#             l.total_principal_paid,
#             l.total_interest_payable,
#             l.written_off_amount,
#             l.debit_adjustment_amount,
#             l.credit_adjustment_amount,
#             l.refund_amount,
#             l.status,
#             l.repayment_status,
#             l.days_past_due,
#             l.is_npa,
#             l.manual_npa,
#             ROUND(
#                 IFNULL(l.total_interest_payable, 0)
#                 - IFNULL(l.total_interest_paid, 0),
#                 {precision}
#             ) AS outstanding_interest,
#             ROUND(
#                 IFNULL((
#                     SELECT SUM(lia.penalty_amount)
#                     FROM `tabLoan Interest Accrual` lia
#                     WHERE lia.loan = l.name
#                     AND lia.docstatus = 1
#                     AND lia.accrual_type = 'Penalty'
#                 ), 0)
#                 - IFNULL((
#                     SELECT SUM(lr2.total_penalty_paid)
#                     FROM `tabLoan Repayment` lr2
#                     WHERE lr2.against_loan = l.name
#                     AND lr2.docstatus = 1
#                 ), 0),
#                 {precision}
#             ) AS outstanding_penalty,
#             ROUND(
#                 (
#                       IFNULL(l.disbursed_amount, 0)
#                     + IFNULL(l.debit_adjustment_amount, 0)
#                     - IFNULL(l.credit_adjustment_amount, 0)
#                     - IFNULL(l.total_principal_paid, 0)
#                     - IFNULL(l.written_off_amount, 0)
#                     + IFNULL(l.refund_amount, 0)
#                 ),
#                 {precision}
#             ) AS outstanding_principal

#         FROM `tabLoan` l
#         WHERE
#             l.docstatus = 1
#             {status_clause}
#             -- AND l.disbursement_date <= %(as_of_date)s
#             {conditions}
#         ORDER BY
#             l.branch,
#             l.loan_product,
#             l.applicant_name
#     """.format(
#         precision=precision,
#         conditions=conditions,
#         status_clause=status_clause,
#     )

#     return frappe.db.sql(sql, filters, as_dict=True, debug=True)


# def get_conditions(filters):
#     conditions = []

#     # --- ADD THIS BLOCK ---
#     if filters.get("loan"):
#         conditions.append("AND l.name = %(loan)s")

#     if filters.get("branch"):
#         conditions.append("AND l.branch = %(branch)s")

#     if filters.get("loan_product"):
#         conditions.append("AND l.loan_product = %(loan_product)s")

#     if filters.get("loan_officer"):
#         conditions.append("AND l.sales_person = %(loan_officer)s")

#     if filters.get("repayment_status"):
#         conditions.append("AND l.repayment_status = %(repayment_status)s")

#     return " ".join(conditions)


# def build_grouped_rows(loans):
#     from itertools import groupby

#     result = []

#     loans_sorted = sorted(
#         loans,
#         key=lambda r: (r.branch or "", r.loan_product or "", r.applicant_name or ""),
#     )

#     for branch, branch_iter in groupby(loans_sorted, key=lambda r: r.branch or ""):
#         branch_rows = list(branch_iter)
#         branch_olp = 0
#         branch_loan_amount = 0
#         branch_disbursed = 0
#         branch_principal_paid = 0
#         branch_written_off = 0
#         branch_clients = 0
#         branch_outstanding_interest = 0
#         branch_outstanding_penalty = 0

#         for product, product_iter in groupby(
#             branch_rows, key=lambda r: r.loan_product or ""
#         ):
#             product_rows = list(product_iter)

#             product_olp = sum(flt(r.outstanding_principal) for r in product_rows)
#             product_loan_amount = sum(flt(r.loan_amount) for r in product_rows)
#             product_disbursed = sum(flt(r.disbursed_amount) for r in product_rows)

#             product_outstanding_interest = sum(
#                 flt(r.outstanding_interest) for r in product_rows
#             )
#             product_outstanding_penalty = sum(
#                 flt(r.outstanding_penalty) for r in product_rows
#             )
#             product_principal_paid = sum(
#                 flt(r.total_principal_paid) for r in product_rows
#             )
#             product_written_off = sum(flt(r.written_off_amount) for r in product_rows)
#             product_clients = len(product_rows)

#             for row in product_rows:
#                 if row.status == "Loan Closure Requested":
#                     row["_closure_pending"] = 1
#                 result.append(row)

#             result.append(
#                 frappe._dict(
#                     {
#                         "applicant_name": _("Subtotal — {0}").format(
#                             product or _("No Product")
#                         ),
#                         "outstanding_principal": product_olp,
#                         "loan_amount": product_loan_amount,
#                         "disbursed_amount": product_disbursed,
#                         "product_outstanding_penalty": product_outstanding_penalty,
#                         "product_outstanding_interest": product_outstanding_interest,
#                         "total_principal_paid": product_principal_paid,
#                         "written_off_amount": product_written_off,
#                         "bold": 1,
#                         "_is_subtotal": 1,
#                         "_active_clients": product_clients,
#                     }
#                 )
#             )

#             branch_outstanding_interest += product_outstanding_interest
#             branch_outstanding_penalty += product_outstanding_penalty
#             branch_olp += product_olp
#             branch_loan_amount += product_loan_amount
#             branch_disbursed += product_disbursed
#             branch_principal_paid += product_principal_paid
#             branch_written_off += product_written_off
#             branch_clients += product_clients

#         result.append(
#             frappe._dict(
#                 {
#                     "applicant_name": _("Branch Total — {0}").format(
#                         branch or _("No Branch")
#                     ),
#                     "outstanding_principal": branch_olp,
#                     "loan_amount": branch_loan_amount,
#                     "disbursed_amount": branch_disbursed,
#                     "total_principal_paid": branch_principal_paid,
#                     "written_off_amount": branch_written_off,
#                     "branch_outstanding_interest": branch_outstanding_interest,
#                     "branch_outstanding_penalty": branch_outstanding_penalty,
#                     "bold": 1,
#                     "_is_branch_total": 1,
#                     "_active_clients": branch_clients,
#                 }
#             )
#         )

#     return result


# def get_summary(data):
#     active_clients = len(
#         [
#             i
#             for i in data
#             if i.get("name") and "disbursed" in str(i.get("status")).lower()
#         ]
#     )
#     detail_rows = [r for r in data if r.get("name")]

#     total_olp = sum(flt(r.outstanding_principal) for r in detail_rows)
#     total_disbursed = sum(flt(r.disbursed_amount) for r in detail_rows)
#     total_principal_paid = sum(flt(r.total_principal_paid) for r in detail_rows)
#     total_written_off = sum(flt(r.written_off_amount) for r in detail_rows)

#     # Closure pending — 'Loan Closure Requested' (non-employee loans after full repayment)
#     closure_pending = sum(
#         1 for r in detail_rows if r.get("status") == "Loan Closure Requested"
#     )

#     # OLP at risk — any repayment_status set by update_loan_repayment_status()
#     # that indicates the client is behind: Overdue, Missed Repayment, Arrears, Past Maturity
#     at_risk_statuses = {
#         "Overdue",
#         "Missed Repayment",
#         "Arrears",
#         "Past Maturity",
#         "Due Today",
#     }
#     overdue_olp = sum(
#         flt(r.outstanding_principal)
#         for r in detail_rows
#         if r.get("repayment_status") in at_risk_statuses
#     )

#     return [
#         {
#             "value": active_clients,
#             "label": _("Active Clients"),
#             "datatype": "Int",
#             "indicator": "blue",
#         },
#         {
#             "value": sum(flt(r.outstanding_interest) for r in detail_rows),
#             "label": _("Total Outstanding Interest"),
#             "datatype": "Currency",
#             "indicator": "orange",
#         },
#         {
#             "value": sum(flt(r.outstanding_penalty) for r in detail_rows),
#             "label": _("Total Outstanding Penalty"),
#             "datatype": "Currency",
#             "indicator": "red",
#         },
#         {
#             "value": total_olp,
#             "label": _("Total OLP (Outstanding Principal)"),
#             "datatype": "Currency",
#             "indicator": "orange",
#         },
#         {
#             "value": total_disbursed,
#             "label": _("Total Disbursed Amount"),
#             "datatype": "Currency",
#             "indicator": "green",
#         },
#         {
#             "value": total_principal_paid,
#             "label": _("Total Principal Recovered"),
#             "datatype": "Currency",
#             "indicator": "green",
#         },
#         {
#             "value": total_written_off,
#             "label": _("Total Written Off"),
#             "datatype": "Currency",
#             "indicator": "red",
#         },
#         {
#             "value": overdue_olp,
#             "label": _("OLP at Risk (Overdue / Arrears / Past Maturity)"),
#             "datatype": "Currency",
#             "indicator": "red",
#         },
#         {
#             "value": closure_pending,
#             "label": _("Closure Pending Approval"),
#             "datatype": "Int",
#             "indicator": "yellow",
#         },
#     ]
