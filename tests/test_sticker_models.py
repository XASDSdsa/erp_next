import sys
from types import SimpleNamespace

import pytest

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("native_sticker_models", Path(__file__).parents[1] / "erpnext/stock/doctype/item/sticker_models.py")
models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(models)


@pytest.mark.parametrize("source,expected", [
    ("山东中性模版", "山东中性方形"), ("山东中性方形模板", "山东中性方形"),
    ("广东油性方形模版", "广东油性方形"), ("02 号圆形", "圆形"),
    ("油性方贴纸", "广东油性方形"),
])
def test_known_aliases(source, expected):
    assert models.normalize_model(source) == expected


@pytest.mark.parametrize("source", ["方形", "中心方形", "山东广东", "三角形", "山东圆形", None])
def test_ambiguous_or_unknown_model_cannot_guess(source):
    with pytest.raises(ValueError):
        models.normalize_model(source)


@pytest.fixture
def frappe_stub(monkeypatch):
    def reject(message):
        raise ValueError(message)
    monkeypatch.setitem(sys.modules, "frappe", SimpleNamespace(throw=reject))


def test_manual_attribute_edit_cannot_add_or_remove_layout(frappe_stub):
    rows = [{"attribute_value": m, "abbr": m} for m in models.ALLOWED_MODELS]
    doc = {"name": "贴纸型号", "item_attribute_values": rows}
    models.validate_model_attribute(doc)
    for bad in (rows[:2], rows + [{"attribute_value": "方形", "abbr": "方形"}],
                [{"attribute_value": r["attribute_value"], "abbr": "自定义"} for r in rows]):
        with pytest.raises(ValueError):
            models.validate_model_attribute({**doc, "item_attribute_values": bad})
    models.validate_model_attribute({"name": "贴纸版本", "item_attribute_values": []})


def test_manual_item_edit_cannot_bypass_model_selection(frappe_stub):
    for value in models.ALLOWED_MODELS:
        models.validate_item_model({"variant_of": "巧克粉贴纸", "attributes": [
            {"attribute": "贴纸型号", "attribute_value": value}]})
    for rows in ([], [{"attribute": "贴纸型号", "attribute_value": "随意型号"}]):
        with pytest.raises(ValueError):
            models.validate_item_model({"variant_of": "巧克粉贴纸", "attributes": rows})
    models.validate_item_model({"name": "其他物料", "attributes": []})
    models.validate_item_model({"name": "巧克粉贴纸", "has_variants": 1, "attributes": [
        {"attribute": "贴纸型号", "attribute_value": None}]})
