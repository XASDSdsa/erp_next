"""Native Shipment contents read from explicit, permission-checked Delivery Notes."""

import json

import frappe
from frappe import _
from frappe.utils import flt


def _sales_order_delivery_date(item):
	"""An item reference does not grant access to its source Sales Order."""
	detail = frappe.db.get_value(
		"Sales Order Item", item.get("so_detail"),
		["parent", "parenttype", "delivery_date"], as_dict=True,
	)
	if not detail or detail.get("parenttype") != "Sales Order":
		return None
	if item.get("against_sales_order") and item.get("against_sales_order") != detail.get("parent"):
		return None
	if not frappe.has_permission("Sales Order", "read", doc=detail.get("parent")):
		return None
	return detail.get("delivery_date")


def goods_from_delivery_notes(names) -> list[dict]:
	from erpnext.stock.doctype.shipment.shipment_stickers import stickers_for_delivery_note

	out = []
	seen = set()
	for name in names or []:
		name = str(name or "").strip()
		if not name or name in seen or not frappe.db.exists("Delivery Note", name):
			continue
		seen.add(name)
		dn = frappe.get_doc("Delivery Note", name)
		dn.check_permission("read")
		stickers = stickers_for_delivery_note(dn)
		for item in dn.get("items") or []:
			qty = flt(item.qty)
			delivery_date = item.get("delivery_date")
			if not delivery_date and item.get("so_detail"):
				delivery_date = _sales_order_delivery_date(item)
			if not delivery_date:
				delivery_date = dn.get("posting_date")
			out.append(
				{
					"delivery_note": dn.name,
					"item_code": item.item_code or "",
					"item_name": (item.item_name or item.item_code or "").strip(),
					"qty": qty,
					"uom": item.uom or "",
					"warehouse": item.warehouse or "",
					"set_warehouse": dn.get("set_warehouse") or item.warehouse or "",
					"delivery_date": delivery_date,
					"rate": flt(item.rate),
					"amount": flt(item.amount),
					"currency": dn.currency or "",
					**({"stickers": stickers[item.name]} if stickers.get(item.name) else {}),
				}
			)
	return out


def source_warehouse(goods: list[dict]) -> str:
	for row in goods or []:
		name = (row.get("set_warehouse") or row.get("warehouse") or "").strip()
		if name:
			return name
	return ""


def goods_summary(goods: list[dict]) -> str:
	parts = []
	for row in goods or []:
		qty = flt(row.get("qty"))
		qty_text = str(int(qty)) if qty == int(qty) else str(qty)
		name = (row.get("item_name") or row.get("item_code") or "").strip()
		if not name:
			continue
		uom = (row.get("uom") or "").strip()
		parts.append(f"{name} × {qty_text}{(' ' + uom) if uom else ''}")
	return ", ".join(parts)[:140]


def warehouse_from_delivery_note(doc):
	if doc.get("set_warehouse"):
		return doc.get("set_warehouse")
	counts = {}
	for row in doc.get("items") or []:
		if row.get("warehouse"):
			counts[row.get("warehouse")] = counts.get(row.get("warehouse"), 0) + 1
	return max(counts, key=counts.get) if counts else None


def _grouped_contents(delivery_notes):
	groups = {}
	for item in goods_from_delivery_notes(delivery_notes):
		name = item["delivery_note"]
		group = groups.setdefault(name, {
			"delivery_note": name, "currency": item["currency"], "warehouse": "", "items": [],
		})
		group["items"].append(item)
	for group in groups.values():
		group["warehouse"] = source_warehouse(group["items"])
	return list(groups.values())


def _document_delivery_notes(doc):
	return [row.get("delivery_note") for row in doc.get("shipment_delivery_note") or [] if row.get("delivery_note")]


@frappe.whitelist()
def get_shipment_contents(shipment=None, delivery_notes=None):
	"""Read saved Shipment contents, or explicit Delivery Notes for an unsaved form."""
	if shipment:
		doc = frappe.get_doc("Shipment", shipment)
		doc.check_permission("read")
		names = _document_delivery_notes(doc)
	else:
		frappe.has_permission("Shipment", "read", throw=True)
		names = json.loads(delivery_notes) if isinstance(delivery_notes, str) else delivery_notes or []
		if not isinstance(names, (list, tuple)):
			frappe.throw(_("Delivery Notes must be provided as a list."))
	return _grouped_contents(names)


def load_shipment_contents(doc, method=None):
	"""Load generic goods once with the form; related permissions stay explicit."""
	doc.check_permission("read")
	try:
		contents = _grouped_contents(_document_delivery_notes(doc))
	except frappe.PermissionError:
		doc.set_onload("shipment_contents", [])
		doc.set_onload("shipment_contents_restricted", {
			"message": _("You do not have permission to view the linked Delivery Note contents."),
		})
	else:
		doc.set_onload("shipment_contents", contents)
		doc.set_onload("shipment_contents_restricted", None)
