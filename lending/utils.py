import frappe
import json
from frappe.utils import now_datetime
from frappe.utils.user import is_website_user

from erpnext.setup.utils import enable_all_roles_and_domains


def before_tests():
    frappe.clear_cache()
    from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

    year = now_datetime().year
    if not frappe.get_list("Company"):
        setup_complete(
            {
                "currency": "INR",
                "full_name": "Test User",
                "company_name": "Wind Power LLC",
                "timezone": "Asia/Kolkata",
                "company_abbr": "WP",
                "industry": "Manufacturing",
                "country": "India",
                "fy_start_date": f"{year}-01-01",
                "fy_end_date": f"{year}-12-31",
                "language": "english",
                "company_tagline": "Testing",
                "email": "test@erpnext.com",
                "password": "test",
                "chart_of_accounts": "Standard",
            }
        )

    enable_all_roles_and_domains()
    frappe.db.commit()  # nosemgrep


def check_app_permission():
    if frappe.session.user == "Administrator":
        return True

    if is_website_user():
        return False

    return True


@frappe.whitelist()
def transfer_branch(branch, customers=None):
    if customers is None:
        customers = []
    elif isinstance(customers, str):
        customers = json.loads(customers)

    if not customers:
        return

    if not frappe.db.exists("Branch", branch):
        frappe.throw(f"Branch '{branch}' does not exist")

    customer_tuple = tuple(customers)
    try:
        frappe.db.sql(
            "UPDATE `tabCustomer` SET custom_branch = %s WHERE name IN %s",
            (branch, customer_tuple),
        )

        for table in (
            "tabLoan Disbursement",
            "tabLoan Repayment",
            "tabLoan Application",
        ):
            frappe.db.sql(
                f"UPDATE `{table}` SET branch = %s WHERE applicant IN %s",
                (branch, customer_tuple),
            )

        frappe.db.sql(
            """UPDATE `tabLoan Interest Accrual` lia
			JOIN `tabLoan` l ON l.name = lia.loan
			SET lia.branch = %s
			WHERE l.applicant IN %s""",
            (branch, customer_tuple),
        )

        frappe.db.sql(
            """UPDATE `tabGL Entry` gle
			JOIN `tabLoan Repayment` rep ON rep.name = gle.voucher_no
			SET gle.branch = %s
			WHERE gle.voucher_type = 'Loan Repayment'
				AND rep.applicant IN %s""",
            (branch, customer_tuple),
        )

        frappe.db.sql(
            """UPDATE `tabGL Entry` gle
			JOIN `tabLoan Disbursement` dis ON dis.name = gle.voucher_no
			SET gle.branch = %s
			WHERE gle.voucher_type = 'Loan Disbursement'
				AND dis.applicant IN %s""",
            (branch, customer_tuple),
        )

        frappe.db.sql(
            """UPDATE `tabGL Entry` gle
			JOIN `tabLoan Interest Accrual` lia ON lia.name = gle.voucher_no
			JOIN `tabLoan` l ON l.name = lia.loan
			SET gle.branch = %s
			WHERE gle.voucher_type = 'Loan Interest Accrual'
				AND l.applicant IN %s""",
            (branch, customer_tuple),
        )

        frappe.db.sql(
            """UPDATE `tabGL Entry`
			SET branch = %s
			WHERE party_type = 'Customer'
				AND party IN %s""",
            (branch, customer_tuple),
        )
        frappe.db.commit()
    except Exception as e:
        frappe.throw(str(e))


@frappe.whitelist()
def transfer_sales_person(sales_person, customers=None):
    if customers is None:
        customers = []
    elif isinstance(customers, str):
        customers = json.loads(customers)

    if not customers:
        return

    if not frappe.db.exists("Sales Person", sales_person):
        frappe.throw(f"CRO '{sales_person}' does not exist")

    customer_tuple = tuple(customers)
    try:
        frappe.db.sql(
            "UPDATE `tabCustomer` SET custom_sales_person = %s WHERE name IN %s",
            (sales_person, customer_tuple),
        )

        for table in (
            "tabLoan",
            "tabLoan Repayment",
            "tabLoan Application",
        ):
            frappe.db.sql(
                f"UPDATE `{table}` SET sales_person = %s WHERE applicant IN %s",
                (sales_person, customer_tuple),
            )

        frappe.db.commit()
    except Exception as e:
        frappe.throw(str(e))
