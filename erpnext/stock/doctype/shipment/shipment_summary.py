# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Carrier-neutral Shipment summaries loaded with Delivery Notes."""

import json

import frappe
from frappe import _
from frappe.desk import reportview
from frappe.utils import cint


SHIPMENT_FIELDS = (
	"name",
	"shipment_id",
	"awb_number",
	"status",
	"docstatus",
	"tracking_status",
	"tracking_status_info",
	"service_provider",
	"carrier",
	"modified",
)
PRESENTATION_FIELDS = ("status_label", "status_color", "extra_details")


def select_linked_shipment_row(rows, *, include_cancelled=False):
	"""Select the current native Shipment independently of any carrier integration."""
	# Sort before choosing: child-table link order does not identify the current shipment.
	rows = sorted(rows or [], key=lambda row: row.get("name") or row.get("shipment") or "")
	rows.sort(key=lambda row: str(row.get("modified") or ""), reverse=True)
	active = [row for row in rows if cint(row.get("docstatus")) != 2]
	if active:
		booked = [row for row in active if row.get("shipment_id") or row.get("awb_number")]
		return (booked or active)[0]
	# Cancelled history is informational; select its newest readable document.
	return rows[0] if include_cancelled and rows else None


def _empty_summary(*, source_cancelled=False):
	return {"shipments": [], "current_shipment": None, "source_cancelled": source_cancelled}


def _shipment_presentations(visible_rows):
	"""Let carrier apps decorate readable rows without changing native identities or state."""
	if not visible_rows:
		return {}
	visible_names = {row["name"] for row in visible_rows}
	presentations = {}
	for method in frappe.get_hooks("shipment_summary_enrichers"):
		# Do not pass mutable query rows to extensions: selection remains owned here.
		enriched = frappe.call(method, shipments=[dict(row) for row in visible_rows]) or {}
		for name, details in enriched.items():
			if name not in visible_names or not isinstance(details, dict):
				continue
			presentation = {
				field: details[field]
				for field in PRESENTATION_FIELDS
				if isinstance(details.get(field), str)
			}
			presentations.setdefault(name, {}).update(presentation)
	return presentations


def _shipping_rows(delivery_notes):
	"""Batch linked Shipments for Delivery Notes already authorized by the caller.

	Only permission-filtered Shipment rows are returned or passed to carrier hooks.
	Minimal unrestricted metadata is used solely to avoid presenting readable history
	as current when the actual active Shipment is inaccessible.
	"""
	result = {name: _empty_summary() for name in delivery_notes}
	if not result:
		return result

	links = frappe.get_all(
		"Shipment Delivery Note",
		filters={"delivery_note": ["in", list(result)], "parenttype": "Shipment"},
		fields=["delivery_note", "parent"],
		limit_page_length=0,
	)
	by_note = {}
	for row in links:
		if row.get("delivery_note") in result and row.get("parent"):
			by_note.setdefault(row["delivery_note"], set()).add(row["parent"])
	parents = sorted({name for names in by_note.values() for name in names})
	if not parents:
		return result

	all_rows = frappe.get_all(
		"Shipment",
		filters={"name": ["in", parents]},
		fields=["name", "shipment_id", "awb_number", "docstatus", "modified"],
		order_by="modified desc, name asc",
		limit_page_length=0,
	)
	try:
		visible_rows = frappe.get_list(
			"Shipment",
			filters={"name": ["in", parents]},
			fields=list(SHIPMENT_FIELDS),
			order_by="modified desc, name asc",
			limit_page_length=0,
		)
	except frappe.PermissionError:
		visible_rows = []
	visible_names = {row["name"] for row in visible_rows}
	presentations = _shipment_presentations(visible_rows)
	for name, linked in by_note.items():
		all_linked = [row for row in all_rows if row["name"] in linked]
		visible_linked = [row for row in visible_rows if row["name"] in linked]
		active = select_linked_shipment_row(all_linked)
		summary = result[name]
		for row in visible_linked:
			shipment = {field: row.get(field) for field in SHIPMENT_FIELDS if field != "modified"}
			shipment["shipment"] = row["name"]
			shipment["docstatus"] = cint(row.get("docstatus"))
			shipment.update(presentations.get(row["name"], {}))
			summary["shipments"].append(shipment)
		if active and active["name"] not in visible_names:
			summary["restricted_current"] = True
		elif visible_linked:
			current = active or select_linked_shipment_row(visible_linked, include_cancelled=True)
			summary["current_shipment"] = current["name"]
		elif all_linked:
			summary["restricted"] = True
	return result


def load_delivery_note_shipping_summary(doc):
	"""Add shipping state to the authorized native form response, before it renders."""
	summary = _empty_summary(source_cancelled=cint(doc.docstatus) == 2)
	if not doc.is_new():
		summary = _shipping_rows([doc.name])[doc.name]
		summary["source_cancelled"] = cint(doc.docstatus) == 2
	doc.set_onload("shipping_summary", summary)


@frappe.whitelist()
@frappe.read_only()
def get_delivery_notes():
	"""Return the authorized native list plus one batch of carrier-neutral shipping data."""
	if frappe.form_dict.get("doctype") != "Delivery Note":
		frappe.throw(_("Invalid DocType"), frappe.ValidationError)
	data = reportview.get()
	if not isinstance(data, dict) or "keys" not in data:
		return data
	keys = data.get("keys") or []
	values = data.get("values") or []
	if "name" not in keys:
		return data
	name_index = keys.index("name")
	names = [row[name_index] for row in values if row[name_index]]
	shipping = _shipping_rows(names)
	status_index = keys.index("docstatus") if "docstatus" in keys else None
	keys.append("_shipping")
	for row in values:
		summary = shipping.get(row[name_index], _empty_summary())
		if status_index is not None:
			summary["source_cancelled"] = cint(row[status_index]) == 2
		row.append(summary)
	data["keys"] = keys
	return data


def retire_legacy_delivery_note_shipment_scripts():
	"""Retire the exact database scripts replaced by the native Delivery Note UI."""
	legacy_views = {
		"Delivery Note Shipment List": "List",
		"Delivery Note Shipment Status": "Form",
	}
	rows = frappe.get_all(
		"Client Script",
		filters={"dt": "Delivery Note", "enabled": 1, "name": ["in", list(legacy_views)]},
		fields=["name", "view"],
	)
	disabled = []
	for row in rows:
		if legacy_views.get(row.name) == row.view:
			frappe.db.set_value("Client Script", row.name, "enabled", 0, update_modified=False)
			disabled.append(row.name)
	if disabled:
		frappe.clear_cache(doctype="Delivery Note")
	return {"disabled": disabled}


def migrate_delivery_note_shipment_presentation():
	"""Move the existing display-column setting to its native ERPNext identifier.

	List View Settings.fields owns list column order and width. Native per-user
	list preferences store filters and sorting, not these display-only columns.
	"""
	result = retire_legacy_delivery_note_shipment_scripts()
	result["column_migrated"] = False
	raw = frappe.db.get_value("List View Settings", "Delivery Note", "fields")
	if not raw:
		return result
	fields = json.loads(raw)
	for field in fields:
		if field.get("fieldname") == "sf_shipment":
			field["fieldname"] = "shipment"
			result["column_migrated"] = True
	if result["column_migrated"]:
		frappe.db.set_value(
			"List View Settings", "Delivery Note", "fields", json.dumps(fields), update_modified=False
		)
		frappe.clear_cache(doctype="Delivery Note")
	return result
