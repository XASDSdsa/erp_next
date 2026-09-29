"""The three approved sticker layouts, independent of customer artwork/version."""
import re
import unicodedata

ALLOWED_MODELS = ("山东中性方形", "广东油性方形", "圆形")
MODEL_ATTRIBUTE = "贴纸型号"
TEMPLATE = "巧克粉贴纸"


def normalize_model(value):
    """Accept unambiguous layout aliases; the agent resolves natural-language context."""
    if not isinstance(value, str):
        raise ValueError("贴纸型号只能选择：山东中性方形、广东油性方形、圆形。")
    key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()
    previous = None
    while previous != key:
        previous = key
        for suffix in ("模板", "模版", "贴纸"):
            if key.endswith(suffix):
                key = key[:-len(suffix)]
    aliases = {
        "山东中性方形": ALLOWED_MODELS[0], "山东中性": ALLOWED_MODELS[0],
        "山东中性方": ALLOWED_MODELS[0], "山东方形": ALLOWED_MODELS[0],
        "山东方": ALLOWED_MODELS[0], "中性方形": ALLOWED_MODELS[0], "中性方": ALLOWED_MODELS[0],
        "广东油性方形": ALLOWED_MODELS[1], "广东油性": ALLOWED_MODELS[1],
        "广东油性方": ALLOWED_MODELS[1], "广东方形": ALLOWED_MODELS[1],
        "广东方": ALLOWED_MODELS[1], "油性方形": ALLOWED_MODELS[1], "油性方": ALLOWED_MODELS[1],
        "圆形": ALLOWED_MODELS[2], "圆": ALLOWED_MODELS[2], "圆贴": ALLOWED_MODELS[2],
        "02号圆形": ALLOWED_MODELS[2], "round": ALLOWED_MODELS[2],
    }
    if key not in aliases:
        raise ValueError("贴纸型号无法唯一匹配。只能选择：山东中性方形、广东油性方形、圆形；只说‘方形’时请明确山东中性或广东油性。")
    return aliases[key]


def validate_model_attribute(doc, method=None):
    import frappe

    if (doc.get("name") or doc.get("attribute_name")) != MODEL_ATTRIBUTE:
        return
    values = doc.get("item_attribute_values") or []
    if (doc.get("numeric_values") or doc.get("disabled") or len(values) != 3
            or {row.get("attribute_value") for row in values} != set(ALLOWED_MODELS)
            or any(row.get("abbr") != row.get("attribute_value") for row in values)):
        frappe.throw("贴纸型号固定为山东中性方形、广东油性方形、圆形，不能新增、删除、改名或停用；缩写须与型号相同。")


def validate_item_model(doc, method=None):
    import frappe

    rows = [row for row in doc.get("attributes") or [] if row.get("attribute") == MODEL_ATTRIBUTE]
    if doc.get("has_variants"):
        return
    if doc.get("variant_of") == TEMPLATE and len(rows) != 1:
        frappe.throw("客户贴纸必须选择一种贴纸型号：山东中性方形、广东油性方形、圆形。")
    for row in rows:
        if row.get("attribute_value") not in ALLOWED_MODELS:
            frappe.throw("贴纸型号只能从山东中性方形、广东油性方形、圆形中选择，不能使用其他名称。")


def protect_model_attribute(doc, method=None, *args, **kwargs):
    import frappe

    if (doc.get("name") or doc.get("attribute_name")) == MODEL_ATTRIBUTE:
        frappe.throw("贴纸型号是固定业务配置，不能删除或重命名。")
