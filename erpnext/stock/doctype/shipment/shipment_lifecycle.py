"""Local lifecycle checks. Cancelling a source document never calls a carrier."""

import frappe
from frappe import _
from frappe.utils import cint


def validate_carrier_change(doc):
	"""Changing a selector must not detach an existing booking from its owner."""
	from erpnext.stock.doctype.shipment.carriers import get_carrier, has_carrier_booking

	previous = doc.get_doc_before_save()
	if not previous:
		return

	def provider(record):
		return str(record.get("service_provider") or record.get("carrier") or "").strip().casefold()

	if provider(previous) == provider(doc):
		return
	previous_carrier = get_carrier(previous)
	if previous_carrier is not None and previous_carrier is get_carrier(doc):
		# Equivalent provider aliases retain the same carrier and booking owner.
		return
	if previous.get("shipment_id") or previous.get("awb_number") or has_carrier_booking(previous):
		frappe.throw(_("This Shipment has a waybill or carrier booking history. Create a separate Shipment to change shipping providers."))


def _locked_rows(doctype, filters, fields):
	return frappe.db.get_values(doctype, filters, fields, as_dict=True, for_update=True, wait=False, order_by="name")


def _locked_document(doctype, name, fields):
	return frappe.db.get_value(doctype, name, fields, as_dict=True, for_update=True, wait=False)


def linked_delivery_notes(doc):
	names = [row.get("delivery_note") for row in doc.get("shipment_delivery_note") or [] if row.get("delivery_note")]
	if len(set(names)) != len(names):
		frappe.throw(_("The same Delivery Note cannot be linked twice to one Shipment."))
	return sorted(names)


def _sales_orders_for_delivery_note(name):
	rows = _locked_rows("Delivery Note Item", {"parent": name, "parenttype": "Delivery Note"}, ["against_sales_order"])
	return sorted({row.against_sales_order for row in rows if row.against_sales_order})


def _check_sales_orders(names, delivery_note):
	for name in sorted(set(names)):
		order = _locked_document("Sales Order", name, ["docstatus", "status"])
		if not order or cint(order.docstatus) == 2 or order.status == "Cancelled":
			frappe.throw(_("Delivery Note {0} is linked to cancelled or missing Sales Order {1}.").format(delivery_note, name))


def validate_delivery_note_orders(doc, method=None):
	if cint(doc.docstatus) == 2:
		return
	_check_sales_orders([row.get("against_sales_order") for row in doc.get("items") or [] if row.get("against_sales_order")], doc.name)


def validate_shipment_links(doc, method=None):
	# Only docstatus=2 proves that the local Shipment cancellation completed.
	# Carrier confirmation and display statuses must not unlock source documents.
	if cint(doc.docstatus) == 2:
		return
	for name in linked_delivery_notes(doc):
		note = _locked_document("Delivery Note", name, ["docstatus", "status"])
		if not note or cint(note.docstatus) != 1 or note.status == "Cancelled":
			frappe.throw(_("Delivery Note {0} must be submitted and active before using this shipment.").format(name))
		_check_sales_orders(_sales_orders_for_delivery_note(name), name)
		links = _locked_rows("Shipment Delivery Note", {"delivery_note": name, "parenttype": "Shipment"}, ["parent"])
		for link in links:
			if link.parent == doc.name:
				continue
			other = _locked_document("Shipment", link.parent, ["docstatus", "status"])
			if other and cint(other.docstatus) < 2:
				frappe.throw(_("Delivery Note {0} is already linked to active Shipment {1}.").format(name, link.parent))


def _active_shipments_for_delivery_note(name):
	links = _locked_rows("Shipment Delivery Note", {"delivery_note": name, "parenttype": "Shipment"}, ["parent"])
	active = []
	for shipment in sorted({row.parent for row in links}):
		row = _locked_document("Shipment", shipment, ["docstatus", "status"])
		if row and cint(row.docstatus) < 2:
			active.append(shipment)
	return active


def before_cancel_delivery_note(doc, method=None):
	active = _active_shipments_for_delivery_note(doc.name)
	if active:
		frappe.throw(_("Delivery Note {0} has active Shipment {1}. Cancel its carrier booking, then cancel or discard the local Shipment before cancelling this Delivery Note.").format(doc.name, active[0]))


def before_cancel_sales_order(doc, method=None):
	items = _locked_rows("Delivery Note Item", {"against_sales_order": doc.name, "parenttype": "Delivery Note"}, ["parent"])
	for name in sorted({row.parent for row in items}):
		note = _locked_document("Delivery Note", name, ["docstatus", "status"])
		active = _active_shipments_for_delivery_note(name)
		if active:
			frappe.throw(_("Sales Order {0} has active Shipment {1} through Delivery Note {2}. Resolve the Shipment before cancelling the order.").format(doc.name, active[0], name))
		if note and cint(note.docstatus) < 2 and note.status != "Cancelled":
			frappe.throw(_("Sales Order {0} has active Delivery Note {1}. Cancel or discard the Delivery Note first.").format(doc.name, name))
