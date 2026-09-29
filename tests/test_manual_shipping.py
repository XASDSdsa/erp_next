"""Focused business tests, independent of a running Frappe database."""

import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest


class Doc(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__


@pytest.fixture
def h(monkeypatch):
	fake = types.ModuleType("frappe")
	fake.whitelist = lambda *a, **k: lambda fn: fn
	fake.has_permission = Mock(return_value=True)
	fake.throw = lambda message, *a, **k: (_ for _ in ()).throw(ValueError(message))
	fake.session = Doc(user="sales@example.com")
	state = Doc(duplicate=None)
	fake.db = Doc(exists=lambda dt, value: dt == "Currency" and value in ("CNY", "USD"),
		get_value=lambda *a, **k: state.duplicate)
	state.address = Doc(country="China", check_permission=Mock())
	fake.get_doc = Mock(return_value=state.address)
	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	utils.now_datetime = lambda: "2026-09-19 12:00:00"
	lifecycle = types.ModuleType("erpnext.stock.doctype.shipment.shipment_lifecycle")
	lifecycle.validate_shipment_links = Mock()
	lifecycle.linked_delivery_notes = lambda doc: [r.get("delivery_note") for r in doc.get("shipment_delivery_note") or []]
	monkeypatch.setitem(sys.modules, "frappe", fake)
	monkeypatch.setitem(sys.modules, "frappe.utils", utils)
	monkeypatch.setitem(sys.modules, lifecycle.__name__, lifecycle)
	carriers = types.ModuleType("erpnext.stock.doctype.shipment.carriers")
	carriers.get_carrier = Mock(return_value=None)
	carriers.has_carrier_booking = Mock(return_value=False)
	monkeypatch.setitem(sys.modules, carriers.__name__, carriers)
	path = Path(__file__).parents[1] / "erpnext/stock/doctype/shipment/manual_shipping.py"
	spec = importlib.util.spec_from_file_location("manual_shipping_test", path)
	m = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(m)
	return m, fake, state, lifecycle


def doc(previous=None, **updates):
	value = Doc(name="SHIPMENT-1", service_provider="其他物流（手工登记）", docstatus=0,
		manual_carrier="中通快递", manual_carrier_name="", manual_waybill="001234567890",
		manual_destination="客户地址", manual_transport_status="待交运", manual_freight_recorded=0,
		manual_freight_currency="CNY", manual_settlement_status="待结算", manual_freight_amount=0,
		delivery_address_name="CUSTOMER-ADDRESS", delivery_customer="CUSTOMER",
		shipment_delivery_note=[Doc(delivery_note="DN-1")])
	value.update(updates)
	value.get_doc_before_save = lambda: previous
	value.is_new = lambda: previous is None
	return value


def existing(m, **updates):
	old = doc(**updates)
	m.validate_shipment(old)
	new = Doc(old)
	new.get_doc_before_save = lambda: old
	new.is_new = lambda: False
	return old, new


def test_cancelled_freight_retains_identity_and_audit(h):
	m, fake, state, lifecycle = h
	cancelled = doc(docstatus=2, modified="2026-09-19 12:00:00")
	cancelled.check_permission = Mock()
	cancelled.as_dict = lambda: dict(cancelled)
	cancelled.db_set = Mock()
	cancelled.save_version = Mock()
	previous = Doc(cancelled)
	fake.get_doc.side_effect = [cancelled, previous]
	result = m.record_cancelled_freight(cancelled.name, 28, "CNY", "待结算", "取消后收到承运商账单", cancelled.modified)
	assert result["shipment"] == cancelled.name
	cancelled.check_permission.assert_called_once_with("write")
	written = cancelled.db_set.call_args.args[0]
	assert written["shipment_amount"] == 28
	assert written["manual_recorded_by"] == "sales@example.com"
	assert not {"docstatus", "status", "shipment_id", "awb_number", "manual_waybill"}.intersection(written)
	cancelled.save_version.assert_called_once()
	assert cancelled.docstatus == 2


def test_cancelled_freight_rejects_stale_or_active_document(h):
	m, fake, state, lifecycle = h
	cancelled = doc(docstatus=2, modified="new")
	cancelled.check_permission = Mock()
	fake.get_doc.return_value = cancelled
	with pytest.raises(ValueError, match="刷新"):
		m.record_cancelled_freight(cancelled.name, 20, "CNY", "待结算", "晚到费用", "old")
	cancelled.docstatus = 1
	with pytest.raises(ValueError, match="只用于已取消"):
		m.record_cancelled_freight(cancelled.name, 20, "CNY", "待结算", "晚到费用", "new")


def test_leading_zeroes_and_unknown_freight_are_preserved(h):
	m, _, _, lifecycle = h
	d = doc(manual_waybill="  001234567890  ")
	m.validate_shipment(d)
	assert d.manual_waybill == d.awb_number == d.shipment_id == "001234567890"
	assert d.manual_freight_recorded == 0
	assert d.manual_settlement_status == "待结算"
	assert d.manual_recorded_by == "sales@example.com"
	assert len(d.manual_waybill_key) == 64
	lifecycle.validate_shipment_links.assert_not_called()


@pytest.mark.parametrize("number", ["ABC\n123", "ABC 123", "123,456", "123\t", "", 123])
def test_single_text_waybill_required(h, number):
	with pytest.raises(ValueError):
		h[0].validate_shipment(doc(manual_waybill=number))


def test_unusual_format_is_soft_with_explanation(h):
	m = h[0]
	d = doc(manual_carrier="顺丰国内", manual_waybill="SF-TEST-123")
	with pytest.raises(ValueError, match="格式"):
		m.validate_shipment(d)
	d.manual_waybill_override = 1
	d.manual_note = "已核对纸质面单，特殊业务单号"
	m.validate_shipment(d)
	assert d.manual_waybill == "SF-TEST-123"


def test_cancelled_duplicate_cannot_be_reused(h):
	m, _, state, _ = h
	state.duplicate = "SHIPMENT-CANCELLED"
	with pytest.raises(ValueError, match="重复登记"):
		m.validate_shipment(doc())


@pytest.mark.parametrize("amount", [-1, "NaN", "Infinity", "invalid", None])
def test_invalid_freight_rejected(h, amount):
	with pytest.raises(ValueError, match="运费"):
		h[0].validate_shipment(doc(manual_freight_recorded=1, manual_freight_amount=amount))


def test_confirmed_zero_needs_reason_and_has_no_financial_writes(h):
	m, fake, *_ = h
	d = doc(manual_freight_recorded=1, manual_freight_amount=0)
	with pytest.raises(ValueError, match="原因"):
		m.validate_shipment(d)
	d.manual_note = "收件方到付，我方不承担运费"
	m.validate_shipment(d)
	assert d.manual_freight_recorded == 1 and d.manual_freight_amount == 0
	assert all(call.args[0] == "Address" for call in fake.get_doc.call_args_list)


def test_settlement_requires_registered_freight_and_evidence_or_reason(h):
	m = h[0]
	d = doc(manual_settlement_status="已结算（人工确认）")
	with pytest.raises(ValueError, match="尚未登记"):
		m.validate_shipment(d)
	d.manual_freight_recorded = 1
	d.manual_freight_amount = 12
	with pytest.raises(ValueError, match="凭据"):
		m.validate_shipment(d)
	d.manual_evidence = "/private/files/receipt.pdf"
	m.validate_shipment(d)
	assert d.shipment_amount == 12


def test_forwarder_handoff_is_not_customer_delivery(h):
	m = h[0]
	d = doc(manual_destination="货代或港口仓", manual_transport_status="已送达")
	with pytest.raises(ValueError, match="客户签收"):
		m.validate_shipment(d)
	d.manual_transport_status = "已交接货代"
	m.validate_shipment(d)
	assert d.tracking_status == "In Progress"
	assert d.delivery_customer == "CUSTOMER"


def test_forwarder_requires_actual_chinese_address(h):
	m, _, state, _ = h
	state.address.country = "United States"
	with pytest.raises(ValueError, match="中国"):
		m.validate_shipment(doc(manual_destination="货代或港口仓"))


def test_carrier_record_unchanged_and_cannot_convert_existing_booking(h):
	m = h[0]
	d = doc(service_provider="Test Carrier", shipment_id="CARRIER123")
	m.validate_shipment(d)
	assert not d.get("manual_waybill_key")
	changed = doc(previous=d)
	m.has_carrier_booking.return_value = True
	with pytest.raises(ValueError, match="承运商"):
		m.validate_shipment(changed)


def test_manual_identity_and_provider_are_immutable_after_submission(h):
	m = h[0]
	old, new = existing(m, docstatus=1)
	new.manual_waybill = "DIFFERENT"
	with pytest.raises(ValueError, match="已提交"):
		m.validate_shipment(new)
	new.manual_waybill = old.manual_waybill
	new.service_provider = "Test Carrier"
	with pytest.raises(ValueError, match="切换"):
		m.validate_shipment(new)


def test_shipped_address_and_status_cannot_be_rewound(h):
	m = h[0]
	_, new = existing(m, manual_transport_status="已交运")
	new.manual_transport_status = "待交运"
	with pytest.raises(ValueError, match="改回待交运"):
		m.validate_shipment(new)
	new.manual_transport_status = "已交运"
	new.delivery_address_name = "OTHER-ADDRESS"
	with pytest.raises(ValueError, match="覆盖收货地址"):
		m.validate_shipment(new)


def test_fee_change_requires_fresh_reason_and_server_audit(h):
	m = h[0]
	old, new = existing(m, manual_freight_recorded=1, manual_freight_amount=10, manual_note="初次录入")
	new.manual_freight_amount = 20
	with pytest.raises(ValueError, match="变更原因"):
		m.validate_shipment(new)
	new.manual_note = "物流公司补收第二箱费用"
	new.manual_recorded_by = "forged@example.com"
	m.validate_shipment(new)
	assert new.manual_recorded_by == "sales@example.com"
	assert old.manual_freight_amount == 10


def test_cancel_cannot_fake_return_in_same_request(h):
	m, fake, *_ = h
	old, new = existing(m, docstatus=1, manual_transport_status="已交运")
	with pytest.raises(ValueError, match="不能直接取消"):
		m.before_cancel(new)
	new.manual_transport_status = "已退回"
	new.manual_note = "包裹已退回"
	with pytest.raises(ValueError, match="先保存"):
		m.before_cancel(new)
	old.manual_transport_status = "已退回"
	old.manual_note = new.manual_note
	m.before_cancel(new)


def test_trash_preserves_expense_and_submitted_history(h):
	m = h[0]
	for d in (doc(docstatus=2), doc(manual_freight_recorded=1), doc(manual_transport_status="已交运")):
		with pytest.raises(ValueError, match="历史"):
			m.before_trash(d)
	m.before_trash(doc())


def test_registered_carrier_cannot_be_entered_as_other(h):
	m = h[0]
	m.get_carrier.return_value = object()
	with pytest.raises(ValueError, match="专用选项"):
		m.validate_shipment(doc(manual_carrier="其他", manual_carrier_name="Registered Carrier"))
	m.get_carrier.assert_called_once_with({"service_provider": "Registered Carrier", "carrier": "Registered Carrier"})


def test_existing_carrier_history_is_checked_even_without_current_waybill(h):
	m = h[0]
	m.has_carrier_booking.return_value = True
	with pytest.raises(ValueError, match="面单历史"):
		m.validate_shipment(doc())


def test_native_waybill_cannot_be_overwritten_by_manual_conversion(h):
	m = h[0]
	previous = doc(service_provider="Independent carrier", shipment_id="ORIGINAL123")
	with pytest.raises(ValueError, match="原始单号"):
		m.validate_shipment(doc(previous=previous))


def test_manual_shipment_requires_delivery_note(h):
	with pytest.raises(ValueError, match="关联已提交"):
		h[0].validate_shipment(doc(shipment_delivery_note=[]))
