"""Customer stickers require real stock; valuation follows native ledger policy."""
from collections import defaultdict
from html import escape

import frappe
from frappe.utils import cint, flt, nowdate, nowtime

TEMPLATE = "巧克粉贴纸"


def is_customer_sticker(item):
    if item.get("variant_of") == TEMPLATE:
        return True
    # Match the existing, strict legacy customer/model/version identity.
    code = item.get("name") or item.get("item_code") or ""
    if item.get("variant_of") or item.get("attributes") or not code.startswith(TEMPLATE + "-"):
        return False
    parts = code[len(TEMPLATE) + 1:].rsplit("-", 2)
    if len(parts) != 3 or not all(p and p == p.strip() for p in parts):
        return False
    owners = frappe.get_all("Customer", filters={"customer_name": parts[0]}, fields=["customer_name"], limit_page_length=2)
    return len(owners) == 1 and owners[0].customer_name == parts[0]


def sticker_customer(item):
    """Resolve the exact sticker owner and enforce access to that customer."""
    if item.get("variant_of") == TEMPLATE:
        names = [r.attribute_value for r in item.get("attributes") or [] if r.attribute == "客户"]
        owner = names[0] if len(names) == 1 else None
    else:
        parts = item.name[len(TEMPLATE) + 1:].rsplit("-", 2)
        owner = parts[0] if len(parts) == 3 else None
    matches = frappe.get_all("Customer", filters={"customer_name": owner}, pluck="name", limit_page_length=2) if owner else []
    if len(matches) != 1:
        frappe.throw("无法唯一确认贴纸所属客户，请核对贴纸档案。")
    if not frappe.db.exists("Customer", matches[0]):
        frappe.throw("来源记录不存在或已删除，请重新核对。")
    frappe.get_doc("Customer", matches[0]).check_permission("read")
    return matches[0]


def validate_sticker_item(doc, method=None):
    if (doc.get("name") or doc.get("item_code")) != TEMPLATE and not is_customer_sticker(doc):
        return
    if not cint(doc.get("is_stock_item")):
        frappe.throw("客户贴纸必须启用‘维护库存’，请通过原生物料设置处理；建立档案不代表已采购入库。")


def _balance(code, warehouse, posting_date, posting_time):
    from erpnext.stock.stock_ledger import get_previous_sle
    from erpnext.stock.utils import get_combine_datetime

    # Lock the same Bin updated by native inventory posting. Keep it until the
    # owning transaction completes so competing submits cannot spend the same stock.
    bins = frappe.db.sql("""select actual_qty, valuation_rate from `tabBin`
        where item_code=%s and warehouse=%s for update""", (code, warehouse), as_dict=True)
    if not bins:
        return 0.0, 0.0
    args = {"item_code": code, "warehouse": warehouse, "posting_date": posting_date, "posting_time": posting_time}
    previous = get_previous_sle(args, for_update=True)
    available = min(flt(bins[0].actual_qty), flt(previous.get("qty_after_transaction")))
    future = frappe.db.sql("""select qty_after_transaction, voucher_type from `tabStock Ledger Entry`
        where item_code=%s and warehouse=%s and is_cancelled=0 and posting_datetime>%s
        order by posting_datetime, creation for update""",
        (code, warehouse, get_combine_datetime(posting_date, posting_time)), as_dict=True)
    for row in future:
        # A stock count resets the quantity rather than carrying forward our delta.
        if row.voucher_type == "Stock Reconciliation":
            break
        available = min(available, flt(row.qty_after_transaction))
    return max(0.0, available), flt(previous.get("valuation_rate"))


def delivery_sticker_stock(doc):
    """Aggregate actual stock units, including native product-bundle components."""
    if cint(doc.get("docstatus")) == 2 or cint(doc.get("is_return")):
        return []
    grouped = defaultdict(float)
    items = {}
    for field in ("items", "packed_items"):
        for row in doc.get(field) or []:
            code = row.get("item_code")
            if not code or flt(row.get("qty")) <= 0:
                continue
            if code not in items:
                items[code] = frappe.get_doc("Item", code)
            item = items[code]
            if not is_customer_sticker(item):
                continue
            validate_sticker_item(item)
            warehouse = row.get("warehouse")
            if not warehouse:
                frappe.throw("贴纸 " + escape(code) + " 未指定出库仓库，请先选择仓库。")
            # Packed Item.qty is already expressed in the component stock UOM.
            factor = 1.0 if field == "packed_items" else flt(row.get("conversion_factor"))
            if factor <= 0:
                frappe.throw("贴纸 " + escape(code) + " 缺少有效库存单位换算，不能核对出库数量。")
            grouped[(code, warehouse)] += flt(row.get("qty")) * factor
    result = []
    for (code, warehouse), qty in sorted(grouped.items()):
        available, rate = _balance(code, warehouse, doc.get("posting_date") or nowdate(), doc.get("posting_time") or nowtime())
        result.append({"item_code": code, "item_name": items[code].item_name, "warehouse": warehouse,
            "stock_uom": items[code].stock_uom, "required_qty": qty, "available_qty": available,
            "shortage_qty": max(0.0, qty - available), "has_valuation": rate > 0})
    return result


def validate_delivery_sticker_stock(doc, method=None):
    # Changes to an already submitted document must not recheck its spent stock.
    if cint(doc.get("docstatus")) == 1 and getattr(doc, "_action", None) != "submit":
        return []
    rows = delivery_sticker_stock(doc)
    problems = []
    for row in rows:
        code, warehouse = escape(row["item_code"]), escape(row["warehouse"])
        if row["shortage_qty"] > 0.000001:
            unit = escape(row["stock_uom"] or "库存单位")
            problems.append(f"贴纸 {code}（{warehouse}）：本次需要 {row['required_qty']:g} {unit}，可用 {row['available_qty']:g}，缺少 {row['shortage_qty']:g}。请先完成实际采购到货入库，或明确减少本次出库数量。")
        # Posted zero-valued stock is valid (e.g. service cost expensed separately).
        # Keep the quantity guard; native receipts and ledgers own valuation checks.
    if problems:
        frappe.throw("<br>".join(problems), title="贴纸暂不能出库")
    return rows
