const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const handlers = {};
const context = {
	console,
	setTimeout,
	flt: (value) => Number(value) || 0,
	cint: (value) => Number(value) || 0,
	format_currency: (value, currency) => `${Number(value).toFixed(2)} ${currency}`,
	__ : (value) => value,
	frappe: {
		ui: { form: { on(doctype, events) { handlers[doctype] = events; } } },
		db: {},
		utils: { escape_html: (value) => String(value).replaceAll("<", "&lt;").replaceAll(">", "&gt;") },
		validated: true,
		msgprint() {},
	},
};
vm.createContext(context);
for (const [slug, doctype] of [["payment_entry", "Payment Entry"], ["sales_invoice", "Sales Invoice"]]) {
 const source = fs.readFileSync(`${__dirname}/../erpnext/accounts/doctype/${slug}/${slug}.js`, "utf8");
 const start = source.lastIndexOf(`frappe.ui.form.on("${doctype}", {`);
 assert.ok(start >= 0, "native form registration exists");
 vm.runInContext(source.slice(start), context, { filename: slug + ".js" });
}

async function paymentFixture({confirmAnswer = true, mutateDuringRead = false} = {}) {
	let reads = 0;
	context.frappe.db.get_doc = async () => ({ items: [] });
	context.frappe.db.get_value = async () => {
		reads += 1;
		if (mutateDuringRead) fixture.doc.references[0].allocated_amount = 99;
		return { message: { name: "SO-1", grand_total: 100, rounded_total: 100, advance_paid: 20, currency: "CNY" } };
	};
	context.frappe.confirm = (_message, yes, no) => (confirmAnswer ? yes() : no());
	const fixture = {
		doc: { payment_type: "Receive", party_type: "Customer", references: [{ reference_doctype: "Sales Order", reference_name: "SO-1", allocated_amount: 20 }], docstatus: 0 },
		events: handlers["Payment Entry"],
	};
	fixture.reads = () => reads;
	return { fixture };
}

(async () => {
	{
		const { fixture } = await paymentFixture();
		const first = fixture.events.leya_block_unconfirmed_extra_pay(fixture);
		const second = fixture.events.leya_block_unconfirmed_extra_pay(fixture);
		await Promise.all([first, second]);
		assert.equal(context.frappe.validated, true, "two saves should share one completed check");
		assert.equal(fixture.reads(), 1, "two saves should issue one balance read");
	}
	{
		const { fixture } = await paymentFixture({ confirmAnswer: false });
		context.frappe.validated = true;
		await fixture.events.leya_block_unconfirmed_extra_pay(fixture);
		assert.equal(context.frappe.validated, false, "cancelling either confirmation blocks save");
	}
	{
		const { fixture } = await paymentFixture({ mutateDuringRead: true });
		context.frappe.validated = true;
		await fixture.events.leya_block_unconfirmed_extra_pay(fixture);
		assert.equal(context.frappe.validated, false, "a changed reference cannot reuse a stale confirmation");
	}
	{
		let values = {
			"SO-CNY": { name: "SO-CNY", rounded_total: 100, advance_paid: 20, currency: "CNY" },
			"SO-USD": { name: "SO-USD", rounded_total: 100, advance_paid: 10, currency: "USD" },
		};
		context.frappe.db.get_value = async (_dt, name) => ({ message: values[name] });
		const frm = { doc: { items: [{ sales_order: "SO-USD" }, { sales_order: "SO-CNY" }] }, _leya_advance_banner_token: 0 };
		frm.events = handlers["Sales Invoice"];
		const rows = await frm.events.leya_load_order_pay(frm);
		assert.equal(JSON.stringify(rows.map((row) => [row.name, row.currency])), JSON.stringify([["SO-CNY", "CNY"], ["SO-USD", "USD"]]), "orders retain separate currencies");
	}
	console.log("PAYMENT_GUARDS_TEST_OK");
})().catch((error) => { console.error(error); process.exitCode = 1; });
