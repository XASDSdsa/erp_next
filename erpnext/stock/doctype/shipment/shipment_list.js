function shipment_status(value, color) {
	if (!value) return "";

	return `<span class="indicator-pill ${color} no-indicator-dot ellipsis">${frappe.utils.escape_html(
		value
	)}</span>`;
}

function interception_color(value) {
	return /(失败|异常|错误)/.test(value || "") ? "red" : "orange";
}

function replacement_color(value) {
	if (/^当前/.test(value || "")) return "green";
	if (/^(待替换|待启用|创建中)/.test(value || "")) return "orange";
	if (/^(已替换|已取消)/.test(value || "")) return "gray";
	return "gray";
}

frappe.listview_settings["Shipment"] = {
	add_fields: ["status"],
	formatters: {
		transport_status_display(value) {
			return shipment_status(value, "blue");
		},
		freight_status_display(value) {
			return shipment_status(value, "orange");
		},
		interception_status_display(value) {
			return shipment_status(value, interception_color(value));
		},
		label_replacement_display(value) {
			return shipment_status(value, replacement_color(value));
		},
	},
	get_indicator: function (doc) {
		if (doc.status == "Booked") {
			return [__("Booked"), "green"];
		}
	},
};
