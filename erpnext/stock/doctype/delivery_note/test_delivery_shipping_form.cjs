const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(`${__dirname}/delivery_note.js`, "utf8");

function harness(state, changes = {}) {
	const handlers = {};
	const routes = [];
	const invalidated = [];
	const mappings = [];
	const host = {
		content: "", click: null,
		html(value) { this.content = value; this.click = null; },
		empty() { this.content = ""; this.click = null; },
		find() { return { on: (event, callback) => { this.click = callback; } }; },
	};
	const context = {
		__: (value) => value,
		cur_frm: { cscript: {}, add_fetch() {} },
		extend_cscript() {},
		erpnext: {
			stock: { delivery_note: {} },
			accounts: { dimensions: {}, taxes: { setup_tax_filters() {}, setup_tax_validations() {} } },
			sales_common: { setup_selling_controller() {} },
			selling: { SellingController: class {} },
		},
		frappe: {
			provide() {}, tour: {},
			ui: { form: { on(doctype, events) { handlers[doctype] = { ...handlers[doctype], ...events }; } } },
			utils: { escape_html: (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;") },
			model: {
				can_create: () => false,
				remove_from_locals: (...args) => invalidated.push(args),
				open_mapped_doc: (options) => mappings.push(options),
			},
			set_route: (...args) => routes.push(args),
			call() { assert.fail("Delivery Note overview must use document onload state, without another RPC"); },
		},
	};
	vm.createContext(context);
	vm.runInContext(source, context);
	const frm = {
		doc: { name: "DN-1", docstatus: 1, __onload: { shipping_state: state }, ...changes },
		fields_dict: { shipping_details: { $wrapper: host } },
		events: handlers["Delivery Note"],
		is_new() { return !!this.doc.__islocal; },
		is_dirty() { return !!this.doc.__unsaved; },
		toggle_display(field, visible) { this.visible = visible; },
	};
	return { frm, host, context, routes, invalidated, mappings };
}

test("Delivery Note renders all carriers through server-projected native Shipment information", () => {
	for (const provider of ["SF International", "DHL", "其他物流（手工登记）"]) {
		const h = harness({
			shipment: "SHIP-1", shipment_id: "001234", shipment_status: "Submitted", service_provider: provider,
			transport_status_display: "已交接货代", freight_status_display: "25 CNY · 人工登记",
		});
		h.frm.events.refresh(h.frm);
		assert.match(h.host.content, /SHIP-1/);
		assert.ok(h.host.content.includes(provider));
		assert.match(h.host.content, /001234/);
		assert.match(h.host.content, /已交接货代/);
		assert.match(h.host.content, /25 CNY · 人工登记/);
		assert.doesNotMatch(h.host.content, /data-sf|data-manual|Create SF|Print Label/);
		h.host.click();
		assert.deepEqual(h.routes, [["Form", "Shipment", "SHIP-1"]]);
		assert.deepEqual(h.invalidated, [["Delivery Note", "DN-1"]]);
		h.frm.doc.__onload.shipping_state.transport_status_display = "已送达";
		h.frm.events.refresh(h.frm);
		assert.match(h.host.content, /已送达/);
		assert.doesNotMatch(h.host.content, /已交接货代/);
	}
});

test("without a Shipment, the overview points to the existing native creation flow", () => {
	const h = harness({ shipment: null });
	h.frm.events.render_shipping_overview(h.frm);
	assert.match(h.host.content, /Create &gt; Shipment/);
	assert.match(h.host.content, /select a carrier/);
	assert.doesNotMatch(h.host.content, /<button|SF/);
	h.context.erpnext.stock.DeliveryNoteController.prototype.make_shipment.call({ frm: h.frm });
	assert.equal(h.mappings[0].method, "erpnext.stock.doctype.delivery_note.delivery_note.make_shipment");
	assert.equal(h.mappings[0].frm, h.frm);
});

test("permission and missing-state messages do not imply an absent Shipment or expose actions", () => {
	for (const state of [undefined, { restricted: true, message: "No access <secret>" }]) {
		const h = harness(state);
		h.frm.events.render_shipping_overview(h.frm);
		assert.doesNotMatch(h.host.content, /<button|No Shipment has been created|<secret>/);
		assert.match(h.host.content, state ? /No access &lt;secret&gt;/ : /Reload the Delivery Note/);
	}
});

test("unsubmitted, cancelled and return documents cannot create a carrier form", () => {
	for (const changes of [{ __islocal: 1, docstatus: 0 }, { docstatus: 0 }]) {
		const h = harness({ shipment: "SHIP-1" }, changes);
		h.frm.events.render_shipping_overview(h.frm);
		assert.match(h.host.content, /Submit the Delivery Note/);
		assert.doesNotMatch(h.host.content, /SHIP-1|<button/);
	}
	for (const changes of [{ docstatus: 2 }, { is_return: 1 }]) {
		const h = harness({ shipment: "SHIP-1" }, changes);
		h.frm.events.render_shipping_overview(h.frm);
		assert.equal(h.host.content, "");
		assert.equal(h.frm.visible, false);
	}
});

test("history remains readable and navigation preserves unsaved Delivery Note edits", () => {
	const h = harness({
		shipment: null,
		historical_shipment: { shipment: "OLD-SHIP", shipment_id: "OLD-WB", shipment_status: "Cancelled" },
	}, { __unsaved: 1 });
	h.frm.events.render_shipping_overview(h.frm);
	assert.match(h.host.content, /Historical Shipment/);
	assert.match(h.host.content, /OLD-WB/);
	assert.match(h.host.content, /Cancelled/);
	h.host.click();
	assert.deepEqual(h.routes, [["Form", "Shipment", "OLD-SHIP"]]);
	assert.deepEqual(h.invalidated, []);
});
