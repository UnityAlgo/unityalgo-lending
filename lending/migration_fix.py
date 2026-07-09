import re
from collections import defaultdict

import frappe
from frappe.utils import flt

from lending.loan_management.doctype.data_migration_tool.data_migration_tool import (
    _read_excel,
    _safe_date,
    _safe_float,
    _cnic,
)

FILE_URL = "/private/files/repayments 1june to 1july.xlsx"
START_DATE = "2026-06-01"
END_DATE = "2026-07-01"
TOLERANCE = 0.01


def money(value):
    return round(flt(value or 0), 2)


def close(a, b):
    return abs(money(a) - money(b)) <= TOLERANCE


def get_loan_ref(description):
    match = re.search(r"LoanRef:([^\s,]+)", description or "")
    return match.group(1).strip() if match else None


def money_match(a, b):
    return (
        close(a["amount"], b["amount"])
        and close(a["principal"], b["principal"])
        and close(a["interest"], b["interest"])
        and close(a["penalty"], b["penalty"])
    )


def amount_difference(a, b):
    return round(
        abs(a["amount"] - b["amount"])
        + abs(a["principal"] - b["principal"])
        + abs(a["interest"] - b["interest"])
        + abs(a["penalty"] - b["penalty"]),
        2,
    )


print("=" * 100)
print("REPAYMENT VALIDATION")
print(f"File: {FILE_URL}")
print(f"Date range: {START_DATE} to {END_DATE} inclusive")
print("=" * 100)


def main():

    excel_raw = _read_excel(FILE_URL)

    source_rows = []
    skipped_rows = []

    for index, row in enumerate(excel_raw, start=3):
        loan_ref = str(row.get("Loan#") or row.get("Loan #") or "").strip()

        posting_date = _safe_date(row.get("Collection Date"))

        if not loan_ref or not posting_date:
            skipped_rows.append(
                {
                    "row": index,
                    "loan_ref": loan_ref,
                    "date": posting_date,
                }
            )
            continue

        if posting_date < START_DATE or posting_date > END_DATE:
            continue

        raw_principal = money(row.get("Principal Paid"))
        interest = money(row.get("Interest Paid"))
        penalty = money(row.get("Penalty Paid"))
        fees = money(row.get("Fees Paid"))

        # IMPORTANT:
        # Migration code adds Fees Paid into ERP principal_amount_paid.
        expected_erp_principal = money(raw_principal + fees)

        # New client file uses "Amount".
        amount = money(
            row.get("Amount")
            or row.get("Total Paid Amount")
            or row.get("Total Paid")
            or row.get("Amount Paid")
        )

        if amount == 0:
            amount = money(expected_erp_principal + interest + penalty)

        source_rows.append(
            {
                "excel_row": index,
                "loan_ref": loan_ref,
                "date": posting_date,
                "borrower": str(row.get("Name") or row.get("Full Name") or "").strip(),
                "cnic": _cnic(row.get("Borrower CNIC #")),
                "amount": amount,
                "principal": expected_erp_principal,
                "raw_principal": raw_principal,
                "interest": interest,
                "penalty": penalty,
                "fees": fees,
                "matched": False,
            }
        )

    print(f"\nExcel rows in date range: {len(source_rows):,}")
    print(f"Skipped invalid Excel rows: {len(skipped_rows):,}")

    # =============================================================================
    # 2. READ ERP REPAYMENTS
    # =============================================================================

    erp_db_rows = frappe.db.sql(
        """
        SELECT
            lr.name AS repayment,
            lr.against_loan AS loan,
            DATE(lr.posting_date) AS posting_date,

            lr.amount_paid,
            lr.principal_amount_paid,
            lr.total_interest_paid,
            lr.total_penalty_paid,

            l.description

        FROM `tabLoan Repayment` lr

        INNER JOIN `tabLoan` l
            ON l.name = lr.against_loan

        WHERE lr.docstatus = 1
        AND DATE(lr.posting_date) BETWEEN %s AND %s
        AND l.description LIKE %s

        ORDER BY
            lr.posting_date,
            lr.against_loan,
            lr.name
        """,
        (
            START_DATE,
            END_DATE,
            "%%LoanRef:%%",
        ),
        as_dict=True,
    )

    erp_rows = []

    for row in erp_db_rows:
        loan_ref = get_loan_ref(row.description)

        if not loan_ref:
            continue

        erp_rows.append(
            {
                "repayment": row.repayment,
                "loan": row.loan,
                "loan_ref": loan_ref,
                "date": str(row.posting_date)[:10],
                "amount": money(row.amount_paid),
                "principal": money(row.principal_amount_paid),
                "interest": money(row.total_interest_paid),
                "penalty": money(row.total_penalty_paid),
                "matched": False,
            }
        )

    print(f"ERP repayment rows in date range: {len(erp_rows):,}")

    # =============================================================================
    # 3. TOTALS
    # =============================================================================

    excel_totals = {
        "count": len(source_rows),
        "amount": money(sum(x["amount"] for x in source_rows)),
        "principal": money(sum(x["principal"] for x in source_rows)),
        "interest": money(sum(x["interest"] for x in source_rows)),
        "penalty": money(sum(x["penalty"] for x in source_rows)),
        "fees": money(sum(x["fees"] for x in source_rows)),
    }

    erp_totals = {
        "count": len(erp_rows),
        "amount": money(sum(x["amount"] for x in erp_rows)),
        "principal": money(sum(x["principal"] for x in erp_rows)),
        "interest": money(sum(x["interest"] for x in erp_rows)),
        "penalty": money(sum(x["penalty"] for x in erp_rows)),
    }

    difference = {
        "count": erp_totals["count"] - excel_totals["count"],
        "amount": money(erp_totals["amount"] - excel_totals["amount"]),
        "principal": money(erp_totals["principal"] - excel_totals["principal"]),
        "interest": money(erp_totals["interest"] - excel_totals["interest"]),
        "penalty": money(erp_totals["penalty"] - excel_totals["penalty"]),
    }

    print("\n" + "=" * 100)
    print("TOTAL COMPARISON")
    print("=" * 100)

    print("\nEXCEL:")
    print(excel_totals)

    print("\nERP:")
    print(erp_totals)

    print("\nERP - EXCEL:")
    print(difference)

    # =============================================================================
    # 4. EXACT MATCHES
    # =============================================================================

    erp_by_loan_date = defaultdict(list)

    for erp in erp_rows:
        erp_by_loan_date[(erp["loan_ref"], erp["date"])].append(erp)

    exact_matches = []

    for source in source_rows:
        candidates = erp_by_loan_date.get(
            (source["loan_ref"], source["date"]),
            [],
        )

        for erp in candidates:
            if erp["matched"]:
                continue

            if money_match(source, erp):
                source["matched"] = True
                erp["matched"] = True
                exact_matches.append((source, erp))
                break

    # =============================================================================
    # 5. SAME LOAN + SAME DATE BUT WRONG AMOUNT/SPLIT
    # =============================================================================

    amount_mismatches = []

    for source in source_rows:
        if source["matched"]:
            continue

        candidates = [
            erp
            for erp in erp_rows
            if not erp["matched"]
            and erp["loan_ref"] == source["loan_ref"]
            and erp["date"] == source["date"]
        ]

        if not candidates:
            continue

        best = min(
            candidates,
            key=lambda erp: amount_difference(source, erp),
        )

        source["matched"] = True
        best["matched"] = True

        amount_mismatches.append(
            {
                "type": "AMOUNT_OR_SPLIT_MISMATCH",
                "excel": source,
                "erp": best,
                "difference": {
                    "amount": money(best["amount"] - source["amount"]),
                    "principal": money(best["principal"] - source["principal"]),
                    "interest": money(best["interest"] - source["interest"]),
                    "penalty": money(best["penalty"] - source["penalty"]),
                },
            }
        )

    # =============================================================================
    # 6. LIKELY WRONG DATE
    # =============================================================================

    wrong_dates = []

    for source in source_rows:
        if source["matched"]:
            continue

        candidates = [
            erp
            for erp in erp_rows
            if not erp["matched"]
            and erp["loan_ref"] == source["loan_ref"]
            and erp["date"] != source["date"]
            and money_match(source, erp)
        ]

        if not candidates:
            continue

        best = candidates[0]

        source["matched"] = True
        best["matched"] = True

        wrong_dates.append(
            {
                "type": "LIKELY_WRONG_DATE",
                "excel": source,
                "erp": best,
            }
        )

    # =============================================================================
    # 7. LIKELY WRONG LOAN
    # =============================================================================

    wrong_loans = []

    for source in source_rows:
        if source["matched"]:
            continue

        candidates = [
            erp
            for erp in erp_rows
            if not erp["matched"]
            and erp["loan_ref"] != source["loan_ref"]
            and erp["date"] == source["date"]
            and money_match(source, erp)
        ]

        if not candidates:
            continue

        best = candidates[0]

        source["matched"] = True
        best["matched"] = True

        wrong_loans.append(
            {
                "type": "LIKELY_WRONG_LOAN",
                "excel": source,
                "erp": best,
            }
        )

    # =============================================================================
    # 8. REMAINING MISSING / EXTRA
    # =============================================================================

    missing_in_erp = [row for row in source_rows if not row["matched"]]

    extra_in_erp = [row for row in erp_rows if not row["matched"]]

    # =============================================================================
    # 9. PRINT RESULTS
    # =============================================================================

    print("\n" + "=" * 100)
    print("VALIDATION SUMMARY")
    print("=" * 100)

    print(f"Exact matches                 : {len(exact_matches):,}")
    print(f"Amount/split mismatches       : {len(amount_mismatches):,}")
    print(f"Likely wrong dates            : {len(wrong_dates):,}")
    print(f"Likely wrong loans            : {len(wrong_loans):,}")
    print(f"Missing in ERP                : {len(missing_in_erp):,}")
    print(f"Extra in ERP                  : {len(extra_in_erp):,}")

    total_issues = (
        len(amount_mismatches)
        + len(wrong_dates)
        + len(wrong_loans)
        + len(missing_in_erp)
        + len(extra_in_erp)
    )

    print(f"\nTOTAL ISSUE RECORDS           : {total_issues:,}")

    print("\n" + "=" * 100)
    print("1. AMOUNT / PRINCIPAL / INTEREST / PENALTY MISMATCHES")
    print("=" * 100)

    for item in amount_mismatches:
        x = item["excel"]
        e = item["erp"]
        d = item["difference"]

        print(
            f"\nLoanRef: {x['loan_ref']}"
            f"\nDate:    {x['date']}"
            f"\nExcel row: {x['excel_row']}"
            f"\nERP repayment: {e['repayment']}"
            f"\n"
            f"\nEXCEL -> amount={x['amount']} "
            f"principal={x['principal']} "
            f"interest={x['interest']} "
            f"penalty={x['penalty']} "
            f"fees={x['fees']}"
            f"\nERP   -> amount={e['amount']} "
            f"principal={e['principal']} "
            f"interest={e['interest']} "
            f"penalty={e['penalty']}"
            f"\nDIFF  -> {d}"
        )

    print("\n" + "=" * 100)
    print("2. LIKELY WRONG DATES")
    print("=" * 100)

    for item in wrong_dates:
        x = item["excel"]
        e = item["erp"]

        print(
            f"\nLoanRef: {x['loan_ref']}"
            f"\nExcel row: {x['excel_row']}"
            f"\nCorrect Excel date: {x['date']}"
            f"\nERP date:           {e['date']}"
            f"\nERP repayment:      {e['repayment']}"
            f"\nAmount:             {x['amount']}"
        )

    print("\n" + "=" * 100)
    print("3. LIKELY WRONG LOANS")
    print("=" * 100)

    for item in wrong_loans:
        x = item["excel"]
        e = item["erp"]

        print(
            f"\nDate: {x['date']}"
            f"\nExcel row: {x['excel_row']}"
            f"\nCorrect LoanRef: {x['loan_ref']}"
            f"\nERP LoanRef:     {e['loan_ref']}"
            f"\nERP loan:        {e['loan']}"
            f"\nERP repayment:   {e['repayment']}"
            f"\nAmount:          {x['amount']}"
        )

    print("\n" + "=" * 100)
    print("4. MISSING IN ERP")
    print("=" * 100)

    for x in missing_in_erp:
        print(
            f"\nExcel row: {x['excel_row']}"
            f"\nLoanRef:   {x['loan_ref']}"
            f"\nDate:      {x['date']}"
            f"\nBorrower:  {x['borrower']}"
            f"\nAmount:    {x['amount']}"
            f"\nPrincipal: {x['principal']}"
            f"\nInterest:  {x['interest']}"
            f"\nPenalty:   {x['penalty']}"
            f"\nFees:      {x['fees']}"
        )

    print("\n" + "=" * 100)
    print("5. EXTRA IN ERP")
    print("=" * 100)

    for e in extra_in_erp:
        print(
            f"\nERP repayment: {e['repayment']}"
            f"\nERP loan:      {e['loan']}"
            f"\nLoanRef:       {e['loan_ref']}"
            f"\nDate:          {e['date']}"
            f"\nAmount:        {e['amount']}"
            f"\nPrincipal:     {e['principal']}"
            f"\nInterest:      {e['interest']}"
            f"\nPenalty:       {e['penalty']}"
        )

    affected_loan_refs = sorted(
        {x["excel"]["loan_ref"] for x in amount_mismatches}
        | {x["excel"]["loan_ref"] for x in wrong_dates}
        | {x["excel"]["loan_ref"] for x in wrong_loans}
        | {x["loan_ref"] for x in missing_in_erp}
        | {x["loan_ref"] for x in extra_in_erp}
    )

    print("\n" + "=" * 100)
    print("AFFECTED LOAN REFS")
    print("=" * 100)

    for loan_ref in affected_loan_refs:
        print(loan_ref)

    print(f"\nAffected loans: {len(affected_loan_refs):,}")

    # Keep results available in the bench console namespace.
    repayment_validation_result = {
        "excel_totals": excel_totals,
        "erp_totals": erp_totals,
        "difference": difference,
        "exact_matches": exact_matches,
        "amount_mismatches": amount_mismatches,
        "wrong_dates": wrong_dates,
        "wrong_loans": wrong_loans,
        "missing_in_erp": missing_in_erp,
        "extra_in_erp": extra_in_erp,
        "affected_loan_refs": affected_loan_refs,
    }
    
    print(repayment_validation_result)
    print("\n" + "=" * 100)
    print("VALIDATION COMPLETE — READ ONLY, NO DATABASE CHANGES MADE")
    print("=" * 100)
    return repayment_validation_result

