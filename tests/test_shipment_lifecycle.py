"""Offline invariants for cancellation and shipment source links."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class ValidationError(Exception):
	pass


class Doc(types.SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


def load_module():
	frappe = types.ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.throw = lambda message, *args, **kwargs: (_ for _ in ()).throw(ValidationError(message))
	frappe.utils = types.ModuleType("frappe.utils")
	frappe.utils.cint = lambda value: int(value or 0)
	frappe.db = Mock()
	spec = importlib.util.spec_from_file_location(
		"shipment_lifecycle_under_test",
		Path(__file__).parents[1] / "erpnext/stock/doctype/shipment/shipment_lifecycle.py",
	)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": frappe.utils}):
		spec.loader.exec_module(module)
	return module, frappe


class ShipmentLifecycleTests(unittest.TestCase):
	def setUp(self):
		self.mod, self.frappe = load_module()

	def test_duplicate_delivery_note_is_rejected(self):
		doc = Doc(shipment_delivery_note=[Doc(delivery_note="DN-1"), Doc(delivery_note="DN-1")])
		with self.assertRaisesRegex(ValidationError, "twice"):
			self.mod.linked_delivery_notes(doc)

	def test_delivery_note_cancel_blocks_active_shipment(self):
		self.frappe.db.get_values.return_value = [Doc(parent="SHIP-1")]
		self.frappe.db.get_value.return_value = Doc(docstatus=0, status="Draft")
		with self.assertRaisesRegex(ValidationError, "active Shipment"):
			self.mod.before_cancel_delivery_note(Doc(name="DN-1"))

	def test_sales_order_cancel_blocks_active_delivery_note(self):
		self.frappe.db.get_values.side_effect = [[Doc(parent="DN-1")], []]
		self.frappe.db.get_value.return_value = Doc(docstatus=1, status="To Bill")
		with self.assertRaisesRegex(ValidationError, "active Delivery Note"):
			self.mod.before_cancel_sales_order(Doc(name="SO-1"))

	def test_missing_sales_order_is_allowed_for_delivery_note(self):
		self.frappe.db.get_values.return_value = []
		self.frappe.db.get_value.return_value = None
		self.mod.validate_delivery_note_orders(Doc(name="DN-1", docstatus=1, items=[]))

	def test_carrier_cancelled_status_still_blocks_until_local_shipment_is_cancelled(self):
		self.frappe.db.get_values.return_value = [Doc(parent="SHIP-1")]
		self.frappe.db.get_value.return_value = Doc(docstatus=1, status="已取消发货")
		with self.assertRaisesRegex(ValidationError, "active Shipment"):
			self.mod.before_cancel_delivery_note(Doc(name="DN-1"))

	def test_cancelled_local_shipment_allows_delivery_note_cancel(self):
		self.frappe.db.get_values.return_value = [Doc(parent="SHIP-1")]
		self.frappe.db.get_value.return_value = Doc(docstatus=2, status="已取消发货")
		self.mod.before_cancel_delivery_note(Doc(name="DN-1"))

	def test_active_shipment_blocks_reuse_even_with_cancelled_display_status(self):
		self.frappe.db.get_values.side_effect = [[], [Doc(parent="OTHER-SHIPMENT")]]
		self.frappe.db.get_value.side_effect = [
			Doc(docstatus=1, status="To Bill"), Doc(docstatus=1, status="Cancelled")
		]
		doc = Doc(name="NEW-SHIPMENT", docstatus=0, shipment_delivery_note=[Doc(delivery_note="DN-1")])
		with self.assertRaisesRegex(ValidationError, "active Shipment OTHER-SHIPMENT"):
			self.mod.validate_shipment_links(doc)

	def test_draft_delivery_note_cannot_be_used_for_shipment(self):
		self.frappe.db.get_value.return_value = Doc(docstatus=0, status="Draft")
		doc = Doc(name="NEW-SHIPMENT", docstatus=0, shipment_delivery_note=[Doc(delivery_note="DN-1")])
		with self.assertRaisesRegex(ValidationError, "submitted and active"):
			self.mod.validate_shipment_links(doc)

	def test_cancelled_shipment_does_not_revalidate_source_documents(self):
		self.mod.validate_shipment_links(Doc(name="CANCELLED", docstatus=2))
		self.frappe.db.get_value.assert_not_called()
		self.frappe.db.get_values.assert_not_called()


if __name__ == "__main__":
	unittest.main()
