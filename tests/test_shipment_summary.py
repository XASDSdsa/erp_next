"""Permission and history boundaries of native Shipment summaries and manual fees."""

import sys
import types
from unittest.mock import Mock

import pytest

from test_shipment_backend import Doc, backend


class SummaryData:
	def __init__(self, backend):
		self.backend = backend
		self.records = {name: {} for name in ("Shipment", "Delivery Note", "Sales Order")}
		self.rows = {name: [] for name in ("Shipment Delivery Note", "Delivery Note Item")}
		backend.frappe.get_doc = Mock(side_effect=lambda doctype, name: self.records[doctype][name])
		backend.frappe.get_all = Mock(side_effect=self.get_all)
		backend.frappe.get_list = Mock(side_effect=lambda doctype, **kwargs: self.get_all(doctype, readable=True, **kwargs))

	def add(self, doctype, name, **fields):
		doc = Doc(name=name, docstatus=1, modified="2026-09-29 12:00:00")
		doc.update(fields)
		doc.check_permission = Mock(side_effect=PermissionError("denied") if doc.get("denied") else None)
		doc.set_onload = Mock()
		self.records[doctype][name] = doc
		return doc

	def link(self, delivery_note, shipment):
		self.rows["Shipment Delivery Note"].append(Doc(parent=shipment, parenttype="Shipment", delivery_note=delivery_note))

	def order_item(self, delivery_note, sales_order):
		self.rows["Delivery Note Item"].append(Doc(parent=delivery_note, parenttype="Delivery Note", against_sales_order=sales_order))

	def get_all(self, doctype, *, filters=None, fields=None, pluck=None, readable=False, distinct=False, **kwargs):
		rows = list(self.records[doctype].values()) if doctype in self.records else self.rows[doctype]
		for key, value in (filters or {}).items():
			rows = [row for row in rows if row.get(key) in value[1]] if isinstance(value, list) else [row for row in rows if row.get(key) == value]
		if readable:
			rows = [row for row in rows if not row.get("denied")]
		if kwargs.get("order_by"):
			rows = sorted(sorted(rows, key=lambda row: row.get("name")), key=lambda row: row.get("modified"), reverse=True)
		if pluck:
			values = [row.get(pluck) for row in rows]
			return list(dict.fromkeys(values)) if distinct else values
		return [Doc({field: row.get(field) for field in fields}) for row in rows]


@pytest.fixture
def data(backend):
	return SummaryData(backend)


def test_native_state_includes_current_and_cancelled_history_without_carrier_app(data):
	dn = data.add("Delivery Note", "DN-1")
	data.add("Shipment", "CURRENT", shipment_id="CURRENT123", tracking_status="In Progress", service_provider="Independent carrier")
	data.add("Shipment", "HISTORY", shipment_id="OLD123", docstatus=2, modified="2026-09-29 14:00:00")
	data.link(dn.name, "CURRENT")
	data.link(dn.name, "HISTORY")
	state = data.backend.shipment_summary.get_delivery_note_shipping_state(dn.name)
	assert state["shipment"] == "CURRENT"
	assert state["shipment_id"] == state["awb_number"] == "CURRENT123"
	assert state["transport_status_display"] == "运输中"
	assert state["historical_shipment"]["shipment"] == "HISTORY"
	assert state["historical_shipment"]["shipment_docstatus"] == 2
	assert not state["restricted"]
	assert not any(key.startswith("sf_") for key in state)


def test_native_onload_supplies_summary_before_render(data):
	dn = data.add("Delivery Note", "DN-1")
	data.backend.shipment_summary.load_delivery_note_shipping_state(dn)
	dn.set_onload.assert_called_once()
	assert dn.set_onload.call_args.args[0] == "shipping_state"
	assert dn.set_onload.call_args.args[1]["shipment"] is None


def test_native_manual_summary_keeps_destination_and_declared_fee_without_carrier_app(data):
	dn = data.add("Delivery Note", "DN-1")
	data.add("Shipment", "MANUAL", service_provider="其他物流（手工登记）", shipment_id="MANUAL123",
		manual_destination="货代或港口仓", manual_transport_status="已交接货代",
		manual_freight_recorded=1, manual_freight_amount=23.5, manual_freight_currency="CNY",
		manual_settlement_status="待结算")
	data.link(dn.name, "MANUAL")
	state = data.backend.shipment_summary.get_delivery_note_shipping_state(dn.name)
	assert state["is_manual"]
	assert state["manual_destination"] == "货代或港口仓"
	assert state["transport_status_display"] == "已交接货代"
	assert state["manual_freight_amount"] == 23.5
	assert state["freight_status_display"] == "待结算"
	assert not any(key.startswith("sf_") for key in state)


@pytest.mark.parametrize("fields", [{"docstatus": 2}, {"is_return": 1}])
def test_closed_delivery_note_keeps_historical_waybill(data, fields):
	dn = data.add("Delivery Note", "DN-1", **fields)
	data.add("Shipment", "HISTORY", awb_number="HISTORY123", docstatus=2)
	data.link(dn.name, "HISTORY")
	state = data.backend.shipment_summary.get_delivery_note_shipping_state(dn.name)
	assert state["cancelled"]
	assert state["shipment"] is None
	assert state["historical_shipment"]["shipment_id"] == "HISTORY123"


def test_restricted_current_shipment_is_not_replaced_by_readable_history(data):
	dn = data.add("Delivery Note", "DN-1")
	data.add("Shipment", "SECRET", shipment_id="SECRET123", denied=True)
	data.add("Shipment", "HISTORY", shipment_id="OLD123", docstatus=2)
	data.link(dn.name, "SECRET")
	data.link(dn.name, "HISTORY")
	state = data.backend.shipment_summary.get_delivery_note_shipping_state(dn.name)
	assert state["restricted"] and state["message"]
	assert state["shipment"] is None and state["historical_shipment"] is None
	assert "SECRET" not in repr(state)


def test_delivery_note_permission_precedes_related_queries(data):
	dn = data.add("Delivery Note", "DENIED", denied=True)
	with pytest.raises(PermissionError):
		data.backend.shipment_summary.get_delivery_note_shipping_state(dn.name)
	data.backend.frappe.get_all.assert_not_called()


def test_unexpected_database_errors_are_not_hidden_as_empty_state(data):
	dn = data.add("Delivery Note", "DN-1")
	data.backend.frappe.get_all.side_effect = RuntimeError("database failure")
	with pytest.raises(RuntimeError, match="database failure"):
		data.backend.shipment_summary.load_delivery_note_shipping_state(dn)


def test_carrier_extension_cannot_replace_native_shipment_identity(data, monkeypatch):
	adapter = types.ModuleType("test_carrier_extension")
	adapter.matches = lambda doc: True
	adapter.get_display_values = lambda doc: {"freight_status_display": "Carrier billed"}
	adapter.get_delivery_note_summary = lambda doc: {"carrier_booking_state": "Cancelled", "shipment": "WRONG"}
	monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
	data.backend.frappe.get_hooks.return_value = [adapter.__name__]
	dn = data.add("Delivery Note", "DN-1")
	data.add("Shipment", "CURRENT", shipment_id="CURRENT123")
	data.link(dn.name, "CURRENT")
	state = data.backend.shipment_summary.get_delivery_note_shipping_state(dn.name)
	assert state["shipment"] == "CURRENT"
	assert state["carrier_booking_state"] == "Cancelled"
	assert state["freight_status_display"] == "Carrier billed"


@pytest.mark.parametrize("amount", [None, "", "NaN", "Infinity", -1, "invalid"])
def test_invalid_manual_freight_is_unknown(data, amount):
	row = Doc(manual_freight_recorded=1, manual_freight_amount=amount, manual_freight_currency="CNY")
	assert data.backend.shipment_summary.manual_freight_summary([row]) == {
		"total": 1, "recorded": 0, "settled": 0, "amounts": {},
	}


def test_manual_freight_keeps_explicit_zero_and_separate_currencies(data):
	rows = [
		Doc(manual_freight_recorded=1, manual_freight_amount=0, manual_freight_currency="CNY", manual_settlement_status="已结算（人工确认）"),
		Doc(manual_freight_recorded=1, manual_freight_amount="12.34", manual_freight_currency="USD"),
	]
	assert data.backend.shipment_summary.manual_freight_summary(rows) == {
		"total": 2, "recorded": 2, "settled": 1, "amounts": {"CNY": 0.0, "USD": 12.34},
	}


def test_order_freight_deduplicates_packages_and_preserves_cancelled_costs(data):
	data.add("Sales Order", "SO-1")
	for name in ("DN-1", "DN-2"):
		data.add("Delivery Note", name, docstatus=2)
		data.order_item(name, "SO-1")
		data.order_item(name, "SO-1")
		for shipment in ("COST", "UNKNOWN", "CURRENT", "API-CARRIER"):
			data.link(name, shipment)
	provider = "其他物流（手工登记）"
	data.add("Shipment", "COST", service_provider=provider, docstatus=2, manual_freight_recorded=1,
		manual_freight_amount="23.50", manual_freight_currency="CNY", manual_settlement_status="已结算（人工确认）")
	data.add("Shipment", "UNKNOWN", service_provider=provider, docstatus=2, manual_freight_recorded=0)
	data.add("Shipment", "CURRENT", service_provider=provider, manual_freight_recorded=0)
	data.add("Shipment", "API-CARRIER", service_provider="API carrier", manual_freight_recorded=1,
		manual_freight_amount=100, manual_freight_currency="CNY")
	assert data.backend.shipment_summary.get_sales_order_manual_freight_map('["SO-1"]') == {
		"SO-1": {"total": 2, "recorded": 1, "settled": 1, "amounts": {"CNY": 23.5}},
	}


@pytest.mark.parametrize("restricted_doctype", ["Sales Order", "Delivery Note", "Shipment"])
def test_order_freight_respects_each_document_permission(data, restricted_doctype):
	data.add("Sales Order", "SO-1", denied=restricted_doctype == "Sales Order")
	data.add("Delivery Note", "DN-1", denied=restricted_doctype == "Delivery Note")
	data.add("Shipment", "MANUAL", denied=restricted_doctype == "Shipment", service_provider="其他物流（手工登记）",
		manual_freight_recorded=1, manual_freight_amount=500, manual_freight_currency="CNY")
	data.order_item("DN-1", "SO-1")
	data.link("DN-1", "MANUAL")
	if restricted_doctype == "Sales Order":
		with pytest.raises(PermissionError):
			data.backend.shipment_summary.get_sales_order_manual_freight_map(["SO-1"])
		data.backend.frappe.get_all.assert_not_called()
	else:
		assert data.backend.shipment_summary.get_sales_order_manual_freight_map(["SO-1"])["SO-1"] == {
			"total": 0, "recorded": 0, "settled": 0, "amounts": {},
		}
