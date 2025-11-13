# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopify.constants import MODULE_NAME, SETTING_DOCTYPE, ITEM_SYNC_CHECKBOX
from ecommerce_integrations.shopify.product import upload_erpnext_item


def update_shopify_price_on_item_price_save(doc, method=None):
	"""
	Update Shopify product price when Item Price is saved/updated
	for items in the configured Shopify Price List.

	This works by triggering the standard upload_erpnext_item function,
	which will update the Shopify product with the new price from the Price List.
	"""
	# Get Shopify settings
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)

	# Only proceed if Shopify is enabled and a price list is configured
	if not setting.enable_shopify or not setting.get("shopify_price_list"):
		return

	# Only proceed if this is the configured Shopify Price List
	if doc.price_list != setting.shopify_price_list:
		return

	# Get the item document
	item = frappe.get_doc("Item", doc.item_code)

	upload_erpnext_item(item, method="on_update")

