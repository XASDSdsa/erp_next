"""Shipment owns derived display fields; carriers provide only their own values."""

import frappe
from frappe.utils import cint

from erpnext.stock.doctype.shipment.carriers import get_carrier
from erpnext.stock.doctype.shipment.manual_shipping import is_manual

DISPLAY_FIELDS = (
	"transport_status_display",
	"freight_status_display",
	"interception_status_display",
	"label_replacement_display",
)
TRANSPORT_LABELS = {
	"In Progress": "运输中",
	"Shipped": "已发货",
	"Booked": "已下单",
	"Delivered": "已送达",
	"Returned": "已退回",
	"Lost": "已丢失",
}


def display_values(doc):
	waybill = str(doc.get("shipment_id") or doc.get("awb_number") or "").strip()
	values = {
		"transport_status_display": TRANSPORT_LABELS.get(
			doc.get("tracking_status"), doc.get("tracking_status") or ""
		),
		"freight_status_display": "",
		"interception_status_display": "",
		"label_replacement_display": f"当前 {waybill}" if waybill else "",
	}
	if is_manual(doc):
		values["transport_status_display"] = str(doc.get("manual_transport_status") or "")
		values["freight_status_display"] = (
			(doc.get("manual_settlement_status") or "待结算")
			if cint(doc.get("manual_freight_recorded")) == 1
			else "运费待登记"
		)
	else:
		carrier = get_carrier(doc)
		if carrier:
			# An adapter returns display data, never generic lifecycle decisions.
			values.update(carrier.get_display_values(doc))
	return {fieldname: values[fieldname] for fieldname in DISPLAY_FIELDS}


def set_display_fields(doc):
	for fieldname, value in display_values(doc).items():
		doc.set(fieldname, value)


def persist_display_fields(doc):
	if not doc.get("name"):
		return
	values = display_values(doc)
	changed = {fieldname: value for fieldname, value in values.items() if (doc.get(fieldname) or "") != value}
	if changed:
		# These are derived fields, so update directly without recursively invoking
		# Document.db_set -> on_change or modifying the business audit timestamp.
		frappe.db.set_value("Shipment", doc.name, changed, update_modified=False)
		for fieldname, value in changed.items():
			doc.set(fieldname, value)


def backfill_shipment_display_fields():
	for name in frappe.get_all("Shipment", pluck="name"):
		persist_display_fields(frappe.get_doc("Shipment", name))
