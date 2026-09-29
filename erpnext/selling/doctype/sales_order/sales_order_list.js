frappe.listview_settings["Sales Order"] = {
	add_fields: [
		"base_grand_total",
		"customer_name",
		"currency",
		"delivery_date",
		"per_delivered",
		"per_billed",
		"status",
		"advance_payment_status",
		"order_type",
		"name",
		"skip_delivery_note",
	],
	get_indicator: function (doc) {
		if (doc.status === "Closed") {
			// Closed
			return [__("Closed"), "green", "status,=,Closed"];
		} else if (doc.status === "On Hold") {
			// on hold
			return [__("On Hold"), "orange", "status,=,On Hold"];
		} else if (doc.status === "Completed") {
			return [__("Completed"), "green", "status,=,Completed"];
		} else if (doc.advance_payment_status === "Requested") {
			return [__("To Pay"), "gray", "advance_payment_status,=,Requested"];
		} else if (!doc.skip_delivery_note && flt(doc.per_delivered) < 100) {
			if (frappe.datetime.get_diff(doc.delivery_date) < 0) {
				// not delivered & overdue
				return [
					__("Overdue"),
					"red",
					"per_delivered,<,100|delivery_date,<,Today|status,!=,Closed|docstatus,=,1",
				];
			} else if (flt(doc.grand_total) === 0) {
				// not delivered (zeroount order)
				return [
					__("To Deliver"),
					"orange",
					"per_delivered,<,100|grand_total,=,0|status,!=,Closed|docstatus,=,1",
				];
			} else if (flt(doc.per_billed) < 100) {
				// not delivered & not billed
				return [
					__("To Deliver and Bill"),
					"orange",
					"per_delivered,<,100|per_billed,<,100|status,!=,Closed",
				];
			} else {
				// not billed
				return [__("To Deliver"), "orange", "per_delivered,<,100|per_billed,=,100|status,!=,Closed"];
			}
		} else if (
			flt(doc.per_delivered) === 100 &&
			flt(doc.grand_total) !== 0 &&
			flt(doc.per_billed) < 100
		) {
			// to bill
			return [__("To Bill"), "orange", "per_delivered,=,100|per_billed,<,100|status,!=,Closed"];
		} else if (doc.skip_delivery_note && flt(doc.per_billed) < 100) {
			return [__("To Bill"), "orange", "per_billed,<,100|status,!=,Closed"];
		}
	},
	onload: function (listview) {
		var method = "erpnext.selling.doctype.sales_order.sales_order.close_or_unclose_sales_orders";

		listview.page.add_action_item(__("Close"), function () {
			listview.call_for_selected_items(method, { status: "Closed" });
		});

		listview.page.add_action_item(__("Re-open"), function () {
			listview.call_for_selected_items(method, { status: "Submitted" });
		});

		if (frappe.model.can_create("Sales Invoice")) {
			listview.page.add_action_item(__("Sales Invoice"), () => {
				erpnext.bulk_transaction_processing.create(listview, "Sales Order", "Sales Invoice");
			});
		}

		if (frappe.model.can_create("Delivery Note")) {
			listview.page.add_action_item(__("Delivery Note"), () => {
				frappe.call({
					method: "erpnext.selling.doctype.sales_order.sales_order.is_enable_cutoff_date_on_bulk_delivery_note_creation",
					callback: (r) => {
						if (r.message) {
							var dialog = new frappe.ui.Dialog({
								title: __("Select Items up to Delivery Date"),
								fields: [
									{
										fieldtype: "Date",
										fieldname: "delivery_date",
										default: frappe.datetime.add_days(frappe.datetime.nowdate(), 1),
									},
								],
							});
							dialog.set_primary_action(__("Select"), function (values) {
								var until_delivery_date = values.delivery_date;
								erpnext.bulk_transaction_processing.create(
									listview,
									"Sales Order",
									"Delivery Note",
									{
										until_delivery_date,
									}
								);
								dialog.hide();
							});
							dialog.show();
						} else {
							erpnext.bulk_transaction_processing.create(
								listview,
								"Sales Order",
								"Delivery Note"
							);
						}
					},
				});
			});
		}

		if (frappe.model.can_create("Payment Entry")) {
			listview.page.add_action_item(__("Advance Payment"), () => {
				erpnext.bulk_transaction_processing.create(listview, "Sales Order", "Payment Entry");
			});
		}
	},
};

// Native Sales Order owns generic freight and advance-payment presentation.
// Carrier apps contribute summaries through the Shipment carrier adapter interface.
(function () {
	const settings = frappe.listview_settings["Sales Order"];
	settings.add_fields = [...new Set([
		...(settings.add_fields || []), "advance_paid", "rounded_total", "grand_total", "currency", "advance_payment_status",
	])];
	settings.formatters = settings.formatters || {};
	settings.formatters.advance_paid = function (value, df, doc) {
		const total = flt(doc.rounded_total || doc.grand_total);
		const paid = flt(value);
		const pct = total ? Math.min(100, Math.round((paid / total) * 100)) : 0;
		const paid_text = frappe.utils.escape_html(format_currency(paid, doc.currency));
		const bar_class = pct >= 100 ? "progress-bar-success" : paid > 0 ? "progress-bar-warning" : "";
		return `<div style="display:flex;align-items:center;gap:8px;min-width:0;white-space:nowrap;">
			<span style="flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;text-align:right;font-variant-numeric:tabular-nums;">${paid_text}</span>
			<div class="progress" style="margin:0;height:10px;flex:1 0 24px;min-width:24px;" title="${frappe.utils.escape_html(__("Advance Paid"))} ${paid_text} · ${pct}%">
				<div class="progress-bar ${bar_class}" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" style="width:${pct}%;"></div>
			</div>
		</div>`;
	};
	settings.formatters.shipment_freight = function (value) {
		const row = value || { text: __("Freight unavailable"), color: "gray", extra: "", title: "" };
		const escape = (text) => frappe.utils.escape_html(String(text || ""));
		const color = ["gray", "green", "blue", "orange", "red"].includes(row.color) ? row.color : "gray";
		const title = escape(row.title);
		const pill = `<span class="indicator-pill ${color} no-indicator-dot ellipsis" style="flex-shrink:0;max-width:100%;" title="${title}"><span class="ellipsis"> ${escape(row.text)}</span></span>`;
		const extra = row.extra
			? `<span class="shipment-freight-amount" style="font-size:var(--text-xs);color:var(--text-muted);flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escape(row.extra)}</span>`
			: "";
		return `<div class="shipment-freight-cell" style="display:flex;align-items:center;gap:6px;min-width:0;max-width:100%;white-space:nowrap;overflow:hidden;" title="${title}">${pill}${extra}</div>`;
	};
	settings.additional_columns = [
		...(settings.additional_columns || []),
		{ fieldname: "shipment_freight", label: "Freight", fieldtype: "Data", in_list_view: 1, insert_after: "status_field", width: 220 },
	];
	settings.method = "erpnext.stock.doctype.shipment.shipment_list_api.get_sales_orders";
	const native_onload = settings.onload;
	settings.onload = function (listview) {
		const result = native_onload?.call(this, listview);
		if (listview.view_name === "List") listview.method = settings.method;
		return result;
	};
})();
