"""Carrier extension boundary for the native Shipment workflow.

ERPNext owns documents, permissions, local lifecycle and generic presentation.
Carrier apps register module paths in ``shipment_carrier_adapters`` and own
only their external booking, labels, tracking and freight integration. No app
may replace the native Shipment controller or another carrier's handlers.
"""

from importlib import import_module

import frappe
from frappe import _


def iter_carriers():
	"""Only installed apps contribute adapters through normal Frappe hooks."""
	for module_path in frappe.get_hooks("shipment_carrier_adapters") or []:
		yield import_module(module_path)


def get_carrier(doc):
	"""Resolve one provider; conflicting registrations are configuration errors."""
	matched = [adapter for adapter in iter_carriers() if adapter.matches(doc)]
	if len(matched) > 1:
		frappe.throw(_("Multiple carrier integrations match this Shipment. Check the carrier registrations."))
	return matched[0] if matched else None


def has_carrier_booking(doc):
	"""History survives changing a selector, so consult every installed adapter.

	Adapters must only inspect local booking/history records here; validation
	must never book or cancel an external shipment as a side effect.
	"""
	return any(adapter.has_booking(doc) for adapter in iter_carriers())
