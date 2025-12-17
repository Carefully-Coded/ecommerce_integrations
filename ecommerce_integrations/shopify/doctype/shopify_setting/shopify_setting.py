# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import get_datetime
from shopify.collection import PaginatedIterator
from shopify.resources import Location

from ecommerce_integrations.controllers.setting import (
	ERPNextWarehouse,
	IntegrationWarehouse,
	SettingController,
)
from ecommerce_integrations.shopify import connection
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	FULLFILLMENT_ID_FIELD,
	ITEM_SELLING_RATE_FIELD,
	ITEM_SYNC_CHECKBOX,
	MODULE_NAME,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SUPPLIER_ID_FIELD,
)
from ecommerce_integrations.shopify.utils import (
	ensure_old_connector_is_disabled,
	migrate_from_old_connector,
)


class ShopifySetting(SettingController):
	def is_enabled(self) -> bool:
		return bool(self.enable_shopify)

	def validate(self):
		ensure_old_connector_is_disabled()

		if self.shopify_url:
			self.shopify_url = self.shopify_url.replace("https://", "")
		self._handle_webhooks()
		self._validate_warehouse_links()
		self._initalize_default_values()

		if self.is_enabled():
			setup_custom_fields()

	def on_update(self):
		if self.is_enabled() and not self.is_old_data_migrated:
			migrate_from_old_connector()

		# Check if price list was changed and trigger price sync
		if self.has_value_changed("shopify_price_list") and self.shopify_price_list:
			frappe.enqueue(
				"ecommerce_integrations.shopify.doctype.shopify_setting.shopify_setting.sync_prices_for_all_items",
				queue="long",
				timeout=3600,
				price_list=self.shopify_price_list,
			)
			frappe.msgprint(
				_("Price sync has been queued. All synced items will be updated with prices from {0}").format(
					self.shopify_price_list
				),
				alert=True,
			)

	def _handle_webhooks(self):
		if self.is_enabled() and not self.webhooks:
			new_webhooks = connection.register_webhooks(self.shopify_url, self.get_password("password"))

			if not new_webhooks:
				msg = _("Failed to register webhooks with Shopify.") + "<br>"
				msg += _("Please check credentials and retry.") + " "
				msg += _("Disabling and re-enabling the integration might also help.")
				frappe.throw(msg)

			for webhook in new_webhooks:
				self.append("webhooks", {"webhook_id": webhook.id, "method": webhook.topic})

		elif not self.is_enabled():
			connection.unregister_webhooks(self.shopify_url, self.get_password("password"))

			self.webhooks = list()  # remove all webhooks

	def _validate_warehouse_links(self):
		for wh_map in self.shopify_warehouse_mapping:
			if not wh_map.erpnext_warehouse:
				frappe.throw(_("ERPNext warehouse required in warehouse map table."))

	def _initalize_default_values(self):
		if not self.last_inventory_sync:
			self.last_inventory_sync = get_datetime("1970-01-01")

	@frappe.whitelist()
	@connection.temp_shopify_session
	def update_location_table(self):
		"""Fetch locations from shopify and add it to child table so user can
		map it with correct ERPNext warehouse."""

		self.shopify_warehouse_mapping = []
		for locations in PaginatedIterator(Location.find()):
			for location in locations:
				self.append(
					"shopify_warehouse_mapping",
					{"shopify_location_id": location.id, "shopify_location_name": location.name},
				)

	@frappe.whitelist()
	def sync_inventory_now(self):
		"""Manually trigger inventory sync to Shopify for all synced items."""
		from ecommerce_integrations.shopify.inventory import upload_inventory_data_to_shopify
		from ecommerce_integrations.controllers.inventory import get_inventory_levels

		if not self.is_enabled():
			frappe.throw(_("Shopify integration is not enabled"))

		if not self.update_erpnext_stock_levels_to_shopify:
			frappe.throw(_("Inventory sync to Shopify is not enabled"))

		warehous_map = self.get_erpnext_to_integration_wh_mapping()
		if not warehous_map:
			frappe.throw(_("Please configure warehouse mapping first"))

		# Force sync all items regardless of whether they appear to need updating
		inventory_levels = get_inventory_levels(tuple(warehous_map.keys()), MODULE_NAME, force_sync=True)

		if not inventory_levels:
			frappe.msgprint(_("No items found to sync"))
			return

		upload_inventory_data_to_shopify(inventory_levels, warehous_map)
		frappe.msgprint(_("Inventory sync completed for {0} items. Check Ecommerce Integration Log for details.").format(len(inventory_levels)), alert=True)

	@frappe.whitelist()
	def sync_prices_now(self):
		"""Manually trigger price sync to Shopify for all synced items."""
		if not self.is_enabled():
			frappe.throw(_("Shopify integration is not enabled"))

		if not self.shopify_price_list:
			frappe.throw(_("Please select a Shopify Price List first"))

		# Queue the batch job
		frappe.enqueue(
			"ecommerce_integrations.shopify.doctype.shopify_setting.shopify_setting.sync_prices_for_all_items",
			queue="long",
			timeout=3600,
			price_list=self.shopify_price_list,
		)

		frappe.msgprint(
			_("Price sync has been queued. All synced items will be updated with prices from {0}").format(
				self.shopify_price_list
			),
			alert=True,
		)

	def get_erpnext_warehouses(self) -> list[ERPNextWarehouse]:
		return [wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping]

	def get_erpnext_to_integration_wh_mapping(self) -> dict[ERPNextWarehouse, IntegrationWarehouse]:
		return {
			wh_map.erpnext_warehouse: wh_map.shopify_location_id for wh_map in self.shopify_warehouse_mapping
		}

	def get_integration_to_erpnext_wh_mapping(self) -> dict[IntegrationWarehouse, ERPNextWarehouse]:
		return {
			wh_map.shopify_location_id: wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping
		}


def setup_custom_fields():
	custom_fields = {
		"Item": [
			dict(
				fieldname=ITEM_SYNC_CHECKBOX,
				label="Sync Item with Shopify",
				fieldtype="Check",
				insert_after="item_code",
				print_hide=1,
			),
			dict(
				fieldname=ITEM_SELLING_RATE_FIELD,
				label="Shopify Selling Rate",
				fieldtype="Currency",
				insert_after="standard_rate",
			),
			dict(
				fieldname="manufacturer_details_section",
				label="Manufacturer Details",
				fieldtype="Section Break",
				insert_after="supplier_items",
				collapsible=1,
			),
			dict(
				fieldname="manufacturer_name",
				label="Manufacturer",
				fieldtype="Link",
				options="Manufacturer",
				insert_after="manufacturer_details_section",
			),
			dict(
				fieldname="manufacturer_part_number",
				label="Manufacturer Part Number",
				fieldtype="Data",
				insert_after="manufacturer_name",
			),
		],
		"Customer": [
			dict(
				fieldname=CUSTOMER_ID_FIELD,
				label="Shopify Customer Id",
				fieldtype="Data",
				insert_after="series",
				read_only=1,
				print_hide=1,
			)
		],
		"Supplier": [
			dict(
				fieldname=SUPPLIER_ID_FIELD,
				label="Shopify Supplier Id",
				fieldtype="Data",
				insert_after="supplier_name",
				read_only=1,
				print_hide=1,
			)
		],
		"Address": [
			dict(
				fieldname=ADDRESS_ID_FIELD,
				label="Shopify Address Id",
				fieldtype="Data",
				insert_after="fax",
				read_only=1,
				print_hide=1,
			)
		],
		"Sales Order": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
		"Sales Order Item": [
			dict(
				fieldname=ORDER_ITEM_DISCOUNT_FIELD,
				label="Shopify Discount per unit",
				fieldtype="Float",
				insert_after="discount_and_margin",
				read_only=1,
			),
		],
		"Delivery Note": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=FULLFILLMENT_ID_FIELD,
				label="Shopify Fulfillment Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
		],
		"Sales Invoice": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
	}

	create_custom_fields(custom_fields)


def sync_prices_for_all_items(price_list: str):
	"""
	Background job to sync prices for all Shopify-synced items from a given price list.
	Called when the Shopify Price List is changed in settings.
	"""
	import time
	from ecommerce_integrations.shopify.product import upload_erpnext_item

	# Get all items that are synced with Shopify
	synced_items = frappe.db.sql(
		"""
		SELECT DISTINCT ei.erpnext_item_code
		FROM `tabEcommerce Item` ei
		INNER JOIN `tabItem` i ON i.name = ei.erpnext_item_code
		WHERE ei.integration = %s
		AND i.{sync_field} = 1
		AND i.disabled = 0
		""".format(
			sync_field=ITEM_SYNC_CHECKBOX
		),
		(MODULE_NAME,),
		as_dict=True,
	)

	if not synced_items:
		frappe.log_error("No synced items found to update prices", "Shopify Price Sync")
		return

	total_items = len(synced_items)
	success_count = 0
	error_count = 0

	frappe.publish_realtime(
		"shopify_price_sync_progress",
		{"total": total_items, "current": 0, "status": "started"},
		user=frappe.session.user,
	)

	for idx, item_row in enumerate(synced_items, start=1):
		try:
			item = frappe.get_doc("Item", item_row.erpnext_item_code)

			# Call the standard upload function which will use the new price list
			upload_erpnext_item(item, method="on_update")

			success_count += 1

		except Exception as e:
			error_count += 1
			frappe.log_error(
				f"Failed to sync price for item {item_row.erpnext_item_code}: {str(e)}",
				"Shopify Price Sync Error",
			)

		# Publish progress every 10 items
		if idx % 10 == 0 or idx == total_items:
			frappe.publish_realtime(
				"shopify_price_sync_progress",
				{
					"total": total_items,
					"current": idx,
					"success": success_count,
					"errors": error_count,
					"status": "in_progress",
				},
				user=frappe.session.user,
			)

			# Wait 1 second every 10 items to avoid rate limiting
			if idx < total_items:  # Don't wait after the last item
				time.sleep(1)

		# Commit every 20 items to avoid long transactions
		if idx % 20 == 0:
			frappe.db.commit()

	frappe.db.commit()

	# Send final notification
	frappe.publish_realtime(
		"shopify_price_sync_progress",
		{
			"total": total_items,
			"current": total_items,
			"success": success_count,
			"errors": error_count,
			"status": "completed",
		},
		user=frappe.session.user,
	)

	# Log summary
	summary_msg = f"Shopify Price Sync Completed: {success_count} succeeded, {error_count} failed out of {total_items} items"
	if error_count > 0:
		frappe.log_error(summary_msg, "Shopify Price Sync Summary")
	else:
		frappe.logger().info(summary_msg)
