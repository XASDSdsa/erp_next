const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(`${__dirname}/shipment.js`, "utf8");

function harness(contents, values = {}) {
	const handlers = {};
	const bindings = new Map();
	const dialogs = [];
	const host = {
		content: "",
		html(value) { this.content = value; },
		append(value) { this.content += value; },
		find(selector) {
			return { on(event, callback) { bindings.set(`${selector}:${event}`, callback); } };
		},
	};
	const getDocfield = () => ({ original: true });
	const locals = { "Sales Order Item": { ORIGINAL: { item_code: "original" } } };
	const context = {
		__: (value) => value,
		format_currency: (value, currency) => `${Number(value).toFixed(2)} ${currency}`,
		locals,
		erpnext: {},
		$: (markup) => ({
			appendTo(target) { target.append(markup); return this; },
			find: host.find,
		}),
		frappe: {
			meta: { get_docfield: getDocfield },
			utils: { escape_html: (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;") },
			call() { assert.fail("Rendering Shipment contents must not query a carrier or fetch another document"); },
			model: { with_doctype() { assert.fail("Read-only contents must not create a child-table control"); } },
			ui: {
				form: { on(doctype, events) { handlers[doctype] = events; }, make_control() { assert.fail("No synthetic child table"); } },
				Dialog: class {
					constructor(options) {
						this.options = options;
						this.fields_dict = { picture: { $wrapper: { ...host, content: "" } } };
						dialogs.push(this);
					}
					show() { this.shown = true; }
				},
			},
		},
	};
	vm.createContext(context);
	vm.runInContext(source, context);
	const frm = {
		doc: {
			name: "SHIP-1", service_provider: "DHL", __unsaved: 0,
			shipment_delivery_note: (contents || []).map((group) => ({ delivery_note: group.delivery_note })),
			__onload: { shipment_contents: contents }, ...values,
		},
		fields_dict: { shipment_contents: { $wrapper: host } },
		events: handlers.Shipment,
	};
	return { frm, host, context, bindings, dialogs, getDocfield, locals };
}

const grouped = () => [{
	delivery_note: "DN-1", currency: "USD", warehouse: "Main Warehouse",
	items: [{ item_code: "ITEM-1", item_name: "Item <one>", qty: 2, uom: "Nos", rate: 12.5, amount: 25 }],
}, {
	delivery_note: "DN-2", currency: "CNY", warehouse: "Other Warehouse",
	items: [{ item_code: "ITEM-2", item_name: "Item two", qty: 1, uom: "Box", rate: 0, amount: 0 }],
}];

test("native contents render grouped Delivery Note items for every carrier without mutating records or metadata", () => {
	for (const service_provider of ["DHL", "SF International", "其他物流（手工登记）"]) {
		const h = harness(grouped(), { service_provider });
		const before = JSON.stringify(h.frm.doc);
		const localBefore = JSON.stringify(h.locals);
		h.frm.events.render_shipment_contents(h.frm);
		assert.equal((h.host.content.match(/<table /g) || []).length, 2);
		for (const value of ["DN-1", "DN-2", "Main Warehouse", "Other Warehouse", "25.00 USD", "0.00 CNY", "Item &lt;one&gt;"]) {
			assert.ok(h.host.content.includes(value));
		}
		assert.doesNotMatch(h.host.content, /<one>|shipment-sticker-review|sf-goods|Sales Order Item/);
		assert.equal(JSON.stringify(h.frm.doc), before);
		assert.equal(JSON.stringify(h.locals), localBefore);
		assert.equal(h.context.frappe.meta.get_docfield, h.getDocfield);
	}
});

test("linked-note changes never display the old note's contents or fetch pending data", () => {
	const h = harness(grouped());
	h.frm.doc.shipment_delivery_note = [{ delivery_note: "DN-2" }, { delivery_note: "DN-NEW" }];
	h.frm.events.render_shipment_contents(h.frm);
	assert.doesNotMatch(h.host.content, /DN-1|ITEM-1|25.00 USD/);
	assert.match(h.host.content, /DN-2/);
	assert.match(h.host.content, /Save or reload/);
	h.frm.doc.shipment_delivery_note = [];
	h.frm.events.render_shipment_contents(h.frm);
	assert.match(h.host.content, /No goods linked/);
	assert.doesNotMatch(h.host.content, /<table|shipment-sticker-review/);
});

test("permission restrictions replace existing item and sticker details with an escaped explanation", () => {
	const h = harness(grouped());
	h.frm.events.render_shipment_contents(h.frm);
	h.frm.doc.__onload.shipment_contents_restricted = { message: "No access <secret>" };
	h.frm.events.render_shipment_contents(h.frm);
	assert.match(h.host.content, /No access &lt;secret&gt;/);
	assert.doesNotMatch(h.host.content, /ITEM-1|DN-1|<table|<img|No goods linked/);
});

test("stickers remain independent cards linked to all their products and open a full-size preview", () => {
	const contents = grouped();
	const sticker = {
		item_code: "STICKER-1", item_name: "Sticker <one>", model: "MODEL-1", version: "V2", image_status: "ready",
		image_url: "/private/files/sticker.png", thumbnail_url: "/private/files/sticker-thumb.png",
	};
	contents[0].items[0].stickers = [sticker];
	contents[1].items[0].stickers = [{ ...sticker }];
	const h = harness(contents);
	h.frm.events.render_shipment_contents(h.frm);
	assert.equal((h.host.content.match(/<article class="shipment-sticker-card"/g) || []).length, 1);
	assert.match(h.host.content, /Sticker &lt;one&gt;/);
	assert.match(h.host.content, /MODEL-1/);
	assert.match(h.host.content, /V2/);
	assert.match(h.host.content, /Item &lt;one&gt;、Item two/);
	assert.match(h.host.content, /src="\/private\/files\/sticker-thumb.png"/);
	assert.ok(h.host.content.indexOf("shipment-sticker-review") > h.host.content.lastIndexOf("</table>"));
	h.bindings.get("[data-sticker-index]:click").call({ dataset: { stickerIndex: "0" } });
	assert.equal(h.dialogs[0].shown, true);
	assert.match(h.dialogs[0].fields_dict.picture.$wrapper.content, /src="\/private\/files\/sticker.png"/);
	assert.ok(h.bindings.has(".shipment-sticker-thumb img:error"));
	assert.ok(h.bindings.has("img:error"));
});

test("restricted, missing and invalid sticker images never emit an image request", () => {
	for (const [image_status, image_url, label] of [
		["restricted", "/private/files/hidden.png", "无权查看图片"],
		["missing", "", "缺少贴纸图片"],
		["unavailable", "https://example.com/image.png", "图片不可用"],
		["ready", "/files/%2e%2e/secret.png", "缺少贴纸图片"],
	]) {
		const contents = grouped();
		contents[0].stickers = [{ item_code: "STICKER-1", image_status, image_url }];
		const h = harness(contents);
		h.frm.events.render_shipment_contents(h.frm);
		assert.ok(h.host.content.includes(label));
		assert.doesNotMatch(h.host.content, /<img|data-sticker-index=/);
	}
});
