"""Atomic registration of manually confirmed PayPal USD customer receipts.

No PayPal API, currency conversion payout, or transaction commit occurs here.
The merchant Account row serializes all cooperating JE/PE receipt writers;
the immutable receipt name is the durable transaction identity.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

import frappe
from frappe.utils import getdate

from . import paypal_accounting as accounting

RECEIPT = "PayPal Receipt Record"


def _require(condition, message):
	if not condition:
		frappe.throw(message)


def normalize_transaction_id(value):
	value = str(value or "").strip().upper()
	_require(bool(re.fullmatch(r"[A-Z0-9-]{1,140}", value)), "请输入真实的 PayPal 交易号，仅允许字母、数字和连字符")
	return value


def receipt_key(transaction_id):
	identity = [accounting.COMPANY, accounting.BANK, normalize_transaction_id(transaction_id)]
	return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def _load(doctype, name, *, lock=False):
	doc = frappe.get_doc(doctype, name, for_update=lock)
	doc.check_permission("read")
	return doc


def _merchant_lock():
	_require(frappe.db.get_value("Account", accounting.BANK, "name", for_update=True), "PayPal 收款科目不存在")


def _record(transaction_id, *, lock=False):
	name = frappe.db.get_value(RECEIPT, receipt_key(transaction_id), "name", for_update=lock)
	return frappe.get_doc(RECEIPT, name, for_update=lock) if name else None


def _legacy_vouchers(transaction_id, *, lock=False):
	# Deliberately independent of row visibility; disclose details only after read permission.
	# Locking reads see the latest committed rows after waiting for the merchant lock.
	rows = []
	for doctype, field in (("Journal Entry", "cheque_no"), ("Payment Entry", "reference_no")):
		found = frappe.db.sql(
			f"SELECT name, docstatus FROM `tab{doctype}` WHERE company=%s "
			f"AND UPPER(TRIM(`{field}`))=%s LIMIT 3" + (" FOR UPDATE" if lock else ""),
			(accounting.COMPANY, transaction_id), as_dict=True,
		)
		rows.extend({"doctype": doctype, **row} for row in found)
	return rows


def _incoming_paypal(doc):
	if doc.get("company") != accounting.COMPANY:
		return False
	if doc.doctype == "Payment Entry":
		return doc.get("payment_type") == "Receive" and doc.get("party_type") == "Customer" and doc.get("paid_to") == accounting.BANK
	if doc.doctype == "Journal Entry":
		rows = doc.get("accounts") or []
		return any(r.account == accounting.BANK and float(r.get("debit_in_account_currency") or 0) > 0 for r in rows) and any(
			r.get("party_type") == "Customer" and float(r.get("credit_in_account_currency") or 0) > 0 for r in rows
		)
	return False


def _order_allocations(doc):
	amounts = {}
	if doc.get("company") != accounting.COMPANY:
		return amounts
	if doc.doctype == "Journal Entry":
		rows = doc.get("accounts") or []
		for row in rows:
			if row.get("reference_type") == "Sales Order" and row.get("party_type") == "Customer":
				name = row.get("reference_name")
				amounts[name] = amounts.get(name, 0) + accounting.decimal_value(row.get("credit_in_account_currency") or 0) - accounting.decimal_value(row.get("debit_in_account_currency") or 0)
	elif doc.doctype == "Payment Entry" and doc.get("payment_type") == "Receive" and doc.get("party_type") == "Customer":
		for row in doc.get("references") or []:
			if row.get("reference_doctype") == "Sales Order":
				name = row.get("reference_name")
				amounts[name] = amounts.get(name, 0) + accounting.decimal_value(row.get("allocated_amount") or 0)
	return {name: amount for name, amount in amounts.items() if name and amount > 0}


def guard_receipt(doc, method=None):
	"""Serialize PayPal identities and customer order advances before native validation."""
	if doc.get("docstatus") == 2:
		return
	paypal = _incoming_paypal(doc)
	if paypal:
		_merchant_lock()
	# All receipt accounts cooperate on order locks, including concurrent Payment Entry.
	for name, amount in sorted(_order_allocations(doc).items()):
		order = frappe.get_doc("Sales Order", name, for_update=True)
		if paypal and doc.doctype == "Journal Entry" and all(
			r.get("account_currency") == order.currency for r in doc.get("accounts") or [] if r.get("reference_name") == name
		):
			total = order.grand_total if order.get("disable_rounded_total") or not order.get("rounded_total") else order.rounded_total
			_require(amount + accounting.money(order.get("advance_paid") or 0) <= accounting.money(total), "超过销售订单当前可收预付款余额")
	if not paypal:
		return
	field = "cheque_no" if doc.doctype == "Journal Entry" else "reference_no"
	transaction_id = normalize_transaction_id(doc.get(field))
	doc.set(field, transaction_id)
	old = _record(transaction_id, lock=True)
	if old:
		_require(old.voucher_type == doc.doctype and old.voucher_name == doc.name, "该 PayPal 交易号已有收款记录，不能重复登记；请核对原凭证")
	for row in _legacy_vouchers(transaction_id, lock=True):
		_require(row["doctype"] == doc.doctype and row["name"] == doc.name, "该 PayPal 交易号已有凭证（包括草稿或已取消凭证），请核对原记录")
	# Once saved, changing the transaction identity requires explicit reconciliation.
	old_key = frappe.db.get_value(RECEIPT, {"voucher_type": doc.doctype, "voucher_name": doc.name}, "name", for_update=True)
	_require(not old_key or old_key == receipt_key(transaction_id), "已登记的 PayPal 交易号不能直接更改")


def register_voucher(doc, method=None):
	"""Keep the identity even on cancellation; normal invoice reconciliation is allowed."""
	if not _incoming_paypal(doc):
		return
	_merchant_lock()
	transaction_id = normalize_transaction_id(doc.get("cheque_no" if doc.doctype == "Journal Entry" else "reference_no"))
	record = _record(transaction_id, lock=True)
	if record:
		_require(record.voucher_type == doc.doctype and record.voucher_name == doc.name, "PayPal 交易已关联其他凭证")
	else:
		record = frappe.get_doc({
			"doctype": RECEIPT, "name": receipt_key(transaction_id), "__islocal": 1, "company": accounting.COMPANY,
			"bank_account": accounting.BANK, "transaction_id": transaction_id, "currency": "USD",
			"voucher_type": doc.doctype, "voucher_name": doc.name,
			"posting_date": doc.posting_date,
			"reference_date": doc.get("cheque_date" if doc.doctype == "Journal Entry" else "reference_date"),
		})
	record.status = "Cancelled" if doc.docstatus == 2 else "Recorded" if doc.docstatus == 1 else "Pending"
	record.flags.paypal_receipt_internal = True
	record.save(ignore_permissions=True)


def _remember_request(doc, order, request, fingerprint):
	register_voucher(doc)
	record = _record(request["transaction_id"], lock=True)
	if record.request_fingerprint:
		_require(record.request_fingerprint == fingerprint, "同一 PayPal 交易号的金额、订单或日期与原记录不一致")
	else:
		record.update({
			"sales_order": order.name, "customer": order.customer,
			"gross": request["gross"], "fee": request["fee"], "net": request["net"],
			"request_json": json.dumps(request, ensure_ascii=False, sort_keys=True),
			"request_fingerprint": fingerprint,
		})
		record.flags.paypal_receipt_internal = True
		record.save(ignore_permissions=True)


def paypal_receipt_procedure(sales_order: str, gross: float, fee: float, net: float, posting_date: str,
	transaction_id: str, reference_date: str, customer: str = "", currency: str = "USD", dry_run: bool = False):
	"""登记已人工确认到账的 PayPal 美元订单预收；已有销售发票请按发票收款。

	必须确认真实交易号、客户、订单、付款总额、手续费及净到账 USD。不会连接 PayPal、
	发起扣款或提现。重复调用返回原凭证；dry_run 只预检；失败回滚本次写入。
	"""
	stage, savepoint, existing_doc = "input", None, None
	try:
		_require(frappe.session.user and frappe.session.user != "Guest", "必须登录")
		_require(currency == "USD", "此工具仅支持 PayPal 美元收款")
		_require(isinstance(dry_run, bool), "dry_run 必须为布尔值")
		transaction_id = normalize_transaction_id(transaction_id)
		amounts = accounting.validate_amounts(gross, fee, net)
		for date in (posting_date, reference_date):
			_require(isinstance(date, str) and len(date) == 10 and str(getdate(date)) == date, "日期必须为 YYYY-MM-DD")
		order = _load("Sales Order", str(sales_order or "").strip())
		_require(order.company == accounting.COMPANY and order.currency == currency, "销售订单公司或币种不匹配")
		_require(not customer or order.customer == customer, "客户不匹配")
		_load("Account", accounting.BANK)
		_require(frappe.has_permission("Journal Entry", "read") and frappe.has_permission("GL Entry", "read"), "需要会计凭证和总账的读取权限以完成核验")
		request = {"sales_order": order.name, "customer": order.customer, "currency": currency,
			"transaction_id": transaction_id, "posting_date": posting_date, "reference_date": reference_date,
			**{k: str(v) for k, v in amounts.items()}}
		fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
		stage = "duplicate_check"
		if not dry_run:
			candidate_savepoint = "paypal_" + uuid.uuid4().hex
			frappe.db.savepoint(candidate_savepoint)
			savepoint = candidate_savepoint
			_merchant_lock()
			order = _load("Sales Order", order.name, lock=True)
			_require(order.company == accounting.COMPANY and order.currency == currency and order.customer == request["customer"], "订单在预检后发生变化，请重新核对")
		record = _record(transaction_id, lock=not dry_run)
		matches = _legacy_vouchers(transaction_id, lock=not dry_run)
		_require(len(matches) <= 1, "同交易号存在多张凭证，请人工核对，不能重新创建")
		if record:
			_require(not record.request_fingerprint or record.request_fingerprint == fingerprint, "同交易号的订单、金额或日期与原记录不一致")
			_require(record.voucher_name, "该交易正在登记，请稍后使用同一交易号重试")
			_require(record.status != "Cancelled", "该交易关联的凭证已取消，请核对原记录，不会自动重新入账")
			if not matches:
				matches = [{"doctype": record.voucher_type, "name": record.voucher_name}]
			_require(matches[0]["doctype"] == record.voucher_type and matches[0]["name"] == record.voucher_name, "PayPal 交易登记与会计凭证不一致，请人工核对")
		if matches:
			row = matches[0]
			existing_doc = _load(row["doctype"], row["name"], lock=not dry_run)
			_require(existing_doc.docstatus == 1, "交易已有草稿或已取消凭证，请处理原单，不会重复创建")
			_require(existing_doc.doctype == "Journal Entry", "交易已通过原生收款单登记，请在原收款单核验，不会重复创建")
			stage = "verify_existing"
			summary = accounting.verify_receipt(existing_doc, order, amounts, posting_date, transaction_id, reference_date)
			if not dry_run:
				_remember_request(existing_doc, order, request, fingerprint)
			return {"status": "already_recorded", "verified": True, "submitted": True, "dry_run": dry_run,
				"journal_entry": existing_doc.name, "sales_order": order.name, "currency": "USD", **summary}
		stage = "preflight"
		accounting.assert_new_order_allowed(order, amounts["gross"])
		doc = accounting.build_receipt(order, amounts, posting_date, transaction_id, reference_date)
		doc.check_permission("create")
		doc.check_permission("submit")
		if dry_run:
			return {"status": "preflight_passed", "dry_run": True, "verified": False, "submitted": False,
				"currency": "USD", "payload": doc.as_dict(), "note": "只读预检；尚未保存、提交或创建收款记录"}
		before = accounting.money(order.get("advance_paid") or 0)
		stage = "create"
		doc.insert()
		accounting.verify_receipt(doc, order, amounts, posting_date, transaction_id, reference_date, check_gl=False, new_receipt=True)
		stage = "submit"
		expected_rate = doc.flags.paypal_expected_exchange_rate
		doc.submit()
		stage = "verify"
		doc = _load("Journal Entry", doc.name, lock=True)
		doc.flags.paypal_expected_exchange_rate = expected_rate
		summary = accounting.verify_receipt(doc, order, amounts, posting_date, transaction_id, reference_date, new_receipt=True)
		updated = _load("Sales Order", order.name, lock=True)
		_require(accounting.money(updated.advance_paid) == before + amounts["gross"], "订单预收款未按本笔金额正确增加")
		_remember_request(doc, order, request, fingerprint)
		return {"status": "success", "verified": True, "submitted": True, "gl_generated": True,
			"journal_entry": doc.name, "sales_order": order.name, "currency": "USD", "order_advance_paid": updated.advance_paid, **summary}
	except Exception as exc:
		if savepoint:
			# Never swallow rollback failures and then claim that the receipt was undone.
			frappe.db.rollback(save_point=savepoint)
		return {"status": "existing_receipt_needs_review" if existing_doc else "error", "stage": stage,
			"error": str(exc), "verified": False, "rolled_back": bool(savepoint),
			"journal_entry": existing_doc.name if existing_doc and existing_doc.doctype == "Journal Entry" else None,
			"submitted": existing_doc.docstatus == 1 if existing_doc else False,
			"note": "原凭证保留，本次未重复记账" if existing_doc else "本次未生成持久收款凭证"}
