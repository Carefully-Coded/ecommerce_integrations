frappe.ui.form.on("Item", {
	refresh(frm) {
		if (frm.doc.sync_with_unicommerce) {
			frm.add_custom_button(
				__("Open Unicommerce Item"),
				function () {
					frappe.call({
						method: "ecommerce_integrations.unicommerce.utils.get_unicommerce_document_url",
						args: {
							code: frm.doc.item_code,
							doctype: frm.doc.doctype,
						},
						callback: function (r) {
							if (!r.exc) {
								window.open(r.message, "_blank");
							}
						},
					});
				},
				__("Unicommerce")
			);
		}

		// Load manufacturer details from Item Manufacturer table
		load_manufacturer_details(frm);
	},
});

function load_manufacturer_details(frm) {
	if (!frm.doc.name || frm.doc.__islocal) return;

	// Get the default Item Manufacturer record
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "Item Manufacturer",
			filters: {
				item_code: frm.doc.name,
				is_default: 1,
			},
			fields: ["manufacturer", "manufacturer_part_no"],
			limit: 1,
		},
		callback: function (r) {
			if (r.message && r.message.length > 0) {
				let item_mfg = r.message[0];
				frm.set_value("manufacturer_name", item_mfg.manufacturer);
				frm.set_value("manufacturer_part_number", item_mfg.manufacturer_part_no);
			}
		},
	});
}
