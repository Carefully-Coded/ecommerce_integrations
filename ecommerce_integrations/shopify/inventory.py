import time
from collections import Counter

import frappe
from frappe.utils import cint, create_batch, now
from pyactiveresource.connection import ResourceNotFound
from shopify.resources import InventoryLevel, Variant
import pyactiveresource.connection

from ecommerce_integrations.controllers.inventory import (
	get_inventory_levels,
	update_inventory_sync_status,
)
from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopify.utils import create_shopify_log


def update_inventory_on_shopify() -> None:
	"""Upload stock levels from ERPNext to Shopify.

	Called by scheduler on configured interval.
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopify:
		return

	if not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
		return

	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	inventory_levels = get_inventory_levels(tuple(warehous_map.keys()), MODULE_NAME)

	if inventory_levels:
		upload_inventory_data_to_shopify(inventory_levels, warehous_map)


@temp_shopify_session
def upload_inventory_data_to_shopify(inventory_levels, warehous_map) -> None:
	synced_on = now()

	for inventory_sync_batch in create_batch(inventory_levels, 50):
		for d in inventory_sync_batch:
			d.shopify_location_id = warehous_map[d.warehouse]

			try:
				# Retry loop for rate limiting - covers both Variant.find and InventoryLevel.set
				max_retries = 5
				for attempt in range(max_retries):
					try:
						variant = Variant.find(d.variant_id)
						inventory_id = variant.inventory_item_id

						InventoryLevel.set(
							location_id=d.shopify_location_id,
							inventory_item_id=inventory_id,
							# shopify doesn't support fractional quantity
							available=cint(d.actual_qty) - cint(d.reserved_qty) - cint(d.reserved_qty_for_production),
						)
						break  # Success, exit retry loop
					except pyactiveresource.connection.ClientError as e:
						if e.response.code == 429 and attempt < max_retries - 1:
							retry_after = float(e.response.headers.get('retry-after', 4))
							time.sleep(retry_after)
							continue  # Retry
						raise  # Re-raise if not 429 or max retries exceeded

				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Success"
			except ResourceNotFound:
				# Variant or location is deleted, mark as last synced and ignore.
				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Not Found"
			except Exception as e:
				d.status = "Failed"
				d.failure_reason = str(e)

			frappe.db.commit()

		_log_inventory_update_status(inventory_sync_batch)


def _log_inventory_update_status(inventory_levels) -> None:
	"""Create log of inventory update."""
	log_message = "variant_id,location_id,status,failure_reason\n"

	log_message += "\n".join(
		f"{d.variant_id},{d.shopify_location_id},{d.status},{d.failure_reason or ''}"
		for d in inventory_levels
	)

	stats = Counter([d.status for d in inventory_levels])
	total_items = len(inventory_levels)

	percent_successful = stats["Success"] / total_items if total_items > 0 else 1.0

	if percent_successful == 0:
		status = "Failed"
	elif percent_successful < 1:
		status = "Partial Success"
	else:
		status = "Success"

	summary = f"Processed: {total_items}, Success rate: {percent_successful * 100:.0f}%\n\n"
	log_message = summary + log_message

	create_shopify_log(method="update_inventory_on_shopify", status=status, message=log_message)
