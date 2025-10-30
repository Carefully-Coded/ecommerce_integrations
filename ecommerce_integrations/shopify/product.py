from typing import Optional

import frappe
from frappe import _, msgprint
from frappe.utils import cint, cstr
from frappe.utils.nestedset import get_root_of
from shopify.resources import Product, Variant
import shopify

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	ITEM_SELLING_RATE_FIELD,
	ITEM_SYNC_CHECKBOX,
	MODULE_NAME,
	SETTING_DOCTYPE,
	SHOPIFY_VARIANTS_ATTR_LIST,
	SUPPLIER_ID_FIELD,
	WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


class ShopifyProduct:
	def __init__(
		self,
		product_id: str,
		variant_id: str | None = None,
		sku: str | None = None,
		has_variants: int | None = 0,
	):
		self.product_id = str(product_id)
		self.variant_id = str(variant_id) if variant_id else None
		self.sku = str(sku) if sku else None
		self.has_variants = has_variants
		self.setting = frappe.get_doc(SETTING_DOCTYPE)

		if not self.setting.is_enabled():
			frappe.throw(_("Can not create Shopify product when integration is disabled."))

	def is_synced(self) -> bool:
		return ecommerce_item.is_synced(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
		)

	def get_erpnext_item(self):
		return ecommerce_item.get_erpnext_item(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
			has_variants=self.has_variants,
		)

	@temp_shopify_session
	def sync_product(self):
		if not self.is_synced():
			shopify_product = Product.find(self.product_id)
			product_dict = shopify_product.to_dict()
			self._make_item(product_dict)

	def _make_item(self, product_dict):
		_add_weight_details(product_dict)

		warehouse = self.setting.warehouse

		if _has_variants(product_dict):
			self.has_variants = 1
			attributes = self._create_attribute(product_dict)
			self._create_item(product_dict, warehouse, 1, attributes)
			self._create_item_variants(product_dict, warehouse, attributes)

		else:
			product_dict["variant_id"] = product_dict["variants"][0]["id"]
			self._create_item(product_dict, warehouse)

	def _create_attribute(self, product_dict):
		attribute = []
		for attr in product_dict.get("options"):
			if not frappe.db.get_value("Item Attribute", attr.get("name"), "name"):
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": attr.get("name"),
						"item_attribute_values": [
							{"attribute_value": attr_value, "abbr": attr_value}
							for attr_value in attr.get("values")
						],
					}
				).insert()
				attribute.append({"attribute": attr.get("name")})

			else:
				# check for attribute values
				item_attr = frappe.get_doc("Item Attribute", attr.get("name"))
				if not item_attr.numeric_values:
					self._set_new_attribute_values(item_attr, attr.get("values"))
					item_attr.save()
					attribute.append({"attribute": attr.get("name")})

				else:
					attribute.append(
						{
							"attribute": attr.get("name"),
							"from_range": item_attr.get("from_range"),
							"to_range": item_attr.get("to_range"),
							"increment": item_attr.get("increment"),
							"numeric_values": item_attr.get("numeric_values"),
						}
					)

		return attribute

	def _set_new_attribute_values(self, item_attr, values):
		for attr_value in values:
			if not any(
				(d.abbr.lower() == attr_value.lower() or d.attribute_value.lower() == attr_value.lower())
				for d in item_attr.item_attribute_values
			):
				item_attr.append("item_attribute_values", {"attribute_value": attr_value, "abbr": attr_value})

	def _create_item(self, product_dict, warehouse, has_variant=0, attributes=None, variant_of=None):
		item_dict = {
			"variant_of": variant_of,
			"is_stock_item": 1,
			"item_code": cstr(product_dict.get("item_code")) or cstr(product_dict.get("id")),
			"item_name": product_dict.get("title", "").strip(),
			"description": product_dict.get("body_html") or product_dict.get("title"),
			"item_group": self._get_item_group(product_dict.get("product_type")),
			"has_variants": has_variant,
			"attributes": attributes or [],
			"stock_uom": product_dict.get("uom") or _("Nos"),
			"sku": product_dict.get("sku") or _get_sku(product_dict),
			"default_warehouse": warehouse,
			"image": _get_item_image(product_dict),
			"weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP[product_dict.get("weight_unit")],
			"weight_per_unit": product_dict.get("weight"),
			"default_supplier": self._get_supplier(product_dict),
		}

		integration_item_code = product_dict["id"]  # shopify product_id
		variant_id = product_dict.get("variant_id", "")  # shopify variant_id if has variants
		sku = item_dict["sku"]

		if not _match_sku_and_link_item(
			item_dict, integration_item_code, variant_id, variant_of=variant_of, has_variant=has_variant
		):
			ecommerce_item.create_ecommerce_item(
				MODULE_NAME,
				integration_item_code,
				item_dict,
				variant_id=variant_id,
				sku=sku,
				variant_of=variant_of,
				has_variants=has_variant,
			)

	def _create_item_variants(self, product_dict, warehouse, attributes):
		template_item = ecommerce_item.get_erpnext_item(
			MODULE_NAME, integration_item_code=product_dict.get("id"), has_variants=1
		)

		if template_item:
			for variant in product_dict.get("variants"):
				shopify_item_variant = {
					"id": product_dict.get("id"),
					"variant_id": variant.get("id"),
					"item_code": variant.get("id"),
					"title": product_dict.get("title", "").strip() + "-" + variant.get("title"),
					"product_type": product_dict.get("product_type"),
					"sku": variant.get("sku"),
					"uom": template_item.stock_uom or _("Nos"),
					"item_price": variant.get("price"),
					"weight_unit": variant.get("weight_unit"),
					"weight": variant.get("weight"),
				}

				for i, variant_attr in enumerate(SHOPIFY_VARIANTS_ATTR_LIST):
					if variant.get(variant_attr):
						attributes[i].update(
							{
								"attribute_value": self._get_attribute_value(
									variant.get(variant_attr), attributes[i]
								)
							}
						)
				self._create_item(shopify_item_variant, warehouse, 0, attributes, template_item.name)

	def _get_attribute_value(self, variant_attr_val, attribute):
		attribute_value = frappe.db.sql(
			"""select attribute_value from `tabItem Attribute Value`
			where parent = %s and (abbr = %s or attribute_value = %s)""",
			(attribute["attribute"], variant_attr_val, variant_attr_val),
			as_list=1,
		)
		return attribute_value[0][0] if len(attribute_value) > 0 else cint(variant_attr_val)

	def _get_item_group(self, product_type=None):
		parent_item_group = get_root_of("Item Group")

		if not product_type:
			return parent_item_group

		if frappe.db.get_value("Item Group", product_type, "name"):
			return product_type
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": product_type,
				"parent_item_group": parent_item_group,
				"is_group": "No",
			}
		).insert()
		return item_group.name

	def _get_supplier(self, product_dict):
		if product_dict.get("vendor"):
			supplier = frappe.db.sql(
				f"""select name from tabSupplier
				where name = %s or {SUPPLIER_ID_FIELD} = %s """,
				(product_dict.get("vendor"), product_dict.get("vendor").lower()),
				as_list=1,
			)

			if supplier:
				return product_dict.get("vendor")
			supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": product_dict.get("vendor"),
					SUPPLIER_ID_FIELD: product_dict.get("vendor").lower(),
					"supplier_group": self._get_supplier_group(),
				}
			).insert()
			return supplier.name
		else:
			return ""

	def _get_supplier_group(self):
		supplier_group = frappe.db.get_value("Supplier Group", _("Shopify Supplier"))
		if not supplier_group:
			supplier_group = frappe.get_doc(
				{"doctype": "Supplier Group", "supplier_group_name": _("Shopify Supplier")}
			).insert()
			return supplier_group.name
		return supplier_group


def _add_weight_details(product_dict):
	variants = product_dict.get("variants")
	if variants:
		product_dict["weight"] = variants[0]["weight"]
		product_dict["weight_unit"] = variants[0]["weight_unit"]


def _has_variants(product_dict) -> bool:
	options = product_dict.get("options")
	return bool(options and "Default Title" not in options[0]["values"])


def _get_sku(product_dict):
	if product_dict.get("variants"):
		return product_dict.get("variants")[0].get("sku")
	return ""


def _get_item_image(product_dict):
	if product_dict.get("image"):
		return product_dict.get("image").get("src")
	return None


def _match_sku_and_link_item(item_dict, product_id, variant_id, variant_of=None, has_variant=False) -> bool:
	"""Tries to match new item with existing item using Shopify SKU == item_code.

	Returns true if matched and linked.
	"""
	sku = item_dict["sku"]
	if not sku or variant_of or has_variant:
		return False

	item_name = frappe.db.get_value("Item", {"item_code": sku})
	if item_name:
		try:
			ecommerce_item = frappe.get_doc(
				{
					"doctype": "Ecommerce Item",
					"integration": MODULE_NAME,
					"erpnext_item_code": item_name,
					"integration_item_code": product_id,
					"has_variants": 0,
					"variant_id": cstr(variant_id),
					"sku": sku,
				}
			)

			ecommerce_item.insert()
			return True
		except Exception:
			return False


def create_items_if_not_exist(order):
	"""Using shopify order, sync all items that are not already synced."""
	for item in order.get("line_items", []):
		product_id = item["product_id"]
		variant_id = item.get("variant_id")
		sku = item.get("sku")
		product = ShopifyProduct(product_id, variant_id=variant_id, sku=sku)

		if not product.is_synced():
			product.sync_product()


def get_item_code(shopify_item):
	"""Get item code using shopify_item dict.

	Item should contain both product_id and variant_id."""

	item = ecommerce_item.get_erpnext_item(
		integration=MODULE_NAME,
		integration_item_code=shopify_item.get("product_id"),
		variant_id=shopify_item.get("variant_id"),
		sku=shopify_item.get("sku"),
	)
	if item:
		return item.item_code


@temp_shopify_session
def upload_erpnext_item(doc, method=None):
	"""This hook is called when inserting new or updating existing `Item`.

	New items are pushed to shopify and changes to existing items are
	updated depending on what is configured in "Shopify Setting" doctype.
	"""
	template_item = item = doc  # alias for readability
	# a new item recieved from ecommerce_integrations is being inserted
	if item.flags.from_integration:
		return

	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.upload_erpnext_items:
		return
	
	# Check if item is marked for Shopify sync
	if not item.get(ITEM_SYNC_CHECKBOX):
		return

	if frappe.flags.in_import:
		return

	if item.has_variants:
		return

	if len(item.attributes) > 3:
		msgprint(_("Template items/Items with 4 or more attributes can not be uploaded to Shopify."))
		return

	if doc.variant_of and not setting.upload_variants_as_items:
		msgprint(_("Enable variant sync in setting to upload item to Shopify."))
		return

	if item.variant_of:
		template_item = frappe.get_doc("Item", item.variant_of)

	product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": template_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	is_new_product = not bool(product_id)

	if is_new_product:
		product = Product()
		product.published = False
		product.status = "active" if setting.sync_new_item_as_active else "draft"

		map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
		is_successful = product.save()

		if is_successful:
			update_default_variant_properties(
				product,
				sku=template_item.item_code,
				price=template_item.get(ITEM_SELLING_RATE_FIELD),
				is_stock_item=template_item.is_stock_item,
			)
			if item.variant_of:
				product.options = []
				product.variants = []
				variant_attributes = {
					"title": template_item.item_name,
					"sku": item.item_code,
					"price": item.get(ITEM_SELLING_RATE_FIELD),
				}
				max_index_range = min(3, len(template_item.attributes))
				for i in range(0, max_index_range):
					attr = template_item.attributes[i]
					product.options.append(
						{
							"name": attr.attribute,
							"values": frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							),
						}
					)
					try:
						variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
					except IndexError:
						frappe.throw(
							_("Shopify Error: Missing value for attribute {}").format(attr.attribute)
						)
				product.variants.append(Variant(variant_attributes))

			product.save()  # push variant

			# Sync lead time metafield after product is saved
			sync_lead_time_metafield(product, template_item)

			ecom_items = list(set([item, template_item]))
			for d in ecom_items:
				ecom_item = frappe.get_doc(
					{
						"doctype": "Ecommerce Item",
						"erpnext_item_code": d.name,
						"integration": MODULE_NAME,
						"integration_item_code": str(product.id),
						"variant_id": "" if d.has_variants else str(product.variants[0].id),
						"sku": "" if d.has_variants else str(product.variants[0].sku),
						"has_variants": d.has_variants,
						"variant_of": d.variant_of,
					}
				)
				ecom_item.insert()

		write_upload_log(status=is_successful, product=product, item=item)
	elif setting.update_shopify_item_on_update:
		product = Product.find(product_id)
		if product:
			map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
			if not item.variant_of:
				update_default_variant_properties(
					product,
					is_stock_item=template_item.is_stock_item,
					price=item.get(ITEM_SELLING_RATE_FIELD),
				)
			else:
				# Get the existing variant_id for this ERPNext item
				existing_variant_id = frappe.db.get_value(
					"Ecommerce Item",
					{"erpnext_item_code": item.name, "integration": MODULE_NAME},
					"variant_id",
				)

				if existing_variant_id:
					# Find and update the existing variant
					for variant in product.variants:
						if str(variant.id) == str(existing_variant_id):
							variant.price = item.get(ITEM_SELLING_RATE_FIELD)
							variant.sku = item.item_code
							break
				else:
					# If no variant_id exists, this is a new variant being added to existing product
					variant_attributes = {"sku": item.item_code, "price": item.get(ITEM_SELLING_RATE_FIELD)}
					product.options = []
					max_index_range = min(3, len(template_item.attributes))
					for i in range(0, max_index_range):
						attr = template_item.attributes[i]
						product.options.append(
							{
								"name": attr.attribute,
								"values": frappe.db.get_all(
									"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
								),
							}
						)
						try:
							variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
						except IndexError:
							frappe.throw(
								_("Shopify Error: Missing value for attribute {}").format(attr.attribute)
							)
					product.variants.append(Variant(variant_attributes))

			is_successful = product.save()
			if is_successful:
				# Sync lead time metafield after product update
				sync_lead_time_metafield(product, template_item)

				if item.variant_of and not existing_variant_id:
					map_erpnext_variant_to_shopify_variant(product, item, variant_attributes)

			write_upload_log(status=is_successful, product=product, item=item, action="Updated")


def map_erpnext_variant_to_shopify_variant(shopify_product: Product, erpnext_item, variant_attributes):
	variant_product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": erpnext_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	if not variant_product_id:
		for variant in shopify_product.variants:
			if (
				variant.option1 == variant_attributes.get("option1")
				and variant.option2 == variant_attributes.get("option2")
				and variant.option3 == variant_attributes.get("option3")
			):
				variant_product_id = str(variant.id)
				if not frappe.flags.in_test:
					frappe.get_doc(
						{
							"doctype": "Ecommerce Item",
							"erpnext_item_code": erpnext_item.name,
							"integration": MODULE_NAME,
							"integration_item_code": str(shopify_product.id),
							"variant_id": variant_product_id,
							"sku": str(variant.sku),
							"variant_of": erpnext_item.variant_of,
						}
					).insert()
				break
		if not variant_product_id:
			msgprint(_("Shopify: Couldn't sync item variant."))
	return variant_product_id


def map_erpnext_item_to_shopify(shopify_product: Product, erpnext_item):
	"""Map erpnext fields to shopify, called both when updating and creating new products.

	Note: Description (body_html) is NOT synced to preserve Shopify's product descriptions.
	"""

	shopify_product.title = erpnext_item.item_name
	# Description is intentionally NOT synced - preserving Shopify's description
	# shopify_product.body_html = erpnext_item.description
	shopify_product.product_type = erpnext_item.item_group

	if erpnext_item.weight_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.values():
		# reverse lookup for key
		uom = get_shopify_weight_uom(erpnext_weight_uom=erpnext_item.weight_uom)
		shopify_product.weight = erpnext_item.weight_per_unit
		shopify_product.weight_unit = uom

	if erpnext_item.disabled:
		shopify_product.status = "draft"
		shopify_product.published = False
		msgprint(_("Status of linked Shopify product is changed to Draft."))


def sync_lead_time_metafield(shopify_product: Product, erpnext_item):
	"""Sync lead time from ERPNext item to Shopify product metafield using GraphQL.

	Syncs the 'lead_time_days' field from ERPNext Item to a Shopify metafield
	under the 'custom' namespace with key 'lead_time_days'.

	Note: This must be called AFTER the product is saved to ensure product.id exists.
	Uses GraphQL metafieldsSet mutation for efficient upsert operation.
	"""
	lead_time = erpnext_item.get("lead_time_days")

	if lead_time is None:
		return

	# Product must be saved first to have an ID
	if not shopify_product.id:
		return

	# Prepare GraphQL mutation for metafield upsert
	# Using metafieldsSet mutation which handles both create and update
	mutation = """
	mutation SetProductMetafield($metafields: [MetafieldsSetInput!]!) {
		metafieldsSet(metafields: $metafields) {
			metafields {
				id
				namespace
				key
				value
				type
			}
			userErrors {
				field
				message
			}
		}
	}
	"""

	# Prepare variables
	# Global ID format: gid://shopify/Product/{product_id}
	owner_id = f"gid://shopify/Product/{shopify_product.id}"

	variables = {
		"metafields": [
			{
				"ownerId": owner_id,
				"namespace": "custom",
				"key": "lead_time_days",
				"value": str(int(lead_time)),
				"type": "number_integer"
			}
		]
	}

	# Request data for logging
	request_data = {
		"mutation": mutation,
		"variables": variables,
		"item_code": erpnext_item.item_code,
		"item_name": erpnext_item.item_name,
		"lead_time_days": lead_time,
		"shopify_product_id": shopify_product.id,
	}

	try:
		# Execute GraphQL mutation
		result = shopify.GraphQL().execute(mutation, variables=variables)

		# Parse response
		import json
		response = json.loads(result)

		if "errors" in response:
			# GraphQL-level errors
			error_messages = [error.get("message", str(error)) for error in response["errors"]]
			error_msg = f"GraphQL errors: {', '.join(error_messages)}"

			create_shopify_log(
				status="Error",
				request_data=request_data,
				response_data=response,
				message=f"Failed to sync lead time metafield for product {shopify_product.id} (Item: {erpnext_item.item_code}). {error_msg}",
				method="sync_lead_time_metafield",
			)

		elif response.get("data", {}).get("metafieldsSet", {}).get("userErrors"):
			# User errors from the mutation
			user_errors = response["data"]["metafieldsSet"]["userErrors"]
			error_messages = [f"{err.get('field', 'unknown')}: {err.get('message', str(err))}" for err in user_errors]
			error_msg = f"User errors: {', '.join(error_messages)}"

			create_shopify_log(
				status="Error",
				request_data=request_data,
				response_data=response,
				message=f"Failed to sync lead time metafield for product {shopify_product.id} (Item: {erpnext_item.item_code}). {error_msg}",
				method="sync_lead_time_metafield",
			)

		else:
			# Success
			metafields = response.get("data", {}).get("metafieldsSet", {}).get("metafields", [])
			if metafields:
				metafield_data = metafields[0]
				create_shopify_log(
					status="Success",
					request_data=request_data,
					response_data=response,
					message=f"Successfully synced lead time metafield for product {shopify_product.id} (Item: {erpnext_item.item_code}). "
							f"Metafield: {metafield_data.get('namespace')}.{metafield_data.get('key')} = {metafield_data.get('value')} days",
					method="sync_lead_time_metafield",
				)
			else:
				# Unexpected: no errors but no metafields returned
				create_shopify_log(
					status="Error",
					request_data=request_data,
					response_data=response,
					message=f"Unexpected response when syncing lead time metafield for product {shopify_product.id} (Item: {erpnext_item.item_code}). "
							f"No errors but no metafields returned.",
					method="sync_lead_time_metafield",
				)

	except Exception as e:
		error_msg = str(e)
		create_shopify_log(
			status="Error",
			request_data=request_data,
			response_data={"exception": error_msg},
			message=f"Exception while syncing lead time metafield for product {shopify_product.id} (Item: {erpnext_item.item_code}). Error: {error_msg}",
			method="sync_lead_time_metafield",
		)


def get_shopify_weight_uom(erpnext_weight_uom: str) -> str:
	for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
		if erpnext_uom == erpnext_weight_uom:
			return shopify_uom


def update_default_variant_properties(
	shopify_product: Product,
	is_stock_item: bool,
	sku: str | None = None,
	price: float | None = None,
):
	"""Shopify creates default variant upon saving the product.

	Some item properties are supposed to be updated on the default variant.
	Input: saved shopify_product, sku and price
	"""
	default_variant: Variant = shopify_product.variants[0]

	# this will create Inventory item and qty will be updated by scheduled job.
	if is_stock_item:
		default_variant.inventory_management = "shopify"

	if price is not None:
		default_variant.price = price
	if sku is not None:
		default_variant.sku = sku


def write_upload_log(status: bool, product: Product, item, action="Created") -> None:
	if not status:
		msg = _("Failed to upload item to Shopify") + "<br>"
		msg += _("Shopify reported errors:") + " " + ", ".join(product.errors.full_messages())
		msgprint(msg, title="Note", indicator="orange")

		create_shopify_log(
			status="Error",
			request_data=product.to_dict(),
			message=msg,
			method="upload_erpnext_item",
		)
	else:
		create_shopify_log(
			status="Success",
			request_data=product.to_dict(),
			message=f"{action} Item: {item.name}, shopify product: {product.id}",
			method="upload_erpnext_item",
		)
