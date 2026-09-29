"""Explicit shipment sticker selection and picture access boundaries."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


class Row(dict):
	__getattr__ = dict.get


class Doc(Row):
	def has_permission(self, permission):
		return not self.get("denied")

	def check_permission(self, permission):
		if not self.has_permission(permission):
			raise PermissionError("denied")


class Harness:
	def __init__(self, monkeypatch):
		self.data = {"Item": {}, "File": {}}
		self.queries, self.reads = [], []
		frappe = types.ModuleType("frappe")
		frappe.DoesNotExistError = KeyError
		frappe.PermissionError = PermissionError
		frappe.get_all = self.get_all
		frappe.get_doc = self.get_doc
		utils = types.ModuleType("frappe.utils")
		utils.flt = lambda value: float(value or 0)
		monkeypatch.setitem(sys.modules, "frappe", frappe)
		monkeypatch.setitem(sys.modules, "frappe.utils", utils)
		spec = importlib.util.spec_from_file_location("shipment_stickers_under_test", Path(__file__).resolve().parents[1] / "erpnext/stock/doctype/shipment/shipment_stickers.py")
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def get_all(self, doctype, filters, pluck=None, fields=None, **kwargs):
		self.queries.append((doctype, filters))
		def matches(row):
			return all(row.get(key) in value[1] if isinstance(value, list) else row.get(key) == value for key, value in filters.items())
		rows = [row for row in self.data[doctype].values() if matches(row)]
		return [row[pluck] for row in rows] if pluck else [Row({key: row.get(key) for key in fields}) for row in rows]

	def get_doc(self, doctype, name):
		self.reads.append((doctype, name))
		return self.data[doctype][name]

	def sticker(self, code="STICKER-1", image="/private/files/sticker.png"):
		self.data["Item"][code] = Doc(name=code, item_name="客户贴纸", variant_of="巧克粉贴纸", image=image,
			attributes=[Row(attribute="贴纸型号", attribute_value="方形"), Row(attribute="贴纸版本", attribute_value="v1")])
		if image:
			self.data["File"][code] = Doc(name=code, file_url=image, is_private=int(image.startswith("/private/")), thumbnail_url="/files/public-thumbnail.png")

	def call(self, items, packed=None, denied=False):
		return self.module.stickers_for_delivery_note(Doc(items=[Row(**row) for row in items], packed_items=[Row(**row) for row in packed or []], denied=denied))


@pytest.fixture
def harness(monkeypatch):
	return Harness(monkeypatch)


def test_direct_sticker_uses_latest_item_picture_and_attributes(harness):
	harness.sticker()
	result = harness.call([dict(name="row-1", item_code="STICKER-1", qty=1)])
	assert result["row-1"] == [dict(item_code="STICKER-1", item_name="客户贴纸", model="方形", version="v1",
		image_url="/private/files/sticker.png", image_status="ready", source_kind="item")]
	assert "public-thumbnail" not in repr(result)


def test_packed_stickers_require_exact_parent_row_and_are_deduplicated(harness):
	harness.sticker()
	items = [dict(name="row-1", item_code="GOODS", qty=1), dict(name="row-2", item_code="GOODS", qty=1)]
	component = dict(parent_detail_docname="row-2", parent_item="GOODS", item_code="STICKER-1", qty=1)
	result = harness.call(items, [component, component, dict(parent_item="GOODS", item_code="STICKER-1", qty=1)])
	assert set(result) == {"row-2"}
	assert len(result["row-2"]) == 1 and result["row-2"][0]["source_kind"] == "packed_item"


def test_two_bundles_display_their_own_sticker_without_cross_pairing(harness):
	harness.sticker("GREEN", "/files/green.png")
	harness.sticker("BLUE", "/files/blue.png")
	result = harness.call(
		[dict(name="row-1", item_code="GREEN-BUNDLE", qty=120), dict(name="row-2", item_code="BLUE-BUNDLE", qty=40)],
		[dict(parent_detail_docname="row-1", parent_item="GREEN-BUNDLE", item_code="GREEN", qty=120),
		 dict(parent_detail_docname="row-2", parent_item="BLUE-BUNDLE", item_code="BLUE", qty=40)],
	)
	assert result["row-1"][0]["image_url"] == "/files/green.png"
	assert result["row-2"][0]["image_url"] == "/files/blue.png"


def test_normal_goods_do_not_infer_customer_stickers_or_read_files(harness):
	harness.sticker()
	assert harness.call([dict(name="row-1", item_code="GOODS", qty=1)]) == {}
	assert len(harness.queries) == 1 and harness.queries[0][0] == "Item"
	assert harness.reads == []


@pytest.mark.parametrize("denied_type", ["Item", "File"])
def test_unreadable_item_or_private_file_never_exposes_picture(harness, denied_type):
	harness.sticker()
	harness.data[denied_type]["STICKER-1"]["denied"] = True
	result = harness.call([dict(name="row-1", item_code="STICKER-1", qty=1)])["row-1"][0]
	assert result["image_status"] == "restricted" and result["image_url"] is None
	if denied_type == "Item":
		assert result["item_name"] == "" and result["model"] == "" and result["version"] == ""
		assert not any(dt == "File" for dt, _ in harness.queries)


def test_shared_url_can_resolve_an_accessible_attachment_copy(harness):
	harness.sticker()
	harness.data["File"]["STICKER-1"]["denied"] = True
	harness.data["File"]["copy"] = Doc(name="copy", file_url="/private/files/sticker.png", is_private=1)
	result = harness.call([dict(name="row-1", item_code="STICKER-1", qty=1)])["row-1"][0]
	assert result["image_status"] == "ready"


@pytest.mark.parametrize("image,status", [(None, "missing"), ("https://example.com/picture.png", "unavailable"), ("/private/files/../secret.png", "unavailable")])
def test_missing_or_unsupported_images_keep_sticker_identity(harness, image, status):
	harness.sticker(image=image)
	result = harness.call([dict(name="row-1", item_code="STICKER-1", qty=1)])["row-1"][0]
	assert result["image_status"] == status and result["image_url"] is None
	assert result["model"] == "方形"
	assert not any(dt == "File" for dt, _ in harness.queries)


def test_missing_file_is_unavailable_without_failing_goods(harness):
	harness.sticker()
	harness.data["File"].clear()
	result = harness.call([dict(name="row-1", item_code="STICKER-1", qty=1)])["row-1"][0]
	assert result["image_status"] == "unavailable"


def test_delivery_note_permission_is_required_before_classification(harness):
	with pytest.raises(PermissionError):
		harness.call([dict(name="row-1", item_code="STICKER-1", qty=1)], denied=True)
	assert harness.queries == []


def test_zero_quantities_and_inconsistent_links_are_ignored_but_returns_work(harness):
	harness.sticker()
	assert harness.call([dict(name="row-1", item_code="STICKER-1", qty=0)]) == {}
	items = [dict(name="row-1", item_code="GOODS", qty=1)]
	assert harness.call(items, [dict(parent_detail_docname="row-1", parent_item="OTHER", item_code="STICKER-1", qty=1)]) == {}
	assert harness.call(items, [dict(parent_detail_docname="row-1", parent_item="GOODS", item_code="STICKER-1", qty=0)]) == {}
	assert harness.call([dict(name="row-1", item_code="STICKER-1", qty=-1)])["row-1"][0]["image_status"] == "ready"
