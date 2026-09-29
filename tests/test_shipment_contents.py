"""Native contents preserve explicit goods links and related-document permissions."""

import sys
import types
from unittest.mock import Mock

import pytest

from test_shipment_backend import Doc, backend


@pytest.fixture
def contents(backend, monkeypatch):
	item = Doc(name="ROW-1", item_code="GOODS-1", item_name="Goods", qty=2, uom="Nos",
		warehouse="ITEM-WAREHOUSE", rate=12, amount=24)
	dn = Doc(name="DN-1", currency="CNY", posting_date="2026-09-29", set_warehouse="DN-WAREHOUSE", items=[item])
	dn.check_permission = Mock()
	shipment = backend.shipment.Shipment(name="SHIPMENT-1", shipment_delivery_note=[Doc(delivery_note=dn.name)])
	shipment.check_permission = Mock()
	state = {}
	shipment.set_onload = lambda key, value: state.update({key: value})
	backend.frappe.db.exists.side_effect = lambda doctype, name: doctype == "Delivery Note" and name == dn.name
	backend.frappe.db.get_value.side_effect = None
	backend.frappe.db.get_value.return_value = None
	backend.frappe.get_doc = Mock(side_effect=lambda doctype, name: shipment if doctype == "Shipment" else dn)
	stickers = types.ModuleType("erpnext.stock.doctype.shipment.shipment_stickers")
	stickers.stickers_for_delivery_note = Mock(return_value={})
	monkeypatch.setitem(sys.modules, stickers.__name__, stickers)
	return Doc(backend=backend, dn=dn, item=item, shipment=shipment, state=state, stickers=stickers)


def test_native_onload_groups_complete_goods_and_permission_checked_stickers(contents):
	sticker = {"item_code": "STICKER-1", "image_status": "ready", "image_url": "/private/files/sticker.png"}
	contents.stickers.stickers_for_delivery_note.return_value = {"ROW-1": [sticker]}
	contents.shipment.onload()
	assert contents.state["shipment_contents"] == [{
		"delivery_note": "DN-1", "currency": "CNY", "warehouse": "DN-WAREHOUSE", "items": [{
			"delivery_note": "DN-1", "item_code": "GOODS-1", "item_name": "Goods", "qty": 2.0,
			"uom": "Nos", "warehouse": "ITEM-WAREHOUSE", "set_warehouse": "DN-WAREHOUSE",
			"delivery_date": "2026-09-29", "rate": 12.0, "amount": 24.0, "currency": "CNY", "stickers": [sticker],
		}],
	}]
	assert contents.state["shipment_contents_restricted"] is None
	contents.shipment.check_permission.assert_called_once_with("read")
	contents.dn.check_permission.assert_called_once_with("read")


def test_draft_contents_endpoint_checks_permissions_and_keeps_one_copy_of_each_source(contents):
	groups = contents.backend.shipment_contents.get_shipment_contents(delivery_notes='["DN-1", "DN-1"]')
	assert len(groups) == 1 and len(groups[0]["items"]) == 1
	contents.backend.frappe.has_permission.assert_called_once_with("Shipment", "read", throw=True)
	contents.backend.frappe.get_doc.assert_called_once_with("Delivery Note", "DN-1")


def test_saved_contents_uses_persisted_shipment_links(contents):
	groups = contents.backend.shipment_contents.get_shipment_contents(
		shipment="SHIPMENT-1", delivery_notes='["UNRELATED-DN"]'
	)
	assert groups[0]["delivery_note"] == "DN-1"
	contents.shipment.check_permission.assert_called_once_with("read")


def test_draft_contents_denied_before_delivery_note_reads(contents):
	contents.backend.frappe.has_permission.side_effect = PermissionError("Shipment")
	with pytest.raises(PermissionError):
		contents.backend.shipment_contents.get_shipment_contents(delivery_notes='["DN-1"]')
	contents.backend.frappe.get_doc.assert_not_called()


def test_restricted_related_note_is_explicit_and_exposes_no_contents(contents):
	contents.dn.check_permission.side_effect = PermissionError("Delivery Note")
	contents.shipment.onload()
	assert contents.state["shipment_contents"] == []
	assert contents.state["shipment_contents_restricted"]["message"]
	assert "GOODS-1" not in repr(contents.state)
	contents.stickers.stickers_for_delivery_note.assert_not_called()
	with pytest.raises(PermissionError):
		contents.backend.shipment_contents.get_shipment_contents(delivery_notes='["DN-1"]')


def test_unexpected_contents_errors_propagate(contents):
	contents.backend.frappe.get_doc.side_effect = RuntimeError("database failed")
	with pytest.raises(RuntimeError, match="database failed"):
		contents.shipment.onload()


@pytest.mark.parametrize("allowed", [True, False])
def test_source_order_delivery_date_requires_order_read_permission(contents, allowed):
	contents.item.so_detail = "SO-ROW-1"
	contents.item.against_sales_order = "SO-1"
	contents.backend.frappe.db.get_value.return_value = Doc(
		parent="SO-1", parenttype="Sales Order", delivery_date="2026-10-05"
	)
	contents.backend.frappe.has_permission.side_effect = lambda doctype, *args, **kwargs: allowed if doctype == "Sales Order" else True
	goods = contents.backend.shipment_contents.goods_from_delivery_notes(["DN-1"])
	assert goods[0]["delivery_date"] == ("2026-10-05" if allowed else "2026-09-29")
	contents.backend.frappe.has_permission.assert_called_once_with("Sales Order", "read", doc="SO-1")


def test_mismatched_source_order_row_cannot_supply_delivery_date(contents):
	contents.item.so_detail = "OTHER-ROW"
	contents.item.against_sales_order = "SO-1"
	contents.backend.frappe.db.get_value.return_value = Doc(
		parent="OTHER-ORDER", parenttype="Sales Order", delivery_date="SECRET-DATE"
	)
	goods = contents.backend.shipment_contents.goods_from_delivery_notes(["DN-1"])
	assert goods[0]["delivery_date"] == "2026-09-29"
	contents.backend.frappe.has_permission.assert_not_called()
