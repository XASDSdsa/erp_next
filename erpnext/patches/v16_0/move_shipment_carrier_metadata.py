"""Move generic Shipment metadata to ERPNext without rewriting shipment records.

Run this pre-model-sync patch directly for a narrow deployment:
bench --site <site> execute erpnext.patches.v16_0.move_shipment_carrier_metadata.execute
Then run each installed carrier's own metadata synchronization if required.
"""

import json
from pathlib import Path

import frappe


EDITABLE_SHIPMENT_FIELDS = (
	"shipment_parcel", "parcel_template", "shipment_id", "awb_number", "carrier",
	"carrier_service", "service_provider", "description_of_content", "tracking_status",
	"tracking_url", "tracking_status_info", "shipment_amount", "status",
)
PARCEL_FIELDS = ("length", "width", "height", "weight", "count")
LEGACY_PROVIDER_OPTIONS = (
	"\n顺丰国际\nLetMeShip\nSendCloud",
	"\n顺丰国际\nLetMeShip\nSendCloud\n其他物流（手工登记）",
)
LEGACY_STATUS_OPTIONS = "Draft\nSubmitted\nBooked\nCancelled\nCompleted\n待打单发货\n已发货\n已取消发货"
# Custom DocPerm.export defaulted to 1 in the historical installer.
SALES_USER_RIGHTS = {"read", "write", "create", "submit", "share", "print", "email", "export"}
LEGACY_DELIVERY_NOTE_FIELDS = (
	{"fieldname": "sf_dn_actions_section", "label": "创建面单", "fieldtype": "Section Break", "collapsible": 0},
	{"fieldname": "sf_dn_actions_html", "label": " ", "fieldtype": "HTML"},
)
LEGACY_SALES_ORDER_LIST_SCRIPTS = ("Sales Order Payment List", "Sales Order Freight List")


def _native_manual_fields():
	path = Path(frappe.get_app_path("erpnext", "stock", "doctype", "shipment", "shipment.json"))
	return [field for field in json.loads(path.read_text())["fields"] if field["fieldname"].startswith("manual_")]


def _comparable(value, fieldtype):
	if fieldtype in ("Check", "Int"):
		return int(value or 0)
	return value or ""


def _check_custom_fields(native_fields, doctype="Shipment", first_after="service_provider"):
	"""Reject changed definitions before any mutation; Property Setters remain intact."""
	rows = frappe.get_all(
		"Custom Field",
		filters={"dt": doctype, "fieldname": ["in", [field["fieldname"] for field in native_fields]]},
		fields=["*"],
	)
	native = {field["fieldname"]: field for field in native_fields}
	custom_meta = frappe.get_meta("Custom Field")
	docfield_properties = {field.fieldname for field in frappe.get_meta("DocField").fields}
	properties = [
		field for field in custom_meta.fields
		if field.fieldname in docfield_properties
		and field.fieldname not in {"fieldname", "module", "idx"}
		and field.fieldtype not in {"Section Break", "Column Break", "Tab Break", "HTML", "Button"}
	]
	previous = first_after
	insert_after = {}
	for field in native_fields:
		insert_after[field["fieldname"]] = previous
		previous = field["fieldname"]
	conflicts = []
	for row in rows:
		expected = native[row["fieldname"]]
		changed = []
		if not row.get("is_system_generated"):
			changed.append("is_system_generated")
		if row.get("insert_after") != insert_after[row["fieldname"]]:
			changed.append("insert_after")
		for property_field in properties:
			key = property_field.fieldname
			default = property_field.get("default")
			if _comparable(row.get(key, default), property_field.fieldtype) != _comparable(
				expected.get(key, default), property_field.fieldtype
			):
				changed.append(key)
		if changed:
			conflicts.append(f"{row['name']}: {', '.join(changed)}")
	if conflicts:
		frappe.throw(
			"Shipping metadata migration stopped before making changes. Preserve these customized "
			"Custom Field definitions as Property Setters before retrying: " + "; ".join(conflicts)
		)
	return rows


def _check_delivery_note_fields():
	rows = _check_custom_fields(LEGACY_DELIVERY_NOTE_FIELDS, "Delivery Note", "tracking_status_info")
	if not rows:
		return []
	fieldnames = [row["fieldname"] for row in rows]
	overrides = frappe.get_all(
		"Property Setter",
		filters={"doc_type": "Delivery Note", "field_name": ["in", fieldnames]},
		fields=["name"],
	)
	if overrides:
		frappe.throw(
			"Delivery Note shipping metadata migration stopped before making changes. Move these "
			"customer overrides to shipping_section/shipping_details before retrying: "
			+ ", ".join(row["name"] for row in overrides)
		)
	layouts = frappe.get_all("DocType Layout", filters={"document_type": "Delivery Note"}, fields=["name"])
	if layouts:
		layout_fields = frappe.get_all(
			"DocType Layout Field",
			filters={"parent": ["in", [row["name"] for row in layouts]], "fieldname": ["in", fieldnames]},
			fields=["name"],
		)
		if layout_fields:
			frappe.throw(
				"Delivery Note shipping metadata migration stopped before making changes. Update these "
				"customer layout rows to shipping_section/shipping_details before retrying: "
				+ ", ".join(row["name"] for row in layout_fields)
			)
	return rows


def _legacy_property_setters():
	setters = [
		("Shipment", "heading_pickup_from", "fieldtype", "Section Break"),
		("Shipment", "heading_pickup_from", "label", "Pickup and Delivery Details"),
		("Shipment", "heading_pickup_from", "collapsible", "1"),
		("Shipment", "heading_pickup_from", "collapsible_depends_on", "eval:doc.__islocal"),
		("Shipment", "column_break_28", "fieldtype", "Section Break"),
		("Shipment", "column_break_28", "hidden", "0"),
		("Shipment", "service_provider", "fieldtype", "Select"),
		("Shipment", "service_provider", "read_only", "0"),
		("Shipment", "status", "options", LEGACY_STATUS_OPTIONS),
		("Shipment", None, "track_changes", "1"),
	]
	setters.extend(("Shipment", field, "allow_on_submit", "1") for field in EDITABLE_SHIPMENT_FIELDS)
	setters.extend(("Shipment Parcel", field, "allow_on_submit", "1") for field in PARCEL_FIELDS)
	setters.extend(("Shipment", "service_provider", "options", options) for options in LEGACY_PROVIDER_OPTIONS)
	return setters


def _remove_legacy_sales_permission():
	rows = frappe.get_all("Custom DocPerm", filters={"parent": "Shipment"}, fields=["*"])
	# Any custom permission rows replace the complete native permissions table.
	# Removing one row from a customer permission setup would remove its access.
	if len(rows) != 1:
		return []
	row = rows[0]
	if row.get("role") != "Sales User" or row.get("parenttype") != "DocType":
		return []
	for field in frappe.get_meta("Custom DocPerm").fields:
		if field.fieldtype in ("Check", "Int"):
			if int(row.get(field.fieldname) or 0) != int(field.fieldname in SALES_USER_RIGHTS):
				return []
	frappe.db.delete("Custom DocPerm", {"name": row["name"], "parent": "Shipment"})
	return [row["name"]]


def _sales_order_freight_fields():
	"""Prepare the saved column rename without changing the user's visible columns."""
	raw = frappe.db.get_value("List View Settings", "Sales Order", "fields")
	if not raw:
		return None
	fields = json.loads(raw)
	if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
		frappe.throw("Sales Order List View Settings fields must be a JSON list of column definitions.")
	changed = False
	for field in fields:
		if field.get("fieldname") == "sf_freight":
			field["fieldname"] = "shipment_freight"
			changed = True
	return json.dumps(fields, ensure_ascii=False) if changed else None


def _retire_sales_order_list_scripts():
	"""Retire only the two known list overrides now supplied by the native list."""
	rows = frappe.get_all(
		"Client Script",
		filters={
			"name": ["in", LEGACY_SALES_ORDER_LIST_SCRIPTS],
			"dt": "Sales Order", "view": "List", "enabled": 1,
		},
		fields=["name"],
	)
	for row in rows:
		frappe.db.set_value("Client Script", row["name"], "enabled", 0, update_modified=False)
	return [row["name"] for row in rows]


def execute():
	"""Idempotently transfer metadata; keep columns, unique indexes and business rows."""
	custom_fields = _check_custom_fields(_native_manual_fields())
	delivery_note_fields = _check_delivery_note_fields()
	sales_order_fields = _sales_order_freight_fields()
	removed_fields = []
	for row in [*custom_fields, *delivery_note_fields]:
		# CustomField.on_trash removes customer Property Setters and DocType Layout
		# references. Transfer ownership by deleting only the redundant metadata row.
		frappe.db.delete("Custom Field", {"name": row["name"], "dt": row["dt"]})
		removed_fields.append(row["name"])

	removed_setters = []
	for doctype, fieldname, property_name, expected in _legacy_property_setters():
		for row in frappe.get_all(
			"Property Setter",
			filters={"doc_type": doctype, "field_name": fieldname, "property": property_name},
			fields=["name", "value", "is_system_generated"],
		):
			if row.get("is_system_generated") and str(row.get("value") or "") == expected:
				frappe.db.delete("Property Setter", {"name": row["name"]})
				removed_setters.append(row["name"])

	removed_permissions = _remove_legacy_sales_permission()
	if sales_order_fields is not None:
		frappe.db.set_value("List View Settings", "Sales Order", "fields", sales_order_fields, update_modified=False)
	disabled_scripts = _retire_sales_order_list_scripts()
	if sales_order_fields is not None or disabled_scripts:
		frappe.clear_cache(doctype="Sales Order")
	frappe.clear_cache(doctype="Shipment")
	frappe.clear_cache(doctype="Shipment Parcel")
	frappe.reload_doc("stock", "doctype", "shipment_parcel", force=True)
	frappe.reload_doc("stock", "doctype", "shipment", force=True)
	frappe.clear_cache(doctype="Delivery Note")
	frappe.reload_doc("stock", "doctype", "delivery_note", force=True)
	frappe.db.add_unique("Shipment", ["manual_waybill_key"], constraint_name="manual_carrier_waybill_unique")
	frappe.clear_cache(doctype="Shipment")
	return {
		"removed_custom_fields": removed_fields,
		"removed_property_setters": removed_setters,
		"removed_custom_permissions": removed_permissions,
		"renamed_sales_order_freight_column": sales_order_fields is not None,
		"disabled_client_scripts": disabled_scripts,
	}
