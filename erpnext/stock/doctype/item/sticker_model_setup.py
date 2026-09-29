"""Explicit, transactional consolidation of the approved sticker model catalog."""
import frappe

from erpnext.stock.doctype.item.sticker_models import (
    ALLOWED_MODELS, MODEL_ATTRIBUTE, TEMPLATE, normalize_model,
)


def synchronize_models(confirmed_items=None, dry_run=True):
    """Keep stock identities intact; only consolidate attribute/display metadata.

    Ambiguous legacy layouts require an explicit item-to-model mapping. The
    caller owns commit and must retain the returned before snapshot for audit.
    """
    confirmed_items = confirmed_items or {}
    frappe.get_doc("Item", TEMPLATE, for_update=not dry_run)
    definition = frappe.get_doc("Item Attribute", MODEL_ATTRIBUTE, for_update=not dry_run)
    names = frappe.db.sql("""SELECT DISTINCT parent FROM `tabItem Variant Attribute`
        WHERE attribute=%s AND COALESCE(attribute_value,'')<>'' AND parenttype='Item'
        ORDER BY parent""", (MODEL_ATTRIBUTE,), pluck=True)
    if set(confirmed_items) - set(names):
        frappe.throw("人工确认列表包含不存在的贴纸物料，已停止同步。")
    before, changes, conflicts, identities = [], [], [], {}
    for name in names:
        doc = frappe.get_doc("Item", name, for_update=not dry_run)
        if doc.variant_of != TEMPLATE:
            frappe.throw(f"物料 {name} 在其他模板中使用贴纸型号，请先核对，未执行同步。")
        attrs = {r.attribute: r.attribute_value for r in doc.attributes}
        if len(doc.attributes) != 3 or set(attrs) != {"客户", MODEL_ATTRIBUTE, "贴纸版本"} or not all(attrs.values()):
            frappe.throw(f"贴纸 {name} 的客户、型号、版本属性不完整，已停止同步。")
        old = attrs[MODEL_ATTRIBUTE]
        if name in confirmed_items:
            model = confirmed_items[name]
            if model not in ALLOWED_MODELS:
                frappe.throw("人工确认的型号不在三种标准型号中。")
        else:
            try:
                model = normalize_model(old)
            except ValueError:
                conflicts.append({"item_code": name, "old_model": old})
                continue
        identity = tuple(str(v).strip().casefold() for v in (attrs["客户"], model, attrs["贴纸版本"]))
        if identity in identities:
            frappe.throw(f"归并后出现重复贴纸：{identities[identity]} 与 {name}。未合并或删除物料，请先核对版本。")
        identities[identity] = name
        row = next(r for r in doc.attributes if r.attribute == MODEL_ATTRIBUTE)
        display = f'{TEMPLATE}-{attrs["客户"]}-{model}-{attrs["贴纸版本"]}'
        if not doc.item_name.startswith(TEMPLATE + "-"):
            display = doc.item_name
        if len(display) > 140:
            frappe.throw(f"贴纸 {name} 同步后的显示名称过长。")
        before.append({"name": name, "item_name": doc.item_name, "modified": doc.modified,
                       "modified_by": doc.modified_by, "image": doc.image,
                       "attributes": [r.as_dict() for r in doc.attributes]})
        if old != model or doc.item_name != display:
            changes.append({"item_code": name, "attribute_row": row.name, "old_model": old,
                            "model": model, "item_name": display})
    result = {"allowed_models": list(ALLOWED_MODELS), "changes": changes,
              "needs_confirmation": conflicts, "before": {"attribute": definition.as_dict(), "items": before}}
    if dry_run:
        return result
    if conflicts:
        frappe.throw("以下旧贴纸型号无法唯一确定，未执行同步：" + "、".join(r["item_code"] for r in conflicts))
    for row in changes:
        frappe.db.set_value("Item Variant Attribute", row["attribute_row"], "attribute_value", row["model"], update_modified=False)
        frappe.db.set_value("Item", row["item_code"], "item_name", row["item_name"])
        frappe.clear_document_cache("Item", row["item_code"])
    definition.set("item_attribute_values", [{"attribute_value": model, "abbr": model} for model in ALLOWED_MODELS])
    definition.disabled = 0
    definition.numeric_values = 0
    definition.save(ignore_permissions=True)
    frappe.flags.attribute_values = None
    frappe.clear_document_cache("Item Attribute", MODEL_ATTRIBUTE)
    result["status"] = "synchronized"
    return result
