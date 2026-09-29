"""Focused transactional tests; no Frappe database or PayPal requests."""

import copy
import importlib.util
import json
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "erpnext/accounts/doctype/payment_entry"


class Document:
	def __init__(self, harness, values):
		object.__setattr__(self, "harness", harness)
		object.__setattr__(self, "values", values)
		object.__setattr__(self, "flags", types.SimpleNamespace())

	def __getattr__(self, key):
		return self.values.get(key)

	def __setattr__(self, key, value):
		self.values[key] = value

	def __deepcopy__(self, memo):
		cloned = Document(self.harness, copy.deepcopy(self.values, memo))
		object.__setattr__(cloned, "flags", copy.deepcopy(self.flags, memo))
		return cloned

	def __eq__(self, other):
		return isinstance(other, Document) and self.values == other.values

	def get(self, key, default=None):
		return self.values.get(key, default)

	def set(self, key, value):
		self.values[key] = value

	def update(self, values):
		self.values.update(values)

	def as_dict(self):
		return copy.deepcopy(self.values)

	def check_permission(self, permission):
		self.harness.permissions.append((self.doctype, self.name, permission))
		if (self.doctype, self.name, permission) in self.harness.denied:
			raise PermissionError("无权访问此凭证")

	def is_new(self):
		return self.name not in self.harness.state.get(self.doctype, {})

	def get_doc_before_save(self):
		return self.harness.before_save

	def insert(self):
		self.harness.inserts += 1
		self.harness.module.guard_receipt(self)
		self.harness.state[self.doctype][self.name] = self.values
		self.harness.module.register_voucher(self)
		if self.harness.failure == "insert":
			raise ValueError("insert failed after write")
		return self

	def submit(self):
		self.harness.submits += 1
		self.harness.module.guard_receipt(self)
		self.docstatus = 1
		self.harness.state["gl"].append(self.name)
		self.harness.state["Sales Order"]["SO-1"]["advance_paid"] += Decimal("100.00")
		self.harness.module.register_voucher(self)
		if self.harness.failure == "submit":
			raise ValueError("submit failed after ledger")
		return self

	def save(self, **kwargs):
		assert self.flags.paypal_receipt_internal is True
		if self.name not in self.harness.state.get(self.doctype, {}):
			assert self.get("__islocal"), "Named new records must use insert semantics"
		self.harness.writes += 1
		self.harness.state[self.doctype][self.name] = self.values
		return self


class Harness:
	def __init__(self, monkeypatch):
		self.state = {
			"Sales Order": {"SO-1": dict(doctype="Sales Order", name="SO-1", company="Company",
				customer="Customer", currency="USD", docstatus=1, grand_total=Decimal("500"),
				advance_paid=Decimal("0"), disable_rounded_total=1)},
			"Account": {"PayPal USD": dict(doctype="Account", name="PayPal USD")},
			"Journal Entry": {}, "Payment Entry": {}, "PayPal Receipt Record": {}, "gl": [],
			"unrelated": ["earlier request work"],
		}
		self.snapshots, self.events, self.permissions = {}, [], []
		self.denied = set()
		self.inserts = self.submits = self.writes = 0
		self.failure = None
		self.before_save = None
		self.frappe = types.ModuleType("frappe")
		self.frappe.throw = lambda message, *args: (_ for _ in ()).throw(ValueError(message))
		self.frappe.PermissionError = PermissionError
		self.frappe.session = types.SimpleNamespace(user="accountant@example.test")
		self.frappe.has_permission = Mock(return_value=True)
		self.frappe.get_doc = self.get_doc
		self.frappe.db = types.SimpleNamespace(get_value=self.get_value, sql=self.sql,
			savepoint=self.savepoint, rollback=self.rollback, commit=Mock())
		utils = types.ModuleType("frappe.utils")
		utils.getdate = lambda value: date.fromisoformat(value)
		accounting = types.ModuleType("paypal_receipt_test.paypal_accounting")
		accounting.COMPANY = "Company"
		accounting.BANK = "PayPal USD"
		accounting.money = lambda value: Decimal(str(value)).quantize(Decimal("0.01"))
		accounting.decimal_value = lambda value: Decimal(str(value))
		accounting.validate_amounts = lambda gross, fee, net: {
			key: accounting.money(value) for key, value in dict(gross=gross, fee=fee, net=net).items()
		}
		accounting.assert_new_order_allowed = Mock()
		accounting.build_receipt = Mock(side_effect=self.build)
		accounting.verify_receipt = Mock(side_effect=self.verify)
		self.accounting = accounting
		package = types.ModuleType("paypal_receipt_test")
		package.__path__ = [str(MODULE_DIR)]
		package.paypal_accounting = accounting
		for name, module in {"frappe": self.frappe, "frappe.utils": utils,
			"paypal_receipt_test": package, "paypal_receipt_test.paypal_accounting": accounting}.items():
			monkeypatch.setitem(sys.modules, name, module)
		spec = importlib.util.spec_from_file_location("paypal_receipt_test.paypal_receipt", MODULE_DIR / "paypal_receipt.py")
		self.module = importlib.util.module_from_spec(spec)
		monkeypatch.setitem(sys.modules, spec.name, self.module)
		spec.loader.exec_module(self.module)

	def get_doc(self, doctype, name=None, **kwargs):
		if isinstance(doctype, dict):
			return Document(self, copy.deepcopy(doctype))
		if kwargs.get("for_update"):
			self.events.append(("lock", doctype, name))
		return Document(self, self.state[doctype][name])

	def get_value(self, doctype, filters, field, **kwargs):
		if kwargs.get("for_update"):
			self.events.append(("lock", doctype, filters))
		rows = self.state.get(doctype, {})
		if isinstance(filters, dict):
			return next((row.get(field) for row in rows.values()
				if all(row.get(key) == value for key, value in filters.items())), None)
		return rows.get(filters, {}).get(field)

	def sql(self, query, values, **kwargs):
		doctype = "Journal Entry" if "tabJournal Entry" in query else "Payment Entry"
		field = "cheque_no" if doctype == "Journal Entry" else "reference_no"
		self.events.append(("duplicate_query", doctype, "FOR UPDATE" in query))
		return [{"name": row["name"], "docstatus": row["docstatus"]}
			for row in self.state[doctype].values()
			if row["company"] == values[0] and str(row.get(field) or "").strip().upper() == values[1]][:3]

	def savepoint(self, name):
		self.events.append(("savepoint", name))
		self.snapshots[name] = copy.deepcopy(self.state)

	def rollback(self, *, save_point):
		self.events.append(("rollback", save_point))
		self.state = copy.deepcopy(self.snapshots[save_point])

	def build(self, order, amounts, posting_date, transaction_id, reference_date):
		doc = self.journal("JE-NEW", transaction_id=transaction_id)
		doc.flags.paypal_expected_exchange_rate = "7.10"
		return doc

	def verify(self, doc, order, amounts, *args, **kwargs):
		if self.failure == "verify_draft" and not kwargs.get("check_gl", True):
			raise ValueError("draft verification failed")
		if self.failure == "verify" and kwargs.get("check_gl", True):
			raise ValueError("ledger verification failed")
		return {"gross": float(amounts["gross"]), "fee": float(amounts["fee"]), "net": float(amounts["net"])}

	def journal(self, name="JE-EXISTING", *, status=0, transaction_id="TX-1"):
		return Document(self, dict(doctype="Journal Entry", name=name, company="Company", docstatus=status,
			cheque_no=transaction_id, cheque_date="2026-09-16", posting_date="2026-09-16", accounts=[
				Document(self, dict(account="PayPal USD", debit_in_account_currency=Decimal("96.00"), account_currency="USD")),
				Document(self, dict(account="Receivable USD", party_type="Customer", party="Customer",
					credit_in_account_currency=Decimal("100.00"), account_currency="USD",
					reference_type="Sales Order", reference_name="SO-1")),
			]))

	def payment(self, name="PE-NEW", *, transaction_id="TX-1"):
		return Document(self, dict(doctype="Payment Entry", name=name, company="Company", docstatus=0,
			payment_type="Receive", party_type="Customer", paid_to="PayPal USD", reference_no=transaction_id,
			posting_date="2026-09-16", reference_date="2026-09-16", references=[Document(self, dict(
				reference_doctype="Sales Order", reference_name="SO-1", allocated_amount="100.00"))]))

	def existing(self, *, status=1, kind="Journal Entry", name="JE-EXISTING"):
		doc = self.journal(name, status=status) if kind == "Journal Entry" else self.payment(name)
		doc.docstatus = status
		self.state[kind][name] = doc.values
		return doc

	def call(self, **overrides):
		values = dict(sales_order="SO-1", gross=100, fee=4, net=96, posting_date="2026-09-16",
			transaction_id="TX-1", reference_date="2026-09-16")
		values.update(overrides)
		return self.module.paypal_receipt_procedure(**values)


@pytest.fixture
def harness(monkeypatch):
	return Harness(monkeypatch)


@pytest.mark.parametrize("failure", ["insert", "verify_draft", "submit", "verify"])
def test_failure_rolls_back_voucher_ledger_receipt_and_advance_only(harness, failure):
	before = copy.deepcopy(harness.state)
	harness.failure = failure
	result = harness.call()
	assert result["status"] == "error"
	assert result["rolled_back"] is True
	assert result["submitted"] is False
	assert result["journal_entry"] is None
	assert harness.state == before
	assert harness.inserts == 1
	assert sum(event[0] == "rollback" for event in harness.events) == 1
	harness.frappe.db.commit.assert_not_called()


def test_success_then_retry_reuses_same_voucher_without_insert(harness):
	first = harness.call()
	assert first["status"] == "success", first
	assert harness.state["gl"] == ["JE-NEW"]
	assert harness.state["Sales Order"]["SO-1"]["advance_paid"] == Decimal("100")
	record = next(iter(harness.state["PayPal Receipt Record"].values()))
	assert record["status"] == "Recorded"
	assert record["request_fingerprint"]
	assert json.loads(record["request_json"])["gross"] == "100.00"
	second = harness.call(transaction_id="  tx-1 ")
	assert second["status"] == "already_recorded", second
	assert second["journal_entry"] == first["journal_entry"]
	assert harness.inserts == harness.submits == 1
	assert len(harness.state["gl"]) == 1
	harness.frappe.db.commit.assert_not_called()


def test_retry_after_native_invoice_allocation_does_not_recheck_order_advance(harness):
	assert harness.call()["status"] == "success"
	harness.state["Sales Order"]["SO-1"]["advance_paid"] = Decimal("0")
	harness.state["Sales Order"]["SO-1"]["per_billed"] = 100
	harness.accounting.assert_new_order_allowed.reset_mock()
	result = harness.call()
	assert result["status"] == "already_recorded", result
	harness.accounting.assert_new_order_allowed.assert_not_called()
	assert harness.inserts == 1


@pytest.mark.parametrize("changed", [dict(gross=110, fee=4, net=106), dict(posting_date="2026-09-15"), dict(reference_date="2026-09-15")])
def test_existing_request_fingerprint_conflict_cannot_create(harness, changed):
	assert harness.call()["status"] == "success"
	result = harness.call(**changed)
	assert result["verified"] is False
	assert "不一致" in result["error"]
	assert harness.inserts == harness.submits == 1
	assert len(harness.state["gl"]) == 1


@pytest.mark.parametrize("status", [0, 2])
def test_existing_draft_or_cancelled_voucher_is_not_replaced(harness, status):
	existing = harness.existing(status=status)
	result = harness.call()
	assert result["verified"] is False
	assert result["journal_entry"] == existing.name
	assert result["submitted"] is False
	assert harness.inserts == harness.submits == 0
	assert existing.name in harness.state["Journal Entry"]


def test_cancelled_receipt_identity_stays_reserved(harness):
	assert harness.call()["status"] == "success"
	record = next(iter(harness.state["PayPal Receipt Record"].values()))
	record["status"] = "Cancelled"
	harness.state["Journal Entry"]["JE-NEW"]["docstatus"] = 2
	result = harness.call()
	assert result["verified"] is False
	assert "取消" in result["error"]
	assert harness.inserts == 1


@pytest.mark.parametrize("existing", [False, True])
def test_dry_run_never_saves_writes_or_acquires_mutating_locks(harness, existing):
	if existing:
		harness.existing()
	before = copy.deepcopy(harness.state)
	result = harness.call(dry_run=True)
	assert result["status"] == ("already_recorded" if existing else "preflight_passed"), result
	assert harness.state == before
	assert harness.inserts == harness.submits == harness.writes == 0
	assert not any(event[0] in ("lock", "savepoint", "rollback") for event in harness.events)
	assert all(not event[2] for event in harness.events if event[0] == "duplicate_query")
	harness.frappe.db.commit.assert_not_called()


def test_global_duplicate_detection_blocks_invisible_voucher_without_leaking_name(harness):
	secret = "JE-CONFIDENTIAL"
	harness.existing(name=secret)
	harness.denied.add(("Journal Entry", secret, "read"))
	result = harness.call()
	assert result["verified"] is False
	assert result["journal_entry"] is None
	assert secret not in json.dumps(result, ensure_ascii=False)
	assert harness.inserts == 0
	harness.accounting.build_receipt.assert_not_called()


def test_multiple_legacy_vouchers_block_before_disclosing_names(harness):
	harness.existing(name="JE-SECRET-A")
	harness.existing(name="JE-SECRET-B")
	result = harness.call()
	assert result["verified"] is False
	assert "JE-SECRET" not in json.dumps(result, ensure_ascii=False)
	assert harness.inserts == 0


def test_existing_native_payment_entry_blocks_journal_creation(harness):
	harness.existing(kind="Payment Entry", name="PE-EXISTING")
	result = harness.call()
	assert result["verified"] is False
	assert "原生收款单" in result["error"]
	assert harness.inserts == 0


@pytest.mark.parametrize("kind", ["Journal Entry", "Payment Entry"])
def test_native_guard_locks_merchant_then_order_and_blocks_other_voucher(harness, kind):
	harness.existing(name="JE-ORIGINAL")
	doc = harness.journal("JE-OTHER") if kind == "Journal Entry" else harness.payment()
	with pytest.raises(ValueError, match="已有凭证"):
		harness.module.guard_receipt(doc)
	locks = [event for event in harness.events if event[0] == "lock"]
	assert locks[0] == ("lock", "Account", "PayPal USD")
	assert ("lock", "Sales Order", "SO-1") in locks
	assert all(event[2] for event in harness.events if event[0] == "duplicate_query")
	assert harness.inserts == 0


@pytest.mark.parametrize("kind", ["Journal Entry", "Payment Entry"])
def test_native_record_blocks_cross_entry_duplicate_after_original_is_deleted(harness, kind):
	original = harness.existing(name="JE-ORIGINAL")
	harness.module.register_voucher(original)
	harness.state["Journal Entry"].clear()
	doc = harness.journal("JE-OTHER") if kind == "Journal Entry" else harness.payment()
	with pytest.raises(ValueError, match="已有收款记录"):
		harness.module.guard_receipt(doc)


def test_non_paypal_payment_still_locks_referenced_order(harness):
	doc = harness.payment()
	doc.paid_to = "Other Bank"
	harness.module.guard_receipt(doc)
	assert ("lock", "Sales Order", "SO-1") in harness.events
	assert not any(event[:2] == ("lock", "Account") for event in harness.events)


def test_native_paypal_receipt_cannot_exceed_order_advance_balance(harness):
	harness.state["Sales Order"]["SO-1"]["advance_paid"] = Decimal("450")
	with pytest.raises(ValueError, match="可收预付款余额"):
		harness.module.guard_receipt(harness.journal("JE-OTHER"))


def test_saved_transaction_number_cannot_be_changed(harness):
	doc = harness.existing(name="JE-ORIGINAL")
	harness.module.register_voucher(doc)
	doc.cheque_no = "TX-2"
	with pytest.raises(ValueError, match="不能直接更改"):
		harness.module.guard_receipt(doc)


def test_canonical_transaction_key_ignores_outer_whitespace_and_letter_case(harness):
	assert harness.module.receipt_key("tx-123") == harness.module.receipt_key("  TX-123 ")
	assert harness.module.receipt_key("TX-124") != harness.module.receipt_key("TX-123")
	assert len(harness.module.receipt_key("TX-123")) == 64
	for invalid in ("", "a b", "a/b", "x" * 141):
		with pytest.raises(ValueError):
			harness.module.receipt_key(invalid)


def test_rollback_failure_propagates_instead_of_claiming_successful_rollback(harness):
	harness.failure = "verify"
	harness.frappe.db.rollback = Mock(side_effect=RuntimeError("database connection lost"))
	with pytest.raises(RuntimeError, match="database connection lost"):
		harness.call()


def test_savepoint_creation_failure_does_not_attempt_rollback_or_write(harness):
	harness.frappe.db.savepoint = Mock(side_effect=RuntimeError("cannot create savepoint"))
	harness.frappe.db.rollback = Mock()
	result = harness.call()
	assert result["error"] == "cannot create savepoint"
	assert not result["rolled_back"]
	assert harness.inserts == harness.writes == 0
	harness.frappe.db.rollback.assert_not_called()
