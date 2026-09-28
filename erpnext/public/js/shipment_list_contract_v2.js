(function () {
	const lv = (frappe.listview_settings["Shipment"] = frappe.listview_settings["Shipment"] || {});
	const shipment_list = (erpnext.shipment_list = erpnext.shipment_list || {});
	const adapters = (shipment_list.adapters = shipment_list.adapters || []);

	lv.add_fields = Array.from(new Set([
		...(lv.add_fields || []),
		"status", "service_provider", "carrier", "shipment_id", "awb_number", "tracking_status",
	]));
	lv.formatters = lv.formatters || {};
	lv.get_indicator = function (doc) {
		if (doc.status === "Booked") return [__("Booked"), "green"];
	};

	function escape(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	function transport_state(value) {
		const status = String(value || "").trim();
		const labels = {
			"In Progress": "运输中", Shipped: "已发货", Booked: "已下单", Delivered: "已送达",
			Returned: "已退回", Lost: "已丢失", "已发货": "已发货", "运输中": "运输中", "派送中": "派送中",
			"已揽收": "已揽收", "已签收": "已签收", "已退回": "已退回", "已丢失": "已丢失",
		};
		const colors = {
			"In Progress": "blue", Shipped: "blue", Booked: "blue", Delivered: "green", "已签收": "green",
			Returned: "orange", Lost: "red", "已退回": "orange", "已丢失": "red", "运输中": "blue",
			"派送中": "blue", "已揽收": "blue",
		};
		return { label: labels[status] || status, color: colors[status] || "gray" };
	}

	shipment_list.transport_state = transport_state;
	shipment_list.register_adapter = function (name, adapter) {
		if (!name || !adapter || typeof adapter.matches !== "function" || typeof adapter.summarize !== "function") return;
		const index = adapters.findIndex((entry) => entry.name === name);
		const entry = { name, ...adapter };
		if (index >= 0) adapters[index] = entry;
		else adapters.push(entry);
	};

	shipment_list.get_summary = function (doc) {
		const summary = {
			provider: doc.service_provider || doc.carrier || "",
			waybill: doc.shipment_id || doc.awb_number || "",
			transport_status: transport_state(doc.tracking_status),
			freight_status: null,
			interception_status: null,
			label_status: doc.shipment_id || doc.awb_number ? { label: "当前", color: "green" } : null,
		};
		for (const adapter of adapters) {
			if (!adapter.matches(doc)) continue;
			return { ...summary, ...(adapter.summarize(doc, summary) || {}) };
		}
		return summary;
	};

	function status_html(status) {
		if (!status?.label) return '<span class="text-muted">—</span>';
		return `<span class="indicator-pill ${escape(status.color || "gray")} no-indicator-dot ellipsis">${escape(status.label)}</span>`;
	}

	function label_html(value, df, doc) {
		const summary = shipment_list.get_summary(doc);
		const status = summary.label_status;
		if (!status?.label && !summary.waybill) return '<span class="text-muted">—</span>';
		return `<div class="shipment-label-cell"><span class="indicator-pill ${escape(status?.color || "gray")} no-indicator-dot">${escape(status?.label || "当前")}</span>${summary.waybill ? `<span class="shipment-waybill">${escape(summary.waybill)}</span>` : ""}</div>`;
	}

	lv.formatters.shipment_transport_status = (value, df, doc) => status_html(shipment_list.get_summary(doc).transport_status);
	lv.formatters.shipment_freight_status = (value, df, doc) => status_html(shipment_list.get_summary(doc).freight_status);
	lv.formatters.shipment_interception_status = (value, df, doc) => status_html(shipment_list.get_summary(doc).interception_status);
	lv.formatters.shipment_label_status = label_html;

	if (!document.getElementById("erpnext-shipment-list-css")) {
		const style = document.createElement("style");
		style.id = "erpnext-shipment-list-css";
		style.textContent =
			'.list-row-head [data-fieldname="shipment_transport_status"],.list-row [data-fieldname="shipment_transport_status"]{min-width:150px!important;flex:1 1 150px!important;width:150px!important;white-space:nowrap;}' +
			'.list-row-head [data-fieldname="shipment_freight_status"],.list-row [data-fieldname="shipment_freight_status"]{min-width:180px!important;flex:1 1 180px!important;width:180px!important;white-space:nowrap;}' +
			'.list-row-head [data-fieldname="shipment_label_status"],.list-row [data-fieldname="shipment_label_status"]{min-width:220px!important;flex:1 1 220px!important;width:220px!important;overflow:visible!important;}' +
			'.shipment-label-cell{display:flex;flex-direction:row;align-items:center;gap:6px;min-width:0;white-space:nowrap;font-size:var(--text-xs);}' +
			'.shipment-label-cell .indicator-pill{flex:0 0 auto;white-space:nowrap;}' +
			'.shipment-waybill{color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;max-width:100%;white-space:nowrap;}';
		document.head.appendChild(style);
	}

	function add_column(listview, fieldname, label) {
		if (!Array.isArray(listview.columns)) listview.columns = [];
		if (listview.columns.some((column) => column.type === "Field" && column.df?.fieldname === fieldname)) return;
		listview.columns.push({ type: "Field", df: { fieldname, fieldtype: "Data", label: __(label), in_list_view: 1 } });
	}

	function ensure_columns(listview) {
		if (!listview) return;
		add_column(listview, "shipment_transport_status", "运输状态");
		add_column(listview, "shipment_freight_status", "运费");
		add_column(listview, "shipment_interception_status", "拦截状态");
		add_column(listview, "shipment_label_status", "面单替换");
	}

	const previous_onload = lv.onload;
	lv.onload = function (listview) {
		if (typeof previous_onload === "function") previous_onload(listview);
		ensure_columns(listview);
		if (!listview.__shipment_columns_wrapped) {
			const original_setup_columns = listview.setup_columns;
			listview.setup_columns = function () {
				original_setup_columns.apply(this, arguments);
				ensure_columns(this);
			};
			listview.__shipment_columns_wrapped = true;
		}
		ensure_columns(listview);
		listview.render_header(true);
	};
})();
