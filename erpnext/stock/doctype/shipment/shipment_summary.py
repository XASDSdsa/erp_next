"""Carrier-neutral helpers for Shipment summaries and linked documents."""

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
import json

import frappe
from frappe import _
from frappe.utils import cint


MANUAL_STATE_FIELDS = (
	"manual_carrier", "manual_waybill", "manual_destination", "manual_transport_status",
	"manual_freight_recorded", "manual_freight_amount", "manual_freight_currency",
	"manual_settlement_status",
)


def select_linked_shipment_row(rows: Iterable, *, include_cancelled: bool = False):
	"""Select the current Shipment row for a linked document.

	The core Shipment model owns identity and document lifecycle. Provider apps
	may add state to the rows, but must not replace this carrier-neutral choice.
	"""
	rows = list(rows or [])
	if not include_cancelled:
		rows = [row for row in rows if int(row.get("docstatus") or 0) != 2]
	if not rows:
		return None

	active = [row for row in rows if int(row.get("docstatus") or 0) != 2]
	candidates = active or rows
	booked = [row for row in candidates if row.get("shipment_id") or row.get("awb_number")]
	return (booked or candidates)[0]


def shipment_state_fields(doc):
	"""Summarize a readable Shipment without consulting any carrier configuration."""
	from erpnext.stock.doctype.shipment.carriers import get_carrier
	from erpnext.stock.doctype.shipment.manual_shipping import is_manual
	from erpnext.stock.doctype.shipment.shipment_display import display_values

	doc.check_permission("read")
	values = {
		"shipment": doc.name,
		"shipment_id": doc.get("shipment_id") or doc.get("awb_number"),
		"awb_number": doc.get("awb_number") or doc.get("shipment_id"),
		"service_provider": doc.get("service_provider"),
		"carrier": doc.get("carrier"),
		"carrier_service": doc.get("carrier_service"),
		"shipment_status": doc.get("status"),
		"shipment_docstatus": cint(doc.get("docstatus")),
		"shipment_amount": doc.get("shipment_amount"),
		"tracking_status": doc.get("tracking_status"),
		"tracking_status_info": doc.get("tracking_status_info"),
		"tracking_url": doc.get("tracking_url"),
		"is_manual": is_manual(doc),
		**{fieldname: doc.get(fieldname) for fieldname in MANUAL_STATE_FIELDS},
		**display_values(doc),
	}
	carrier = get_carrier(doc)
	extend = getattr(carrier, "get_delivery_note_summary", None) if carrier else None
	# Carrier data may extend a summary, but never replace its native identity.
	return {**(extend(doc) if extend else {}), **values}


def _blank_shipping_state():
	return {
		"submitted": False, "cancelled": False, "restricted": False, "message": "",
		"shipment": None, "historical_shipment": None, "shipment_id": None, "awb_number": None,
		"carrier": None, "service_provider": None, "shipment_status": None, "shipment_docstatus": None,
		"transport_status_display": "", "freight_status_display": "",
	}


def _delivery_note_shipping_state(doc):
	doc.check_permission("read")
	state = _blank_shipping_state()
	state["submitted"] = cint(doc.get("docstatus")) == 1
	state["cancelled"] = cint(doc.get("docstatus")) == 2 or bool(cint(doc.get("is_return")))
	if not doc.get("name"):
		return state
	parents = frappe.get_all(
		"Shipment Delivery Note", filters={"delivery_note": doc.name, "parenttype": "Shipment"},
		pluck="parent", distinct=True,
	)
	if not parents:
		return state
	# Select the actual current identity before checking its permissions. Filtering
	# by readable Shipments first could incorrectly substitute an older waybill.
	rows = frappe.get_all(
		"Shipment", filters={"name": ["in", parents]},
		fields=["name", "shipment_id", "awb_number", "docstatus", "modified"],
		order_by="modified desc, name asc",
	)
	active = select_linked_shipment_row(rows)
	historical = select_linked_shipment_row(
		[row for row in rows if cint(row.get("docstatus")) == 2], include_cancelled=True,
	)
	try:
		if active:
			state.update(shipment_state_fields(frappe.get_doc("Shipment", active.get("name"))))
		if historical:
			state["historical_shipment"] = shipment_state_fields(
				frappe.get_doc("Shipment", historical.get("name"))
			)
	except frappe.PermissionError:
		# Do not expose a name, waybill or financial state from a restricted record.
		state = {**_blank_shipping_state(), "submitted": state["submitted"], "cancelled": state["cancelled"]}
		state.update(
			restricted=True,
			message=_("You do not have permission to view the linked Shipment. Please contact your administrator."),
		)
	return state


@frappe.whitelist()
def get_delivery_note_shipping_state(delivery_note=None):
	if not delivery_note:
		frappe.has_permission("Delivery Note", "read", throw=True)
		return _blank_shipping_state()
	return _delivery_note_shipping_state(frappe.get_doc("Delivery Note", delivery_note))


def load_delivery_note_shipping_state(doc, method=None):
	"""Include generic state in the native form response before its first render."""
	doc.set_onload("shipping_state", _delivery_note_shipping_state(doc))


def manual_freight_summary(shipments):
	"""Total operator declarations by currency; this is not accounting evidence."""
	summary = {"total": 0, "recorded": 0, "settled": 0, "amounts": {}}
	for ship in shipments:
		summary["total"] += 1
		if not cint(ship.get("manual_freight_recorded")):
			continue
		raw_amount = ship.get("manual_freight_amount")
		currency = str(ship.get("manual_freight_currency") or "").strip().upper()
		if raw_amount in (None, "") or not currency:
			continue
		try:
			amount = Decimal(str(raw_amount))
		except (InvalidOperation, ValueError):
			continue
		if not amount.is_finite() or amount < 0:
			continue
		summary["recorded"] += 1
		summary["settled"] += int(ship.get("manual_settlement_status") == "已结算（人工确认）")
		summary["amounts"][currency] = summary["amounts"].get(currency, Decimal("0")) + amount
	summary["amounts"] = {currency: float(round(amount, 2)) for currency, amount in summary["amounts"].items()}
	return summary


@frappe.whitelist()
def get_sales_order_manual_freight_map(sales_orders=None):
	"""Read linked manual freight through readable orders, delivery notes and shipments."""
	from erpnext.stock.doctype.shipment.manual_shipping import PROVIDER

	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders)
	names = list(dict.fromkeys(str(name) for name in (sales_orders or []) if name))
	out = {name: manual_freight_summary([]) for name in names}
	if not names:
		return out
	for name in names:
		frappe.get_doc("Sales Order", name).check_permission("read")
	items = frappe.get_all(
		"Delivery Note Item", filters={"against_sales_order": ["in", names], "parenttype": "Delivery Note"},
		fields=["parent", "against_sales_order"],
	)
	delivery_notes = list({row.get("parent") for row in items if row.get("parent")})
	if not delivery_notes:
		return out
	readable_delivery_notes = set(frappe.get_list(
		"Delivery Note", filters={"name": ["in", delivery_notes]}, pluck="name", limit_page_length=0,
	))
	orders_by_delivery_note = {}
	for row in items:
		if row.get("parent") in readable_delivery_notes and row.get("against_sales_order") in out:
			orders_by_delivery_note.setdefault(row.get("parent"), set()).add(row.get("against_sales_order"))
	if not orders_by_delivery_note:
		return out
	links = frappe.get_all(
		"Shipment Delivery Note",
		filters={"delivery_note": ["in", list(orders_by_delivery_note)], "parenttype": "Shipment"},
		fields=["delivery_note", "parent"],
	)
	parent_names = list({row.get("parent") for row in links if row.get("parent")})
	if not parent_names:
		return out
	shipments = {
		row.name: row for row in frappe.get_list(
			"Shipment", filters={"name": ["in", parent_names], "service_provider": PROVIDER},
			fields=["name", "docstatus", *MANUAL_STATE_FIELDS], limit_page_length=0,
		)
	}
	shipments_by_order = {name: {} for name in names}
	for link in links:
		shipment = shipments.get(link.get("parent"))
		if not shipment:
			continue
		# Cancellation preserves incurred freight; an unpriced cancelled draft
		# does not represent pending freight. One package linked to multiple notes
		# on an order contributes its declared fee exactly once.
		if cint(shipment.get("docstatus")) == 2 and not cint(shipment.get("manual_freight_recorded")):
			continue
		for name in orders_by_delivery_note.get(link.get("delivery_note"), []):
			shipments_by_order[name][shipment.name] = shipment
	return {name: manual_freight_summary(rows.values()) for name, rows in shipments_by_order.items()}
