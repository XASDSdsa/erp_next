const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(`${__dirname}/shipment.js`, "utf8");
const manualProvider = "其他物流（手工登记）";

function harness(values = {}, isNew = true) {
	const handlers = {};
	const calls = [];
	const queries = {};
	const properties = [];
	const userReads = [];
	const context = {
		erpnext: { utils: { get_address_display() {} } },
		__: (value) => value,
		cint: (value) => Number(value) || 0,
		$: () => ({ hide() {} }),
		frappe: {
			ui: { form: { on(doctype, events) { handlers[doctype] = events; } } },
			defaults: { get_user_default: () => "Default Company", get_global_default: () => "" },
			datetime: { get_today: () => "2026-09-29" },
			model: { scrub: (value) => value.toLowerCase().replaceAll(" ", "_") },
			session: { user: "operator@example.com" },
			utils: { escape_html: (value) => String(value).replaceAll("<", "&lt;").replaceAll(">", "&gt;") },
			db: {
				get_value(doctype, filters, fields, callback) {
					if (doctype === "User") userReads.push({ filters, callback });
					else callback({ name: "Company Address" });
					return Promise.resolve();
				},
			},
			call(options) {
				calls.push(options);
				if (options.method === "erpnext.stock.doctype.shipment.shipment.get_address_name") {
					options.callback({ message: "Company Address" });
				}
				return Promise.resolve();
			},
		},
	};
	vm.createContext(context);
	vm.runInContext(source, context);
	const frm = {
		doc: {
			name: "SHIP-1", docstatus: 0, pickup_from_type: "Company", delivery_to_type: "Customer",
			delivery_customer: "Customer A", ...values,
		},
		fields_dict: {},
		events: handlers.Shipment,
		is_new: () => isNew,
		async set_value(field, value) {
			const updates = typeof field === "object" ? field : { [field]: value };
			for (const [key, next] of Object.entries(updates)) {
				if (this.doc[key] === next) continue;
				this.doc[key] = next;
				await this.events[key]?.(this);
			}
		},
		trigger(event) { return this.events[event]?.(this); },
		set_query(field, ...args) { queries[field] = args.at(-1); },
		set_df_property(...args) {
			properties.push(args);
			if (this.fields_dict[args[0]]) this.fields_dict[args[0]].df[args[1]] = args[2];
		},
		remove_custom_button() {},
		dashboard: { set_headline() {} },
		page: { set_indicator() {} },
	};
	return { frm, context, calls, queries, properties, userReads };
}

test("new Shipment defaults belong to ERPNext for every carrier without fabricated parcel or value", async () => {
	for (const service_provider of ["", "DHL", manualProvider, "SF International"]) {
		const h = harness({ service_provider });
		await h.frm.events.onload(h.frm);
		assert.equal(h.frm.doc.pickup_company, "Default Company");
		assert.equal(h.frm.doc.pickup_date, "2026-09-29");
		assert.equal(h.frm.doc.pickup_contact_person, "operator@example.com");
		assert.equal(h.frm.doc.pickup_address_name, "Company Address");
		assert.equal(h.userReads.length, 1, "one native user event supplies the contact");
		assert.equal(h.frm.doc.value_of_goods, undefined);
		assert.equal(h.frm.doc.shipment_parcel, undefined);
		assert.equal(h.frm.doc.description_of_content, undefined);
	}
});

test("defaults preserve existing values and do not inject company details into supplier pickups", async () => {
	const h = harness({
		pickup_company: "Selected Company", pickup_date: "2026-10-01",
		pickup_address_name: "Selected Address", pickup_contact_person: "selected@example.com", pickup_contact: "Selected Contact",
		value_of_goods: 72, shipment_parcel: [{ weight: 2 }],
	});
	const before = JSON.stringify(h.frm.doc);
	await h.frm.events.set_new_shipment_defaults(h.frm);
	assert.equal(JSON.stringify(h.frm.doc), before);
	assert.equal(h.userReads.length, 0);
	const supplier = harness({ pickup_from_type: "Supplier" });
	await supplier.frm.events.set_new_shipment_defaults(supplier.frm);
	assert.equal(supplier.frm.doc.pickup_company, undefined);
	assert.equal(supplier.frm.doc.pickup_contact_person, undefined);
	const saved = harness({}, false);
	await saved.frm.events.set_new_shipment_defaults(saved.frm);
	assert.equal(saved.frm.doc.pickup_date, undefined);
});

test("manual address queries allow the actual recipient while other carriers keep native party filters", () => {
	const h = harness({ service_provider: manualProvider });
	h.frm.events.manual_shipping_address_queries(h.frm);
	assert.equal(h.queries.delivery_address_name().filters.disabled, 0);
	assert.equal(Object.keys(h.queries.delivery_contact_name()).length, 0);
	h.frm.doc.service_provider = "DHL";
	assert.equal(h.queries.delivery_address_name().filters.link_name, "Customer A");
	assert.equal(h.queries.delivery_contact_name().filters.link_doctype, "Customer");
});

test("manual waybill validation uses the ERPNext API and ignores responses for a changed carrier", () => {
	const h = harness({ service_provider: manualProvider, manual_carrier: "DHL", manual_waybill: "001234" });
	h.frm.events.manual_shipping_check_waybill(h.frm);
	assert.equal(h.calls.length, 1);
	assert.equal(h.calls[0].method, "erpnext.stock.doctype.shipment.manual_shipping.check_waybill");
	h.frm.doc.service_provider = "DHL";
	const count = h.properties.length;
	h.calls[0].callback({ message: { unusual: true, hint: "old hint" } });
	assert.equal(h.properties.length, count);
	h.frm.events.manual_shipping_check_waybill(h.frm);
	assert.equal(h.calls.length, 1);
});

test("native contact uses the selected user and keeps company data when optional phone is missing", () => {
	const h = harness({ pickup_company: "Selected Company", pickup_contact_person: "selected@example.com" });
	h.frm.events.set_company_contact(h.frm, "Pickup");
	assert.equal(h.userReads[0].filters.name, "selected@example.com");
	h.userReads[0].callback({ first_name: "<Selected>", email: "selected@example.com" });
	assert.equal(h.frm.doc.pickup_company, "Selected Company");
	assert.equal(h.frm.doc.pickup_contact, "&lt;Selected&gt;<br>selected@example.com");
	h.frm.events.set_company_contact(h.frm, "Delivery");
	h.userReads[1].callback({ full_name: "Operator", email: "operator@example.com", mobile_no: "123" });
	assert.equal(h.frm.doc.pickup_contact_person, "selected@example.com");
	assert.equal(h.frm.doc.delivery_contact, "Operator<br>operator@example.com<br>123");
});

test("contact responses from an earlier user or document cannot overwrite the active contact", () => {
	for (const change of [
		(h) => { h.frm.doc.pickup_contact_person = "another@example.com"; },
		(h) => { h.frm.doc = { ...h.frm.doc, name: "SHIP-2" }; },
	]) {
		const h = harness({ pickup_contact_person: "old@example.com", pickup_contact: "Current contact" });
		h.frm.events.set_company_contact(h.frm, "Pickup");
		change(h);
		h.userReads[0].callback({ full_name: "Old User" });
		assert.equal(h.frm.doc.pickup_contact, "Current contact");
	}
});

test("carrier capabilities control API ownership and native layout restores on carrier changes", () => {
	const h = harness({ service_provider: manualProvider }, false);
	for (const field of ["shipment_id", "tracking_status", "service_provider"]) {
		h.frm.fields_dict[field] = { df: { hidden: 0, read_only: 0 } };
	}
	assert.equal(h.frm.events.shipping_api_enabled(h.frm), false);
	h.frm.events.prepare_shipment_form(h.frm);
	assert.equal(h.frm.fields_dict.shipment_id.df.hidden, 1);
	assert.equal(h.frm.fields_dict.tracking_status.df.hidden, 1);
	assert.equal(h.frm.fields_dict.service_provider.df.read_only, 1);
	h.frm.doc.service_provider = "DHL";
	h.frm.events.prepare_shipment_form(h.frm);
	assert.equal(h.frm.events.shipping_api_enabled(h.frm), true);
	assert.equal(h.frm.fields_dict.shipment_id.df.hidden, 0);
	assert.equal(h.frm.fields_dict.tracking_status.df.hidden, 0);
	assert.equal(h.frm.fields_dict.service_provider.df.read_only, 0);
	h.context.erpnext.shipment.register_carrier("example", {
		matches: (doc) => doc.service_provider === "Example Carrier",
		owns_api_ui: true,
	});
	h.frm.doc.service_provider = "Example Carrier";
	assert.equal(h.frm.events.shipping_api_enabled(h.frm), false);
	h.frm.doc.service_provider = "SendCloud";
	assert.equal(h.frm.events.shipping_api_enabled(h.frm), true);
});
