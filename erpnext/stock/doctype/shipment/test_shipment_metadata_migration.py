"""Offline regression coverage for the bounded Shipment metadata transfer."""

import copy
import importlib.util
import json
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[4]
SHIPMENT = ROOT / "erpnext/stock/doctype/shipment/shipment.json"
PATCH = ROOT / "erpnext/patches/v16_0/move_shipment_carrier_metadata.py"


class Field(dict):
	def __getattr__(self, name):
		return self.get(name)


class MetadataStore:
	def __init__(self):
		self.rows = {"Custom Field": [], "Property Setter": [], "Custom DocPerm": [], "DocType Layout": [], "DocType Layout Field": []}
		self.rows.update({"List View Settings": [], "Client Script": []})
		self.deleted = []
		self.updated = []
		self.sql = sqlite3.connect(":memory:")
		self.sql.execute("CREATE TABLE Shipment (name TEXT, manual_waybill TEXT, manual_waybill_key TEXT)")
		self.sql.execute("CREATE UNIQUE INDEX manual_carrier_waybill_unique ON Shipment (manual_waybill_key)")
		self.sql.execute("INSERT INTO Shipment VALUES ('SHIP-1', '00123456789', 'existing-key')")

	def get_all(self, doctype, filters, fields):
		def matches(row):
			return all(
				row.get(key) in value[1] if isinstance(value, list) and value[0] == "in" else row.get(key) == value
				for key, value in filters.items()
			)
		return [copy.deepcopy(row) for row in self.rows[doctype] if matches(row)]

	def delete(self, doctype, filters):
		self.deleted.append((doctype, filters))
		self.rows[doctype] = [row for row in self.rows[doctype] if not all(row.get(key) == value for key, value in filters.items())]

	def get_value(self, doctype, name, fieldname):
		return next((row.get(fieldname) for row in self.rows[doctype] if row["name"] == name), None)

	def set_value(self, doctype, name, fieldname, value, update_modified=True):
		self.updated.append((doctype, name, fieldname, value, update_modified))
		next(row for row in self.rows[doctype] if row["name"] == name)[fieldname] = value

	def add_unique(self, doctype, fields, constraint_name):
		assert (doctype, fields, constraint_name) == ("Shipment", ["manual_waybill_key"], "manual_carrier_waybill_unique")
		self.sql.execute("CREATE UNIQUE INDEX IF NOT EXISTS manual_carrier_waybill_unique ON Shipment (manual_waybill_key)")


def load_migration():
	store = MetadataStore()
	frappe = types.ModuleType("frappe")
	frappe.db = store
	frappe.get_all = store.get_all
	frappe.clear_cache = Mock()
	frappe.reload_doc = Mock()
	frappe.get_app_path = lambda app, *parts: str(ROOT / app / Path(*parts))
	def throw(message):
		raise ValueError(message)
	frappe.throw = throw
	native = json.loads(SHIPMENT.read_text())
	manual = [field for field in native["fields"] if field["fieldname"].startswith("manual_")]
	property_names = set().union(*(field.keys() for field in manual)) | {"hidden", "unique", "precision", "in_list_view", "collapsible"}
	checks = {"allow_on_submit", "read_only", "no_copy", "hidden", "unique", "in_list_view", "collapsible"}
	properties = [Field(fieldname=name, fieldtype="Check" if name in checks else "Data", default="0" if name in checks else None) for name in property_names]
	permission_properties = [Field(fieldname=name, fieldtype="Int" if name == "permlevel" else "Check") for name in (
		"read", "write", "create", "submit", "share", "print", "email", "cancel", "amend",
		"report", "delete", "import", "export", "select", "if_owner", "permlevel", "mask",
	)]
	frappe.get_meta = lambda doctype: Field(fields=permission_properties if doctype == "Custom DocPerm" else properties)
	spec = importlib.util.spec_from_file_location("shipment_metadata_migration_under_test", PATCH)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(sys.modules, {"frappe": frappe}):
		spec.loader.exec_module(module)
	return module, frappe, store, manual


def legacy_fields(manual, doctype="Shipment", previous="service_provider"):
	rows = []
	for field in manual:
		rows.append({**field, "name": doctype + "-" + field["fieldname"], "dt": doctype, "is_system_generated": 1, "insert_after": previous})
		previous = field["fieldname"]
	return rows


def setter(name, fieldname, property_name, value, generated=1, doctype="Shipment"):
	return {"name": name, "doc_type": doctype, "field_name": fieldname, "property": property_name, "value": value, "is_system_generated": generated}


class TestShipmentMetadataMigration(unittest.TestCase):
	EMPTY_RESULT = {
		"removed_custom_fields": [], "removed_property_setters": [], "removed_custom_permissions": [],
		"renamed_sales_order_freight_column": False, "disabled_client_scripts": [],
	}

	def test_upgrade_preserves_values_indexes_and_customer_overrides_and_is_idempotent(self):
		module, frappe, store, manual = load_migration()
		store.rows["Custom Field"] = legacy_fields(manual) + [{"name": "Shipment-customer_note", "dt": "Shipment", "fieldname": "customer_note"}]
		store.rows["Property Setter"] = [
			setter("old-layout", "column_break_28", "fieldtype", "Section Break"),
			setter("old-status", "status", "options", module.LEGACY_STATUS_OPTIONS),
			setter("customer-layout", "heading_pickup_from", "label", "Our pickup details"),
			setter("user-field", "manual_note", "read_only", "1", generated=0),
			setter("user-parcel", "shipment_parcel", "allow_on_submit", "1", generated=0),
			setter("sf-field", "sf_actions_html", "label", " "),
		]
		before = store.sql.execute("SELECT * FROM Shipment").fetchall()
		result = module.execute()
		self.assertEqual(len(result["removed_custom_fields"]), 19)
		self.assertEqual(set(result["removed_property_setters"]), {"old-layout", "old-status"})
		self.assertEqual([row["name"] for row in store.rows["Custom Field"]], ["Shipment-customer_note"])
		self.assertEqual({row["name"] for row in store.rows["Property Setter"]}, {"customer-layout", "user-field", "user-parcel", "sf-field"})
		self.assertEqual(store.sql.execute("SELECT * FROM Shipment").fetchall(), before)
		self.assertEqual(module.execute(), self.EMPTY_RESULT)
		self.assertEqual(len(store.sql.execute("PRAGMA index_list(Shipment)").fetchall()), 1)
		with self.assertRaises(sqlite3.IntegrityError):
			store.sql.execute("INSERT INTO Shipment VALUES ('SHIP-2', '00123456789', 'existing-key')")
		self.assertEqual([call.args[2] for call in frappe.reload_doc.call_args_list[:2]], ["shipment_parcel", "shipment"])

	def test_fresh_erp_only_site_gets_native_manual_provider_and_layout(self):
		module, frappe, store, manual = load_migration()
		self.assertEqual(module.execute(), self.EMPTY_RESULT)
		fields = {field["fieldname"]: field for field in json.loads(SHIPMENT.read_text())["fields"]}
		self.assertEqual(len(manual), 19)
		self.assertEqual(fields["service_provider"]["fieldtype"], "Select")
		self.assertIn("其他物流（手工登记）", fields["service_provider"]["options"])
		self.assertNotIn("顺丰国际", fields["service_provider"]["options"])
		self.assertEqual(fields["column_break_28"]["fieldtype"], "Section Break")
		self.assertEqual(fields["heading_pickup_from"]["collapsible"], 1)
		self.assertFalse(store.deleted)
		permissions = json.loads(SHIPMENT.read_text())["permissions"]
		self.assertEqual(next(row for row in permissions if row["role"] == "Sales User")["submit"], 1)

	def test_customized_custom_field_stops_before_any_mutation(self):
		for changes in ({"label": "Customer label"}, {"is_system_generated": 0}, {"insert_after": "status"}, {"unique": 1}):
			with self.subTest(changes=changes):
				module, frappe, store, manual = load_migration()
				store.rows["Custom Field"] = legacy_fields(manual)
				store.rows["Custom Field"][2].update(changes)
				before = copy.deepcopy(store.rows)
				with self.assertRaisesRegex(ValueError, "Shipment-manual_carrier"):
					module.execute()
				self.assertEqual(store.rows, before)
				self.assertFalse(store.deleted)
				frappe.reload_doc.assert_not_called()

	def test_database_default_zeroes_do_not_create_false_conflicts(self):
		module, frappe, store, manual = load_migration()
		store.rows["Custom Field"] = legacy_fields(manual)
		for row in store.rows["Custom Field"]:
			row.setdefault("hidden", 0)
			row.setdefault("unique", None)
			row.setdefault("precision", "")
			row.setdefault("in_list_view", "0")
		self.assertEqual(len(module.execute()["removed_custom_fields"]), 19)

	def test_legacy_permission_without_child_table_columns_is_replaced_only_for_shipment(self):
		module, frappe, store, manual = load_migration()
		other = {"name": "other-sales", "parent": "Delivery Note", "role": "Sales User", **dict.fromkeys(module.SALES_USER_RIGHTS, 1)}
		store.rows["Custom DocPerm"] = [{
			"name": "legacy-sales", "parent": "Shipment", "role": "Sales User",
			**dict.fromkeys(module.SALES_USER_RIGHTS, 1),
		}, other]
		self.assertEqual(module.execute()["removed_custom_permissions"], ["legacy-sales"])
		self.assertEqual(store.rows["Custom DocPerm"], [other])
		self.assertFalse(module.execute()["removed_custom_permissions"])

	def test_customer_permission_combinations_remain_intact(self):
		for custom in ({"role": "Warehouse User", "read": 1}, {"role": "Sales User", "cancel": 1}):
			with self.subTest(custom=custom):
				module, frappe, store, manual = load_migration()
				legacy = {"name": "legacy-sales", "parent": "Shipment", "role": "Sales User", **dict.fromkeys(module.SALES_USER_RIGHTS, 1)}
				store.rows["Custom DocPerm"] = [legacy, {"name": "customer-permission", "parent": "Shipment", **custom}]
				before = copy.deepcopy(store.rows["Custom DocPerm"])
				self.assertFalse(module.execute()["removed_custom_permissions"])
				self.assertEqual(store.rows["Custom DocPerm"], before)

	def test_nonmatching_single_permissions_are_not_deleted(self):
		for changes in (
			{"role": "Warehouse User"}, {"cancel": 1}, {"mask": 1}, {"if_owner": 1},
			{"permlevel": 1}, {"read": 0}, {"export": 0}, {"parent": "Sales Order"},
		):
			with self.subTest(changes=changes):
				module, frappe, store, manual = load_migration()
				store.rows["Custom DocPerm"] = [{
					"name": "customer-sales", "parent": "Shipment", "role": "Sales User",
					**dict.fromkeys(module.SALES_USER_RIGHTS, 1), **changes,
				}]
				before = copy.deepcopy(store.rows["Custom DocPerm"])
				self.assertFalse(module.execute()["removed_custom_permissions"])
				self.assertEqual(store.rows["Custom DocPerm"], before)

	def test_legacy_delivery_note_panel_is_replaced_by_native_layout(self):
		module, frappe, store, manual = load_migration()
		store.rows["Custom Field"] = legacy_fields(module.LEGACY_DELIVERY_NOTE_FIELDS, "Delivery Note", "tracking_status_info")
		result = module.execute()
		self.assertEqual(set(result["removed_custom_fields"]), {"Delivery Note-sf_dn_actions_section", "Delivery Note-sf_dn_actions_html"})
		self.assertFalse(store.rows["Custom Field"])
		self.assertEqual(frappe.reload_doc.call_args.args[2], "delivery_note")
		fields = {field["fieldname"]: field for field in json.loads((ROOT / "erpnext/stock/doctype/delivery_note/delivery_note.json").read_text())["fields"]}
		self.assertEqual(fields["shipping_section"]["fieldtype"], "Section Break")
		self.assertEqual(fields["shipping_details"]["fieldtype"], "HTML")
		self.assertFalse(module.execute()["removed_custom_fields"])

	def test_delivery_note_customer_override_stops_before_shipment_mutations(self):
		module, frappe, store, manual = load_migration()
		store.rows["Custom Field"] = legacy_fields(manual) + legacy_fields(module.LEGACY_DELIVERY_NOTE_FIELDS, "Delivery Note", "tracking_status_info")
		store.rows["Property Setter"] = [setter("customer-panel-label", "sf_dn_actions_section", "label", "Custom Shipping", doctype="Delivery Note")]
		with self.assertRaisesRegex(ValueError, "customer-panel-label"):
			module.execute()
		self.assertFalse(store.deleted)
		frappe.reload_doc.assert_not_called()

	def test_delivery_note_customer_layout_reference_is_not_orphaned(self):
		module, frappe, store, manual = load_migration()
		store.rows["Custom Field"] = legacy_fields(module.LEGACY_DELIVERY_NOTE_FIELDS, "Delivery Note", "tracking_status_info")
		store.rows["DocType Layout"] = [{"name": "Custom Delivery Note", "document_type": "Delivery Note"}]
		store.rows["DocType Layout Field"] = [{"name": "customer-layout-row", "parent": "Custom Delivery Note", "fieldname": "sf_dn_actions_html"}]
		with self.assertRaisesRegex(ValueError, "customer-layout-row"):
			module.execute()
		self.assertFalse(store.deleted)

	def test_saved_sales_order_freight_column_keeps_label_width_order_and_other_settings(self):
		module, frappe, store, manual = load_migration()
		fields = [
			{"fieldname": "name", "label": "订单", "width": 130},
			{"fieldname": "sf_freight", "label": "我们的物流费用", "width": 287, "custom": "preserved"},
			{"fieldname": "advance_paid", "label": "收款", "width": 150},
		]
		store.rows["List View Settings"] = [
			{"name": "Sales Order", "fields": json.dumps(fields), "disable_count": 1, "modified": "unchanged"},
			{"name": "Delivery Note", "fields": '[{"fieldname":"sf_freight"}]'},
		]
		expected = copy.deepcopy(store.rows["List View Settings"])
		fields[1]["fieldname"] = "shipment_freight"
		result = module.execute()
		self.assertTrue(result["renamed_sales_order_freight_column"])
		self.assertEqual(json.loads(store.rows["List View Settings"][0]["fields"]), fields)
		expected[0]["fields"] = store.rows["List View Settings"][0]["fields"]
		self.assertEqual(store.rows["List View Settings"], expected)
		self.assertEqual(len(store.updated), 1)
		self.assertFalse(store.updated[0][-1])
		self.assertEqual(module.execute(), self.EMPTY_RESULT)
		self.assertEqual(len(store.updated), 1)

	def test_saved_lists_without_old_freight_column_remain_exactly_unchanged(self):
		for raw in (None, "", "[]", '[{"fieldname":"status_field","label":"状态"}]', '[{"fieldname":"shipment_freight"}]'):
			with self.subTest(raw=raw):
				module, frappe, store, manual = load_migration()
				store.rows["List View Settings"] = [{"name": "Sales Order", "fields": raw}]
				before = copy.deepcopy(store.rows["List View Settings"])
				self.assertFalse(module.execute()["renamed_sales_order_freight_column"])
				self.assertEqual(store.rows["List View Settings"], before)
				self.assertFalse(store.updated)

	def test_only_named_enabled_sales_order_list_scripts_are_retired(self):
		module, frappe, store, manual = load_migration()
		store.rows["Client Script"] = [
			{"name": name, "dt": "Sales Order", "view": "List", "enabled": 1, "script": "kept", "modified": "kept"}
			for name in module.LEGACY_SALES_ORDER_LIST_SCRIPTS
		] + [
			{"name": "Customer Sales Order List", "dt": "Sales Order", "view": "List", "enabled": 1},
			{"name": "Sales Order Payment", "dt": "Sales Order", "view": "Form", "enabled": 1},
		]
		expected = copy.deepcopy(store.rows["Client Script"])
		for row in expected[:2]:
			row["enabled"] = 0
		self.assertEqual(module.execute()["disabled_client_scripts"], list(module.LEGACY_SALES_ORDER_LIST_SCRIPTS))
		self.assertEqual(store.rows["Client Script"], expected)
		self.assertTrue(all(update[-1] is False for update in store.updated))
		self.assertEqual(module.execute(), self.EMPTY_RESULT)
		self.assertEqual(len(store.updated), 2)
		for change in ({"dt": "Delivery Note"}, {"view": "Form"}, {"enabled": 0}):
			with self.subTest(change=change):
				store.rows["Client Script"] = [{"name": "Sales Order Freight List", "dt": "Sales Order", "view": "List", "enabled": 1, **change}]
				self.assertFalse(module.execute()["disabled_client_scripts"])
				self.assertEqual(len(store.updated), 2)

	def test_invalid_saved_columns_stop_before_any_metadata_mutation(self):
		for raw in ("invalid JSON", '{"fieldname":"sf_freight"}', '["sf_freight"]'):
			with self.subTest(raw=raw):
				module, frappe, store, manual = load_migration()
				store.rows["Custom Field"] = legacy_fields(manual)
				store.rows["List View Settings"] = [{"name": "Sales Order", "fields": raw}]
				before = copy.deepcopy(store.rows)
				with self.assertRaises(ValueError):
					module.execute()
				self.assertEqual(store.rows, before)
				self.assertFalse(store.updated)
				self.assertFalse(store.deleted)
				frappe.reload_doc.assert_not_called()


if __name__ == "__main__":
	unittest.main()
