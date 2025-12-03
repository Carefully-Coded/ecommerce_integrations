from copy import deepcopy

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from frappe.utils import cint, cstr, getdate
import shopify

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	FULLFILLMENT_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.order import get_sales_order
from ecommerce_integrations.shopify.utils import create_shopify_log


def prepare_delivery_note(payload, request_id=None):
	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	order = payload

	try:
		sales_order = get_sales_order(cstr(order["id"]))
		if sales_order:
			create_delivery_note(order, setting, sales_order)
			create_shopify_log(status="Success")
		else:
			create_shopify_log(status="Invalid", message="Sales Order not found for syncing delivery note.")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def create_delivery_note(shopify_order, setting, so):
	if not cint(setting.sync_delivery_note):
		return

	for fulfillment in shopify_order.get("fulfillments"):
		if (
			not frappe.db.get_value("Delivery Note", {FULLFILLMENT_ID_FIELD: fulfillment.get("id")}, "name")
			and so.docstatus == 1
		):
			dn = make_delivery_note(so.name)
			setattr(dn, ORDER_ID_FIELD, fulfillment.get("order_id"))
			setattr(dn, ORDER_NUMBER_FIELD, shopify_order.get("name"))
			setattr(dn, FULLFILLMENT_ID_FIELD, fulfillment.get("id"))
			dn.set_posting_time = 1
			dn.posting_date = getdate(fulfillment.get("created_at"))
			dn.naming_series = setting.delivery_note_series or "DN-Shopify-"
			dn.items = get_fulfillment_items(
				dn.items, fulfillment.get("line_items"), fulfillment.get("location_id")
			)
			dn.flags.ignore_mandatory = True
			dn.save()
			dn.submit()

			if shopify_order.get("note"):
				dn.add_comment(text=f"Order Note: {shopify_order.get('note')}")


def get_fulfillment_items(dn_items, fulfillment_items, location_id=None):
	# local import to avoid circular imports
	from ecommerce_integrations.shopify.product import get_item_code

	fulfillment_items = deepcopy(fulfillment_items)

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	wh_map = setting.get_integration_to_erpnext_wh_mapping()
	warehouse = wh_map.get(str(location_id)) or setting.warehouse

	final_items = []

	def find_matching_fullfilement_item(dn_item):
		nonlocal fulfillment_items

		for item in fulfillment_items:
			if get_item_code(item) == dn_item.item_code:
				fulfillment_items.remove(item)
				return item

	for dn_item in dn_items:
		if shopify_item := find_matching_fullfilement_item(dn_item):
			final_items.append(dn_item.update({"qty": shopify_item.get("quantity"), "warehouse": warehouse}))

	return final_items


def push_fulfillment_to_shopify(delivery_note, method=None):
	"""Push Delivery Note to Shopify as a fulfillment.

	This function is called via document hook when a Delivery Note is submitted.
	It creates a fulfillment in Shopify for orders that originated from Shopify.
	"""
	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)

	# Check if sync is enabled
	if not cint(setting.sync_delivery_note) or not setting.is_enabled():
		create_shopify_log(
			status="Info",
			message=f"Delivery Note {delivery_note.name} submitted but sync_delivery_note is disabled or Shopify integration is not enabled"
		)
		return

	# Check if this delivery note already has a Shopify fulfillment ID (avoid duplicates)
	if delivery_note.get(FULLFILLMENT_ID_FIELD):
		create_shopify_log(
			status="Info",
			message=f"Delivery Note {delivery_note.name} already has Shopify fulfillment ID {delivery_note.get(FULLFILLMENT_ID_FIELD)}, skipping"
		)
		return

	# Check if this delivery note is for a Shopify order
	shopify_order_id = delivery_note.get(ORDER_ID_FIELD)
	if not shopify_order_id:
		create_shopify_log(
			status="Info",
			message=f"Delivery Note {delivery_note.name} is not linked to a Shopify order, skipping fulfillment sync"
		)
		return

	try:
		_create_shopify_fulfillment(delivery_note, shopify_order_id, setting)
		create_shopify_log(status="Success", message=f"Fulfillment created for Delivery Note {delivery_note.name}")
	except Exception as e:
		create_shopify_log(
			status="Error",
			exception=e,
			message=f"Failed to create fulfillment for Delivery Note {delivery_note.name}",
			rollback=False  # Don't rollback the delivery note submission
		)


@temp_shopify_session
def _create_shopify_fulfillment(delivery_note, shopify_order_id, setting):
	"""Create a fulfillment in Shopify for the given delivery note.

	Uses the modern GraphQL API with FulfillmentOrder workflow.
	The workflow is:
	1. Query FulfillmentOrders for the order via GraphQL
	2. Create a Fulfillment via GraphQL mutation
	"""
	import json

	# Get location ID from warehouse mapping
	wh_map = setting.get_erpnext_to_integration_wh_mapping()
	location_id = None

	# Try to get location from the first item's warehouse
	if delivery_note.items and delivery_note.items[0].warehouse:
		location_id = wh_map.get(delivery_note.items[0].warehouse)

	# If no location found, try to use the default from Shopify settings
	if not location_id:
		# Get the first available location from the settings
		locations = setting.get("shopify_warehouse_mapping", [])
		if locations:
			location_id = locations[0].shopify_warehouse

	# Step 1: Query fulfillment orders using GraphQL
	query = """
	query getFulfillmentOrders($orderId: ID!) {
		order(id: $orderId) {
			id
			displayFulfillmentStatus
			fulfillmentOrders(first: 10) {
				edges {
					node {
						id
						status
						assignedLocation {
							location {
								id
								legacyResourceId
							}
						}
						lineItems(first: 50) {
							edges {
								node {
									id
									remainingQuantity
								}
							}
						}
					}
				}
			}
		}
	}
	"""

	# Convert numeric order ID to GraphQL global ID
	gid_order_id = f"gid://shopify/Order/{shopify_order_id}"

	graphql_client = shopify.GraphQL()
	print(f"Using Shopify order GID: {gid_order_id}")
	create_shopify_log(
		status="Info",
		message=f"Using Shopify order GID: {gid_order_id}",
		response_data={"gid_order_id": gid_order_id}
	)
	try:
		# Use Shopify GraphQL client
		result_json = graphql_client.execute(
			query=query,
			variables={"orderId": gid_order_id}
		)
		result = json.loads(result_json)

		if "errors" in result:
			create_shopify_log(
				status="Error",
				message=f"GraphQL query error for order {shopify_order_id}",
				response_data=result
			)
			frappe.throw(f"GraphQL query error: {result['errors']}")

		order_data = result.get("data", {}).get("order")
		if not order_data:
			create_shopify_log(
				status="Error",
				message=f"Order {shopify_order_id} not found in Shopify",
				response_data=result
			)
			frappe.throw("Order not found in Shopify")

		fulfillment_status = order_data.get("displayFulfillmentStatus", "UNKNOWN")
		fulfillment_orders = order_data.get("fulfillmentOrders", {}).get("edges", [])

		# Log the fulfillment orders for debugging
		create_shopify_log(
			status="Info",
			message=f"Order {shopify_order_id} has fulfillment status '{fulfillment_status}'. Found {len(fulfillment_orders)} fulfillment orders.",
			response_data={"order": order_data}
		)
	except Exception as e:
		frappe.throw(f"Failed to query fulfillment orders: {str(e)}")

	if not fulfillment_orders:
		# If the order is already fulfilled, just log it and return
		if fulfillment_status == "FULFILLED":
			create_shopify_log(
				status="Info",
				message=f"Order {shopify_order_id} is already fulfilled in Shopify. Skipping fulfillment creation for Delivery Note {delivery_note.name}."
			)
			return
		else:
			# No fulfillment orders available - order may need manual fulfillment setup in Shopify
			create_shopify_log(
				status="Warning",
				message=f"Order {shopify_order_id} has status '{fulfillment_status}' but no fulfillment orders. This order may require manual fulfillment in Shopify or the fulfillment service may need to be configured."
			)
			frappe.throw(f"No fulfillment orders found for this Shopify order (status: {fulfillment_status}). Please check the order's fulfillment settings in Shopify admin.")

	# Find the fulfillment order that matches our location (or use the first one)
	fulfillment_order = None
	for edge in fulfillment_orders:
		fo = edge.get("node", {})
		status = fo.get("status")

		if status in ["OPEN", "SCHEDULED"]:
			assigned_location = fo.get("assignedLocation", {}).get("location", {})
			assigned_location_id = assigned_location.get("legacyResourceId")

			if location_id and str(assigned_location_id) == str(location_id):
				fulfillment_order = fo
				break
			elif not fulfillment_order:  # Fallback to first available
				fulfillment_order = fo

	if not fulfillment_order:
		frappe.throw("No open fulfillment orders found for this Shopify order")

	# Step 2: Build line items for fulfillment
	line_items = []
	for edge in fulfillment_order.get("lineItems", {}).get("edges", []):
		line_item = edge.get("node", {})
		remaining_qty = line_item.get("remainingQuantity", 0)

		if remaining_qty > 0:
			line_items.append({
				"id": line_item.get("id"),
				"quantity": remaining_qty
			})

	if not line_items:
		frappe.throw("No fulfillable items found in the fulfillment order")

	# Step 3: Create the fulfillment using GraphQL mutation
	mutation = """
	mutation fulfillmentCreateV2($fulfillment: FulfillmentV2Input!) {
		fulfillmentCreateV2(fulfillment: $fulfillment) {
			fulfillment {
				id
				legacyResourceId
				status
			}
			userErrors {
				field
				message
			}
		}
	}
	"""

	fulfillment_input = {
		"lineItemsByFulfillmentOrder": [
			{
				"fulfillmentOrderId": fulfillment_order.get("id"),
				"fulfillmentOrderLineItems": line_items
			}
		],
		"notifyCustomer": True
	}

	# Add tracking information if available
	if hasattr(delivery_note, 'lr_no') and delivery_note.lr_no:
		tracking_info = {
			"number": delivery_note.lr_no,
		}
		if hasattr(delivery_note, 'lr_url') and delivery_note.lr_url:
			tracking_info["url"] = delivery_note.lr_url
		if hasattr(delivery_note, 'transporter_name') and delivery_note.transporter_name:
			tracking_info["company"] = delivery_note.transporter_name

		fulfillment_input["trackingInfo"] = tracking_info

	try:
		# Use Shopify GraphQL client for mutation
		graphql_client = shopify.GraphQL()
		result_json = graphql_client.execute(
			query=mutation,
			variables={"fulfillment": fulfillment_input}
		)
		result = json.loads(result_json)

		if "errors" in result:
			frappe.throw(f"GraphQL mutation error: {result['errors']}")

		fulfillment_result = result.get("data", {}).get("fulfillmentCreateV2", {})
		user_errors = fulfillment_result.get("userErrors", [])

		if user_errors:
			error_messages = [f"{err.get('field', 'unknown')}: {err.get('message')}" for err in user_errors]
			frappe.throw(f"Failed to create fulfillment: {', '.join(error_messages)}")

		fulfillment = fulfillment_result.get("fulfillment", {})
		fulfillment_id = fulfillment.get("legacyResourceId")

		if fulfillment_id:
			# Update the delivery note with the Shopify fulfillment ID
			frappe.db.set_value(
				"Delivery Note",
				delivery_note.name,
				FULLFILLMENT_ID_FIELD,
				str(fulfillment_id),
				update_modified=False
			)
			frappe.db.commit()
		else:
			frappe.throw("Fulfillment created but no ID returned")

	except Exception as e:
		frappe.throw(f"Failed to create fulfillment: {str(e)}")
