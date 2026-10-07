"""Offline accounting edge cases for the USD-only Flow PayPal tool."""

import copy
import importlib.util
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch


class Doc(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__

	def check_permission(self, action):
		if action in self.get("denied", ()):
			raise ValueError("denied " + action)

	def precision(self, field):
		return 9 if field == "exchange_rate" else 2

	def set(self, key, value):
		self[key] = value

	def append(self, field, value):
		row = Doc(value)
		self.setdefault(field, []).append(row)
		return row


class PayPalAccountingTests(unittest.TestCase):
	def setUp(self):
		self.frappe = types.ModuleType("frappe")
		self.frappe.throw = lambda message: (_ for _ in ()).throw(ValueError(message))
		self.frappe.has_permission = Mock(return_value=True)
		self.frappe.db = Mock()
		self.frappe.db.sql.return_value = []
		self.frappe.get_all = Mock(return_value=[])
		utils = types.ModuleType("frappe.utils")
		utils.flt = lambda value, precision=None: round(float(value), precision) if precision is not None else float(value)
		setup = types.ModuleType("erpnext.setup.utils")
		setup.get_exchange_rate = Mock(return_value=7.49)
		self.rate = setup.get_exchange_rate
		self.module_patch = patch.dict(sys.modules, {"frappe": self.frappe, "frappe.utils": utils,
			"erpnext": types.ModuleType("erpnext"), "erpnext.setup": types.ModuleType("erpnext.setup"), "erpnext.setup.utils": setup})
		self.module_patch.start()
		self.addCleanup(self.module_patch.stop)
		path = Path(__file__).resolve().parents[1] / "erpnext/accounts/doctype/payment_entry/paypal_accounting.py"
		spec = importlib.util.spec_from_file_location("paypal_accounting_under_test", path)
		self.a = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.a)
		a = self.a
		self.order = Doc(name="SO-1", company=a.COMPANY, customer="C-1", currency="USD", docstatus=1,
			status="To Deliver and Bill", grand_total=1000, rounded_total=1000, advance_paid=0)
		self.company = Doc(default_currency="CNY", round_off_account="Roundoff", round_off_cost_center=a.COST_CENTER)
		self.docs = {("Company", a.COMPANY): self.company, ("Customer", "C-1"): Doc(disabled=0, is_frozen=0),
			("Cost Center", a.COST_CENTER): Doc(company=a.COMPANY), ("Mode of Payment", a.MODE): Doc(enabled=1),
			("Sales Invoice", "INV-1"): Doc(company=a.COMPANY, customer="C-1", currency="USD", docstatus=1)}
		for account, kind in ((a.BANK, "Bank"), (a.EXPENSE, "Expense Account"), (a.RECEIVABLE, "Receivable")):
			self.docs[("Account", account)] = Doc(company=a.COMPANY, account_currency="USD", account_type=kind)
		self.docs[("Account", "Roundoff")] = Doc(company=a.COMPANY, account_currency="CNY", root_type="Expense")
		self.frappe.get_doc = Mock(side_effect=self._get_doc)

	def _get_doc(self, doctype, name=None):
		if isinstance(doctype, dict):
			doc = Doc(doctype, flags=Doc(), docstatus=0)
			doc.accounts = [Doc(row) for row in doc.accounts]
			return doc
		return self.docs[(doctype, name)]

	def _build(self, gross="100.00", fee="3.00", net="97.00"):
		amounts = self.a.validate_amounts(gross, fee, net)
		doc = self.a.build_receipt(self.order, amounts, "2026-09-16", "TX-1", "2026-09-16")
		return doc, amounts

	def _ledger(self, doc):
		rows = []
		for entry in doc.accounts:
			row = Doc(copy.deepcopy(entry), company=doc.company, posting_date=doc.posting_date)
			if entry.get("reference_type") == "Sales Order":
				row.update(against_voucher_type="Journal Entry", against_voucher=doc.name,
					advance_voucher_type="Sales Order", advance_voucher_no=self.order.name)
			else:
				row.update(against_voucher_type=entry.get("reference_type"), against_voucher=entry.get("reference_name"))
			row.pop("advance_voucher_type", None)
			row.pop("advance_voucher_no", None)
			rows.append(row)
		return rows

	def _verify(self, doc, amounts, **kwargs):
		return self.a.verify_receipt(doc, self.order, amounts, "2026-09-16", "TX-1", "2026-09-16", **kwargs)

	def test_decimal_validation_rejects_nonfinite_subcent_and_unbalanced_amounts(self):
		for value in ("NaN", "Infinity", "0.001", True, None, "bad"):
			with self.subTest(value=value), self.assertRaises(ValueError):
				self.a.money(value)
		with self.assertRaisesRegex(ValueError, "不平衡"):
			self.a.validate_amounts("100", "3", "98")
		self.assertEqual(self.a.validate_amounts(0.3, 0.1, 0.2)["net"], Decimal("0.20"))

	def test_new_receipt_rejects_partial_invoice_and_stale_order_billed_percent(self):
		self.order.per_billed = 1
		with self.assertRaisesRegex(ValueError, "已开票"):
			self.a.assert_new_order_allowed(self.order, Decimal("100"))
		self.order.per_billed = 0
		self.frappe.db.sql.return_value = [("INV-1",)]
		with self.assertRaisesRegex(ValueError, "已开票"):
			self.a.assert_new_order_allowed(self.order, Decimal("100"))

	def test_new_receipt_rejects_excess_advance_and_unavailable_customer(self):
		self.order.advance_paid = 950
		with self.assertRaisesRegex(ValueError, "余额"):
			self.a.assert_new_order_allowed(self.order, Decimal("100"))
		self.order.advance_paid = 0
		self.docs[("Customer", "C-1")].is_frozen = 1
		with self.assertRaisesRegex(ValueError, "冻结"):
			self.a.assert_new_order_allowed(self.order, Decimal("100"))

	def test_ledger_permission_and_submit_permission_checked_before_insert(self):
		self.frappe.has_permission.return_value = False
		with self.assertRaisesRegex(ValueError, "总账读取"):
			self._build()
		self.rate.assert_not_called()
		self.frappe.has_permission.return_value = True
		original = self._get_doc
		def deny_submit(doctype, name=None):
			doc = original(doctype, name)
			if isinstance(doctype, dict):
				doc.denied = ["submit"]
			return doc
		self.frappe.get_doc.side_effect = deny_submit
		with self.assertRaisesRegex(ValueError, "submit"):
			self._build()

	def test_missing_native_dated_rate_is_rejected_without_fallback_one(self):
		for rate in (0, None, "NaN", -1):
			self.rate.return_value = rate
			with self.subTest(rate=rate), self.assertRaises(ValueError):
				self._build()
		self.rate.assert_called_with("USD", "CNY", "2026-09-16")

	def test_one_cent_roundoff_uses_base_account_without_changing_usd(self):
		doc, amounts = self._build("0.02", "0.01", "0.01")
		self.assertEqual(len(doc.accounts), 4)
		self.assertEqual(doc.accounts[-1].account_currency, "CNY")
		self.assertEqual(doc.accounts[-1].debit, 0.01)
		self.assertEqual(doc.accounts[0].debit_in_account_currency, 0.01)
		self.assertEqual(doc.accounts[2].credit_in_account_currency, 0.02)
		self.assertTrue(self._verify(doc, amounts, check_gl=False, new_receipt=True)["verified"])

	def test_roundoff_without_config_is_rejected(self):
		self.company.round_off_account = None
		with self.assertRaisesRegex(ValueError, "配置公司的舍入"):
			self._build("0.02", "0.01", "0.01")

	def test_new_receipt_checks_trusted_rate_and_base_amount(self):
		doc, amounts = self._build()
		doc.accounts[0].debit += 1
		with self.assertRaisesRegex(ValueError, "折算金额"):
			self._verify(doc, amounts, check_gl=False, new_receipt=True)
		doc, amounts = self._build()
		for row in doc.accounts:
			row.exchange_rate = 1
			row.debit = row.debit_in_account_currency
			row.credit = row.credit_in_account_currency
		with self.assertRaisesRegex(ValueError, "已核实汇率"):
			self._verify(doc, amounts, check_gl=False, new_receipt=True)

	def test_historical_split_allocations_pass_without_current_rate_or_advance_paid(self):
		doc, amounts = self._build()
		doc.update(name="JE-1", docstatus=1)
		receivable = doc.accounts[-1]
		receivable.credit_in_account_currency = 60
		receivable.credit = 449.4
		allocated = doc.append("accounts", dict(receivable))
		allocated.update(credit_in_account_currency=40, credit=299.6, reference_type="Sales Invoice", reference_name="INV-1",
			advance_voucher_type="Sales Order", advance_voucher_no="SO-1")
		self.order.advance_paid = 0
		self.order.per_billed = 100
		self.frappe.get_all.return_value = self._ledger(doc)
		self.rate.reset_mock()
		self.assertTrue(self._verify(doc, amounts)["verified"])
		self.rate.assert_not_called()

	def test_historical_invoice_reference_from_another_order_is_rejected(self):
		doc, amounts = self._build()
		doc.accounts[-1].update(reference_type="Sales Invoice", reference_name="INV-1",
			advance_voucher_type="Sales Order", advance_voucher_no="OTHER")
		with self.assertRaisesRegex(ValueError, "来源销售订单"):
			self._verify(doc, amounts, check_gl=False)

	def test_gl_aggregates_allow_split_rows_but_reject_bad_base_total(self):
		doc, amounts = self._build()
		doc.update(name="JE-1", docstatus=1)
		ledger = self._ledger(doc)
		credit = ledger[-1]
		credit.credit_in_account_currency = 60
		credit.credit = 449.4
		second = Doc(credit, credit_in_account_currency=40, credit=299.6)
		ledger.append(second)
		self.frappe.get_all.return_value = ledger
		self.assertTrue(self._verify(doc, amounts)["verified"])
		second.credit += 1
		with self.assertRaisesRegex(ValueError, "总账USD或本位币"):
			self._verify(doc, amounts)

	def test_zero_fee_omits_zero_posting_and_still_verifies(self):
		doc, amounts = self._build("100", "0", "100")
		self.assertEqual(len(doc.accounts), 2)
		result = self._verify(doc, amounts, check_gl=False, new_receipt=True)
		self.assertTrue(result["verified"])
		self.assertEqual(result["actual_receipt_currency"], "USD")
		self.assertEqual(result["company_base_currency"], "CNY")
		self.assertIn("不代表实际收到CNY", result["base_currency_note"])

	def test_native_allocation_roundoff_can_exist_in_gl_only(self):
		doc, amounts = self._build("0.02", "0", "0.02")
		doc.update(name="JE-1", docstatus=1)
		receivable = doc.accounts[-1]
		receivable.update(credit_in_account_currency=0.01, credit=0.07)
		allocated = doc.append("accounts", dict(receivable))
		allocated.update(reference_type="Sales Invoice", reference_name="INV-1", advance_voucher_type="Sales Order", advance_voucher_no="SO-1")
		ledger = self._ledger(doc)
		ledger.append(Doc(account="Roundoff", account_currency="CNY", cost_center=self.a.COST_CENTER,
			company=self.a.COMPANY, posting_date="2026-09-16", debit=0, credit=0.01,
			debit_in_account_currency=0, credit_in_account_currency=0.01))
		self.frappe.get_all.return_value = ledger
		self.assertTrue(self._verify(doc, amounts)["verified"])
		ledger[-1].credit = 0.02
		with self.assertRaisesRegex(ValueError, "总账USD或本位币"):
			self._verify(doc, amounts)


if __name__ == "__main__":
	unittest.main()
