# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Unit tests for the native shipping query boundary without a carrier app or site."""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class Row(dict):
	__getattr__ = dict.get


class TestShipmentSummary(unittest.TestCase):
	def setUp(self):
		self.frappe = types.ModuleType("frappe")
		self.frappe._ = lambda value: value
		self.frappe.whitelist = self.frappe.read_only = lambda: lambda function: function
		self.frappe.PermissionError = PermissionError
		self.frappe.ValidationError = ValueError
		self.frappe.get_all = Mock()
		self.frappe.get_list = Mock()
		self.frappe.get_hooks = Mock(return_value=[])
		self.frappe.call = Mock()
		self.frappe.db = Mock()
		self.frappe.clear_cache = Mock()
		self.frappe.form_dict = {"doctype": "Delivery Note"}
		self.frappe.throw = Mock(side_effect=ValueError("Invalid DocType"))
		desk = types.ModuleType("frappe.desk")
		self.reportview = desk.reportview = types.SimpleNamespace(get=Mock())
		utils = types.ModuleType("frappe.utils")
		utils.cint = lambda value: int(value or 0)
		modules = {"frappe": self.frappe, "frappe.desk": desk, "frappe.utils": utils}
		with patch.dict(sys.modules, modules):
			spec = importlib.util.spec_from_file_location(
				"native_shipment_summary_test", Path(__file__).with_name("shipment_summary.py")
			)
			self.summary = importlib.util.module_from_spec(spec)
			spec.loader.exec_module(self.summary)

	def shipments(self, *, visible=None, cancelled=False):
		rows = [
			Row(
				name="NEW", shipment_id="WAYBILL-NEW", docstatus=2 if cancelled else 1,
				modified="2026-09-26 12:00:00", service_provider="Example Carrier",
				status="Cancelled" if cancelled else "Booked", tracking_status="In Progress",
			),
			Row(
				name="OLD", shipment_id="WAYBILL-OLD", docstatus=2,
				modified="2026-09-25 12:00:00", service_provider="Example Carrier",
				status="Cancelled", tracking_status="Returned",
			),
		]
		links = [Row(delivery_note="DN-1", parent=row.name) for row in rows]
		self.frappe.get_all.side_effect = [links, rows]
		self.frappe.get_list.return_value = rows if visible is None else [rows[i] for i in visible]
		return rows

	def test_active_selection_prefers_waybill_then_modified_with_stable_name_tie_break(self):
		rows = [
			Row(name="DRAFT", docstatus=0, modified="2026-09-27"),
			Row(name="B", docstatus=1, awb_number="B1", modified="2026-09-25"),
			Row(name="CANCELLED", docstatus=2, shipment_id="C1", modified="2026-09-28"),
			Row(name="A", docstatus=1, shipment_id="A1", modified="2026-09-25"),
		]
		self.assertEqual(self.summary.select_linked_shipment_row(rows).name, "A")

	def test_cancelled_history_selects_latest_even_without_waybill(self):
		rows = [
			Row(name="OLD", docstatus=2, shipment_id="OLD1", modified="2026-09-25"),
			Row(name="NEW", docstatus=2, modified="2026-09-26"),
		]
		self.assertIsNone(self.summary.select_linked_shipment_row(rows))
		self.assertEqual(self.summary.select_linked_shipment_row(rows, include_cancelled=True).name, "NEW")

	def test_native_summary_works_without_sf_fields_or_hooks(self):
		self.shipments()
		result = self.summary._shipping_rows(["DN-1", "DN-2"])
		self.assertEqual(result["DN-1"]["current_shipment"], "NEW")
		self.assertEqual(result["DN-1"]["shipments"][0]["service_provider"], "Example Carrier")
		self.assertEqual(result["DN-2"], {"shipments": [], "current_shipment": None, "source_cancelled": False})
		fields = self.frappe.get_list.call_args.kwargs["fields"]
		self.assertFalse(any(field.startswith(("sf_", "manual_")) for field in fields))
		self.assertEqual(self.frappe.get_list.call_count, 1)
		self.frappe.call.assert_not_called()

	def test_unreadable_current_cannot_be_replaced_with_readable_history(self):
		self.shipments(visible=[1])
		result = self.summary._shipping_rows(["DN-1"])["DN-1"]
		self.assertTrue(result["restricted_current"])
		self.assertIsNone(result["current_shipment"])
		self.assertEqual([row["shipment"] for row in result["shipments"]], ["OLD"])
		self.assertNotIn("WAYBILL-NEW", json.dumps(result))
		self.assertNotIn('"NEW"', json.dumps(result))

	def test_no_shipment_permission_preserves_access_to_delivery_note(self):
		self.shipments()
		self.frappe.get_list.side_effect = PermissionError("Shipment")
		result = self.summary._shipping_rows(["DN-1"])["DN-1"]
		self.assertTrue(result["restricted_current"])
		self.assertEqual(result["shipments"], [])
		self.assertIsNone(result["current_shipment"])
		self.frappe.call.assert_not_called()

	def test_cancelled_history_uses_only_readable_rows(self):
		self.shipments(visible=[1], cancelled=True)
		result = self.summary._shipping_rows(["DN-1"])["DN-1"]
		self.assertEqual(result["current_shipment"], "OLD")
		self.assertNotIn("restricted_current", result)

	def test_all_history_restricted_is_explicit_without_identifiers(self):
		self.shipments(visible=[], cancelled=True)
		result = self.summary._shipping_rows(["DN-1"])["DN-1"]
		self.assertTrue(result["restricted"])
		self.assertEqual(result["shipments"], [])
		self.assertIsNone(result["current_shipment"])

	def test_provider_enrichment_is_batched_and_cannot_change_native_selection(self):
		self.shipments(visible=[1])
		self.frappe.get_hooks.return_value = ["example.shipping.decorate"]

		def decorate(method, *, shipments):
			self.assertEqual([row["name"] for row in shipments], ["OLD"])
			shipments[0]["docstatus"] = 1
			shipments[0]["shipment_id"] = "REPLACED"
			return {
				"NEW": {"status_label": "restricted information"},
				"OLD": {
					"shipment": "NEW", "docstatus": 1, "status_label": "Returned",
					"status_color": "orange", "extra_details": "Carrier-confirmed return",
				},
			}

		self.frappe.call.side_effect = decorate
		result = self.summary._shipping_rows(["DN-1"])["DN-1"]
		self.frappe.call.assert_called_once()
		self.assertTrue(result["restricted_current"])
		self.assertEqual(result["shipments"][0]["docstatus"], 2)
		self.assertEqual(result["shipments"][0]["shipment_id"], "WAYBILL-OLD")
		self.assertEqual(result["shipments"][0]["status_label"], "Returned")
		self.assertNotIn("restricted information", json.dumps(result))

	def test_list_enriches_only_names_authorized_by_native_reportview(self):
		self.shipments()
		self.reportview.get.return_value = {
			"keys": ["name", "docstatus"], "values": [["DN-1", 2], ["DN-2", 1]], "user_info": {}
		}
		result = self.summary.get_delivery_notes()
		self.reportview.get.assert_called_once_with()
		self.assertEqual(result["keys"], ["name", "docstatus", "_shipping"])
		self.assertTrue(result["values"][0][-1]["source_cancelled"])
		self.assertEqual(result["values"][1][-1]["shipments"], [])
		self.assertEqual(self.frappe.get_all.call_args_list[0].kwargs["filters"]["delivery_note"], ["in", ["DN-1", "DN-2"]])

	def test_list_rejects_other_doctypes_before_query(self):
		self.frappe.form_dict["doctype"] = "Sales Order"
		with self.assertRaises(ValueError):
			self.summary.get_delivery_notes()
		self.reportview.get.assert_not_called()

	def test_onload_includes_summary_in_form_response(self):
		self.shipments()
		doc = Mock(name="delivery note")
		doc.name = "DN-1"
		doc.docstatus = 2
		doc.is_new.return_value = False
		self.summary.load_delivery_note_shipping_summary(doc)
		key, result = doc.set_onload.call_args.args
		self.assertEqual(key, "shipping_summary")
		self.assertTrue(result["source_cancelled"])
		self.assertEqual(result["current_shipment"], "NEW")

	def test_new_form_has_explicit_empty_summary_without_query(self):
		doc = Mock()
		doc.is_new.return_value = True
		doc.docstatus = 0
		self.summary.load_delivery_note_shipping_summary(doc)
		self.assertEqual(doc.set_onload.call_args.args[1]["shipments"], [])
		self.frappe.get_all.assert_not_called()

	def test_cleanup_matches_exact_known_name_and_view(self):
		self.frappe.get_all.return_value = [
			Row(name="Delivery Note Shipment List", view="List"),
			Row(name="Delivery Note Shipment Status", view="Form"),
			Row(name="Delivery Note Shipment Status", view="List"),
			Row(name="My Delivery Note Shipment", view="Form"),
		]
		result = self.summary.retire_legacy_delivery_note_shipment_scripts()
		self.assertEqual(result["disabled"], ["Delivery Note Shipment List", "Delivery Note Shipment Status"])
		self.assertEqual(self.frappe.db.set_value.call_count, 2)
		self.frappe.clear_cache.assert_called_once_with(doctype="Delivery Note")

	def test_column_migration_preserves_order_label_width_and_other_settings(self):
		self.frappe.get_all.return_value = []
		fields = [
			{"fieldname": "name", "width": 100},
			{"fieldname": "sf_shipment", "label": "运单", "width": 270, "type": "Data"},
			{"fieldname": "customer", "width": 200},
		]
		self.frappe.db.get_value.return_value = json.dumps(fields)
		result = self.summary.migrate_delivery_note_shipment_presentation()
		self.assertTrue(result["column_migrated"])
		fields[1]["fieldname"] = "shipment"
		self.assertEqual(json.loads(self.frappe.db.set_value.call_args.args[3]), fields)
		self.assertEqual(self.frappe.db.set_value.call_args.args[:3], ("List View Settings", "Delivery Note", "fields"))


if __name__ == "__main__":
	unittest.main()
