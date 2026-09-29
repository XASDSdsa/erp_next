"""Manual carrier records. No carrier, stock, or accounting writes occur here."""

import hashlib
import re
from decimal import Decimal, InvalidOperation

import frappe
from frappe.utils import cint, now_datetime

from erpnext.stock.doctype.shipment.carriers import get_carrier, has_carrier_booking
from erpnext.stock.doctype.shipment.shipment_lifecycle import linked_delivery_notes


PROVIDER = "其他物流（手工登记）"
CARRIERS = (
	"顺丰国内", "中通快递", "圆通速递", "申通快递", "韵达快递", "极兔速递", "京东物流",
	"邮政EMS", "德邦快递", "德邦快运", "跨越速运", "安能物流", "中通快运", "顺心捷达", "其他",
)
DESTINATIONS = ("客户地址", "货代或港口仓")
TRANSPORT_STATUSES = ("待交运", "已交运", "已送达", "已交接货代", "已退回", "异常")
SETTLEMENT_STATUSES = ("待结算", "已结算（人工确认）")
# These are documented input conventions, not proof that a waybill exists.
FORMAT_RULES = {
	"顺丰国内": (r"(?:\d{12}|SF\d{13}|9901\d{11})", "常见为 12 位数字、SF 加 13 位数字或 9901 开头的 15 位数字"),
	"韵达快递": (r"\d{13}", "常见为 13 位数字"),
	"德邦快递": (r"[A-Z0-9-]{8,20}", "官网查询入口接受 8–20 位字母、数字或连字符"),
	"德邦快运": (r"[A-Z0-9-]{8,20}", "官网查询入口接受 8–20 位字母、数字或连字符"),
	"跨越速运": (r"(?:\d{7}|\d{11}|KY(?:\d{12}|\d{13}|\d{16})|KYE(?:\d{11}|\d{12}|\d{15}))", "请核对跨越运单原件：支持常见数字、KY 或 KYE 开头的单号"),
}
IDENTITY_FIELDS = ("manual_carrier", "manual_carrier_name", "manual_waybill")
ADDRESS_FIELDS = ("manual_destination", "delivery_address_name", "delivery_contact_name", "delivery_customer")
REVIEW_FIELDS = IDENTITY_FIELDS + ADDRESS_FIELDS + (
	"manual_transport_status", "manual_freight_recorded", "manual_freight_amount", "manual_freight_currency",
	"manual_settlement_status", "manual_evidence",
)
AUDIT_FIELDS = REVIEW_FIELDS + ("manual_waybill_override", "manual_note")


def is_manual(doc):
	return doc.get("service_provider") == PROVIDER


def _previous(doc):
	return doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None


def _changed(doc, previous, fields):
	return any((doc.get(field) or "") != (previous.get(field) or "") for field in fields)


def _normalize_waybill(value):
	# Leading zeroes and hyphens are significant; never cast a waybill to a number.
	if not isinstance(value, str):
		frappe.throw("请填写已有物流单号，单号必须以文本保存。")
	if any(ord(char) < 32 or ord(char) == 127 for char in value):
		frappe.throw("物流单号中不能包含换行、制表符或控制字符，请一次填写一个单号。")
	value = value.strip()
	if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", value):
		frappe.throw("请填写一个物流单号，只允许字母、数字和连字符，不要粘贴空格或多个单号。")
	return value


@frappe.whitelist()
def get_config():
	frappe.has_permission("Shipment", "read", throw=True)
	return {"provider": PROVIDER, "carriers": list(CARRIERS), "destinations": list(DESTINATIONS),
		"transport_statuses": list(TRANSPORT_STATUSES), "settlement_statuses": list(SETTLEMENT_STATUSES),
		"format_hints": {name: rule[1] for name, rule in FORMAT_RULES.items()}}


@frappe.whitelist()
def check_waybill(carrier, waybill):
	frappe.has_permission("Shipment", "read", throw=True)
	return waybill_check(carrier, waybill)


def waybill_check(carrier, waybill):
	if carrier not in CARRIERS:
		frappe.throw("请选择物流服务商。")
	number = _normalize_waybill(waybill)
	rule = FORMAT_RULES.get(carrier)
	unusual = bool(rule and not re.fullmatch(rule[0], number, flags=re.IGNORECASE))
	return {"waybill": number, "unusual": unusual,
		"hint": rule[1] if rule else "仅校验单号填写格式；未联网核验单号或物流状态。"}


def _carrier_name(doc):
	carrier = doc.get("manual_carrier")
	if carrier not in CARRIERS:
		frappe.throw("请选择物流服务商。")
	if carrier == "其他":
		name = str(doc.get("manual_carrier_name") or "").strip()
		if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
			frappe.throw("选择其他物流时，请填写有效的服务商名称。")
		if name in CARRIERS[:-1] or get_carrier({"service_provider": name, "carrier": name}):
			frappe.throw("该服务商已有专用选项，请直接选择对应服务商。")
		doc.manual_carrier_name = name
		return name
	doc.manual_carrier_name = ""
	return carrier


def _validate_freight(doc):
	doc.manual_freight_recorded = cint(doc.get("manual_freight_recorded"))
	doc.manual_settlement_status = doc.get("manual_settlement_status") or "待结算"
	if doc.manual_settlement_status not in SETTLEMENT_STATUSES:
		frappe.throw("请选择有效的人工结算状态。")
	if not doc.manual_freight_recorded:
		if doc.manual_settlement_status != "待结算":
			frappe.throw("运费尚未登记，不能标记为人工确认已结算。")
		# Avoid hidden stale values being treated as confirmed money in native views.
		if doc.get("manual_freight_amount") not in (None, "", 0, 0.0, "0"):
			frappe.throw("已填写运费金额，请勾选运费已登记；金额未知时请清空金额。")
		doc.manual_freight_amount = 0
		doc.shipment_amount = 0
		return
	try:
		amount = Decimal(str(doc.get("manual_freight_amount")))
	except (InvalidOperation, ValueError, TypeError):
		frappe.throw("运费必须是明确的非负金额；未知运费请取消勾选运费已登记。")
	if not amount.is_finite() or amount < 0 or amount > Decimal("999999999999.99"):
		frappe.throw("运费必须是有效的非负金额。")
	if amount == 0 and not str(doc.get("manual_note") or "").strip():
		frappe.throw("确认运费为 0 时，请填写免费、到付或其他实际原因。")
	currency = str(doc.get("manual_freight_currency") or "").strip()
	if not currency or not frappe.db.exists("Currency", currency):
		frappe.throw("请填写有效的运费币种。")
	if doc.manual_settlement_status == "已结算（人工确认）" and not (doc.get("manual_evidence") or str(doc.get("manual_note") or "").strip()):
		frappe.throw("人工确认结算时，请上传凭据或填写结算说明；此操作不会生成会计凭证。")
	doc.manual_freight_amount = float(amount)
	doc.shipment_amount = float(amount)


def validate_shipment(doc, method=None):
	previous = _previous(doc)
	if previous and is_manual(previous) and not is_manual(doc):
		frappe.throw("已登记的手工物流不能直接切换服务类型；请保留原运单历史。")
	if not is_manual(doc):
		# Non-manual records must not reserve a manual waybill identity.
		if doc.get("manual_waybill_key"):
			doc.manual_waybill_key = None
		return
	if has_carrier_booking(previous or doc):
		frappe.throw("已有承运商面单或面单历史，不能切换为手工物流绕过原流程。")
	if previous and not is_manual(previous) and (previous.get("shipment_id") or previous.get("awb_number")):
		frappe.throw("原运单已有物流单号，不能直接改为手工登记；请保留原始单号记录。")
	carrier = _carrier_name(doc)
	result = waybill_check(doc.manual_carrier, doc.get("manual_waybill"))
	doc.manual_waybill = result["waybill"]
	if previous and is_manual(previous) and cint(previous.get("docstatus")) == 1 and _changed(doc, previous, IDENTITY_FIELDS):
		frappe.throw("运单已提交，服务商和物流单号不能直接修改，请保留原单号历史。")
	if (previous and is_manual(previous) and previous.get("manual_transport_status") not in (None, "", "待交运")
		and _changed(doc, previous, IDENTITY_FIELDS)):
		frappe.throw("已有交运记录，服务商和物流单号不能直接修改，请保留原交运历史。")
	if result["unusual"] and not (cint(doc.get("manual_waybill_override")) and str(doc.get("manual_note") or "").strip()):
		frappe.throw("单号格式与常见规则不同。请核对面单原件，勾选单号格式已核对并填写原因后保存。" + result["hint"])
	if (result["unusual"] and previous and _changed(doc, previous, IDENTITY_FIELDS)
		and cint(previous.get("manual_waybill_override")) and doc.get("manual_note") == previous.get("manual_note")):
		frappe.throw("单号已更换，请重新核对新单号并填写新的核对说明。")
	doc.manual_destination = doc.get("manual_destination") or "客户地址"
	doc.manual_transport_status = doc.get("manual_transport_status") or "待交运"
	if doc.manual_destination not in DESTINATIONS or doc.manual_transport_status not in TRANSPORT_STATUSES:
		frappe.throw("请选择有效的收货目的地和运输状态。")
	if doc.manual_destination == "货代或港口仓" and doc.manual_transport_status == "已送达":
		frappe.throw("货物送到国内货代或港口仓不代表海外客户签收，请选择已交接货代。")
	if doc.manual_destination == "客户地址" and doc.manual_transport_status == "已交接货代":
		frappe.throw("已交接货代仅适用于货代或港口仓目的地。")
	if previous and is_manual(previous) and previous.get("manual_transport_status") not in (None, "", "待交运"):
		if _changed(doc, previous, ADDRESS_FIELDS):
			frappe.throw("已有交运记录，不能直接覆盖收货地址或客户，请保留原交运事实。")
		if doc.manual_transport_status == "待交运":
			frappe.throw("已有交运记录，不能改回待交运；实际退回后请记录已退回及原因。")
	if not doc.get("delivery_address_name"):
		frappe.throw("请选择实际收货地址；寄往货代时应选择货代或港口仓地址。")
	if not previous or _changed(doc, previous, ADDRESS_FIELDS):
		address = frappe.get_doc("Address", doc.delivery_address_name)
		address.check_permission("read")
		if cint(address.get("disabled")):
			frappe.throw("实际收货地址已停用，请选择有效地址。")
		if doc.manual_destination == "货代或港口仓" and address.get("country") != "China":
			frappe.throw("寄往国内货代或港口仓时，请选择国家为中国的实际收货地址。")
	if not linked_delivery_notes(doc):
		frappe.throw("手工物流必须关联已提交的出库单。")
	_validate_freight(doc)
	# Cancelled records retain this key: the same physical waybill is never silently reused.
	key = hashlib.sha256((carrier.casefold() + "\0" + doc.manual_waybill.upper()).encode()).hexdigest()
	duplicate = frappe.db.get_value("Shipment", {"manual_waybill_key": key, "name": ["!=", doc.get("name") or ""]}, "name")
	if duplicate:
		frappe.throw("同一服务商及单号已登记在运单 {0}，请打开原运单处理，不要重复登记。".format(duplicate))
	doc.manual_waybill_key = key
	doc.shipment_id = doc.manual_waybill
	doc.awb_number = doc.manual_waybill
	doc.carrier = carrier
	doc.carrier_service = "手工登记"
	doc.tracking_status = {"已送达": "Delivered", "已退回": "Returned", "待交运": ""}.get(doc.manual_transport_status, "In Progress")
	doc.tracking_status_info = doc.manual_transport_status + "（人工登记）"
	if previous and is_manual(previous) and _changed(doc, previous, REVIEW_FIELDS):
		if not str(doc.get("manual_note") or "").strip() or doc.get("manual_note") == previous.get("manual_note"):
			frappe.throw("物流或费用信息发生变化，请填写本次变更原因，保留可核对的记录。")
	if not previous or not is_manual(previous) or _changed(doc, previous, AUDIT_FIELDS):
		doc.manual_recorded_by = frappe.session.user
		doc.manual_recorded_at = now_datetime()
	else:
		doc.manual_recorded_by = previous.get("manual_recorded_by")
		doc.manual_recorded_at = previous.get("manual_recorded_at")


def before_cancel(doc, method=None):
	# Read the persisted record, so a cancel request cannot change the status/fee and
	# cancel in one unvalidated call. Returned goods must be recorded in a normal save.
	saved = _previous(doc) or frappe.get_doc("Shipment", doc.name)
	if not is_manual(doc) and not is_manual(saved):
		return
	if doc.get("service_provider") != saved.get("service_provider"):
		frappe.throw("请勿在取消运单时变更物流服务类型。")
	if _changed(doc, saved, AUDIT_FIELDS + ("manual_waybill_key", "shipment_id", "awb_number", "carrier", "shipment_amount")):
		frappe.throw("请先保存物流或费用的变更并填写原因，再取消运单。")
	if saved.get("manual_transport_status") not in ("待交运", "已退回"):
		frappe.throw("已有交运记录，不能直接取消；实际退回后先记录已退回及原因，再取消运单。")
	if saved.get("manual_transport_status") == "已退回" and not str(saved.get("manual_note") or "").strip():
		frappe.throw("请先填写货物实际退回的说明。")


def on_trash(doc, method=None):
	if is_manual(doc) and (cint(doc.get("docstatus")) != 0 or doc.get("manual_transport_status") not in (None, "", "待交运") or cint(doc.get("manual_freight_recorded"))):
		frappe.throw("该运单已有交运、提交或费用记录，请保留运单和单号历史，不能直接删除。")


before_trash = on_trash


@frappe.whitelist(methods=["POST"])
def record_cancelled_freight(shipment, amount, currency, settlement_status, note, expected_modified, evidence=None):
	"""A late carrier charge survives cancellation; only fee facts may change."""
	doc = frappe.get_doc("Shipment", shipment, for_update=True)
	doc.check_permission("write")
	if not is_manual(doc) or cint(doc.docstatus) != 2:
		frappe.throw("此入口只用于已取消的手工物流运单；有效运单请直接在表单登记运费。")
	if str(doc.modified) != str(expected_modified):
		frappe.throw("运单已被其他人更新，请刷新后重新核对费用。")
	note = str(note or "").strip()
	if not note:
		frappe.throw("请填写本次补登记或更正运费的原因。")
	doc._doc_before_save = frappe.get_doc(doc.as_dict())
	doc.manual_freight_recorded = 1
	doc.manual_freight_amount = amount
	doc.manual_freight_currency = currency
	doc.manual_settlement_status = settlement_status
	doc.manual_note = note
	if evidence is not None:
		doc.manual_evidence = evidence
	_validate_freight(doc)
	doc.manual_recorded_by = frappe.session.user
	doc.manual_recorded_at = now_datetime()
	fields = ("manual_freight_recorded", "manual_freight_amount", "manual_freight_currency",
		"manual_settlement_status", "manual_note", "manual_evidence", "manual_recorded_by", "manual_recorded_at", "shipment_amount")
	doc.db_set({field: doc.get(field) for field in fields})
	doc.save_version()
	return {"shipment": doc.name, "message": "已登记取消运单的运费，原单号、取消状态和费用历史已保留；未生成会计凭证。"}


@frappe.whitelist()
def make_shipment(delivery_note):
	"""Return an unsaved native form; the operator reviews and saves normally."""
	frappe.has_permission("Shipment", "create", throw=True)
	note = frappe.get_doc("Delivery Note", delivery_note)
	note.check_permission("read")
	if cint(note.docstatus) != 1 or note.get("is_return"):
		frappe.throw("请选择已提交且不是退货单的出库单。")
	from erpnext.stock.doctype.delivery_note.delivery_note import make_shipment as native_make_shipment
	from erpnext.stock.doctype.shipment.shipment_lifecycle import validate_shipment_links
	doc = native_make_shipment(delivery_note)
	for field in ("shipment_id", "awb_number", "carrier", "carrier_service", "tracking_url", "tracking_status", "tracking_status_info"):
		doc.set(field, "")
	for field in ("manual_waybill", "manual_waybill_key", "manual_waybill_override", "manual_recorded_by", "manual_recorded_at"):
		doc.set(field, None)
	doc.shipment_amount = 0
	# Native mapping may only map item reference columns; explicitly use the source DN.
	if not any(row.get("delivery_note") == delivery_note for row in doc.get("shipment_delivery_note") or []):
		doc.set("shipment_delivery_note", [])
		doc.append("shipment_delivery_note", {"delivery_note": delivery_note, "grand_total": note.base_grand_total})
	validate_shipment_links(doc)
	doc.service_provider = PROVIDER
	doc.manual_destination = "客户地址"
	doc.manual_transport_status = "待交运"
	doc.manual_freight_recorded = 0
	doc.manual_freight_currency = "CNY"
	doc.manual_settlement_status = "待结算"
	return doc
