"""Read-only validation of posted delivery accounting before booking."""

import frappe
from frappe.utils import strip_html


def validate_delivery_accounting(name, company):
    """Block booking for a submitted stock issue that has no required GL entries."""
    from erpnext import is_perpetual_inventory_enabled

    if not is_perpetual_inventory_enabled(company):
        return
    valued_movements = frappe.db.exists("Stock Ledger Entry", {
        "voucher_type": "Delivery Note", "voucher_no": name,
        "is_cancelled": 0, "stock_value_difference": ["!=", 0],
    })
    if valued_movements and not frappe.db.exists("GL Entry", {
        "voucher_type": "Delivery Note", "voucher_no": name, "is_cancelled": 0,
    }):
        # Internal transfers/offsetting accounts can legitimately net to no GL.
        # Only calculate the native entries here; never post or rebuild them.
        try:
            expected = frappe.get_doc("Delivery Note", name).get_gl_entries()
        except frappe.ValidationError as exc:
            frappe.throw(f"出库单 {name} 记账校验未完成，不能创建面单。原因：{strip_html(str(exc))}")
        if not expected:
            return
        frappe.throw(f"出库单 {name} 已有库存流水，但缺少对应会计分录，不能创建面单。请先核对物料成本并修复出库记账。")
