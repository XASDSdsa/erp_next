"""Carrier-neutral helpers for Shipment summaries and linked documents."""

from collections.abc import Iterable


def select_linked_shipment_row(rows: Iterable, *, include_cancelled: bool = False):
	"""Select the current Shipment row for a linked document.

	The core Shipment model owns identity and document lifecycle. Provider apps
	may add state to the rows, but must not replace this carrier-neutral choice.
	"""
	rows = list(rows or [])
	if not include_cancelled:
		rows = [row for row in rows if int(row.get("docstatus") or 0) != 2]
	if not rows:
		return None

	active = [row for row in rows if int(row.get("docstatus") or 0) != 2]
	candidates = active or rows
	booked = [row for row in candidates if row.get("shipment_id") or row.get("awb_number")]
