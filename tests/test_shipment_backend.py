"""Native Shipment behavior with real generic modules and no carrier app installed."""

import importlib.util
import sys
import types
from datetime import time
from pathlib import Path
from unittest.mock import Mock

import pytest


class Doc(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__

	def set(self, key, value):
		self[key] = value

	def get_doc_before_save(self):
		return self.get("_doc_before_save")

	def is_new(self):
		return not self.get("_doc_before_save")


@pytest.fixture
def backend(monkeypatch):
	frappe = types.ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.PermissionError = PermissionError
	frappe.whitelist = lambda *args, **kwargs: lambda fn: fn
	frappe.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(ValueError(message))
	frappe.has_permission = Mock(return_value=True)
	frappe.session = Doc(user="operator@example.com")
	frappe.get_hooks = Mock(return_value=[])
	frappe.get_module = Mock(side_effect=AssertionError("No carrier app should be imported"))
	frappe.db = Mock()
	frappe.db.exists.side_effect = lambda doctype, name: doctype == "Currency" and name == "CNY"
	frappe.db.get_values.return_value = []
	frappe.db.get_value.side_effect = lambda doctype, *args, **kwargs: (
		Doc(docstatus=1, status="To Bill") if doctype == "Delivery Note" else None
	)
	frappe.get_doc = Mock(return_value=Doc(country="China", check_permission=Mock()))
	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	utils.flt = lambda value: float(value or 0)
	utils.get_time = lambda value: time.fromisoformat(value)
	utils.now_datetime = lambda: "2026-09-29 12:00:00"
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)

	for name in (
		"erpnext", "erpnext.stock", "erpnext.stock.doctype", "erpnext.stock.doctype.shipment",
		"erpnext.accounts", "frappe.contacts", "frappe.contacts.doctype",
		"frappe.contacts.doctype.contact", "frappe.model",
	):
		module = types.ModuleType(name)
		module.__path__ = []
		monkeypatch.setitem(sys.modules, name, module)
	contact = types.ModuleType("frappe.contacts.doctype.contact.contact")
	contact.get_default_contact = Mock()
	party = types.ModuleType("erpnext.accounts.party")
	party.get_party_shipping_address = Mock()
	document = types.ModuleType("frappe.model.document")
	document.Document = Doc
	for module in (contact, party, document):
		monkeypatch.setitem(sys.modules, module.__name__, module)

	modules = {}
	base = Path(__file__).parents[1] / "erpnext/stock/doctype/shipment"
	for name in (
		"carriers", "shipment_lifecycle", "manual_shipping", "shipment_display", "shipment_contents",
		"shipment", "shipment_summary",
	):
		full_name = f"erpnext.stock.doctype.shipment.{name}"
		spec = importlib.util.spec_from_file_location(full_name, base / f"{name}.py")
		module = importlib.util.module_from_spec(spec)
		monkeypatch.setitem(sys.modules, full_name, module)
		spec.loader.exec_module(module)
		modules[name] = module
	return Doc(frappe=frappe, **modules)


def manual_doc(backend, **changes):
	doc = backend.shipment.Shipment(
		name="MANUAL-1", docstatus=0, service_provider="其他物流（手工登记）",
		manual_carrier="中通快递", manual_waybill="001234567890", manual_freight_recorded=0,
		manual_freight_currency="CNY", delivery_address_name="ADDRESS-1",
		shipment_delivery_note=[Doc(delivery_note="DN-1", grand_total=100)],
		shipment_parcel=[Doc(weight=2, count=1)],
	)
	doc.update(changes)
	return doc


def test_native_manual_validate_without_installed_carrier(backend):
	doc = manual_doc(backend)
	doc.validate()
	assert doc.manual_waybill == doc.shipment_id == doc.awb_number == "001234567890"
	assert doc.status == "Draft"
	assert doc.total_weight == 2
	assert doc.transport_status_display == "待交运"
	assert doc.freight_status_display == "运费待登记"
	assert doc.manual_recorded_by == "operator@example.com"
	backend.frappe.get_module.assert_not_called()
	# Native validation owns source locking once; the manual validator does not repeat it.
	delivery_note_reads = [
		call for call in backend.frappe.db.get_value.call_args_list if call.args[0] == "Delivery Note"
	]
	assert len(delivery_note_reads) == 1
	assert delivery_note_reads[0].kwargs["for_update"] is True


def test_native_submitted_manual_identity_remains_protected(backend):
	previous = manual_doc(backend)
	previous.validate()
	previous.docstatus = 1
	doc = manual_doc(backend, **{**previous, "_doc_before_save": previous, "manual_waybill": "NEW123"})
	with pytest.raises(ValueError, match="已提交"):
		doc.before_update_after_submit()


def test_native_discard_cannot_erase_handover(backend):
	previous = manual_doc(backend, manual_transport_status="已交运")
	previous.validate()
	doc = manual_doc(backend, **{**previous, "_doc_before_save": previous})
	with pytest.raises(ValueError, match="不能直接取消"):
		doc.before_discard()


def test_native_trash_preserves_recorded_freight(backend):
	doc = manual_doc(backend, manual_freight_recorded=1)
	with pytest.raises(ValueError, match="历史"):
		doc.on_trash()


def test_default_display_clears_previous_carrier_values(backend):
	doc = Doc(service_provider="Independent carrier", tracking_status="Delivered", shipment_id="TRACK123",
		freight_status_display="old carrier state", interception_status_display="old cancellation")
	backend.shipment_display.set_display_fields(doc)
	assert doc.freight_status_display == doc.interception_status_display == ""
	assert doc.transport_status_display == "已送达"
	assert doc.label_replacement_display == "当前 TRACK123"
	backend.frappe.get_module.assert_not_called()


def test_manual_display_never_uses_carrier_extension(backend, monkeypatch):
	get_carrier = Mock(side_effect=AssertionError("Manual display must be carrier-independent"))
	monkeypatch.setattr(backend.shipment_display, "get_carrier", get_carrier)
	doc = manual_doc(backend, manual_freight_recorded=1, manual_settlement_status="已结算（人工确认）")
	values = backend.shipment_display.display_values(doc)
	assert values["freight_status_display"] == "已结算（人工确认）"
	get_carrier.assert_not_called()


def test_change_persists_derived_display_without_recursive_document_write(backend):
	doc = manual_doc(backend, manual_transport_status="已退回")
	doc.db_set = Mock(side_effect=AssertionError("Display update must not re-enter on_change"))
	doc.on_change()
	backend.frappe.db.set_value.assert_called_once()
	assert backend.frappe.db.set_value.call_args.args[:2] == ("Shipment", "MANUAL-1")
	assert backend.frappe.db.set_value.call_args.kwargs == {"update_modified": False}
	assert doc.transport_status_display == "已退回"
	doc.on_change()
	backend.frappe.db.set_value.assert_called_once()


@pytest.mark.parametrize("event", ["validate", "before_update_after_submit", "before_cancel", "before_discard"])
def test_existing_waybill_cannot_change_provider_or_bypass_cancel_guard(backend, event):
	previous = manual_doc(backend, service_provider="Original carrier", carrier="Original carrier",
		shipment_id="ORIGINAL123", awb_number="ORIGINAL123")
	doc = manual_doc(backend, service_provider="Different carrier", carrier="Original carrier",
		shipment_id="", awb_number="", _doc_before_save=previous)
	with pytest.raises(ValueError, match="booking history"):
		getattr(doc, event)()
	backend.frappe.db.set_value.assert_not_called()


def booking_adapter(backend, monkeypatch, *, has_history):
	adapter = types.ModuleType("test_booking_carrier")
	adapter.matches = lambda doc: doc.get("service_provider") in ("Registered carrier", "Carrier alias")
	adapter.has_booking = lambda doc: has_history
	adapter.get_display_values = lambda doc: {}
	monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
	backend.frappe.get_hooks.return_value = [adapter.__name__]
	return adapter


def test_history_only_booking_cannot_change_provider(backend, monkeypatch):
	booking_adapter(backend, monkeypatch, has_history=True)
	previous = manual_doc(backend, service_provider="Registered carrier", shipment_id="", awb_number="")
	doc = manual_doc(backend, service_provider="Different carrier", _doc_before_save=previous)
	with pytest.raises(ValueError, match="booking history"):
		doc.validate()


def test_same_registered_carrier_alias_preserves_booking_owner(backend, monkeypatch):
	booking_adapter(backend, monkeypatch, has_history=True)
	previous = manual_doc(backend, service_provider="Registered carrier", shipment_id="ORIGINAL123")
	doc = manual_doc(backend, service_provider="Carrier alias", shipment_id="ORIGINAL123", _doc_before_save=previous)
	doc.validate()
	assert doc.shipment_id == "ORIGINAL123"


def test_unbooked_draft_can_choose_another_carrier(backend):
	previous = manual_doc(backend, service_provider="Original carrier", shipment_id="", awb_number="")
	doc = manual_doc(backend, service_provider="Different carrier", _doc_before_save=previous)
	doc.validate()
	assert doc.service_provider == "Different carrier"
