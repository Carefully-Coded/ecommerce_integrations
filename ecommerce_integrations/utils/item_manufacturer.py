# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import frappe


def update_item_manufacturer_on_save(doc, method=None):
	"""
	Update Item Manufacturer table when Item is saved with manufacturer details.
	This is called via doc_events hook on Item before_save.
	Creates a new Item Manufacturer record if one doesn't exist.
	Deletes the Item Manufacturer record if manufacturer is removed.
	"""
	manufacturer = doc.get("manufacturer_name")
	part_no = doc.get("manufacturer_part_number")

	# If manufacturer is not set, delete any existing default Item Manufacturer records
	# and clear the part number field
	if not manufacturer:
		# Clear the part number field if manufacturer is removed
		if part_no:
			doc.manufacturer_part_number = None

		# Find and delete all default Item Manufacturer records for this item
		existing_records = frappe.db.get_all(
			"Item Manufacturer",
			filters={"item_code": doc.name, "is_default": 1},
			fields=["name"],
		)

		for record in existing_records:
			frappe.delete_doc("Item Manufacturer", record.name, ignore_permissions=True)

		return

	# Check if an Item Manufacturer record exists for this item and manufacturer
	existing = frappe.db.get_all(
		"Item Manufacturer",
		filters={"item_code": doc.name, "manufacturer": manufacturer},
		fields=["name", "manufacturer_part_no", "is_default"],
		limit=1,
	)

	if existing:
		# Update existing record
		item_mfg = existing[0]
		if item_mfg.manufacturer_part_no != (part_no or ""):
			frappe.db.set_value(
				"Item Manufacturer",
				item_mfg.name,
				{
					"manufacturer_part_no": part_no or "",
					"is_default": 1,
				},
			)
	else:
		# If manufacturer changed, delete old default manufacturer records first
		old_default_records = frappe.db.get_all(
			"Item Manufacturer",
			filters={"item_code": doc.name, "is_default": 1},
			fields=["name"],
		)

		for record in old_default_records:
			frappe.delete_doc("Item Manufacturer", record.name, ignore_permissions=True)

		# Create new Item Manufacturer record
		item_mfg = frappe.get_doc(
			{
				"doctype": "Item Manufacturer",
				"item_code": doc.name,
				"manufacturer": manufacturer,
				"manufacturer_part_no": part_no or "",
				"is_default": 1,
			}
		)
		item_mfg.insert(ignore_permissions=True)
