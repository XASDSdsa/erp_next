# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.model.document import Document
from frappe.utils import flt, get_time

from erpnext.accounts.party import get_party_shipping_address
from erpnext.stock.doctype.shipment import manual_shipping, shipment_contents, shipment_display, shipment_lifecycle


class Shipment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.stock.doctype.shipment_delivery_note.shipment_delivery_note import ShipmentDeliveryNote
		from erpnext.stock.doctype.shipment_parcel.shipment_parcel import ShipmentParcel

		amended_from: DF.Link | None
		awb_number: DF.Data | None
		carrier: DF.Data | None
		carrier_service: DF.Data | None
		delivery_address: DF.TextEditor | None
		delivery_address_name: DF.Link
		delivery_company: DF.Link | None
		delivery_contact: DF.TextEditor | None
		delivery_contact_email: DF.Data | None
		delivery_contact_name: DF.Link | None
		delivery_customer: DF.Link | None
		delivery_supplier: DF.Link | None
		delivery_to: DF.Data | None
		delivery_to_type: DF.Literal["Company", "Customer", "Supplier"]
		description_of_content: DF.SmallText
		freight_status_display: DF.Data | None
		interception_status_display: DF.Data | None
		label_replacement_display: DF.Data | None
		incoterm: DF.Link | None
		pallets: DF.Literal["No", "Yes"]
		parcel_template: DF.Link | None
		pickup: DF.Data | None
		pickup_address: DF.TextEditor | None
		pickup_address_name: DF.Link
		pickup_company: DF.Link | None
		pickup_contact: DF.TextEditor | None
		pickup_contact_email: DF.Data | None
		pickup_contact_name: DF.Link | None
		pickup_contact_person: DF.Link | None
		pickup_customer: DF.Link | None
		pickup_date: DF.Date
		pickup_from: DF.Time
		pickup_from_type: DF.Literal["Company", "Customer", "Supplier"]
		pickup_supplier: DF.Link | None
		pickup_to: DF.Time
		pickup_type: DF.Literal["Pickup", "Self delivery"]
		service_provider: DF.Data | None
		shipment_amount: DF.Currency
		shipment_delivery_note: DF.Table[ShipmentDeliveryNote]
		shipment_id: DF.Data | None
		shipment_parcel: DF.Table[ShipmentParcel]
		shipment_type: DF.Literal["Goods", "Documents"]
		status: DF.Literal["Draft", "Submitted", "Booked", "Cancelled", "Completed"]
		total_weight: DF.Float
		tracking_status: DF.Literal["", "In Progress", "Delivered", "Returned", "Lost"]
		transport_status_display: DF.Data | None
		tracking_status_info: DF.Data | None
		tracking_url: DF.SmallText | None
		value_of_goods: DF.Currency
	# end: auto-generated types

	def onload(self):
		shipment_contents.load_shipment_contents(self)

	def on_discard(self):
		self.db_set("status", "Cancelled")

	def validate(self):
		self.validate_shipping_details()
		self.validate_weight()
		self.validate_pickup_time()
		self.set_value_of_goods()
		self.set_total_weight()
		self.set_list_display_fields()
		if self.docstatus == 0:
			self.status = "Draft"

	def validate_shipping_details(self):
		manual_shipping.validate_shipment(self)
		shipment_lifecycle.validate_carrier_change(self)
		shipment_lifecycle.validate_shipment_links(self)

	def before_update_after_submit(self):
		self.validate_shipping_details()
		self.set_list_display_fields()

	def before_cancel(self):
		# Local document transitions never cancel or modify a carrier booking.
		# The carrier app owns its explicit booking/cancellation operations.
		shipment_lifecycle.validate_carrier_change(self)
		manual_shipping.before_cancel(self)

	def before_discard(self):
		shipment_lifecycle.validate_carrier_change(self)
		manual_shipping.before_cancel(self)

	def on_trash(self):
		manual_shipping.on_trash(self)

	def on_change(self):
		shipment_display.persist_display_fields(self)

	def on_submit(self):
		if not self.shipment_parcel:
			frappe.throw(_("Please enter Shipment Parcel information"))
		if self.value_of_goods == 0:
			frappe.throw(_("Value of goods cannot be 0"))
		self.db_set("status", "Submitted")

	def on_cancel(self):
		self.db_set("status", "Cancelled")

	def set_list_display_fields(self):
		shipment_display.set_display_fields(self)

	def validate_weight(self):
		for parcel in self.shipment_parcel:
			if flt(parcel.weight) <= 0:
				frappe.throw(_("Parcel weight cannot be 0"))

	def set_total_weight(self):
		self.total_weight = self.get_total_weight()

	def get_total_weight(self):
		return sum(flt(parcel.weight) * parcel.count for parcel in self.shipment_parcel if parcel.count > 0)

	def validate_pickup_time(self):
		if self.pickup_from and self.pickup_to and get_time(self.pickup_to) < get_time(self.pickup_from):
			frappe.throw(_("Pickup To time should be greater than Pickup From time"))

	def set_value_of_goods(self):
		value_of_goods = 0
		for entry in self.get("shipment_delivery_note"):
			value_of_goods += flt(entry.get("grand_total"))
		self.value_of_goods = value_of_goods if value_of_goods else self.value_of_goods


def on_doctype_update():
	# Keep a cancelled physical waybill reserved and protect concurrent saves.
	frappe.db.add_unique("Shipment", ["manual_waybill_key"], constraint_name="manual_carrier_waybill_unique")


@frappe.whitelist()
def get_address_name(ref_doctype, docname):
	# Return address name
	return get_party_shipping_address(ref_doctype, docname)


@frappe.whitelist()
def get_contact_name(ref_doctype, docname):
	# Return address name
	return get_default_contact(ref_doctype, docname)


@frappe.whitelist()
def get_company_contact(user: str):
	frappe.has_permission("User", "read", throw=True)

	contact = frappe.db.get_value(
		"User",
		user,
		[
			"first_name",
			"last_name",
			"email",
			"phone",
			"mobile_no",
			"gender",
		],
		as_dict=1,
	)
	if not contact.phone:
		contact.phone = contact.mobile_no
	return contact
