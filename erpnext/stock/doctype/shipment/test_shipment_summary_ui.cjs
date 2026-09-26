const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../../..");
const handlers = [];
const noop = () => {};
const ctx = {
	console, document: {}, window: { innerWidth: 1440 }, __: (s, args = []) => s.replace(/\{(\d+)\}/g, (_, i) => args[i]),
	cint: (v) => Number(v) || 0, flt: (v) => Number(v) || 0,
	$: () => ({ trigger: noop, ready: noop }), extend_cscript: noop,
	cur_frm: { add_fetch: noop, cscript: {} },
	erpnext: {
		accounts: { taxes: { setup_tax_filters: noop, setup_tax_validations: noop } },
		sales_common: { setup_selling_controller: noop }, selling: { SellingController: class {} },
	},
	frappe: {
		ui: { form: { on: (dt, events) => { if (dt === "Delivery Note" && events.refresh) handlers.push(events.refresh); } } },
		listview_settings: {}, tour: {}, boot: { desk_settings: {} },
		call: () => { throw Error("Initial render must not issue a second shipping request"); },
		model: { can_create: () => false, std_fields_list: ["name", "owner", "creation", "modified"] },
		utils: {
			escape_html: (v) => String(v).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
			get_form_link: (dt, name) => `/desk/shipment/${encodeURIComponent(name)}`,
		},
		after_ajax: noop, run_serially: (tasks) => { ctx.rendered = tasks.reduce((p, fn) => p.then(fn), Promise.resolve()); },
		provide(name) { name.split(".").reduce((obj, key) => obj[key] ||= {}, ctx); },
	},
};
vm.createContext(ctx);
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
vm.runInContext(read("public/js/utils/shipment_summary.js"), ctx);
vm.runInContext(read("stock/doctype/delivery_note/delivery_note.js"), ctx);
vm.runInContext(read("stock/doctype/delivery_note/delivery_note_list.js"), ctx);
assert.equal(handlers.length, 1, "exactly one native refresh handler owns the summary");
const shipment = { shipment: "S-1", shipment_id: "OTHER123", docstatus: 1, status: "Booked", tracking_status: "In Progress", tracking_status_info: "At depot" };
const info = { shipments: [shipment], current_shipment: "S-1" };
const messages = [], buttons = [];
const frm = {
	doc: { docstatus: 1, __onload: { shipping_summary: info } }, is_new: () => false,
	layout: { attach_doc_and_docfields: noop, show_message: (text, color) => text ? messages.push({ text, color }) : messages.splice(0) },
	add_custom_button: (label) => buttons.push(label),
};
handlers[0](frm);
assert.equal(messages.length, 1);
assert.match(messages[0].text, /OTHER123/);
assert.equal(buttons[0], "View Shipment");
const api = ctx.erpnext.shipment_summary;
const list = ctx.frappe.listview_settings["Delivery Note"];
assert.match(list.formatters.shipment(null, {}, { _shipping: info }), /OTHER123/);
const restricted = { ...info, restricted_current: true };
assert.doesNotMatch(list.formatters.shipment(null, {}, { _shipping: restricted }), /S-1|OTHER123|href=/);
assert.equal(api.get({ shipments: [], current_shipment: null }).label, "No Shipment created");
assert.equal(api.get({ shipments: [{ ...shipment, docstatus: 2 }], current_shipment: "S-1" }).cancelled, true);
assert.equal(api.get({ ...info, source_cancelled: true }).color, "red");
const enriched = { shipments: [{ ...shipment, status_label: "Carrier cancellation pending", status_color: "orange", extra_details: "Carrier note" }], current_shipment: "S-1" };
assert.match(api.get(enriched).details, /Carrier note/);
assert.equal(api.get(enriched).color, "orange");
const malicious = { shipments: [{ ...shipment, shipment_id: '<img src=x onerror="bad()">' }], current_shipment: "S-1" };
assert.doesNotMatch(list.formatters.shipment(null, {}, { _shipping: malicious }), /<img/);
const report = { view_name: "Report", method: "frappe.desk.reportview.get" };
list.onload(report);
assert.equal(report.method, "frappe.desk.reportview.get");
assert.equal(list.additional_columns[0].fieldname, "shipment");

async function nativeLifecycle() {
	const sourceRoot = process.env.FRAPPE_JS_ROOT;
	assert.ok(sourceRoot, "Set FRAPPE_JS_ROOT to the installed framework public/js/frappe directory");
	const framework = (file) => fs.readFileSync(path.join(sourceRoot, file), "utf8").replace(/^import .*;\n/gm, "");
	vm.runInContext(framework("form/form.js"), ctx);
	Object.assign(frm, {
		meta: {}, page: { sidebar: { hide: noop } }, cscript: {}, $wrapper: { trigger: noop },
		refresh_header() { buttons.splice(0); }, refresh_fields: noop, run_after_load_hook: noop,
		dashboard: { after_refresh: noop }, configure_breadcrumb_width: noop,
		script_manager: { trigger(event) { if (event === "refresh") handlers.forEach((fn) => fn(frm)); } },
	});
	for (let n = 0; n < 3; n++) {
		ctx.frappe.ui.form.Form.prototype.render_form.call(frm, false);
		await ctx.rendered;
		assert.equal(messages.length, 1, "native render clears and renders a single summary");
		assert.equal(buttons.length, 1);
	}
	ctx.frappe.views = { BaseList: class {} };
	ctx.frappe.meta = { get_docfield: () => undefined };
	ctx.frappe.has_indicator = () => true;
	ctx.frappe.is_mobile = () => false;
	vm.runInContext(framework("list/list_view.js"), ctx);
	const native = Object.create(ctx.frappe.views.ListView.prototype);
	Object.assign(native, {
		doctype: "Delivery Note", settings: list, meta: { fields: [] }, list_view_settings: {},
		max_number_of_fields: 50, get_fields_in_list_view: () => [], column_max_widths: {},
	});
	native.setup_columns();
	assert.equal(native.columns.filter((col) => col.df?.fieldname === "shipment").length, 1);
	const columns = native.columns;
	list.onload(native);
	assert.equal(native.columns, columns, "onload does not replace/append columns after layout");
	const widths = {};
	native.$result = { find: () => ({ css: noop, filter(predicate) {
		const fieldname = native.columns.map((col) => col.df?.fieldname).find((name) => name && predicate(0, { dataset: { fieldname: name } }));
		return { css(value) { widths[fieldname] = value; } };
	} }) };
	native.apply_column_widths();
	assert.equal(widths.shipment.width, 220);
	native.list_view_settings.fields = JSON.stringify([{ fieldname: "shipment", width: 310 }, { fieldname: "status_field" }]);
	native.setup_columns();
	native.apply_column_widths();
	assert.equal(widths.shipment.width, 310);
	native.list_view_settings.fields = JSON.stringify([{ fieldname: "status_field" }]);
	native.setup_columns();
	list.onload(native);
	assert.equal(native.columns.some((col) => col.df?.fieldname === "shipment"), false);
	console.log("Native Shipment summary: first render, refresh, list layout, permissions, non-SF/SF and escaping passed");
}
nativeLifecycle().catch((err) => { console.error(err); process.exitCode = 1; });
