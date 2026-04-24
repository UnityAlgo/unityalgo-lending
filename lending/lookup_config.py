# lookup_config.py
# Run with: bench execute lending.lookup_config.lookup

import frappe

def lookup():
    print("\n" + "=" * 60)
    print("CONFIG LOOKUP — copy these values into migrate_lending_data.py")
    print("=" * 60)

    # ── Branches ─────────────────────────────────────────────────
    print("\n── Available Branches ──")
    branches = frappe.db.sql("SELECT name FROM `tabBranch` ORDER BY name", as_dict=True)
    if branches:
        for b in branches:
            print(f"  · '{b.name}'")
    else:
        print("  (none found — create a Branch first in HR > Branch)")

    # ── Sales Persons ─────────────────────────────────────────────
    print("\n── Available Sales Persons ──")
    sps = frappe.db.sql(
        "SELECT name FROM `tabSales Person` WHERE is_group = 0 ORDER BY name",
        as_dict=True
    )
    if sps:
        for s in sps:
            print(f"  · '{s.name}'")
    else:
        print("  (none found — create a Sales Person first)")

    # ── GL Accounts for this company ─────────────────────────────
    company = frappe.db.get_single_value("Global Defaults", "default_company")
    abbr    = frappe.db.get_value("Company", company, "abbr")
    print(f"\n── GL Accounts for '{company}' (abbr: {abbr}) ──")

    # Likely account types for lending
    account_filters = [
        ("Loan Accounts (asset)",     {"account_type": "Bank", "is_group": 0, "company": company}),
        ("Income Accounts",           {"root_type": "Income",  "is_group": 0, "company": company}),
        ("Receivable Accounts",       {"account_type": "Receivable", "is_group": 0, "company": company}),
    ]

    # Direct name search for common lending accounts
    print("\n  Searching by name keywords:")
    keywords = ["loan", "interest", "penalty", "disburse", "repay", "receivable"]
    found = frappe.db.sql("""
        SELECT name, account_type, root_type
        FROM `tabAccount`
        WHERE company = %s
          AND is_group = 0
          AND (
            LOWER(name) LIKE '%%loan%%'
            OR LOWER(name) LIKE '%%interest%%'
            OR LOWER(name) LIKE '%%penalty%%'
            OR LOWER(name) LIKE '%%disburse%%'
            OR LOWER(name) LIKE '%%repay%%'
          )
        ORDER BY root_type, name
    """, (company,), as_dict=True)

    if found:
        for a in found:
            print(f"  · '{a.name}'  [{a.root_type} / {a.account_type}]")
    else:
        print("  (no matching accounts found)")
        print("\n  All Asset accounts:")
        assets = frappe.db.sql("""
            SELECT name FROM `tabAccount`
            WHERE company = %s AND root_type = 'Asset' AND is_group = 0
            ORDER BY name LIMIT 30
        """, (company,), as_dict=True)
        for a in assets:
            print(f"    · '{a.name}'")

        print("\n  All Income accounts:")
        incomes = frappe.db.sql("""
            SELECT name FROM `tabAccount`
            WHERE company = %s AND root_type = 'Income' AND is_group = 0
            ORDER BY name LIMIT 30
        """, (company,), as_dict=True)
        for a in incomes:
            print(f"    · '{a.name}'")

    # ── Existing Loan Products (already created) ──────────────────
    print("\n── Existing Loan Products ──")
    products = frappe.db.sql(
        "SELECT name, rate_of_interest, repayment_periods FROM `tabLoan Product` ORDER BY name",
        as_dict=True
    )
    if products:
        for p in products:
            print(f"  · '{p.name}'  (rate={p.rate_of_interest}%, periods={p.repayment_periods}m)")
    else:
        print("  (none)")

    # ── Mode of Payment ───────────────────────────────────────────
    print("\n── Available Modes of Payment ──")
    mops = frappe.db.sql("SELECT name FROM `tabMode of Payment` ORDER BY name", as_dict=True)
    for m in mops:
        print(f"  · '{m.name}'")

    # ── Banks ─────────────────────────────────────────────────────
    print("\n── Available Banks (for bank_name config) ──")
    banks = frappe.db.sql("SELECT name FROM `tabBank` ORDER BY name LIMIT 20", as_dict=True)
    if banks:
        for b in banks:
            print(f"  · '{b.name}'")
    else:
        print("  (none found)")

    print("\n" + "=" * 60)
    print("Copy the correct values into the constants at the top of")
    print("migrate_lending_data.py then run check_config() again.")
    print("=" * 60 + "\n")