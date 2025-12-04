import frappe
from frappe.utils import cstr, getdate, nowdate

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopify.utils import create_shopify_log


def prepare_payment_against_sales_order(payload, request_id=None):
	"""Process payment for a Shopify order by creating a payment entry against the Sales Order.

	This is called when an order is marked as paid in Shopify.
	Creates a payment entry against the Sales Order (as an advance payment),
	which sets the Sales Order status to "To Deliver".
	"""
	from ecommerce_integrations.shopify.order import get_sales_order

	order = payload

	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	try:
		sales_order = get_sales_order(cstr(order["id"]))
		if sales_order:
			make_payment_entry_against_sales_order(order, setting, sales_order)
			create_shopify_log(status="Success")
		else:
			create_shopify_log(status="Invalid", message="Sales Order not found for payment entry.")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def make_payment_entry_against_sales_order(shopify_order, setting, so):
	"""Create a payment entry against the Sales Order.

	This creates an advance payment against the SO, which:
	- Records the payment received from Shopify
	- Updates the Sales Order status to "To Deliver" (paid but not yet fulfilled)
	"""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	# Check if payment entry already exists for this order
	existing_payment = frappe.db.get_value(
		"Payment Entry Reference",
		{"reference_doctype": "Sales Order", "reference_name": so.name},
		"parent"
	)

	if existing_payment:
		# Payment already recorded
		return

	if so.docstatus != 1:
		# Sales Order must be submitted
		return

	if so.grand_total <= 0:
		# No payment needed for zero-value orders
		return

	posting_date = getdate(shopify_order.get("created_at")) or nowdate()

	payment_entry = get_payment_entry("Sales Order", so.name, bank_account=setting.cash_bank_account)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = so.name
	payment_entry.posting_date = posting_date
	payment_entry.reference_date = posting_date
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
