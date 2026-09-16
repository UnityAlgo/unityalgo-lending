# Block new Demand Loan creation

Date: 2026-09-16

## Goal

Stop new Demand Loans (`is_term_loan = 0`) from being created anywhere in the
system, while leaving existing Demand Loan products, applications, loans, and
all their downstream logic (interest accrual, repayment allocation,
disbursement, restructure) fully intact and working as today.

## Non-goals

- No schema changes to `is_term_loan` or any doctype field.
- No removal of demand-loan-specific business logic (e.g.
  `process_loan_interest_accrual_for_demand_loans`,
  `allocate_excess_payment_for_demand_loans`). These must keep serving
  existing demand loans.
- No data migration of existing Demand Loan records.

## Control points

`is_term_loan` on Loan and Loan Application is `read_only` and
`fetch_from: loan_product.is_term_loan` — it is never set independently by a
user on those two doctypes. The only place a human chooses "Demand Loan" is
the **Loan Product** checkbox. That checkbox choice then propagates via link
selection. So there are exactly three places to gate:

1. **Loan Product** — creating a *new* product with the checkbox unchecked.
2. **Loan Application** — creating a *new* application against an *existing*
   Demand Loan product (checkbox already unchecked on some older product).
3. **Loan** — creating a *new* loan directly (bypassing Application) against
   an existing Demand Loan product.

## Changes

### 1. `lending/loan_management/doctype/loan_product/loan_product.py`

In `validate()`, add a guard:

```python
if self.is_new() and not self.is_term_loan and not self.flags.ignore_demand_loan_restriction:
    frappe.throw(_("Demand Loan products can no longer be created. Please create a Term Loan product instead."))
```

The `flags.ignore_demand_loan_restriction` escape hatch exists solely for test
fixture setup (see Testing below) — it is never set by any UI or API call
path, so real users cannot bypass the guard.

### 2. `lending/loan_management/doctype/loan_application/loan_application.py`

In `validate_loan_product()`, after existing checks, add: if `self.is_new()`
and the linked Loan Product's `is_term_loan` is falsy, throw the same style
of error naming the product.

### 3. `lending/loan_management/doctype/loan/loan.py`

Same guard in `validate_loan_product()`, covering direct Loan creation that
doesn't go through Loan Application.

### 4. `lending/loan_management/doctype/loan_product/loan_product.js`

For new, unsaved Loan Product docs: default the "Is Term Loan" checkbox to
checked and make it read-only, so the option to create a Demand Loan product
is not presented in the UI at all. Server-side throw in (1) remains the real
enforcement (API/import paths, script creation).

## Testing

- New tests: creating a Loan Product / Loan Application / Loan with
  `is_term_loan=0` (new doc, no bypass flag) raises `frappe.ValidationError`,
  one case per doctype.
- Existing demand-loan fixture: `create_loan_product()` in
  `lending/loan_management/doctype/loan/test_loan.py` is the single shared
  helper used by `test_loan.py`, `test_loan_disbursement.py`, and
  `test_loan_interest_accrual.py` to create the `"Demand Loan"` product
  fixture (`is_term_loan=0`). Before calling `loan_product.insert()`, set
  `loan_product.flags.ignore_demand_loan_restriction = True` when
  `is_term_loan` is falsy, so existing fixture creation is unaffected and
  the full existing demand-loan test suite (interest accrual, disbursement,
  repayment allocation, etc.) keeps passing unchanged.
- `create_loan_application` / `create_demand_loan` test helpers create
  Loan Application/Loan docs against that already-existing fixture product
  (not a *new* Demand Loan product), so they don't need the bypass flag —
  confirm this holds once the Loan/Loan Application guards are written by
  running the existing demand-loan test suite.
