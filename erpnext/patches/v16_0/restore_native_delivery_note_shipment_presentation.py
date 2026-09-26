# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


def execute():
	from erpnext.stock.doctype.shipment.shipment_summary import (
		migrate_delivery_note_shipment_presentation,
	)

	migrate_delivery_note_shipment_presentation()
