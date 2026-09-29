"""Compatibility projection into optional, legacy Delivery Note shipping fields."""

import frappe


def update_delivery_note(delivery_notes, shipment_info=None, tracking_info=None):
	"""Project carrier results without requiring a carrier app's Custom Fields.

	This internal helper retains the caller's existing authorization and db_set
	semantics. Native shipping summaries read Shipment as their source of truth;
	these optional fields only support installations with the legacy projection.
	"""
	values = {}
	if shipment_info:
		values.update(
			delivery_type="Parcel Service",
			parcel_service=shipment_info.get("carrier"),
			parcel_service_type=shipment_info.get("carrier_service"),
		)
	if tracking_info:
		values.update(
			tracking_number=tracking_info.get("awb_number"),
			tracking_url=tracking_info.get("tracking_url"),
			tracking_status=tracking_info.get("tracking_status"),
			tracking_status_info=tracking_info.get("tracking_status_info"),
		)
	if not values:
		return

	meta = frappe.get_meta("Delivery Note")
	values = {fieldname: value for fieldname, value in values.items() if meta.has_field(fieldname)}
	if not values:
		return
	for name in dict.fromkeys(delivery_notes):
		# One write also gives on_change a complete projection, not partial states.
		frappe.get_doc("Delivery Note", name).db_set(values)
