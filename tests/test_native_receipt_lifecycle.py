"""Run native accounting lifecycle methods without loading an optional carrier app."""

import ast
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("slug,class_name", [("journal_entry", "JournalEntry"), ("payment_entry", "PaymentEntry")])
def test_native_receipt_lifecycle_preserves_event_order(monkeypatch, slug, class_name):
	events = []
	module = ModuleType("erpnext.accounts.doctype.payment_entry.paypal_receipt")
	module.guard_receipt = lambda doc: events.append("guard")
	module.register_voucher = lambda doc: events.append("register")
	monkeypatch.setitem(sys.modules, module.__name__, module)

	class NativeParent:
		difference_amount = 0

		def on_cancel(self):
			events.append("parent_cancel")

		def __getattr__(self, name):
			if name == "ignore_linked_doctypes":
				return ()
			return lambda *args, **kwargs: events.append(name)

	path = ROOT / f"erpnext/accounts/doctype/{slug}/{slug}.py"
	controller = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == class_name)
	names = {"before_validate", "after_insert", "on_submit", "on_cancel"}
	controller.body = [n for n in controller.body if isinstance(n, ast.FunctionDef) and n.name in names]
	controller.bases = [ast.Name(id="NativeParent", ctx=ast.Load())]
	assert {n.name for n in controller.body} == names
	withholding = lambda doc: SimpleNamespace(on_submit=lambda: events.append("tax_submit"), on_cancel=lambda: events.append("tax_cancel"))
	scope = {"NativeParent": NativeParent, "JournalTaxWithholding": withholding, "PaymentTaxWithholding": withholding}
	exec(compile(ast.fix_missing_locations(ast.Module(body=[controller], type_ignores=[])), str(path), "exec"), scope)
	doc = scope[class_name]()
	for event in names:
		events.clear()
		getattr(doc, event)()
		if event == "before_validate":
			assert events == ["guard"]
		elif event == "after_insert":
			assert events == ["register"]
		else:
			assert "make_gl_entries" in events
			assert events.count("register") == 1
			assert events[-1] == "register"
