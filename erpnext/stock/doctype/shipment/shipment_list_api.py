"""Permission-aware, carrier-neutral freight summaries for native Sales Order lists."""

import frappe
from frappe import _
from frappe.desk import reportview
from frappe.utils import cint, fmt_money

from erpnext.stock.doctype.shipment.carriers import iter_carriers
from erpnext.stock.doctype.shipment.shipment_summary import get_sales_order_manual_freight_map


def manual_freight_presentation(info):
	"""Manual declarations remain separate from carrier bills and accounting facts."""
	total = cint(info.get("total"))
	if not total:
		return None
	recorded = cint(info.get("recorded"))
	settled = cint(info.get("settled"))
	text = _("手工运费已登记") if recorded == total else _("手工运费待登记 {0}/{1}").format(total - recorded, total)
	amounts = " / ".join(
		f"{fmt_money(amount, currency=currency)} {currency}"
		for currency, amount in sorted((info.get("amounts") or {}).items())
	)
	extra = " · ".join(filter(None, [_("人工确认结算 {0}/{1}").format(settled, total), amounts]))
	return {
		"text": text,
		"color": "blue" if recorded == total else "orange",
		"extra": extra,
		"title": " · ".join([text, extra, _("人工登记，不代表会计已记账")]),
	}


def merge_freight_presentations(rows):
	"""Compose independent provider summaries without adding money or interpreting provider states."""
	rows = [row for row in rows if row and row.get("text")]
	if not rows:
		return {"text": "—", "color": "gray", "extra": "", "title": _("No freight information")}
	priority = {"gray": 0, "green": 1, "blue": 2, "orange": 3, "red": 4}
	color = max((row.get("color", "gray") for row in rows), key=lambda value: priority.get(value, 0))
	return {
		"text": " · ".join(str(row["text"]) for row in rows),
		"color": color if color in priority else "gray",
		"extra": " · ".join(str(row["extra"]) for row in rows if row.get("extra")),
		"title": " · ".join(str(row.get("title") or row["text"]) for row in rows),
	}


def get_sales_order_freight_summary(names):
	"""Installed carriers may supply presentation facts through an optional adapter method."""
	try:
		manual = get_sales_order_manual_freight_map(names)
		sources = [{name: manual_freight_presentation(manual.get(name) or {}) for name in names}]
		for carrier in iter_carriers():
			get_summary = getattr(carrier, "get_sales_order_freight_summary", None)
			if get_summary:
				sources.append(get_summary(names))
	except frappe.PermissionError:
		# Reading an order does not require warehouse access. Do not expose a
		# partial freight total when one of its contributing sources is restricted.
		return {
			name: {"text": _("无权查看运费"), "color": "gray", "extra": "", "title": ""}
			for name in names
		}
	return {
		name: merge_freight_presentations([source.get(name) for source in sources])
		for name in names
	}


@frappe.whitelist()
@frappe.read_only()
def get_sales_orders():
	"""Add freight to the native list response, preserving its query and permission behavior."""
	if frappe.form_dict.get("doctype") != "Sales Order":
		frappe.throw(_("Invalid DocType"), frappe.ValidationError)
	data = reportview.get()
	if not isinstance(data, dict) or "keys" not in data:
		return data
	keys = data.get("keys") or []
	values = data.get("values") or []
	if "name" not in keys or not values:
		return data
	name_index = keys.index("name")
	names = list(dict.fromkeys(row[name_index] for row in values if row[name_index]))
	freight = get_sales_order_freight_summary(names)
	keys.append("shipment_freight")
	for row in values:
		row.append(freight.get(row[name_index]))
	return data
