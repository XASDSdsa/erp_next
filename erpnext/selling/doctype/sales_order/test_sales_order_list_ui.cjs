const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(`${__dirname}/sales_order_list.js`, "utf8");

function harness() {
	const actions = [];
	const calls = [];
	const context = {
		__: (value) => value,
		flt: (value) => Number(value) || 0,
		format_currency: (value, currency) => `${Number(value).toFixed(2)} ${currency}`,
		frappe: {
			listview_settings: {},
			utils: { escape_html: (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;") },
			model: { can_create: () => false },
		},
	};
	vm.createContext(context);
	const boundary = source.indexOf("// Native Sales Order owns generic freight");
	vm.runInContext(source.slice(0, boundary), context);
	const settings = context.frappe.listview_settings["Sales Order"];
	const nativeIndicator = settings.get_indicator;
	const unrelated = () => "existing formatter";
	settings.formatters = { grand_total: unrelated };
	settings.additional_columns = [{ fieldname: "another_column" }];
	vm.runInContext(source.slice(boundary), context);
	const list = {
		view_name: "List", page: { add_action_item(label, callback) { actions.push({ label, callback }); } },
		call_for_selected_items(method, args) { calls.push({ method, args }); },
	};
	return { settings, actions, calls, list, nativeIndicator, unrelated };
}

test("freight extends native list settings, menus and unrelated formatters", () => {
	const h = harness();
	h.settings.onload(h.list);
	assert.deepEqual(h.actions.map((action) => action.label), ["Close", "Re-open"]);
	h.actions[0].callback();
	assert.equal(h.calls[0].method, "erpnext.selling.doctype.sales_order.sales_order.close_or_unclose_sales_orders");
	assert.equal(h.calls[0].args.status, "Closed");
	assert.equal(h.list.method, "erpnext.stock.doctype.shipment.shipment_list_api.get_sales_orders");
	assert.equal(h.settings.get_indicator, h.nativeIndicator);
	assert.equal(h.settings.formatters.grand_total, h.unrelated);
	assert.ok(h.settings.add_fields.includes("skip_delivery_note"));
	assert.ok(h.settings.add_fields.includes("advance_paid"));
	assert.deepEqual(Array.from(h.settings.additional_columns, (column) => column.fieldname), ["another_column", "shipment_freight"]);
	const report = { ...h.list, view_name: "Report", method: "native-report" };
	h.settings.onload(report);
	assert.equal(report.method, "native-report");
});

test("generic freight cells render and escape presentation without interpreting carrier bill states", () => {
	const render = harness().settings.formatters.shipment_freight;
	const html = render({ text: "Manual recorded", color: "blue", extra: "12.00 CNY / 0.00 USD", title: "Manual does not imply accounting" });
	assert.match(html, /Manual recorded/);
	assert.match(html, /12.00 CNY \/ 0.00 USD/);
	assert.match(html, /indicator-pill blue/);
	assert.match(render({ text: '<img src=x>', color: 'red" onclick="bad', extra: "<secret>", title: '" onmouseover="bad' }), /&lt;img/);
	assert.doesNotMatch(render({ text: "text", color: 'red" onclick="bad' }), /onclick/);
	assert.match(render(null), /Freight unavailable/);
});

test("native advance-payment formatter preserves progress and rounded total behavior", () => {
	const render = harness().settings.formatters.advance_paid;
	assert.match(render(40, {}, { grand_total: 100, currency: "USD" }), /40.00 USD/);
	assert.match(render(40, {}, { grand_total: 100, currency: "USD" }), /aria-valuenow="40"/);
	assert.match(render(150, {}, { grand_total: 100, currency: "USD" }), /aria-valuenow="100"/);
	assert.match(render(50, {}, { rounded_total: 50, grand_total: 60, currency: "USD" }), /aria-valuenow="100"/);
	assert.doesNotMatch(render(40, {}, { grand_total: 100, currency: "USD" }), /240px|!important/);
});
