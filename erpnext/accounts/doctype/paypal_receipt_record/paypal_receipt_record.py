"""Internal reservation shared by PayPal receipt entry points."""

import frappe
from frappe.model.document import Document


class PayPalReceiptRecord(Document):
	def _require_internal_write(self):
		if not self.flags.get("paypal_receipt_internal"):
			frappe.throw("PayPal 收款记录只能通过专用收款操作维护。", frappe.PermissionError)

	def validate(self):
		self._require_internal_write()

	def on_trash(self):
		self._require_internal_write()

	def before_rename(self, old, new, merge=False):
		frappe.throw("PayPal 收款记录使用固定交易标识，不可重命名。", frappe.PermissionError)
