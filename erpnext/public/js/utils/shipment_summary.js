// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.provide("erpnext.shipment_summary");

erpnext.shipment_summary = {
	get(info) {
		if (!info) return { label: __("Shipment information is unavailable"), color: "orange" };
		if (info.restricted_current || info.restricted) {
			return { label: __("No permission to view the linked Shipment"), color: "orange" };
		}
		const rows = info.shipments || [];
		const shipment = rows.find((row) => row.shipment === info.current_shipment);
		if (!shipment) return { label: __("No Shipment created"), color: "orange" };
		const cancelled = Number(shipment.docstatus) === 2;
		const label = cancelled
			? __("Cancelled")
			: __(shipment.status_label || shipment.status || "Submitted");
		const colors = { Draft: "orange", Submitted: "blue", Booked: "blue", Completed: "green" };
		const provider_color = ["blue", "green", "orange", "red", "yellow"].includes(shipment.status_color)
			? shipment.status_color
			: null;
		const color = cancelled || (info.source_cancelled && !cancelled)
			? "red"
			: provider_color || colors[shipment.status] || "blue";
		const waybill = shipment.shipment_id || shipment.awb_number || "";
		const details = [
			info.source_cancelled && !cancelled ? __("Delivery Note is cancelled but the Shipment is active") : "",
			cancelled ? __("Historical Shipment") : __("Shipment"),
			shipment.shipment,
			label,
			waybill,
			shipment.tracking_status ? __("Transport Status: {0}", [__(shipment.tracking_status)]) : "",
			shipment.tracking_status_info,
			shipment.extra_details,
			rows.length > 1 ? __("{0} other Shipments", [rows.length - 1]) : "",
		].filter(Boolean).join(" · ");
		return { shipment, cancelled, label, color, waybill, details };
	},

	show_on_delivery_note(frm) {
		if (frm.is_new() || frm.doc.is_return) return;
		const state = this.get(frm.doc.__onload?.shipping_summary);
		if (Number(frm.doc.docstatus) === 2 && !state.shipment) return;
		frm.layout.show_message(frappe.utils.escape_html(state.details || state.label), state.color);
		if (state.shipment) {
			frm.add_custom_button(__(state.cancelled ? "View Historical Shipment" : "View Shipment"), () => {
				frappe.set_route("Form", "Shipment", state.shipment.shipment);
			});
		}
	},

	format(value, df, doc) {
		const state = erpnext.shipment_summary.get(doc._shipping);
		const escape = frappe.utils.escape_html;
		const pill = `<span class="indicator-pill ${state.color} no-indicator-dot ellipsis">${escape(state.label)}</span>`;
		if (!state.shipment) return pill;
		const text = [state.waybill || state.shipment.shipment, __(state.shipment.tracking_status || "")]
			.filter(Boolean).join(" · ");
		const href = frappe.utils.get_form_link("Shipment", state.shipment.shipment);
		return `<a class="ellipsis" href="${escape(href)}" title="${escape(state.details)}">${pill} ${escape(text)}</a>`;
	},
};
