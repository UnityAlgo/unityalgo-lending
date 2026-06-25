from lending.loan_management.doctype.data_migration_tool.data_migration_tool import (
    _read_excel,
    _safe_float,
)

may_files = [
    "/private/files/BWL Repayment till 31st may.xlsx",
    "/private/files/JBD Repayment till 31st May.xlsx",
    "/private/files/JBD2 Repayment till 31st May.xlsx",
    "/private/files/JWR Repayment till 31st May.xlsx",
    "/private/files/KHB Repayment till 31st may.xlsx",
    "/private/files/LLN Repayment till 31st May.xlsx",
    "/private/files/NSR Repayment till 31st may.xlsx",
    "/private/files/SAM Repayment till 31st may.xlsx",
    "/private/files/SRG 1 Repayment till 31st may.xlsx",
    "/private/files/SRG2 Repayment till 31st may.xlsx",
    "/private/files/SRG3 Repayment till 31st may.xlsx",
    "/private/files/SHP Repayment till 31st May.xlsx",
    "/private/files/TLG Repayment till 31st May.xlsx",
]

june_files = [
    "/private/files/BWL277071.xlsx",
    "/private/files/JBD161c0fa.xlsx",
    "/private/files/JBD29dd6c7.xlsx",
    "/private/files/JWRd8d0f6.xlsx",
    "/private/files/KHB52c9bd.xlsx",
    "/private/files/LLN05d1a8.xlsx",
    "/private/files/NSR01d15a.xlsx",
    "/private/files/SAM.xlsx",
    "/private/files/SRG1dc61cc.xlsx",
    "/private/files/SRG2736165.xlsx",
    "/private/files/SRG3fb9f10.xlsx",
    "/private/files/SHPf84de8.xlsx",
    "/private/files/TLG5a0855.xlsx",
]

june_7_10 = "/private/files/7th june to 10th june repayments.xlsx"


def get_totals(files):
    rows = prin = intr = pen = amt = 0
    for fpath in files:
        try:
            for r in _read_excel(fpath):
                ref = str(r.get("Loan#") or r.get("Loan #") or "").strip()
                if not ref:
                    continue
                p = _safe_float(r.get("Principal Paid"))
                i = _safe_float(r.get("Interest Paid"))
                n = _safe_float(r.get("Penalty Paid"))
                fees = _safe_float(r.get("Fees Paid"))
                p += fees
                a = _safe_float(r.get("Amount") or r.get("Total Paid Amount")) or (
                    p + i + n
                )
                if a:
                    prin += p
                    intr += i
                    pen += n
                    amt += a
                    rows += 1
        except Exception as e:
            print(f"  ⚠️  {fpath.split('/')[-1]}: {e}")
    return rows, prin, intr, pen, amt


def run():
    print("\n  Loading May files...")
    may_rows, may_p, may_i, may_n, may_t = get_totals(may_files)

    print("  Loading June 1-6 files...")
    jun_rows, jun_p, jun_i, jun_n, jun_t = get_totals(june_files)

    print("  Loading June 7-10 file...")
    j710_rows, j710_p, j710_i, j710_n, j710_t = get_totals([june_7_10])

    # Grand totals
    all_p = may_p + jun_p + j710_p
    all_i = may_i + jun_i + j710_i
    all_n = may_n + jun_n + j710_n
    all_t = may_t + jun_t + j710_t
    all_r = may_rows + jun_rows + j710_rows

    CLIENT_JUNE6 = {
        "principal": 533149911.56,
        "interest": 123901296.78,
        "penalty": 7286136.66,
    }
    CLIENT_JUNE10 = {
        "principal": 537836474.56,
        "interest": 125606459.78,
        "penalty": 7323996.66,
    }

    print(f"""
    {"=" * 75}
    EXCEL REPAYMENT TOTALS — MONTH WISE
    {"=" * 75}
    {"Period":<20} {"Rows":>7} {"Principal":>18} {"Interest":>16} {"Penalty":>12} {"Total":>14}
    {"─" * 75}
    {"May (till 31st)":<20} {may_rows:>7,} {may_p:>18,.2f} {may_i:>16,.2f} {may_n:>12,.2f} {may_t:>14,.2f}
    {"June 1-6":<20} {jun_rows:>7,} {jun_p:>18,.2f} {jun_i:>16,.2f} {jun_n:>12,.2f} {jun_t:>14,.2f}
    {"June 7-10":<20} {j710_rows:>7,} {j710_p:>18,.2f} {j710_i:>16,.2f} {j710_n:>12,.2f} {j710_t:>14,.2f}
    {"─" * 75}
    {"GRAND TOTAL":<20} {all_r:>7,} {all_p:>18,.2f} {all_i:>16,.2f} {all_n:>12,.2f} {all_t:>14,.2f}
    {"=" * 75}

    CLIENT REPORT COMPARISON:
    {"─" * 75}
    {"June 6 report":<20} {"":>7} {CLIENT_JUNE6["principal"]:>18,.2f} {CLIENT_JUNE6["interest"]:>16,.2f} {CLIENT_JUNE6["penalty"]:>12,.2f}
    {"May+June1-6 excel":<20} {"":>7} {may_p + jun_p:>18,.2f} {may_i + jun_i:>16,.2f} {may_n + jun_n:>12,.2f}
    {"Diff":<20} {"":>7} {(may_p + jun_p) - CLIENT_JUNE6["principal"]:>+18,.2f} {(may_i + jun_i) - CLIENT_JUNE6["interest"]:>+16,.2f} {(may_n + jun_n) - CLIENT_JUNE6["penalty"]:>+12,.2f}
    {"─" * 75}
    {"June 10 report":<20} {"":>7} {CLIENT_JUNE10["principal"]:>18,.2f} {CLIENT_JUNE10["interest"]:>16,.2f} {CLIENT_JUNE10["penalty"]:>12,.2f}
    {"All excel total":<20} {"":>7} {all_p:>18,.2f} {all_i:>16,.2f} {all_n:>12,.2f}
    {"Diff":<20} {"":>7} {all_p - CLIENT_JUNE10["principal"]:>+18,.2f} {all_i - CLIENT_JUNE10["interest"]:>+16,.2f} {all_n - CLIENT_JUNE10["penalty"]:>+12,.2f}
    {"=" * 75}
    """)
