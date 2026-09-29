"""Offline coverage for optional legacy shipping projections without carrier apps."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


class DeliveryNoteUpdateTests(unittest.TestCase):
	def setUp(self):
		self.fields = set()
		self.documents = {name: types.SimpleNamespace(db_set=Mock()) for name in ("DN-1", "DN-2")}
		self.frappe = types.ModuleType("frappe")
		self.frappe.get_meta = Mock(return_value=types.SimpleNamespace(has_field=self.fields.__contains__))
		self.frappe.get_doc = Mock(side_effect=lambda doctype, name: self.documents[name])
		modules = patch.dict(sys.modules, {"frappe": self.frappe})
		modules.start()
		self.addCleanup(modules.stop)
		path = Path(__file__).with_name("delivery_note_update.py")
		spec = importlib.util.spec_from_file_location("delivery_note_update_under_test", path)
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def test_no_plugin_fields_is_noop_without_loading_or_writing_delivery_notes(self):
		self.module.update_delivery_note(
			["DN-1"], shipment_info={"carrier": "Carrier"}, tracking_info={"awb_number": "00123"}
		)
		self.frappe.get_meta.assert_called_once_with("Delivery Note")
		self.frappe.get_doc.assert_not_called()
		self.documents["DN-1"].db_set.assert_not_called()

	def test_complete_projection_uses_one_write_per_note_and_preserves_leading_zero_waybill(self):
		expected = {
			"delivery_type": "Parcel Service",
			"parcel_service": "Carrier",
			"parcel_service_type": "Express",
			"tracking_number": "00123",
			"tracking_url": "https://carrier.example/00123",
			"tracking_status": "Delivered",
			"tracking_status_info": "Signed",
		}
		self.fields.update(expected)
		self.module.update_delivery_note(
			["DN-1", "DN-2", "DN-1"],
			shipment_info={"carrier": "Carrier", "carrier_service": "Express"},
			tracking_info={
				"awb_number": "00123", "tracking_url": "https://carrier.example/00123",
				"tracking_status": "Delivered", "tracking_status_info": "Signed",
			},
		)
		self.assertEqual(self.frappe.get_doc.call_args_list, [call("Delivery Note", "DN-1"), call("Delivery Note", "DN-2")])
		for doc in self.documents.values():
			doc.db_set.assert_called_once_with(expected)

	def test_partial_metadata_and_tracking_only_preserve_booking_and_allow_clearing_tracking_url(self):
		self.fields.update({"parcel_service", "tracking_number", "tracking_url"})
		self.module.update_delivery_note(["DN-1"], tracking_info={"awb_number": "00456"})
		self.documents["DN-1"].db_set.assert_called_once_with({"tracking_number": "00456", "tracking_url": None})
		self.module.update_delivery_note(["DN-2"])
		self.documents["DN-2"].db_set.assert_not_called()


if __name__ == "__main__":
	unittest.main()
