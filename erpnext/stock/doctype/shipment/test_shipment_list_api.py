"""Offline regression coverage for native list enrichment and optional carrier summaries."""

import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class ShipmentListApiTests(unittest.TestCase):
	def setUp(self):
		self.frappe = types.ModuleType("frappe")
		self.frappe._ = lambda value: value
		self.frappe.whitelist = self.frappe.read_only = lambda: lambda method: method
		self.frappe.PermissionError = PermissionError
		self.frappe.ValidationError = ValueError
		self.frappe.form_dict = {"doctype": "Sales Order"}
		self.frappe.throw = lambda message, error: (_ for _ in ()).throw(error(message))
		utils = types.ModuleType("frappe.utils")
		utils.cint = lambda value: int(value or 0)
		utils.fmt_money = lambda amount, currency=None: f"{amount:.2f}"
		self.result = {"keys": ["grand_total", "name"], "values": [[20, "SO-2"], [10, "SO-1"]], "user_info": {"kept": True}}
		self.query = Mock(side_effect=lambda: copy.deepcopy(self.result))
		desk = types.ModuleType("frappe.desk")
		desk.reportview = types.SimpleNamespace(get=self.query)
		self.adapters = []
		carriers = types.ModuleType("erpnext.stock.doctype.shipment.carriers")
		carriers.iter_carriers = lambda: iter(self.adapters)
		self.manual = Mock(return_value={})
		summary = types.ModuleType("erpnext.stock.doctype.shipment.shipment_summary")
		summary.get_sales_order_manual_freight_map = self.manual
		modules = [self.frappe, utils, desk, carriers, summary]
		self.modules = patch.dict(sys.modules, {module.__name__: module for module in modules})
		self.modules.start()
		self.addCleanup(self.modules.stop)
		path = Path(__file__).with_name("shipment_list_api.py")
		spec = importlib.util.spec_from_file_location("shipment_list_api_under_test", path)
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def test_manual_freight_works_without_any_carrier_app_and_keeps_currency_totals_separate(self):
		self.manual.return_value = {"SO-1": {"total": 3, "recorded": 2, "settled": 1, "amounts": {"USD": 0, "CNY": 12}}}
		result = self.module.get_sales_orders()
		self.assertEqual(result["keys"], ["grand_total", "name", "shipment_freight"])
		self.assertEqual([row[:2] for row in result["values"]], self.result["values"])
		self.assertEqual(result["user_info"], {"kept": True})
		self.manual.assert_called_once_with(["SO-2", "SO-1"])
		self.assertEqual(result["values"][0][-1]["text"], "—")
		freight = result["values"][1][-1]
		self.assertEqual(freight["text"], "手工运费待登记 1/3")
		self.assertIn("12.00 CNY / 0.00 USD", freight["extra"])
		self.assertIn("不代表会计已记账", freight["title"])
		self.assertNotIn("账单", freight["text"])

	def test_adapter_summaries_merge_as_presentation_without_interpreting_their_states_or_adding_amounts(self):
		self.manual.return_value = {"SO-1": {"total": 1, "recorded": 1, "settled": 0, "amounts": {"CNY": 12}}}
		get_summary = Mock(return_value={"SO-1": {
			"text": "Carrier bill ready", "color": "green", "extra": "25.00 USD", "title": "Bill 25.00 USD",
		}})
		self.adapters = [types.SimpleNamespace(), types.SimpleNamespace(get_sales_order_freight_summary=get_summary)]
		result = self.module.get_sales_orders()["values"][1][-1]
		get_summary.assert_called_once_with(["SO-2", "SO-1"])
		self.assertIn("手工运费已登记", result["text"])
		self.assertIn("Carrier bill ready", result["text"])
		self.assertIn("12.00 CNY", result["extra"])
		self.assertIn("25.00 USD", result["extra"])
		self.assertNotIn("37.00", result["extra"])
		self.assertEqual(result["color"], "blue")

	def test_native_order_permission_failure_is_not_swallowed(self):
		self.query.side_effect = PermissionError("Sales Order")
		with self.assertRaises(PermissionError):
			self.module.get_sales_orders()
		self.manual.assert_not_called()

	def test_shipping_permission_failure_retains_orders_without_exposing_partial_freight(self):
		for provider_failure in [False, True]:
			with self.subTest(provider_failure=provider_failure):
				self.manual.side_effect = None if provider_failure else PermissionError("Shipment")
				self.manual.return_value = {"SO-1": {"total": 1, "recorded": 1, "amounts": {"USD": 9}}}
				self.adapters = [types.SimpleNamespace(get_sales_order_freight_summary=Mock(side_effect=PermissionError("Carrier freight")))]
				result = self.module.get_sales_orders()
				self.assertEqual([row[:2] for row in result["values"]], self.result["values"])
				self.assertTrue(all(row[-1]["text"] == "无权查看运费" for row in result["values"]))
				self.assertTrue(all(not row[-1]["extra"] for row in result["values"]))

	def test_unexpected_carrier_errors_are_not_hidden_as_missing_freight(self):
		self.adapters = [types.SimpleNamespace(get_sales_order_freight_summary=Mock(side_effect=RuntimeError("broken adapter")))]
		with self.assertRaisesRegex(RuntimeError, "broken adapter"):
			self.module.get_sales_orders()

	def test_empty_or_non_row_response_is_preserved_without_summary_queries(self):
		for response in [{"keys": ["name"], "values": []}, {"keys": ["count"], "values": [[2]]}, {"count": 2}]:
			self.result = response
			self.assertEqual(self.module.get_sales_orders(), response)
		self.manual.assert_not_called()

	def test_other_doctype_is_rejected_before_native_query(self):
		self.frappe.form_dict["doctype"] = "User"
		with self.assertRaises(ValueError):
			self.module.get_sales_orders()
		self.query.assert_not_called()


if __name__ == "__main__":
	unittest.main()
